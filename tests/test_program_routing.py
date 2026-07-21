"""Focused tests for hyperscale program and per-spec routing primitives."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.gui import review_run_controller
from src.programs import (
    DATACENTER_ARCHITECTURE_MODULE_ID,
    DATACENTER_ELECTRICAL_MODULE_ID,
    DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
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
        DATACENTER_ELECTRICAL_MODULE_ID,
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    )
    assert program.implemented_module_ids == (
        DATACENTER_FIRE_MODULE_ID,
        DATACENTER_ARCHITECTURE_MODULE_ID,
        DATACENTER_ELECTRICAL_MODULE_ID,
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
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
    ],
)
def test_division_21_routes_to_fire_suppression_module(
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


@pytest.mark.parametrize(
    "section_number,section_title",
    [
        ("28-31-00", "Fire Detection and Alarm"),
        ("28 46 00", "Fire Detection and Alarm"),
    ],
)
def test_fire_alarm_csi_families_route_to_electronic_safety_module(
    section_number: str, section_title: str
) -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="fire-alarm.docx",
            section_number=section_number,
            section_title=section_title,
            content="Provide the complete system.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    )
    assert decision.confidence >= 0.95
    assert any(
        item.source is RoutingEvidenceSource.CSI_SECTION
        and item.module_id == DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID
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
    "section_number,section_title",
    [
        ("26 05 00", "Common Work Results for Electrical"),
        ("33 71 00", "Electrical Utility Transmission and Distribution"),
        ("48 11 00", "Fossil Fuel Electrical Power Generation"),
    ],
)
def test_electrical_csi_families_route_to_electrical_module(
    section_number: str, section_title: str
) -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="electrical.docx",
            section_number=section_number,
            section_title=section_title,
            content="Provide the complete system.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_ELECTRICAL_MODULE_ID,)
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


def test_non_alarm_division_28_does_not_route_to_an_implemented_module() -> None:
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


@pytest.mark.parametrize(
    "section_number,section_title",
    [
        ("28 31 00", "Intrusion Detection"),
        ("28 46 00", "Access Control"),
    ],
)
def test_mapped_alarm_family_with_non_alarm_title_is_unsupported(
    section_number: str,
    section_title: str,
) -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id=f"{section_number}.docx",
            section_number=section_number,
            section_title=section_title,
            content="Provide the complete security system.",
        )
    )

    assert decision.state is RoutingState.UNSUPPORTED
    assert decision.module_ids == ()
    assert decision.candidate_module_ids == ()
    assert decision.confidence == 0.95
    assert any(
        item.module_id is None and "outside" in item.detail
        for item in decision.evidence
    )


@pytest.mark.parametrize("section_number", ["28 46 00", ""])
def test_mixed_alarm_and_non_alarm_title_requires_confirmation(
    section_number: str,
) -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="28 46 00.docx",
            section_number=section_number,
            section_title="Access Control and Fire Alarm Systems",
            content="Provide both systems.",
        )
    )

    assert decision.state is RoutingState.AMBIGUOUS
    assert decision.candidate_module_ids == (
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    )
    assert decision.confidence == 0.50


def test_uncorroborated_legacy_28_31_requires_confirmation() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="28 31 00.docx",
            section_number="28 31 00",
            section_title="General System Requirements",
            content="Provide the complete system.",
        )
    )

    assert decision.state is RoutingState.AMBIGUOUS
    assert decision.candidate_module_ids == (
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    )
    assert any(
        item.module_id is None and "corroborating" in item.detail
        for item in decision.evidence
    )


def test_single_candidate_confirmation_can_skip_only_that_spec(monkeypatch) -> None:
    filename = "28 31 00.docx"
    supported_filename = "26 05 00 Electrical.docx"
    app = SimpleNamespace(
        _selected_program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
        _extracted_specs=[
            SimpleNamespace(
                filename=filename,
                section_number="28 31 00",
                section_title="General System Requirements",
                content="Provide the complete system.",
            ),
            SimpleNamespace(
                filename=supported_filename,
                section_number="26 05 00",
                section_title="Common Work Results for Electrical",
                content="Provide the complete electrical system.",
            ),
        ],
        log=SimpleNamespace(
            log=lambda *args, **kwargs: None,
            log_error=lambda *args: None,
        ),
    )
    prompts: list[tuple[str, str]] = []
    answers = iter((False, True, True))

    def _answer(title: str, message: str) -> bool:
        prompts.append((title, message))
        return next(answers)

    monkeypatch.setattr(review_run_controller.messagebox, "askyesno", _answer)

    assignments = review_run_controller._build_program_assignments(
        app,
        [Path("C:/specs") / filename, Path("C:/specs") / supported_filename],
    )

    assert assignments is not None
    assert len(assignments) == 2
    assert assignments[0].state is RoutingState.UNSUPPORTED
    assert assignments[0].decision.is_user_overridden is True
    assert assignments[0].module_ids == ()
    assert assignments[0].decision.user_override is not None
    assert assignments[0].decision.user_override.reason.startswith("Skipped")
    assert assignments[1].module_ids == (DATACENTER_ELECTRICAL_MODULE_ID,)
    assert [title for title, _ in prompts] == [
        "Confirm specification routing",
        "Unsupported specifications",
        "Confirm hyperscale routing",
    ]
    assert "skip this specification" in prompts[0][1]


@pytest.mark.parametrize("section_number", ["27 15 00", "28 13 00"])
def test_unimplemented_low_voltage_division_cannot_auto_route_from_title(
    section_number: str,
) -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="low-voltage.docx",
            section_number=section_number,
            section_title="Electrical Power Monitoring and Access Control",
            content="Provide an EPMS interface.",
        )
    )

    assert decision.state is RoutingState.AMBIGUOUS
    assert decision.module_ids == ()
    assert decision.candidate_module_ids == (DATACENTER_ELECTRICAL_MODULE_ID,)


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


def test_content_only_fire_alarm_route_requires_several_independent_signals() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="unlabeled-fire-alarm.docx",
            content=(
                "Comply with NFPA 72 for the fire alarm system. Provide the FACP, "
                "signaling-line circuits, notification appliance circuits, and "
                "initiating devices."
            ),
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    )


def test_division_26_with_single_fire_cross_reference_stays_electrical() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="26 05 00.docx",
            section_number="26 05 00",
            section_title="Common Work Results for Electrical",
            content="Coordinate equipment shutdown with the fire alarm system.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_ELECTRICAL_MODULE_ID,)
    assert decision.confidence >= 0.95


def test_division_26_with_explicit_fire_alarm_title_requires_confirmation() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="26 50 00.docx",
            section_number="26 50 00",
            section_title="Fire Alarm Systems",
            content="Provide the complete system.",
        )
    )

    assert decision.state is RoutingState.AMBIGUOUS
    assert decision.module_ids == ()
    assert decision.candidate_module_ids == (
        DATACENTER_ELECTRICAL_MODULE_ID,
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    )
    assert decision.confidence == 0.50


def test_content_only_electrical_route_requires_several_independent_signals() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="unlabeled-electrical.docx",
            content=(
                "Comply with NFPA 70 and IEEE 1584. Provide switchgear with SCCR "
                "ratings, selective coordination, and an arc-flash study."
            ),
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_ELECTRICAL_MODULE_ID,)


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
            section_title="Architectural Door Hardware and Fire Alarm Systems",
            content="Combined owner specification.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (
        DATACENTER_ARCHITECTURE_MODULE_ID,
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    )


def test_fire_alarm_interface_phrase_does_not_claim_an_architectural_spec() -> None:
    decision = route_spec(
        SpecRoutingInput(
            spec_id="interfaces.docx",
            section_title="Architectural Door Hardware and Fire Alarm Interfaces",
            content="Coordinate interface requirements.",
        )
    )

    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_ARCHITECTURE_MODULE_ID,)
    assert all(
        item.module_id != DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID
        for item in decision.evidence
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
            ("future_datacenter_structural",),
            reason="Not a valid program member.",
        )


def test_route_specs_preserves_input_order_and_zero_to_many_cardinality() -> None:
    decisions = route_specs(
        (
            SpecRoutingInput(
                spec_id="unsupported.docx",
                section_number="27 15 00",
                section_title="Communications Horizontal Cabling",
            ),
            SpecRoutingInput(
                spec_id="fire.docx",
                section_number="21 13 13",
                section_title="Sprinklers",
            ),
            SpecRoutingInput(
                spec_id="both.docx",
                section_title="Architectural Doors and Fire Alarm Systems",
            ),
            SpecRoutingInput(
                spec_id="electrical.docx",
                section_number="26 05 00",
                section_title="Electrical Common Work",
            ),
        )
    )

    assert tuple(decision.spec_id for decision in decisions) == (
        "unsupported.docx",
        "fire.docx",
        "both.docx",
        "electrical.docx",
    )
    assert tuple(len(decision.module_ids) for decision in decisions) == (0, 1, 2, 1)


def test_labeled_compact_csi_is_accepted_in_titles() -> None:
    """E8: ``SECTION 072726`` — the explicit SECTION label makes the compact
    six digits credible CSI metadata even in a title/filename."""
    decision = route_spec(
        SpecRoutingInput(
            spec_id="SECTION 072726 Air Barriers.docx",
            section_title="SECTION 072726 - Membrane Air Barriers",
            content="Provide complete air-barrier work.",
        )
    )
    assert decision.state is RoutingState.SUPPORTED
    assert decision.module_ids == (DATACENTER_ARCHITECTURE_MODULE_ID,)
    assert any(
        item.source is RoutingEvidenceSource.SECTION_TITLE
        and item.signal == "07 27 26"
        for item in decision.evidence
    )


def test_bare_compact_numeric_in_title_stays_rejected() -> None:
    """The SECTION label is load-bearing: a bare compact numeric in a title
    is still not CSI metadata."""
    decision = route_spec(
        SpecRoutingInput(
            spec_id="072726 Project Reference.docx",
            section_title="072726 Project Reference",
            content="General project requirements.",
        )
    )
    assert decision.state is RoutingState.UNSUPPORTED
    assert decision.evidence == ()
