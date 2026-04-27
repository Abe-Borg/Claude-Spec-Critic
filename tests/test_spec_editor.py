from pathlib import Path

import pytest
from docx import Document

from src.edit_locator import EditLocation, LocatorResult
from src.apply_edits import execute_edit_plan
from src.extractor import extract_text_from_docx
from src.extractor import ParagraphMapping
from src.reviewer import Finding
from src.spec_editor import apply_edits_to_spec, build_edit_actions


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


def test_conflict_resolution_skips_lower_confidence_overlap(tmp_path: Path):
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
        confidence=0.90,
    )

    report = apply_edits_to_spec(source, output, build_edit_actions([low, high]))
    saved = Document(output)

    assert saved.paragraphs[0].text == "abc XYZ ghi"
    assert report.edits_applied == 1
    assert report.edits_skipped == 1


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


def test_ambiguous_locator_result_does_not_build_action():
    result = _locator_result(
        status="ambiguous",
        text="Repeated paragraph",
        match_start=0,
        match_end=len("Repeated paragraph"),
        matched_text="Repeated paragraph",
        replacement_text="Replacement",
    )

    actions = build_edit_actions([result])

    assert actions == []
    assert result.warning == "Ambiguous locator result; manual review required."


def test_add_action_inserts_after_explicit_anchor(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Anchor paragraph.")
    doc.add_paragraph("Following paragraph.")
    doc.save(source)

    result = _locator_result(
        action="ADD",
        text="Anchor paragraph.",
        body_index=0,
        match_start=0,
        match_end=len("Anchor paragraph."),
        matched_text="Anchor paragraph.",
        replacement_text="Inserted paragraph.",
    )
    result.finding.anchorText = "Anchor paragraph."
    result.finding.insertPosition = "after"

    report = apply_edits_to_spec(source, output, build_edit_actions([result]))
    saved = Document(output)

    assert [p.text for p in saved.paragraphs] == ["Anchor paragraph.", "Inserted paragraph.", "Following paragraph."]
    assert report.edits_applied == 1


def test_delete_then_add_uses_safe_structural_order(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Delete me.")
    doc.add_paragraph("Anchor paragraph.")
    doc.save(source)

    delete = _locator_result(
        action="DELETE",
        text="Delete me.",
        body_index=0,
        match_start=0,
        match_end=len("Delete me."),
        matched_text="Delete me.",
        replacement_text=None,
    )
    add = _locator_result(
        action="ADD",
        text="Anchor paragraph.",
        body_index=1,
        match_start=0,
        match_end=len("Anchor paragraph."),
        matched_text="Anchor paragraph.",
        replacement_text="Inserted safely.",
    )
    add.finding.anchorText = "Anchor paragraph."
    add.finding.insertPosition = "after"

    report = apply_edits_to_spec(source, output, build_edit_actions([delete, add]))
    saved = Document(output)

    assert [p.text for p in saved.paragraphs] == ["Anchor paragraph.", "Inserted safely."]
    assert report.edits_applied == 2


def test_grouped_finding_edit_expands_to_all_occurrences(tmp_path: Path):
    source_a = tmp_path / "a.docx"
    source_b = tmp_path / "b.docx"
    output_dir = tmp_path / "edited"

    for path in (source_a, source_b):
        doc = Document()
        doc.add_paragraph("Replace this repeated boilerplate.")
        doc.save(path)

    occurrence_a = _finding(existing="Replace this repeated boilerplate.", replacement="Replaced text.")
    occurrence_a.fileName = "a.docx"
    occurrence_a.verification = type("V", (), {"verdict": "CONFIRMED", "correction": None})()
    occurrence_b = _finding(existing="Replace this repeated boilerplate.", replacement="Replaced text.")
    occurrence_b.fileName = "b.docx"
    occurrence_b.verification = type("V", (), {"verdict": "CONFIRMED", "correction": None})()
    grouped = _finding(existing="Replace this repeated boilerplate.", replacement="Replaced text.")
    grouped.fileName = "a.docx"
    grouped.verification = type("V", (), {"verdict": "CONFIRMED", "correction": None})()
    grouped.affected_files = ["a.docx", "b.docx"]
    grouped.occurrences = [occurrence_a, occurrence_b]

    reports = execute_edit_plan(
        [0],
        [grouped],
        [],
        [extract_text_from_docx(source_a), extract_text_from_docx(source_b)],
        [source_a, source_b],
        output_dir,
    )

    assert len(reports) == 2
    assert Document(output_dir / "a_edited.docx").paragraphs[0].text == "Replaced text."
    assert Document(output_dir / "b_edited.docx").paragraphs[0].text == "Replaced text."


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
