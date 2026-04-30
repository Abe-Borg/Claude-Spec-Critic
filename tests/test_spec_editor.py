from pathlib import Path

import pytest
from docx import Document

from src.edit_locator import EditLocation, LocatorResult
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
        confidence=0.70,
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


def test_apply_edits_downgrades_when_replacement_spans_distinct_formatting(tmp_path: Path):
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    p = doc.add_paragraph()
    p.add_run("Provide bracing per ")
    bold_run = p.add_run("ASCE")
    bold_run.bold = True
    italic_run = p.add_run(" 7-16")
    italic_run.italic = True
    p.add_run(".")
    doc.save(source)

    text = "Provide bracing per ASCE 7-16."
    result = _locator_result(
        text=text,
        match_start=text.index("ASCE 7-16"),
        match_end=text.index("ASCE 7-16") + len("ASCE 7-16"),
        matched_text="ASCE 7-16",
        replacement_text="ASCE 7-22",
    )
    report = apply_edits_to_spec(source, output, build_edit_actions([result]))

    saved = Document(output)
    assert saved.paragraphs[0].text == text  # No mutation should have occurred.
    assert report.edits_applied == 0
    assert report.edits_skipped == 1
    skipped = [o for o in report.outcomes if o.status == "skipped"]
    assert any("manual review" in o.detail for o in skipped)


def test_execute_edit_plan_comments_mode_writes_change_log_without_mutating(tmp_path: Path):
    from src.apply_edits import execute_edit_plan
    from src.extractor import extract_text_from_docx
    from src.verifier import VerificationResult

    source = tmp_path / "spec.docx"
    doc = Document()
    doc.add_paragraph("Provide seismic bracing per ASCE 7-16.")
    doc.save(source)

    spec = extract_text_from_docx(source)
    finding = Finding(
        severity="HIGH",
        fileName="spec.docx",
        section="1.0",
        issue="Outdated standard reference",
        actionType="EDIT",
        existingText="ASCE 7-16",
        replacementText="ASCE 7-22",
        codeReference="ASCE 7-22",
        confidence=0.9,
    )
    finding.verification = VerificationResult(verdict="CONFIRMED")

    output_dir = tmp_path / "out"
    reports = execute_edit_plan(
        selected_finding_indices=[0],
        all_findings=[finding],
        cross_check_findings=[],
        extracted_specs=[spec],
        source_paths=[source],
        output_dir=output_dir,
        mode="comments",
    )

    change_log = output_dir / "spec_proposed_edits.md"
    assert change_log.exists()
    body = change_log.read_text(encoding="utf-8")
    assert "ASCE 7-16" in body
    assert "ASCE 7-22" in body

    # Ensure no _edited.docx file was created and source is untouched.
    assert not (output_dir / "spec_edited.docx").exists()
    saved = Document(source)
    assert saved.paragraphs[0].text == "Provide seismic bracing per ASCE 7-16."

    assert len(reports) == 1
    assert reports[0].edits_applied == 0
    assert reports[0].edits_skipped == 1


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
