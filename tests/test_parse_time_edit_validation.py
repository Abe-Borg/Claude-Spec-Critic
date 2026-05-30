"""Tests for validating edit proposals at parse time.

Plan section "Validate edit proposals at parse time". The parser
demotes EDIT / DELETE / ADD findings that omit action-specific required
fields to ``REPORT_ONLY``, clears the executable edit fields, and stamps
a short ``demotion_reason`` so diagnostics, the report, and the
edit-candidate UI all see *why* the proposal was rejected. The chunk's
six acceptance scenarios are exercised below, plus a couple of regression
guards for the surrounding pipeline.
"""

from __future__ import annotations


from src.orchestration.pipeline import _deduplicate_findings
from src.review.reviewer import (
    EditProposal,
    Finding,
    REPORT_ONLY_ACTION,
    _parse_findings,
    validate_edit_shape,
)
from src.verification.verifier import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 1. validate_edit_shape returns specific demotion reasons
# ---------------------------------------------------------------------------


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
        # Whitespace-only existingText is treated as missing.
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

    def test_noop_edit_demotes(self):
        # existingText byte-for-byte identical to replacementText is a no-op
        # edit — find X, replace with the same X. It must demote, not pass.
        reason = validate_edit_shape(
            "EDIT", existing_text="per CPC 2022", replacement_text="per CPC 2022"
        )
        assert reason is not None
        assert "no-op" in reason

    def test_edit_differing_only_by_case_survives(self):
        # Exact-equality scope is deliberate: a case-only delta can be a
        # genuine fix (defined-term capitalization), so it is NOT a no-op.
        assert (
            validate_edit_shape(
                "EDIT", existing_text="shall", replacement_text="Shall"
            )
            is None
        )

    def test_edit_differing_only_by_whitespace_survives(self):
        # Likewise a whitespace delta (e.g. spacing correction) is not
        # byte-equal and is intentionally allowed through.
        assert (
            validate_edit_shape(
                "EDIT", existing_text="NFPA13", replacement_text="NFPA 13"
            )
            is None
        )

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
        # REPORT_ONLY is the explicit "no edit" action; the validator
        # returns None so the parser's cleanup path keeps the finding.
        assert (
            validate_edit_shape(
                REPORT_ONLY_ACTION,
                existing_text=None,
                replacement_text=None,
            )
            is None
        )


# ---------------------------------------------------------------------------
# 2. Parse-time demotion: invalid EDIT / DELETE / ADD payloads
# ---------------------------------------------------------------------------


class TestParseTimeDemotion:
    def test_invalid_edit_demotes_to_report_only(self):
        # Acceptance: "invalid EDIT demotes."
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
        # The finding itself is preserved (issue, severity, file).
        assert f.issue == "Stale code reference."
        assert f.severity == "HIGH"

    def test_noop_edit_demotes_to_report_only(self):
        # A no-op EDIT (replacementText == existingText) must demote end to
        # end so no empty edit instruction reaches the report or the sidecar,
        # while the finding's prose is preserved as REPORT_ONLY.
        findings = _parse_findings(
            [_valid_review_payload(replacementText="per CPC 2022")]
        )
        assert len(findings) == 1
        f = findings[0]
        assert f.actionType == REPORT_ONLY_ACTION
        assert f.existingText is None
        assert f.replacementText is None
        assert f.edit_proposal is None
        assert f.has_edit_proposal() is False
        assert f.demotion_reason is not None
        assert "no-op" in f.demotion_reason
        # The underlying finding is still surfaced.
        assert f.issue == "Stale code reference."
        assert f.severity == "HIGH"

    def test_invalid_add_missing_anchor_demotes(self):
        # Acceptance: "invalid ADD demotes." — anchor missing.
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


# ---------------------------------------------------------------------------
# 3. Valid proposals survive intact
# ---------------------------------------------------------------------------


class TestValidProposalsSurvive:
    def test_valid_edit_survives(self):
        # Acceptance: "valid proposals survive."
        findings = _parse_findings([_valid_review_payload()])
        f = findings[0]
        assert f.actionType == "EDIT"
        assert f.existingText == "per CPC 2022"
        assert f.replacementText == "per CPC 2025"
        assert f.demotion_reason is None
        assert f.edit_proposal is not None
        assert f.edit_proposal.action_type == "EDIT"
        assert f.has_edit_proposal() is True

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


# ---------------------------------------------------------------------------
# 4. REPORT_ONLY with stray edit fields is cleaned
# ---------------------------------------------------------------------------


class TestReportOnlyCleansStrayFields:
    def test_report_only_clears_stray_existing_and_replacement(self):
        # Acceptance: "REPORT_ONLY with stray edit fields is cleaned."
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
        # REPORT_ONLY emitted natively does NOT get a demotion reason —
        # this is the model's explicit choice, not a parser-driven demote.
        assert f.demotion_reason is None


# ---------------------------------------------------------------------------
# 5. Dedup/grouping does not rehydrate invalid edit fields
# ---------------------------------------------------------------------------


class TestDedupDoesNotRehydrate:
    def test_dedup_preserves_demoted_status(self):
        # Acceptance: "dedup/grouping does not rehydrate invalid edit fields."
        # Two identical demoted findings merge into one — the merged finding
        # must still be REPORT_ONLY with cleared fields and the original
        # demotion_reason intact.
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
        # Both demote and have the same dedup identity (REPORT_ONLY +
        # empty existing/replacement), so dedup merges them.
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
        # Construct a demoted finding directly with a stale legacy
        # existingText (as if a buggy code path tried to "fix" the demote
        # afterwards). The merged group must NOT rehydrate a proposal
        # from those legacy fields.
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
        # The finding stays REPORT_ONLY; even though legacy fields are
        # set, ``as_edit_proposal`` rejects the shape because the action
        # is REPORT_ONLY (not in EDIT_ACTION_TYPES).
        assert m.actionType == REPORT_ONLY_ACTION
        assert m.as_edit_proposal() is None


# ---------------------------------------------------------------------------
# 6. Downstream consumers see demoted findings as report-only
# ---------------------------------------------------------------------------


class TestDownstreamConsumers:
    def test_as_edit_proposal_defends_against_legacy_invalid_shapes(self):
        # A Finding constructed directly with actionType="EDIT" but
        # missing existingText must not produce an EditProposal. The
        # parser is the canonical demotion path, but the defensive check
        # in ``as_edit_proposal`` guards legacy resume payloads and
        # ad-hoc test Findings that bypass the parser.
        legacy = Finding(
            severity="MEDIUM",
            fileName="spec.docx",
            section="3.0",
            issue="Stale claim.",
            actionType="EDIT",
            existingText=None,  # invalid for EDIT
            replacementText="new",
            codeReference=None,
            confidence=0.6,
        )
        assert legacy.as_edit_proposal() is None
        assert legacy.has_edit_proposal() is False

    def test_as_edit_proposal_rejects_noop_edit(self):
        # The sidecar and report gate on as_edit_proposal(); a directly
        # constructed no-op EDIT (bypassing the parser, e.g. a legacy resume
        # payload) must not leak a no-op instruction into either.
        noop = Finding(
            severity="MEDIUM",
            fileName="spec.docx",
            section="3.0",
            issue="Reworded but identical.",
            actionType="EDIT",
            existingText="install per manufacturer instructions",
            replacementText="install per manufacturer instructions",
            codeReference=None,
            confidence=0.6,
        )
        assert noop.as_edit_proposal() is None
        assert noop.has_edit_proposal() is False

    def test_explicit_proposal_with_invalid_shape_is_rejected(self):
        # Even when ``edit_proposal`` is set explicitly, an invalid shape
        # is rejected so a buggy resume payload or test can't smuggle a
        # bad proposal past the validator.
        bad_proposal = EditProposal(
            action_type="ADD",
            existing_text=None,
            replacement_text="new",
            anchor_text=None,  # invalid for ADD
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
