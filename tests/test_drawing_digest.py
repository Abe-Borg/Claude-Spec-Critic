"""Construction-drawing digest: packing, request shape, runner, preflight.

Hermetic throughout — PDFs are built inline with ``pypdf.PdfWriter`` (the
same mechanism the oversized-fetched-PDF fixture in
``test_requirements_research.py`` uses), the streaming client is the scripted
stand-in pattern from that suite (routed by the chunk-manifest marker in the
user text block, since chunks run on worker threads), and the tokenizer /
count_tokens endpoints are monkeypatched so nothing downloads or dials out.

Phase-registration assertions live here too (rather than in
``test_token_budgets.py``) so the whole drawing-digest contract is pinned in
one file.
"""
from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from src.core.api_config import (
    DRAWING_DIGEST_MODEL_DEFAULT,
    DRAWING_DIGEST_OUTPUT_CAP,
    MODEL_HAIKU_45,
    MODEL_SONNET_5,
    PHASE_DRAWING_DIGEST,
    cache_policy_for,
    drawing_digest_max_tokens,
    effort_config_for,
    model_capabilities,
    phase_output_cap,
    thinking_config_for,
)
from src.core.pricing import estimate_request_cost
from src.input import drawing_digest as dd
from src.input.drawing_digest import (
    DIGEST_ATTACHMENT_LABEL,
    DigestChunk,
    DigestChunkPart,
    DrawingDigestError,
    DrawingFile,
    build_chunk_messages,
    build_chunk_user_text,
    build_digest_chunks,
    build_digest_system_prompt,
    digest_drawing_files,
    effective_page_cap,
    format_digest_confirm_message,
    preflight_digest_cost,
    run_drawing_digest,
    validate_drawing_files,
    wrapped_digest_block,
)
from tests.fixtures.fake_anthropic import FakeMessage, FakeTextBlock, FakeUsage


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _write_pdf(tmp_path: Path, name: str, pages: int) -> Path:
    path = tmp_path / name
    path.write_bytes(_make_pdf_bytes(pages))
    return path


def _drawing_file(name: str, pages: int) -> DrawingFile:
    data = _make_pdf_bytes(pages)
    return DrawingFile(path=Path(name), name=name, data=data, page_count=pages)


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._message


class _FakeMessagesAPI:
    def __init__(self, route):
        self._route = route
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        result = self._route(kwargs)
        if isinstance(result, Exception):
            raise result
        return _FakeStream(result)


class FakeDigestClient:
    """Scripted ``client.messages.stream`` stand-in (research-suite pattern)."""

    def __init__(self, route):
        self.messages = _FakeMessagesAPI(route)

    @property
    def calls(self) -> list[dict]:
        return self.messages.calls


def _user_text(kwargs: dict) -> str:
    """The instruction text block of a digest request (content is a list)."""
    for block in kwargs["messages"][0]["content"]:
        if isinstance(block, dict) and block.get("type") == "text":
            return block["text"]
    return ""


def _route_by_chunk_marker(script: dict[str, list], *, delays: dict[str, float] | None = None):
    """Route requests by a marker in the user text; each marker pops its list.

    Chunks run on worker threads, so routing keys off content, not call
    order. ``delays`` lets a test hold one chunk back to prove the merge is
    index-ordered rather than completion-ordered.
    """
    remaining = {marker: list(items) for marker, items in script.items()}

    def route(kwargs):
        text = _user_text(kwargs)
        for marker, items in remaining.items():
            if marker in text:
                if delays and marker in delays:
                    time.sleep(delays[marker])
                if not items:
                    raise AssertionError(f"script exhausted for marker {marker!r}")
                return items.pop(0)
        raise AssertionError(f"no route for user text: {text[:120]!r}")

    return route


def _digest_message(text: str, *, stop_reason: str = "end_turn", input_tokens: int = 100, output_tokens: int = 50) -> FakeMessage:
    return FakeMessage(
        content=[FakeTextBlock(text=text)],
        stop_reason=stop_reason,
        usage=FakeUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _LogCollector:
    def __init__(self):
        self.entries: list[tuple[str, str]] = []

    def __call__(self, msg, **kwargs):
        self.entries.append((str(msg), str(kwargs.get("level", "info"))))

    def messages(self, level: str | None = None) -> list[str]:
        return [m for m, lvl in self.entries if level is None or lvl == level]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_pdf_loads_with_page_count(self, tmp_path):
        path = _write_pdf(tmp_path, "plans.pdf", 3)
        files, errors = validate_drawing_files([path])
        assert errors == []
        assert len(files) == 1
        assert files[0].name == "plans.pdf"
        assert files[0].page_count == 3
        assert files[0].data == path.read_bytes()

    def test_missing_and_non_pdf_files_rejected(self, tmp_path):
        notes = tmp_path / "notes.txt"
        notes.write_text("not a drawing")
        files, errors = validate_drawing_files(
            [tmp_path / "ghost.pdf", notes]
        )
        assert files == []
        assert len(errors) == 2
        assert "ghost.pdf: file not found" in errors[0]
        assert "notes.txt: not a PDF" in errors[1]

    def test_corrupt_pdf_rejected_with_per_file_error(self, tmp_path):
        bad = tmp_path / "corrupt.pdf"
        bad.write_bytes(b"%PDF-1.4 this is not really a pdf")
        files, errors = validate_drawing_files([bad])
        assert files == []
        assert len(errors) == 1
        assert errors[0].startswith("corrupt.pdf:")

    def test_encrypted_pdf_rejected_with_per_file_error(self, tmp_path):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("hunter2")
        path = tmp_path / "locked.pdf"
        with path.open("wb") as fh:
            writer.write(fh)
        files, errors = validate_drawing_files([path])
        assert files == []
        assert len(errors) == 1
        assert "locked.pdf" in errors[0]
        assert "encrypted" in errors[0]


# ---------------------------------------------------------------------------
# Chunk packing
# ---------------------------------------------------------------------------


class TestChunkPacking:
    def test_single_small_pdf_yields_one_chunk(self):
        files = [_drawing_file("a.pdf", 3)]
        chunks = build_digest_chunks(files)
        assert len(chunks) == 1
        assert chunks[0].index == 0
        assert chunks[0].labels == ["a.pdf"]
        assert chunks[0].known_page_count == 3
        assert chunks[0].has_uncountable is False

    def test_greedy_packing_respects_page_cap(self):
        files = [_drawing_file(f"{n}.pdf", 4) for n in ("a", "b", "c")]
        # tokens_per_page=1 makes the window-derived cap enormous, so the
        # injected page cap is the binding constraint.
        chunks = build_digest_chunks(
            files, max_pages_per_request=8, tokens_per_page=1
        )
        assert [c.labels for c in chunks] == [["a.pdf", "b.pdf"], ["c.pdf"]]
        assert [c.known_page_count for c in chunks] == [8, 4]
        assert [c.index for c in chunks] == [0, 1]

    def test_byte_cap_starts_new_chunk(self):
        files = [_drawing_file("a.pdf", 2), _drawing_file("b.pdf", 2)]
        cap = len(files[0].data) + len(files[1].data) - 1
        chunks = build_digest_chunks(
            files, max_bytes_per_request=cap, tokens_per_page=1
        )
        assert [c.labels for c in chunks] == [["a.pdf"], ["b.pdf"]]
        assert all(c.raw_bytes <= cap for c in chunks)

    def test_oversized_file_splits_by_page_ranges_with_labels(self):
        files = [_drawing_file("big.pdf", 10)]
        chunks = build_digest_chunks(
            files, max_pages_per_request=4, tokens_per_page=1
        )
        labels = [label for chunk in chunks for label in chunk.labels]
        assert labels == [
            "big.pdf (pages 1-4)",
            "big.pdf (pages 5-8)",
            "big.pdf (pages 9-10)",
        ]
        # Nothing dropped: the parts' pages sum to the original, and each
        # part round-trips through pypdf with the advertised page count.
        parts = [part for chunk in chunks for part in chunk.parts]
        assert sum(p.page_count for p in parts) == 10
        for part in parts:
            assert len(PdfReader(io.BytesIO(part.data)).pages) == part.page_count

    def test_token_ceiling_derives_effective_page_cap_from_context_window(self):
        for model in (MODEL_SONNET_5, MODEL_HAIKU_45):
            caps = model_capabilities(model)
            expected = min(
                dd.API_MAX_PDF_PAGES_PER_REQUEST,
                (
                    caps.context_window
                    - drawing_digest_max_tokens(model=model)
                    - dd._DIGEST_PROMPT_OVERHEAD_TOKENS
                )
                // dd.DIGEST_TOKENS_PER_PAGE_ESTIMATE,
            )
            assert effective_page_cap(model=model) == expected
        # The window is the binding constraint on every current model: the
        # API's 600-page ceiling is never reachable at ~3k tokens/page.
        assert effective_page_cap(model=MODEL_SONNET_5) < dd.API_MAX_PDF_PAGES_PER_REQUEST
        assert effective_page_cap(model=MODEL_HAIKU_45) < effective_page_cap(
            model=MODEL_SONNET_5
        )

    def test_uncountable_pdf_is_unsplittable_single_chunk(self):
        uncountable = DrawingFile(
            path=Path("scan.pdf"),
            name="scan.pdf",
            data=b"%PDF- opaque",
            page_count=None,
        )
        files = [_drawing_file("a.pdf", 2), uncountable, _drawing_file("b.pdf", 2)]
        chunks = build_digest_chunks(files, tokens_per_page=1)
        by_labels = [c.labels for c in chunks]
        assert ["scan.pdf"] in by_labels
        scan_chunk = chunks[by_labels.index(["scan.pdf"])]
        assert scan_chunk.has_uncountable is True
        # The union of chunk parts equals the input set — nothing dropped.
        all_labels = [label for c in chunks for label in c.labels]
        assert sorted(all_labels) == ["a.pdf", "b.pdf", "scan.pdf"]


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class TestRequestShape:
    def _one_chunk(self, pages: int = 2) -> DigestChunk:
        return build_digest_chunks([_drawing_file("a.pdf", pages)])[0]

    def test_document_blocks_precede_text_block(self):
        chunk = self._one_chunk()
        messages = build_chunk_messages(chunk, total_chunks=1)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        content = messages[0]["content"]
        assert [b["type"] for b in content[:-1]] == ["document"] * len(chunk.parts)
        assert content[-1]["type"] == "text"

    def test_document_block_shape_base64_no_newlines_roundtrips(self):
        chunk = self._one_chunk()
        content = build_chunk_messages(chunk, total_chunks=1)[0]["content"]
        source = content[0]["source"]
        assert source["type"] == "base64"
        assert source["media_type"] == "application/pdf"
        assert "\n" not in source["data"]
        assert base64.b64decode(source["data"]) == chunk.parts[0].data

    def test_request_kwargs_use_phase_policy_no_tools_no_beta(self):
        kwargs = dd._build_request_kwargs(model=MODEL_SONNET_5)
        assert "tools" not in kwargs
        assert "extra_headers" not in kwargs
        assert "betas" not in kwargs
        assert kwargs["max_tokens"] == phase_output_cap(
            PHASE_DRAWING_DIGEST, model=MODEL_SONNET_5
        )
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "medium"}
        # System prompt carries the cache breakpoint (system-only policy).
        system = kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert system[0]["text"] == build_digest_system_prompt()

    def test_system_prompt_byte_identical_across_chunks(self):
        # The per-run bits (chunk manifest, module name) live in the user
        # text; the system prompt is the cache-stable protocol contract.
        assert build_digest_system_prompt() == build_digest_system_prompt()
        chunks = build_digest_chunks(
            [_drawing_file("a.pdf", 2), _drawing_file("b.pdf", 2)],
            max_pages_per_request=2,
            tokens_per_page=1,
        )
        texts = [
            build_chunk_user_text(c, total_chunks=len(chunks)) for c in chunks
        ]
        assert texts[0] != texts[1]

    def test_user_text_carries_manifest_and_module_display_name(self):
        chunk = self._one_chunk(pages=3)
        text = build_chunk_user_text(
            chunk, total_chunks=2, module_display_name="California K-12 (DSA) M&P"
        )
        assert "This is chunk 1 of 2" in text
        assert "- a.pdf: 3 page(s)" in text
        assert "California K-12 (DSA) M&P" in text
        assert "analyzed separately" in text
        # Single-chunk runs drop the multi-chunk caveat.
        solo = build_chunk_user_text(chunk, total_chunks=1)
        assert "analyzed separately" not in solo


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _two_chunks() -> list[DigestChunk]:
    return build_digest_chunks(
        [_drawing_file("a.pdf", 2), _drawing_file("b.pdf", 2)],
        max_pages_per_request=2,
        tokens_per_page=1,
    )


class TestRunner:
    def test_two_chunk_digest_merges_in_index_order_despite_completion_order(self):
        chunks = _two_chunks()
        client = FakeDigestClient(
            _route_by_chunk_marker(
                {
                    "chunk 1 of 2": [_digest_message("ALPHA DIGEST")],
                    "chunk 2 of 2": [_digest_message("BETA DIGEST")],
                },
                delays={"chunk 1 of 2": 0.1},  # chunk 2 completes first
            )
        )
        result = run_drawing_digest(chunks, client=client)
        assert result.completed_chunks == 2
        assert result.failed_chunks == 0
        alpha = result.digest_text.index("ALPHA DIGEST")
        beta = result.digest_text.index("BETA DIGEST")
        assert alpha < beta
        assert "--- Chunk 1 of 2: a.pdf ---" in result.digest_text
        assert "--- Chunk 2 of 2: b.pdf ---" in result.digest_text
        assert result.digest_text.startswith("CONSTRUCTION DRAWING DIGEST")

    def test_single_chunk_digest_has_no_chunk_headers(self):
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        client = FakeDigestClient(
            _route_by_chunk_marker({"chunk 1 of 1": [_digest_message("SOLO")]})
        )
        result = run_drawing_digest(chunks, client=client)
        assert "SOLO" in result.digest_text
        assert "--- Chunk" not in result.digest_text

    def test_partial_failure_keeps_completed_chunks_and_inlines_failure_note(self):
        chunks = _two_chunks()
        log = _LogCollector()
        client = FakeDigestClient(
            _route_by_chunk_marker(
                {
                    # ValueError classifies as non-retryable → immediate failure.
                    "chunk 1 of 2": [ValueError("boom")],
                    "chunk 2 of 2": [_digest_message("BETA DIGEST")],
                }
            )
        )
        result = run_drawing_digest(chunks, client=client, log=log)
        assert result.failed_chunks == 1
        assert result.completed_chunks == 1
        assert "[Chunk 1 of 2 (a.pdf) FAILED: ValueError: boom]" in result.digest_text
        assert "BETA DIGEST" in result.digest_text
        assert any("FAILED" in m for m in log.messages("warning"))

    def test_all_chunks_fail_raises_typed_error_with_per_chunk_errors(self):
        chunks = _two_chunks()
        client = FakeDigestClient(
            _route_by_chunk_marker(
                {
                    "chunk 1 of 2": [ValueError("alpha down")],
                    "chunk 2 of 2": [ValueError("beta down")],
                }
            )
        )
        with pytest.raises(DrawingDigestError) as excinfo:
            run_drawing_digest(chunks, client=client)
        message = str(excinfo.value)
        assert "alpha down" in message
        assert "beta down" in message

    def test_retryable_error_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(dd.time, "sleep", lambda _s: None)
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        client = FakeDigestClient(
            _route_by_chunk_marker(
                {
                    "chunk 1 of 1": [
                        # String-matching fallback classifies as CONNECTION.
                        RuntimeError("connection reset by peer"),
                        _digest_message("RECOVERED", input_tokens=222, output_tokens=33),
                    ]
                }
            )
        )
        result = run_drawing_digest(chunks, client=client)
        assert len(client.calls) == 2
        assert result.completed_chunks == 1
        assert "RECOVERED" in result.digest_text
        status = result.chunk_statuses[0]
        assert status.input_tokens == 222
        assert status.output_tokens == 33

    def test_max_tokens_marks_chunk_truncated_and_keeps_text_with_warning(self):
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        client = FakeDigestClient(
            _route_by_chunk_marker(
                {
                    "chunk 1 of 1": [
                        _digest_message("PARTIAL NOTES", stop_reason="max_tokens")
                    ]
                }
            )
        )
        result = run_drawing_digest(chunks, client=client)
        status = result.chunk_statuses[0]
        assert status.status == "truncated"
        assert result.truncated_chunks == 1
        assert result.completed_chunks == 1  # truncated still counts as usable
        assert "output hit the token cap" in result.digest_text
        assert "PARTIAL NOTES" in result.digest_text

    def test_unexpected_stop_reason_fails_chunk(self):
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        client = FakeDigestClient(
            _route_by_chunk_marker(
                {
                    "chunk 1 of 1": [
                        _digest_message("ignored", stop_reason="pause_turn")
                    ]
                }
            )
        )
        with pytest.raises(DrawingDigestError) as excinfo:
            run_drawing_digest(chunks, client=client)
        assert "pause_turn" in str(excinfo.value)

    def test_empty_text_response_fails_chunk(self):
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        client = FakeDigestClient(
            _route_by_chunk_marker(
                {"chunk 1 of 1": [FakeMessage(content=[], stop_reason="end_turn")]}
            )
        )
        with pytest.raises(DrawingDigestError) as excinfo:
            run_drawing_digest(chunks, client=client)
        assert "no text" in str(excinfo.value)

    def test_worker_exception_never_escapes_fanout(self, monkeypatch):
        def _explode(*_args, **_kwargs):
            raise RuntimeError("worker blew up outside the guarded path")

        monkeypatch.setattr(dd, "_run_digest_chunk", _explode)
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        client = FakeDigestClient(lambda kwargs: _digest_message("unused"))
        # The escape is converted into a failed chunk; with every chunk
        # failed the run raises the *typed* error, not the RuntimeError.
        with pytest.raises(DrawingDigestError) as excinfo:
            run_drawing_digest(chunks, client=client)
        assert "worker blew up" in str(excinfo.value)

    def test_diag_hook_receives_phase_and_usage(self):
        class _Diag:
            def __init__(self):
                self.calls = []

            def record_api_call(self, **kwargs):
                self.calls.append(kwargs)

        diag = _Diag()
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        client = FakeDigestClient(
            _route_by_chunk_marker({"chunk 1 of 1": [_digest_message("OK")]})
        )
        run_drawing_digest(chunks, client=client, diag=diag)
        assert len(diag.calls) == 1
        assert diag.calls[0]["phase"] == "drawing_digest"
        assert diag.calls[0]["input_tokens"] == 100


# ---------------------------------------------------------------------------
# Preflight / cost / wrapping
# ---------------------------------------------------------------------------


class TestPreflightAndCost:
    def test_preflight_uses_exact_count_when_available(self, monkeypatch):
        monkeypatch.setattr(
            dd, "count_tokens_via_api", lambda **_kw: 1_234
        )
        chunks = _two_chunks()
        preflight = preflight_digest_cost(chunks, model=MODEL_SONNET_5)
        assert preflight.exact is True
        assert preflight.per_chunk_input_tokens == [1_234, 1_234]
        assert preflight.total_input_tokens == 2_468
        assert preflight.max_output_tokens == DRAWING_DIGEST_OUTPUT_CAP * 2
        assert preflight.estimated_max_cost_usd == pytest.approx(
            estimate_request_cost(
                2_468, DRAWING_DIGEST_OUTPUT_CAP * 2, model=MODEL_SONNET_5
            )
        )
        assert preflight.over_window_chunk_indices == []

    def test_preflight_falls_back_to_local_page_estimate_flagged_inexact(
        self, monkeypatch
    ):
        monkeypatch.setattr(dd, "count_tokens_via_api", lambda **_kw: None)
        monkeypatch.setattr(dd, "count_tokens", lambda text: len(text.split()))
        chunks = build_digest_chunks([_drawing_file("a.pdf", 4)])
        preflight = preflight_digest_cost(chunks, model=MODEL_SONNET_5)
        assert preflight.exact is False
        prompt_text = (
            build_digest_system_prompt()
            + "\n"
            + build_chunk_user_text(chunks[0], total_chunks=1)
        )
        expected = 4 * dd.DIGEST_TOKENS_PER_PAGE_ESTIMATE + len(prompt_text.split())
        assert preflight.per_chunk_input_tokens == [expected]

    def test_preflight_flags_over_window_chunk(self, monkeypatch):
        window = model_capabilities(MODEL_SONNET_5).context_window
        monkeypatch.setattr(dd, "count_tokens_via_api", lambda **_kw: window)
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        preflight = preflight_digest_cost(chunks, model=MODEL_SONNET_5)
        assert preflight.over_window_chunk_indices == [0]

    def test_unknown_model_cost_is_none_not_a_guess(self, monkeypatch):
        monkeypatch.setattr(dd, "count_tokens_via_api", lambda **_kw: 1_000)
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        preflight = preflight_digest_cost(chunks, model="mystery-model-9")
        assert preflight.estimated_max_cost_usd is None
        message = format_digest_confirm_message(
            preflight, chunks=chunks, model="mystery-model-9"
        )
        assert "Cost: unknown" in message

    def test_confirm_message_contents(self, monkeypatch):
        monkeypatch.setattr(dd, "count_tokens_via_api", lambda **_kw: 10_000)
        chunks = _two_chunks()
        preflight = preflight_digest_cost(chunks, model=MODEL_SONNET_5)
        message = format_digest_confirm_message(
            preflight, chunks=chunks, model=MODEL_SONNET_5
        )
        assert "Sonnet 5" in message
        assert "2 document(s)" in message
        assert "4 page(s)" in message
        assert "Requests: 2" in message
        assert "20,000 input tokens" in message
        assert "(estimated)" not in message
        assert "Cost: up to $" in message
        assert "editable text digest" in message

    def test_confirm_message_marks_inexact_estimates(self, monkeypatch):
        monkeypatch.setattr(dd, "count_tokens_via_api", lambda **_kw: None)
        monkeypatch.setattr(dd, "count_tokens", lambda text: 10)
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        preflight = preflight_digest_cost(chunks, model=MODEL_SONNET_5)
        message = format_digest_confirm_message(
            preflight, chunks=chunks, model=MODEL_SONNET_5
        )
        assert "(estimated)" in message

    def test_wrapped_digest_block_uses_attachment_label(self):
        chunks = build_digest_chunks([_drawing_file("a.pdf", 2)])
        client = FakeDigestClient(
            _route_by_chunk_marker({"chunk 1 of 1": [_digest_message("BODY")]})
        )
        result = run_drawing_digest(chunks, client=client)
        wrapped = wrapped_digest_block(result)
        assert wrapped.startswith(
            f"--- BEGIN ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---"
        )
        assert wrapped.endswith(f"--- END ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---")
        # digest_text itself stays unwrapped — wrapping is the caller's move.
        assert "BEGIN ATTACHMENT" not in result.digest_text


# ---------------------------------------------------------------------------
# Headless convenience entry point
# ---------------------------------------------------------------------------


class TestDigestDrawingFiles:
    def test_headless_one_shot_validates_chunks_and_runs(self, tmp_path):
        path = _write_pdf(tmp_path, "plans.pdf", 2)
        client = FakeDigestClient(
            _route_by_chunk_marker({"chunk 1 of 1": [_digest_message("HEADLESS")]})
        )
        result = digest_drawing_files([path], client=client)
        assert "HEADLESS" in result.digest_text

    def test_no_usable_files_raises_with_joined_errors(self, tmp_path):
        with pytest.raises(DrawingDigestError) as excinfo:
            digest_drawing_files([tmp_path / "ghost.pdf"])
        assert "ghost.pdf: file not found" in str(excinfo.value)

    def test_invalid_files_are_skipped_with_warnings(self, tmp_path):
        good = _write_pdf(tmp_path, "plans.pdf", 2)
        log = _LogCollector()
        client = FakeDigestClient(
            _route_by_chunk_marker({"chunk 1 of 1": [_digest_message("OK")]})
        )
        result = digest_drawing_files(
            [good, tmp_path / "ghost.pdf"], client=client, log=log
        )
        assert "OK" in result.digest_text
        assert any("ghost.pdf" in m for m in log.messages("warning"))


# ---------------------------------------------------------------------------
# Phase registration (api_config contract)
# ---------------------------------------------------------------------------


class TestPhaseRegistration:
    def test_phase_registered_not_16k_fallback(self):
        assert (
            phase_output_cap(PHASE_DRAWING_DIGEST, model=MODEL_SONNET_5)
            == DRAWING_DIGEST_OUTPUT_CAP
            == 24_000
        )
        # The unknown-phase fallback is the conservative 16k verification
        # cap — the digest phase must not silently inherit it (invariant 9).
        assert phase_output_cap("not_a_phase", model=MODEL_SONNET_5) == 16_000

    def test_effort_medium_thinking_adaptive_cache_system_only(self):
        assert effort_config_for(
            model=DRAWING_DIGEST_MODEL_DEFAULT, phase=PHASE_DRAWING_DIGEST
        ) == {"effort": "medium"}
        assert thinking_config_for(
            model=DRAWING_DIGEST_MODEL_DEFAULT, phase=PHASE_DRAWING_DIGEST
        ) == {"type": "adaptive"}
        policy = cache_policy_for(PHASE_DRAWING_DIGEST)
        assert policy.cache_system is True
        assert policy.cache_tools is False

    def test_default_model_is_sonnet_5(self):
        # Holds when SPEC_CRITIC_DRAWING_DIGEST_MODEL is unset (harness env).
        assert DRAWING_DIGEST_MODEL_DEFAULT == MODEL_SONNET_5

    def test_model_env_override_in_subprocess(self):
        # The default is read at import time, so the override is pinned in a
        # subprocess instead of reloading api_config mid-session (a reload
        # would re-mint every constant other suites hold references to).
        env = {
            **os.environ,
            "SPEC_CRITIC_DRAWING_DIGEST_MODEL": "claude-opus-4-8",
        }
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from src.core.api_config import DRAWING_DIGEST_MODEL_DEFAULT; "
                "print(DRAWING_DIGEST_MODEL_DEFAULT)",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
            check=True,
        )
        assert out.stdout.strip() == "claude-opus-4-8"
