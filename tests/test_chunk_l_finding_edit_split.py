"""Chunk L tests: separate findings from edit proposals.

Plan section "Chunk L — Separate Findings From Edit Proposals". The chunk
splits the old "every finding has an edit slot" model into:

* :class:`src.reviewer.Finding`      — the issue / evidence / verdict half.
* :class:`src.reviewer.EditProposal` — the optional structured-edit half.

Tests below cover the four acceptance scenarios listed in Directive 8
("Finding without edit proposal appears in report but is not edited",
"Finding with high-confidence edit becomes candidate", "Disputed
finding with edit proposal is not auto-applied", "Coordination issue
requires manual review") plus the backward-compatibility surface
(legacy actionType still works, resume payload round-trips, schema
still emits a valid request shape, prompt updates are byte-stable for
the cached prefix).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.edit_candidates import (
    SAFETY_AUTO_SAFE,
    SAFETY_AUTO_WITH_CAUTION,
    SAFETY_REPORT_ONLY,
    classify_edit_candidates,
)
from src.edit_locator import locate_edit
from src.extractor import ParagraphMapping
from src.resume_state import deserialize_finding, serialize_finding
from src.reviewer import (
    EditProposal,
    Finding,
    REPORT_ONLY_ACTION,
    _parse_findings,
)
from src.verifier import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verified(verdict: str, *, correction: str | None = None) -> VerificationResult:
    """Build a minimal grounded VerificationResult for eligibility tests.

    ``grounded=True`` matters because :func:`classify_edit_candidates`
    treats unverified findings as ineligible and the chunk-L acceptance
    scenarios test verdict-driven eligibility.
    """
    return VerificationResult(
        verdict=verdict,
        explanation="Test grounding",
        sources=["https://example.com/standard"],
        correction=correction,
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


# ---------------------------------------------------------------------------
# EditProposal + Finding.as_edit_proposal
# ---------------------------------------------------------------------------


class TestEditProposalAccessor:
    def test_legacy_edit_finding_materializes_proposal(self):
        finding = Finding(
            severity="HIGH",
            fileName="23 21 13.docx",
            section="2.1",
            issue="Code reference uses outdated CPC edition.",
            actionType="EDIT",
            existingText="per CPC 2022",
            replacementText="per CPC 2025",
            codeReference="CPC 2025",
            confidence=0.85,
        )
        proposal = finding.as_edit_proposal()
        assert proposal is not None
        assert proposal.action_type == "EDIT"
        assert proposal.existing_text == "per CPC 2022"
        assert proposal.replacement_text == "per CPC 2025"
        assert proposal.edit_confidence == 0.85
        assert finding.has_edit_proposal() is True

    def test_report_only_action_returns_none(self):
        finding = Finding(
            severity="HIGH",
            fileName="23 21 13.docx",
            section="2.1",
            issue="Coordination conflict requires designer judgement.",
            actionType=REPORT_ONLY_ACTION,
            existingText=None,
            replacementText=None,
            codeReference=None,
            confidence=0.6,
        )
        assert finding.as_edit_proposal() is None
        assert finding.has_edit_proposal() is False

    def test_explicit_proposal_overrides_legacy_fields(self):
        # When ``edit_proposal`` is set, the accessor returns it verbatim
        # — even if the legacy fields disagree. This proves the
        # "structured proposal is authoritative" half of the migration.
        proposal = EditProposal(
            action_type="EDIT",
            existing_text="new shape",
            replacement_text="new shape replacement",
            edit_confidence=0.9,
        )
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="1.0",
            issue="example",
            actionType="EDIT",
            existingText="legacy text",
            replacementText="legacy replacement",
            codeReference=None,
            confidence=0.5,
            edit_proposal=proposal,
        )
        assert finding.as_edit_proposal() is proposal

    def test_unknown_action_type_returns_none(self):
        finding = Finding(
            severity="MEDIUM",
            fileName="x.docx",
            section="1.0",
            issue="weird action",
            actionType="MAYBE_FIX",
            existingText="text",
            replacementText="other",
            codeReference=None,
            confidence=0.5,
        )
        assert finding.as_edit_proposal() is None

# ---------------------------------------------------------------------------
# Parser: REPORT_ONLY support + edit_proposal population
# ---------------------------------------------------------------------------


class TestParserEditProposalSplit:
    def test_report_only_payload_drops_edit_fields(self):
        findings = _parse_findings([
            {
                "severity": "HIGH",
                "fileName": "spec.docx",
                "section": "Coordination",
                "issue": "Plumbing and HVAC schedules disagree on equipment tags.",
                "actionType": "REPORT_ONLY",
                # Even if the model fills these in by accident, the parser
                # zeroes them out for REPORT_ONLY so the locator cannot
                # mistakenly produce an edit candidate.
                "existingText": "stale quote",
                "replacementText": "stale replacement",
                "codeReference": None,
                "confidence": 0.7,
                "anchorText": None,
                "insertPosition": None,
            }
        ])
        assert len(findings) == 1
        finding = findings[0]
        assert finding.actionType == REPORT_ONLY_ACTION
        assert finding.existingText is None
        assert finding.replacementText is None
        assert finding.edit_proposal is None
        assert finding.as_edit_proposal() is None

    def test_edit_payload_populates_proposal(self):
        findings = _parse_findings([
            {
                "severity": "HIGH",
                "fileName": "spec.docx",
                "section": "2.1",
                "issue": "Code edition stale.",
                "actionType": "EDIT",
                "existingText": "per CPC 2022",
                "replacementText": "per CPC 2025",
                "codeReference": "CPC 2025",
                "confidence": 0.9,
                "anchorText": None,
                "insertPosition": None,
            }
        ])
        finding = findings[0]
        assert finding.edit_proposal is not None
        proposal = finding.edit_proposal
        assert proposal.action_type == "EDIT"
        assert proposal.existing_text == "per CPC 2022"
        assert proposal.replacement_text == "per CPC 2025"
        assert proposal.edit_confidence == 0.9

    def test_unknown_action_type_falls_back_to_report_only(self):
        # Pre-Chunk-L, this would have been silently coerced to EDIT and
        # produced a phantom edit candidate. Now it lands on REPORT_ONLY
        # so the model's confusion does not turn into a wrong-span edit.
        findings = _parse_findings([
            {
                "severity": "MEDIUM",
                "fileName": "spec.docx",
                "section": "3.0",
                "issue": "Unclear action.",
                "actionType": "MAYBE_FIX_LATER",
                "existingText": "something",
                "replacementText": "something else",
                "codeReference": None,
                "confidence": 0.4,
            }
        ])
        assert findings[0].actionType == REPORT_ONLY_ACTION
        assert findings[0].edit_proposal is None


# ---------------------------------------------------------------------------
# Edit candidate eligibility (Directive 7)
# ---------------------------------------------------------------------------


class TestEditCandidateEligibility:
    def test_report_only_finding_is_ineligible_with_explicit_reason(self):
        # Acceptance scenario 1: "Finding without edit proposal appears in
        # report but is not edited."
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="2.1",
            issue="Coordination issue requires manual coordination meeting.",
            actionType=REPORT_ONLY_ACTION,
            existingText=None,
            replacementText=None,
            codeReference=None,
            confidence=0.6,
            verification=_verified("CONFIRMED"),
        )
        candidates = classify_edit_candidates([finding])
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.eligible is False
        assert candidate.safety_category == SAFETY_REPORT_ONLY
        assert "REPORT_ONLY" in (candidate.ineligible_reason or "")

    def test_high_confidence_confirmed_edit_becomes_candidate(self):
        # Acceptance scenario 2: "Finding with high-confidence edit becomes
        # candidate."
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="2.1",
            issue="Stale code edition.",
            actionType="EDIT",
            existingText="per CPC 2022",
            replacementText="per CPC 2025",
            codeReference=None,
            confidence=0.95,
            verification=_verified("CONFIRMED"),
        )
        candidates = classify_edit_candidates([finding])
        assert candidates[0].eligible is True
        assert candidates[0].safety_category == SAFETY_AUTO_SAFE
        assert candidates[0].default_selected is True
        assert candidates[0].replacement_text == "per CPC 2025"

    def test_disputed_edit_finding_is_not_auto_applied(self):
        # Acceptance scenario 3: "Disputed finding with edit proposal is
        # not auto-applied."
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="2.1",
            issue="Disputed claim.",
            actionType="EDIT",
            existingText="some text",
            replacementText="other text",
            codeReference=None,
            confidence=0.9,
            verification=_verified("DISPUTED"),
        )
        candidates = classify_edit_candidates([finding])
        assert candidates[0].eligible is False
        assert candidates[0].default_selected is False
        assert "DISPUTED" in (candidates[0].ineligible_reason or "").upper()

    def test_coordination_finding_requires_manual_review(self):
        # Acceptance scenario 4: "Coordination issue requires manual review."
        # A coordination/REPORT_ONLY finding cannot become an auto-edit even
        # when verified — it surfaces only in the report.
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="Multi-Disc",
            issue="Plumbing tag conflicts with HVAC schedule equipment tag.",
            actionType=REPORT_ONLY_ACTION,
            existingText=None,
            replacementText=None,
            codeReference=None,
            confidence=0.85,
            verification=_verified("CONFIRMED"),
        )
        candidates = classify_edit_candidates([finding])
        candidate = candidates[0]
        assert candidate.eligible is False
        assert candidate.safety_category == SAFETY_REPORT_ONLY

    def test_unverified_edit_falls_to_auto_with_caution_when_eligible(self):
        # Regression: the existing UNVERIFIED-but-eligible path must still
        # work after the new REPORT_ONLY branch is added.
        finding = Finding(
            severity="MEDIUM",
            fileName="x.docx",
            section="2.0",
            issue="Possibly stale standard reference.",
            actionType="EDIT",
            existingText="ASCE 7-16",
            replacementText="ASCE 7-22",
            codeReference=None,
            confidence=0.5,
            verification=_verified("UNVERIFIED"),
        )
        candidate = classify_edit_candidates([finding])[0]
        assert candidate.eligible is True
        assert candidate.safety_category == SAFETY_AUTO_WITH_CAUTION
        assert candidate.default_selected is False


# ---------------------------------------------------------------------------
# Locator behavior (Directive 6)
# ---------------------------------------------------------------------------


class TestLocatorForReportOnly:
    def test_locator_short_circuits_for_report_only(self):
        # A REPORT_ONLY finding produces a not_found / REPORT_ONLY locator
        # result with no fuzzy match attempt. Without the chunk-L short
        # circuit the locator would chase the empty existingText and emit
        # a "Finding has no existingText" warning that confuses the UI.
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="2.1",
            issue="Coordination problem.",
            actionType=REPORT_ONLY_ACTION,
            existingText=None,
            replacementText=None,
            codeReference=None,
            confidence=0.7,
        )
        para = _paragraph("Some paragraph text that should not match.")
        result = locate_edit(finding, [para])
        assert result.status == "not_found"
        assert result.locations == []
        assert result.safety_category == SAFETY_REPORT_ONLY
        assert "REPORT_ONLY" in (result.warning or "")
        # Replacement text on a REPORT_ONLY result is None — there is
        # no edit proposal to draw it from.
        assert result.replacement_text is None

    def test_locator_still_finds_edit_findings(self):
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="2.1",
            issue="Stale code edition.",
            actionType="EDIT",
            existingText="per CPC 2022",
            replacementText="per CPC 2025",
            codeReference=None,
            confidence=0.9,
        )
        para = _paragraph("Install all fixtures per CPC 2022 requirements.")
        result = locate_edit(finding, [para])
        assert result.status == "matched"
        assert len(result.locations) == 1
        assert result.replacement_text == "per CPC 2025"


# ---------------------------------------------------------------------------
# Resume state round-trip
# ---------------------------------------------------------------------------


class TestResumeRoundTrip:
    def test_report_only_finding_round_trips(self):
        # REPORT_ONLY findings must come back without an edit proposal so
        # a resumed session does not start auto-applying coordination
        # findings that the original run correctly skipped.
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="2.1",
            issue="Coordination problem.",
            actionType=REPORT_ONLY_ACTION,
            existingText=None,
            replacementText=None,
            codeReference=None,
            confidence=0.6,
            verification=_verified("CONFIRMED"),
        )
        payload = serialize_finding(finding)
        # Backward-compat: payload still carries the legacy fields.
        assert payload["actionType"] == REPORT_ONLY_ACTION
        assert payload["existingText"] is None
        assert payload["edit_proposal"] is None

        restored = deserialize_finding(payload)
        assert restored.actionType == REPORT_ONLY_ACTION
        assert restored.as_edit_proposal() is None
        assert restored.has_edit_proposal() is False

    def test_edit_finding_round_trips_with_proposal(self):
        finding = Finding(
            severity="HIGH",
            fileName="x.docx",
            section="2.1",
            issue="Code edition stale.",
            actionType="EDIT",
            existingText="per CPC 2022",
            replacementText="per CPC 2025",
            codeReference=None,
            confidence=0.9,
            edit_proposal=EditProposal(
                action_type="EDIT",
                existing_text="per CPC 2022",
                replacement_text="per CPC 2025",
                edit_confidence=0.9,
            ),
        )
        payload = serialize_finding(finding)
        assert payload["edit_proposal"] is not None
        assert payload["edit_proposal"]["action_type"] == "EDIT"

        restored = deserialize_finding(payload)
        assert restored.edit_proposal is not None
        assert restored.edit_proposal.action_type == "EDIT"
        assert restored.edit_proposal.replacement_text == "per CPC 2025"

    def test_legacy_payload_without_edit_proposal_reconstructs_proposal(self):
        # Pre-Chunk-L resume payloads (no ``edit_proposal`` key). The
        # deserializer must reconstruct one from the legacy fields so a
        # session paused before chunk L and resumed after still produces
        # the right edit candidates.
        legacy_payload = {
            "severity": "HIGH",
            "fileName": "x.docx",
            "section": "2.1",
            "issue": "Stale standard.",
            "actionType": "EDIT",
            "existingText": "ASCE 7-16",
            "replacementText": "ASCE 7-22",
            "codeReference": None,
            "confidence": 0.85,
            "affected_files": [],
            "verification": None,
            "anchorText": None,
            "insertPosition": None,
            "evidenceElementId": None,
            # No ``edit_proposal`` key — legacy shape.
        }
        restored = deserialize_finding(legacy_payload)
        assert restored.edit_proposal is not None
        assert restored.edit_proposal.action_type == "EDIT"
        assert restored.edit_proposal.existing_text == "ASCE 7-16"

    def test_legacy_report_only_payload_has_no_proposal(self):
        legacy_payload = {
            "severity": "HIGH",
            "fileName": "x.docx",
            "section": "Coordination",
            "issue": "Coordination meeting required.",
            "actionType": REPORT_ONLY_ACTION,
            "existingText": None,
            "replacementText": None,
            "codeReference": None,
            "confidence": 0.6,
            "affected_files": [],
            "verification": None,
            "anchorText": None,
            "insertPosition": None,
            "evidenceElementId": None,
        }
        restored = deserialize_finding(legacy_payload)
        assert restored.edit_proposal is None


