"""Eligibility classification for finding-to-edit selection UI."""

from __future__ import annotations

from dataclasses import dataclass

from .reviewer import Finding


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
        anchor_text = (finding.anchorText or "").strip()
        insert_position = (finding.insertPosition or "").strip().lower()
        verification = finding.verification
        verdict = (verification.verdict or "").strip().upper() if verification else ""

        eligible = True
        ineligible_reason: str | None = None
        if action_type not in {"EDIT", "DELETE", "ADD"}:
            eligible = False
            ineligible_reason = f"Unsupported action type: {action_type or 'UNKNOWN'}"

        if eligible and action_type == "ADD":
            if not anchor_text:
                eligible = False
                ineligible_reason = "ADD finding has no anchor text for insertion point"
            elif insert_position not in {"before", "after"}:
                eligible = False
                ineligible_reason = "ADD finding has no insertPosition (\"before\"/\"after\")"
        elif eligible and not existing_text:
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

        candidates.append(
            EditCandidate(
                finding_index=idx,
                finding=finding,
                source_file=finding.fileName or "Unknown",
                eligible=eligible,
                ineligible_reason=ineligible_reason,
                default_selected=eligible and verdict in {"CONFIRMED", "CORRECTED"},
                replacement_text=_resolved_replacement_text(finding),
                verdict_badge=verdict,
                action_type=action_type,
            )
        )

    return candidates
