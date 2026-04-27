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


def test_expand_finding_across_affected_files_unaffected_when_single_file():
    from src.apply_edits import _expand_finding_across_affected_files

    f = Finding(
        severity="HIGH",
        fileName="a.docx",
        section="1",
        issue="i",
        actionType="EDIT",
        existingText="x",
        replacementText="y",
        codeReference="C",
        confidence=0.9,
    )
    expanded = _expand_finding_across_affected_files(f)
    assert len(expanded) == 1
    assert expanded[0] is f


def test_expand_finding_across_affected_files_clones_per_file():
    from src.apply_edits import _expand_finding_across_affected_files

    f = Finding(
        severity="HIGH",
        fileName="a.docx",
        section="1",
        issue="i",
        actionType="EDIT",
        existingText="x",
        replacementText="y",
        codeReference="C",
        confidence=0.9,
        affected_files=["a.docx", "b.docx", "c.docx"],
    )
    expanded = _expand_finding_across_affected_files(f)
    assert [clone.fileName for clone in expanded] == ["a.docx", "b.docx", "c.docx"]
    # Clones preserve all other attributes
    for clone in expanded:
        assert clone.existingText == "x"
        assert clone.replacementText == "y"
        assert clone.severity == "HIGH"


def test_execute_edit_plan_applies_grouped_finding_to_every_affected_file(tmp_path: Path):
    from src.apply_edits import execute_edit_plan
    from src.extractor import extract_text_from_docx
    from src.verifier import VerificationResult

    source_a = tmp_path / "a.docx"
    source_b = tmp_path / "b.docx"
    output_dir = tmp_path / "out"

    for path in (source_a, source_b):
        doc = Document()
        doc.add_paragraph("Provide seismic bracing per ASCE 7-16.")
        doc.save(path)

    spec_a = extract_text_from_docx(source_a)
    spec_b = extract_text_from_docx(source_b)

    grouped = Finding(
        severity="HIGH",
        fileName="a.docx",
        section="1",
        issue="Outdated ASCE reference (found in 2 specs: a.docx, b.docx)",
        actionType="EDIT",
        existingText="ASCE 7-16",
        replacementText="ASCE 7-22",
        codeReference="ASCE",
        confidence=0.95,
        affected_files=["a.docx", "b.docx"],
    )
    grouped.verification = VerificationResult(verdict="CONFIRMED", explanation="ok", sources=[], correction=None)

    reports = execute_edit_plan(
        selected_finding_indices=[0],
        all_findings=[grouped],
        cross_check_findings=[],
        extracted_specs=[spec_a, spec_b],
        source_paths=[source_a, source_b],
        output_dir=output_dir,
    )

    assert len(reports) == 2
    output_paths = sorted(report.output_path.name for report in reports)
    assert output_paths == ["a_edited.docx", "b_edited.docx"]
    for report in reports:
        assert report.edits_applied == 1
        saved = Document(report.output_path)
        assert saved.paragraphs[0].text == "Provide seismic bracing per ASCE 7-22."


def test_build_edit_actions_skips_ambiguous_locator_results():
    text = "Use non-shrink grout at equipment bases."
    mapping = ParagraphMapping(body_index=0, element_type="paragraph", text=text, table_index=None, row_index=None, cell_index=None)
    loc_a = EditLocation(mapping=mapping, match_start=0, match_end=len(text), matched_text=text, match_confidence=0.95, match_method="exact")
    loc_b = EditLocation(mapping=mapping, match_start=0, match_end=len(text), matched_text=text, match_confidence=0.93, match_method="exact")
    finding = Finding(severity="HIGH", fileName="spec.docx", section="1", issue="i", actionType="EDIT", existingText=text, replacementText="x", codeReference="C", confidence=0.9)
    result = LocatorResult(finding=finding, status="ambiguous", locations=[loc_a, loc_b], replacement_text="x", action_type="EDIT", warning=None)

    actions = build_edit_actions([result])
    assert actions == []
    assert result.warning is not None
    assert "manual review" in result.warning.lower()


def test_build_edit_actions_skips_cross_paragraph_span_match():
    para_one = ParagraphMapping(body_index=1, element_type="paragraph", text="Provide submittals within ten days.", table_index=None, row_index=None, cell_index=None)
    para_two = ParagraphMapping(body_index=2, element_type="paragraph", text="Submit operation and maintenance manuals.", table_index=None, row_index=None, cell_index=None)
    loc_one = EditLocation(mapping=para_one, match_start=0, match_end=len(para_one.text), matched_text=para_one.text, match_confidence=0.88, match_method="exact")
    loc_two = EditLocation(mapping=para_two, match_start=0, match_end=len(para_two.text), matched_text=para_two.text, match_confidence=0.88, match_method="exact")
    finding = Finding(severity="HIGH", fileName="spec.docx", section="1", issue="i", actionType="EDIT", existingText=para_one.text + "\n\n" + para_two.text, replacementText="x", codeReference="C", confidence=0.9)
    result = LocatorResult(finding=finding, status="matched", locations=[loc_one, loc_two], replacement_text="x", action_type="EDIT", warning="Matched text spans multiple paragraphs; review before auto-applying edit.")

    actions = build_edit_actions([result])
    assert actions == []


def test_build_edit_actions_allows_unique_match():
    text = "Provide seismic bracing per ASCE 7-16."
    mapping = ParagraphMapping(body_index=0, element_type="paragraph", text=text, table_index=None, row_index=None, cell_index=None)
    loc = EditLocation(mapping=mapping, match_start=28, match_end=37, matched_text="ASCE 7-16", match_confidence=1.0, match_method="exact")
    finding = Finding(severity="HIGH", fileName="spec.docx", section="1", issue="i", actionType="EDIT", existingText="ASCE 7-16", replacementText="ASCE 7-22", codeReference="C", confidence=0.9)
    result = LocatorResult(finding=finding, status="matched", locations=[loc], replacement_text="ASCE 7-22", action_type="EDIT", warning=None)

    actions = build_edit_actions([result])
    assert len(actions) == 1
    assert actions[0].location is loc


def test_add_action_inserts_after_anchor_using_explicit_position(tmp_path: Path):
    from src.edit_locator import locate_edit
    from src.extractor import extract_text_from_docx

    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Provide submittals within ten days.")
    doc.add_paragraph("End of section.")
    doc.save(source)

    spec = extract_text_from_docx(source)
    finding = Finding(
        severity="HIGH",
        fileName="source.docx",
        section="1",
        issue="Add seismic restraint requirement",
        actionType="ADD",
        existingText=None,
        replacementText="Provide seismic restraints per ASCE 7-22.",
        codeReference="ASCE",
        confidence=0.95,
        anchorText="Provide submittals within ten days.",
        insertPosition="after",
    )

    result = locate_edit(finding, spec.paragraph_map)
    actions = build_edit_actions([result])
    assert len(actions) == 1

    apply_edits_to_spec(source, output, actions)
    saved = Document(output)
    paragraphs = [p.text for p in saved.paragraphs]
    assert paragraphs == [
        "Provide submittals within ten days.",
        "Provide seismic restraints per ASCE 7-22.",
        "End of section.",
    ]


def test_add_action_inserts_before_anchor_using_explicit_position(tmp_path: Path):
    from src.edit_locator import locate_edit
    from src.extractor import extract_text_from_docx

    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("First paragraph.")
    doc.add_paragraph("Last paragraph.")
    doc.save(source)

    spec = extract_text_from_docx(source)
    finding = Finding(
        severity="HIGH",
        fileName="source.docx",
        section="1",
        issue="Insert preamble",
        actionType="ADD",
        existingText=None,
        replacementText="Preamble paragraph.",
        codeReference="C",
        confidence=0.95,
        anchorText="Last paragraph.",
        insertPosition="before",
    )

    result = locate_edit(finding, spec.paragraph_map)
    actions = build_edit_actions([result])
    apply_edits_to_spec(source, output, actions)

    saved = Document(output)
    paragraphs = [p.text for p in saved.paragraphs]
    assert paragraphs == ["First paragraph.", "Preamble paragraph.", "Last paragraph."]


def test_delete_then_add_inserts_at_correct_anchor_after_index_shift(tmp_path: Path):
    from src.edit_locator import locate_edit
    from src.extractor import extract_text_from_docx

    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"

    doc = Document()
    doc.add_paragraph("Keep paragraph 0")
    doc.add_paragraph("Delete me paragraph 1")
    doc.add_paragraph("Anchor paragraph 2")
    doc.add_paragraph("Tail paragraph 3")
    doc.save(source)

    spec = extract_text_from_docx(source)

    delete_finding = Finding(
        severity="HIGH",
        fileName="source.docx",
        section="1",
        issue="Delete me",
        actionType="DELETE",
        existingText="Delete me paragraph 1",
        replacementText=None,
        codeReference="C",
        confidence=0.95,
    )
    add_finding = Finding(
        severity="HIGH",
        fileName="source.docx",
        section="1",
        issue="Add new content after anchor",
        actionType="ADD",
        existingText=None,
        replacementText="Inserted paragraph",
        codeReference="C",
        confidence=0.95,
        anchorText="Anchor paragraph 2",
        insertPosition="after",
    )

    delete_result = locate_edit(delete_finding, spec.paragraph_map)
    add_result = locate_edit(add_finding, spec.paragraph_map)
    actions = build_edit_actions([delete_result, add_result])
    assert len(actions) == 2

    apply_edits_to_spec(source, output, actions)
    saved = Document(output)
    paragraphs = [p.text for p in saved.paragraphs]
    assert paragraphs == [
        "Keep paragraph 0",
        "Anchor paragraph 2",
        "Inserted paragraph",
        "Tail paragraph 3",
    ]


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
