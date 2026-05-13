"""Chunk 7 tests: validate edit proposals at parse time.

Plan section "Chunk 7 — Validate edit proposals at parse time". The parser
demotes EDIT / DELETE / ADD findings that omit action-specific required
fields to ``REPORT_ONLY``, clears the executable edit fields, and stamps
a short ``demotion_reason`` so diagnostics, the report, and the
edit-candidate UI all see *why* the proposal was rejected. The chunk's
six acceptance scenarios are exercised below, plus a couple of regression
guards for the surrounding pipeline.
"""

from __future__ import annotations

import pytest

from src.edit_candidates import SAFETY_REPORT_ONLY, classify_edit_candidates
from src.edit_locator import locate_edit
from src.extractor import ParagraphMapping
from src.pipeline import _deduplicate_findings
from src.resume_state import deserialize_finding, serialize_finding
from src.reviewer import (
    EDIT_ACTION_TYPES,
    EditProposal,
    Finding,
    REPORT_ONLY_ACTION,
    _parse_findings,
    validate_edit_shape,
)
from src.verifier import VerificationResult




def _verified(verdict: str) -> VerificationResult:
    return VerificationResult(
        verdict=verdict,
        explanation="t",
        sources=["https://example.com/standard"],
        correction=None,
        grounded=True,
        model_used="fake",
        cache_status="miss",
    )


def _paragraph(text: str, *, body_index: int = 0) -> ParagraphMapping:
    return ParagraphMapping(
        body_index=body_index,
        element_type="paragraph",
        text=text,
        table_index=None,
        row_index=None,
        cell_index=None,
        section_index=0,
        run_count=1,
        distinct_formatting_runs=1,
        element_id=f"p{body_index}",
        section_id="",
    )


def _valid_review_payload(**overrides) -> dict:
    base = {
        "severity": "HIGH",
        "fileName": "spec.docx",
        "section": "2.1",
        "issue": "Stale code reference.",
        "actionType": "EDIT",
        "existingText": "per CPC 2022",
        "replacementText": "per CPC 2025",
        "codeReference": "CPC 2025",
        "confidence": 0.9,
        "anchorText": None,
        "insertPosition": None,
    }
    base.update(overrides)
    return base




class TestValidateEditShape:
    def test_valid_edit_returns_none(self):
        assert (
            validate_edit_shape(
                "EDIT",
                existing_text="old",
                replacement_text="new",
            )
            is None
        )

    def test_valid_delete_returns_none(self):
        assert (
            validate_edit_shape(
                "DELETE",
                existing_text="old",
                replacement_text=None,
            )
            is None
        )

    def test_valid_add_returns_none(self):
        assert (
            validate_edit_shape(
                "ADD",
                existing_text=None,
                replacement_text="new",
                anchor_text="anchor",
                insert_position="after",
            )
            is None
        )

    def test_edit_missing_existing_text_demotes(self):
        reason = validate_edit_shape(
            "EDIT", existing_text=None, replacement_text="new"
        )
        assert reason is not None
        assert "existingText" in reason

    def test_edit_blank_existing_text_demotes(self):
        reason = validate_edit_shape(
            "EDIT", existing_text="   ", replacement_text="new"
        )
        assert reason is not None
        assert "existingText" in reason

    def test_edit_missing_replacement_demotes(self):
        reason = validate_edit_shape(
            "EDIT", existing_text="old", replacement_text=None
        )
        assert reason is not None
        assert "replacementText" in reason

    def test_delete_missing_existing_text_demotes(self):
        reason = validate_edit_shape(
            "DELETE", existing_text=None, replacement_text=None
        )
        assert reason is not None
        assert "DELETE" in reason
        assert "existingText" in reason

    def test_add_missing_anchor_demotes(self):
        reason = validate_edit_shape(
            "ADD",
            existing_text=None,
            replacement_text="new",
            anchor_text=None,
            insert_position="after",
        )
        assert reason is not None
        assert "anchorText" in reason

    def test_add_missing_insert_position_demotes(self):
        reason = validate_edit_shape(
            "ADD",
            existing_text=None,
            replacement_text="new",
            anchor_text="anchor",
            insert_position=None,
        )
        assert reason is not None
        assert "insertPosition" in reason

    def test_add_bogus_insert_position_demotes(self):
        reason = validate_edit_shape(
            "ADD",
            existing_text=None,
            replacement_text="new",
            anchor_text="anchor",
            insert_position="middle",
        )
        assert reason is not None
        assert "insertPosition" in reason

    def test_add_missing_replacement_demotes(self):
        reason = validate_edit_shape(
            "ADD",
            existing_text=None,
            replacement_text=None,
            anchor_text="anchor",
            insert_position="before",
        )
        assert reason is not None
        assert "replacementText" in reason

    def test_report_only_returns_none(self):
        assert (
            validate_edit_shape(
                REPORT_ONLY_ACTION,
                existing_text=None,
                replacement_text=None,
            )
            is None
        )




class TestParseTimeDemotion:
    def test_invalid_edit_demotes_to_report_only(self):
        findings = _parse_findings(
            [_valid_review_payload(existingText=None)]
        )
        assert len(findings) == 1
        f = findings[0]
        assert f.actionType == REPORT_ONLY_ACTION
        assert f.existingText is None
        assert f.replacementText is None
        assert f.edit_proposal is None
        assert f.demotion_reason is not None
        assert "EDIT" in f.demotion_reason
        assert "existingText" in f.demotion_reason
        assert f.issue == "Stale code reference."
        assert f.severity == "HIGH"

    def test_edit_with_empty_replacement_demotes(self):
        findings = _parse_findings(
            [_valid_review_payload(replacementText="")]
        )
        f = findings[0]
        assert f.actionType == REPORT_ONLY_ACTION
        assert f.replacementText is None
        assert f.demotion_reason is not None
        assert "replacementText" in f.demotion_reason

    def test_invalid_delete_demotes_to_report_only(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    actionType="DELETE",
                    existingText=None,
                    replacementText=None,
                )
            ]
        )
        f = findings[0]
        assert f.actionType == REPORT_ONLY_ACTION
        assert f.existingText is None
        assert f.edit_proposal is None
        assert f.demotion_reason is not None
        assert "DELETE" in f.demotion_reason

    def test_invalid_add_missing_anchor_demotes(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    actionType="ADD",
                    existingText=None,
                    replacementText="new paragraph text",
                    anchorText=None,
                    insertPosition="after",
                )
            ]
        )
        f = findings[0]
        assert f.actionType == REPORT_ONLY_ACTION
        assert f.anchorText is None
        assert f.insertPosition is None
        assert f.replacementText is None
        assert f.edit_proposal is None
        assert f.demotion_reason is not None
        assert "ADD" in f.demotion_reason

    def test_invalid_add_missing_position_demotes(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    actionType="ADD",
                    existingText=None,
                    replacementText="new",
                    anchorText="anchor",
                    insertPosition=None,
                )
            ]
        )
        f = findings[0]
        assert f.actionType == REPORT_ONLY_ACTION
        assert f.demotion_reason is not None
        assert "insertPosition" in f.demotion_reason

    def test_invalid_add_missing_replacement_demotes(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    actionType="ADD",
                    existingText=None,
                    replacementText="",
                    anchorText="anchor",
                    insertPosition="after",
                )
            ]
        )
        f = findings[0]
        assert f.actionType == REPORT_ONLY_ACTION
        assert f.replacementText is None
        assert f.demotion_reason is not None
        assert "replacementText" in f.demotion_reason




class TestValidProposalsSurvive:
    def test_valid_edit_survives(self):
        findings = _parse_findings([_valid_review_payload()])
        f = findings[0]
        assert f.actionType == "EDIT"
        assert f.existingText == "per CPC 2022"
        assert f.replacementText == "per CPC 2025"
        assert f.demotion_reason is None
        assert f.edit_proposal is not None
        assert f.edit_proposal.action_type == "EDIT"
        assert f.has_edit_proposal() is True

    def test_valid_delete_survives(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    actionType="DELETE",
                    existingText="redundant clause",
                    replacementText=None,
                )
            ]
        )
        f = findings[0]
        assert f.actionType == "DELETE"
        assert f.existingText == "redundant clause"
        assert f.demotion_reason is None
        assert f.edit_proposal is not None

    def test_valid_add_survives(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    actionType="ADD",
                    existingText=None,
                    replacementText="New requirement.",
                    anchorText="Existing paragraph text.",
                    insertPosition="after",
                )
            ]
        )
        f = findings[0]
        assert f.actionType == "ADD"
        assert f.anchorText == "Existing paragraph text."
        assert f.insertPosition == "after"
        assert f.replacementText == "New requirement."
        assert f.demotion_reason is None
        assert f.edit_proposal is not None
        assert f.edit_proposal.action_type == "ADD"




class TestReportOnlyCleansStrayFields:
    def test_report_only_clears_stray_existing_and_replacement(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    actionType="REPORT_ONLY",
                    existingText="stale quote model emitted by accident",
                    replacementText="stale replacement",
                    anchorText="stale anchor",
                    insertPosition="after",
                )
            ]
        )
        f = findings[0]
        assert f.actionType == REPORT_ONLY_ACTION
        assert f.existingText is None
        assert f.replacementText is None
        assert f.anchorText is None
        assert f.insertPosition is None
        assert f.edit_proposal is None
        assert f.demotion_reason is None




class TestDedupDoesNotRehydrate:
    def test_dedup_preserves_demoted_status(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    fileName="spec1.docx", existingText=None
                ),
                _valid_review_payload(
                    fileName="spec2.docx", existingText=None
                ),
            ]
        )
        assert all(f.actionType == REPORT_ONLY_ACTION for f in findings)
        merged = _deduplicate_findings(findings)
        assert len(merged) == 1
        m = merged[0]
        assert m.actionType == REPORT_ONLY_ACTION
        assert m.existingText is None
        assert m.replacementText is None
        assert m.edit_proposal is None
        assert m.demotion_reason is not None

    def test_dedup_does_not_resurrect_proposal_from_legacy_field(self):
        demoted = Finding(
            severity="HIGH",
            fileName="spec1.docx",
            section="2.1",
            issue="Stale code reference.",
            actionType=REPORT_ONLY_ACTION,
            existingText="leaked stale quote",
            replacementText="leaked stale replacement",
            codeReference="CPC 2025",
            confidence=0.9,
            demotion_reason="EDIT action missing required existingText",
        )
        merged = _deduplicate_findings([demoted])
        m = merged[0]
        assert m.actionType == REPORT_ONLY_ACTION
        assert m.as_edit_proposal() is None




class TestDownstreamConsumers:
    def test_edit_candidates_marks_demoted_finding_ineligible(self):
        findings = _parse_findings(
            [_valid_review_payload(replacementText="")]
        )
        f = findings[0]
        f.verification = _verified("CONFIRMED")
        candidates = classify_edit_candidates([f])
        c = candidates[0]
        assert c.eligible is False
        assert c.safety_category == SAFETY_REPORT_ONLY
        assert "Demoted to REPORT_ONLY" in (c.ineligible_reason or "")
        assert "replacementText" in (c.ineligible_reason or "")

    def test_locator_short_circuits_for_demoted_finding(self):
        findings = _parse_findings(
            [
                _valid_review_payload(
                    actionType="ADD",
                    existingText=None,
                    replacementText="new",
                    anchorText=None,
                    insertPosition="after",
                )
            ]
        )
        f = findings[0]
        result = locate_edit(
            f, [_paragraph("Some paragraph", body_index=0)]
        )
        assert result.status == "not_found"
        assert result.safety_category == SAFETY_REPORT_ONLY

    def test_as_edit_proposal_defends_against_legacy_invalid_shapes(self):
        legacy = Finding(
            severity="MEDIUM",
            fileName="spec.docx",
            section="3.0",
            issue="Stale claim.",
            actionType="EDIT",
            existingText=None,
            replacementText="new",
            codeReference=None,
            confidence=0.6,
        )
        assert legacy.as_edit_proposal() is None
        assert legacy.has_edit_proposal() is False

    def test_explicit_proposal_with_invalid_shape_is_rejected(self):
        bad_proposal = EditProposal(
            action_type="ADD",
            existing_text=None,
            replacement_text="new",
            anchor_text=None,
            insert_position="after",
        )
        f = Finding(
            severity="HIGH",
            fileName="spec.docx",
            section="2.1",
            issue="Stale claim.",
            actionType="ADD",
            existingText=None,
            replacementText="new",
            codeReference=None,
            confidence=0.7,
            anchorText=None,
            insertPosition="after",
            edit_proposal=bad_proposal,
        )
        assert f.as_edit_proposal() is None




class TestResumeStateRoundTrip:
    def test_demotion_reason_round_trips_through_resume_state(self):
        findings = _parse_findings(
            [_valid_review_payload(existingText=None)]
        )
        original = findings[0]
        payload = serialize_finding(original)
        assert payload["demotion_reason"] == original.demotion_reason
        restored = deserialize_finding(payload)
        assert restored.demotion_reason == original.demotion_reason
        assert restored.actionType == REPORT_ONLY_ACTION
        assert restored.existingText is None

    def test_legacy_resume_payload_loads_with_demotion_reason_none(self):
        legacy_payload = {
            "severity": "HIGH",
            "fileName": "spec.docx",
            "section": "2.1",
            "issue": "Legacy.",
            "actionType": "EDIT",
            "existingText": "old",
            "replacementText": "new",
            "codeReference": None,
            "confidence": 0.8,
            "affected_files": [],
            "verification": None,
            "anchorText": None,
            "insertPosition": None,
            "evidenceElementId": None,
        }
        restored = deserialize_finding(legacy_payload)
        assert restored.demotion_reason is None
        assert restored.actionType == "EDIT"




def test_native_report_only_emission_has_no_demotion_reason():
    findings = _parse_findings(
        [_valid_review_payload(actionType="REPORT_ONLY", existingText=None, replacementText=None)]
    )
    assert findings[0].demotion_reason is None
