"""Focused contract tests for the data-center electrical module."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.core.project_profile import ProjectProfile
from src.cross_check.cross_checker import _assign_chunk
from src.input.preprocessor import preprocess_spec
from src.modules import AVAILABLE_MODULES, module_for_cycle, validate_module_registry
from src.modules.datacenter_electrical import (
    DATACENTER_ELECTRICAL,
    DATACENTER_ELECTRICAL_IBC_2024,
)
from src.research.requirements_research import build_dimension_user_message
from src.verification.verification_profiles import (
    VerificationProfile,
    classify_finding_profile,
)


def test_module_is_registry_ready_with_unique_cycle_label() -> None:
    assert DATACENTER_ELECTRICAL.module_id == "datacenter_electrical"
    assert DATACENTER_ELECTRICAL.cycle.label == "dc-electrical-ibc-2024"
    assert AVAILABLE_MODULES["datacenter_electrical"] is DATACENTER_ELECTRICAL
    assert module_for_cycle(DATACENTER_ELECTRICAL.cycle) is DATACENTER_ELECTRICAL

    # Exercises every prompt/example/template/vocabulary contract as well as
    # the registry-wide module-id and cycle-label uniqueness invariants.
    validate_module_registry(AVAILABLE_MODULES.values())


def test_electrical_code_basis_and_verified_standard_editions() -> None:
    module = DATACENTER_ELECTRICAL
    cycle = DATACENTER_ELECTRICAL_IBC_2024

    assert module.cycle is cycle
    assert [code.key for code in cycle.base_codes] == [
        "ibc",
        "ifc",
        "iecc",
        "iebc",
    ]
    assert cycle.primary_code_year == "2024"
    assert cycle.asce7 == "7-22"
    assert cycle.asce7_previous == "7-16"
    assert {standard.name: standard.edition for standard in cycle.standards} == {
        "NFPA 70 (NEC)": "2023",
        "NFPA 110": "2022",
        "NFPA 111": "2022",
        "ASHRAE 90.1": "2022",
        "ASHRAE 90.4": "2022",
        "IEEE 1584": "2018",
        "UL 2200": "2020",
    }
    assert cycle.unverified_standards() == ()
    assert all("https://" in standard.source for standard in cycle.standards)
    assert "errata" in cycle.edition_phrase("IEEE 1584").lower()
    assert "where applicable" in cycle.edition_phrase("UL 2200").lower()
    assert "with Supplement 1" in module.review_categories_template


def test_detector_is_i_code_only_and_does_not_conflate_nec_or_cec_years() -> None:
    vocabulary = DATACENTER_ELECTRICAL.detector_vocabulary

    assert vocabulary.code_abbreviations == ("IBC", "IFC", "IECC", "IEBC")
    assert vocabulary.flag_leed_references is False
    detector_codes = {value.casefold() for value in vocabulary.code_abbreviations}
    assert detector_codes.isdisjoint(
        {"nec", "nfpa 70", "cec", "csa c22.1", "canadian electrical code"}
    )


def test_electrical_profile_and_scope_are_broad_but_discipline_specific() -> None:
    module = DATACENTER_ELECTRICAL
    scope = "\n".join(
        (
            module.reviewer_persona,
            module.review_categories_template,
            module.compliance_persona,
        )
    ).lower()

    assert module.project_profile_enabled is True
    assert module.research_persona
    assert module.compliance_persona
    assert module.compliance_severity_definitions
    assert module.report_title == "Spec Critic — Electrical Specification Review Report"
    assert module.report_context_phrase == (
        "hyperscale data-center electrical projects"
    )
    assert "electrical specification reviewer" in scope
    assert "utility service" in scope
    assert "medium-voltage" in scope
    assert "ups" in scope
    assert "power-system studies" in scope
    assert "fire-suppression specification reviewer" not in scope
    assert "architectural specification reviewer" not in scope


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
    module = DATACENTER_ELECTRICAL
    assert [dimension.dimension_id for dimension in module.research_dimensions] == [
        "governing_codes_certification",
        "utility_service_interconnection",
        "ahj_permitting_emergency_power",
        "client_reliability_commissioning",
        "site_environment_electrical_design",
    ]

    for dimension in module.research_dimensions:
        message = build_dimension_user_message(module, profile, dimension)
        assert message.startswith(f"Project: {expected_location}. Client: ExampleCo.")
        assert "{" not in message
        assert "}" not in message


def test_electrical_verification_keywords_route_domain_claims() -> None:
    module = DATACENTER_ELECTRICAL
    jurisdictional = SimpleNamespace(
        codeReference="",
        issue="The serving utility interconnection approval is not defined.",
        existingText="",
        replacementText="",
        section="",
    )
    standard = SimpleNamespace(
        codeReference="IEEE 1584",
        issue="The arc-flash study method is misstated.",
        existingText="",
        replacementText="",
        section="",
    )
    ordinary_electrical = SimpleNamespace(
        codeReference="",
        issue="Provide a local disconnecting means for necessary maintenance.",
        existingText="",
        replacementText="",
        section="",
    )

    assert classify_finding_profile(
        jurisdictional, keywords=module.profile_keywords
    ) is VerificationProfile.JURISDICTIONAL
    assert classify_finding_profile(
        standard, keywords=module.profile_keywords
    ) is VerificationProfile.CODE_STANDARD
    assert classify_finding_profile(
        ordinary_electrical, keywords=module.profile_keywords
    ) is VerificationProfile.CONSTRUCTABILITY


def test_chunk_map_and_corpus_signals_cover_electrical_document_families() -> None:
    module = DATACENTER_ELECTRICAL
    groups = module.cross_check_chunk_groups

    assert _assign_chunk("01 91 13 General Commissioning.docx", groups) == (
        "procurement_general"
    )
    assert _assign_chunk("21 05 00 Fire Interfaces.docx", groups) == (
        "mechanical_fire_interfaces"
    )
    assert _assign_chunk("26 24 13 Switchboards.docx", groups) == (
        "electrical_technology"
    )
    assert _assign_chunk("33 71 19 Electrical Utility Lines.docx", groups) == (
        "site_utility_generation"
    )
    assert _assign_chunk("99 00 00 Unclassified.docx", groups) == "general"

    corpus = (
        "Refer to the Owner's electrical design criteria and single-line diagram. "
        "Complete the short-circuit study and integrated systems testing."
    )
    matches = [
        pattern
        for pattern in module.corpus_signal_patterns
        if re.search(pattern, corpus, flags=re.IGNORECASE)
    ]
    assert len(matches) >= 4


def test_preprocessor_uses_electrical_vocabulary_without_flagging_nec_year() -> None:
    result = preprocess_spec(
        (
            "The project pursues LEED Gold. Comply with 2018 IBC and 2019 IECC. "
            "Electrical work shall comply with the 2023 NEC."
        ),
        "26 05 00 Common Work Results for Electrical.docx",
        cycle=DATACENTER_ELECTRICAL.cycle,
    )

    assert result.leed_alerts == []
    assert any(alert["found_year"] == "2018" for alert in result.code_cycle_alerts)
    assert any(
        alert["found_year"] == "2019"
        for alert in result.invalid_code_cycle_alerts
    )
    all_cycle_alerts = result.code_cycle_alerts + result.invalid_code_cycle_alerts
    assert all(alert["found_year"] != "2023" for alert in all_cycle_alerts)


@pytest.mark.parametrize(
    ("country", "content", "expected_fragment"),
    [
        (
            "CA",
            "Comply with NFPA 70 (NEC) and provide UL Listed equipment.",
            "US model electrical code",
        ),
        (
            "US",
            "Comply with CSA C22.1, Canadian Electrical Code, and CSA Z462.",
            "Canadian model code",
        ),
    ],
)
def test_wrong_polity_rules_cover_us_and_canada(
    country: str,
    content: str,
    expected_fragment: str,
) -> None:
    result = preprocess_spec(
        content,
        "26 05 00 Common Work Results for Electrical.docx",
        cycle=DATACENTER_ELECTRICAL.cycle,
        profile_country=country,
    )

    assert result.polity_alerts
    assert any(expected_fragment in alert["note"] for alert in result.polity_alerts)


def test_canadian_polity_rule_recognizes_hyphenated_ul_listing_language() -> None:
    result = preprocess_spec(
        "Provide U.L.-Listed equipment.",
        "26 05 00 Common Work Results for Electrical.docx",
        cycle=DATACENTER_ELECTRICAL.cycle,
        profile_country="CA",
    )

    assert any("bare US UL listing" in alert["note"] for alert in result.polity_alerts)


def test_us_polity_rule_distinguishes_near_field_communication_from_fire_code() -> None:
    near_field = preprocess_spec(
        "Provide an NFC-enabled metering interface.",
        "26 09 13 Electrical Power Monitoring.docx",
        cycle=DATACENTER_ELECTRICAL.cycle,
        profile_country="US",
    )
    canadian_code = preprocess_spec(
        "Comply with NFC 2020.",
        "26 05 00 Common Work Results for Electrical.docx",
        cycle=DATACENTER_ELECTRICAL.cycle,
        profile_country="US",
    )

    assert near_field.polity_alerts == []
    assert any(
        "Canadian code vocabulary" in alert["note"]
        for alert in canadian_code.polity_alerts
    )
