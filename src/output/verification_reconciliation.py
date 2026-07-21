"""Reconcile verification verdicts back onto the requirements profile (E5).

The research profile is rendered into the report (and the compliance pass's
context) as-written at research time. When round-1/round-2 verification later
CORRECTS or DISPUTES a finding that was built on one of those researched
claims, nothing previously flowed back — the profile bullets, the editions
table, the coverage matrix, and the exported ``.profile.json`` all kept
rendering the uncorrected claim (observed live: a verification-CORRECTED
"Climate Zone 7" research claim still rendered uncorrected in four places).

This module is a pure report-time helper — no pipeline changes, no
persistence changes, no LLM calls:

* :func:`collect_verification_corrections` selects findings whose verdict is
  CORRECTED / DISPUTED (or whose classified status is VERIFIED_CONTESTED —
  two grounded verifiers disagreed) and extracts the ``r-`` requirement-item
  ids referenced in the finding's text and the verifier's rationale /
  correction, linking each correction to the profile items it touches.
* Renderers annotate matched profile surfaces with a bold amber
  "corrected by verification — see finding <id>" marker; corrections with
  no extractable ``r-`` ids surface as one profile-level caution sentence
  (individual items may be affected even where not flagged).
* :func:`serialize_corrections` feeds the ``.profile.json`` /sidecar export.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .report_status import ReportStatus, classify_status

# Requirement ids as research mints them (``r-`` + 12 hex). Same shape the
# compliance checker's linkage filter keys on.
_REQUIREMENT_ID_RE = re.compile(r"\br-[0-9a-f]{12}\b")

# Verdict labels for the annotation text, keyed by what fired.
_VERDICT_LABELS = {
    "CORRECTED": "corrected",
    "DISPUTED": "disputed",
}


@dataclass(frozen=True)
class VerificationCorrection:
    """One finding whose verification outcome supersedes a researched claim."""

    finding_id: str
    file_name: str
    verdict_label: str  # "corrected" | "disputed" | "contested"
    item_ids: tuple[str, ...]

    @property
    def display_reference(self) -> str:
        return self.finding_id or self.file_name or "unidentified finding"


def _correction_texts(finding) -> list[str]:
    """The texts an ``r-`` id can plausibly appear in for this finding."""
    texts = [
        str(getattr(finding, "issue", "") or ""),
        str(getattr(finding, "codeReference", "") or ""),
        str(getattr(finding, "existingText", "") or ""),
    ]
    verification = getattr(finding, "verification", None)
    if verification is not None:
        texts.append(str(getattr(verification, "explanation", "") or ""))
        texts.append(str(getattr(verification, "correction", "") or ""))
    return texts


def collect_verification_corrections(findings) -> list[VerificationCorrection]:
    """Select verdict-superseded findings and link them to profile items.

    A finding qualifies when its verification verdict is CORRECTED or
    DISPUTED, or when :func:`classify_status` resolves it to
    VERIFIED_CONTESTED (grounded initial and escalation verifiers
    disagreed). Every field is read defensively so test doubles and legacy
    findings classify cleanly.
    """
    corrections: list[VerificationCorrection] = []
    for finding in findings or []:
        verification = getattr(finding, "verification", None)
        if verification is None:
            continue
        verdict = str(getattr(verification, "verdict", "") or "").upper()
        if verdict in _VERDICT_LABELS:
            label = _VERDICT_LABELS[verdict]
        elif classify_status(finding) is ReportStatus.VERIFIED_CONTESTED:
            label = "contested"
        else:
            continue
        item_ids: list[str] = []
        for text in _correction_texts(finding):
            for match in _REQUIREMENT_ID_RE.findall(text):
                if match not in item_ids:
                    item_ids.append(match)
        corrections.append(
            VerificationCorrection(
                finding_id=str(getattr(finding, "finding_id", "") or ""),
                file_name=str(getattr(finding, "fileName", "") or ""),
                verdict_label=label,
                item_ids=tuple(item_ids),
            )
        )
    return corrections


def corrections_by_item_id(
    corrections: list[VerificationCorrection],
) -> dict[str, list[VerificationCorrection]]:
    """Index corrections by the requirement-item ids they reference."""
    by_id: dict[str, list[VerificationCorrection]] = {}
    for correction in corrections:
        for item_id in correction.item_ids:
            by_id.setdefault(item_id, []).append(correction)
    return by_id


def unattributed_corrections(
    corrections: list[VerificationCorrection],
) -> list[VerificationCorrection]:
    """Corrections that name no ``r-`` id — annotatable only profile-wide."""
    return [c for c in corrections if not c.item_ids]


def correction_marker_text(correction: VerificationCorrection) -> str:
    """The inline annotation for a matched profile surface."""
    return (
        f"⚠ {correction.verdict_label} by verification — "
        f"see finding {correction.display_reference}"
    )


def serialize_corrections(
    corrections: list[VerificationCorrection],
) -> list[dict]:
    """JSON-ready form for the ``.profile.json`` export / sidecar."""
    return [
        {
            "finding_id": c.finding_id,
            "file_name": c.file_name,
            "verdict": c.verdict_label,
            "requirement_item_ids": list(c.item_ids),
        }
        for c in corrections
    ]
