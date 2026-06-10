"""Labeled spec set for the live-capture eval (:mod:`evals.live_capture`).

Unlike :mod:`evals.fixtures` — which pairs a spec with a *canned* model
payload to regression-test the parser — each :class:`LabeledSpec` here
carries only the spec text plus a hand-authored description of the defects
a correct review *should* surface. The live-capture harness runs the
**real** review + verification prompts over these specs and scores the
model's findings against these labels. That is the signal neither hermetic
harness can produce, because both replay captured output rather than
calling the model.

Keep each spec tiny and purposeful. The original five cases exercise the
prompt improvements that motivated this harness —

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

— and the growth set broadens coverage to one spec per defect *class* the
review is expected to catch (placeholder, template marker, duplicate
paragraph, invalid code cycle, stale CPC cycle, stale ASCE 7, stale
NFPA 72, a flatly-wrong California seismic exemption, plus a second clean
spec in Division 22 so false-positive measurement isn't hostage to one
clean sample). Per-spec comments below state each case's purpose and the
expected verification path.

⚠️ The labels are the eval's oracle: severities are soft-scored, but
``expected_verdict`` / ``expected_status`` seed calibration fixtures.
Treat a label change like a code change — wrong ground truth is worse
than no ground truth.

The default matching here is coarse (case-insensitive substring) so the
hermetic path stays free and deterministic. Under ``--live`` capture the
substring check is superseded by the LLM-as-judge matcher in
:mod:`evals.judge` — phrasing-robust matching plus classification of extra
findings — with this substring matcher retained as the per-spec fallback
whenever the judge is unusable. Severity is scored softly (reported,
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

_PLACEHOLDER_BODY = (
    "SECTION 23 74 13 - PACKAGED ROOFTOP AIR CONDITIONING UNITS\n"
    "PART 2 PRODUCTS\n"
    "2.02 CAPACITY\n"
    "A. Cooling capacity: [SELECT] tons at AHRI rating conditions.\n"
    "B. Electrical: 460V/3-phase; MCA and MOCP as scheduled.\n"
)

_TEMPLATE_MARKER_BODY = (
    "SECTION 23 09 93 - SEQUENCE OF OPERATIONS\n"
    "PART 3 EXECUTION\n"
    "3.02 ECONOMIZER\n"
    "A. TODO: coordinate the economizer changeover setpoint with the electrical engineer.\n"
    "B. Economizer shall lock out on outside air above the high-limit setpoint.\n"
)

# The duplicated paragraph must genuinely trip the production
# ``duplicate_paragraph`` detector (``detect_duplicate_paragraphs``): the
# detector splits on BLANK lines and keys on the whole stripped paragraph,
# so the two copies are blank-line-separated and byte-identical — same
# "A." prefix, the classic renumber-after-copy-paste artifact. The shape
# is locked by ``tests/test_labeled_specs.py`` executing the real
# detector. In live capture the model gets no pre-detected alerts, so the
# case measures the review catching the duplicate unaided.
_DUPLICATE_PARAGRAPH_BODY = (
    "SECTION 22 07 19 - PLUMBING PIPING INSULATION\n"
    "PART 3 EXECUTION\n"
    "3.01 INSTALLATION\n"
    "\n"
    "A. Install insulation continuously through wall and floor penetrations, "
    "with the vapor barrier unbroken and all joints sealed per the manufacturer's instructions.\n"
    "\n"
    "A. Install insulation continuously through wall and floor penetrations, "
    "with the vapor barrier unbroken and all joints sealed per the manufacturer's instructions.\n"
)

_INVALID_CYCLE_BODY = (
    "SECTION 23 05 00 - COMMON WORK RESULTS FOR HVAC\n"
    "PART 1 GENERAL\n"
    "1.03 REFERENCES\n"
    "A. All work shall comply with the 2018 California Building Code.\n"
)

_STALE_CPC_BODY = (
    "SECTION 22 11 16 - DOMESTIC WATER PIPING\n"
    "PART 1 GENERAL\n"
    "1.02 REFERENCES\n"
    "A. Install domestic water piping per the 2016 California Plumbing Code.\n"
)

_STALE_ASCE7_BODY = (
    "SECTION 23 05 48 - VIBRATION AND SEISMIC CONTROLS FOR HVAC\n"
    "PART 1 GENERAL\n"
    "1.04 SEISMIC DESIGN\n"
    "A. Seismic design of equipment anchorage shall be per ASCE 7-10 Chapter 13.\n"
)

_STALE_NFPA72_BODY = (
    "SECTION 23 33 00 - AIR DUCT ACCESSORIES\n"
    "PART 2 PRODUCTS\n"
    "2.05 DUCT SMOKE DETECTORS\n"
    "A. Duct smoke detectors shall be installed and tested per NFPA 72, 2016 edition.\n"
)

_SEISMIC_EXEMPTION_BODY = (
    "SECTION 22 05 48 - VIBRATION AND SEISMIC CONTROLS FOR PLUMBING\n"
    "PART 1 GENERAL\n"
    "1.05 SEISMIC RESTRAINT\n"
    "A. Seismic restraint of piping systems is not required for this project.\n"
)

_CLEAN_PLUMBING_BODY = (
    "SECTION 22 13 16 - SANITARY WASTE AND VENT PIPING\n"
    "PART 1 GENERAL\n"
    "1.01 SUMMARY\n"
    "A. Comply with the California Plumbing Code as adopted for this project.\n"
    "PART 2 PRODUCTS\n"
    "2.01 PIPE\n"
    "A. Provide cast iron soil pipe with no-hub couplings as scheduled.\n"
    "PART 3 EXECUTION\n"
    "3.01 INSTALLATION\n"
    "A. Slope horizontal drainage piping per code and test before concealment.\n"
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
    # --- Growth set: one spec per defect class -----------------------------
    # Unresolved template selection — a deterministic ``placeholder`` class
    # defect; spec-text-only, should resolve locally without web search.
    LabeledSpec(
        spec_id="placeholder_selection",
        filename="23 74 13 - RTU (placeholder).docx",
        category="internal_coordination",
        spec_text=_PLACEHOLDER_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Unresolved [SELECT] placeholder left in the cooling capacity",
                expected_severity="MEDIUM",
                must_match=("[select]",),
                expected_verdict="UNVERIFIED",
                expected_status="LOCALLY_CLASSIFIED",
            ),
        ),
    ),
    # Leftover authoring note — the ``template_marker`` class (TODO/FIXME).
    LabeledSpec(
        spec_id="template_todo_marker",
        filename="23 09 93 - Sequences (TODO marker).docx",
        category="internal_coordination",
        spec_text=_TEMPLATE_MARKER_BODY,
        expected_defects=(
            ExpectedDefect(
                label="TODO authoring note left in the economizer sequence",
                expected_severity="GRIPES",
                must_match=("todo",),
                expected_verdict="UNVERIFIED",
                expected_status="LOCALLY_CLASSIFIED",
            ),
        ),
    ),
    # Verbatim duplicated paragraph — measures the model catching it with no
    # pre-detected alerts attached (live capture sends the bare spec body).
    LabeledSpec(
        spec_id="duplicate_paragraph",
        filename="22 07 19 - Insulation (duplicate paragraph).docx",
        category="internal_coordination",
        spec_text=_DUPLICATE_PARAGRAPH_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Installation paragraph duplicated verbatim (3.01.A and 3.01.B)",
                expected_severity="GRIPES",
                must_match=("duplicat",),
                expected_verdict="UNVERIFIED",
                expected_status="LOCALLY_CLASSIFIED",
            ),
        ),
    ),
    # Fabricated code year — CBC editions are triennial (…2016, 2019, 2022,
    # 2025); "2018 CBC" never existed. Distinct from the *stale* 2019 cite.
    LabeledSpec(
        spec_id="invalid_2018_cbc",
        filename="23 05 00 - Common HVAC (invalid 2018 CBC).docx",
        category="california_ahj",
        spec_text=_INVALID_CYCLE_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Cites a 2018 California Building Code, an edition that does not exist",
                expected_severity="MEDIUM",
                must_match=("2018",),
                expected_verdict="CORRECTED",
                expected_status="VERIFIED_CONTRADICTED",
            ),
        ),
    ),
    # Stale CPC cycle — the Division 22 twin of ``stale_cbc``.
    LabeledSpec(
        spec_id="stale_cpc",
        filename="22 11 16 - Domestic Water (stale CPC).docx",
        category="california_ahj",
        spec_text=_STALE_CPC_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Cites the 2016 California Plumbing Code for a 2025-cycle project",
                expected_severity="MEDIUM",
                must_match=("2016",),
                expected_verdict="CORRECTED",
                expected_status="VERIFIED_CONTRADICTED",
            ),
        ),
    ),
    # Stale ASCE 7 edition — the ``stale_asce7`` deterministic class; the
    # 2025 cycle adopts ASCE 7-22 (confirm against the published code).
    LabeledSpec(
        spec_id="stale_asce7",
        filename="23 05 48 - Seismic Controls (stale ASCE 7).docx",
        category="code_standard",
        spec_text=_STALE_ASCE7_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Cites ASCE 7-10 for seismic design; the current cycle adopts a newer edition",
                expected_severity="MEDIUM",
                must_match=("asce 7",),
                expected_verdict="CORRECTED",
                expected_status="VERIFIED_CONTRADICTED",
            ),
        ),
    ),
    # Stale pinned NFPA standard beyond the legacy hardcoded subset — the
    # cycle pins NFPA 72 at 2025 (CA-amended); 2016 is two-plus cycles old.
    LabeledSpec(
        spec_id="stale_nfpa72",
        filename="23 33 00 - Duct Accessories (stale NFPA 72).docx",
        category="code_standard",
        spec_text=_STALE_NFPA72_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Duct smoke detectors cite NFPA 72 2016 edition; cycle pins NFPA 72 2025",
                expected_severity="MEDIUM",
                must_match=("nfpa 72",),
                expected_verdict="CORRECTED",
                expected_status="VERIFIED_CONTRADICTED",
            ),
        ),
    ),
    # Flatly wrong California exemption: a DSA K-12 project cannot waive
    # seismic restraint of piping. The finding's claim (restraint IS
    # required) is web-supportable, so this exercises the CRITICAL
    # california_ahj deep-reasoning verification path end to end.
    LabeledSpec(
        spec_id="seismic_exemption",
        filename="22 05 48 - Seismic Controls (false exemption).docx",
        category="california_ahj",
        spec_text=_SEISMIC_EXEMPTION_BODY,
        expected_defects=(
            ExpectedDefect(
                label="Declares seismic restraint of piping not required — wrong for a California DSA K-12 project",
                expected_severity="CRITICAL",
                must_match=("seismic",),
                expected_verdict="CONFIRMED",
                expected_status="VERIFIED_SUPPORTED",
            ),
        ),
    ),
    # Second clean spec, Division 22 — false-positive measurement should not
    # be hostage to a single clean sample in one division.
    LabeledSpec(
        spec_id="clean_sanitary",
        filename="22 13 16 - Sanitary Waste (clean).docx",
        spec_text=_CLEAN_PLUMBING_BODY,
        is_clean=True,
        category="california_ahj",
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
    # Which matcher decided this spec's defect matches: "substring" (the
    # hermetic default / judge fallback) or "judge" (evals.judge). Recorded
    # so a capture report never presents mixed-method recall as one number
    # without saying so.
    match_method: str = "substring"
    # Judge classification of extra findings (matched to no defect). Filled
    # only on judged --live captures; reporting telemetry, never a gate.
    extra_finding_count: int = 0
    fp_legitimate: int = 0
    fp_duplicate: int = 0
    fp_hallucination: int = 0


# Matcher protocol: ``(defect, findings) -> matched finding | None``.
# ``defect_matched`` is the hermetic default; ``evals.judge`` adapts the
# LLM-as-judge decisions to the same shape via ``matcher_from_matches``.
Matcher = Any


def score_spec_review(
    spec: LabeledSpec,
    findings: list[Any],
    *,
    matcher: Matcher = defect_matched,
) -> SpecReviewScore:
    """Score one spec's live findings against its labels.

    Recall is matched / expected defects. On a clean spec every emitted
    finding is a false positive. Severity match is counted only for defects
    that were found, and is reported (not gated) so the CRITICAL/HIGH/MEDIUM
    boundary can be observed rather than enforced. ``matcher`` selects the
    match decision per defect — the substring default keeps this function
    hermetic; the live capture passes the judge-backed matcher.
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
        hit = matcher(defect, findings)
        if hit is None:
            continue
        score.matched_defect_count += 1
        hit_sev = str(getattr(hit, "severity", "") or "").strip().upper()
        if hit_sev == defect.expected_severity.strip().upper():
            score.severity_match_count += 1
    return score
