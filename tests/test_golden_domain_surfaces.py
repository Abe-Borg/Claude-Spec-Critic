"""Byte-exact golden pins for every domain-flavored prompt / detector surface.

Phase 0 of the module-extraction refactor ("California K-12 DSA M&P" becomes
one selectable module among several). These tests freeze the *current* output
of every surface that carries domain knowledge — the reviewer prompts, the
cross-check prompts, the verifier prompts, and the deterministic preprocessor
alerts — so the extraction phases that follow can prove they changed nothing
for the existing California configuration.

Contract for later phases: a diff that moves domain content into a module
object MUST keep every golden in ``tests/goldens/`` byte-identical. A golden
may only change in a diff that *intentionally* changes what the model is sent,
with the regeneration called out in review.

Regenerating (only when an output change is intentional)::

    SPEC_CRITIC_UPDATE_GOLDENS=1 pytest tests/test_golden_domain_surfaces.py

The tests are hermetic: fixed inputs, no API key, no network. Env-sensitive
paths (element-id rendering) explicitly clear the toggle so the goldens pin
the default-enabled behavior regardless of the ambient environment.
"""
from __future__ import annotations

import difflib
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from src.core.code_cycles import CALIFORNIA_2025
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
from src.review.prompts import get_single_spec_user_message, get_system_prompt
from src.review.reviewer import Finding
from src.verification.verifier import (
    _build_verification_prompt,
    _get_verification_system_prompt,
)


# ---------------------------------------------------------------------------
# Golden-file machinery
# ---------------------------------------------------------------------------

_GOLDEN_DIR = Path(__file__).parent / "goldens"
_UPDATE_ENV = "SPEC_CRITIC_UPDATE_GOLDENS"


def _update_goldens_enabled() -> bool:
    return os.environ.get(_UPDATE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def assert_matches_golden(name: str, actual: str) -> None:
    """Compare ``actual`` byte-for-byte against ``tests/goldens/<name>``.

    With ``SPEC_CRITIC_UPDATE_GOLDENS`` truthy the golden is (re)written
    instead — use only when an output change is intentional, and commit the
    regenerated file alongside the change that caused it.
    """
    path = _GOLDEN_DIR / name
    data = actual.encode("utf-8")
    if _update_goldens_enabled():
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return
    if not path.exists():
        pytest.fail(
            f"Golden file tests/goldens/{name} is missing. Generate it with "
            f"{_UPDATE_ENV}=1 pytest {Path(__file__).name} and commit the result."
        )
    expected = path.read_bytes()
    if data == expected:
        return
    diff = "\n".join(
        difflib.unified_diff(
            expected.decode("utf-8").splitlines(),
            actual.splitlines(),
            fromfile=f"goldens/{name}",
            tofile="actual",
            lineterm="",
        )
    )
    pytest.fail(
        f"Output diverged from golden tests/goldens/{name}. The domain surfaces "
        f"are pinned byte-exactly for the module-extraction refactor; if this "
        f"change is intentional, regenerate with {_UPDATE_ENV}=1 and call the "
        f"regeneration out in review.\n{diff}"
    )


@pytest.fixture()
def default_element_ids(monkeypatch):
    """Pin the default-enabled element-id rendering regardless of ambient env."""
    monkeypatch.delenv("SPEC_CRITIC_ELEMENT_IDS", raising=False)


# ---------------------------------------------------------------------------
# Shared fixture inputs (fixed constants — never derive from runtime state)
# ---------------------------------------------------------------------------

_SPEC_FILENAME = "23 05 00 - Common Work for HVAC.docx"

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
        text="A. Comply with 2019 CBC Chapter 6 for all mechanical work.",
        table_index=None,
        row_index=None,
        cell_index=None,
        element_id="p1",
        section_id="1.01 SUMMARY",
    ),
    ParagraphMapping(
        body_index=2,
        element_type="table_cell",
        text="EF-1 | 500 CFM | Greenheck model GB-041",
        table_index=0,
        row_index=0,
        cell_index=None,
        element_id="t0r0",
        section_id="1.01 SUMMARY",
    ),
]

_SPEC_CONTENT = "\n\n".join(m.text for m in _PARAGRAPH_MAP)

_PROJECT_CONTEXT = (
    "Modernization of two classroom wings at a K-12 campus. No LEED scope."
)

_PRE_DETECTED_ALERTS = [
    {
        "filename": _SPEC_FILENAME,
        "type": "SELECT placeholder",
        "match": "[SELECT MODEL]",
        "context": "Furnish fans per the fan schedule [SELECT MODEL] prior to review.",
        "position": 120,
        "deterministic_rule": "placeholder",
    },
    {
        "filename": _SPEC_FILENAME,
        "type": "TBD placeholder",
        "match": "[TBD]",
        "context": "Motor voltage [TBD] pending electrical coordination.",
        "position": 180,
        "deterministic_rule": "placeholder",
    },
    {
        "filename": _SPEC_FILENAME,
        "type": "Stale code cycle reference (2019 vs selected 2025)",
        "match": "2019 CBC",
        "context": "Comply with 2019 CBC Chapter 6 for all mechanical work.",
        "position": 60,
        "deterministic_rule": "stale_code_cycle",
    },
]


def _fixture_finding(**overrides) -> Finding:
    base = dict(
        severity="HIGH",
        fileName=_SPEC_FILENAME,
        section="3.01",
        issue="Sequence of operations references damper types missing from the schedule.",
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
            "reviewer_system_prompt.txt", get_system_prompt(CALIFORNIA_2025)
        )

    def test_user_message_plain(self):
        # No paragraph map -> legacy plain-body rendering regardless of the
        # element-id env toggle; no context / alerts -> minimal shape.
        message = get_single_spec_user_message(
            _SPEC_CONTENT,
            _SPEC_FILENAME,
            "",
            cycle=CALIFORNIA_2025,
            paragraph_map=None,
            pre_detected_alerts=None,
        )
        assert_matches_golden("reviewer_user_message_plain.txt", message)

    def test_user_message_full(self, default_element_ids):
        # Element ids + project context + pre-detected alerts: the maximal
        # user-message shape (id-tagged body, <project_context>, <pre_detected>).
        message = get_single_spec_user_message(
            _SPEC_CONTENT,
            _SPEC_FILENAME,
            _PROJECT_CONTEXT,
            cycle=CALIFORNIA_2025,
            paragraph_map=_PARAGRAPH_MAP,
            pre_detected_alerts=_PRE_DETECTED_ALERTS,
        )
        assert_matches_golden("reviewer_user_message_full.txt", message)


# ---------------------------------------------------------------------------
# Cross-check prompts
# ---------------------------------------------------------------------------


class TestCrossCheckPromptGoldens:
    def test_system_prompt(self):
        assert_matches_golden(
            "cross_check_system_prompt.txt", _cross_system_prompt(CALIFORNIA_2025)
        )

    def test_user_message(self, default_element_ids):
        specs = [
            ExtractedSpec(
                filename=_SPEC_FILENAME,
                content=_SPEC_CONTENT,
                word_count=42,
                paragraph_map=list(_PARAGRAPH_MAP),
            ),
            # No paragraph map -> pins the plain <spec> fallback path too.
            ExtractedSpec(
                filename="22 11 16 - Domestic Water Piping.docx",
                content="1.01 SUMMARY\n\nA. Provide domestic water piping as indicated.",
                word_count=9,
                paragraph_map=None,
            ),
        ]
        prior_findings = [
            _fixture_finding(finding_id="rf-0123456789ab"),
            # Missing section / finding_id pins the attribute-skip path.
            _fixture_finding(
                section="",
                issue="Water heater capacity differs between plumbing and schedule.",
                fileName="22 11 16 - Domestic Water Piping.docx",
            ),
        ]
        spec_input = _build_cross_check_input(specs, prior_findings)
        message = _get_cross_check_user_message(
            spec_input, len(specs), "District-wide modernization program."
        )
        assert_matches_golden("cross_check_user_message.txt", message)


# ---------------------------------------------------------------------------
# Verifier prompts
# ---------------------------------------------------------------------------


class TestVerifierPromptGoldens:
    def test_system_prompt_with_verdict_tool(self):
        assert_matches_golden(
            "verifier_system_prompt_with_verdict_tool.txt",
            _get_verification_system_prompt(CALIFORNIA_2025, include_verdict_tool=True),
        )

    def test_system_prompt_without_verdict_tool(self):
        assert_matches_golden(
            "verifier_system_prompt_without_verdict_tool.txt",
            _get_verification_system_prompt(CALIFORNIA_2025, include_verdict_tool=False),
        )

    def test_user_prompt_with_verdict_tool(self):
        finding = _fixture_finding(
            severity="CRITICAL",
            section="1.03",
            issue="Spec cites a superseded California Building Code edition.",
            actionType="EDIT",
            existingText="Comply with 2019 CBC Chapter 6.",
            replacementText="Comply with 2025 CBC Chapter 6.",
            codeReference="CBC Chapter 6",
            confidence=0.9,
        )
        assert_matches_golden(
            "verifier_user_prompt_with_verdict_tool.txt",
            _build_verification_prompt(
                finding, cycle=CALIFORNIA_2025, include_verdict_tool=True
            ),
        )

    def test_user_prompt_without_verdict_tool(self):
        # None fields render as "none" -> pins the fallback text path as well.
        assert_matches_golden(
            "verifier_user_prompt_without_verdict_tool.txt",
            _build_verification_prompt(
                _fixture_finding(), cycle=CALIFORNIA_2025, include_verdict_tool=False
            ),
        )


# ---------------------------------------------------------------------------
# Deterministic preprocessor output
# ---------------------------------------------------------------------------

# One fixture spec that exercises every deterministic rule at least once.
# Paragraphs are separated by blank lines (the heading regex anchors on
# "\n\n"); no body paragraph may start with a bare number or it would be
# misread as a section heading.
_PREPROCESS_FIXTURE_TEXT = """\
1.01 SUMMARY

A. Provide HVAC work per 2019 CBC Chapter 6 and anchor equipment per ASCE 7-10.

B. The building was previously permitted under the 2022 CBC.

C. Comply with 2018 CBC requirements for mechanical penetrations.

D. Contractor shall document LEED-NC credits and USGBC checklists where applicable.

E. Furnish fans per the fan schedule [SELECT MODEL] prior to submittal review.

F. TODO: confirm duct gauge selections with the structural engineer.

Provide seismic restraints for all suspended equipment in accordance with the approved structural details and project drawings.

1.02 REFERENCES

A. Reference standards apply as listed.

1.02 REFERENCES

A. Reference standards apply as listed.

2.01 PRODUCTS

2.02 WARRANTY

Provide seismic restraints for all suspended equipment in accordance with the approved structural details and project drawings.
"""

_PROJECT_FILENAMES = [
    _SPEC_FILENAME,
    "22 11 16 - Domestic Water Piping.docx",
    "23-31-13-Metal-Ducts.docx",
]


class TestPreprocessorGolden:
    def _alert_payload(self) -> dict:
        result = preprocess_spec(
            _PREPROCESS_FIXTURE_TEXT, _SPEC_FILENAME, cycle=CALIFORNIA_2025
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
        assert_matches_golden("preprocessor_alerts.json", serialized)

    def test_every_deterministic_rule_fires_once(self):
        """Semantic companion to the byte pin: the fixture covers every rule.

        If a later phase reworks detector vocabulary, this test explains
        *which* rule went missing instead of dumping a JSON diff.
        """
        payload = self._alert_payload()
        fired = {
            str(alert.get("deterministic_rule"))
            for alerts in payload.values()
            for alert in alerts
        }
        expected = {
            DETERMINISTIC_RULE_LEED,
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

    def test_negated_stale_reference_is_suppressed(self):
        """"previously ... 2022 CBC" is historical context, not a stale citation."""
        payload = self._alert_payload()
        stale_matches = [
            alert["match"]
            for alert in payload["code_cycle_alerts"]
            if alert.get("deterministic_rule") == DETERMINISTIC_RULE_STALE_CODE_CYCLE
        ]
        assert not any("2022" in match for match in stale_matches)
