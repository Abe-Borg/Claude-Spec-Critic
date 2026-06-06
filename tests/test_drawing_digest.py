"""Vision-digest engine + orchestration tests.

The request-building and per-sheet digest tests use the dependency-free models
and a fake Anthropic client, so they run without PyMuPDF. The end-to-end
pipeline tests render a synthetic PDF and are skipped when PyMuPDF is absent.
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from src.drawings.digest import (
    DIGEST_SYSTEM_PROMPT,
    SheetDigest,
    build_user_content,
    digest_sheet,
)
from src.drawings.models import ImageTile, RenderedSheet, SheetRef
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
# pipeline (renders a synthetic PDF; needs PyMuPDF)
# --------------------------------------------------------------------------- #


def test_pipeline_combines_per_sheet_digests(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from src.drawings.pipeline import extract_drawing_context

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
    from src.drawings.pipeline import extract_drawing_context

    path = _make_pdf(pymupdf, tmp_path / "set.pdf", pages=2)

    state = {"n": 0}

    def responder(_kw):
        state["n"] += 1
        if state["n"] == 2:
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


def test_list_sheets_splits_pages(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from src.drawings.render import list_sheets

    path = _make_pdf(pymupdf, tmp_path / "multi.pdf", pages=3)
    refs = list_sheets([path])

    assert len(refs) == 3
    assert refs[0].page_count == 3
    assert refs[1].page_index == 1
    assert refs[0].source_name == "multi.pdf"
