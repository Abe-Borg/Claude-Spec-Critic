"""Constructed, closed-world packages for prompt-coverage comparisons.

These fixtures are supplied owner requirements, not researched code claims.
The two splits use different values but share templates; neither is evidence
of generalization to real project packages. No model output supplied the labels.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any

from evals.model_effort_dataset import ExpectedDefect, SPLITS, Trap


@dataclass(frozen=True)
class Spec:
    filename: str
    text: str


@dataclass(frozen=True)
class Requirement:
    item_id: str
    text: str
    grounded: bool = True
    actionability: str = "spec_requirement"


@dataclass(frozen=True)
class PackageCase:
    case_id: str
    split: str
    stage: str
    specs: tuple[Spec, ...]
    rationale: str
    defects: tuple[ExpectedDefect, ...] = ()
    traps: tuple[Trap, ...] = ()
    requirements: tuple[Requirement, ...] = ()
    expected_coverage: dict[str, str] = field(default_factory=dict)
    existing_findings: tuple[dict[str, Any], ...] = ()
    # Fixed partitions isolate wording from token-dependent chunk planning.
    chunks: tuple[tuple[int, ...], ...] = ()
    is_clean: bool = False
    module_id: str = "datacenter_fire"


PROJECT_CONTEXT = (
    "Constructed evaluation project. All owner requirements supplied in the "
    "profile are the entire controlling basis for this exercise. Evaluate "
    "coordination and that supplied basis; no jurisdiction or adopted code "
    "edition is established. Process and unverified items are non-controlling."
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def case_digest(case: PackageCase) -> str:
    return digest(asdict(case))


def load_dataset() -> tuple[PackageCase, ...]:
    cases = []
    for split, tag, voltage, other, duration in (
        ("tuning", "FP-1", "480", "208", "30"),
        ("held_out", "FP-7", "600", "240", "45"),
    ):
        def spec(title: str, text: str) -> Spec:
            return Spec(f"21 {title}.docx", text)

        pump = spec("30 00 Fire Pumps", f"2.01 EQUIPMENT\nA. Fire pump {tag}: {voltage} V, three-phase.\n")
        controls = spec("30 13 Controllers", f"2.01 CONTROLLERS\nA. Controller for {tag}: {other} V, three-phase.\n")
        good_controls = spec("30 13 Controllers", f"2.01 CONTROLLERS\nA. Controller for {tag}: {voltage} V, three-phase.\n")
        voltage_defect = ExpectedDefect("pump_controller_voltage", "HIGH", ((tag.lower(), voltage, other),))
        cases.extend([
            PackageCase(f"{split}.cross.voltage", split, "cross_check", (pump, controls),
                        "Same pump has incompatible supply voltages in two specs.", (voltage_defect,)),
            PackageCase(f"{split}.cross.minor", split, "cross_check", (
                spec("05 00 Common", "1.01 SUBMITTALS\nA. Use the name Pump Room North in all closeout schedules.\n"),
                spec("30 00 Fire Pumps", "1.01 SUBMITTALS\nA. Closeout schedule room name: Pump Room N.\n")),
                "Explicit naming convention drifts across specs; a low-severity issue.",
                (ExpectedDefect("room_name_drift", "LOW", (("pump room north", "pump room n"),)),)),
            PackageCase(f"{split}.cross.clean", split, "cross_check", (pump, good_controls),
                        "Identical explicit equipment values; no coordination issue.", is_clean=True),
            PackageCase(f"{split}.cross.already_identified", split, "cross_check", (pump, controls),
                        "The real voltage conflict has already been reported; do not duplicate it.",
                        traps=(Trap("repeat_identified_voltage", ((tag.lower(), voltage, other),),
                                    "Already identified in the supplied findings.",
                                    ("EDIT", "ADD", "DELETE", "REPORT_ONLY")),),
                        existing_findings=({"severity": "HIGH", "fileName": pump.filename,
                            "section": "2.01", "issue": f"{tag} pump is {voltage} V but its controller is {other} V.",
                            "actionType": "REPORT_ONLY", "existingText": pump.text.strip(),
                            "replacementText": "", "codeReference": "Supplied equipment data",
                            "affected_files": [pump.filename, controls.filename]},), is_clean=True),
        ])
        rid = f"r-{('a' if split == 'tuning' else 'b') * 12}"
        req = Requirement(rid, f"Owner acceptance basis: record a {duration}-minute full-flow fire pump acceptance test.")
        anchor = spec("30 00 Fire Pumps", "3.01 ACCEPTANCE\nA. Record the full-flow fire pump acceptance test results.\n")
        represented = spec("05 00 Common", f"3.01 ACCEPTANCE\nA. Record a {duration}-minute full-flow fire pump acceptance test.\n")
        missing = ExpectedDefect("missing_test_duration", "HIGH", ((rid, "missing"), (rid, "omit"), (rid, "not specif")))
        cases.extend([
            PackageCase(f"{split}.compliance.missing", split, "compliance", (anchor,),
                        "Controlling owner test duration is absent, with a literal insertion anchor.",
                        (missing,), requirements=(req,), expected_coverage={rid: "missing"}),
            PackageCase(f"{split}.compliance.contradicted", split, "compliance", (
                spec("30 00 Fire Pumps", "3.01 ACCEPTANCE\nA. Record a 10-minute full-flow fire pump acceptance test.\n"),),
                "Explicit test duration contradicts the supplied owner basis.",
                (ExpectedDefect("wrong_test_duration", "HIGH", ((rid, "10"),)),),
                requirements=(req,), expected_coverage={rid: "contradicted"}),
            PackageCase(f"{split}.compliance.clean", split, "compliance", (represented,),
                        "Controlling requirement is represented literally.", requirements=(req,),
                        expected_coverage={rid: "represented"}, is_clean=True),
            PackageCase(f"{split}.compliance.chunk_represented", split, "compliance", (
                represented, spec("13 13 Wet Pipe", "1.01 SUMMARY\nA. Provide wet-pipe sprinklers.\n")),
                "A no-anchor subset lacks a requirement represented elsewhere; no package omission.",
                traps=(Trap("subset_absence", ((rid,),), "The package represents the requirement.",
                            ("ADD", "EDIT", "DELETE", "REPORT_ONLY")),),
                requirements=(req,), expected_coverage={rid: "represented"}, chunks=((0,), (1,)), is_clean=True),
            PackageCase(f"{split}.compliance.chunk_missing", split, "compliance", (
                anchor, spec("30 13 Controllers", "3.01 ACCEPTANCE\nA. Record controller operation during the fire pump acceptance test.\n")),
                "Both subsets lack the controlling duration; reconcile anchored additions once.",
                (missing,), requirements=(req,), expected_coverage={rid: "missing"}, chunks=((0,), (1,))),
        ])
        unverified_id = f"r-{('c' if split == 'tuning' else 'd') * 12}"
        process_id = f"r-{('e' if split == 'tuning' else 'f') * 12}"
        cases.append(PackageCase(f"{split}.compliance.non_controlling", split, "compliance", (represented,),
            "Unverified owner preference may justify a confirmation advisory, never a mandatory edit; process advice has no coverage.",
            traps=(Trap("unverified_edit", ((unverified_id,),), "Unverified profile item cannot ground an edit."),
                   Trap("process_edit", ((process_id,),), "Workflow advice cannot ground a spec edit.")),
            requirements=(req, Requirement(unverified_id, "Confirm whether the owner wants a witness from the insurer.", grounded=False),
                          Requirement(process_id, "Schedule an owner coordination meeting before construction.", actionability="process_advisory")),
            expected_coverage={rid: "represented"}, is_clean=True))
    return tuple(cases)


def validate_dataset(cases: tuple[PackageCase, ...] | None = None) -> list[str]:
    cases = load_dataset() if cases is None else cases
    problems = []
    ids = set()
    for case in cases:
        prefix = case.case_id
        if prefix in ids:
            problems.append(f"{prefix}: duplicate case id")
        ids.add(prefix)
        if case.split not in SPLITS or case.stage not in ("cross_check", "compliance"):
            problems.append(f"{prefix}: invalid split/stage")
        if not case.specs or len({s.filename for s in case.specs}) != len(case.specs):
            problems.append(f"{prefix}: missing specs or duplicate filenames")
        partitions = case.chunks or (tuple(range(len(case.specs))),)
        indexes = [i for chunk in partitions for i in chunk]
        if any(not chunk for chunk in partitions) or sorted(indexes) != list(range(len(case.specs))):
            problems.append(f"{prefix}: chunks must partition every spec exactly once")
        if case.stage == "cross_check" and any(len(chunk) < 2 for chunk in partitions):
            problems.append(f"{prefix}: cross-check needs two specs per request")
        controlling = {r.item_id for r in case.requirements if r.grounded and r.actionability != "process_advisory"}
        if case.stage == "compliance" and set(case.expected_coverage) != controlling:
            problems.append(f"{prefix}: coverage labels must equal controlling requirement ids")
        if len({r.item_id for r in case.requirements}) != len(case.requirements):
            problems.append(f"{prefix}: duplicate requirement id")
        if any(v not in {"missing", "represented", "contradicted"} for v in case.expected_coverage.values()):
            problems.append(f"{prefix}: invalid coverage status")
        if case.is_clean and case.defects:
            problems.append(f"{prefix}: clean case has defects")
        if len({d.label for d in case.defects}) != len(case.defects):
            problems.append(f"{prefix}: duplicate defect label")
        if any(not d.match_any or any(not group for group in d.match_any) for d in (*case.defects, *case.traps)):
            problems.append(f"{prefix}: empty matcher")
    return problems
