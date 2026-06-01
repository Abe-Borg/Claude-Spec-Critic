"""Labeled spec set for the live-capture eval (:mod:`evals.live_capture`).

Unlike :mod:`evals.fixtures` — which pairs a spec with a *canned* model
payload to regression-test the parser — each :class:`LabeledSpec` here
carries only the spec text plus a hand-authored description of the defects
a correct review *should* surface. The live-capture harness runs the
**real** review + verification prompts over these specs and scores the
model's findings against these labels. That is the signal neither hermetic
harness can produce, because both replay captured output rather than
calling the model.

Keep the set tiny and purposeful: each case exercises one of the prompt
improvements —

* ``clean_hydronic`` — a clean spec: proves the reasoning scaffold did not
  raise the false-positive rate.
* ``stale_cbc`` — a stale primary-code citation: an unambiguous,
  high-confidence defect (confidence-rubric calibration).
* ``stale_ashrae15`` — a stale *pinned-standard* edition the old review
  prompt never enumerated: proves the broadened, unified edition list
  surfaces it.
* ``duct_pressure_contradiction`` — an internal contradiction: a
  spec-text-only defect that should never burn a web search.
* ``obscure_product_rating`` — a hard-to-ground manufacturer claim: should
  land a clean UNVERIFIED, not a guessed (and then downgraded) CONFIRMED.

The matching is intentionally coarse (case-insensitive substring) so the
recall signal is robust to wording. Severity is scored softly (reported,
never pass/fail) because the CRITICAL/HIGH/MEDIUM boundary is itself one
of the things we are measuring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExpectedDefect:
    """One defect a correct review should surface for a labeled spec."""

    label: str
    # The severity band we'd expect a calibrated reviewer to assign. Scored
    # softly — reported as a match rate, never used to fail a capture.
    expected_severity: str
    # Case-insensitive substrings that jointly identify the finding. A
    # finding "matches" this defect when every entry appears somewhere in
    # its issue / existingText / section / codeReference text.
    must_match: tuple[str, ...]
    # Verification ground truth for the matched finding when it is sent to
    # the verifier. Defaults to UNVERIFIED — refine by hand after the first
    # capture (the harness seeds the fixture from the captured verdict and
    # flags it for human review).
    expected_verdict: str = "UNVERIFIED"
    expected_status: str | None = None


@dataclass(frozen=True)
class LabeledSpec:
    """A spec body plus the defects a correct review should surface."""

    spec_id: str
    filename: str
    spec_text: str
    is_clean: bool = False
    # Calibration category, mirrors the verification profile taxonomy so the
    # emitted fixtures slot into the calibration scorer's per-category view.
    category: str = "code_standard"
    expected_defects: tuple[ExpectedDefect, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Spec bodies — tiny, each just enough text to carry its labeled defect(s).
# ---------------------------------------------------------------------------

_CLEAN_BODY = (
    "SECTION 23 21 13 - HYDRONIC PIPING\n"
    "PART 1 GENERAL\n"
    "1.01 SUMMARY\n"
    "A. Comply with the California Mechanical Code and California Plumbing Code.\n"
    "PART 2 PRODUCTS\n"
    "2.01 PIPE\n"
    "A. Provide Type L copper for chilled water as scheduled.\n"
    "PART 3 EXECUTION\n"
    "3.01 INSTALLATION\n"
    "A. Install per manufacturer's written instructions.\n"
)

_STALE_CBC_BODY = (
    "SECTION 23 05 00 - COMMON WORK RESULTS FOR HVAC\n"
    "PART 1 GENERAL\n"
    "1.03 REFERENCES\n"
    "A. Comply with 2019 CBC Chapter 6 for all mechanical work.\n"
)

_STALE_ASHRAE15_BODY = (
    "SECTION 23 64 00 - PACKAGED WATER CHILLERS\n"
    "PART 1 GENERAL\n"
    "1.02 REFERENCES\n"
    "A. Refrigeration machinery rooms shall comply with ASHRAE 15-2019.\n"
)

_DUCT_CONTRADICTION_BODY = (
    "SECTION 23 31 13 - METAL DUCTWORK\n"
    "PART 2 PRODUCTS\n"
    "2.01 GENERAL\n"
    "A. Provide galvanized steel ductwork rated for 2 inches w.g.\n"
    "B. All supply ductwork shall be constructed for 4 inches w.g.\n"
)

_OBSCURE_PRODUCT_BODY = (
    "SECTION 23 09 23 - DIRECT DIGITAL CONTROLS\n"
    "PART 2 PRODUCTS\n"
    "2.04 SENSORS\n"
    "A. Duct temperature sensors: Acme Model QX-9000, accuracy +/- 0.05 degF.\n"
)


# ---------------------------------------------------------------------------
# The labeled set.
# ---------------------------------------------------------------------------

LABELED_SPECS: tuple[LabeledSpec, ...] = (
    LabeledSpec(
        spec_id="clean_hydronic",
        filename="23 21 13 - Hydronic (clean).docx",
        spec_text=_CLEAN_BODY,
        is_clean=True,
        category="california_ahj",
    ),
    LabeledSpec(
        spec_id="stale_cbc",
        filename="23 05 00 - Common HVAC (stale CBC).docx",
        category="california_ahj",
        spec_text=_STALE_CBC_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Cites 2019 CBC for a 2025-cycle project",
                expected_severity="MEDIUM",
                must_match=("2019",),
                expected_verdict="CORRECTED",
                expected_status="VERIFIED_CONTRADICTED",
            ),
        ),
    ),
    LabeledSpec(
        spec_id="stale_ashrae15",
        filename="23 64 00 - Chillers (stale ASHRAE 15).docx",
        category="code_standard",
        spec_text=_STALE_ASHRAE15_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Cites ASHRAE 15-2019; cycle pins ASHRAE 15 2022",
                expected_severity="MEDIUM",
                must_match=("ashrae 15",),
                expected_verdict="CORRECTED",
                expected_status="VERIFIED_CONTRADICTED",
            ),
        ),
    ),
    LabeledSpec(
        spec_id="duct_pressure_contradiction",
        filename="23 31 13 - Ductwork (contradiction).docx",
        category="internal_coordination",
        spec_text=_DUCT_CONTRADICTION_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Duct pressure class stated as both 2 and 4 in. w.g.",
                expected_severity="HIGH",
                must_match=("w.g.",),
                expected_verdict="UNVERIFIED",
                expected_status="LOCALLY_CLASSIFIED",
            ),
        ),
    ),
    LabeledSpec(
        spec_id="obscure_product_rating",
        filename="23 09 23 - DDC (obscure product).docx",
        category="manufacturer",
        spec_text=_OBSCURE_PRODUCT_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Unverifiable sensor accuracy claim for an obscure model",
                expected_severity="GRIPES",
                must_match=("qx-9000",),
                expected_verdict="UNVERIFIED",
                expected_status="INSUFFICIENT_EVIDENCE",
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Pure scoring helpers (no model, no network — unit-tested hermetically).
# ---------------------------------------------------------------------------


def _finding_haystack(finding: Any) -> str:
    """Lower-cased blob of the finding fields a defect label keys on."""
    parts = [
        str(getattr(finding, attr, "") or "")
        for attr in ("issue", "existingText", "section", "codeReference")
    ]
    return " ".join(parts).lower()


def defect_matched(defect: ExpectedDefect, findings: list[Any]) -> Any | None:
    """Return the first finding that satisfies every ``must_match`` token."""
    needles = [m.lower() for m in defect.must_match if m]
    if not needles:
        return None
    for finding in findings:
        haystack = _finding_haystack(finding)
        if all(needle in haystack for needle in needles):
            return finding
    return None


@dataclass
class SpecReviewScore:
    """Per-spec review outcome scored against the labels."""

    spec_id: str
    is_clean: bool
    expected_defect_count: int = 0
    matched_defect_count: int = 0
    severity_match_count: int = 0
    false_positive_count: int = 0
    finding_count: int = 0


def score_spec_review(spec: LabeledSpec, findings: list[Any]) -> SpecReviewScore:
    """Score one spec's live findings against its labels.

    Recall is matched / expected defects. On a clean spec every emitted
    finding is a false positive. Severity match is counted only for defects
    that were found, and is reported (not gated) so the CRITICAL/HIGH/MEDIUM
    boundary can be observed rather than enforced.
    """
    score = SpecReviewScore(
        spec_id=spec.spec_id,
        is_clean=spec.is_clean,
        expected_defect_count=len(spec.expected_defects),
        finding_count=len(findings),
    )
    if spec.is_clean:
        score.false_positive_count = len(findings)
        return score
    for defect in spec.expected_defects:
        hit = defect_matched(defect, findings)
        if hit is None:
            continue
        score.matched_defect_count += 1
        hit_sev = str(getattr(hit, "severity", "") or "").strip().upper()
        if hit_sev == defect.expected_severity.strip().upper():
            score.severity_match_count += 1
    return score
