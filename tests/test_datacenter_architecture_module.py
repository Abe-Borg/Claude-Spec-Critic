"""Focused contract tests for the data-center architecture module."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.project_profile import ProjectProfile
from src.cross_check.cross_checker import _assign_chunk
from src.input.preprocessor import preprocess_spec
from src.modules import AVAILABLE_MODULES, validate_module_registry
from src.modules.datacenter_architecture import (
    DATACENTER_ARCHITECTURE,
    DATACENTER_ARCHITECTURE_IBC_2024,
)
from src.research.requirements_research import build_dimension_user_message
from src.verification.verification_profiles import (
    VerificationProfile,
    classify_finding_profile,
)


def test_module_is_registry_ready_with_unique_cycle_label() -> None:
    assert DATACENTER_ARCHITECTURE.module_id == "datacenter_architecture"
    assert DATACENTER_ARCHITECTURE.cycle.label == "dc-architecture-ibc-2024"
    assert AVAILABLE_MODULES["datacenter_architecture"] is DATACENTER_ARCHITECTURE

    # The real import-time validator proves every prompt/example/template/
    # vocabulary contract and the registry-unique cycle invariant.
    validate_module_registry(AVAILABLE_MODULES.values())


def test_architecture_code_basis_and_profile_capability() -> None:
    module = DATACENTER_ARCHITECTURE
    cycle = DATACENTER_ARCHITECTURE_IBC_2024

    assert module.cycle is cycle
    assert [code.key for code in cycle.base_codes] == [
        "ibc",
        "ifc",
        "iecc",
        "iebc",
    ]
    assert cycle.primary_code_year == "2024"
    assert cycle.asce7 == "7-22"
    assert cycle.standards
    assert cycle.unverified_standards() == ()
    assert cycle.edition_phrase("ICC A117.1") == "2017 with Supplement 1"
    assert cycle.edition_phrase("ASTM E119") == "20"
    assert cycle.edition_phrase("ASTM E84") == "21a"
    assert "with Supplement 1" in module.review_categories_template

    assert module.project_profile_enabled is True
    assert module.research_persona
    assert module.compliance_persona
    assert module.compliance_severity_definitions
    assert module.report_title == (
        "Spec Critic — Architectural Specification Review Report"
    )
    assert module.report_context_phrase == (
        "hyperscale data-center architectural projects"
    )


def test_architecture_scope_is_broad_but_discipline_specific() -> None:
    module = DATACENTER_ARCHITECTURE
    scope = "\n".join(
        (
            module.reviewer_persona,
            module.review_categories_template,
            module.compliance_persona,
        )
    ).lower()

    assert "architectural" in scope
    assert "accessibility" in scope
    assert "exterior enclosure" in scope
    assert "means of egress" in scope
    assert "security" in scope
    assert "site and civil coordination" in scope
    assert "electrical specification reviewer" not in scope
    assert "fire-suppression specification reviewer" not in scope


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
    module = DATACENTER_ARCHITECTURE
    assert [dimension.dimension_id for dimension in module.research_dimensions] == [
        "governing_codes_accessibility",
        "ahj_planning_permitting",
        "client_architectural_standards",
        "site_climate_enclosure",
    ]

    for dimension in module.research_dimensions:
        message = build_dimension_user_message(module, profile, dimension)
        assert message.startswith(f"Project: {expected_location}. Client: ExampleCo.")
        assert "{" not in message
        assert "}" not in message


def test_architecture_verification_keywords_route_domain_claims() -> None:
    module = DATACENTER_ARCHITECTURE

    jurisdictional = SimpleNamespace(
        codeReference="",
        issue="The zoning approval requires additional facade screening.",
        existingText="",
        replacementText="",
        section="",
    )
    standard = SimpleNamespace(
        codeReference="ICC A117.1",
        issue="The maneuvering clearance is misstated.",
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


def test_architecture_chunk_map_covers_architectural_families() -> None:
    groups = DATACENTER_ARCHITECTURE.cross_check_chunk_groups

    assert _assign_chunk("01 81 13 Sustainable Design Requirements.docx", groups) == (
        "procurement_general"
    )
    assert _assign_chunk("07 27 26 Fluid-Applied Air Barriers.docx", groups) == (
        "structure_enclosure"
    )
    assert _assign_chunk("08 71 00 Door Hardware.docx", groups) == (
        "openings_interiors"
    )
    assert _assign_chunk("32 13 13 Concrete Paving.docx", groups) == "sitework"
    assert _assign_chunk("99 00 00 Unclassified.docx", groups) == "general"


def test_preprocessor_uses_architecture_vocabulary_when_cycle_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The candidate is intentionally not registered by production code in this
    # change. Bind its unique cycle in the reverse lookup for this direct seam
    # test, exactly as registry registration would do.
    from src.modules import registry as registry_module

    module = DATACENTER_ARCHITECTURE
    monkeypatch.setitem(
        registry_module._MODULES_BY_CYCLE_LABEL,
        module.cycle.label,
        module,
    )

    result = preprocess_spec(
        "The project pursues LEED Gold. Comply with 2018 IBC and 2019 IECC.",
        "07 00 00 Enclosure.docx",
        cycle=module.cycle,
    )

    assert result.leed_alerts == []
    assert any(alert["found_year"] == "2018" for alert in result.code_cycle_alerts)
    assert any(
        alert["found_year"] == "2019"
        for alert in result.invalid_code_cycle_alerts
    )


@pytest.mark.parametrize(
    ("country", "content", "expected_fragment"),
    [
        (
            "CA",
            "Comply with ADA and ICC A117.1.",
            "United States federal accessibility law",
        ),
        (
            "US",
            "Design in accordance with the National Building Code of Canada.",
            "Canadian model code",
        ),
    ],
)
def test_wrong_polity_rules_cover_us_and_canada(
    monkeypatch: pytest.MonkeyPatch,
    country: str,
    content: str,
    expected_fragment: str,
) -> None:
    from src.modules import registry as registry_module

    module = DATACENTER_ARCHITECTURE
    monkeypatch.setitem(
        registry_module._MODULES_BY_CYCLE_LABEL,
        module.cycle.label,
        module,
    )

    result = preprocess_spec(
        content,
        "08 00 00 Openings.docx",
        cycle=module.cycle,
        profile_country=country,
    )

    assert result.polity_alerts
    assert any(expected_fragment in alert["note"] for alert in result.polity_alerts)
