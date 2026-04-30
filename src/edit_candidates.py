"""Eligibility classification for finding-to-edit selection UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .reviewer import Finding


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


def _resolved_replacement_text(finding: Finding) -> str | None:
    verification = finding.verification
    if verification is None:
        return finding.replacementText
    if verification.verdict == "CORRECTED" and verification.correction:
        return verification.correction
    return finding.replacementText


def classify_edit_candidates(
    findings: list[Finding],
    *,
    include_cross_check: bool = True,
    cross_check_findings: list[Finding] | None = None,
) -> list[EditCandidate]:
    """Return edit candidates for selection UI, including ineligible findings."""
    merged: list[Finding] = list(findings)
    if include_cross_check and cross_check_findings:
        merged.extend(cross_check_findings)

    candidates: list[EditCandidate] = []
    for idx, finding in enumerate(merged):
        action_type = (finding.actionType or "").strip().upper()
        existing_text = (finding.existingText or "").strip()
        verification = finding.verification
        verdict = (verification.verdict or "").strip().upper() if verification else ""

        eligible = True
        ineligible_reason: str | None = None
        if action_type not in {"EDIT", "DELETE", "ADD"}:
            eligible = False
            ineligible_reason = f"Unsupported action type: {action_type or 'UNKNOWN'}"

        # ADD actions may use the explicit anchorText field instead of
        # existingText to locate the insertion point (audit Issue 5).
        anchor_text = (getattr(finding, "anchorText", None) or "").strip()
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
                replacement_text=_resolved_replacement_text(finding),
                verdict_badge=verdict,
                action_type=action_type,
                safety_category=safety_category,
            )
        )

    return candidates
