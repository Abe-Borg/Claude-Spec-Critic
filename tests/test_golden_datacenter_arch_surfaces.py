"""Byte-exact golden pins for the ``datacenter_architectural`` module's surfaces.

Mirrors ``tests/test_golden_datacenter_surfaces.py`` (the fire-module pins) for
the third module: it freezes the assembled reviewer / cross-check / verifier
prompts, the deterministic preprocessor alerts, and the location-aware
research / compliance surfaces for the architectural configuration, so a later
engine change that would silently alter what the model is sent shows up as a
golden diff.

The arch goldens live beside the others under ``tests/goldens/`` with a
``dcarch_`` prefix. The California and fire-module goldens stay byte-identical
— this module touches no engine file — which their own suites prove.

This module is also the first to pin a **zero-pinned-standards cycle**
(``standards=()``), so these goldens are the first artifacts to freeze the
engine's empty-standards renderings: the reviewer system prompt's
``"current editions"`` fallback path (unused here — the arch categories anchor
edition checks to the requirements profile instead of the placeholder), the
omitted pinned-editions clause in the reviewer user message, and the verifier
system prompt with no pinned-editions block. ``TestZeroPinnedStandardsSurfaces``
states those expectations as explicit assertions alongside the byte pins.

Regenerating (only when an output change is intentional)::

    SPEC_CRITIC_UPDATE_GOLDENS=1 pytest tests/test_golden_datacenter_arch_surfaces.py

Hermetic: fixed inputs, no API key, no network. Env-sensitive paths (element-id
rendering) explicitly clear the toggle so the goldens pin the default-enabled
behavior regardless of the ambient environment.
"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from src.cross_check.cross_checker import (
    _build_cross_check_input,
    _cross_system_prompt,
    _get_cross_check_user_message,
)
from src.input.extractor import ExtractedSpec, ParagraphMapping
from src.input.preprocessor import (
    DETERMINISTIC_RULE_DUPLICATE_HEADING,
    DETERMINISTIC_RULE_DUPLICATE_PARAGRAPH,
    DETERMINISTIC_RULE_EMPTY_SECTION,
    DETERMINISTIC_RULE_INCONSISTENT_FILENAME,
    DETERMINISTIC_RULE_INVALID_CODE_CYCLE,
    DETERMINISTIC_RULE_LEED,
    DETERMINISTIC_RULE_PLACEHOLDER,
    DETERMINISTIC_RULE_STALE_ASCE7,
    DETERMINISTIC_RULE_STALE_CODE_CYCLE,
    DETERMINISTIC_RULE_TEMPLATE_MARKER,
    detect_inconsistent_file_naming,
    preprocess_spec,
)
from src.modules.datacenter_architectural import DATACENTER_ARCH_IBC_2024
from src.review.prompts import get_single_spec_user_message, get_system_prompt
from src.review.reviewer import Finding
from src.verification.verifier import (
    _build_verification_prompt,
    _get_verification_system_prompt,
)

# Reuse the golden-file machinery (assert helper + update-env fixture) from the
# California suite — one source of truth for how goldens are compared/written.
from tests.test_golden_domain_surfaces import (  # noqa: E402
    assert_matches_golden,
    default_element_ids,  # noqa: F401 — pytest fixture, imported for use
)

_CYCLE = DATACENTER_ARCH_IBC_2024


# ---------------------------------------------------------------------------
# Shared fixture inputs (fixed constants — never derive from runtime state)
# ---------------------------------------------------------------------------

_SPEC_FILENAME = "08 11 13 - Hollow Metal Doors and Frames.docx"

_PARAGRAPH_MAP = [
    ParagraphMapping(
        body_index=0,
        element_type="paragraph",
        text="1.01 SUMMARY",
        table_index=None,
        row_index=None,
        cell_index=None,
        element_id="p0",
        section_id="1.01 SUMMARY",
    ),
    ParagraphMapping(
        body_index=1,
        element_type="paragraph",
        text="A. Comply with 2015 IBC Chapter 7 for all fire-rated door assemblies.",
        table_index=None,
        row_index=None,
        cell_index=None,
        element_id="p1",
        section_id="1.01 SUMMARY",
    ),
    ParagraphMapping(
        body_index=2,
        element_type="table_cell",
        text="D-101 | 90 min | Steelcraft H-series",
        table_index=0,
        row_index=0,
        cell_index=None,
        element_id="t0r0",
        section_id="1.01 SUMMARY",
    ),
]

_SPEC_CONTENT = "\n\n".join(m.text for m in _PARAGRAPH_MAP)

_PROJECT_CONTEXT = (
    "New hyperscale data-center campus. Governing code and AHJ supplied "
    "separately; pursuing LEED certification."
)

_PRE_DETECTED_ALERTS = [
    {
        "filename": _SPEC_FILENAME,
        "type": "SELECT placeholder",
        "match": "[SELECT FINISH]",
        "context": "Furnish door hardware sets per the hardware schedule [SELECT FINISH] prior to review.",
        "position": 120,
        "deterministic_rule": "placeholder",
    },
    {
        "filename": _SPEC_FILENAME,
        "type": "Stale code cycle reference (2015 vs selected 2024)",
        "match": "2015 IBC",
        "context": "Comply with 2015 IBC Chapter 7 for all fire-rated door assemblies.",
        "position": 60,
        "deterministic_rule": "stale_code_cycle",
    },
]


def _fixture_finding(**overrides) -> Finding:
    base = dict(
        severity="HIGH",
        fileName=_SPEC_FILENAME,
        section="2.02",
        issue="Door schedule assigns 90-minute opening protectives at openings the wall-types section describes as 1-hour partitions.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
        confidence=0.7,
    )
    base.update(overrides)
    return Finding(**base)


# ---------------------------------------------------------------------------
# Reviewer prompts
# ---------------------------------------------------------------------------


class TestReviewerPromptGoldens:
    def test_system_prompt(self):
        assert_matches_golden(
            "dcarch_reviewer_system_prompt.txt", get_system_prompt(_CYCLE)
        )

    def test_user_message_plain(self):
        message = get_single_spec_user_message(
            _SPEC_CONTENT,
            _SPEC_FILENAME,
            "",
            cycle=_CYCLE,
            paragraph_map=None,
            pre_detected_alerts=None,
        )
        assert_matches_golden("dcarch_reviewer_user_message_plain.txt", message)

    def test_user_message_full(self, default_element_ids):
        message = get_single_spec_user_message(
            _SPEC_CONTENT,
            _SPEC_FILENAME,
            _PROJECT_CONTEXT,
            cycle=_CYCLE,
            paragraph_map=_PARAGRAPH_MAP,
            pre_detected_alerts=_PRE_DETECTED_ALERTS,
        )
        assert_matches_golden("dcarch_reviewer_user_message_full.txt", message)


# ---------------------------------------------------------------------------
# Zero-pinned-standards renderings (this module's novel surface)
# ---------------------------------------------------------------------------


class TestZeroPinnedStandardsSurfaces:
    """Explicit assertions for the ``standards=()`` degradations.

    The engine paths are unit-covered elsewhere against synthetic cycles
    (``test_pinned_standards_editions.py``, ``test_cache_standards_fingerprint``)
    — these pin them against the first REAL module that ships zero pins, and
    the byte goldens above freeze the exact renderings.
    """

    def test_pinned_standards_kwarg_falls_back_to_current_editions(self):
        from src.modules.base import code_basis_format_kwargs

        # The fallback literal exists even though the arch categories template
        # deliberately does not use the placeholder (edition checks anchor to
        # the requirements profile instead).
        assert code_basis_format_kwargs(_CYCLE)["pinned_standards"] == "current editions"

    def test_reviewer_user_message_omits_pinned_editions_clause(self):
        message = get_single_spec_user_message(
            _SPEC_CONTENT,
            _SPEC_FILENAME,
            "",
            cycle=_CYCLE,
            paragraph_map=None,
            pre_detected_alerts=None,
        )
        assert "Pinned standard editions" not in message
        assert "Current code basis: IBC 2024, IECC 2024, ASCE 7-22." in message

    def test_verifier_system_prompt_omits_pinned_editions_block(self):
        prompt = _get_verification_system_prompt(_CYCLE, include_verdict_tool=True)
        assert "Pinned standards editions" not in prompt


# ---------------------------------------------------------------------------
# Cross-check prompts
# ---------------------------------------------------------------------------


class TestCrossCheckPromptGoldens:
    def test_system_prompt(self):
        assert_matches_golden(
            "dcarch_cross_check_system_prompt.txt", _cross_system_prompt(_CYCLE)
        )

    def test_user_message(self, default_element_ids):
        specs = [
            ExtractedSpec(
                filename=_SPEC_FILENAME,
                content=_SPEC_CONTENT,
                word_count=42,
                paragraph_map=list(_PARAGRAPH_MAP),
            ),
            ExtractedSpec(
                filename="07 84 13 - Penetration Firestopping.docx",
                content="1.01 SUMMARY\n\nA. Provide penetration firestopping as indicated.",
                word_count=9,
                paragraph_map=None,
            ),
        ]
        prior_findings = [
            _fixture_finding(finding_id="rf-0123456789ab"),
            _fixture_finding(
                section="",
                issue="Firestop system schedule references wall types absent from the wall-types section.",
                fileName="07 84 13 - Penetration Firestopping.docx",
            ),
        ]
        spec_input = _build_cross_check_input(specs, prior_findings)
        message = _get_cross_check_user_message(
            spec_input, len(specs), "Hyperscale data-center new-build program."
        )
        assert_matches_golden("dcarch_cross_check_user_message.txt", message)


# ---------------------------------------------------------------------------
# Verifier prompts
# ---------------------------------------------------------------------------


class TestVerifierPromptGoldens:
    def test_system_prompt_with_verdict_tool(self):
        assert_matches_golden(
            "dcarch_verifier_system_prompt_with_verdict_tool.txt",
            _get_verification_system_prompt(_CYCLE, include_verdict_tool=True),
        )

    def test_system_prompt_without_verdict_tool(self):
        assert_matches_golden(
            "dcarch_verifier_system_prompt_without_verdict_tool.txt",
            _get_verification_system_prompt(_CYCLE, include_verdict_tool=False),
        )

    def test_user_prompt_with_verdict_tool(self):
        finding = _fixture_finding(
            severity="CRITICAL",
            section="1.03",
            issue="Spec cites a stale IBC edition for the current project.",
            actionType="EDIT",
            existingText="Comply with 2015 IBC Chapter 7.",
            replacementText="Comply with the current adopted IBC edition.",
            codeReference="IBC Chapter 7",
            confidence=0.9,
        )
        assert_matches_golden(
            "dcarch_verifier_user_prompt_with_verdict_tool.txt",
            _build_verification_prompt(
                finding, cycle=_CYCLE, include_verdict_tool=True
            ),
        )

    def test_user_prompt_without_verdict_tool(self):
        assert_matches_golden(
            "dcarch_verifier_user_prompt_without_verdict_tool.txt",
            _build_verification_prompt(
                _fixture_finding(), cycle=_CYCLE, include_verdict_tool=False
            ),
        )


# ---------------------------------------------------------------------------
# Deterministic preprocessor output
# ---------------------------------------------------------------------------

# One arch fixture spec exercising the I-code detectors and the module-specific
# behaviors: stale "2018 IBC", invalid "2019 IBC", long-form "2015
# International Building Code" (shared pattern) AND "2015 International Energy
# Conservation Code" (the module-local pattern), ASCE 7-10, a LEED mention that
# must NOT alert (flag_leed_references=False), and generic (jurisdiction-free)
# invalid wording. Paragraphs are separated by blank lines; no body paragraph
# starts with a bare number (it would be misread as a heading).
_PREPROCESS_FIXTURE_TEXT = """\
1.01 SUMMARY

A. Provide fire-rated door assemblies per 2018 IBC Chapter 7 and anchor curtain-wall framing per ASCE 7-10.

B. The building was previously permitted under the 2021 IBC.

C. Comply with 2019 IBC requirements for fire-rated glazing.

D. Provide protection consistent with the 2015 International Building Code where referenced.

E. Envelope insulation shall comply with the 2015 International Energy Conservation Code.

F. Contractor shall document LEED-NC credits and USGBC checklists where applicable.

G. Furnish door hardware sets per the hardware schedule [SELECT FINISH] prior to submittal review.

H. TODO: confirm frame elevations against the door schedule.

Provide fire-rated joint systems at all head-of-wall conditions in accordance with the approved details and project drawings.

1.02 REFERENCES

A. Reference standards apply as listed.

1.02 REFERENCES

A. Reference standards apply as listed.

2.01 PRODUCTS

2.02 WARRANTY

Provide fire-rated joint systems at all head-of-wall conditions in accordance with the approved details and project drawings.
"""

_PROJECT_FILENAMES = [
    _SPEC_FILENAME,
    "08 71 00 - Door Hardware.docx",
    "07-84-13-Penetration-Firestopping.docx",
]


class TestPreprocessorGolden:
    def _alert_payload(self) -> dict:
        result = preprocess_spec(
            _PREPROCESS_FIXTURE_TEXT, _SPEC_FILENAME, cycle=_CYCLE
        )
        payload = asdict(result)
        payload["inconsistent_file_naming_alerts"] = detect_inconsistent_file_naming(
            list(_PROJECT_FILENAMES)
        )
        return payload

    def test_alerts_golden(self):
        serialized = (
            json.dumps(self._alert_payload(), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        )
        assert_matches_golden("dcarch_preprocessor_alerts.json", serialized)

    def test_leed_reference_is_not_flagged(self):
        """LEED is in-scope for data centers — the detector must stay silent."""
        payload = self._alert_payload()
        fired = {
            str(alert.get("deterministic_rule"))
            for alerts in payload.values()
            for alert in alerts
        }
        assert DETERMINISTIC_RULE_LEED not in fired
        assert payload["leed_alerts"] == []

    def test_expected_rules_fire(self):
        """Every non-LEED deterministic rule the fixture exercises fires."""
        payload = self._alert_payload()
        fired = {
            str(alert.get("deterministic_rule"))
            for alerts in payload.values()
            for alert in alerts
        }
        expected = {
            DETERMINISTIC_RULE_PLACEHOLDER,
            DETERMINISTIC_RULE_TEMPLATE_MARKER,
            DETERMINISTIC_RULE_STALE_CODE_CYCLE,
            DETERMINISTIC_RULE_STALE_ASCE7,
            DETERMINISTIC_RULE_INVALID_CODE_CYCLE,
            DETERMINISTIC_RULE_EMPTY_SECTION,
            DETERMINISTIC_RULE_DUPLICATE_HEADING,
            DETERMINISTIC_RULE_DUPLICATE_PARAGRAPH,
            DETERMINISTIC_RULE_INCONSISTENT_FILENAME,
        }
        assert expected <= fired

    def test_iecc_long_form_citation_alerts(self):
        """The module-local Energy Conservation long-form pattern fires."""
        payload = self._alert_payload()
        stale_contexts = [
            alert.get("context", "")
            for alert in payload["code_cycle_alerts"]
            if alert.get("deterministic_rule") == DETERMINISTIC_RULE_STALE_CODE_CYCLE
        ]
        assert any(
            "International Energy Conservation Code" in ctx for ctx in stale_contexts
        )

    def test_invalid_year_uses_generic_wording(self):
        """No jurisdiction label -> generic 'Invalid code cycle year' wording."""
        payload = self._alert_payload()
        invalid = [
            alert
            for alert in payload["invalid_code_cycle_alerts"]
            if alert.get("deterministic_rule") == DETERMINISTIC_RULE_INVALID_CODE_CYCLE
        ]
        assert invalid, "expected an invalid-code-cycle alert for '2019 IBC'"
        assert any("2019" in a.get("match", "") for a in invalid)
        for a in invalid:
            assert "California" not in a.get("type", "")
            assert a["type"].startswith("Invalid code cycle year")

    def test_negated_stale_reference_is_suppressed(self):
        """"previously ... 2021 IBC" is historical context, not a stale citation."""
        payload = self._alert_payload()
        stale_matches = [
            alert["match"]
            for alert in payload["code_cycle_alerts"]
            if alert.get("deterministic_rule") == DETERMINISTIC_RULE_STALE_CODE_CYCLE
        ]
        assert not any("2021" in match for match in stale_matches)


# ---------------------------------------------------------------------------
# Location-aware surfaces (research fan-out + compliance pass + rendered
# requirements profile). Byte-pinned like every other surface so a later
# engine change to the research/compliance protocol shows up as a diff.
# ---------------------------------------------------------------------------

from src.compliance.compliance_checker import (  # noqa: E402
    _build_compliance_user_message,
    _compliance_system_prompt,
)
from src.core.project_profile import ProjectProfile  # noqa: E402
from src.modules.datacenter_architectural import DATACENTER_ARCHITECTURAL  # noqa: E402
from src.research.requirements_research import (  # noqa: E402
    DimensionStatus,
    RequirementsProfile,
    ResearchItem,
    build_dimension_user_message,
    build_research_system_prompt,
)

# Fixed dummy profile — display forms match what a real run would render
# ("Ashburn, Virginia, USA"), never de-anonymized field identifiers.
_GOLDEN_PROFILE = ProjectProfile(
    city="Ashburn", state_or_province="VA", country="US", client_name="ExampleCo"
)

# A fixed corpus-signal block (already rendered) so the golden pins the
# <corpus_signals> wrap without depending on the scrape's own output.
_GOLDEN_CORPUS_SIGNALS = (
    "Client/owner documents named in the specifications:\n"
    "- Comply with the ExampleCo Architectural Basis of Design, Rev. 2.\n\n"
    "Standards cited with edition years:\n"
    "- NFPA 80 (2019)"
)


def _golden_requirements_profile() -> RequirementsProfile:
    """A fixed multi-item profile exercising every render branch.

    Grounded spec_requirement, ungrounded (→ [UNVERIFIED]), and a process
    advisory (→ [PROCESS]); one item per rendered section; a partially-failed
    dimension set so the header shows "N of M dimensions completed".
    """
    items = [
        ResearchItem(
            item_id="r-000000000001",
            dimension_id="governing_codes",
            topic="Building code edition",
            category="governing_code",
            requirement="The 2021 Virginia USBC (2021 IBC basis) governs, effective 2024-01-18.",
            authority="Virginia DHCD",
            code_reference="13VAC5-63",
            accepted_sources=["https://law.lis.virginia.gov/admincode/title13/agency5/chapter63/"],
            grounded=True,
            confidence=0.92,
        ),
        ResearchItem(
            item_id="r-000000000002",
            dimension_id="governing_codes",
            topic="Referenced fire-door standard",
            category="referenced_standard",
            requirement="The adopted code references NFPA 80-2019 for fire door and opening-protective installation.",
            authority="Virginia DHCD",
            code_reference="IBC 716",
            accepted_sources=["https://law.lis.virginia.gov/admincode/title13/agency5/chapter63/"],
            grounded=True,
            confidence=0.78,
        ),
        ResearchItem(
            item_id="r-000000000003",
            dimension_id="ahj_requirements",
            topic="Air-barrier testing window",
            category="ahj_requirement",
            requirement="Third-party whole-building airtightness tests are witnessed April through October only.",
            authority="Loudoun County Building Department",
            accepted_sources=["https://www.loudoun.gov/building"],
            grounded=True,
            confidence=0.7,
            actionability="process_advisory",
        ),
        ResearchItem(
            item_id="r-000000000004",
            dimension_id="client_standards",
            topic="Owner roofing preference",
            category="client_standard",
            requirement="ExampleCo standards prefer mechanically attached single-ply roofing on data halls.",
            authority="ExampleCo",
            grounded=False,
            confidence=0.4,
        ),
        ResearchItem(
            item_id="r-000000000005",
            dimension_id="site_environment",
            topic="Seismic design category",
            category="site_environment",
            requirement="ASCE 7-22 Seismic Design Category B applies at this site.",
            authority="ASCE 7-22",
            accepted_sources=["https://ascehazardtool.org/"],
            grounded=True,
            confidence=0.65,
        ),
    ]
    statuses = [
        DimensionStatus(dimension_id="governing_codes", status="completed",
                        item_count=2, grounded_count=2, web_search_requests=18),
        DimensionStatus(dimension_id="ahj_requirements", status="completed",
                        item_count=1, grounded_count=1, web_search_requests=12),
        DimensionStatus(dimension_id="client_standards", status="failed",
                        error="all searches returned confidential/unretrievable sources"),
        DimensionStatus(dimension_id="site_environment", status="completed",
                        item_count=1, grounded_count=1, web_search_requests=6),
    ]
    return RequirementsProfile(
        items=items,
        dimension_statuses=statuses,
        research_date="2026-07-15",
        project=_GOLDEN_PROFILE.to_dict(),
    )


class TestResearchPromptGoldens:
    def test_system_prompt(self):
        assert_matches_golden(
            "dcarch_research_system_prompt.txt",
            build_research_system_prompt(DATACENTER_ARCHITECTURAL),
        )

    @pytest.mark.parametrize(
        "dimension_id",
        ["governing_codes", "ahj_requirements", "client_standards", "site_environment"],
    )
    def test_dimension_user_messages(self, dimension_id):
        dimension = next(
            d for d in DATACENTER_ARCHITECTURAL.research_dimensions
            if d.dimension_id == dimension_id
        )
        message = build_dimension_user_message(
            DATACENTER_ARCHITECTURAL, _GOLDEN_PROFILE, dimension
        )
        assert_matches_golden(f"dcarch_research_user_{dimension_id}.txt", message)

    def test_dimension_user_message_with_corpus_signals(self):
        dimension = DATACENTER_ARCHITECTURAL.research_dimensions[0]
        message = build_dimension_user_message(
            DATACENTER_ARCHITECTURAL,
            _GOLDEN_PROFILE,
            dimension,
            corpus_signals_block=_GOLDEN_CORPUS_SIGNALS,
        )
        assert_matches_golden(
            "dcarch_research_user_governing_codes_with_signals.txt", message
        )


class TestRequirementsProfileBlockGolden:
    def test_rendered_block(self):
        assert_matches_golden(
            "dcarch_requirements_profile_block.txt",
            _golden_requirements_profile().render_text(),
        )


class TestCompliancePromptGoldens:
    def test_system_prompt(self):
        assert_matches_golden(
            "dcarch_compliance_system_prompt.txt",
            _compliance_system_prompt(_CYCLE),
        )

    def test_user_message(self):
        specs = [
            ExtractedSpec(
                filename=_SPEC_FILENAME,
                content=_SPEC_CONTENT,
                word_count=42,
                paragraph_map=list(_PARAGRAPH_MAP),
            ),
            ExtractedSpec(
                filename="07 84 13 - Penetration Firestopping.docx",
                content="1.01 SUMMARY\n\nA. Provide penetration firestopping as indicated.",
                word_count=9,
                paragraph_map=None,
            ),
        ]
        existing = [
            _fixture_finding(finding_id="rf-0123456789ab"),
        ]
        message = _build_compliance_user_message(
            specs,
            _golden_requirements_profile(),
            existing,
            project_context="Hyperscale data-center new-build program.",
        )
        assert_matches_golden("dcarch_compliance_user_message.txt", message)
