from src.edit_candidates import classify_edit_candidates
from src.reviewer import Finding
from src.verifier import VerificationResult


def _finding(
    *,
    action="EDIT",
    existing="text",
    replacement="new",
    verdict="CONFIRMED",
    correction=None,
    anchor_text=None,
    insert_position=None,
):
    if action == "ADD":
        if anchor_text is None:
            anchor_text = "Anchor paragraph text"
        if insert_position is None:
            insert_position = "after"
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
        anchorText=anchor_text,
        insertPosition=insert_position,
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


def test_disputed_marked_ineligible_with_reason():
    candidates = classify_edit_candidates([_finding(verdict="DISPUTED")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.eligible is False
    assert c.default_selected is False
    assert c.ineligible_reason == "Finding was disputed by the verifier"
    assert c.verdict_badge == "DISPUTED"


def test_add_action_included_and_eligible_when_verified():
    candidates = classify_edit_candidates([_finding(action="ADD", verdict="CONFIRMED")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.action_type == "ADD"
    assert c.eligible is True
    assert c.default_selected is True
    assert c.ineligible_reason is None


def test_none_verification_marked_ineligible():
    candidates = classify_edit_candidates([_finding(verdict=None)])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.eligible is False
    assert c.default_selected is False
    assert c.ineligible_reason == "Finding has not been verified"


def test_empty_existing_text_marked_ineligible():
    candidates = classify_edit_candidates([_finding(existing="  ", verdict="CONFIRMED")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.eligible is False
    assert c.default_selected is False
    assert c.ineligible_reason == "Finding has no anchor text to locate in the document"


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

    assert len(candidates) == 5
    assert [c.verdict_badge for c in candidates] == ["CONFIRMED", "CONFIRMED", "UNVERIFIED", "DISPUTED", "CORRECTED"]
    assert [c.finding_index for c in candidates] == [0, 1, 2, 3, 4]
    assert [c.eligible for c in candidates] == [True, True, True, False, True]
    assert candidates[3].ineligible_reason == "Finding was disputed by the verifier"


def test_add_action_without_anchor_text_marked_ineligible():
    candidates = classify_edit_candidates([_finding(action="ADD", verdict="CONFIRMED", anchor_text="")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.eligible is False
    assert c.ineligible_reason == "ADD finding has no anchor text for insertion point"


def test_add_action_without_insert_position_marked_ineligible():
    candidates = classify_edit_candidates([_finding(action="ADD", verdict="CONFIRMED", anchor_text="Anchor", insert_position="")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.eligible is False
    assert "insertPosition" in c.ineligible_reason


def test_add_action_with_invalid_insert_position_marked_ineligible():
    candidates = classify_edit_candidates([_finding(action="ADD", verdict="CONFIRMED", anchor_text="Anchor", insert_position="middle")])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.eligible is False
    assert "insertPosition" in c.ineligible_reason
