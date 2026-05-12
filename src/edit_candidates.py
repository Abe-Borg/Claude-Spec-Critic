"""Eligibility classification for finding-to-edit selection UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .reviewer import EDIT_ACTION_TYPES, EditProposal, Finding, REPORT_ONLY_ACTION


# Phase 4 edit-safety categories (audit Section 8.1). The eligibility flag and
# default_selected stay for UI back-compat; safety_category gives downstream
# code (and the locator/spec_editor) a single dimension on which to gate
# auto-application versus manual review.
SAFETY_AUTO_SAFE: Final[str] = "AUTO_SAFE"
SAFETY_AUTO_WITH_CAUTION: Final[str] = "AUTO_WITH_CAUTION"
SAFETY_MANUAL_REVIEW: Final[str] = "MANUAL_REVIEW"
SAFETY_REPORT_ONLY: Final[str] = "REPORT_ONLY"


@dataclass
class EditCandidate:
    finding_index: int
    finding: Finding
    source_file: str
    eligible: bool
    ineligible_reason: str | None
    default_selected: bool
    replacement_text: str | None
    verdict_badge: str
    action_type: str
    safety_category: str = SAFETY_REPORT_ONLY


def _resolved_replacement_text(
    finding: Finding, proposal: EditProposal | None
) -> str | None:
    """Resolve the actual replacement text for an edit, preferring verifier corrections.

    Chunk L: when there is no edit proposal, there is no replacement text
    either — return None so the UI/edit pipeline does not show a stale
    quote that the model emitted before the parser zeroed it out.
    """
    if proposal is None:
        return None
    verification = finding.verification
    if verification is None:
        return proposal.replacement_text
    if verification.verdict == "CORRECTED" and verification.correction:
        return verification.correction
    return proposal.replacement_text


def classify_edit_candidates(
    findings: list[Finding],
    *,
    include_cross_check: bool = True,
    cross_check_findings: list[Finding] | None = None,
) -> list[EditCandidate]:
    """Return edit candidates for selection UI, including ineligible findings.

    Chunk L / plan section "Separate Findings From Edit Proposals": this
    pass routes through :meth:`Finding.as_edit_proposal` so REPORT_ONLY
    findings (and findings whose ``edit_proposal`` was zeroed out at parse
    time) cleanly land in the ineligible bucket with a clear reason
    rather than masquerading as "unsupported action type". The acceptance
    criteria in directive 7 are enforced in priority order:

    1. Has an edit proposal at all (else REPORT_ONLY).
    2. Has a usable anchor (existingText for EDIT/DELETE, anchorText for ADD).
    3. Has been verified.
    4. Is not DISPUTED.
    5. Verdict is recognized.

    The default-selected and safety-category rules are unchanged so
    existing UI behavior is preserved for legacy findings that still
    arrive with the old shape.
    """
    merged: list[Finding] = list(findings)
    if include_cross_check and cross_check_findings:
        merged.extend(cross_check_findings)

    candidates: list[EditCandidate] = []
    for idx, finding in enumerate(merged):
        proposal = finding.as_edit_proposal()
        action_type = (proposal.action_type if proposal else (finding.actionType or "")).strip().upper()
        existing_text = (proposal.existing_text or "").strip() if proposal else ""
        anchor_text = (proposal.anchor_text or "").strip() if proposal else ""
        verification = finding.verification
        verdict = (verification.verdict or "").strip().upper() if verification else ""

        eligible = True
        ineligible_reason: str | None = None

        # Chunk L Directive 6: edit-candidate generation considers only
        # findings with valid edit proposals. REPORT_ONLY and findings
        # whose legacy actionType is outside the EDIT_ACTION_TYPES set
        # produce ``proposal is None`` and fall straight into the
        # "report-only" bucket with a clear, user-readable reason.
        #
        # Chunk 7: when the parser demoted an EDIT/DELETE/ADD because a
        # required field was missing, the finding now carries the specific
        # reason on ``demotion_reason``. Surface it so the UI explains
        # *why* the proposal was rejected (e.g., "EDIT action missing
        # required existingText") rather than reporting a generic
        # REPORT_ONLY or — worse — falling through to the legacy "no
        # anchor text" branch and inventing a misleading explanation.
        if proposal is None:
            eligible = False
            demotion = (finding.demotion_reason or "").strip()
            if demotion:
                ineligible_reason = (
                    f"Demoted to REPORT_ONLY at parse time: {demotion}"
                )
            elif (finding.actionType or "").strip().upper() == REPORT_ONLY_ACTION:
                ineligible_reason = (
                    "Finding is REPORT_ONLY — surfaced in the report but has "
                    "no edit proposal to apply."
                )
            else:
                ineligible_reason = (
                    f"Unsupported action type: {finding.actionType or 'UNKNOWN'}"
                )

        # ADD actions may use the explicit anchorText field instead of
        # existingText to locate the insertion point (audit Issue 5).
        has_anchor_for_add = action_type == "ADD" and bool(anchor_text)
        if eligible and not existing_text and not has_anchor_for_add:
            eligible = False
            ineligible_reason = "Finding has no anchor text to locate in the document"

        if eligible and verification is None:
            eligible = False
            ineligible_reason = "Finding has not been verified"

        if eligible and verdict == "DISPUTED":
            eligible = False
            ineligible_reason = "Finding was disputed by the verifier"

        if eligible and verdict not in {"CONFIRMED", "CORRECTED", "UNVERIFIED"}:
            eligible = False
            ineligible_reason = f"Unrecognized verification verdict: {verdict or 'UNKNOWN'}"

        default_selected = eligible and verdict in {"CONFIRMED", "CORRECTED"}
        if not eligible:
            safety_category = SAFETY_REPORT_ONLY
        elif verdict in {"CONFIRMED", "CORRECTED"}:
            safety_category = SAFETY_AUTO_SAFE
        else:
            # UNVERIFIED is eligible but not auto-selected; treat as caution.
            safety_category = SAFETY_AUTO_WITH_CAUTION

        candidates.append(
            EditCandidate(
                finding_index=idx,
                finding=finding,
                source_file=finding.fileName or "Unknown",
                eligible=eligible,
                ineligible_reason=ineligible_reason,
                default_selected=default_selected,
                replacement_text=_resolved_replacement_text(finding, proposal),
                verdict_badge=verdict,
                action_type=action_type,
                safety_category=safety_category,
            )
        )

    return candidates
