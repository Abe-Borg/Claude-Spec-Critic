"""Focused contract tests for the data-center fire-alarm module."""
from __future__ import annotations

import pytest

from src.core.project_profile import ProjectProfile
from src.input.preprocessor import preprocess_spec
from src.modules import AVAILABLE_MODULES, module_for_cycle, validate_module_registry
from src.modules.datacenter_electronic_safety_security import (
    DATACENTER_ELECTRONIC_SAFETY_SECURITY,
    DATACENTER_ELECTRONIC_SAFETY_SECURITY_IBC_2024,
)
from src.research.requirements_research import build_dimension_user_message


def test_module_is_registry_ready_with_unique_cycle_label() -> None:
    module = DATACENTER_ELECTRONIC_SAFETY_SECURITY

    assert module.module_id == "datacenter_electronic_safety_security"
    assert module.cycle.label == "dc-electronic-safety-fire-alarm-ibc-2024"
    assert AVAILABLE_MODULES[module.module_id] is module
    assert module_for_cycle(module.cycle) is module

    # Exercises the module's prompt, example, template, vocabulary, and
    # registry-wide module-id/cycle-label contracts.
    validate_module_registry(AVAILABLE_MODULES.values())


def test_fire_alarm_code_basis_and_exact_core_standard_editions() -> None:
    module = DATACENTER_ELECTRONIC_SAFETY_SECURITY
    cycle = DATACENTER_ELECTRONIC_SAFETY_SECURITY_IBC_2024

    assert module.cycle is cycle
    assert [code.key for code in cycle.base_codes] == ["ibc", "ifc", "iebc"]
    assert cycle.primary_code_year == "2024"
    assert cycle.asce7 == "7-22"
    assert cycle.asce7_previous == "7-16"
    assert [(standard.name, standard.edition) for standard in cycle.standards] == [
        ("NFPA 72", "2022"),
        ("NFPA 70 (NEC)", "2023"),
    ]
    assert cycle.unverified_standards() == ()
    assert all("https://" in standard.source for standard in cycle.standards)


def test_detector_vocabulary_is_limited_to_primary_i_codes() -> None:
    vocabulary = DATACENTER_ELECTRONIC_SAFETY_SECURITY.detector_vocabulary

    assert vocabulary.code_abbreviations == ("IBC", "IFC", "IEBC")
    assert vocabulary.flag_leed_references is False
    detector_codes = {value.casefold() for value in vocabulary.code_abbreviations}
    assert detector_codes.isdisjoint(
        {
            "nfpa 72",
            "nfpa 70",
            "nec",
            "can/ulc-s524",
            "can/ulc-s536",
            "can/ulc-s537",
            "can/ulc-s561",
            "can/ulc-s1001",
        }
    )


def test_preprocessor_flags_stale_i_code_but_not_standard_edition_years() -> None:
    result = preprocess_spec(
        (
            "Comply with 2018 IBC, NFPA 72-2019, NFPA 70-2020, "
            "CAN/ULC-S524:2019, CAN/ULC-S536:2019, and CAN/ULC-S537:2019."
        ),
        "28 46 00 Fire Detection and Alarm.docx",
        cycle=DATACENTER_ELECTRONIC_SAFETY_SECURITY.cycle,
    )

    assert [alert["found_year"] for alert in result.code_cycle_alerts] == ["2018"]
    assert result.invalid_code_cycle_alerts == []


def test_phase_one_scope_is_fire_alarm_specific_and_declares_exclusions() -> None:
    module = DATACENTER_ELECTRONIC_SAFETY_SECURITY
    scope = "\n".join(
        (
            module.description,
            module.reviewer_persona,
            module.review_categories_template,
            module.compliance_persona,
        )
    ).lower()

    assert module.project_profile_enabled is True
    assert "phase 1" in scope
    assert "fire detection and alarm" in scope
    assert "aspirating" in scope
    assert "notification" in scope
    assert "cause-and-effect" in scope
    assert "supervising-station" in scope
    assert "access control" in scope
    assert "video surveillance" in scope
    assert "intrusion detection" in scope
    assert "outside this version's scope" in module.description.lower()


@pytest.mark.parametrize(
    ("profile", "expected_location"),
    [
        (
            ProjectProfile(
                city="Ashburn",
                state_or_province="VA",
                country="US",
                client_name="ExampleCo",
            ),
            "Ashburn, Virginia, USA",
        ),
        (
            ProjectProfile(
                city="Markham",
                state_or_province="ON",
                country="CA",
                client_name="ExampleCo",
            ),
            "Markham, Ontario, Canada",
        ),
    ],
)
def test_every_research_dimension_formats_for_us_and_canada(
    profile: ProjectProfile,
    expected_location: str,
) -> None:
    module = DATACENTER_ELECTRONIC_SAFETY_SECURITY
    assert [dimension.dimension_id for dimension in module.research_dimensions] == [
        "governing_codes_certification",
        "ahj_permitting_monitoring",
        "detection_notification_special_hazards",
        "client_sequences_reliability_commissioning",
        "site_campus_emergency_interfaces",
    ]

    for dimension in module.research_dimensions:
        message = build_dimension_user_message(module, profile, dimension)
        assert message.startswith(f"Project: {expected_location}. Client: ExampleCo.")
        assert "{" not in message
        assert "}" not in message


@pytest.mark.parametrize(
    ("content", "expected_fragment"),
    [
        ("Comply with IBC 2024.", "US code/authority vocabulary"),
        ("Comply with the NEC.", "US model electrical code"),
        ("Provide UL Listed equipment.", "bare US UL listing"),
    ],
)
def test_canadian_polity_rules_flag_us_only_basis_language(
    content: str,
    expected_fragment: str,
) -> None:
    result = preprocess_spec(
        content,
        "28 46 00 Fire Detection and Alarm.docx",
        cycle=DATACENTER_ELECTRONIC_SAFETY_SECURITY.cycle,
        profile_country="CA",
    )

    assert any(expected_fragment in alert["note"] for alert in result.polity_alerts)


def test_canadian_polity_rules_do_not_treat_nfpa_72_as_inherently_wrong() -> None:
    result = preprocess_spec(
        "The fire alarm system shall comply with NFPA 72.",
        "28 46 00 Fire Detection and Alarm.docx",
        cycle=DATACENTER_ELECTRONIC_SAFETY_SECURITY.cycle,
        profile_country="CA",
    )

    assert result.polity_alerts == []


def test_us_polity_rule_flags_can_ulc_fire_alarm_standard() -> None:
    result = preprocess_spec(
        "Install and verify the system in accordance with CAN/ULC-S524.",
        "28 46 00 Fire Detection and Alarm.docx",
        cycle=DATACENTER_ELECTRONIC_SAFETY_SECURITY.cycle,
        profile_country="US",
    )

    assert any(
        "Canadian fire-alarm standard family" in alert["note"]
        for alert in result.polity_alerts
    )


def test_us_polity_rule_does_not_conflate_nfc_enabled_with_canadian_fire_code() -> None:
    result = preprocess_spec(
        "Provide an NFC-enabled fire-alarm service interface.",
        "28 46 00 Fire Detection and Alarm.docx",
        cycle=DATACENTER_ELECTRONIC_SAFETY_SECURITY.cycle,
        profile_country="US",
    )

    assert result.polity_alerts == []
