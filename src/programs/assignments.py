"""Serializable per-spec assignments used by routed program runs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..input.input_files import basename_key, same_file_key, unique_spec_inputs
from ..input.section_identity import SectionHeading
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
                    "location": item.location,
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
                        location=str(item.get("location") or ""),
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
    """Route extracted specs and retain the exact source path for execution.

    Each spec is bound to exactly one supplied file (plan WP-05). A review
    identifies a spec by its file name, so two different supplied files that
    share a name (compared case-insensitively) are refused before anything
    is routed (``input_files.BasenameCollisionError``, a ``ValueError``),
    whatever order they came in, and so are two specs with one name. A spec
    that records the file it was extracted from (``ExtractedSpec.source_path``)
    must name the supplied file of that name, not another one.

    The router reads each spec's own SECTION heading
    (``ExtractedSpec.section_heading``), its file name, and any metadata a
    richer caller supplies (``section_number`` / ``section_title``), each as
    a surface of its own; see ``routing.route_spec``.
    """

    supplied = unique_spec_inputs(source_paths)
    by_name = {basename_key(path.name): path for path in supplied}
    routing_inputs: list[SpecRoutingInput] = []
    bound: list[str] = []
    seen_names: dict[str, str] = {}
    for spec in specs:
        filename = str(getattr(spec, "filename", "") or "")
        key = basename_key(filename)
        if key in seen_names:
            raise ValueError(
                "Two specifications to route are both named "
                f"{seen_names[key]!r}; a review identifies each specification "
                "by its file name."
            )
        seen_names[key] = filename
        path = by_name.get(key)
        extracted_from = str(getattr(spec, "source_path", "") or "")
        if (
            path is not None
            and extracted_from
            and same_file_key(extracted_from) != same_file_key(path)
        ):
            raise ValueError(
                f"{filename!r} was extracted from {extracted_from}, but the file "
                f"supplied under that name is {path}; reselect the files so each "
                "specification is read from the file it will be reviewed as."
            )
        bound.append(str(path) if path is not None else (extracted_from or filename))
        # Dedicated metadata stays distinct from the file name and the
        # heading. Passing every filename through ``section_number`` once
        # granted compact/arbitrary numbers metadata authority (for example
        # ``NFPA 13`` or a project date); the file name is now its own
        # surface, where a compact number counts only when the document's
        # heading confirms it.
        heading = getattr(spec, "section_heading", None)
        routing_inputs.append(
            SpecRoutingInput(
                spec_id=filename,
                section_number=str(getattr(spec, "section_number", "") or ""),
                section_title=str(getattr(spec, "section_title", "") or ""),
                content=str(getattr(spec, "content", "") or ""),
                filename=filename,
                heading=heading if isinstance(heading, SectionHeading) else None,
            )
        )
    # ``route_specs`` preserves input order, so each decision pairs with the
    # path its spec was bound to above.
    decisions = route_specs(routing_inputs, program=program)
    return tuple(
        SpecAssignment(source_path=source_path, decision=decision)
        for source_path, decision in zip(bound, decisions, strict=True)
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
