"""EX-03 dataset: adjudicated findings and specs, split into tuning and held-out.

The decision record is ``plans/experiments/EX-03-model-effort-confidence.md``;
the arms, runner, scorer, and decision rules that use this set are in
:mod:`evals.model_effort`.

Two stages, one per kind of experiment:

* **Verification cases** (:class:`VerificationCase`): one finding each, with
  the verdict a correct verifier should reach about the *finding's claim*
  (CONFIRMED / CORRECTED / DISPUTED / UNVERIFIED — the verifier judges the
  finding, not the spec), the sources that establish it, and the reasoning.
  The escalation-model experiment runs these.
* **Review cases** (:class:`ReviewCase`): one small specification each, with
  the defects a correct review must find (their severity says which are
  *severe*) and the **traps** — correct text a review must not propose to
  change. The review-effort and review-wording experiments run these.

**Held-out cases are new.** A case that has already been used to tune
prompts or labels (the labeled specs and live captures the 2026-09-09
baseline retuned, and the data-center scenarios that shaped the S18 prompt
change) carries ``prior_exposure`` and must sit in the tuning split. Every
held-out case was written for this set and has never been shown to a model
from this repository. The decision rules in :mod:`evals.model_effort` score
the held-out split only; the tuning split is for developing matchers,
judges, and prompts, and for checking a harness before a paid run.

**Adjudication rules** (the same as the calibration oracle ledger's):

* The verdict is about the finding as written. ``CORRECTED`` means the finding
  was wrong in a fixable way, not merely that the spec needs an edit.
* Facts come from outside this repository: the standard or code itself, the
  body that adopts it, or a measurement authority. Justifying a case with this
  repository's own pins would judge them against themselves; validation
  rejects a source that cites the repository.
* A case whose answer rests on something no source can establish (a
  fictitious product, a private owner document) says so with
  ``evidence_basis="constructed"`` and expects UNVERIFIED. A case whose
  answer is in the finding's own quoted spec text (an internal
  contradiction, a placeholder) says ``evidence_basis="spec_text"``.
* ``acceptable_verdicts`` lists the other verdicts a careful adjudicator would
  not call wrong; the scorer counts them as partial, never as a false
  CONFIRMED or a false DISPUTED.

Section numbers of NFPA standards move between editions; the sources below
name the requirement and the editions it was checked in rather than a number
that may not survive the next edition.

Nothing here calls a model.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

DATASET_VERSION = 1

SPLIT_TUNING = "tuning"
SPLIT_HELD_OUT = "held_out"
SPLITS = (SPLIT_TUNING, SPLIT_HELD_OUT)

STAGE_VERIFICATION = "verification"
STAGE_REVIEW = "review"

VERDICTS = ("CONFIRMED", "CORRECTED", "DISPUTED", "UNVERIFIED")

#: The report status each verdict earns when the verifier grounds it.
STATUS_FOR_VERDICT = {
    "CONFIRMED": "VERIFIED_SUPPORTED",
    "CORRECTED": "VERIFIED_CONTRADICTED",
    "DISPUTED": "DISPUTED",
    "UNVERIFIED": "INSUFFICIENT_EVIDENCE",
}

SEVERE_SEVERITIES = frozenset({"CRITICAL", "HIGH"})
SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "GRIPES"})

EVIDENCE_EXTERNAL = "external"
EVIDENCE_CONSTRUCTED = "constructed"
#: The finding's own quoted spec text establishes the verdict (an internal
#: contradiction, an unresolved placeholder): no outside source is needed.
EVIDENCE_SPEC_TEXT = "spec_text"
EVIDENCE_BASES = (EVIDENCE_EXTERNAL, EVIDENCE_CONSTRUCTED, EVIDENCE_SPEC_TEXT)

#: What the plan (EX-03, "Dataset and controls") says the set must include.
#: Every dimension needs at least one tuning and one held-out case.
REQUIRED_DIMENSIONS: frozenset[str] = frozenset(
    {
        "correct_citation_supported",
        "wrong_edition_adoption",
        "numeric_units",
        "exception_negation",
        "ambiguous_evidence_unverified",
        "plausible_false_claim",
        "low_severity_factual",
        "severe_omission",
        "project_override_mixed_authority",
    }
)

#: Held-out minimums below which a decision rule cannot pass (see
#: ``evals.model_effort.DECISION_RULES``); validation keeps the set above them.
MIN_HELD_OUT_VERIFICATION_CASES = 15
MIN_HELD_OUT_REVIEW_CASES = 6
MIN_HELD_OUT_SEVERE_REVIEW_DEFECTS = 8

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIVE_FIXTURES_DIR = _REPO_ROOT / "evals" / "calibration" / "fixtures_live"

#: Substrings that mark a source as this repository rather than an authority.
_REPO_SOURCE_MARKERS = ("src/", "code_cycles", "evals/", "tests/", ".py", "github.com/abe-borg")


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One authority a case rests on. ``url`` is optional: an unsure URL is
    left out rather than guessed; ``citation`` names the authority."""

    citation: str
    url: str = ""
    establishes: str = ""


@dataclass(frozen=True)
class VerificationCase:
    case_id: str
    split: str
    dimensions: tuple[str, ...]
    module_id: str
    finding: Mapping[str, Any]
    expected_verdict: str
    rationale: str
    sources: tuple[Source, ...] = ()
    acceptable_verdicts: tuple[str, ...] = ()
    evidence_basis: str = EVIDENCE_EXTERNAL
    #: ``{"city", "state_or_province", "country", "client_name"}`` or empty.
    project: Mapping[str, str] = field(default_factory=dict)
    provenance: str = "new (EX-03)"
    prior_exposure: str = ""

    stage = STAGE_VERIFICATION

    @property
    def severity(self) -> str:
        return str(self.finding.get("severity") or "").strip().upper()

    @property
    def severe(self) -> bool:
        return self.severity in SEVERE_SEVERITIES

    @property
    def expected_status(self) -> str:
        return STATUS_FOR_VERDICT[self.expected_verdict]

    @property
    def accepted(self) -> tuple[str, ...]:
        """The expected verdict plus the acceptable ones, expected first."""
        out = [self.expected_verdict]
        out.extend(v for v in self.acceptable_verdicts if v not in out)
        return tuple(out)


@dataclass(frozen=True)
class ExpectedDefect:
    """A defect a correct review must find.

    A finding matches when every string of any one group in ``match_any``
    appears (case-insensitively) in its issue, existing text, section, or
    code reference — the same haystack ``evals.labeled_specs`` uses. The
    runner's JSON records keep every finding, so an adjudication file can
    override the match (see ``evals.model_effort.score_review``).
    """

    label: str
    severity: str
    match_any: tuple[tuple[str, ...], ...]

    @property
    def severe(self) -> bool:
        return self.severity.strip().upper() in SEVERE_SEVERITIES


@dataclass(frozen=True)
class Trap:
    """Correct text a review must not propose to change.

    A finding counts against the trap when it matches ``match_any`` and its
    action is in ``actions``: a REPORT_ONLY note that asks the owner to
    confirm something is not an unsupported edit.
    """

    label: str
    match_any: tuple[tuple[str, ...], ...]
    why: str
    actions: tuple[str, ...] = ("EDIT", "ADD", "DELETE")


@dataclass(frozen=True)
class ReviewCase:
    case_id: str
    split: str
    dimensions: tuple[str, ...]
    module_id: str
    filename: str
    spec_text: str
    rationale: str
    defects: tuple[ExpectedDefect, ...] = ()
    traps: tuple[Trap, ...] = ()
    is_clean: bool = False
    #: What an operator would put in Project Context (a location-aware run's
    #: research profile, abbreviated): the adoption facts the review needs.
    project_context: str = ""
    project: Mapping[str, str] = field(default_factory=dict)
    sources: tuple[Source, ...] = ()
    provenance: str = "new (EX-03)"
    prior_exposure: str = ""

    stage = STAGE_REVIEW

    @property
    def severe_defects(self) -> tuple[ExpectedDefect, ...]:
        return tuple(d for d in self.defects if d.severe)


EvalCase = VerificationCase | ReviewCase


# --------------------------------------------------------------------------
# Sources used by the new cases
# --------------------------------------------------------------------------

_NFPA13 = "https://www.nfpa.org/codes-and-standards/nfpa-13-standard-development/13"
_NFPA14 = "https://www.nfpa.org/codes-and-standards/nfpa-14-standard-development/14"
_NFPA20 = "https://www.nfpa.org/codes-and-standards/nfpa-20-standard-development/20"
_NFPA72 = "https://www.nfpa.org/codes-and-standards/nfpa-72-standard-development/72"
_BSC = "https://www.dgs.ca.gov/bsc/codes"

SRC_NFPA13_AREA = Source(
    citation=(
        "NFPA 13, Standard for the Installation of Sprinkler Systems (2019 and 2022 "
        "editions), protection area per sprinkler for standard pendent and upright "
        "spray sprinklers"
    ),
    url=_NFPA13,
    establishes=(
        "Maximum protection area per sprinkler: light hazard 225 ft² (20.9 m², "
        "hydraulically calculated), ordinary hazard 130 ft² (12.1 m²), extra "
        "hazard 100 ft² (9.3 m², hydraulically calculated at a density of "
        "0.25 gpm/ft² or more)."
    ),
)
SRC_NFPA13_HYDRO = Source(
    citation="NFPA 13 (2019 and 2022 editions), hydrostatic tests of new systems",
    url=_NFPA13,
    establishes=(
        "Piping is hydrostatically tested at not less than 200 psi for 2 hours, or at "
        "50 psi above the maximum pressure where that exceeds 150 psi; additives such "
        "as sodium silicate or its derivatives, brine, or other chemicals shall not be "
        "used while hydrostatically testing systems or to stop leaks."
    ),
)
SRC_NFPA13_ELEC = Source(
    citation=(
        "NFPA 13 (2019 and 2022 editions), sprinkler location requirements, "
        "electrical equipment rooms"
    ),
    url=_NFPA13,
    establishes=(
        "Sprinklers are not required in an electrical equipment room when all of: the "
        "room is dedicated to electrical equipment only, only dry-type electrical "
        "equipment is used, the equipment is in a 2-hour fire-rated enclosure "
        "including protection for penetrations, and storage is not permitted in the "
        "room."
    ),
)
SRC_NFPA13_STORAGE = Source(
    citation="NFPA 13 (2019 and 2022 editions), clearance to storage, standard spray sprinklers",
    url=_NFPA13,
    establishes="The clearance between the deflector and the top of storage is 18 in. or greater.",
)
SRC_NFPA13_FDC = Source(
    citation="NFPA 13 (2019 and 2022 editions), fire department connections",
    url=_NFPA13,
    establishes=(
        "A fire department connection is provided, with narrow exceptions (buildings "
        "in remote areas inaccessible to fire department support, large-capacity "
        "deluge systems beyond fire department pumping capacity, single-story "
        "buildings not exceeding 2,000 ft²); a multi-story or large data hall "
        "building fits none of them."
    ),
)
SRC_NFPA13_PREACTION = Source(
    citation="NFPA 13 (2019 and 2022 editions), preaction systems",
    url=_NFPA13,
    establishes=(
        "Sprinkler piping and fire detection devices are automatically supervised "
        "where there are more than 20 sprinklers on the system."
    ),
)
SRC_NFPA13_SEISMIC = Source(
    citation=(
        "NFPA 13, protection of piping against damage where subject to earthquakes "
        "(its earthquake-protection chapter), and ASCE 7 Chapter 13 (nonstructural "
        "components), which requires it for sprinkler piping in Seismic Design "
        "Category D"
    ),
    url=_NFPA13,
    establishes=(
        "Sprinkler piping in a building assigned Seismic Design Category D is braced "
        "and restrained per NFPA 13's earthquake-protection requirements."
    ),
)
SRC_NFPA13_TITLE = Source(
    citation="NFPA 13, Standard for the Installation of Sprinkler Systems (the standard's title)",
    url=_NFPA13,
    establishes="NFPA 13 is titled 'Standard for the Installation of Sprinkler Systems'.",
)
SRC_NFPA14 = Source(
    citation="NFPA 14, Standard for the Installation of Standpipe and Hose Systems (2019 and 2024 editions)",
    url=_NFPA14,
    establishes=(
        "Class I standpipe systems are designed to provide 100 psi residual pressure at "
        "the outlet of the hydraulically most remote 2½ in. hose connection at the "
        "required flow (500 gpm for the first standpipe). The 65 psi figure is from "
        "editions before 1993."
    ),
)
SRC_NFPA20 = Source(
    citation="NFPA 20, Standard for the Installation of Stationary Pumps for Fire Protection (2019 and 2022 editions)",
    url=_NFPA20,
    establishes=(
        "A centrifugal fire pump furnishes not less than 150 percent of rated capacity "
        "at not less than 65 percent of rated total head, and its shutoff head does "
        "not exceed 140 percent of rated total head."
    ),
)
SRC_NFPA72_MFAB = Source(
    citation="NFPA 72, National Fire Alarm and Signaling Code (2019 and 2022 editions), manual fire alarm boxes",
    url=_NFPA72,
    establishes=(
        "The operable part of a manual fire alarm box is not less than 42 in. and not "
        "more than 48 in. above floor level; a box is within 5 ft of the entrance to "
        "each exit; additional boxes keep the travel distance to the nearest box at "
        "not more than 200 ft, measured horizontally on the same floor."
    ),
)
SRC_NFPA72_SMOKE = Source(
    citation="NFPA 72 (2019 and 2022 editions), spot-type smoke detector spacing",
    url=_NFPA72,
    establishes="On smooth ceilings, a spacing of 30 ft is permitted to be used as a guide.",
)
SRC_NFPA72_WATERFLOW = Source(
    citation=(
        "NFPA 13 (waterflow alarm devices) and NFPA 72 (initiating devices: waterflow "
        "alarm-initiating devices connected to the fire alarm system)"
    ),
    url=_NFPA72,
    establishes=(
        "Sprinkler waterflow switches are alarm-initiating devices; their connection, "
        "monitoring, and signaling are governed by NFPA 72, whatever division of the "
        "specification the switch itself is specified in."
    ),
)
SRC_NIST_811 = Source(
    citation="NIST Special Publication 811, Guide for the Use of the International System of Units (SI)",
    url="https://www.nist.gov/pml/special-publication-811",
    establishes="1 ft² = 0.09290304 m², so 130 ft² = 12.08 m² (a length of 130 ft is 39.6 m).",
)
SRC_BSC_2025 = Source(
    citation="California Building Standards Commission, 2025 California Building Standards Code (Title 24)",
    url=_BSC,
    establishes=(
        "The 2025 Title 24 took effect 1 January 2026; the 2022 code was in effect "
        "from 1 January 2023 to 31 December 2025."
    ),
)
SRC_ASTM_A135 = Source(
    citation="ASTM A135/A135M, Standard Specification for Electric-Resistance-Welded Steel Pipe (ASTM International)",
    establishes="A135's title names electric-resistance-welded pipe, not seamless pipe.",
)
SRC_ASTM_A795 = Source(
    citation=(
        "ASTM A795/A795M, Standard Specification for Black and Hot-Dipped Zinc-Coated "
        "(Galvanized) Welded and Seamless Steel Pipe for Fire Protection Use (ASTM International)"
    ),
    establishes="A795 is a steel pipe standard for fire protection use, not a copper tube standard.",
)
SRC_FM_DS = Source(
    citation="FM Global Property Loss Prevention Data Sheet 5-32, Data Centers and Related Facilities",
    url="https://www.fmglobal.com/research-and-resources/fm-global-data-sheets",
    establishes=(
        "FM Global data sheets are loss-prevention guidance invoked by an owner or "
        "insurer, not adopted law; that a building code does not adopt one says "
        "nothing about whether the owner's contract requires it."
    ),
)


# --------------------------------------------------------------------------
# New verification cases
# --------------------------------------------------------------------------


def _finding(
    *,
    severity: str,
    fileName: str,
    section: str,
    issue: str,
    actionType: str = "REPORT_ONLY",
    existingText: str | None = None,
    replacementText: str | None = None,
    codeReference: str | None = None,
    confidence: float = 0.8,
    anchorText: str | None = None,
    insertPosition: str | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "fileName": fileName,
        "section": section,
        "issue": issue,
        "actionType": actionType,
        "existingText": existingText,
        "replacementText": replacementText,
        "codeReference": codeReference,
        "confidence": confidence,
        "anchorText": anchorText,
        "insertPosition": insertPosition,
    }


_VA_PROJECT = {
    "city": "Ashburn",
    "state_or_province": "VA",
    "country": "US",
    "client_name": "Example Hyperscale Operator",
}

_F_PREACTION = "21 13 16 - Preaction Sprinkler Systems.docx"
_F_WET = "21 13 13 - Wet-Pipe Sprinkler Systems.docx"
_F_ALARM = "28 46 21 - Fire Alarm.docx"
_F_PUMP = "21 30 00 - Fire Pumps.docx"
_F_STANDPIPE = "21 12 00 - Fire-Suppression Standpipes.docx"
_F_PIPE = "21 05 23 - Fire Suppression Piping.docx"
_F_CA_SPRINKLER = "21 13 13 - Wet-Pipe Sprinkler Systems (Classroom Building).docx"


NEW_VERIFICATION_CASES: tuple[VerificationCase, ...] = (
    # ---------------------------------------------------------------- held-out
    VerificationCase(
        case_id="v-nfpa13-oh-coverage-225",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units", "correct_citation_supported"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="HIGH",
            fileName=_F_PREACTION,
            section="3.01",
            issue=(
                "The data halls are classified Ordinary Hazard Group 1, but the maximum "
                "protection area per sprinkler is set at 225 sq ft, the light hazard "
                "maximum. NFPA 13 limits standard spray sprinklers in ordinary hazard "
                "occupancies to 130 sq ft per sprinkler."
            ),
            actionType="EDIT",
            existingText="Maximum protection area per sprinkler: 225 sq ft.",
            replacementText="Maximum protection area per sprinkler: 130 sq ft.",
            codeReference="NFPA 13, protection area per sprinkler (standard spray, ordinary hazard)",
            confidence=0.9,
        ),
        expected_verdict="CONFIRMED",
        rationale=(
            "The finding is correct as written: 130 ft² is the ordinary hazard "
            "maximum for standard pendent and upright spray sprinklers, and 225 ft² "
            "is the light hazard maximum."
        ),
        sources=(SRC_NFPA13_AREA,),
    ),
    VerificationCase(
        case_id="v-nfpa13-metric-conversion-false",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units", "plausible_false_claim"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="MEDIUM",
            fileName=_F_WET,
            section="3.01",
            issue=(
                "The metric value is wrong: 130 sq ft converts to 39.6 square meters, "
                "not 12.1 square meters. Correct the SI value."
            ),
            actionType="EDIT",
            existingText="Maximum protection area per sprinkler: 130 sq ft (12.1 m2).",
            replacementText="Maximum protection area per sprinkler: 130 sq ft (39.6 m2).",
            codeReference="NFPA 13, protection area per sprinkler",
            confidence=0.8,
        ),
        expected_verdict="DISPUTED",
        rationale=(
            "The finding confuses length with area: 130 ft is 39.6 m, but 130 ft² is "
            "12.08 m², and NFPA 13 itself states the ordinary hazard limit as "
            "130 ft² (12.1 m²). The spec is right and the finding would introduce "
            "an error."
        ),
        sources=(SRC_NIST_811, SRC_NFPA13_AREA),
    ),
    VerificationCase(
        case_id="v-nfpa13-electrical-room-exception",
        split=SPLIT_HELD_OUT,
        dimensions=("exception_negation", "plausible_false_claim"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="HIGH",
            fileName=_F_PREACTION,
            section="3.05",
            issue=(
                "Sprinklers are omitted from the electrical room. NFPA 13 requires "
                "sprinkler protection in all electrical equipment rooms; the omission is "
                "noncompliant. Provide sprinklers in the electrical room."
            ),
            actionType="EDIT",
            existingText=(
                "Sprinklers are omitted from the electrical room, which is dedicated to "
                "dry-type electrical equipment, enclosed in 2-hour fire-rated construction "
                "with protected penetrations, and has no storage."
            ),
            replacementText="Provide sprinklers in the electrical room.",
            codeReference="NFPA 13",
            confidence=0.75,
        ),
        expected_verdict="DISPUTED",
        rationale=(
            "NFPA 13 permits omitting sprinklers from an electrical equipment room that "
            "meets all four conditions, and the quoted spec text states all four. The "
            "finding's claim that NFPA 13 requires sprinklers in all electrical rooms is "
            "false. (An owner or insurer may still require them; that is a different "
            "claim from the one the finding makes.)"
        ),
        sources=(SRC_NFPA13_ELEC,),
    ),
    VerificationCase(
        case_id="v-nfpa13-sodium-silicate-permitted",
        split=SPLIT_HELD_OUT,
        dimensions=("exception_negation", "correct_citation_supported"),
        module_id="california_k12_mep",
        finding=_finding(
            severity="HIGH",
            fileName=_F_CA_SPRINKLER,
            section="3.08",
            issue=(
                "The testing article allows leaks found during the hydrostatic test to be "
                "sealed with a sodium silicate additive. NFPA 13 prohibits sodium silicate "
                "and similar additives while hydrostatically testing or to stop leaks. "
                "Delete the allowance."
            ),
            actionType="DELETE",
            existingText=(
                "Leaks found during the hydrostatic test may be sealed with a sodium "
                "silicate additive introduced into the system water; retest after 24 hours."
            ),
            codeReference="NFPA 13, hydrostatic tests",
            confidence=0.9,
        ),
        expected_verdict="CONFIRMED",
        rationale="NFPA 13 states the prohibition in those terms; the finding is right.",
        sources=(SRC_NFPA13_HYDRO,),
    ),
    VerificationCase(
        case_id="v-nfpa13-sodium-silicate-negation-misread",
        split=SPLIT_HELD_OUT,
        dimensions=("exception_negation", "plausible_false_claim"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="HIGH",
            fileName=_F_WET,
            section="3.08",
            issue=(
                "The spec permits sodium silicate additives to seal leaks during the "
                "hydrostatic test, which NFPA 13 prohibits. Revise the testing article to "
                "prohibit additives."
            ),
            actionType="EDIT",
            existingText=(
                "Additives such as sodium silicate shall not be used during hydrostatic "
                "testing or to stop leaks."
            ),
            replacementText=(
                "Additives such as sodium silicate shall not be used during hydrostatic "
                "testing or to stop leaks; leaks shall be repaired and the test repeated."
            ),
            codeReference="NFPA 13, hydrostatic tests",
            confidence=0.8,
        ),
        expected_verdict="DISPUTED",
        acceptable_verdicts=("CORRECTED",),
        rationale=(
            "The finding misreads a negation: the quoted text already prohibits "
            "additives, in NFPA 13's own terms. Its premise ('the spec permits') is false "
            "and its edit adds nothing the standard requires. DISPUTED is expected; a "
            "CORRECTED that says the spec already complies is not wrong. CONFIRMED would "
            "endorse a false premise."
        ),
        sources=(SRC_NFPA13_HYDRO,),
    ),
    VerificationCase(
        case_id="v-nfpa72-pull-station-height",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units", "correct_citation_supported"),
        module_id="datacenter_electronic_safety_security",
        finding=_finding(
            severity="MEDIUM",
            fileName=_F_ALARM,
            section="3.03",
            issue=(
                "Manual fire alarm boxes are mounted with the operable part 54 inches above "
                "the finished floor. NFPA 72 requires the operable part to be not less than "
                "42 in. and not more than 48 in. above the floor."
            ),
            actionType="EDIT",
            existingText=(
                "Mount manual fire alarm boxes with the operable part 54 inches above "
                "finished floor."
            ),
            replacementText=(
                "Mount manual fire alarm boxes with the operable part not less than 42 "
                "inches and not more than 48 inches above finished floor."
            ),
            codeReference="NFPA 72, manual fire alarm boxes",
            confidence=0.9,
        ),
        expected_verdict="CONFIRMED",
        rationale="NFPA 72's 42 in. to 48 in. range is stated in the standard; 54 in. is outside it.",
        sources=(SRC_NFPA72_MFAB,),
    ),
    VerificationCase(
        case_id="v-nfpa72-travel-distance-false-250",
        split=SPLIT_HELD_OUT,
        dimensions=("plausible_false_claim", "numeric_units"),
        module_id="datacenter_electronic_safety_security",
        finding=_finding(
            severity="MEDIUM",
            fileName=_F_ALARM,
            section="3.03",
            issue=(
                "The spec limits travel distance to the nearest manual fire alarm box to "
                "200 ft. NFPA 72 permits 250 ft; revise to match the code and avoid "
                "unnecessary devices."
            ),
            actionType="EDIT",
            existingText=(
                "Provide additional manual fire alarm boxes so that the travel distance to "
                "the nearest box does not exceed 200 ft, measured horizontally on the same floor."
            ),
            replacementText=(
                "Provide additional manual fire alarm boxes so that the travel distance to "
                "the nearest box does not exceed 250 ft, measured horizontally on the same floor."
            ),
            codeReference="NFPA 72, manual fire alarm boxes",
            confidence=0.7,
        ),
        expected_verdict="DISPUTED",
        rationale="NFPA 72's limit is 200 ft; the finding's 250 ft is invented.",
        sources=(SRC_NFPA72_MFAB,),
    ),
    VerificationCase(
        case_id="v-nfpa14-class1-residual-65psi",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units", "wrong_edition_adoption"),
        module_id="california_k12_mep",
        finding=_finding(
            severity="HIGH",
            fileName=_F_STANDPIPE,
            section="2.01",
            issue=(
                "The Class I standpipe design basis of 65 psi residual pressure at the "
                "hydraulically most remote 2-1/2 inch hose connection comes from editions "
                "of NFPA 14 before 1993. The current standard requires 100 psi residual "
                "pressure at that outlet."
            ),
            actionType="EDIT",
            existingText=(
                "Design Class I standpipes for 65 psi residual pressure at the hydraulically "
                "most remote 2-1/2 inch hose connection."
            ),
            replacementText=(
                "Design Class I standpipes for 100 psi residual pressure at the "
                "hydraulically most remote 2-1/2 inch hose connection."
            ),
            codeReference="NFPA 14, minimum design pressure for Class I systems",
            confidence=0.85,
        ),
        expected_verdict="CONFIRMED",
        rationale="Every NFPA 14 edition from 1993 on uses 100 psi; 65 psi is the older value.",
        sources=(SRC_NFPA14,),
    ),
    VerificationCase(
        case_id="v-title24-2025-effective-date-false",
        split=SPLIT_HELD_OUT,
        dimensions=("wrong_edition_adoption", "plausible_false_claim"),
        module_id="california_k12_mep",
        finding=_finding(
            severity="MEDIUM",
            fileName="23 05 00 - Common Work Results for HVAC.docx",
            section="1.03",
            issue=(
                "The spec cites the 2025 California Building Code, but the 2025 California "
                "Building Standards Code does not take effect until 1 January 2027. The 2022 "
                "CBC governs this DSA project; revise the citation."
            ),
            actionType="EDIT",
            existingText="Comply with the 2025 California Building Code (CBC).",
            replacementText="Comply with the 2022 California Building Code (CBC).",
            codeReference="California Building Standards Code (Title 24)",
            confidence=0.7,
        ),
        expected_verdict="DISPUTED",
        rationale=(
            "The 2025 Title 24 took effect 1 January 2026, not 2027. The finding's date is "
            "false and its edit would cite a superseded code. The case tests adoption-date "
            "reasoning, not a pinned edition."
        ),
        sources=(SRC_BSC_2025,),
    ),
    VerificationCase(
        case_id="v-astm-a135-title-misstated",
        split=SPLIT_HELD_OUT,
        dimensions=("low_severity_factual", "correct_citation_supported"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="GRIPES",
            fileName=_F_PIPE,
            section="1.02",
            issue=(
                "The reference list gives ASTM A135 the title 'Seamless Steel Pipe'. ASTM "
                "A135 is the standard specification for electric-resistance-welded steel "
                "pipe; correct the title."
            ),
            actionType="EDIT",
            existingText="ASTM A135 - Standard Specification for Seamless Steel Pipe.",
            replacementText=(
                "ASTM A135 - Standard Specification for Electric-Resistance-Welded Steel Pipe."
            ),
            codeReference="ASTM A135",
            confidence=0.85,
        ),
        expected_verdict="CONFIRMED",
        rationale="A135's title names electric-resistance-welded pipe; the finding is right and minor.",
        sources=(SRC_ASTM_A135,),
    ),
    VerificationCase(
        case_id="v-astm-a795-false-copper",
        split=SPLIT_HELD_OUT,
        dimensions=("low_severity_factual", "plausible_false_claim"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="MEDIUM",
            fileName=_F_PIPE,
            section="2.01",
            issue=(
                "ASTM A795 is a copper tube standard and cannot be used for the steel "
                "sprinkler pipe this article describes. Replace it with ASTM A53."
            ),
            actionType="EDIT",
            existingText="Steel pipe: ASTM A795, Schedule 10, roll-grooved ends.",
            replacementText="Steel pipe: ASTM A53, Schedule 10, roll-grooved ends.",
            codeReference="ASTM A795",
            confidence=0.7,
        ),
        expected_verdict="DISPUTED",
        rationale="ASTM A795 is a steel pipe standard written for fire protection use.",
        sources=(SRC_ASTM_A795,),
    ),
    VerificationCase(
        case_id="v-fictional-release-panel-approval",
        split=SPLIT_HELD_OUT,
        dimensions=("ambiguous_evidence_unverified",),
        module_id="datacenter_fire",
        finding=_finding(
            severity="HIGH",
            fileName=_F_PREACTION,
            section="2.04",
            issue=(
                "The specified Kestrelbrook Suppression Controls KBR-7Q440 releasing panel "
                "is not listed for preaction releasing service, so the double-interlock "
                "release cannot be accepted. Specify a listed releasing panel."
            ),
            actionType="REPORT_ONLY",
            codeReference="NFPA 13, NFPA 72 (listed releasing service)",
            confidence=0.6,
        ),
        expected_verdict="UNVERIFIED",
        evidence_basis=EVIDENCE_CONSTRUCTED,
        rationale=(
            "The manufacturer and model are invented for this case, so no source can "
            "establish or refute the listing claim. A verifier that CONFIRMs or DISPUTEs "
            "it has grounded a conclusion on evidence about something else."
        ),
    ),
    VerificationCase(
        case_id="v-private-owner-standard-density",
        split=SPLIT_HELD_OUT,
        dimensions=("ambiguous_evidence_unverified", "project_override_mixed_authority"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="HIGH",
            fileName=_F_PREACTION,
            section="1.04",
            issue=(
                "The Owner's Data Center Design Standard DCS-FP-2025, section 4.3, requires "
                "a design density of 0.30 gpm/sq ft over 2,500 sq ft in data halls; the spec's "
                "0.20 gpm/sq ft does not meet it."
            ),
            actionType="REPORT_ONLY",
            codeReference="Owner design standard DCS-FP-2025 section 4.3",
            confidence=0.6,
        ),
        expected_verdict="UNVERIFIED",
        evidence_basis=EVIDENCE_CONSTRUCTED,
        project=_VA_PROJECT,
        rationale=(
            "The owner standard is a private document invented for this case. Neither the "
            "requirement nor its absence can be established from public sources; a code "
            "density is not evidence of what the owner's contract requires."
        ),
    ),
    VerificationCase(
        case_id="v-dsa-seismic-bracing-omitted",
        split=SPLIT_HELD_OUT,
        dimensions=("severe_omission", "correct_citation_supported"),
        module_id="california_k12_mep",
        finding=_finding(
            severity="CRITICAL",
            fileName=_F_CA_SPRINKLER,
            section="3.01",
            issue=(
                "The section states the building is assigned Seismic Design Category D but "
                "contains no requirement for seismic bracing or restraint of sprinkler "
                "piping. For this DSA school project, NFPA 13's earthquake-protection "
                "requirements apply; add them."
            ),
            actionType="ADD",
            replacementText=(
                "Protect sprinkler piping against earthquake damage (sway bracing, "
                "restraint, flexible couplings, and clearances) in accordance with NFPA 13."
            ),
            anchorText="The building is assigned Seismic Design Category D.",
            insertPosition="after",
            codeReference="NFPA 13 (earthquake protection); ASCE 7 Chapter 13",
            confidence=0.9,
        ),
        expected_verdict="CONFIRMED",
        rationale=(
            "Sprinkler piping in Seismic Design Category D is seismically protected per "
            "NFPA 13; a spec that omits it is missing a life-safety requirement."
        ),
        sources=(SRC_NFPA13_SEISMIC,),
    ),
    VerificationCase(
        case_id="v-fdc-omitted-data-hall",
        split=SPLIT_HELD_OUT,
        dimensions=("severe_omission",),
        module_id="datacenter_fire",
        finding=_finding(
            severity="CRITICAL",
            fileName=_F_PREACTION,
            section="2.06",
            issue=(
                "The preaction specification for the two-story data hall building has no "
                "fire department connection. NFPA 13 requires one for a building of this "
                "size, and the fire marshal will expect it; add a fire department connection."
            ),
            actionType="REPORT_ONLY",
            codeReference="NFPA 13, fire department connections",
            confidence=0.85,
        ),
        expected_verdict="CONFIRMED",
        project=_VA_PROJECT,
        rationale=(
            "NFPA 13's exceptions to a fire department connection (remote buildings, "
            "oversize deluge systems, single-story buildings of 2,000 ft² or less) do "
            "not fit a two-story data hall building."
        ),
        sources=(SRC_NFPA13_FDC,),
    ),
    VerificationCase(
        case_id="v-fm-ds-5-32-delete-as-noncode",
        split=SPLIT_HELD_OUT,
        dimensions=("project_override_mixed_authority", "plausible_false_claim"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="HIGH",
            fileName=_F_PREACTION,
            section="1.02",
            issue=(
                "FM Global Data Sheet 5-32 is not adopted by the Virginia Uniform Statewide "
                "Building Code, so it is not an enforceable requirement. Delete it from the "
                "references and design criteria."
            ),
            actionType="DELETE",
            existingText=(
                "Sprinkler protection for data halls shall comply with NFPA 13 and with FM "
                "Global Data Sheet 5-32 as required by the Owner's design standard."
            ),
            codeReference="Virginia USBC; FM Global Data Sheet 5-32",
            confidence=0.7,
        ),
        expected_verdict="DISPUTED",
        acceptable_verdicts=("CORRECTED",),
        project=_VA_PROJECT,
        rationale=(
            "The code does not adopt FM data sheets, but the spec invokes DS 5-32 because "
            "the owner's design standard requires it: a contractual authority, separate "
            "from adopted law. Deleting it on the ground that the code does not adopt it "
            "confuses the two authorities. DISPUTED is expected; a CORRECTED that keeps "
            "the requirement and fixes the reasoning is not wrong. CONFIRMED is."
        ),
        sources=(SRC_FM_DS,),
    ),
    VerificationCase(
        case_id="v-nfpa72-waterflow-out-of-scope-false",
        split=SPLIT_HELD_OUT,
        dimensions=("project_override_mixed_authority", "plausible_false_claim"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="MEDIUM",
            fileName=_F_WET,
            section="2.05",
            issue=(
                "NFPA 72 does not apply to Division 21 fire suppression work. Remove the "
                "requirement that waterflow switches be connected to the fire alarm system "
                "per NFPA 72."
            ),
            actionType="DELETE",
            existingText=(
                "Connect waterflow switches and valve supervisory switches to the building "
                "fire alarm system in accordance with NFPA 72."
            ),
            codeReference="NFPA 72",
            confidence=0.65,
        ),
        expected_verdict="DISPUTED",
        rationale=(
            "Which division specifies a device does not decide which standard governs its "
            "connection: waterflow switches are alarm-initiating devices under NFPA 72. "
            "The case tests mixed authority across the fire suppression and fire alarm "
            "modules."
        ),
        sources=(SRC_NFPA72_WATERFLOW,),
    ),
    VerificationCase(
        case_id="v-nfpa20-150-percent-head",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units", "correct_citation_supported"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="HIGH",
            fileName=_F_PUMP,
            section="2.01",
            issue=(
                "The pump performance requirement accepts 50 percent of rated head at 150 "
                "percent of rated capacity. NFPA 20 requires not less than 65 percent of "
                "rated total head at 150 percent of rated capacity."
            ),
            actionType="EDIT",
            existingText=(
                "At 150 percent of rated capacity, the pump shall deliver not less than 50 "
                "percent of rated total head."
            ),
            replacementText=(
                "At 150 percent of rated capacity, the pump shall deliver not less than 65 "
                "percent of rated total head."
            ),
            codeReference="NFPA 20, centrifugal pump performance",
            confidence=0.9,
        ),
        expected_verdict="CONFIRMED",
        rationale="NFPA 20's 150 percent / 65 percent point is stated in the standard.",
        sources=(SRC_NFPA20,),
    ),
    VerificationCase(
        case_id="v-nfpa13-storage-clearance-12in",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units",),
        module_id="datacenter_fire",
        finding=_finding(
            severity="MEDIUM",
            fileName=_F_WET,
            section="3.02",
            issue=(
                "The spare-parts storage room allows storage within 12 inches of sprinkler "
                "deflectors. NFPA 13 requires a clearance of at least 18 inches between the "
                "deflector and the top of storage."
            ),
            actionType="EDIT",
            existingText=(
                "Maintain a clearance of not less than 12 inches between sprinkler "
                "deflectors and the top of storage."
            ),
            replacementText=(
                "Maintain a clearance of not less than 18 inches between sprinkler "
                "deflectors and the top of storage."
            ),
            codeReference="NFPA 13, clearance to storage",
            confidence=0.85,
        ),
        expected_verdict="CONFIRMED",
        rationale="18 in. is NFPA 13's minimum for standard spray sprinklers; 12 in. is short of it.",
        sources=(SRC_NFPA13_STORAGE,),
    ),
    # ------------------------------------------------------------------ tuning
    VerificationCase(
        case_id="v-nfpa13-hydrostatic-150psi",
        split=SPLIT_TUNING,
        dimensions=("numeric_units", "correct_citation_supported"),
        module_id="datacenter_fire",
        finding=_finding(
            severity="HIGH",
            fileName=_F_WET,
            section="3.08",
            issue=(
                "The spec hydrostatically tests new sprinkler piping at 150 psi for 2 "
                "hours. NFPA 13 requires not less than 200 psi for 2 hours (or 50 psi above "
                "the maximum pressure where that exceeds 150 psi)."
            ),
            actionType="EDIT",
            existingText="Hydrostatically test new piping at 150 psi for 2 hours.",
            replacementText="Hydrostatically test new piping at not less than 200 psi for 2 hours.",
            codeReference="NFPA 13, hydrostatic tests",
            confidence=0.9,
        ),
        expected_verdict="CONFIRMED",
        rationale="NFPA 13's hydrostatic test is 200 psi for 2 hours; 150 psi is short of it.",
        sources=(SRC_NFPA13_HYDRO,),
    ),
    VerificationCase(
        case_id="v-nfpa72-smoke-spacing-false-20ft",
        split=SPLIT_TUNING,
        dimensions=("plausible_false_claim", "numeric_units"),
        module_id="datacenter_electronic_safety_security",
        finding=_finding(
            severity="MEDIUM",
            fileName=_F_ALARM,
            section="3.02",
            issue=(
                "Spot-type smoke detectors are spaced 30 ft apart on smooth ceilings. NFPA "
                "72 limits that spacing to 20 ft; revise."
            ),
            actionType="EDIT",
            existingText=(
                "Space spot-type smoke detectors not more than 30 ft apart on smooth ceilings."
            ),
            replacementText=(
                "Space spot-type smoke detectors not more than 20 ft apart on smooth ceilings."
            ),
            codeReference="NFPA 72, spot-type smoke detector spacing",
            confidence=0.7,
        ),
        expected_verdict="DISPUTED",
        rationale="NFPA 72's smooth-ceiling guide spacing is 30 ft; 20 ft is invented.",
        sources=(SRC_NFPA72_SMOKE,),
    ),
    VerificationCase(
        case_id="v-preaction-supervision-omitted",
        split=SPLIT_TUNING,
        dimensions=("severe_omission",),
        module_id="datacenter_fire",
        finding=_finding(
            severity="CRITICAL",
            fileName=_F_PREACTION,
            section="2.03",
            issue=(
                "The preaction system serves about 400 sprinklers, but the specification "
                "has no automatic supervision of the sprinkler piping or the detection "
                "devices. NFPA 13 requires both to be automatically supervised where a "
                "preaction system has more than 20 sprinklers."
            ),
            actionType="REPORT_ONLY",
            codeReference="NFPA 13, preaction systems",
            confidence=0.85,
        ),
        expected_verdict="CONFIRMED",
        rationale="NFPA 13 states the more-than-20-sprinklers supervision requirement for preaction systems.",
        sources=(SRC_NFPA13_PREACTION,),
    ),
)


# --------------------------------------------------------------------------
# New review cases (all held-out except the one tuning omission spec)
# --------------------------------------------------------------------------

_VA_CONTEXT = (
    "Project location: Ashburn (Loudoun County), Virginia, US.\n"
    "Governing building code: 2021 Virginia Uniform Statewide Building Code (Virginia "
    "Construction Code), based on the 2021 International Building Code.\n"
    "Sprinkler standard referenced by the adopted code: NFPA 13-2019.\n"
    "Seismic loads: ASCE 7-16, as referenced by the 2021 IBC.\n"
    "Owner design standard: requires FM Global Data Sheet 5-32 for data halls."
)

_ELECTRICAL_ROOM_LINE = (
    "B. Sprinklers are omitted from the electrical room, which is dedicated to dry-type "
    "electrical equipment, enclosed in 2-hour fire-rated construction with protected "
    "penetrations, and has no storage."
)

NEW_REVIEW_CASES: tuple[ReviewCase, ...] = (
    ReviewCase(
        case_id="r-dc-preaction-data-hall",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units", "exception_negation"),
        module_id="datacenter_fire",
        filename=_F_PREACTION,
        project=_VA_PROJECT,
        project_context=_VA_CONTEXT,
        spec_text=(
            "SECTION 21 13 16 - PREACTION SPRINKLER SYSTEMS\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Double-interlock preaction sprinkler protection for the data halls.\n"
            "1.02 REFERENCES\n"
            "A. NFPA 13, Standard for the Installation of Sprinkler Systems, 2019 edition, as "
            "referenced by the Virginia Construction Code.\n"
            "PART 2 PRODUCTS\n"
            "2.01 SPRINKLERS\n"
            "A. Listed standard-response pendent spray sprinklers, 155 degF rated.\n"
            "PART 3 EXECUTION\n"
            "3.01 INSTALLATION\n"
            "A. Data halls are Ordinary Hazard Group 1. Maximum protection area per "
            "sprinkler: 225 sq ft.\n"
            f"{_ELECTRICAL_ROOM_LINE}\n"
            "3.02 TESTING\n"
            "A. Hydrostatically test new piping at 150 psi for 2 hours.\n"
            "B. Additives such as sodium silicate shall not be used during hydrostatic "
            "testing or to stop leaks."
        ),
        defects=(
            ExpectedDefect(
                label="Ordinary hazard coverage set at the light hazard 225 sq ft",
                severity="HIGH",
                match_any=(("225",),),
            ),
            ExpectedDefect(
                label="Hydrostatic test at 150 psi instead of 200 psi",
                severity="HIGH",
                match_any=(("150 psi",), ("hydrostatic",)),
            ),
        ),
        traps=(
            Trap(
                label="Electrical room omission meets NFPA 13's four conditions",
                match_any=(("electrical room",),),
                why="NFPA 13 permits the omission; the text states every condition.",
            ),
            Trap(
                label="The sodium silicate sentence already prohibits additives",
                match_any=(("sodium silicate",),),
                why="A negation, stated in NFPA 13's own terms.",
            ),
            Trap(
                label="NFPA 13-2019 is the edition Virginia's code references",
                match_any=(("nfpa 13", "2022"),),
                why="Proposing NFPA 13-2022 overrides the adopted edition in Project Context.",
            ),
        ),
        rationale=(
            "Two severe numeric defects beside three correct statements a review must "
            "leave alone: an exception, a negation, and an older adopted edition."
        ),
        sources=(SRC_NFPA13_AREA, SRC_NFPA13_HYDRO, SRC_NFPA13_ELEC),
    ),
    ReviewCase(
        case_id="r-ca-sprinkler-seismic-omitted",
        split=SPLIT_HELD_OUT,
        dimensions=("severe_omission", "correct_citation_supported"),
        module_id="california_k12_mep",
        filename=_F_CA_SPRINKLER,
        spec_text=(
            "SECTION 21 13 13 - WET-PIPE SPRINKLER SYSTEMS\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Wet-pipe sprinkler system for the new two-story classroom building.\n"
            "B. The building is assigned Seismic Design Category D.\n"
            "1.02 REFERENCES\n"
            "A. NFPA 13, as adopted and amended by the 2025 California Fire Code.\n"
            "1.03 SUBMITTALS\n"
            "A. Working plans and hydraulic calculations, stamped by the fire protection "
            "engineer, for review by the Division of the State Architect.\n"
            "PART 2 PRODUCTS\n"
            "2.01 PIPE\n"
            "A. Steel pipe: ASTM A795, Schedule 10, roll-grooved ends.\n"
            "PART 3 EXECUTION\n"
            "3.01 INSTALLATION\n"
            "A. Classrooms are Light Hazard. Maximum protection area per sprinkler: "
            "225 sq ft, hydraulically calculated.\n"
            "3.02 TESTING\n"
            "A. Hydrostatically test new piping at not less than 200 psi for 2 hours."
        ),
        defects=(
            ExpectedDefect(
                label="No seismic bracing or restraint of sprinkler piping in SDC D",
                severity="CRITICAL",
                match_any=(("seismic",), ("brac",), ("earthquake",)),
            ),
        ),
        traps=(
            Trap(
                label="225 sq ft is the light hazard maximum",
                match_any=(("225",),),
                why="Correct for light hazard, hydraulically calculated.",
            ),
            Trap(
                label="200 psi for 2 hours is NFPA 13's hydrostatic test",
                match_any=(("200 psi",),),
                why="Correct as written.",
            ),
            Trap(
                label="ASTM A795 is a fire protection steel pipe standard",
                match_any=(("a795",),),
                why="Correct as written.",
            ),
        ),
        rationale=(
            "A life-safety omission (no seismic protection in SDC D) in a spec whose other "
            "numbers are right."
        ),
        sources=(SRC_NFPA13_SEISMIC, SRC_NFPA13_AREA, SRC_NFPA13_HYDRO, SRC_ASTM_A795),
    ),
    ReviewCase(
        case_id="r-dc-fire-alarm-devices",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units", "correct_citation_supported"),
        module_id="datacenter_electronic_safety_security",
        filename=_F_ALARM,
        project=_VA_PROJECT,
        project_context=_VA_CONTEXT.replace(
            "Sprinkler standard referenced by the adopted code: NFPA 13-2019.",
            "Fire alarm standard referenced by the adopted code: NFPA 72-2019.",
        ),
        spec_text=(
            "SECTION 28 46 21 - FIRE ALARM\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Addressable fire alarm system for the data center building.\n"
            "1.02 REFERENCES\n"
            "A. NFPA 72, National Fire Alarm and Signaling Code, 2019 edition, as "
            "referenced by the Virginia Construction Code.\n"
            "PART 3 EXECUTION\n"
            "3.02 DETECTION\n"
            "A. Space spot-type smoke detectors not more than 45 ft apart on smooth "
            "ceilings in electrical and battery rooms.\n"
            "3.03 MANUAL FIRE ALARM BOXES\n"
            "A. Provide a manual fire alarm box within 5 ft of the entrance to each exit.\n"
            "B. Provide additional manual fire alarm boxes so that the travel distance to "
            "the nearest box does not exceed 200 ft, measured horizontally on the same floor.\n"
            "C. Mount manual fire alarm boxes with the operable part 54 inches above "
            "finished floor."
        ),
        defects=(
            ExpectedDefect(
                label="Smoke detector spacing of 45 ft exceeds the 30 ft smooth-ceiling guide",
                severity="HIGH",
                match_any=(("45 ft",), ("spacing",)),
            ),
            ExpectedDefect(
                label="Manual box operable part at 54 in., outside 42-48 in.",
                severity="MEDIUM",
                match_any=(("54",),),
            ),
        ),
        traps=(
            Trap(
                label="200 ft travel distance is NFPA 72's limit",
                match_any=(("200 ft",),),
                why="Correct as written.",
            ),
            Trap(
                label="5 ft from each exit entrance is NFPA 72's rule",
                match_any=(("5 ft",),),
                why="Correct as written.",
            ),
        ),
        rationale="One severe spacing defect and one accessibility-height defect beside two correct NFPA 72 rules.",
        sources=(SRC_NFPA72_SMOKE, SRC_NFPA72_MFAB),
    ),
    ReviewCase(
        case_id="r-dc-fire-pump",
        split=SPLIT_HELD_OUT,
        dimensions=("numeric_units",),
        module_id="datacenter_fire",
        filename=_F_PUMP,
        project=_VA_PROJECT,
        project_context=_VA_CONTEXT,
        spec_text=(
            "SECTION 21 30 00 - FIRE PUMPS\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Electric-motor-driven horizontal split-case fire pump, 1,500 gpm at 100 psi.\n"
            "1.02 REFERENCES\n"
            "A. NFPA 20, Standard for the Installation of Stationary Pumps for Fire Protection.\n"
            "PART 2 PRODUCTS\n"
            "2.01 PUMP\n"
            "A. The pump shall be listed for fire protection service.\n"
            "B. At 150 percent of rated capacity, the pump shall deliver not less than 50 "
            "percent of rated total head.\n"
            "C. Shutoff (churn) head shall not exceed 150 percent of rated total head.\n"
            "2.02 CONTROLLER\n"
            "A. Listed fire pump controller, arranged for automatic start on pressure drop."
        ),
        defects=(
            ExpectedDefect(
                label="Head at 150 percent capacity set at 50 percent instead of 65 percent",
                severity="HIGH",
                match_any=(("65",), ("50 percent",)),
            ),
            ExpectedDefect(
                label="Shutoff head allowed to 150 percent instead of 140 percent",
                severity="HIGH",
                match_any=(("140",), ("shutoff",), ("churn",)),
            ),
        ),
        traps=(
            Trap(
                label="A listed controller with automatic start is correct",
                match_any=(("controller",),),
                why="Correct as written.",
                actions=("DELETE",),
            ),
        ),
        rationale="Two severe numeric defects in the pump curve, NFPA 20's two acceptance points.",
        sources=(SRC_NFPA20,),
    ),
    ReviewCase(
        case_id="r-dc-storage-and-references",
        split=SPLIT_HELD_OUT,
        dimensions=("low_severity_factual", "numeric_units"),
        module_id="datacenter_fire",
        filename=_F_PIPE,
        project=_VA_PROJECT,
        project_context=_VA_CONTEXT,
        spec_text=(
            "SECTION 21 05 23 - FIRE SUPPRESSION PIPING\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Sprinkler piping for the data center building, including the spare-parts "
            "storage room.\n"
            "1.02 REFERENCES\n"
            "A. ASTM A135 - Standard Specification for Seamless Steel Pipe.\n"
            "B. ASTM A795 - Standard Specification for Black and Hot-Dipped Zinc-Coated "
            "(Galvanized) Welded and Seamless Steel Pipe for Fire Protection Use.\n"
            "PART 2 PRODUCTS\n"
            "2.01 PIPE\n"
            "A. Steel pipe: ASTM A795, Schedule 10, roll-grooved ends.\n"
            "PART 3 EXECUTION\n"
            "3.02 STORAGE ROOM\n"
            "A. Maintain a clearance of not less than 12 inches between sprinkler "
            "deflectors and the top of storage."
        ),
        defects=(
            ExpectedDefect(
                label="Storage clearance of 12 in. is short of NFPA 13's 18 in.",
                severity="HIGH",
                match_any=(("18",), ("12 inches",), ("clearance",)),
            ),
            ExpectedDefect(
                label="ASTM A135 title misstated as seamless",
                severity="GRIPES",
                match_any=(("a135",),),
            ),
        ),
        traps=(
            Trap(
                label="ASTM A795 is correctly titled and correctly used",
                match_any=(("a795",),),
                why="Correct as written.",
            ),
        ),
        rationale="A severe clearance defect and a minor title error; A795 is a trap.",
        sources=(SRC_NFPA13_STORAGE, SRC_ASTM_A135, SRC_ASTM_A795),
    ),
    ReviewCase(
        case_id="r-dc-owner-standard-no-fdc",
        split=SPLIT_HELD_OUT,
        dimensions=("severe_omission", "project_override_mixed_authority"),
        module_id="datacenter_fire",
        filename=_F_WET,
        project=_VA_PROJECT,
        project_context=_VA_CONTEXT,
        spec_text=(
            "SECTION 21 13 13 - WET-PIPE SPRINKLER SYSTEMS\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Wet-pipe sprinkler systems for the two-story data center office and "
            "support areas.\n"
            "1.02 REFERENCES\n"
            "A. NFPA 13-2019, as referenced by the Virginia Construction Code.\n"
            "B. Sprinkler protection shall comply with NFPA 13 and with FM Global Data Sheet "
            "5-32 as required by the Owner's design standard.\n"
            "PART 2 PRODUCTS\n"
            "2.01 VALVES AND DEVICES\n"
            "A. Listed alarm check valves, waterflow switches, and supervisory switches.\n"
            "B. Connect waterflow switches and valve supervisory switches to the building "
            "fire alarm system in accordance with NFPA 72.\n"
            "PART 3 EXECUTION\n"
            "3.01 INSTALLATION\n"
            "A. Install systems complete from the service entrance to every sprinkler, "
            "including the riser, drains, and inspector's test connections."
        ),
        defects=(
            ExpectedDefect(
                label="No fire department connection for a two-story building",
                severity="CRITICAL",
                match_any=(("fire department connection",), ("fdc",)),
            ),
        ),
        traps=(
            Trap(
                label="The FM Global requirement is the owner's, not the code's",
                match_any=(("fm global",), ("5-32",)),
                why="A contractual authority stands beside adopted law; neither discharges the other.",
                actions=("EDIT", "DELETE"),
            ),
            Trap(
                label="NFPA 13-2019 is the edition Virginia's code references",
                match_any=(("nfpa 13", "2022"),),
                why="Proposing NFPA 13-2022 overrides the adopted edition in Project Context.",
            ),
            Trap(
                label="NFPA 72 governs waterflow switch connections",
                match_any=(("nfpa 72",),),
                why="Correct as written; the switch is an alarm-initiating device.",
                actions=("EDIT", "DELETE"),
            ),
        ),
        rationale=(
            "A severe omission (no FDC) in a spec that mixes adopted law, an owner "
            "requirement, and a cross-discipline standard, all correct."
        ),
        sources=(SRC_NFPA13_FDC, SRC_FM_DS, SRC_NFPA72_WATERFLOW),
    ),
    ReviewCase(
        case_id="r-ca-standpipe",
        split=SPLIT_HELD_OUT,
        dimensions=("wrong_edition_adoption", "numeric_units"),
        module_id="california_k12_mep",
        filename=_F_STANDPIPE,
        spec_text=(
            "SECTION 21 12 00 - FIRE-SUPPRESSION STANDPIPES\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Class I standpipes in the stairways of the new three-story classroom "
            "building.\n"
            "1.02 REFERENCES\n"
            "A. NFPA 14, as adopted and amended by the 2025 California Fire Code.\n"
            "PART 2 PRODUCTS\n"
            "2.01 DESIGN\n"
            "A. Design Class I standpipes for 65 psi residual pressure at the "
            "hydraulically most remote 2-1/2 inch hose connection.\n"
            "B. Hose connections: 2-1/2 inch, with caps and chains."
        ),
        defects=(
            ExpectedDefect(
                label="65 psi residual is a pre-1993 value; NFPA 14 requires 100 psi",
                severity="HIGH",
                match_any=(("65 psi",), ("100 psi",), ("residual",)),
            ),
        ),
        traps=(
            Trap(
                label="2-1/2 inch hose connections are correct for Class I",
                match_any=(("2-1/2",),),
                why="Correct as written.",
                actions=("EDIT", "DELETE"),
            ),
        ),
        rationale="An outdated numeric requirement (an old edition's value) in a short spec.",
        sources=(SRC_NFPA14,),
    ),
    ReviewCase(
        case_id="r-dc-clean-wet-pipe",
        split=SPLIT_HELD_OUT,
        dimensions=("correct_citation_supported", "project_override_mixed_authority"),
        module_id="datacenter_fire",
        filename="21 13 13 - Wet-Pipe Sprinkler Systems (Support Building).docx",
        project=_VA_PROJECT,
        project_context=_VA_CONTEXT,
        is_clean=True,
        spec_text=(
            "SECTION 21 13 13 - WET-PIPE SPRINKLER SYSTEMS\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Wet-pipe sprinkler systems for the two-story support building: offices, "
            "shipping and receiving, and the spare-parts storage room.\n"
            "1.02 REFERENCES\n"
            "A. NFPA 13-2019, as referenced by the 2021 Virginia Construction Code.\n"
            "B. NFPA 72-2019 for alarm-initiating device connections.\n"
            "C. ASCE 7-16 for seismic design forces, as referenced by the 2021 IBC.\n"
            "1.03 SUBMITTALS\n"
            "A. Working plans, hydraulic calculations, and product data, submitted to the "
            "authority having jurisdiction before installation.\n"
            "PART 2 PRODUCTS\n"
            "2.01 PIPE AND FITTINGS\n"
            "A. Steel pipe: ASTM A795, Schedule 10, roll-grooved ends, listed grooved couplings.\n"
            "2.02 SPRINKLERS\n"
            "A. Listed quick-response pendent spray sprinklers in offices; listed "
            "standard-response upright spray sprinklers in shipping and storage areas.\n"
            "2.03 FIRE DEPARTMENT CONNECTION\n"
            "A. Listed 2-1/2 inch by 2-1/2 inch by 4 inch fire department connection at the "
            "location approved by the fire marshal.\n"
            "PART 3 EXECUTION\n"
            "3.01 INSTALLATION\n"
            "A. Offices are Light Hazard: maximum protection area per sprinkler 225 sq ft, "
            "hydraulically calculated. Shipping, receiving, and storage are Ordinary Hazard "
            "Group 2: maximum protection area per sprinkler 130 sq ft.\n"
            "B. Maintain a clearance of not less than 18 inches between sprinkler "
            "deflectors and the top of storage.\n"
            "C. Protect piping against earthquake damage in accordance with NFPA 13.\n"
            "3.02 TESTING\n"
            "A. Hydrostatically test new piping at not less than 200 psi for 2 hours.\n"
            "B. Connect waterflow switches to the building fire alarm system in accordance "
            "with NFPA 72."
        ),
        traps=(
            Trap(
                label="NFPA 13-2019 / NFPA 72-2019 / ASCE 7-16 are Virginia's adopted editions",
                match_any=(("2022",), ("2024",), ("7-22",)),
                why="Proposing a newer edition overrides the adoption in Project Context.",
            ),
            Trap(
                label="225 sq ft (light hazard) and 130 sq ft (ordinary hazard) are right",
                match_any=(("225",), ("130",)),
                why="Correct as written.",
            ),
            Trap(
                label="18 in. storage clearance and 200 psi test are right",
                match_any=(("18 inches",), ("200 psi",)),
                why="Correct as written.",
            ),
        ),
        rationale=(
            "A clean control. Every stated value and edition is correct for a Virginia "
            "project; any finding proposing a newer edition or different number is unsupported."
        ),
        sources=(SRC_NFPA13_AREA, SRC_NFPA13_STORAGE, SRC_NFPA13_HYDRO, SRC_NFPA13_FDC),
    ),
    # ------------------------------------------------------------------ tuning
    ReviewCase(
        case_id="r-dc-preaction-no-supervision",
        split=SPLIT_TUNING,
        dimensions=("severe_omission",),
        module_id="datacenter_fire",
        filename="21 13 16 - Preaction Sprinkler Systems (Hall B).docx",
        project=_VA_PROJECT,
        project_context=_VA_CONTEXT,
        spec_text=(
            "SECTION 21 13 16 - PREACTION SPRINKLER SYSTEMS\n"
            "PART 1 GENERAL\n"
            "1.01 SUMMARY\n"
            "A. Single-interlock preaction system for data hall B, approximately 400 "
            "sprinklers.\n"
            "1.02 REFERENCES\n"
            "A. NFPA 13-2019, as referenced by the Virginia Construction Code.\n"
            "PART 2 PRODUCTS\n"
            "2.01 VALVES\n"
            "A. Listed preaction valve with electric release.\n"
            "2.02 DETECTION\n"
            "A. Spot-type smoke detectors actuate the release.\n"
            "PART 3 EXECUTION\n"
            "3.01 INSTALLATION\n"
            "A. Install piping pitched to drain."
        ),
        defects=(
            ExpectedDefect(
                label="No supervision of piping or detection on a 400-sprinkler preaction system",
                severity="CRITICAL",
                match_any=(("supervis",),),
            ),
        ),
        rationale="The tuning split's severe omission: supervision is required above 20 sprinklers.",
        sources=(SRC_NFPA13_PREACTION,),
    ),
)


# --------------------------------------------------------------------------
# Cases reused from adjudicated material (all prior exposure → tuning)
# --------------------------------------------------------------------------

#: Live captures resolved in the calibration oracle ledger, with the
#: dimensions each covers. The verdict, rationale, and sources come from the
#: ledger; the finding from the capture.
LEDGER_VERIFICATION_REFERENCES: dict[str, tuple[str, ...]] = {
    "live_stale_cbc_0": ("wrong_edition_adoption", "correct_citation_supported"),
    "live_stale_cpc_0": ("wrong_edition_adoption",),
    "live_stale_asce7_0": ("wrong_edition_adoption",),
    "live_stale_ashrae15_0": ("wrong_edition_adoption",),
    "live_stale_nfpa72_0": ("wrong_edition_adoption",),
    "live_seismic_exemption_0": ("exception_negation", "plausible_false_claim"),
    "live_duct_pressure_contradiction_0": ("numeric_units",),
    "live_placeholder_selection_0": ("correct_citation_supported",),
    "live_template_todo_marker_0": ("correct_citation_supported",),
}

#: Ledger captures left out, with the reason (the ledger has no label for them).
LEDGER_EXCLUSIONS: dict[str, str] = {
    "live_invalid_2018_cbc_0": "unresolved in the oracle ledger (no label to score against)",
    "live_obscure_product_rating_0": "unresolved in the oracle ledger (no label to score against)",
    "live_duplicate_paragraph_0": (
        "a GRIPES duplicate-paragraph finding is classified locally (no model call), "
        "so it measures nothing about a model or effort arm"
    ),
}

_LEDGER_PRIOR_EXPOSURE = (
    "captured from a labeled spec by the 2026-09-09 live-capture baseline, which "
    "retuned prompts and labels on it"
)

#: Labeled specs (evals.labeled_specs), reviewed under the California module.
LABELED_SPEC_REFERENCES: dict[str, tuple[str, ...]] = {
    "clean_hydronic": ("correct_citation_supported",),
    "stale_cbc": ("wrong_edition_adoption",),
    "stale_ashrae15": ("wrong_edition_adoption",),
    "duct_pressure_contradiction": ("numeric_units",),
    "obscure_product_rating": ("ambiguous_evidence_unverified",),
    "placeholder_selection": ("correct_citation_supported",),
    "template_todo_marker": ("low_severity_factual",),
    "duplicate_paragraph": ("low_severity_factual",),
    "invalid_2018_cbc": ("wrong_edition_adoption", "plausible_false_claim"),
    "stale_cpc": ("wrong_edition_adoption",),
    "stale_asce7": ("wrong_edition_adoption",),
    "stale_nfpa72": ("wrong_edition_adoption",),
    "seismic_exemption": ("exception_negation", "severe_omission"),
    "clean_sanitary": ("correct_citation_supported",),
}

_LABELED_SPEC_PRIOR_EXPOSURE = (
    "labeled spec of the live-capture set; the 2026-09-09 baseline retuned prompts and "
    "labels on it"
)

#: Data-center applicability scenarios (evals.dc_applicability) reused as review
#: cases. The partial-research scenario is judged on report surfaces and the
#: no-research one allows silence either way, so neither is a review case.
DC_SCENARIO_REFERENCES: dict[str, tuple[str, ...]] = {
    "dc_va_correct_older_edition": ("wrong_edition_adoption", "project_override_mixed_authority"),
    "dc_va_asce7_prompt_example_collision": ("wrong_edition_adoption",),
    "dc_oh_code_named_2024_based_2021": ("wrong_edition_adoption", "project_override_mixed_authority"),
    "dc_owner_standard_stricter_than_code": ("project_override_mixed_authority",),
    "dc_inversion_correct_finding_discarded": ("wrong_edition_adoption", "project_override_mixed_authority"),
}

DC_SCENARIO_EXCLUSIONS: dict[str, str] = {
    "dc_partial_research_missing_dimension": "judged on report surfaces, not on review findings",
    "dc_no_research_generic_pins": "a finding is optional either way, so it cannot score recall or traps",
}

_DC_PRIOR_EXPOSURE = (
    "data-center applicability scenario; it shaped the edition-authority correction and "
    "the S18 review prompt change"
)

#: Traps per scenario: what a review must not propose there.
_DC_SCENARIO_TRAPS: dict[str, tuple[Trap, ...]] = {
    "dc_va_correct_older_edition": (
        Trap(
            label="Proposes the module pin (2024 IBC / NFPA 13-2022) for Virginia",
            match_any=(("2024",), ("nfpa 13", "2022")),
            why="Virginia's adoption governs: 2021 IBC and NFPA 13-2019.",
        ),
    ),
    "dc_va_asce7_prompt_example_collision": (
        Trap(
            label="Proposes ASCE 7-22 for a 2021-IBC jurisdiction",
            match_any=(("7-22",),),
            why="The 2021 IBC references ASCE 7-16.",
        ),
    ),
    "dc_oh_code_named_2024_based_2021": (
        Trap(
            label="Proposes NFPA 13-2022 because the Ohio code is named 2024",
            match_any=(("nfpa 13", "2022"),),
            why="The 2024 Ohio Building Code is based on the 2021 IBC.",
        ),
    ),
    "dc_owner_standard_stricter_than_code": (
        Trap(
            label="Deletes or rewrites the owner's FM Global requirement",
            match_any=(("fm global",), ("5-32",)),
            why="A contractual authority stands beside adopted law.",
            actions=("EDIT", "DELETE"),
        ),
    ),
}


def _dc_project_context(adoption: Any) -> str:
    if adoption is None:
        return ""
    return (
        f"Project jurisdiction: {adoption.jurisdiction}.\n"
        f"Governing building code: {adoption.code_name}, based on the "
        f"{adoption.model_code_base}.\n"
        f"Referenced standards: {adoption.referenced_nfpa13}; {adoption.referenced_asce7}.\n"
        f"Effective: {adoption.effective}"
    )


def _dc_sources(adoption: Any) -> tuple[Source, ...]:
    if adoption is None:
        return ()
    return tuple(Source(citation=text) for text in adoption.sources)


#: Finding fields a case may carry (the rest of a capture's finding is runtime state).
_FINDING_FIELDS = frozenset(
    {
        "severity", "fileName", "section", "issue", "actionType", "existingText",
        "replacementText", "codeReference", "confidence", "anchorText",
        "insertPosition", "evidenceElementId",
    }
)


def _ledger_cases() -> list[VerificationCase]:
    from .calibration import oracle_reviews

    ledger = oracle_reviews.load_ledger()
    out: list[VerificationCase] = []
    for fixture_id, dims in LEDGER_VERIFICATION_REFERENCES.items():
        review = ledger.reviews[fixture_id]
        raw = json.loads((_LIVE_FIXTURES_DIR / f"{fixture_id}.json").read_text(encoding="utf-8"))
        if oracle_reviews.evidence_digest(raw) != review.evidence_sha256:
            raise ValueError(
                f"{fixture_id}: the capture changed since it was adjudicated "
                "(evidence digest mismatch); re-adjudicate before reusing it"
            )
        # The captures pin the California cycle (``evals/live_capture.py``).
        finding = {k: v for k, v in dict(raw["finding"]).items() if k in _FINDING_FIELDS}
        # A ledger record may also cite this repository's classifier code; that
        # explains which status the classifier assigns, not the verdict, and
        # this set derives the status from the verdict. Only the external
        # sources carry over; with none left, the quoted spec text is the evidence.
        external = tuple(
            Source(citation=str(src))
            for src in review.sources
            if not any(marker in str(src).lower() for marker in _REPO_SOURCE_MARKERS)
        )
        out.append(
            VerificationCase(
                case_id=f"v-ledger-{fixture_id}",
                split=SPLIT_TUNING,
                dimensions=dims,
                module_id="california_k12_mep",
                finding=finding,
                expected_verdict=str(review.correct_verdict),
                rationale=str(review.rationale),
                sources=external,
                evidence_basis=EVIDENCE_EXTERNAL if external else EVIDENCE_SPEC_TEXT,
                provenance=f"oracle_ledger:{fixture_id}",
                prior_exposure=_LEDGER_PRIOR_EXPOSURE,
            )
        )
    return out


def _labeled_spec_cases() -> list[ReviewCase]:
    from .labeled_specs import LABELED_SPECS

    by_id = {s.spec_id: s for s in LABELED_SPECS}
    out: list[ReviewCase] = []
    for spec_id, dims in LABELED_SPEC_REFERENCES.items():
        spec = by_id[spec_id]
        out.append(
            ReviewCase(
                case_id=f"r-labeled-{spec_id}",
                split=SPLIT_TUNING,
                dimensions=dims,
                module_id="california_k12_mep",
                filename=spec.filename,
                spec_text=spec.spec_text,
                is_clean=spec.is_clean,
                defects=tuple(
                    ExpectedDefect(
                        label=d.label,
                        severity=d.expected_severity,
                        match_any=(tuple(d.must_match),),
                    )
                    for d in spec.expected_defects
                ),
                rationale=f"Labeled spec '{spec_id}' ({spec.category}); labels from evals.labeled_specs.",
                provenance=f"labeled_spec:{spec_id}",
                prior_exposure=_LABELED_SPEC_PRIOR_EXPOSURE,
            )
        )
    return out


def _dc_scenario_cases() -> tuple[list[ReviewCase], list[VerificationCase]]:
    from .dc_applicability import SCENARIOS

    by_id = {s.scenario_id: s for s in SCENARIOS}
    reviews: list[ReviewCase] = []
    verifications: list[VerificationCase] = []
    for scenario_id, dims in DC_SCENARIO_REFERENCES.items():
        s = by_id[scenario_id]
        defects: tuple[ExpectedDefect, ...] = ()
        if s.finding_expectation == "required":
            defects = (
                ExpectedDefect(
                    label=s.expected_review_finding,
                    severity="HIGH",
                    match_any=(("2019",),),
                ),
            )
        reviews.append(
            ReviewCase(
                case_id=f"r-{scenario_id}",
                split=SPLIT_TUNING,
                dimensions=dims,
                module_id=s.module_id,
                filename=f"{scenario_id}.docx",
                spec_text=s.spec_excerpt,
                is_clean=s.finding_expectation == "none",
                defects=defects,
                traps=_DC_SCENARIO_TRAPS.get(scenario_id, ()),
                project=dict(s.project),
                project_context=_dc_project_context(s.adoption),
                rationale=s.summary,
                sources=_dc_sources(s.adoption),
                provenance=f"dc_scenario:{scenario_id}",
                prior_exposure=_DC_PRIOR_EXPOSURE,
            )
        )
        if s.finding_expectation == "required":
            verifications.append(
                VerificationCase(
                    case_id=f"v-{scenario_id}",
                    split=SPLIT_TUNING,
                    dimensions=dims,
                    module_id=s.module_id,
                    finding=_finding(
                        severity="HIGH",
                        fileName=f"{scenario_id}.docx",
                        section="A",
                        issue=s.expected_review_finding,
                        actionType="REPORT_ONLY",
                        codeReference="NFPA 13; 2021 Virginia USBC",
                        confidence=0.8,
                    ),
                    expected_verdict=s.expected_verdict,
                    acceptable_verdicts=("UNVERIFIED",),
                    project=dict(s.project),
                    rationale=(
                        f"{s.summary} UNVERIFIED is a partial pass (the finding survives); "
                        "DISPUTED is the failure the scenario exists to catch."
                    ),
                    sources=_dc_sources(s.adoption),
                    provenance=f"dc_scenario:{scenario_id}",
                    prior_exposure=_DC_PRIOR_EXPOSURE,
                )
            )
    return reviews, verifications


def load_dataset() -> tuple[EvalCase, ...]:
    """Every case: the new ones plus the resolved references, in a fixed order."""
    dc_reviews, dc_verifications = _dc_scenario_cases()
    cases: list[EvalCase] = [
        *NEW_VERIFICATION_CASES,
        *_ledger_cases(),
        *dc_verifications,
        *NEW_REVIEW_CASES,
        *_labeled_spec_cases(),
        *dc_reviews,
    ]
    return tuple(cases)


def verification_cases(cases: Iterable[EvalCase], *, split: str | None = None) -> list[VerificationCase]:
    return [c for c in cases if isinstance(c, VerificationCase) and (split is None or c.split == split)]


def review_cases(cases: Iterable[EvalCase], *, split: str | None = None) -> list[ReviewCase]:
    return [c for c in cases if isinstance(c, ReviewCase) and (split is None or c.split == split)]


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def case_payload(case: EvalCase) -> dict[str, Any]:
    payload = asdict(case)
    payload["stage"] = case.stage
    return payload


def case_digest(case: EvalCase) -> str:
    """SHA-256 of the case's full content (what a model sees and what it is scored against)."""
    return hashlib.sha256(_canonical(case_payload(case)).encode("utf-8")).hexdigest()


def dataset_digest(cases: Iterable[EvalCase], *, split: str | None = None) -> str:
    """SHA-256 over the dataset version and every (case id, case digest), sorted."""
    rows = sorted(
        (c.case_id, case_digest(c)) for c in cases if split is None or c.split == split
    )
    return hashlib.sha256(_canonical({"version": DATASET_VERSION, "cases": rows}).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _source_problems(case: EvalCase) -> list[str]:
    problems = []
    for src in case.sources:
        text = f"{src.citation} {src.url}".lower()
        if not src.citation.strip():
            problems.append(f"{case.case_id}: a source has no citation")
        for marker in _REPO_SOURCE_MARKERS:
            if marker in text:
                problems.append(
                    f"{case.case_id}: source '{src.citation[:60]}' cites this repository "
                    f"({marker!r}); facts must come from outside it"
                )
    return problems


def validate_dataset(cases: Iterable[EvalCase] | None = None) -> list[str]:
    """Problems with the set; an empty list means it is valid."""
    from src.modules.registry import AVAILABLE_MODULES, get_module
    from src.review.reviewer import Finding
    from src.verification.verification_cache import make_cache_key

    cases = list(load_dataset() if cases is None else cases)
    problems: list[str] = []
    seen: set[str] = set()
    cache_keys: dict[str, str] = {}
    review_files: dict[tuple[str, str], str] = {}
    module_ids = set(AVAILABLE_MODULES)

    for case in cases:
        cid = case.case_id
        if cid in seen:
            problems.append(f"{cid}: duplicate case_id")
        seen.add(cid)
        if case.split not in SPLITS:
            problems.append(f"{cid}: split {case.split!r} is not one of {SPLITS}")
        if case.prior_exposure and case.split != SPLIT_TUNING:
            problems.append(
                f"{cid}: a case with prior exposure ({case.prior_exposure[:50]}...) must be "
                "in the tuning split; held-out cases are ones no prompt was tuned on"
            )
        if not case.dimensions:
            problems.append(f"{cid}: no dimensions")
        for dim in case.dimensions:
            if dim not in REQUIRED_DIMENSIONS:
                problems.append(f"{cid}: unknown dimension {dim!r}")
        if case.module_id not in module_ids:
            problems.append(f"{cid}: unknown module {case.module_id!r}")
        if not case.rationale.strip():
            problems.append(f"{cid}: no rationale")
        problems.extend(_source_problems(case))

        if isinstance(case, VerificationCase):
            if case.expected_verdict not in VERDICTS:
                problems.append(f"{cid}: expected_verdict {case.expected_verdict!r} is not a verdict")
            for v in case.acceptable_verdicts:
                if v not in VERDICTS or v == case.expected_verdict:
                    problems.append(f"{cid}: acceptable verdict {v!r} is not a distinct verdict")
            if case.evidence_basis == EVIDENCE_EXTERNAL and not case.sources:
                problems.append(f"{cid}: an externally evidenced case needs a source")
            elif case.evidence_basis == EVIDENCE_CONSTRUCTED:
                if case.expected_verdict != "UNVERIFIED":
                    problems.append(
                        f"{cid}: a constructed case (nothing can establish it) must expect UNVERIFIED"
                    )
            elif case.evidence_basis == EVIDENCE_SPEC_TEXT:
                if not (case.finding.get("existingText") or case.finding.get("issue")):
                    problems.append(f"{cid}: a spec-text case must quote or describe the text")
            elif case.evidence_basis not in EVIDENCE_BASES:
                problems.append(f"{cid}: unknown evidence_basis {case.evidence_basis!r}")
            if case.severity not in SEVERITIES:
                problems.append(f"{cid}: finding severity {case.severity!r} is not a severity")
            try:
                finding = Finding(**dict(case.finding))
            except TypeError as exc:
                problems.append(f"{cid}: finding does not construct ({exc})")
                continue
            if case.module_id in module_ids:
                key = make_cache_key(finding, cycle=get_module(case.module_id).cycle)
                if key in cache_keys:
                    # Two cases with one cache key: the second would replay the
                    # first's verdict inside an arm and never reach the model.
                    problems.append(
                        f"{cid}: same verification cache key as {cache_keys[key]}; the "
                        "second would be a cache hit inside an arm"
                    )
                cache_keys[key] = cid
        else:
            if not case.spec_text.strip():
                problems.append(f"{cid}: empty spec_text")
            file_key = (case.module_id, case.filename.lower())
            if file_key in review_files:
                problems.append(f"{cid}: file name also used by {review_files[file_key]}")
            review_files[file_key] = cid
            if case.is_clean and case.defects:
                problems.append(f"{cid}: a clean case carries defects")
            if not case.is_clean and not case.defects:
                problems.append(f"{cid}: a case with no defects must be marked clean")
            for item in (*case.defects, *case.traps):
                if not item.match_any or any(not group or not all(t.strip() for t in group) for group in item.match_any):
                    problems.append(f"{cid}: '{item.label[:40]}' has an empty match group")
            for d in case.defects:
                if d.severity.strip().upper() not in SEVERITIES:
                    problems.append(f"{cid}: defect severity {d.severity!r} is not a severity")
            if case.split == SPLIT_HELD_OUT and not case.sources:
                problems.append(f"{cid}: a held-out review case needs a source for its labels")

    for split in SPLITS:
        covered = {d for c in cases if c.split == split for d in c.dimensions}
        for dim in sorted(REQUIRED_DIMENSIONS - covered):
            problems.append(f"dimension {dim!r} has no {split} case")

    held_v = verification_cases(cases, split=SPLIT_HELD_OUT)
    held_r = review_cases(cases, split=SPLIT_HELD_OUT)
    if len(held_v) < MIN_HELD_OUT_VERIFICATION_CASES:
        problems.append(
            f"only {len(held_v)} held-out verification cases (minimum {MIN_HELD_OUT_VERIFICATION_CASES})"
        )
    if len(held_r) < MIN_HELD_OUT_REVIEW_CASES:
        problems.append(f"only {len(held_r)} held-out review cases (minimum {MIN_HELD_OUT_REVIEW_CASES})")
    severe = sum(len(c.severe_defects) for c in held_r)
    if severe < MIN_HELD_OUT_SEVERE_REVIEW_DEFECTS:
        problems.append(
            f"only {severe} severe held-out review defects (minimum {MIN_HELD_OUT_SEVERE_REVIEW_DEFECTS})"
        )
    return problems


def summarize(cases: Iterable[EvalCase] | None = None) -> dict[str, Any]:
    """Counts and digests, as the decision record reports them."""
    cases = list(load_dataset() if cases is None else cases)
    out: dict[str, Any] = {
        "dataset_version": DATASET_VERSION,
        "dataset_sha256": dataset_digest(cases),
        "split_sha256": {s: dataset_digest(cases, split=s) for s in SPLITS},
        "cases": {},
        "dimensions": {},
    }
    for split in SPLITS:
        v = verification_cases(cases, split=split)
        r = review_cases(cases, split=split)
        out["cases"][split] = {
            "verification": len(v),
            "verification_severe": sum(1 for c in v if c.severe),
            "verification_by_expected_verdict": {
                verdict: sum(1 for c in v if c.expected_verdict == verdict) for verdict in VERDICTS
            },
            "review": len(r),
            "review_clean": sum(1 for c in r if c.is_clean),
            "review_defects": sum(len(c.defects) for c in r),
            "review_severe_defects": sum(len(c.severe_defects) for c in r),
            "review_traps": sum(len(c.traps) for c in r),
        }
    for dim in sorted(REQUIRED_DIMENSIONS):
        out["dimensions"][dim] = {
            split: sum(1 for c in cases if c.split == split and dim in c.dimensions) for split in SPLITS
        }
    out["excluded"] = {
        **{f"oracle_ledger:{k}": v for k, v in LEDGER_EXCLUSIONS.items()},
        **{f"dc_scenario:{k}": v for k, v in DC_SCENARIO_EXCLUSIONS.items()},
    }
    return out
