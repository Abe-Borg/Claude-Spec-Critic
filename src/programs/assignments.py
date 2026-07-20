"""Serializable per-spec assignments used by routed program runs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import (
    ProgramDefinition,
    RoutingEvidence,
    RoutingEvidenceSource,
    RoutingState,
    SpecRoutingDecision,
    SpecRoutingInput,
    UserRoutingOverride,
)
from .routing import route_specs


@dataclass(frozen=True)
class SpecAssignment:
    """One source file and its auditable 0..N module routing decision."""

    source_path: str
    decision: SpecRoutingDecision

    def __post_init__(self) -> None:
        if not isinstance(self.source_path, str) or not self.source_path.strip():
            raise ValueError("source_path must be a non-empty string")

    @property
    def spec_id(self) -> str:
        return self.decision.spec_id

    @property
    def module_ids(self) -> tuple[str, ...]:
        return self.decision.module_ids

    @property
    def state(self) -> RoutingState:
        return self.decision.state

    def to_dict(self) -> dict:
        override = self.decision.user_override
        return {
            "source_path": self.source_path,
            "spec_id": self.decision.spec_id,
            "program_id": self.decision.program_id,
            "automatic_state": self.decision.automatic_state.value,
            "automatic_module_ids": list(self.decision.automatic_module_ids),
            "confidence": self.decision.confidence,
            "evidence": [
                {
                    "source": item.source.value,
                    "signal": item.signal,
                    "detail": item.detail,
                    "module_id": item.module_id,
                    "weight": item.weight,
                }
                for item in self.decision.evidence
            ],
            "user_override": (
                {"module_ids": list(override.module_ids), "reason": override.reason}
                if override is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: object) -> "SpecAssignment":
        if not isinstance(data, dict):
            raise ValueError("assignment must be an object")
        evidence_data = data.get("evidence")
        evidence: list[RoutingEvidence] = []
        if isinstance(evidence_data, list):
            for item in evidence_data:
                if not isinstance(item, dict):
                    continue
                evidence.append(
                    RoutingEvidence(
                        source=RoutingEvidenceSource(str(item.get("source", "content"))),
                        signal=str(item.get("signal", "routing signal")),
                        detail=str(item.get("detail", "routing evidence")),
                        module_id=(
                            str(item["module_id"])
                            if item.get("module_id") is not None
                            else None
                        ),
                        weight=float(item.get("weight", 0.0)),
                    )
                )
        override_data = data.get("user_override")
        override = None
        if isinstance(override_data, dict):
            ids = override_data.get("module_ids")
            override = UserRoutingOverride(
                module_ids=tuple(str(v) for v in ids) if isinstance(ids, list) else (),
                reason=str(override_data.get("reason") or "Restored user override"),
            )
        ids = data.get("automatic_module_ids")
        decision = SpecRoutingDecision(
            spec_id=str(data.get("spec_id") or Path(str(data.get("source_path", ""))).name),
            program_id=str(data.get("program_id") or "hyperscale_datacenter"),
            automatic_state=RoutingState(str(data.get("automatic_state") or "unsupported")),
            automatic_module_ids=(
                tuple(str(v) for v in ids) if isinstance(ids, list) else ()
            ),
            confidence=float(data.get("confidence", 0.0)),
            evidence=tuple(evidence),
            user_override=override,
        )
        return cls(source_path=str(data.get("source_path") or ""), decision=decision)


def assignments_for_specs(
    specs: Iterable,
    source_paths: Iterable[Path | str],
    *,
    program: ProgramDefinition,
) -> tuple[SpecAssignment, ...]:
    """Route extracted specs and retain the exact source path for execution."""

    by_name = {Path(path).name: str(path) for path in source_paths}
    routing_inputs: list[SpecRoutingInput] = []
    ordered_specs = list(specs)
    for spec in ordered_specs:
        filename = str(getattr(spec, "filename", "") or "")
        # Keep dedicated metadata distinct from the filename.  Passing every
        # filename through ``section_number`` previously granted compact/
        # arbitrary numbers metadata authority (for example ``NFPA 13`` or a
        # project date).  ExtractedSpec does not currently expose these two
        # optional fields, so normal runs fall back to the filename as a title;
        # richer callers can supply explicit metadata without losing it.
        section_number = str(getattr(spec, "section_number", "") or "")
        section_title = str(getattr(spec, "section_title", "") or "")
        routing_inputs.append(
            SpecRoutingInput(
                spec_id=filename,
                section_number=section_number,
                # A filename may carry credible leading/explicit CSI metadata,
                # but the router treats it as title text and therefore refuses
                # compact or embedded numeric substrings.
                section_title=section_title or filename,
                content=str(getattr(spec, "content", "") or ""),
            )
        )
    decisions = route_specs(routing_inputs, program=program)
    return tuple(
        SpecAssignment(
            source_path=by_name.get(decision.spec_id, decision.spec_id),
            decision=decision,
        )
        for decision in decisions
    )


def partition_assignments(
    assignments: Iterable[SpecAssignment],
    *,
    program: ProgramDefinition,
) -> dict[str, list[Path]]:
    """Return deterministic module partitions in program declaration order."""

    partitions: dict[str, list[Path]] = {
        module_id: [] for module_id in program.implemented_module_ids
    }
    for assignment in assignments:
        if assignment.decision.program_id != program.program_id:
            raise ValueError(
                f"Assignment for {assignment.spec_id!r} belongs to "
                f"{assignment.decision.program_id!r}, not {program.program_id!r}"
            )
        for module_id in assignment.module_ids:
            if module_id not in partitions:
                raise ValueError(
                    f"Assignment for {assignment.spec_id!r} names unavailable "
                    f"program module {module_id!r}"
                )
            path = Path(assignment.source_path)
            if path not in partitions[module_id]:
                partitions[module_id].append(path)
    return {module_id: paths for module_id, paths in partitions.items() if paths}


def routed_module_ids(
    assignments: Iterable[SpecAssignment], *, program: ProgramDefinition
) -> tuple[str, ...]:
    """Implemented module ids actually selected, in program order."""
    selected = {
        module_id for assignment in assignments for module_id in assignment.module_ids
    }
    unknown = selected - set(program.implemented_module_ids)
    if unknown:
        raise ValueError(
            "Assignments name unavailable program module(s): "
            + ", ".join(sorted(unknown))
        )
    return tuple(
        module_id
        for module_id in program.implemented_module_ids
        if module_id in selected
    )
