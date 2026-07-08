"""Behavior pins for the domain-vocabulary routing surfaces.

Phase 0 of the module-extraction refactor. The verification-profile
classifier, the mode router, the local-skip prescreen, the severity search
budgets, and the code-cycle registry are all driven by California-K-12-M&P
vocabulary that later phases move onto a module object. These tables pin the
*current* finding-to-decision mapping so that move is provably behavior-
preserving for the existing California configuration.

Contract for later phases: a diff that relocates keyword lists / registry
wiring MUST keep every case below passing unchanged. A case may only change
in a diff that intentionally changes routing behavior — as the Phase-4
jurisdictional-profile generalization did: ``CALIFORNIA_AHJ`` became
``JURISDICTIONAL`` (value ``jurisdictional``; legacy strings map via
``parse_verification_profile``), with the CA keyword vocabulary moved onto
the module. The finding->profile mappings below are unchanged.

Complements ``test_golden_domain_surfaces.py`` (byte pins of the prompt /
detector output); these tests pin decisions rather than text. Existing suites
already lock adjacent behavior — cross-check chunk assignment lives in
``test_cross_check_chunking.py``; pinned standards editions in
``test_pinned_standards_editions.py``.
"""
from __future__ import annotations

import pytest

from src.core.api_config import (
    DEFAULT_VERIFICATION_MAX_USES,
    web_search_max_uses_for_severity,
)
from src.core.code_cycles import AVAILABLE_CYCLES, CALIFORNIA_2025, DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.verification_modes import (
    VerificationMode,
    select_verification_mode,
)
from src.verification.verification_prescreen import (
    classify_finding_for_verification,
    local_skip_requires_elevated_confidence,
)
from src.verification.verification_profiles import (
    VerificationProfile,
    classify_finding_profile,
)


def _finding(
    *,
    severity: str = "MEDIUM",
    issue: str = "",
    code_reference: str | None = None,
    existing: str | None = None,
    replacement: str | None = None,
    section: str = "",
    filename: str = "23 05 00 - Common Work for HVAC.docx",
    action: str = "REPORT_ONLY",
) -> Finding:
    return Finding(
        severity=severity,
        fileName=filename,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_reference,
    )


# ---------------------------------------------------------------------------
# Profile classification
# ---------------------------------------------------------------------------


_PROFILE_CASES = [
    pytest.param(
        dict(issue="Missing DSA closeout testing requirement for HVAC systems."),
        VerificationProfile.JURISDICTIONAL,
        id="california-keyword-dsa",
    ),
    pytest.param(
        dict(issue="Ventilation rate conflicts with Title 24 requirements."),
        VerificationProfile.JURISDICTIONAL,
        id="california-keyword-title-24",
    ),
    pytest.param(
        dict(
            issue="CBC amendment adopted by DSA differs from the spec language.",
            code_reference="CBC 1616A",
        ),
        VerificationProfile.JURISDICTIONAL,
        id="jurisdictional-precedes-code-standard",
    ),
    pytest.param(
        dict(
            issue="Referenced edition has been withdrawn.",
            code_reference="NFPA 13-2010",
        ),
        VerificationProfile.CODE_STANDARD,
        id="code-standard-via-code-reference",
    ),
    pytest.param(
        dict(issue="Cites a superseded fire code edition."),
        VerificationProfile.CODE_STANDARD,
        id="code-standard-via-keyword",
    ),
    pytest.param(
        dict(
            issue="Basis-of-design model number does not appear in the Greenheck catalog."
        ),
        VerificationProfile.MANUFACTURER,
        id="manufacturer-keywords",
    ),
    pytest.param(
        dict(
            issue="Access clearance above the corridor ceiling is too small for filter replacement."
        ),
        VerificationProfile.CONSTRUCTABILITY,
        id="constructability-default",
    ),
    pytest.param(
        dict(
            issue="Internal contradiction: Title 24 ventilation rates differ between 1.02 and 3.01."
        ),
        VerificationProfile.INTERNAL_COORDINATION,
        id="internal-coordination-precedes-jurisdictional",
    ),
    pytest.param(
        dict(
            issue="Valve label color formatting is inconsistent.",
            code_reference="ASME A13.1",
        ),
        VerificationProfile.INTERNAL_COORDINATION,
        id="internal-coordination-precedes-code-reference",
    ),
    pytest.param(
        dict(issue="LEED reference is inappropriate for this project type."),
        VerificationProfile.INTERNAL_COORDINATION,
        id="internal-coordination-leed",
    ),
]


class TestProfileClassificationPins:
    @pytest.mark.parametrize("kwargs, expected", _PROFILE_CASES)
    def test_profile(self, kwargs, expected):
        assert classify_finding_profile(_finding(**kwargs)) is expected

    def test_none_and_empty_default_to_constructability(self):
        assert classify_finding_profile(None) is VerificationProfile.CONSTRUCTABILITY
        assert (
            classify_finding_profile(_finding(issue=""))
            is VerificationProfile.CONSTRUCTABILITY
        )

    def test_profile_enum_values_are_pinned(self):
        # Serialized into caches / resume state / diagnostics. Updated
        # consciously in Phase 4 (california_ahj -> jurisdictional);
        # parse_verification_profile maps the legacy value on load.
        assert {p.value for p in VerificationProfile} == {
            "code_standard",
            "jurisdictional",
            "manufacturer",
            "constructability",
            "internal_coordination",
        }


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


_MODE_CASES = [
    pytest.param(
        dict(severity="CRITICAL", issue="Missing DSA structural test requirement."),
        dict(),
        VerificationMode.DEEP_REASONING,
        id="critical-jurisdictional-goes-deep",
    ),
    pytest.param(
        dict(severity="CRITICAL", issue="Referenced NFPA 13 edition is withdrawn."),
        dict(),
        VerificationMode.STANDARD_REASONING,
        id="critical-code-standard-stays-standard",
    ),
    pytest.param(
        dict(severity="HIGH", issue="Missing DSA closeout requirement."),
        dict(),
        VerificationMode.STANDARD_REASONING,
        id="high-jurisdictional-stays-standard",
    ),
    pytest.param(
        dict(
            severity="GRIPES",
            issue="Valve label color formatting is inconsistent.",
            code_reference="ASME A13.1",
        ),
        dict(),
        VerificationMode.STRICT_STRUCTURED,
        id="gripes-with-code-reference-strict",
    ),
    pytest.param(
        dict(
            severity="HIGH",
            issue="Internal contradiction between 1.02 and 3.01 hose bibb requirements.",
        ),
        dict(),
        VerificationMode.STRICT_STRUCTURED,
        id="non-gripes-internal-coordination-strict",
    ),
    pytest.param(
        dict(severity="MEDIUM", issue="Duct routing conflicts with joist depth."),
        dict(),
        VerificationMode.STANDARD_REASONING,
        id="default-standard-reasoning",
    ),
    pytest.param(
        dict(severity="GRIPES", issue="Typo: 'seperate' should be 'separate'."),
        dict(local_skip=True),
        VerificationMode.LOCAL_SKIP,
        id="local-skip-wins-outright",
    ),
    pytest.param(
        dict(severity="MEDIUM", issue="Duct routing conflicts with joist depth."),
        dict(escalated=True),
        VerificationMode.DEEP_REASONING,
        id="escalated-forces-deep",
    ),
    pytest.param(
        dict(severity="CRITICAL", issue="Missing DSA structural test requirement."),
        dict(cached_mode="standard_reasoning"),
        VerificationMode.STANDARD_REASONING,
        id="cached-mode-is-preserved",
    ),
    pytest.param(
        dict(severity="CRITICAL", issue="Missing DSA structural test requirement."),
        dict(cached_mode="not-a-real-mode"),
        VerificationMode.DEEP_REASONING,
        id="invalid-cached-mode-falls-through",
    ),
]


class TestModeSelectionPins:
    @pytest.mark.parametrize("finding_kwargs, mode_kwargs, expected", _MODE_CASES)
    def test_mode(self, finding_kwargs, mode_kwargs, expected):
        assert (
            select_verification_mode(_finding(**finding_kwargs), **mode_kwargs)
            is expected
        )

    def test_none_finding_defaults_to_standard(self):
        assert select_verification_mode(None) is VerificationMode.STANDARD_REASONING


# ---------------------------------------------------------------------------
# Local-skip prescreen
# ---------------------------------------------------------------------------


_PRESCREEN_CASES = [
    pytest.param(
        dict(severity="GRIPES", issue="Typo: 'seperate' should be 'separate'."),
        "local_skip",
        False,
        id="gripes-typo-local-skip",
    ),
    pytest.param(
        dict(severity="GRIPES", issue="Placeholder [SELECT] left in paragraph 2.01."),
        "local_skip",
        False,
        id="gripes-placeholder-local-skip",
    ),
    pytest.param(
        dict(
            severity="GRIPES",
            issue="LEED reference is inappropriate for this project type.",
        ),
        "local_skip",
        True,
        id="gripes-leed-elevated-confidence",
    ),
    pytest.param(
        dict(
            severity="GRIPES",
            issue="Internal contradiction between warranty durations.",
        ),
        "local_skip",
        True,
        id="gripes-internal-contradiction-elevated",
    ),
    pytest.param(
        dict(
            severity="GRIPES",
            issue="Placeholder [SELECT] remains in the LEED credits paragraph.",
        ),
        "local_skip",
        False,
        id="regular-keyword-beats-elevated-keyword",
    ),
    pytest.param(
        dict(
            severity="GRIPES",
            issue="Valve label color formatting is inconsistent.",
            code_reference="ASME A13.1",
        ),
        "web_required",
        False,
        id="code-reference-always-web-required",
    ),
    pytest.param(
        dict(
            severity="CRITICAL",
            issue="LEED reference is inappropriate for this project type.",
        ),
        "web_required",
        False,
        id="non-gripes-severity-always-web-required",
    ),
    pytest.param(
        dict(severity="GRIPES", issue="Inconsistent hose bibb spelling."),
        "web_required",
        False,
        id="gripes-without-keyword-web-required",
    ),
]


class TestPrescreenPins:
    @pytest.mark.parametrize("kwargs, expected, elevated", _PRESCREEN_CASES)
    def test_classification_and_elevated_flag(self, kwargs, expected, elevated):
        finding = _finding(**kwargs)
        assert classify_finding_for_verification(finding) == expected
        assert local_skip_requires_elevated_confidence(finding) is elevated


# ---------------------------------------------------------------------------
# Severity search budgets
# ---------------------------------------------------------------------------


class TestSeverityBudgetPins:
    @pytest.mark.parametrize(
        "severity, expected",
        [("CRITICAL", 8), ("HIGH", 7), ("MEDIUM", 5), ("GRIPES", 3)],
    )
    def test_budget(self, severity, expected):
        assert web_search_max_uses_for_severity(severity) == expected

    def test_unknown_severity_falls_back_to_default(self):
        assert (
            web_search_max_uses_for_severity("UNRANKED")
            == DEFAULT_VERIFICATION_MAX_USES
        )
        assert web_search_max_uses_for_severity(None) == DEFAULT_VERIFICATION_MAX_USES


# ---------------------------------------------------------------------------
# Code-cycle registry
# ---------------------------------------------------------------------------


class TestCycleRegistryPins:
    def test_registry_shape(self):
        # Phase 1 generalizes this registry pattern to modules; the existing
        # single-entry California registry must survive that move unchanged.
        assert DEFAULT_CYCLE is CALIFORNIA_2025
        assert AVAILABLE_CYCLES == {"2025": CALIFORNIA_2025}
        assert DEFAULT_CYCLE.label == "2025"
