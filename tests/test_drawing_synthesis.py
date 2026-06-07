"""Cross-sheet synthesis tests (Workstream 3).

Hermetic: synthesis is a text-only call, so these build fake ``SheetDigest``
objects and a fake client — no PyMuPDF for the core tests. The pipeline
integration test renders a synthetic PDF and is skipped without PyMuPDF.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.api_config import REVIEW_MODEL_DEFAULT
from src.drawings.digest import DIGEST_SYSTEM_PROMPT, SheetDigest
from src.drawings.models import SheetRef
from src.drawings.synthesis import (
    SYNTHESIS_SYSTEM_PROMPT,
    SynthesisResult,
    build_synthesis_user_text,
    default_synthesis_model,
    synthesize_drawing_set,
)
from tests.fixtures.fake_anthropic import FakeMessage, FakeTextBlock, FakeUsage

OPUS = "claude-opus-4-8"


def _digest(name: str, text: str, error: str | None = None) -> SheetDigest:
    ref = SheetRef(
        pdf_path=Path(f"{name}.pdf"), page_index=0, source_name=f"{name}.pdf", page_count=1
    )
    return SheetDigest(ref=ref, text=text, error=error)


class _FakeClient:
    def __init__(self, responder):
        self.calls: list[dict] = []

        class _Msgs:
            def create(_self, **kw):
                self.calls.append(kw)
                return responder(kw)

        self.messages = _Msgs()


# --------------------------------------------------------------------------- #
# Model selection
# --------------------------------------------------------------------------- #


def test_default_synthesis_model_is_opus(monkeypatch):
    monkeypatch.delenv("SPEC_CRITIC_DRAWING_SYNTHESIS_MODEL", raising=False)
    assert default_synthesis_model() == REVIEW_MODEL_DEFAULT  # Opus 4.8
    monkeypatch.setenv("SPEC_CRITIC_DRAWING_SYNTHESIS_MODEL", "claude-sonnet-4-6")
    assert default_synthesis_model() == "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# build_synthesis_user_text
# --------------------------------------------------------------------------- #


def test_build_synthesis_user_text_includes_each_sheet():
    sheets = [_digest("M-101", "VAV-3 plan"), _digest("M-501", "VAV-3 schedule row")]
    text = build_synthesis_user_text(sheets)
    assert "Sheet 1/2: M-101.pdf" in text
    assert "Sheet 2/2: M-501.pdf" in text
    assert "VAV-3 plan" in text and "VAV-3 schedule row" in text
    assert text.rstrip().endswith("conflicts, and cite the sheet numbers involved.")


# --------------------------------------------------------------------------- #
# synthesize_drawing_set
# --------------------------------------------------------------------------- #


def test_synthesis_skipped_below_two_sheets():
    client = _FakeClient(lambda kw: FakeMessage(content=[FakeTextBlock(text="x")]))
    result = synthesize_drawing_set([_digest("M-101", "only one")], client=client, model=OPUS)
    assert not result.ok
    assert "insufficient" in result.error
    assert client.calls == []  # never hit the API


def test_synthesis_success_shape_and_telemetry():
    resp = FakeMessage(
        content=[FakeTextBlock(text="VAV-3 appears on M-101 and M-501 — consistent.")],
        usage=FakeUsage(input_tokens=900, output_tokens=120),
    )
    client = _FakeClient(lambda kw: resp)
    sheets = [_digest("M-101", "VAV-3 plan"), _digest("M-501", "VAV-3 schedule")]

    result = synthesize_drawing_set(sheets, client=client, model=OPUS)

    assert result.ok
    assert "VAV-3" in result.text
    assert result.input_tokens == 900 and result.output_tokens == 120
    assert result.model_used == OPUS
    kw = client.calls[0]
    assert kw["model"] == OPUS
    assert kw["system"] == SYNTHESIS_SYSTEM_PROMPT
    # The digests are in the user content.
    assert "VAV-3 plan" in kw["messages"][0]["content"]


def test_synthesis_excludes_failed_sheets_from_input():
    client = _FakeClient(lambda kw: FakeMessage(content=[FakeTextBlock(text="overview")]))
    sheets = [
        _digest("M-101", "good plan text"),
        _digest("M-102", "", error="502 bad gateway"),  # failed -> excluded
        _digest("M-501", "good schedule text"),
    ]
    synthesize_drawing_set(sheets, client=client, model=OPUS)
    content = client.calls[0]["messages"][0]["content"]
    assert "good plan text" in content and "good schedule text" in content
    assert "502 bad gateway" not in content  # failed sheet not fed in


def test_synthesis_api_error_is_clean_and_nonraising():
    class _StatusErr(Exception):
        status_code = 502

    def boom(_kw):
        raise _StatusErr("<html>502 Bad Gateway</html>")

    sheets = [_digest("a", "x"), _digest("b", "y")]
    result = synthesize_drawing_set(
        sheets, client=_FakeClient(boom), model=OPUS, sleep=lambda _s: None
    )
    assert not result.ok
    assert result.error == "502 bad gateway (server temporarily unavailable — try again)"


def test_synthesis_empty_response_flags_error():
    client = _FakeClient(lambda kw: FakeMessage(content=[], stop_reason="max_tokens"))
    sheets = [_digest("a", "x"), _digest("b", "y")]
    result = synthesize_drawing_set(sheets, client=client, model=OPUS)
    assert not result.ok and "empty synthesis" in result.error


def test_synthesis_retries_transient_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    class _StatusErr(Exception):
        status_code = 503

    def responder(_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _StatusErr("down")
        return FakeMessage(content=[FakeTextBlock(text="recovered overview")])

    sheets = [_digest("a", "x"), _digest("b", "y")]
    result = synthesize_drawing_set(
        sheets, client=_FakeClient(responder), model=OPUS, max_retries=2, sleep=slept.append
    )
    assert result.ok and "recovered overview" in result.text
    assert calls["n"] == 2 and slept == [2.0]


# --------------------------------------------------------------------------- #
# pipeline integration (renders a synthetic PDF; needs PyMuPDF)
# --------------------------------------------------------------------------- #


def _make_pdf(pymupdf, path: Path, pages: int) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=792, height=612)
        page.insert_text((72, 72), f"SHEET M-10{i + 1} TEST")
    doc.save(str(path))
    doc.close()
    return path


def _routing_client(pymupdf_calls: list):
    """Return digest text for digest calls and overview text for the synthesis
    call, distinguished by the system prompt."""

    def responder(kw):
        pymupdf_calls.append(kw)
        if kw["system"] == SYNTHESIS_SYSTEM_PROMPT:
            return FakeMessage(
                content=[FakeTextBlock(text="SET OVERVIEW: VAV-3 spans M-101/M-501")],
                usage=FakeUsage(input_tokens=300, output_tokens=50),
            )
        assert kw["system"] == DIGEST_SYSTEM_PROMPT
        return FakeMessage(
            content=[FakeTextBlock(text="per-sheet digest body")],
            usage=FakeUsage(input_tokens=100, output_tokens=20),
        )

    class _C:
        def __init__(self):
            class _M:
                def create(_s, **kw):
                    return responder(kw)

            self.messages = _M()

    return _C()


def test_pipeline_synthesize_prepends_overview(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from src.drawings.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=2)
    calls: list = []
    ctx = extract_drawing_context(
        [path], client=_routing_client(calls), rows=2, cols=2, synthesize=True
    )

    assert ctx.ok_sheet_count == 2
    assert ctx.synthesis_text == "SET OVERVIEW: VAV-3 spans M-101/M-501"
    assert "## Drawing Set Overview (cross-sheet synthesis)" in ctx.combined_text
    # Overview precedes the per-sheet sections.
    assert ctx.combined_text.index("Drawing Set Overview") < ctx.combined_text.index("## Sheet 1/2")
    # Synthesis tokens are folded into the run totals (2*100 digests + 300 synth).
    assert ctx.total_input_tokens == 500
    assert ctx.errors == []


def test_pipeline_no_synthesis_by_default(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from src.drawings.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=2)
    calls: list = []
    ctx = extract_drawing_context([path], client=_routing_client(calls), rows=2, cols=2)

    assert ctx.synthesis_text == ""
    assert "Drawing Set Overview" not in ctx.combined_text
    # No synthesis call was made (only the 2 digest calls).
    assert all(kw["system"] == DIGEST_SYSTEM_PROMPT for kw in calls)
    assert len(calls) == 2


def test_pipeline_synthesis_failure_falls_back(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from src.drawings.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=2)

    def responder(kw):
        if kw["system"] == SYNTHESIS_SYSTEM_PROMPT:
            return FakeMessage(content=[], stop_reason="max_tokens")  # empty -> error
        return FakeMessage(content=[FakeTextBlock(text="digest body")])

    class _C:
        def __init__(self):
            class _M:
                def create(_s, **kw):
                    return responder(kw)

            self.messages = _M()

    ctx = extract_drawing_context([path], client=_C(), rows=2, cols=2, synthesize=True)

    assert ctx.synthesis_text == ""  # synthesis failed
    assert "Drawing Set Overview" not in ctx.combined_text
    assert "digest body" in ctx.combined_text  # per-sheet digests still shipped
    assert any("Cross-sheet synthesis" in e for e in ctx.errors)  # failure surfaced
