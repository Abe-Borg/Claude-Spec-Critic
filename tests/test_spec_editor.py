from pathlib import Path

import pytest
from docx import Document

from src.editing.edit_locator import EditLocation, LocatorResult
from src.input.extractor import ParagraphMapping
from src.review.reviewer import Finding
from src.editing.spec_editor import apply_edits_to_spec, build_edit_actions


def _finding(*, action: str = "EDIT", existing: str = "", replacement: str | None = "") -> Finding:
    return Finding(
        severity="HIGH",
        fileName="spec.docx",
        section="1.0",
        issue="Issue",
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference="Code",
        confidence=0.9,
    )


def _locator_result(
    *,
    action: str = "EDIT",
    status: str = "matched",
    body_index: int = 0,
    element_type: str = "paragraph",
    text: str,
    match_start: int,
    match_end: int,
    matched_text: str,
    replacement_text: str | None,
    confidence: float = 1.0,
    row_index: int | None = None,
    severity: str = "HIGH",
) -> LocatorResult:
    mapping = ParagraphMapping(
        body_index=body_index,
        element_type=element_type,
        text=text,
        table_index=0 if element_type == "table_cell" else None,
        row_index=row_index,
        cell_index=None,
    )
    location = EditLocation(
        mapping=mapping,
        match_start=match_start,
        match_end=match_end,
        matched_text=matched_text,
        match_confidence=confidence,
        match_method="exact",
    )
    return LocatorResult(
        finding=Finding(
            severity=severity,
            fileName="spec.docx",
            section="1.0",
            issue="Issue",
            actionType=action,
            existingText=matched_text,
            replacementText=replacement_text,
            codeReference="Code",
            confidence=0.9,
        ),
        status=status,
        locations=[location],
        replacement_text=replacement_text,
        action_type=action,
        warning=None,
    )


def test_apply_edits_simple_paragraph_replacement(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Provide seismic bracing per ASCE 7-16.")
    doc.save(source)

    result = _locator_result(
        text="Provide seismic bracing per ASCE 7-16.",
        match_start=28,
        match_end=37,
        matched_text="ASCE 7-16",
        replacement_text="ASCE 7-22",
    )
    report = apply_edits_to_spec(source, output, build_edit_actions([result]))

    saved = Document(output)
    assert saved.paragraphs[0].text == "Provide seismic bracing per ASCE 7-22."
    assert report.edits_applied == 1
    assert report.edits_failed == 0


def test_apply_edits_multi_run_replacement(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    p = doc.add_paragraph()
    p.add_run("Provide seismic bracing per ")
    b1 = p.add_run("ASCE")
    b1.bold = True
    p.add_run(" ")
    b2 = p.add_run("7-16")
    b2.bold = True
    p.add_run(".")
    doc.save(source)

    result = _locator_result(
        text="Provide seismic bracing per ASCE 7-16.",
        match_start=28,
        match_end=37,
        matched_text="ASCE 7-16",
        replacement_text="ASCE 7-22",
    )
    apply_edits_to_spec(source, output, build_edit_actions([result]))

    saved = Document(output)
    para = saved.paragraphs[0]
    assert para.text == "Provide seismic bracing per ASCE 7-22."
    assert para.runs[0].text == "Provide seismic bracing per "
    assert para.runs[-1].text == "."


def test_apply_edits_delete_entire_paragraph(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Keep this paragraph")
    doc.add_paragraph("Delete this paragraph")
    doc.add_paragraph("Keep this too")
    doc.save(source)

    full = "Delete this paragraph"
    result = _locator_result(
        action="DELETE",
        text=full,
        body_index=1,
        match_start=0,
        match_end=len(full),
        matched_text=full,
        replacement_text=None,
    )
    report = apply_edits_to_spec(source, output, build_edit_actions([result]))

    saved = Document(output)
    assert [p.text for p in saved.paragraphs] == ["Keep this paragraph", "Keep this too"]
    assert report.edits_applied == 1


def test_conflict_resolution_applies_non_overlapping_in_reverse_order(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    text = "Alpha Beta Gamma Delta"
    doc = Document()
    doc.add_paragraph(text)
    doc.save(source)

    first = _locator_result(
        text=text,
        match_start=6,
        match_end=10,
        matched_text="Beta",
        replacement_text="BETA",
    )
    second = _locator_result(
        text=text,
        match_start=17,
        match_end=22,
        matched_text="Delta",
        replacement_text="DELTA",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([first, second]))
    saved = Document(output)

    assert saved.paragraphs[0].text == "Alpha BETA Gamma DELTA"
    assert report.edits_applied == 2


def test_conflict_resolution_skips_both_on_ambiguous_partial_overlap(tmp_path: Path):
    """Chunk D3.1: partial overlap (no containment) is ambiguous — skip both.

    Previously the higher-confidence edit silently won and was applied. Per
    the delta plan, ambiguous overlapping edits must be flagged for manual
    review rather than auto-applied, since picking either intent silently
    discards the other.
    """
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    text = "abc def ghi"
    doc = Document()
    doc.add_paragraph(text)
    doc.save(source)

    high = _locator_result(
        text=text,
        match_start=4,
        match_end=7,
        matched_text="def",
        replacement_text="XYZ",
        confidence=0.95,
    )
    low = _locator_result(
        text=text,
        match_start=2,
        match_end=6,
        matched_text="c de",
        replacement_text="1234",
        confidence=0.70,
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([low, high]))
    saved = Document(output)

    # Paragraph unchanged; both edits routed to manual review.
    assert saved.paragraphs[0].text == "abc def ghi"
    assert report.edits_applied == 0
    assert report.edits_skipped == 2
    for outcome in report.outcomes:
        assert outcome.status == "skipped"
        assert "ambiguous" in outcome.detail.lower()
        assert "manual review" in outcome.detail.lower()


def test_conflict_resolution_prefers_broader_subsuming_edit(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    text = "Pipe markers include refrigerant piping and condensate piping using R454B notation."
    doc = Document()
    doc.add_paragraph(text)
    doc.save(source)

    gripe = _locator_result(
        text=text,
        match_start=text.index("R454B"),
        match_end=text.index("R454B") + len("R454B"),
        matched_text="R454B",
        replacement_text="R-454B",
        confidence=1.0,
        severity="GRIPES",
    )
    medium = _locator_result(
        text=text,
        match_start=0,
        match_end=len(text),
        matched_text=text,
        replacement_text="Pipe markers shall separate refrigerant piping from condensate piping and use R-454B notation.",
        confidence=1.0,
        severity="MEDIUM",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([gripe, medium]))
    saved = Document(output)

    assert saved.paragraphs[0].text.startswith("Pipe markers shall separate refrigerant piping")
    assert report.edits_applied == 1
    assert report.edits_skipped == 1
    assert any("broader/higher-priority" in outcome.detail for outcome in report.outcomes if outcome.status == "skipped")


def test_table_cell_edit_updates_target_only(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    table = doc.add_table(rows=1, cols=3)
    table.cell(0, 0).text = "R1C1"
    table.cell(0, 1).text = "Allowance Amount"
    table.cell(0, 2).text = "$10,000"
    doc.save(source)

    joined = "R1C1 | Allowance Amount | $10,000"
    start = joined.find("Allowance Amount")
    result = _locator_result(
        text=joined,
        element_type="table_cell",
        row_index=0,
        match_start=start,
        match_end=start + len("Allowance Amount"),
        matched_text="Allowance Amount",
        replacement_text="Allowance Value",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)
    table = saved.tables[0]

    assert table.cell(0, 0).text == "R1C1"
    assert table.cell(0, 1).text == "Allowance Value"
    assert table.cell(0, 2).text == "$10,000"
    assert report.edits_applied == 1


def test_same_source_and_output_raises_value_error(tmp_path: Path):
    source = tmp_path / "source.docx"
    doc = Document()
    doc.add_paragraph("Hello")
    doc.save(source)

    result = _locator_result(
        text="Hello",
        match_start=0,
        match_end=5,
        matched_text="Hello",
        replacement_text="Hi",
    )

    with pytest.raises(ValueError):
        apply_edits_to_spec(source, source, build_edit_actions([result]))


# ---------------------------------------------------------------------------
# Phase 1 / Step 1.1 — Replacement-text typographic normalization
#
# Integration tests verifying that ``apply_edits_to_spec`` profiles the
# source document's typography and normalizes the model's replacement text
# to match before writing it into the file. The ``replacement_style``
# unit tests in ``test_replacement_style.py`` cover the pure functions;
# these tests cover the wiring through the edit pipeline.
# ---------------------------------------------------------------------------


def test_replacement_normalized_to_curly_when_doc_uses_curly_quotes(tmp_path: Path):
    """Model emits straight quotes; source doc is curly. Applied edit keeps curly."""
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    # Pure curly-quote document to make the profiler's vote unambiguous.
    doc.add_paragraph("Provide “schedule 40” steel piping per project standards.")
    doc.add_paragraph("Don’t substitute without engineer’s approval.")
    doc.add_paragraph("Confirm “seismic” bracing per ASCE 7-22.")
    target_text = "ASCE 7-22"
    paragraph_index = 2
    doc.save(source)

    full_text = "Confirm “seismic” bracing per ASCE 7-22."
    start = full_text.index(target_text)
    result = _locator_result(
        text=full_text,
        body_index=paragraph_index,
        match_start=start,
        match_end=start + len(target_text),
        matched_text=target_text,
        # Model's replacement uses straight ASCII quotes.
        replacement_text='Per "ASCE 7-22" with don\'t substitute',
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    # The applied edit should have rewritten straight " to curly " and
    # straight ' to curly ' so the new sentence matches the doc's style.
    applied_text = saved.paragraphs[paragraph_index].text
    assert "“ASCE 7-22”" in applied_text
    assert "don’t" in applied_text
    assert report.replacement_normalized_count == 1


def test_replacement_normalized_to_straight_when_doc_uses_straight_quotes(tmp_path: Path):
    """Inverse of the above: doc uses straight, model emits curly."""
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph('Provide "schedule 40" steel piping per project standards.')
    doc.add_paragraph("Don't substitute without engineer's approval.")
    doc.add_paragraph('Confirm "seismic" bracing per ASCE 7-22.')
    target_text = "ASCE 7-22"
    paragraph_index = 2
    doc.save(source)

    full_text = 'Confirm "seismic" bracing per ASCE 7-22.'
    start = full_text.index(target_text)
    result = _locator_result(
        text=full_text,
        body_index=paragraph_index,
        match_start=start,
        match_end=start + len(target_text),
        matched_text=target_text,
        # Claude likes curly — should land as straight in this doc.
        replacement_text="Per “ASCE 7-22” with don’t substitute",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    applied_text = saved.paragraphs[paragraph_index].text
    assert '"ASCE 7-22"' in applied_text
    assert "don't" in applied_text
    assert report.replacement_normalized_count == 1


def test_replacement_unchanged_when_already_matches(tmp_path: Path):
    """When the replacement already matches the doc style, no normalize counter."""
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph('Provide "schedule 40" steel piping per project standards.')
    full_text = 'Provide "schedule 40" steel piping per project standards.'
    doc.save(source)

    start = full_text.index("schedule 40")
    result = _locator_result(
        text=full_text,
        match_start=start,
        match_end=start + len("schedule 40"),
        matched_text="schedule 40",
        replacement_text="schedule 80",  # plain ASCII; matches profile.
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))

    assert report.edits_applied == 1
    assert report.replacement_normalized_count == 0


def test_replacement_normalization_disabled_via_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """SPEC_CRITIC_NORMALIZE_REPLACEMENT_STYLE=0 keeps the model's text verbatim."""
    monkeypatch.setenv("SPEC_CRITIC_NORMALIZE_REPLACEMENT_STYLE", "0")
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Provide “schedule 40” steel piping per project standards.")
    full_text = "Provide “schedule 40” steel piping per project standards."
    doc.save(source)

    start = full_text.index("schedule 40")
    result = _locator_result(
        text=full_text,
        match_start=start,
        match_end=start + len("schedule 40"),
        matched_text="schedule 40",
        replacement_text="schedule 80",
    )
    # Override style profile defaults via an explicit passthrough to confirm
    # the env var short-circuits the profile creation path too.
    report = apply_edits_to_spec(source, output, build_edit_actions([result]))

    assert report.edits_applied == 1
    assert report.replacement_normalized_count == 0


# ---------------------------------------------------------------------------
# Phase 1 / Step 1.2 — Punctuation boundary preservation
#
# When the model's existingText includes terminal punctuation but the
# replacement_text does not (or vice versa), the applied edit used to
# silently drop or double the punctuation. The fix is a deterministic
# pass that compares the trailing character of existing vs replacement
# and inspects the character immediately after the match in the live
# paragraph to decide whether to add or strip a terminating punctuation
# mark on the replacement.
# ---------------------------------------------------------------------------


def test_punctuation_boundary_preserves_trailing_period(tmp_path: Path):
    """existing ends with '.', replacement does not -> add the period back."""
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Provide seismic bracing per ASCE 7-16.")
    doc.save(source)

    full = "Provide seismic bracing per ASCE 7-16."
    start = full.index("per ASCE 7-16.")
    result = _locator_result(
        text=full,
        match_start=start,
        match_end=start + len("per ASCE 7-16."),
        matched_text="per ASCE 7-16.",
        replacement_text="per ASCE 7-22",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    assert saved.paragraphs[0].text == "Provide seismic bracing per ASCE 7-22."
    assert report.punctuation_boundary_fixed_count == 1


def test_punctuation_boundary_prevents_doubled_period(tmp_path: Path):
    """existing and replacement both end with '.' next char is also '.' -> strip."""
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    # Construct a sentence whose terminal period is the char after the match.
    doc.add_paragraph("Confirm sec 5.")
    doc.save(source)

    full = "Confirm sec 5."
    start = full.index("sec 5")
    result = _locator_result(
        text=full,
        match_start=start,
        match_end=start + len("sec 5"),
        matched_text="sec 5",
        replacement_text="sec 6.",  # already ends with period, next char is also period.
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    assert saved.paragraphs[0].text == "Confirm sec 6."  # not "Confirm sec 6.."
    assert report.punctuation_boundary_fixed_count == 1


def test_punctuation_boundary_noop_when_already_correct(tmp_path: Path):
    """No boundary issue: replacement ends as expected, no counter bump."""
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Provide ASCE 7-16 references.")
    doc.save(source)

    full = "Provide ASCE 7-16 references."
    start = full.index("ASCE 7-16")
    result = _locator_result(
        text=full,
        match_start=start,
        match_end=start + len("ASCE 7-16"),
        matched_text="ASCE 7-16",
        replacement_text="ASCE 7-22",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    assert saved.paragraphs[0].text == "Provide ASCE 7-22 references."
    assert report.punctuation_boundary_fixed_count == 0


def test_punctuation_boundary_preserves_trailing_comma(tmp_path: Path):
    """existing ends with ',', replacement does not -> add the comma back."""
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Provide ASCE 7-16, ASHRAE 90.1 and CBC 2025.")
    doc.save(source)

    full = "Provide ASCE 7-16, ASHRAE 90.1 and CBC 2025."
    start = full.index("ASCE 7-16,")
    result = _locator_result(
        text=full,
        match_start=start,
        match_end=start + len("ASCE 7-16,"),
        matched_text="ASCE 7-16,",
        replacement_text="ASCE 7-22",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    assert saved.paragraphs[0].text == "Provide ASCE 7-22, ASHRAE 90.1 and CBC 2025."
    assert report.punctuation_boundary_fixed_count == 1


def test_punctuation_boundary_does_not_add_period_when_next_is_word(
    tmp_path: Path,
):
    """The fix only adds punctuation back when the next char is whitespace or end-of-paragraph.

    If the match was mid-word (unusual but possible with normalized
    matching), don't fabricate a period that interrupts the next token.
    """
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Provide foo.bar baseline.")
    doc.save(source)

    full = "Provide foo.bar baseline."
    start = full.index("foo.")
    result = _locator_result(
        text=full,
        match_start=start,
        match_end=start + len("foo."),
        matched_text="foo.",
        replacement_text="qux",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    # Period is *not* re-added because the char after the match is 'b', not
    # whitespace or end-of-paragraph. The fix is conservative.
    assert saved.paragraphs[0].text == "Provide quxbar baseline."
    assert report.punctuation_boundary_fixed_count == 0


def test_punctuation_boundary_fix_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """SPEC_CRITIC_PUNCTUATION_BOUNDARY_FIX=0 disables the fix entirely."""
    monkeypatch.setenv("SPEC_CRITIC_PUNCTUATION_BOUNDARY_FIX", "0")
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Provide seismic bracing per ASCE 7-16.")
    doc.save(source)

    full = "Provide seismic bracing per ASCE 7-16."
    start = full.index("per ASCE 7-16.")
    result = _locator_result(
        text=full,
        match_start=start,
        match_end=start + len("per ASCE 7-16."),
        matched_text="per ASCE 7-16.",
        replacement_text="per ASCE 7-22",
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    # With the fix off, the terminal period is lost.
    assert saved.paragraphs[0].text == "Provide seismic bracing per ASCE 7-22"
    assert report.punctuation_boundary_fixed_count == 0


# ---------------------------------------------------------------------------
# Phase 1 / Step 1.3 — Whole-paragraph DELETE for table cells
#
# _is_whole_paragraph_delete used to require element_type=="paragraph",
# so a DELETE covering the entire matched paragraph inside a table cell
# fell through to substring deletion. The substring deletion clears the
# paragraph's text but leaves the empty <w:p> in the cell, which Word
# renders as a blank line. The fix removes the paragraph element from
# its cell when (a) the cell has more than one paragraph, or leaves it
# empty when removing it would violate Word's "every cell needs at
# least one paragraph" rule.
# ---------------------------------------------------------------------------


def _make_table_with_two_para_cell(tmp_path: Path) -> Path:
    """One-row table whose only cell contains two paragraphs."""
    source = tmp_path / "two_para_cell.docx"
    doc = Document()
    doc.add_paragraph("PART 2 PRODUCTS")
    table = doc.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    # Use python-docx's first auto-paragraph for the first line so we
    # control the paragraph ordering deterministically.
    cell.paragraphs[0].text = "Delete this header line"
    cell.add_paragraph("Keep this body line")
    doc.save(source)
    return source


def test_table_cell_whole_paragraph_delete_removes_paragraph_element(
    tmp_path: Path,
):
    """Whole-paragraph DELETE on a cell with multiple paragraphs removes the element."""
    source = _make_table_with_two_para_cell(tmp_path)
    output = tmp_path / "output.docx"

    target = "Delete this header line"
    # Match position 0 of the cell paragraph; the locator-supplied
    # match_start/match_end span the whole paragraph text.
    result = _locator_result(
        action="DELETE",
        element_type="table_cell",
        body_index=1,
        row_index=0,
        text=f"{target} | ",  # unused for the resolver, see _resolve_cell_and_offsets
        match_start=0,
        match_end=len(target),
        matched_text=target,
        replacement_text=None,
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    cell_paragraphs = saved.tables[0].cell(0, 0).paragraphs
    cell_texts = [p.text for p in cell_paragraphs]
    # Cell now has exactly ONE paragraph (the body line). No empty
    # placeholder paragraph above it.
    assert cell_texts == ["Keep this body line"]


def test_table_cell_whole_paragraph_delete_when_only_paragraph_keeps_empty_para(
    tmp_path: Path,
):
    """Whole-paragraph DELETE on a cell with one paragraph leaves an empty one.

    Word requires every cell to contain at least one paragraph; removing
    the only one would produce an invalid <w:tc> element. The fix is to
    clear the paragraph's text via the existing substring path when the
    cell has just one paragraph left.
    """
    source = tmp_path / "single_para_cell.docx"
    doc = Document()
    doc.add_paragraph("PART 2 PRODUCTS")
    table = doc.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    cell.paragraphs[0].text = "Delete me entirely"
    doc.save(source)

    output = tmp_path / "output.docx"
    target = "Delete me entirely"
    result = _locator_result(
        action="DELETE",
        element_type="table_cell",
        body_index=1,
        row_index=0,
        text=f"{target}",
        match_start=0,
        match_end=len(target),
        matched_text=target,
        replacement_text=None,
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    cell_paragraphs = saved.tables[0].cell(0, 0).paragraphs
    # Exactly one paragraph remains; its text is empty. Word treats this
    # as a valid (empty) cell, not as a missing structural element.
    assert len(cell_paragraphs) == 1
    assert cell_paragraphs[0].text == ""


def test_table_cell_partial_delete_still_uses_substring_path(tmp_path: Path):
    """Partial DELETE (not whole paragraph) is unchanged — substring removal still applies."""
    source = tmp_path / "partial_delete.docx"
    doc = Document()
    doc.add_paragraph("PART 2 PRODUCTS")
    table = doc.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    cell.paragraphs[0].text = "Keep prefix and delete this suffix"
    doc.save(source)

    output = tmp_path / "output.docx"
    target = " and delete this suffix"
    full = "Keep prefix and delete this suffix"
    start = full.index(target)
    result = _locator_result(
        action="DELETE",
        element_type="table_cell",
        body_index=1,
        row_index=0,
        text=full,
        match_start=start,
        match_end=start + len(target),
        matched_text=target,
        replacement_text=None,
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert report.edits_applied == 1
    cell_paragraphs = saved.tables[0].cell(0, 0).paragraphs
    # Paragraph survives, just with the suffix gone.
    assert len(cell_paragraphs) == 1
    assert cell_paragraphs[0].text == "Keep prefix"
