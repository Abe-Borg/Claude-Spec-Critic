"""Project Context attachment tests: Markdown / text extraction + the pure
(tkinter-free) merge / token-cap / drawing-digest planning helpers.

These run fully hermetically — no tkinter, no PyMuPDF, no network — because
``src.gui.context_attachment`` deliberately imports only the tokenizer and a
drawing digest is duck-typed (see the module docstring). The GUI flow that wires
these into the threaded "Attach Drawings…" button is covered separately in
``test_drawing_context_integration.py`` (which skips without customtkinter).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from docx import Document

from src.core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS
from src.gui import context_attachment as ca
from src.gui.context_attachment import (
    DrawingAttachmentPlan,
    build_drawing_attachment_block,
    context_within_token_cap,
    merge_into_context,
    plan_drawing_attachment,
    wrap_attachment,
)
from src.input.extractor import CONTEXT_ATTACHMENT_EXTENSIONS, extract_context_text


# Real ``count_tokens`` downloads the cl100k_base encoding on first use; the
# rest of the suite is hermetic and never does. Stub the tokenizer with a
# deterministic word count (1 word ≈ 1 token) so the cap-comparison plumbing is
# tested without the network — matching the suite's ``stub_count_tokens`` pattern.
def _word_tokens(text: str) -> int:
    return len(text.split())


@pytest.fixture(autouse=True)
def _stub_tokenizer(monkeypatch):
    monkeypatch.setattr(ca, "count_tokens", _word_tokens)


# --------------------------------------------------------------------------- #
# Fake drawing digest (duck-typed; no PyMuPDF / engine import)
# --------------------------------------------------------------------------- #


@dataclass
class _FakeCtx:
    combined_text: str
    sheet_count: int = 1
    file_count: int = 1
    _ok: int = 1
    errors: list[str] = field(default_factory=list)

    @property
    def ok_sheet_count(self) -> int:
        return self._ok


# A string comfortably over the 100k-token Project Context cap under the
# word-count stub (and under real tiktoken too, since each "word" is ~1 token).
_OVER_CAP_TEXT = "word " * (PROJECT_CONTEXT_MAX_TOKENS + 1)


# --------------------------------------------------------------------------- #
# extract_context_text — Markdown / text attachments
# --------------------------------------------------------------------------- #


def test_context_attachment_extensions_cover_md_and_txt():
    assert {".docx", ".pdf", ".md", ".txt"} <= CONTEXT_ATTACHMENT_EXTENSIONS


def test_extract_context_text_reads_markdown_verbatim(tmp_path):
    body = "# Drawing Set Context Digest\n\n## Sheet 1/1: M-101\nVAV-3 serves Rm 120.\n"
    path = tmp_path / "drawing_context.md"
    path.write_text(body, encoding="utf-8")
    assert extract_context_text(path) == body


def test_extract_context_text_reads_plaintext_verbatim(tmp_path):
    body = "Plain project note.\nSecond line.\n"
    path = tmp_path / "notes.txt"
    path.write_text(body, encoding="utf-8")
    assert extract_context_text(path) == body


def test_extract_context_text_preserves_non_ascii(tmp_path):
    body = "Détails du CVC — café façade °C ½\"\n"
    path = tmp_path / "unicode.md"
    path.write_text(body, encoding="utf-8")
    assert extract_context_text(path) == body


def test_extract_context_text_replaces_undecodable_bytes(tmp_path):
    path = tmp_path / "latin1.txt"
    path.write_bytes(b"valve label \xff done")  # invalid UTF-8 byte
    out = extract_context_text(path)
    assert out.startswith("valve label ")
    assert "done" in out  # one bad byte never sinks the whole attachment


def test_extract_context_text_routes_docx(tmp_path):
    doc = Document()
    doc.add_paragraph("Hello from a context docx")
    path = tmp_path / "ctx.docx"
    doc.save(path)
    assert "Hello from a context docx" in extract_context_text(path)


def test_extract_context_text_rejects_unsupported_extension(tmp_path):
    path = tmp_path / "ctx.rtf"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported context attachment format"):
        extract_context_text(path)


def test_extract_context_text_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_context_text(tmp_path / "absent.md")


# --------------------------------------------------------------------------- #
# wrap_attachment / merge_into_context / token cap
# --------------------------------------------------------------------------- #


def test_wrap_attachment_shape():
    assert wrap_attachment("LBL", "body") == (
        "--- BEGIN ATTACHMENT: LBL ---\nbody\n--- END ATTACHMENT: LBL ---"
    )


def test_merge_into_context_variants():
    assert merge_into_context("", "B") == "B"
    assert merge_into_context("A", "") == "A"
    assert merge_into_context("A", "B") == "A\n\nB"
    # both sides stripped before joining
    assert merge_into_context("  A  ", "  B  ") == "A\n\nB"


def test_context_within_token_cap_small_text_fits():
    text = "a short project note"
    tokens, fits = context_within_token_cap(text)
    assert fits is True
    assert tokens == _word_tokens(text)


def test_context_within_token_cap_rejects_oversized():
    tokens, fits = context_within_token_cap(_OVER_CAP_TEXT)
    assert tokens > PROJECT_CONTEXT_MAX_TOKENS
    assert fits is False


# --------------------------------------------------------------------------- #
# build_drawing_attachment_block
# --------------------------------------------------------------------------- #


def test_build_drawing_attachment_block_labels_counts_and_wraps():
    ctx = _FakeCtx(combined_text="DIGEST BODY", sheet_count=3, file_count=2, _ok=2)
    block = build_drawing_attachment_block(ctx)
    assert block.startswith("--- BEGIN ATTACHMENT: DRAWING DIGEST — 2/3 sheet(s) from 2 file(s) ---")
    assert block.endswith("--- END ATTACHMENT: DRAWING DIGEST — 2/3 sheet(s) from 2 file(s) ---")
    assert "DIGEST BODY" in block


def test_build_drawing_attachment_block_empty_digest_returns_empty():
    assert build_drawing_attachment_block(_FakeCtx(combined_text="   ")) == ""
    assert build_drawing_attachment_block(_FakeCtx(combined_text="")) == ""


def test_build_drawing_attachment_block_refuses_failure_only_set():
    # A fully-failed set still has non-empty combined_text — the engine emits a
    # header + a per-sheet failure blockquote — but nothing actually digested
    # (ok_sheet_count == 0), so it must attach nothing, not a wall of failures.
    ctx = _FakeCtx(
        combined_text=(
            "# Drawing Set Context Digest\n\n## Sheet 1/1: M-101\n\n"
            "> [drawing analysis failed for this sheet: 401 invalid x-api-key]"
        ),
        sheet_count=1,
        _ok=0,
        errors=["M-101: 401 invalid x-api-key"],
    )
    assert build_drawing_attachment_block(ctx) == ""


# --------------------------------------------------------------------------- #
# plan_drawing_attachment
# --------------------------------------------------------------------------- #


def test_plan_drawing_attachment_happy_path_merges_under_cap():
    ctx = _FakeCtx(combined_text="VAV-3 serves Rm 120", sheet_count=1, file_count=1, _ok=1)
    plan = plan_drawing_attachment("Existing context.", ctx)
    assert isinstance(plan, DrawingAttachmentPlan)
    assert plan.has_digest and plan.within_cap and plan.attachable
    assert plan.merged_context.startswith("Existing context.\n\n--- BEGIN ATTACHMENT:")
    assert "VAV-3 serves Rm 120" in plan.merged_context
    assert plan.tokens == _word_tokens(plan.merged_context)
    assert plan.error_lines == []


def test_plan_drawing_attachment_no_existing_context():
    ctx = _FakeCtx(combined_text="body")
    plan = plan_drawing_attachment("", ctx)
    assert plan.merged_context == plan.block  # no leading separator
    assert plan.attachable


def test_plan_drawing_attachment_carries_sheet_errors():
    ctx = _FakeCtx(
        combined_text="partial body",
        sheet_count=2,
        _ok=1,
        errors=["M-102.pdf p2: boom"],
    )
    plan = plan_drawing_attachment("", ctx)
    assert plan.error_lines == ["M-102.pdf p2: boom"]
    assert plan.attachable  # the readable sheet still attaches


def test_plan_drawing_attachment_empty_digest_not_attachable():
    ctx = _FakeCtx(combined_text="", sheet_count=1, _ok=0, errors=["only sheet failed"])
    plan = plan_drawing_attachment("keep me", ctx)
    assert not plan.has_digest
    assert not plan.attachable
    assert plan.merged_context == "keep me"  # existing context preserved
    assert plan.error_lines == ["only sheet failed"]


def test_plan_drawing_attachment_failure_only_set_not_attachable():
    # combined_text non-empty (failure blockquotes) but ok_sheet_count == 0.
    ctx = _FakeCtx(
        combined_text="## Sheet 1/1\n\n> [drawing analysis failed: boom]",
        sheet_count=1,
        _ok=0,
        errors=["boom"],
    )
    plan = plan_drawing_attachment("keep me", ctx)
    assert not plan.has_digest and not plan.attachable
    assert plan.merged_context == "keep me"  # existing context preserved
    assert plan.error_lines == ["boom"]


def test_plan_drawing_attachment_over_cap_not_attachable():
    ctx = _FakeCtx(combined_text=_OVER_CAP_TEXT, sheet_count=40, file_count=1, _ok=40)
    plan = plan_drawing_attachment("small existing", ctx)
    assert plan.has_digest
    assert not plan.within_cap
    assert not plan.attachable
    assert plan.tokens > PROJECT_CONTEXT_MAX_TOKENS
