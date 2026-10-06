"""The operator-supplied drawing analysis: read, count, wrap, parse, read out.

Spec Critic does not read drawings. The operator attaches the text output of
a separate drawing-analyzer program; these tests pin how that file is read
and token-counted, the ``Construction Drawing Digest`` block it becomes (the
same marker the drawing-impact pass and saved pending-batch records key on),
and the FILES-panel readout derived from whatever is in Project Context.

Hermetic: the tokenizer is stubbed with a word count (the suite's pattern),
no tkinter (``src.input.drawing_analysis`` imports only the tkinter-free
``context_attachment`` helpers), no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.drawing_impact import extract_drawing_digest
from src.gui import context_attachment as ca
from src.gui.context_attachment import (
    context_has_drawing_digest,
    drawing_analysis_readout,
    wrap_attachment,
)
from src.input import drawing_analysis as da
from src.input.drawing_analysis import (
    DIGEST_ATTACHMENT_LABEL,
    DRAWING_ANALYSIS_EXTENSIONS,
    MAX_DRAWING_ANALYSIS_BYTES,
    SOURCE_LINE_PREFIX,
    UNNAMED_BLOCK_LABEL,
    DrawingAnalysis,
    DrawingAnalysisError,
    drawing_analysis_blocks,
    load_drawing_analyses,
    read_drawing_analysis,
    wrapped_drawing_analysis_block,
)


def _word_tokens(text: str) -> int:
    return len(text.split())


@pytest.fixture(autouse=True)
def _stub_tokenizer(monkeypatch):
    monkeypatch.setattr(da, "count_tokens", _word_tokens)
    monkeypatch.setattr(ca, "count_tokens", _word_tokens)


_ANALYSIS = (
    "SHEET INDEX\n"
    "M-101 Mechanical Plan Level 1 [plans.pdf p.3]\n"
    "FP-601 Sprinkler Riser Diagram\n"
    "GENERAL NOTES\n"
    "1. All work per NFPA 13.\n"
)


def _write(tmp_path: Path, name: str, body: str | bytes) -> Path:
    path = tmp_path / name
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Reading the analyzer's output file
# --------------------------------------------------------------------------- #


class TestReadDrawingAnalysis:
    def test_reads_text_verbatim_and_counts_tokens(self, tmp_path):
        path = _write(tmp_path, "plans_analysis.txt", "\n" + _ANALYSIS + "\n\n")
        got = read_drawing_analysis(path)
        assert got == DrawingAnalysis(
            name="plans_analysis.txt",
            text=_ANALYSIS.strip(),
            tokens=_word_tokens(_ANALYSIS),
        )

    @pytest.mark.parametrize("ext", sorted(DRAWING_ANALYSIS_EXTENSIONS))
    def test_every_supported_extension_is_read(self, tmp_path, ext):
        path = _write(tmp_path, f"analysis{ext}", _ANALYSIS)
        assert read_drawing_analysis(path).text == _ANALYSIS.strip()

    def test_supported_extensions_are_text_only(self):
        # The analyzer's output is text. Word/PDF reference material is the
        # Attach Files… flow's job (it extracts text); nothing here parses a
        # binary format, and nothing is uploaded.
        assert DRAWING_ANALYSIS_EXTENSIONS == frozenset({".txt", ".md", ".json"})

    @pytest.mark.parametrize("name", ["plans.pdf", "notes.docx", "sheet.dwg", "noext"])
    def test_unsupported_extension_is_named_with_the_supported_list(self, tmp_path, name):
        path = _write(tmp_path, name, "x")
        with pytest.raises(DrawingAnalysisError) as exc:
            read_drawing_analysis(path)
        message = str(exc.value)
        assert message.startswith(f"{name}: ")
        assert "not a text analysis file" in message
        for ext in DRAWING_ANALYSIS_EXTENSIONS:
            assert ext in message

    def test_missing_file(self, tmp_path):
        with pytest.raises(DrawingAnalysisError, match=r"^absent\.txt: file not found$"):
            read_drawing_analysis(tmp_path / "absent.txt")

    def test_directory_is_not_a_file(self, tmp_path):
        folder = tmp_path / "analysis.txt"
        folder.mkdir()
        with pytest.raises(DrawingAnalysisError, match="file not found"):
            read_drawing_analysis(folder)

    @pytest.mark.parametrize("body", ["", "   \n\t\n"])
    def test_empty_file_is_refused(self, tmp_path, body):
        path = _write(tmp_path, "empty.txt", body)
        with pytest.raises(DrawingAnalysisError, match=r"^empty\.txt: the file has no text$"):
            read_drawing_analysis(path)

    def test_oversized_file_is_refused_before_it_is_read(self, tmp_path, monkeypatch):
        # Project Context is capped at 100k tokens, so a file far over that
        # can never be attached; refuse on size rather than tokenizing it.
        monkeypatch.setattr(da, "MAX_DRAWING_ANALYSIS_BYTES", 16)
        path = _write(tmp_path, "huge.txt", "x" * 17)
        counted: list[str] = []
        monkeypatch.setattr(da, "count_tokens", lambda t: counted.append(t) or 0)
        with pytest.raises(DrawingAnalysisError) as exc:
            read_drawing_analysis(path)
        assert str(exc.value).startswith("huge.txt: ")
        assert "limit for a drawing analysis file" in str(exc.value)
        assert counted == []

    def test_byte_limit_is_far_above_the_context_cap(self):
        # ~100k tokens is roughly 400 KB of English text; the guard is only
        # for a wrong file (a log, an export), never a plausible analysis.
        assert MAX_DRAWING_ANALYSIS_BYTES >= 4 * 1024 * 1024

    def test_undecodable_bytes_are_replaced_not_refused(self, tmp_path):
        path = _write(tmp_path, "latin1.txt", b"valve label \xff done")
        got = read_drawing_analysis(path)
        assert got.text.startswith("valve label ")
        assert got.text.endswith("done")

    def test_non_ascii_is_preserved(self, tmp_path):
        body = "Détails du CVC — café façade °C ½\""
        path = _write(tmp_path, "unicode.md", body)
        assert read_drawing_analysis(path).text == body

    def test_a_delimiter_line_in_the_file_cannot_close_the_block_early(self, tmp_path):
        # An analyzer output that quotes a previous Project Context carries
        # the digest's own END marker. Verbatim interpolation would end the
        # block there: the readout and the drawing-impact pass would see a
        # truncated digest while the review calls got the whole text. The
        # line is escaped on read, so text, count, block, gate and readout
        # all agree on the full content.
        body = (
            "SHEET INDEX\n"
            "M-101 Plan\n"
            f"--- END ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---\n"
            f"--- BEGIN ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---\n"
            "GENERAL NOTES\n"
            "1. All work per NFPA 13."
        )
        path = _write(tmp_path, "quoted.txt", body)
        got = read_drawing_analysis(path)
        assert got.text == (
            "SHEET INDEX\n"
            "M-101 Plan\n"
            f"\\--- END ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---\n"
            f"\\--- BEGIN ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---\n"
            "GENERAL NOTES\n"
            "1. All work per NFPA 13."
        )
        assert got.tokens == _word_tokens(got.text)
        ctx = "notes\n\n" + wrapped_drawing_analysis_block(got)
        [block] = drawing_analysis_blocks(ctx)
        assert block.text == got.text  # nothing after the quoted marker is lost
        assert drawing_analysis_readout(ctx) == [{"name": "quoted.txt", "tokens": got.tokens}]
        digest = extract_drawing_digest(ctx)
        assert digest.endswith("1. All work per NFPA 13.")
        assert "GENERAL NOTES" in digest


class TestLoadDrawingAnalyses:
    def test_one_bad_file_does_not_block_the_others(self, tmp_path):
        good = _write(tmp_path, "a.txt", _ANALYSIS)
        bad = _write(tmp_path, "b.pdf", "%PDF-1.4")
        empty = _write(tmp_path, "c.md", "")
        analyses, errors = load_drawing_analyses([good, bad, empty, tmp_path / "d.txt"])
        assert [a.name for a in analyses] == ["a.txt"]
        assert errors == [
            "b.pdf: not a text analysis file (expected .json, .md, .txt)",
            "c.md: the file has no text",
            "d.txt: file not found",
        ]

    def test_empty_input(self):
        assert load_drawing_analyses([]) == ([], [])


# --------------------------------------------------------------------------- #
# The block in Project Context
# --------------------------------------------------------------------------- #


def _analysis(name="plans_analysis.txt", text=_ANALYSIS.strip()) -> DrawingAnalysis:
    return DrawingAnalysis(name=name, text=text, tokens=_word_tokens(text))


class TestBlockFormat:
    def test_label_is_the_schema_string_older_saved_runs_carry(self):
        # A pending-batch record from a build whose block came from the
        # retired vision digest carries this label in its persisted
        # project_context; keeping it byte-identical keeps that run's
        # drawing-impact pass.
        assert DIGEST_ATTACHMENT_LABEL == "Construction Drawing Digest"

    def test_wrapped_block_names_its_source_file_then_the_text_verbatim(self):
        block = wrapped_drawing_analysis_block(_analysis())
        assert block == (
            f"--- BEGIN ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---\n"
            f"{SOURCE_LINE_PREFIX}plans_analysis.txt\n"
            f"{_ANALYSIS.strip()}\n"
            f"--- END ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---"
        )

    def test_the_drawing_impact_gate_finds_the_block(self):
        # extract_drawing_digest is the pass's gate; the source line rides
        # inside the digest as provenance.
        ctx = "Project notes.\n\n" + wrapped_drawing_analysis_block(_analysis())
        digest = extract_drawing_digest(ctx)
        assert digest.startswith(f"{SOURCE_LINE_PREFIX}plans_analysis.txt\n")
        assert digest.endswith(_ANALYSIS.strip())

    def test_context_has_drawing_digest(self):
        assert context_has_drawing_digest(wrapped_drawing_analysis_block(_analysis())) is True
        assert context_has_drawing_digest("just some project notes") is False
        assert context_has_drawing_digest("") is False
        assert context_has_drawing_digest(None) is False

    def test_lookalike_context_file_is_not_a_digest(self):
        # A context *file* named like the digest carries an extension in its
        # label, so neither the gate nor the readout mistakes it.
        lookalike = wrap_attachment(f"{DIGEST_ATTACHMENT_LABEL}.docx", "not a digest")
        assert context_has_drawing_digest(lookalike) is False
        assert drawing_analysis_blocks(lookalike) == []
        assert extract_drawing_digest(lookalike) == ""


class TestDrawingAnalysisBlocks:
    def test_parses_name_and_body_in_order(self):
        a = wrapped_drawing_analysis_block(_analysis("a.txt", "alpha one"))
        b = wrapped_drawing_analysis_block(_analysis("b.md", "beta two three"))
        blocks = drawing_analysis_blocks(f"notes\n\n{a}\n\nmore notes\n\n{b}")
        assert [(x.name, x.text) for x in blocks] == [
            ("a.txt", "alpha one"),
            ("b.md", "beta two three"),
        ]
        assert [x.display_name for x in blocks] == ["a.txt", "b.md"]

    def test_block_without_a_source_line_is_still_a_digest(self):
        # Hand-pasted, or written by an earlier build: no source line, so
        # the whole body is the text and the row reads out unnamed.
        legacy = wrap_attachment(DIGEST_ATTACHMENT_LABEL, "SHEET INDEX\nM-101 Plan")
        [block] = drawing_analysis_blocks(legacy)
        assert block.name == ""
        assert block.text == "SHEET INDEX\nM-101 Plan"
        assert block.display_name == UNNAMED_BLOCK_LABEL

    def test_blank_blocks_are_skipped(self):
        empty = wrap_attachment(DIGEST_ATTACHMENT_LABEL, "   ")
        only_source = wrap_attachment(DIGEST_ATTACHMENT_LABEL, f"{SOURCE_LINE_PREFIX}x.txt\n   ")
        assert drawing_analysis_blocks(f"{empty}\n\n{only_source}") == []

    def test_empty_context(self):
        assert drawing_analysis_blocks("") == []
        assert drawing_analysis_blocks(None) == []


# --------------------------------------------------------------------------- #
# The FILES-panel readout: a pure function of the textbox contents
# --------------------------------------------------------------------------- #


class TestDrawingAnalysisReadout:
    def test_one_row_per_block_with_the_attach_time_count(self):
        first = _analysis("plans_analysis.txt")
        second = _analysis("electrical_analysis.md", "PANEL SCHEDULE LP-1 42 circuits")
        ctx = "\n\n".join(
            ["Owner notes.", wrapped_drawing_analysis_block(first), wrapped_drawing_analysis_block(second)]
        )
        rows = drawing_analysis_readout(ctx)
        # The count is of the analysis text alone (the source line is not
        # the operator's content), so it equals the figure logged at attach.
        assert rows == [
            {"name": "plans_analysis.txt", "tokens": first.tokens},
            {"name": "electrical_analysis.md", "tokens": second.tokens},
        ]

    def test_hand_deleting_a_block_drops_its_row(self):
        block = wrapped_drawing_analysis_block(_analysis())
        assert drawing_analysis_readout(f"notes\n\n{block}") != []
        assert drawing_analysis_readout("notes") == []

    def test_trimming_a_block_shrinks_its_count(self):
        full = wrapped_drawing_analysis_block(_analysis("a.txt", "one two three four"))
        trimmed = wrapped_drawing_analysis_block(_analysis("a.txt", "one two"))
        assert drawing_analysis_readout(full)[0]["tokens"] == 4
        assert drawing_analysis_readout(trimmed)[0]["tokens"] == 2

    def test_memo_skips_recounting_unchanged_blocks_and_is_pruned(self, monkeypatch):
        counted: list[str] = []

        def _count(text):
            counted.append(text)
            return _word_tokens(text)

        monkeypatch.setattr(ca, "count_tokens", _count)
        a = wrapped_drawing_analysis_block(_analysis("a.txt", "alpha one"))
        b = wrapped_drawing_analysis_block(_analysis("b.txt", "beta two three"))
        memo: dict[str, int] = {}
        drawing_analysis_readout(f"{a}\n\n{b}", token_memo=memo)
        assert counted == ["alpha one", "beta two three"]
        # Typing elsewhere in the textbox: same blocks, nothing re-counted.
        drawing_analysis_readout(f"new notes\n\n{a}\n\n{b}", token_memo=memo)
        assert counted == ["alpha one", "beta two three"]
        # Deleting one block prunes its memo entry; the survivor is reused.
        rows = drawing_analysis_readout(f"{b}", token_memo=memo)
        assert rows == [{"name": "b.txt", "tokens": 3}]
        assert memo == {"beta two three": 3}
        assert counted == ["alpha one", "beta two three"]

    def test_unnamed_block_reads_out_with_the_placeholder_name(self):
        legacy = wrap_attachment(DIGEST_ATTACHMENT_LABEL, "SHEET INDEX M-101")
        assert drawing_analysis_readout(legacy) == [{"name": UNNAMED_BLOCK_LABEL, "tokens": 3}]
