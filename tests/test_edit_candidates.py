from src.edit_candidates import classify_edit_candidates
from src.reviewer import Finding
from src.verifier import VerificationResult


def _finding(*, action="EDIT", existing="text", replacement="new", verdict="CONFIRMED", correction=None):
    f = Finding(
        severity="HIGH",
        fileName="spec.docx",
        section="2.1",
        issue="Issue",
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference="CBC",
        confidence=0.9,
    )
    if verdict is not None:
        f.verification = VerificationResult(verdict=verdict, explanation="", sources=[], correction=correction)
    return f


def test_confirmed_edit_defaults_selected():
    candidates = classify_edit_candidates([_finding(verdict="CONFIRMED")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.eligible is True
    assert c.default_selected is True
    assert c.verdict_badge == "CONFIRMED"


def test_corrected_uses_correction_replacement_text():
    candidates = classify_edit_candidates([_finding(verdict="CORRECTED", replacement="old", correction="better")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.default_selected is True
    assert c.verdict_badge == "CORRECTED"
    assert c.replacement_text == "better"


def test_unverified_included_unchecked_by_default():
    candidates = classify_edit_candidates([_finding(verdict="UNVERIFIED")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.default_selected is False
    assert c.verdict_badge == "UNVERIFIED"


def test_disputed_filtered_out():
    candidates = classify_edit_candidates([_finding(verdict="DISPUTED")])

    assert candidates == []


def test_add_action_filtered_out():
    candidates = classify_edit_candidates([_finding(action="ADD", verdict="CONFIRMED")])

    assert candidates == []


def test_none_verification_filtered_out():
    candidates = classify_edit_candidates([_finding(verdict=None)])

    assert candidates == []


def test_empty_existing_text_filtered_out():
    candidates = classify_edit_candidates([_finding(existing="  ", verdict="CONFIRMED")])

    assert candidates == []


def test_mixed_findings_counts_and_ordering_with_cross_check():
    main = [
        _finding(verdict="CONFIRMED"),
        _finding(action="ADD", verdict="CONFIRMED"),
        _finding(verdict="UNVERIFIED"),
    ]
    cross = [
        _finding(verdict="DISPUTED"),
        _finding(verdict="CORRECTED", correction="fixed"),
    ]

    candidates = classify_edit_candidates(main, cross_check_findings=cross, include_cross_check=True)

    assert len(candidates) == 3
    assert [c.verdict_badge for c in candidates] == ["CONFIRMED", "UNVERIFIED", "CORRECTED"]
    assert [c.finding_index for c in candidates] == [0, 2, 4]
