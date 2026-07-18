"""Project Context attachment tests: Markdown / text extraction + the pure
(tkinter-free) merge / token-cap / attachment-wrapping helpers.

These run fully hermetically — no tkinter, no network — because
``src.gui.context_attachment`` deliberately imports only the tokenizer.
"""
from __future__ import annotations

import pytest

from docx import Document

from src.core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS
from src.gui import context_attachment as ca
from src.gui.context_attachment import (
    context_has_drawing_digest,
    context_within_token_cap,
    digested_drawing_filenames,
    merge_into_context,
    wrap_attachment,
)
from src.input.drawing_digest import DIGEST_ATTACHMENT_LABEL
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
# Drawing-readout helpers (FILES-panel drawing feedback)
# --------------------------------------------------------------------------- #


def test_context_has_drawing_digest_detects_merged_block():
    digest = wrap_attachment(DIGEST_ATTACHMENT_LABEL, "PROJECT IDENTITY & OVERVIEW ...")
    assert context_has_drawing_digest(f"project notes\n\n{digest}") is True


def test_context_has_drawing_digest_false_without_block():
    assert context_has_drawing_digest("just some project notes") is False
    assert context_has_drawing_digest("") is False
    assert context_has_drawing_digest(None) is False


def test_context_has_drawing_digest_ignores_lookalike_attachment():
    # A context *file* named like the digest carries an extension in its
    # label, so it must not be mistaken for the vision digest block.
    lookalike = wrap_attachment(f"{DIGEST_ATTACHMENT_LABEL}.docx", "body")
    assert context_has_drawing_digest(lookalike) is False


class _FakeChunkStatus:
    def __init__(self, status, file_labels):
        self.status = status
        self.file_labels = file_labels


def test_digested_drawing_filenames_strips_page_suffix_and_dedups():
    statuses = [
        _FakeChunkStatus("completed", ["a.pdf", "big.pdf (pages 1-300)"]),
        _FakeChunkStatus("truncated", ["big.pdf (pages 301-600)", "d.pdf"]),
    ]
    # Order-preserving, de-duped: the split "big.pdf" collapses to one entry.
    assert digested_drawing_filenames(statuses) == ["a.pdf", "big.pdf", "d.pdf"]


def test_digested_drawing_filenames_excludes_failed_chunks():
    statuses = [
        _FakeChunkStatus("completed", ["kept.pdf"]),
        _FakeChunkStatus("failed", ["dropped.pdf"]),
    ]
    # A failed chunk's sheets are not in the digest text, so they are not
    # surfaced in the FILES-panel readout (the partial-failure warning names
    # them instead).
    assert digested_drawing_filenames(statuses) == ["kept.pdf"]


def test_digested_drawing_filenames_empty_when_all_failed():
    statuses = [_FakeChunkStatus("failed", ["a.pdf", "b.pdf"])]
    assert digested_drawing_filenames(statuses) == []
