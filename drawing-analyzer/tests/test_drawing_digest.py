"""Vision-digest engine + orchestration tests.

The request-building and per-sheet digest tests use the dependency-free models
and a fake Anthropic client, so they run without PyMuPDF. The end-to-end
pipeline tests render a synthetic PDF and are skipped when PyMuPDF is absent.
"""
from __future__ import annotations

import base64
import threading
from pathlib import Path

import pytest

from drawing_analyzer.digest import (
    DIGEST_SYSTEM_PROMPT,
    SheetDigest,
    _clean_error,
    _is_transient_error,
    build_user_content,
    digest_sheet,
)
from drawing_analyzer.models import ImageTile, RenderedSheet, SheetRef
from tests.fixtures.fake_anthropic import FakeMessage, FakeTextBlock, FakeUsage

OPUS = "claude-opus-4-8"


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #


class _Msgs:
    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder(kwargs)


class _FakeClient:
    def __init__(self, responder):
        self.messages = _Msgs(responder)


def _make_sheet(rows: int = 2, cols: int = 2) -> RenderedSheet:
    ref = SheetRef(
        pdf_path=Path("M-101.pdf"),
        page_index=0,
        source_name="M-101.pdf",
        page_count=1,
    )
    overview = ImageTile(
        png_bytes=b"OVERVIEW", width_px=2000, height_px=1500, kind="overview"
    )
    tiles = [
        ImageTile(
            png_bytes=f"T{r}{c}".encode(),
            width_px=2000,
            height_px=1500,
            kind="tile",
            row=r,
            col=c,
            label=f"r{r}c{c}",
        )
        for r in range(rows)
        for c in range(cols)
    ]
    return RenderedSheet(
        ref=ref,
        overview=overview,
        tiles=tiles,
        page_width_pt=3168,
        page_height_pt=2448,
        rows=rows,
        cols=cols,
    )


def _make_pdf(pymupdf, path: Path, pages: int) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=792, height=612)
        page.insert_text((72, 72), f"SHEET M-10{i + 1} TEST")
    doc.save(str(path))
    doc.close()
    return path


# --------------------------------------------------------------------------- #
# build_user_content (pure)
# --------------------------------------------------------------------------- #


def test_build_user_content_orders_images_before_final_task():
    sheet = _make_sheet(rows=2, cols=2)  # overview + 4 tiles
    blocks = build_user_content(sheet)

    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 5  # overview + 4 tiles

    assert blocks[0]["type"] == "text"          # framing text first
    assert blocks[-1]["type"] == "text"         # task instruction last
    assert "digest" in blocks[-1]["text"].lower()

    # overview image round-trips through base64
    assert base64.standard_b64decode(images[0]["source"]["data"]) == b"OVERVIEW"

    texts = " ".join(b["text"] for b in blocks if b["type"] == "text")
    assert "Tile r1c1" in texts  # zero-based (0,0) renders as r1c1


# --------------------------------------------------------------------------- #
# digest_sheet (fake client)
# --------------------------------------------------------------------------- #


def test_digest_sheet_success_shape_and_request():
    resp = FakeMessage(
        content=[FakeTextBlock(text="Sheet M-101 - Mechanical - Plan\nVAV-3 served...")],
        usage=FakeUsage(input_tokens=1234, output_tokens=210),
    )
    client = _FakeClient(lambda kw: resp)

    sd = digest_sheet(_make_sheet(), client=client, model=OPUS)

    assert sd.ok
    assert "Sheet M-101" in sd.text
    assert sd.input_tokens == 1234
    assert sd.output_tokens == 210
    assert sd.image_token_estimate > 0
    assert sd.error is None

    kw = client.messages.calls[0]
    assert kw["model"] == OPUS
    assert kw["system"] == DIGEST_SYSTEM_PROMPT
    assert kw["thinking"] == {"type": "adaptive"}      # Opus supports adaptive
    assert kw["output_config"] == {"effort": "high"}
    assert kw["messages"][0]["role"] == "user"


def test_digest_sheet_captures_api_error_without_raising():
    def boom(_kw):
        raise RuntimeError("rate limited")

    sd = digest_sheet(_make_sheet(), client=_FakeClient(boom), model=OPUS)

    assert not sd.ok
    assert sd.error is not None and "rate limited" in sd.error
    assert sd.text == ""
    # estimate is computed before the call, so it survives a failure
    assert sd.image_token_estimate > 0


def test_digest_sheet_flags_empty_output():
    resp = FakeMessage(content=[], stop_reason="max_tokens")
    sd = digest_sheet(_make_sheet(), client=_FakeClient(lambda kw: resp), model=OPUS)
    assert not sd.ok
    assert sd.error is not None and "empty digest" in sd.error


# --------------------------------------------------------------------------- #
# Transient-error classification, sanitization, and retry (Workstream 0)
# --------------------------------------------------------------------------- #


class _StatusError(Exception):
    """Duck-typed stand-in for anthropic.APIStatusError (carries status_code)."""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


class APIConnectionError(Exception):
    """Name matches the SDK class the classifier recognizes by name."""


def test_is_transient_error_classification():
    assert _is_transient_error(_StatusError(502)) is True
    assert _is_transient_error(_StatusError(503)) is True
    assert _is_transient_error(_StatusError(429)) is True
    assert _is_transient_error(APIConnectionError("boom")) is True
    # Caller errors and unknown plain exceptions are NOT retried.
    assert _is_transient_error(_StatusError(400)) is False
    assert _is_transient_error(RuntimeError("nope")) is False


def test_clean_error_sanitizes_html_and_status():
    # The real 502 the operator saw: a full cloudflare HTML page.
    html = (
        "<html><head><title>502 Bad Gateway</title></head><body>"
        "<center><h1>502 Bad Gateway</h1></center><hr><center>cloudflare</center>"
        "</body></html>"
    )
    assert _clean_error(_StatusError(502, html)) == (
        "502 bad gateway (server temporarily unavailable — try again)"
    )
    assert "html" not in _clean_error(_StatusError(502, html)).lower()
    assert _clean_error(APIConnectionError("x")) == (
        "connection error (network/API unreachable — try again)"
    )
    # Unknown status renders cleanly; generic exceptions are tag-stripped.
    assert _clean_error(_StatusError(418)) == "HTTP 418"
    assert _clean_error(RuntimeError("<b>plain</b> message")) == "plain message"


def test_digest_sheet_retries_transient_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    def responder(_kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _StatusError(502, "<html>502</html>")
        return FakeMessage(content=[FakeTextBlock(text="recovered digest")])

    sd = digest_sheet(
        _make_sheet(), client=_FakeClient(responder), model=OPUS,
        max_retries=2, sleep=slept.append,
    )

    assert sd.ok and "recovered digest" in sd.text
    assert calls["n"] == 3            # 1 initial + 2 retries
    assert slept == [2.0, 4.0]        # exponential backoff between attempts


def test_digest_sheet_transient_exhausted_returns_clean_error():
    calls = {"n": 0}

    def responder(_kw):
        calls["n"] += 1
        raise _StatusError(502, "<html><title>502 Bad Gateway</title></html>")

    sd = digest_sheet(
        _make_sheet(), client=_FakeClient(responder), model=OPUS,
        max_retries=2, sleep=lambda _s: None,
    )

    assert not sd.ok
    assert sd.error == "502 bad gateway (server temporarily unavailable — try again)"
    assert calls["n"] == 3            # exhausted all attempts


def test_digest_sheet_does_not_retry_permanent_error():
    calls = {"n": 0}

    def responder(_kw):
        calls["n"] += 1
        raise _StatusError(400, "request exceeds the maximum allowed size")

    sd = digest_sheet(
        _make_sheet(), client=_FakeClient(responder), model=OPUS,
        max_retries=3, sleep=lambda _s: None,
    )

    assert not sd.ok
    assert calls["n"] == 1            # permanent error => no retry
    # A 4xx keeps its API message — a bare "HTTP 400" is exactly what hid the
    # real (request-size) cause that broke the inline-base64 path.
    assert sd.error == "HTTP 400: request exceeds the maximum allowed size"


# --------------------------------------------------------------------------- #
# pipeline (renders a synthetic PDF; needs PyMuPDF)
# --------------------------------------------------------------------------- #


def test_pipeline_combines_per_sheet_digests(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from drawing_analyzer.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=2)

    def responder(_kw):
        return FakeMessage(
            content=[FakeTextBlock(text="Sheet M-10X - Mechanical digest body")],
            usage=FakeUsage(input_tokens=500, output_tokens=80),
        )

    client = _FakeClient(responder)
    progress: list[tuple] = []
    ctx = extract_drawing_context(
        [path],
        client=client,
        rows=2,
        cols=2,
        progress=lambda d, t, label: progress.append((d, t, label)),
    )

    assert ctx.sheet_count == 2
    assert ctx.ok_sheet_count == 2
    assert ctx.file_count == 1
    assert ctx.errors == []
    assert "## Sheet 1/2" in ctx.combined_text
    assert "## Sheet 2/2" in ctx.combined_text
    assert "set.pdf" in ctx.combined_text
    assert "Mechanical digest body" in ctx.combined_text
    assert ctx.total_input_tokens == 1000          # 2 sheets * 500
    assert ctx.total_image_token_estimate > 0
    assert len(client.messages.calls) == 2          # one request per sheet
    assert progress[-1] == (2, 2, "Done")


def test_pipeline_records_per_sheet_error_and_continues(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from drawing_analyzer.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=2)

    # Serialize the counter so exactly one sheet fails regardless of which
    # worker thread gets the 2nd call (digests now run concurrently).
    state = {"n": 0}
    lock = threading.Lock()

    def responder(_kw):
        with lock:
            state["n"] += 1
            n = state["n"]
        if n == 2:
            raise RuntimeError("boom on sheet 2")
        return FakeMessage(content=[FakeTextBlock(text="ok digest")])

    ctx = extract_drawing_context(
        [path], client=_FakeClient(responder), rows=2, cols=2
    )

    assert ctx.sheet_count == 2
    assert ctx.ok_sheet_count == 1
    assert len(ctx.errors) == 1
    assert "boom on sheet 2" in ctx.errors[0]
    assert "[drawing analysis failed" in ctx.combined_text


def test_pipeline_serves_second_run_from_injected_cache(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from drawing_analyzer.digest_cache import DigestCache
    from drawing_analyzer.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=2)
    client = _FakeClient(
        lambda _kw: FakeMessage(
            content=[FakeTextBlock(text="digest body")],
            usage=FakeUsage(input_tokens=100, output_tokens=20),
        )
    )
    cache = DigestCache(None, persist=False)

    ctx1 = extract_drawing_context([path], client=client, rows=2, cols=2, cache=cache)
    assert ctx1.ok_sheet_count == 2 and ctx1.cached_sheet_count == 0
    assert ctx1.total_input_tokens == 200 and ctx1.total_output_tokens == 40
    assert ctx1.total_image_token_estimate > 0
    calls_after_first = len(client.messages.calls)
    assert calls_after_first == 2

    # Identical second run: every sheet is served from the cache, no new calls
    # and zero billed tokens reported for the run (the cost was already paid).
    ctx2 = extract_drawing_context([path], client=client, rows=2, cols=2, cache=cache)
    assert ctx2.ok_sheet_count == 2 and ctx2.cached_sheet_count == 2
    assert len(client.messages.calls) == calls_after_first
    assert ctx2.total_input_tokens == 0 and ctx2.total_output_tokens == 0
    assert ctx2.total_image_token_estimate == 0
    assert "digest body" in ctx2.combined_text


# --------------------------------------------------------------------------- #
# Parallel digest dispatch (Workstream 2a)
# --------------------------------------------------------------------------- #


def test_resolve_workers_arg_env_default(monkeypatch):
    from drawing_analyzer.pipeline import DEFAULT_DIGEST_WORKERS, _resolve_workers

    monkeypatch.delenv("DRAWING_ANALYZER_MAX_WORKERS", raising=False)
    assert _resolve_workers(None, 10) == DEFAULT_DIGEST_WORKERS
    assert _resolve_workers(2, 10) == 2
    assert _resolve_workers(8, 3) == 3       # capped at sheet count
    assert _resolve_workers(0, 10) == 1      # floored at 1
    assert _resolve_workers(5, 0) == 1       # no sheets -> still >= 1
    monkeypatch.setenv("DRAWING_ANALYZER_MAX_WORKERS", "6")
    assert _resolve_workers(None, 10) == 6
    monkeypatch.setenv("DRAWING_ANALYZER_MAX_WORKERS", "not-a-number")
    assert _resolve_workers(None, 10) == DEFAULT_DIGEST_WORKERS  # bad env -> default


def test_pipeline_digests_run_concurrently(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from drawing_analyzer.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=3)
    # The barrier only releases once all three digests are in flight at the same
    # time — a sequential pipeline would block on the first and time out.
    barrier = threading.Barrier(3, timeout=8)

    def responder(_kw):
        barrier.wait()
        return FakeMessage(content=[FakeTextBlock(text="concurrent ok")])

    ctx = extract_drawing_context(
        [path], client=_FakeClient(responder), rows=2, cols=2, max_workers=3
    )
    assert ctx.ok_sheet_count == 3  # all cleared the barrier => true concurrency


def test_pipeline_parallel_preserves_page_order(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from drawing_analyzer.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=5)
    client = _FakeClient(
        lambda _kw: FakeMessage(content=[FakeTextBlock(text="body")])
    )
    ctx = extract_drawing_context(
        [path], client=client, rows=2, cols=2, max_workers=4
    )
    headers = [l for l in ctx.combined_text.splitlines() if l.startswith("## Sheet ")]
    assert len(headers) == 5
    # Page order is preserved even though digests complete out of order.
    for k, header in enumerate(headers, start=1):
        assert header.startswith(f"## Sheet {k}/5:")


def test_pipeline_max_workers_one_processes_all(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from drawing_analyzer.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=3)
    client = _FakeClient(
        lambda _kw: FakeMessage(content=[FakeTextBlock(text="seq body")])
    )
    ctx = extract_drawing_context(
        [path], client=client, rows=2, cols=2, max_workers=1
    )
    assert ctx.ok_sheet_count == 3
    assert len(client.messages.calls) == 3
    for k in (1, 2, 3):
        assert f"## Sheet {k}/3:" in ctx.combined_text


def test_list_sheets_splits_pages(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from drawing_analyzer.render import list_sheets

    path = _make_pdf(pymupdf, tmp_path / "multi.pdf", pages=3)
    refs = list_sheets([path])

    assert len(refs) == 3
    assert refs[0].page_count == 3
    assert refs[1].page_index == 1
    assert refs[0].source_name == "multi.pdf"
