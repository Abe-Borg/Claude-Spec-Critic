from src.edit_locator import locate_edit, locate_edits
from src.extractor import ParagraphMapping
from src.reviewer import Finding
from src.verifier import VerificationResult


def _mapping(text: str, *, idx: int, element_type: str = "paragraph") -> ParagraphMapping:
    return ParagraphMapping(
        body_index=idx,
        element_type=element_type,
        text=text,
        table_index=0 if element_type == "table_cell" else None,
        row_index=0 if element_type == "table_cell" else None,
        cell_index=None,
    )


def _finding(existing: str | None, replacement: str | None = "new") -> Finding:
    return Finding(
        severity="HIGH",
        fileName="spec.docx",
        section="2.1.A",
        issue="Issue",
        actionType="EDIT",
        existingText=existing,
        replacementText=replacement,
        codeReference="CBC",
        confidence=0.9,
    )


def test_locate_edit_exact_match_single_paragraph():
    paragraph_map = [_mapping("Install copper piping with Type L wall thickness.", idx=0)]
    result = locate_edit(_finding("Type L wall thickness"), paragraph_map)

    assert result.status == "matched"
    assert len(result.locations) == 1
    assert result.locations[0].match_confidence == 1.0
    assert result.locations[0].match_method == "exact"


def test_locate_edit_normalized_match_handles_whitespace_and_case():
    paragraph_map = [_mapping("Provide  FIRE\u00a0RATED    assemblies", idx=0)]
    result = locate_edit(_finding("provide fire rated assemblies"), paragraph_map)

    assert result.status == "matched"
    assert result.locations[0].match_method == "normalized"
    assert result.locations[0].match_confidence == 0.90


def test_locate_edit_fuzzy_match_paraphrase():
    paragraph_map = [_mapping("Provide 2-hour fire-resistance rated shaft enclosures.", idx=0)]
    result = locate_edit(_finding("Provide two hour fire resistance shaft enclosure."), paragraph_map)

    assert result.status == "matched"
    assert result.locations[0].match_method == "fuzzy"
    assert 0.80 <= result.locations[0].match_confidence <= 1.0


def test_locate_edit_ambiguous_when_text_appears_multiple_times():
    paragraph_map = [
        _mapping("Use non-shrink grout at equipment bases.", idx=0),
        _mapping("Use non-shrink grout at equipment bases and supports.", idx=1),
    ]
    result = locate_edit(_finding("Use non-shrink grout at equipment bases"), paragraph_map)

    assert result.status == "ambiguous"
    assert len(result.locations) == 2


def test_locate_edit_not_found():
    paragraph_map = [_mapping("No related text here.", idx=0)]
    result = locate_edit(_finding("Completely absent sentence"), paragraph_map)

    assert result.status == "not_found"


def test_replacement_resolution_from_verification_verdicts():
    paragraph_map = [_mapping("Target text here", idx=0)]

    corrected = _finding("Target text", replacement="orig")
    corrected.verification = VerificationResult(verdict="CORRECTED", correction="better", explanation="", sources=[])

    confirmed = _finding("Target text", replacement="orig")
    confirmed.verification = VerificationResult(verdict="CONFIRMED", correction="ignored", explanation="", sources=[])

    disputed = _finding("Target text", replacement="orig")
    disputed.verification = VerificationResult(verdict="DISPUTED", correction="ignored", explanation="", sources=[])

    corrected_result = locate_edit(corrected, paragraph_map)
    confirmed_result = locate_edit(confirmed, paragraph_map)
    disputed_result = locate_edit(disputed, paragraph_map)

    assert corrected_result.replacement_text == "better"
    assert confirmed_result.replacement_text == "orig"
    assert disputed_result.replacement_text is None


def test_short_text_exact_confidence_is_lowered_and_section_anchored_preferred():
    paragraph_map = [
        _mapping("1.0 GENERAL", idx=0),
        _mapping("Voltage shall be 208V", idx=1),
        _mapping("2.0 PRODUCTS", idx=2),
        _mapping("Voltage shall be 480V", idx=3),
    ]
    finding = _finding("208V")
    finding.section = "1.0"

    result = locate_edit(finding, paragraph_map)

    assert result.status == "matched"
    assert result.locations[0].match_method == "section_anchored"
    assert result.locations[0].match_confidence <= 0.70


def test_none_existing_text_returns_not_found_warning():
    paragraph_map = [_mapping("Anything", idx=0)]
    result = locate_edit(_finding(None), paragraph_map)

    assert result.status == "not_found"
    assert result.warning is not None


def test_cross_paragraph_match_returns_multiple_locations_with_warning():
    paragraph_map = [
        _mapping("PART 1 - GENERAL", idx=0),
        _mapping("Provide submittals within ten days.", idx=1),
        _mapping("Submit operation and maintenance manuals.", idx=2),
    ]
    finding = _finding("Provide submittals within ten days.\n\nSubmit operation and maintenance manuals.")

    result = locate_edit(finding, paragraph_map)

    assert result.status == "matched"
    assert len(result.locations) == 2
    assert result.warning is not None


def test_table_row_matching_supports_individual_cell_lookup():
    paragraph_map = [_mapping("R1C1 | Allowance Amount | $10,000", idx=0, element_type="table_cell")]
    result = locate_edit(_finding("Allowance Amount"), paragraph_map)

    assert result.status == "matched"
    assert result.locations[0].matched_text == "Allowance Amount"


def test_locate_edits_batch_helper():
    paragraph_map = [_mapping("Paragraph one", idx=0), _mapping("Paragraph two", idx=1)]
    findings = [_finding("Paragraph one"), _finding("missing")]

    results = locate_edits(findings, paragraph_map)

    assert len(results) == 2
    assert results[0].status == "matched"
    assert results[1].status == "not_found"
