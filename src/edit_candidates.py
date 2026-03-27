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
    """Return actionable edit candidates for selection UI.

    Only eligible, actionable findings are returned:
      - actionType in {EDIT, DELETE}
      - verification present and verdict in {CONFIRMED, CORRECTED, UNVERIFIED}
      - existingText is non-empty
    """
    merged: list[Finding] = list(findings)
    if include_cross_check and cross_check_findings:
        merged.extend(cross_check_findings)

    candidates: list[EditCandidate] = []
    for idx, finding in enumerate(merged):
        action_type = (finding.actionType or "").strip().upper()
        if action_type not in {"EDIT", "DELETE"}:
            continue

        existing_text = (finding.existingText or "").strip()
        if not existing_text:
            continue

        verification = finding.verification
        if verification is None:
            continue

        verdict = (verification.verdict or "").strip().upper()
        if verdict == "DISPUTED":
            continue
        if verdict not in {"CONFIRMED", "CORRECTED", "UNVERIFIED"}:
            continue

        candidates.append(
            EditCandidate(
                finding_index=idx,
                finding=finding,
                source_file=finding.fileName or "Unknown",
                eligible=True,
                ineligible_reason=None,
                default_selected=verdict in {"CONFIRMED", "CORRECTED"},
                replacement_text=_resolved_replacement_text(finding),
                verdict_badge=verdict,
                action_type=action_type,
            )
        )

    return candidates
