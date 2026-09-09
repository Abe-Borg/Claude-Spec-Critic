"""Data-center edition-applicability scenarios.

The set the gated researched-context expansion is judged against; see
CLAUDE.md "Open items" for why the evaluation has not been run.

**This module is a specification, not a measurement.** It defines the cases
that the step-2 edition-authority change must be judged against, and the
criteria for judging them, *before* any model output is reviewed — so the bar
cannot be moved to fit whatever a later run happens to produce.

Why it exists: the twelve adjudicated live captures under
``calibration/fixtures_live/`` cannot evaluate step 2 at all, because
``evals/live_capture.py`` pins ``CALIFORNIA_2025``. Nothing in that set says
anything about data-center module behaviour.

What step 2 is: the ``datacenter_*`` modules pin a **national model-code**
basis (``dc-ibc-2024``: IBC/IFC 2024, ASCE 7-22, NFPA 13-2022 and NFPA 72-2022,
every standard marked ``UNVERIFIED``). Three surfaces assert an edition from
those pins and they do not agree with each other:

* the deterministic stale-cycle detector compares citations against
  ``cycle.primary_code_year`` (2024) with no adoption facts at all;
* the review prompt names the pins, but its category #2 also instructs
  deference to project adoption where the project context names it;
* the verifier prompt has neither the adoption facts nor the deference rule,
  and instructs the model to "treat the pinned edition as authoritative".

The failure that matters is the **inversion**: the stage holding the facts is
overruled by the stage without them, so a *correct* adoption-deferring finding
can be discarded as ``DISPUTED`` rather than a wrong one being confirmed. A
false positive gets human review; a silently discarded true positive does not.
:data:`SCENARIOS` covers that case explicitly.

Sourcing rule (learned the hard way in the oracle ledger): every adoption fact
here is grounded **outside this repository**. Citing ``code_cycles.py`` to
justify a scenario that judges those pins would be circular — a wrong pin would
leave behaviour and expectation agreeing.

Nothing here runs a model. A billed comparison needs its own authorization with
a dataset, cost ceiling and stopping rule agreed in advance; see
:data:`EVALUATION_PROTOCOL` for the settings such a run must record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------
# Coverage contract
# --------------------------------------------------------------------------

#: The dimensions this set must cover. A scenario set
#: missing any of these cannot evaluate step 2, so the coverage test fails.
REQUIRED_DIMENSIONS: frozenset[str] = frozenset(
    {
        "differing_local_adoption",
        "generic_pins_no_research",
        "partial_research",
        "conflicting_adoption_claims",
        "contractual_vs_legal",
        "inversion_correct_finding_discarded",
    }
)

_RESEARCH_STATES = frozenset({"available", "partial", "unavailable", "conflicting"})

_FINDING_EXPECTATIONS = frozenset({"none", "optional", "required"})

#: Marker used in verdict/status when no finding is expected.
NOT_APPLICABLE = "N/A"


# --------------------------------------------------------------------------
# Externally sourced adoption facts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AdoptionFact:
    """What a jurisdiction actually adopts, with the evidence for it.

    ``sources`` must cite authorities outside this repository — a state code
    agency, the model code itself, or the standards body. A scenario whose
    adoption fact cited this repo's own pins would be judging those pins
    against themselves.
    """

    jurisdiction: str
    code_name: str
    model_code_base: str
    referenced_nfpa13: str
    referenced_asce7: str
    effective: str
    sources: tuple[str, ...]
    note: str = ""


_ICODE_SOURCES = (
    "2021 IBC Chapter 35 updated its NFPA 13 reference to the 2019 edition — "
    "https://nfsa.org/2020/10/20/icc-and-nfpa-updates-that-impact-fire-sprinkler-installations/",
    "The 2024 IBC references ASCE 7-22, replacing the 2021 IBC's ASCE 7-16 — "
    "https://www.structuremag.org/article/2024-ibc-significant-structural-changes-part-6-loads/",
)

VIRGINIA = AdoptionFact(
    jurisdiction="Loudoun County, Virginia",
    code_name="2021 Virginia Uniform Statewide Building Code (Virginia Construction Code)",
    model_code_base="2021 IBC",
    referenced_nfpa13="NFPA 13-2019",
    referenced_asce7="ASCE 7-16",
    effective="Effective 18 January 2024; mandatory for new permit applications from 17 January 2025.",
    sources=(
        "Virginia Department of Housing and Community Development — https://www.dhcd.virginia.gov/codes",
        "2021 Virginia Construction Code adopts IBC 2021 Chapters 2-35 by reference — "
        "https://www.dhcd.virginia.gov/sites/default/files/DocX/building-codes-regulations/"
        "archive-codes/2021/2021-virginia-construction-code.pdf",
    )
    + _ICODE_SOURCES,
    note=(
        "The largest hyperscale market in the world sits two I-code cycles behind the "
        "dc-ibc-2024 module basis."
    ),
)

OHIO = AdoptionFact(
    jurisdiction="New Albany, Ohio (Columbus metro)",
    code_name="2024 Ohio Building Code",
    model_code_base="2021 IBC",
    referenced_nfpa13="NFPA 13-2019",
    referenced_asce7="ASCE 7-16",
    effective="Effective 1 March 2024.",
    sources=(
        "Ohio Board of Building Standards, 2024 OBC executive summary — "
        "https://dam.assets.ohio.gov/image/upload/com.ohio.gov/documents/"
        "2024%20OBC%20Executive%20Summary%201.pdf",
        "2024 Ohio Building Code is based on the 2021 IBC — "
        "https://up.codes/viewer/ohio/ibc-2021",
    )
    + _ICODE_SOURCES,
    note=(
        "The code is NAMED 2024 but is based on the 2021 IBC. Year-matching on the code's "
        "name agrees with the module's 2024 basis while the referenced standards do not — "
        "the trap this scenario set exists to catch. Ohio also substantially amends the "
        "model codes, so the model IBC must not be applied directly."
    ),
)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DCScenario:
    """One judged case for the step-2 edition-authority change."""

    scenario_id: str
    dimension: str
    summary: str
    project: dict[str, str]
    module_id: str
    research_state: str
    spec_excerpt: str
    adoption: AdoptionFact | None
    #: Whether a finding is expected at all. Verification only runs on
    #: findings, so this governs whether the verdict/status fields mean
    #: anything: ``none`` requires them to be N/A (a scorer that checked a
    #: verdict here would fail a correct silent review), ``required`` requires
    #: real values, and ``optional`` requires the criteria to say what happens
    #: in both branches.
    finding_expectation: str
    #: What a correct review pass should produce. Empty when
    #: ``finding_expectation`` is ``none``.
    expected_review_finding: str
    #: What a correct verification pass should conclude about that finding.
    #: ``"N/A"`` when no finding is expected.
    expected_verdict: str
    expected_status: str
    #: What the deterministic stale-cycle detector emits for ``spec_excerpt``
    #: TODAY, observed by running it rather than asserted by reading it.
    #: ``tests/test_dc_applicability.py`` re-runs the real detector and fails
    #: when this drifts — so a criterion about the pre-screen can never be
    #: vacuous, and step 2 changing the detector shows up here.
    observed_detector_alerts: tuple[str, ...]
    #: What the pipeline does today, and why it is wrong. This is the thing
    #: step 2 has to change; it is recorded now so the bar cannot drift.
    failure_mode_today: str
    judging_criteria: tuple[str, ...]
    #: What a real ``preprocess_spec`` run SURFACES for this excerpt, which is
    #: not the same thing. Step 2 suppresses stale-cycle detection for a
    #: location-aware module (CLAUDE.md, "Edition authority"), so the raw
    #: detector above can
    #: fire while the pipeline emits nothing. Recording only the raw output
    #: would let a scenario describe an alert no run ever shows.
    observed_pipeline_alerts: tuple[str, ...] = ()
    module_pins: dict[str, str] = field(
        default_factory=lambda: {
            "base_codes": "IBC 2024 / IFC 2024",
            "asce7": "ASCE 7-22",
            "nfpa13": "NFPA 13-2022",
            "provenance": "UNVERIFIED (every standard in the dc-ibc-2024 cycle)",
        }
    )


_VA_PROJECT = {
    "city": "Ashburn",
    "state_or_province": "VA",
    "country": "US",
    "client_name": "Example Hyperscale Operator",
}
_OH_PROJECT = {
    "city": "New Albany",
    "state_or_province": "OH",
    "country": "US",
    "client_name": "Example Hyperscale Operator",
}


SCENARIOS: tuple[DCScenario, ...] = (
    DCScenario(
        scenario_id="dc_va_correct_older_edition",
        dimension="differing_local_adoption",
        summary=(
            "A Loudoun County spec correctly cites its governing 2021 I-code base and "
            "NFPA 13-2019, the edition that base references. The module pins the 2024 "
            "IBC and NFPA 13-2022."
        ),
        project=_VA_PROJECT,
        module_id="datacenter_fire",
        research_state="available",
        spec_excerpt=(
            "A. Comply with the 2021 Virginia Construction Code, based on the 2021 "
            "International Building Code.\n"
            "B. Automatic sprinkler systems shall be designed and installed in "
            "accordance with NFPA 13-2019."
        ),
        adoption=VIRGINIA,
        finding_expectation="none",
        expected_review_finding="",
        expected_verdict=NOT_APPLICABLE + " — no finding should be raised",
        expected_status=NOT_APPLICABLE,
        observed_detector_alerts=("stale_code_cycle",),
        failure_mode_today=(
            "Both citations are correct for this jurisdiction, and the deterministic "
            "stale-cycle detector flags the 2021 I-code citation anyway: it compares "
            "against cycle.primary_code_year (2024) with no adoption facts at all. "
            "Observed by running the real detector, not asserted. The NFPA citation is "
            "not flagged for an unrelated reason — the DC detector vocabulary scans only "
            "IBC/IFC/IEBC/IFGC plus ASCE, never NFPA editions — which is why this "
            "scenario carries an I-code citation: a criterion about the pre-screen that "
            "no input can trigger would let step 2 pass without touching it."
        ),
        judging_criteria=(
            "PASS when no edition finding is raised against either citation.",
            "PASS when no deterministic stale-cycle alert reaches the review request "
            "for the 2021 I-code citation. Step 2 suppresses the detector for this "
            "module, so observed_pipeline_alerts is empty while the raw detector in "
            "observed_detector_alerts still fires — the raw record is what keeps this "
            "criterion from going vacuous if the vocabulary ever stops matching.",
            "FAIL if any surface proposes 2024 IBC or NFPA 13-2022 as the governing edition.",
            "A REPORT_ONLY note observing that the module pin differs, while naming the "
            "Virginia adoption, is acceptable but not required.",
        ),
    ),
    DCScenario(
        scenario_id="dc_va_asce7_prompt_example_collision",
        dimension="differing_local_adoption",
        summary=(
            "A Loudoun County spec correctly cites ASCE 7-16 — the exact pairing the "
            "review prompt's own category #2 gives as its example of a superseded "
            "edition ('ASCE 7-16 instead of 7-22')."
        ),
        project=_VA_PROJECT,
        module_id="datacenter_fire",
        research_state="available",
        spec_excerpt=(
            "A. Seismic bracing of fire protection piping shall be designed per "
            "ASCE 7-16 Chapter 13."
        ),
        adoption=VIRGINIA,
        finding_expectation="none",
        expected_review_finding="",
        expected_verdict=NOT_APPLICABLE + " — no finding should be raised",
        expected_status=NOT_APPLICABLE,
        observed_detector_alerts=("stale_asce7",),
        failure_mode_today=(
            "Three surfaces push the wrong way at once. The detector emits stale_asce7 "
            "today (observed). The review prompt names this precise citation as its "
            "example of staleness while the same sentence instructs deference to project "
            "adoption, so its concrete example and its rule point opposite ways on one "
            "string. The verifier has neither the adoption facts nor the deference rule."
        ),
        judging_criteria=(
            "PASS when no finding proposes ASCE 7-22 for a 2021-IBC jurisdiction.",
            "PASS when the stale_asce7 alert no longer fires for this jurisdiction.",
            "FAIL if a finding cites the prompt's own example as its justification.",
            "Record whether the prompt example or the deference clause dominated — that "
            "answer decides whether step 2 must change the example as well as the pins.",
        ),
    ),
    DCScenario(
        scenario_id="dc_oh_code_named_2024_based_2021",
        dimension="conflicting_adoption_claims",
        summary=(
            "The 2024 Ohio Building Code is based on the 2021 IBC. The code's NAME "
            "matches the module's 2024 basis; its referenced standards do not."
        ),
        project=_OH_PROJECT,
        module_id="datacenter_fire",
        research_state="available",
        spec_excerpt=(
            "A. Comply with the 2024 Ohio Building Code.\n"
            "B. Sprinkler systems shall be installed per NFPA 13-2019."
        ),
        adoption=OHIO,
        finding_expectation="none",
        expected_review_finding="",
        expected_verdict=NOT_APPLICABLE + " — no finding should be raised",
        expected_status=NOT_APPLICABLE,
        observed_detector_alerts=(),
        failure_mode_today=(
            "This is a reasoning failure, not a detector failure — the detector is "
            "silent here (observed, and correctly so: 'Ohio Building Code' is not an "
            "I-code abbreviation it scans). The risk is that year-matching on '2024 Ohio "
            "Building Code' agrees with the module's 2024 basis and wrongly licenses the "
            "NFPA 13-2022 pin. The correct chain runs through the model-code base (2021 "
            "IBC), not the state code's name."
        ),
        judging_criteria=(
            "PASS when the NFPA 13-2019 citation is left alone.",
            "FAIL if the reasoning treats '2024 OBC' as equivalent to '2024 IBC'.",
            "FAIL if Ohio's substantial amendments are ignored in favour of applying the "
            "model IBC directly.",
            "The pre-screen is not the surface under test here; do not read a silent "
            "detector as a pass.",
        ),
    ),
    DCScenario(
        scenario_id="dc_inversion_correct_finding_discarded",
        dimension="inversion_correct_finding_discarded",
        summary=(
            "The edition-authority inversion. A spec cites NFPA 13-2022; review "
            "correctly defers to Virginia's 2021-IBC adoption and flags the mismatch. "
            "Verification, which has neither the adoption facts nor the deference rule, "
            "can ground a DISPUTED against that correct finding and discard it."
        ),
        project=_VA_PROJECT,
        module_id="datacenter_fire",
        research_state="available",
        spec_excerpt=(
            "A. Automatic sprinkler systems shall be designed and installed in "
            "accordance with NFPA 13-2022."
        ),
        adoption=VIRGINIA,
        finding_expectation="required",
        expected_review_finding=(
            "The spec cites NFPA 13-2022, but the 2021 Virginia USBC references "
            "NFPA 13-2019. Confirm the edition with the AHJ before issue."
        ),
        expected_verdict="CONFIRMED",
        expected_status="VERIFIED_SUPPORTED",
        observed_detector_alerts=(),
        failure_mode_today=(
            "Verification receives the finding, a user_location dict and a jurisdiction "
            "fingerprint — never the researched adoption facts (src/verification/ "
            "contains no reference to project_context) — plus an instruction that the "
            "pinned edition is authoritative. It can ground DISPUTED on the module pin "
            "and discard a correct finding. DISPUTED is the verdict that tells a reviewer "
            "to throw the finding away, so this failure is silent. The detector is not "
            "involved (observed: no alerts)."
        ),
        judging_criteria=(
            "PASS only when the correct finding SURVIVES verification.",
            "FAIL on DISPUTED — the primary failure this scenario exists to detect.",
            "FAIL on CORRECTED that rewrites the finding back toward the module pin.",
            "An UNVERIFIED outcome is a partial pass: the finding survives, but the "
            "verifier could not reach the adoption facts it needed.",
            "Measure this scenario separately from the false-positive scenarios. A change "
            "that fixes false positives by making verification more willing to DISPUTE "
            "would make this one worse.",
        ),
    ),
    DCScenario(
        scenario_id="dc_no_research_generic_pins",
        dimension="generic_pins_no_research",
        summary=(
            "A profile-less data-center run: no location, so no adoption research. The "
            "module's UNVERIFIED national pins are the only edition information available."
        ),
        project={},
        module_id="datacenter_fire",
        research_state="unavailable",
        spec_excerpt=(
            "A. Comply with the 2021 International Building Code.\n"
            "B. Automatic sprinkler systems shall be designed and installed in "
            "accordance with NFPA 13-2019."
        ),
        adoption=None,
        finding_expectation="optional",
        expected_review_finding=(
            "Optional. If raised it must be explicitly provisional: the module's "
            "reference assumption is the 2024 IBC with NFPA 13-2022 (UNVERIFIED), and "
            "the governing edition for this project has not been established."
        ),
        expected_verdict="UNVERIFIED",
        expected_status="INSUFFICIENT_EVIDENCE",
        observed_detector_alerts=("stale_code_cycle",),
        failure_mode_today=(
            "With no jurisdiction known, a national model-code year is not a defensible "
            "comparison target, yet the stale-cycle detector fires against 2024 anyway "
            "(observed) and the verifier is still told the pin is authoritative. Plan "
            "section 5.2.1 makes suppression the default here."
        ),
        judging_criteria=(
            "PASS when no confident edition edit is proposed.",
            "PASS when the deterministic stale-cycle alert no longer fires (it fires today).",
            "A run with no finding at all is a PASS — silence is correct when nothing can "
            "be established.",
            "If a finding IS raised it must be provisional and must verify to UNVERIFIED / "
            "INSUFFICIENT_EVIDENCE; a confident EDIT is a FAIL.",
            "FAIL on any output presenting the UNVERIFIED pin as established.",
        ),
    ),
    DCScenario(
        scenario_id="dc_partial_research_missing_dimension",
        dimension="partial_research",
        summary=(
            "Research succeeds on some dimensions and fails on the one that would have "
            "established the sprinkler-standard adoption. The citations in the spec are "
            "correct; the question is whether the gap stays visible."
        ),
        project=_VA_PROJECT,
        module_id="datacenter_fire",
        research_state="partial",
        spec_excerpt=(
            "A. Comply with the 2021 Virginia Construction Code, based on the 2021 "
            "International Building Code.\n"
            "B. Automatic sprinkler systems shall be designed and installed in "
            "accordance with NFPA 13-2019."
        ),
        adoption=None,
        finding_expectation="none",
        expected_review_finding="",
        expected_verdict=NOT_APPLICABLE + " — no finding should be raised",
        expected_status=NOT_APPLICABLE,
        observed_detector_alerts=("stale_code_cycle",),
        failure_mode_today=(
            "A partial profile must not read as a complete one. The failed dimension is "
            "exactly the one that would have settled the sprinkler-edition question, so "
            "the correct behaviour is to preserve uncertainty rather than fall back to "
            "the pin. The detector also fires on the 2021 I-code citation today "
            "(observed). This scenario is judged on report surfaces rather than on a "
            "verdict: with no finding raised, verification never runs."
        ),
        judging_criteria=(
            "PASS when the failed dimension is visible in the report.",
            "PASS when no edition finding is raised against the correct citations.",
            "FAIL if the module pin silently fills the gap the failed dimension left.",
            "FAIL if the report presents the research as complete.",
            "Judge this on the Run Diagnostics research row and the requirements section, "
            "not on a verification verdict.",
        ),
    ),
    DCScenario(
        scenario_id="dc_owner_standard_stricter_than_code",
        dimension="contractual_vs_legal",
        summary=(
            "An owner standard requires FM Global data-sheet compliance beyond what the "
            "adopted code requires. Contractual obligation and legal adoption are "
            "different authorities and must not be merged."
        ),
        project=_VA_PROJECT,
        module_id="datacenter_fire",
        research_state="available",
        spec_excerpt=(
            "A. Sprinkler protection for data halls shall comply with NFPA 13-2019 and "
            "with FM Global Data Sheet 5-32 as required by the Owner's design standard."
        ),
        adoption=VIRGINIA,
        finding_expectation="none",
        expected_review_finding="",
        expected_verdict=NOT_APPLICABLE + " — no finding should be raised",
        expected_status=NOT_APPLICABLE,
        observed_detector_alerts=(),
        failure_mode_today=(
            "An owner requirement can be stricter than code without the spec being "
            "non-compliant, and a code citation cannot discharge a contractual one. "
            "Collapsing the two in either direction is an error: treating the FM Global "
            "requirement as adopted law to verify, or treating the code citation as "
            "satisfied because the owner standard is stricter. The detector is silent "
            "here (observed); this is a compliance-pass and verification question."
        ),
        judging_criteria=(
            "PASS when both requirements are left standing as separate authorities.",
            "FAIL if the FM Global requirement is verified as though it were adopted law.",
            "FAIL if either requirement is proposed for deletion as redundant.",
            "The compliance pass must not record the owner requirement as a code miss.",
        ),
    ),
)


# --------------------------------------------------------------------------
# Protocol for a future billed comparison
# --------------------------------------------------------------------------

#: Settings a billed comparison must record so two runs can be compared at all.
#: Recorded now, before any outcome is seen.
EVALUATION_PROTOCOL: dict[str, str] = {
    "status": (
        "NOT RUN. This module defines what to measure. No model call has been made "
        "against these scenarios, and no claim about model behaviour is made anywhere "
        "in this file."
    ),
    "authorization": (
        "A billed comparison requires its own authorization with a dataset, a maximum "
        "cost and a stopping rule agreed in advance. The presence of an API key is not "
        "authorization to spend."
    ),
    "record_per_run": (
        "review model and effort; verifier initial model and effort; escalation model; "
        "prompt/policy version (the step-2 saved policy_version); review transport; "
        "research dimensions attempted and their per-dimension status; the resolved "
        "ProjectProfile; and the judge settings if a model judge is used."
    ),
    "paired_comparison": (
        "Run the same scenarios before and after the step-2 change with everything else "
        "held fixed. Report the two failure directions separately: incorrect confirmations "
        "(a wrong edition edit proposed) and incorrect disputes (a correct finding "
        "discarded). A change that trades one for the other has not improved anything."
    ),
    "hermetic_limit": (
        "Hermetic tests establish transport and safety behaviour — that the basis reaches "
        "every path, that cache identity isolates, that resume preserves the snapshot. "
        "They do not establish improved model reasoning and must not be reported as if "
        "they did."
    ),
}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_scenarios(scenarios: Iterable[DCScenario] = SCENARIOS) -> list[str]:
    """Return a list of problems with the scenario set; empty means valid."""
    problems: list[str] = []
    scenarios = list(scenarios)

    seen: set[str] = set()
    for s in scenarios:
        if s.scenario_id in seen:
            problems.append(f"{s.scenario_id}: duplicate scenario_id")
        seen.add(s.scenario_id)

        if s.dimension not in REQUIRED_DIMENSIONS:
            problems.append(
                f"{s.scenario_id}: dimension '{s.dimension}' is not one of "
                f"{sorted(REQUIRED_DIMENSIONS)}"
            )
        if s.research_state not in _RESEARCH_STATES:
            problems.append(
                f"{s.scenario_id}: research_state '{s.research_state}' is not one of "
                f"{sorted(_RESEARCH_STATES)}"
            )
        if not s.spec_excerpt.strip():
            problems.append(f"{s.scenario_id}: spec_excerpt must not be empty")
        if not s.failure_mode_today.strip():
            problems.append(
                f"{s.scenario_id}: failure_mode_today must state what is wrong today, "
                "otherwise there is no bar for step 2 to clear"
            )
        if not s.judging_criteria:
            problems.append(f"{s.scenario_id}: judging_criteria must not be empty")

        if s.finding_expectation not in _FINDING_EXPECTATIONS:
            problems.append(
                f"{s.scenario_id}: finding_expectation "
                f"'{s.finding_expectation}' is not one of "
                f"{sorted(_FINDING_EXPECTATIONS)}"
            )
        elif s.finding_expectation == "none":
            # Verification only runs on findings. A verdict expectation with no
            # finding to attach it to is unreachable, and a scorer honouring it
            # would fail a correct silent review.
            if s.expected_review_finding.strip():
                problems.append(
                    f"{s.scenario_id}: finding_expectation 'none' must leave "
                    "expected_review_finding empty"
                )
            for label, value in (
                ("expected_verdict", s.expected_verdict),
                ("expected_status", s.expected_status),
            ):
                if not value.startswith(NOT_APPLICABLE):
                    problems.append(
                        f"{s.scenario_id}: {label} must be '{NOT_APPLICABLE}...' when "
                        "no finding is expected — verification never runs, so this "
                        "expectation would be unreachable"
                    )
        elif s.finding_expectation == "required":
            if not s.expected_review_finding.strip():
                problems.append(
                    f"{s.scenario_id}: finding_expectation 'required' needs an "
                    "expected_review_finding"
                )
            for label, value in (
                ("expected_verdict", s.expected_verdict),
                ("expected_status", s.expected_status),
            ):
                if value.startswith(NOT_APPLICABLE):
                    problems.append(
                        f"{s.scenario_id}: {label} must be a real value when a "
                        "finding is required"
                    )
        else:  # optional
            joined = " ".join(s.judging_criteria).lower()
            if "no finding" not in joined:
                problems.append(
                    f"{s.scenario_id}: finding_expectation 'optional' must say in "
                    "its criteria what a run with no finding scores as"
                )

        # An adoption fact must be sourced outside this repository, or the
        # scenario judges the pins against themselves.
        if s.adoption is not None:
            if not s.adoption.sources:
                problems.append(f"{s.scenario_id}: adoption fact carries no sources")
            for src in s.adoption.sources:
                if "src/" in src or "code_cycles" in src:
                    problems.append(
                        f"{s.scenario_id}: adoption source '{src[:60]}' cites this "
                        "repository; adoption facts must be grounded externally"
                    )
        elif s.research_state == "available":
            problems.append(
                f"{s.scenario_id}: research_state 'available' requires an adoption fact"
            )

    covered = {s.dimension for s in scenarios}
    for missing in sorted(REQUIRED_DIMENSIONS - covered):
        problems.append(f"required dimension not covered: {missing}")

    return problems
