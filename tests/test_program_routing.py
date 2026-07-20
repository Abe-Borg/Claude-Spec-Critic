"""Focused tests for hyperscale program and per-spec routing primitives."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.programs import (
    DATACENTER_ARCHITECTURE_MODULE_ID,
    DATACENTER_FIRE_MODULE_ID,
    HYPERSCALE_DATACENTER_PROGRAM,
    ProgramDefinition,
    RoutingEvidenceSource,
    RoutingState,
    SpecRoutingInput,
    apply_user_override,
    assignments_for_specs,
    remove_user_override,
    route_spec,
    route_specs,
)


def test_hyperscale_program_models_current_and_future_modules() -> None:
    program = HYPERSCALE_DATACENTER_PROGRAM

    assert program.module_ids == (
        DATACENTER_FIRE_MODULE_ID,
        DATACENTER_ARCHITECTURE_MODULE_ID,
    )
    assert program.implemented_module_ids == (
        DATACENTER_FIRE_MODULE_ID,
        DATACENTER_ARCHITECTURE_MODULE_ID,
    )
    assert program.planned_module_ids == ()


def test_program_rejects_planned_module_outside_membership() -> None:
    with pytest.raises(ValueError, match="unknown: future_module"):
        ProgramDefinition(
            program_id="test",
            display_name="Test",
            description="Test program",
            module_ids=("current_module",),
            planned_module_ids=("future_module",),
        )


@pytest.mark.parametrize(
    "section_number,section_title",
    [
        ("SECTION 21 13 13", "Wet-Pipe Sprinkler Systems"),
        ("213113", "Electric-Drive Fire Pumps"),
        ("28-31-00", "Fire Detection and Alarm"),
    ],
)
def test_fire_csi_families_route_to_existing_fire_module(
    section_number: str, section_title: str
) -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="fire.docx",
            section_number=section_number,
            section_title=section_title,
            content="Provide the complete system.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_FIRE_MODULE_ID,)
    assert decision.confidence >= 0.95
    assert any(
        item.source is RoutingEvidenceSource.CSI_SECTION
        and item.module_id == DATACENTER_FIRE_MODULE_ID
        for item in decision.evidence
    )


def test_architectural_csi_family_routes_to_architecture_module() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="07 27 26.docx",
            section_number="07 27 26",
            section_title="Fluid-Applied Membrane Air Barriers",
            content="Seal transitions at the curtain wall and roof membrane.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_ARCHITECTURE_MODULE_ID,)
    assert decision.confidence >= 0.95


@pytest.mark.parametrize(
    "filename",
    [
        "NFPA 13 Reference Criteria.docx",
        "Campus Project 07 27 26 Issue 2026-07-20.docx",
        "Project 09 - Revision 2026-07-20.docx",
        "2026-07-20 Project 13 Coordination Notes.docx",
    ],
)
def test_numeric_filename_substrings_are_not_csi_metadata(filename: str) -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id=filename,
            # Exercise both metadata inputs because legacy callers passed a
            # filename through both fields.
            section_number=filename,
            section_title=filename,
            content="General project requirements.",
        )
    )

    assert decision.state is RoutingState.UNSUPPORTED
    assert decision.module_ids == ()
    assert decision.evidence == ()


def test_nfpa_number_in_filename_cannot_override_fire_title_signal() -> None:
    filename = "NFPA 13 Fire Sprinklers.docx"
    decision = route_spec(
        SpecRoutingInput(
            spec_id=filename,
            section_number=filename,
            section_title=filename,
            content="Provide the complete sprinkler system.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_FIRE_MODULE_ID,)
    assert all(
        item.module_id != DATACENTER_ARCHITECTURE_MODULE_ID
        for item in decision.evidence
    )


def test_title_accepts_leading_separated_or_explicit_labeled_csi_metadata() -> None:
    architecture = route_spec(
        SpecRoutingInput(
            spec_id="architecture.docx",
            section_title="07-27-26 - Fluid-Applied Membrane Air Barriers",
            content="Provide complete work.",
        )
    )
    fire = route_spec(
        SpecRoutingInput(
            spec_id="fire.docx",
            section_title=(
                "Project Manual - SECTION 21 13 13 - General System Requirements"
            ),
            content="Provide complete work.",
        )
    )

    assert architecture.state is RoutingState.SUPPORTED
    assert architecture.module_ids == (DATACENTER_ARCHITECTURE_MODULE_ID,)
    assert any(
        item.source is RoutingEvidenceSource.SECTION_TITLE
        and item.signal == "07 27 26"
        for item in architecture.evidence
    )
    assert fire.state is RoutingState.SUPPORTED
    assert fire.module_ids == (DATACENTER_FIRE_MODULE_ID,)
    assert any(
        item.source is RoutingEvidenceSource.SECTION_TITLE
        and item.signal == "21 13 13"
        for item in fire.evidence
    )


def test_compact_csi_is_accepted_only_as_dedicated_section_number() -> None:
    title_only = route_spec(
        SpecRoutingInput(
            spec_id="072726 Project Reference.docx",
            section_title="072726 Project Reference.docx",
            content="General project requirements.",
        )
    )
    dedicated = route_spec(
        SpecRoutingInput(
            spec_id="architecture.docx",
            section_number="072726",
            section_title="General System Requirements",
            content="Provide complete work.",
        )
    )

    assert title_only.state is RoutingState.UNSUPPORTED
    assert title_only.evidence == ()
    assert dedicated.state is RoutingState.SUPPORTED
    assert dedicated.module_ids == (DATACENTER_ARCHITECTURE_MODULE_ID,)
    assert any(
        item.source is RoutingEvidenceSource.CSI_SECTION
        and item.signal == "07 27 26"
        for item in dedicated.evidence
    )


def test_assignments_for_specs_does_not_grant_filenames_section_authority() -> None:
    specs = (
        SimpleNamespace(
            filename="NFPA 13 Reference Criteria.docx",
            content="General project requirements.",
        ),
        SimpleNamespace(
            filename="Campus Project 07 27 26 Issue 2026-07-20.docx",
            content="General project requirements.",
        ),
        SimpleNamespace(
            filename="072726 Project Reference.docx",
            content="General project requirements.",
        ),
        SimpleNamespace(
            filename="07 27 26 - Membrane Air Barriers.docx",
            content="Provide complete work.",
        ),
        SimpleNamespace(
            filename="Fire Package.docx",
            section_number="211313",
            section_title="General System Requirements",
            content="Provide complete work.",
        ),
    )
    source_paths = tuple(f"C:/specs/{spec.filename}" for spec in specs)

    assignments = assignments_for_specs(
        specs,
        source_paths,
        program=HYPERSCALE_DATACENTER_PROGRAM,
    )

    assert tuple(assignment.state for assignment in assignments) == (
        RoutingState.UNSUPPORTED,
        RoutingState.UNSUPPORTED,
        RoutingState.UNSUPPORTED,
        RoutingState.SUPPORTED,
        RoutingState.SUPPORTED,
    )
    assert tuple(assignment.module_ids for assignment in assignments) == (
        (),
        (),
        (),
        (DATACENTER_ARCHITECTURE_MODULE_ID,),
        (DATACENTER_FIRE_MODULE_ID,),
    )
    assert tuple(assignment.source_path for assignment in assignments) == source_paths


def test_non_fire_division_28_does_not_route_as_fire_or_electrical() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="28 13 00.docx",
            section_number="28 13 00",
            section_title="Access Control",
            content="Provide card readers and credential management software.",
        )
    )

    assert decision.state is RoutingState.UNSUPPORTED
    assert decision.module_ids == ()
    assert decision.automatic_module_ids == ()
    assert all("electrical" not in module_id for module_id in decision.module_ids)


def test_content_only_route_requires_several_independent_signals() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="unlabeled-fire.docx",
            content=(
                "Design sprinklers to NFPA 13. Coordinate the standpipe and "
                "electric-drive fire pump."
            ),
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_FIRE_MODULE_ID,)
    assert any(
        item.source is RoutingEvidenceSource.CONTENT
        and "NFPA 13" in item.signal
        for item in decision.evidence
    )


def test_single_cross_discipline_content_reference_is_ambiguous_not_executable() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="26 05 00.docx",
            section_number="26 05 00",
            section_title="Common Work Results for Electrical",
            content="Coordinate equipment shutdown with the fire alarm system.",
        )
    )

    assert decision.state is RoutingState.AMBIGUOUS
    assert decision.module_ids == ()
    assert decision.candidate_module_ids == (DATACENTER_FIRE_MODULE_ID,)
    assert decision.confidence <= 0.69


def test_conflicting_csi_and_title_are_ambiguous_with_both_candidates() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="mislabeled.docx",
            section_number="09 91 00",
            section_title="Automatic Sprinkler Systems",
            content="Provide complete work.",
        )
    )

    assert decision.state is RoutingState.AMBIGUOUS
    assert decision.module_ids == ()
    assert decision.candidate_module_ids == (
        DATACENTER_FIRE_MODULE_ID,
        DATACENTER_ARCHITECTURE_MODULE_ID,
    )
    assert decision.confidence == 0.50


def test_title_can_deliberately_route_one_spec_to_multiple_modules() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="combined.docx",
            section_title="Architectural Door Hardware and Fire Alarm Interfaces",
            content="Combined owner specification.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (
        DATACENTER_FIRE_MODULE_ID,
        DATACENTER_ARCHITECTURE_MODULE_ID,
    )


def test_user_override_resolves_ambiguity_without_losing_automatic_evidence() -> None:
    automatic = route_spec(
        SpecRoutingInput(
            spec_id="mislabeled.docx",
            section_number="09 91 00",
            section_title="Automatic Sprinkler Systems",
        )
    )
    overridden = apply_user_override(
        automatic,
        (DATACENTER_ARCHITECTURE_MODULE_ID,),
        reason="The architect confirmed this is an architectural package.",
    )

    assert overridden.state is RoutingState.SUPPORTED
    assert overridden.module_ids == (DATACENTER_ARCHITECTURE_MODULE_ID,)
    assert overridden.is_user_overridden is True
    assert overridden.automatic_state is RoutingState.AMBIGUOUS
    assert overridden.automatic_module_ids == automatic.automatic_module_ids
    assert overridden.evidence == automatic.evidence
    assert overridden.confidence == automatic.confidence

    restored = remove_user_override(overridden)
    assert restored.state is RoutingState.AMBIGUOUS
    assert restored.module_ids == ()
    assert restored.is_user_overridden is False


def test_empty_user_override_explicitly_routes_to_no_modules() -> None:
    automatic = route_spec(
        SpecRoutingInput(
            spec_id="21 13 13.docx",
            section_number="21 13 13",
            section_title="Wet-Pipe Sprinklers",
        )
    )
    overridden = apply_user_override(
        automatic,
        (),
        reason="This file is reference-only and should not be reviewed.",
    )

    assert overridden.state is RoutingState.UNSUPPORTED
    assert overridden.module_ids == ()
    assert overridden.automatic_state is RoutingState.SUPPORTED


def test_user_override_rejects_module_outside_program() -> None:
    automatic = route_spec(SpecRoutingInput(spec_id="unknown.docx"))

    with pytest.raises(ValueError, match="outside the program"):
        apply_user_override(
            automatic,
            ("datacenter_electrical",),
            reason="Not a valid program member.",
        )


def test_route_specs_preserves_input_order_and_zero_to_many_cardinality() -> None:
    decisions = route_specs(
        (
            SpecRoutingInput(
                spec_id="unsupported.docx",
                section_number="26 05 00",
                section_title="Electrical Common Work",
            ),
            SpecRoutingInput(
                spec_id="fire.docx",
                section_number="21 13 13",
                section_title="Sprinklers",
            ),
            SpecRoutingInput(
                spec_id="both.docx",
                section_title="Architectural Doors and Fire Alarm Interfaces",
            ),
        )
    )

    assert tuple(decision.spec_id for decision in decisions) == (
        "unsupported.docx",
        "fire.docx",
        "both.docx",
    )
    assert tuple(len(decision.module_ids) for decision in decisions) == (0, 1, 2)
