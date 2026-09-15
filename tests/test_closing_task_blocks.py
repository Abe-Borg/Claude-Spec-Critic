"""Closing ``<final_task>`` blocks, the live fetch-budget line, and triage's gated tool choice.

Three prompt-layer corrections from the September 2026 prompt review, each
pinned so the link that makes it work cannot silently regress:

1. Cross-check and compliance user messages END with a ``<final_task>`` block
   placed after the corpus (and after every other trailing section), mirroring
   the per-spec review — Anthropic's long-context guidance puts the query after
   the documents, and these are the two passes with the largest multi-document
   input. The block must be *last*, must follow the cache breakpoints (it lives
   in the uncached user message), and must not disturb the stable instruction
   prefix the prompt-serialization tests already pin.
2. The verifier's ``<web_fetch_usage>`` names the fetch budget by interpolating
   ``DEFAULT_VERIFICATION_MAX_FETCHES`` — the same constant that sets the
   tool's enforced ``max_uses`` — instead of a hand-typed literal.
3. Triage forces its single tool on Haiku 4.5 (the one phase that never sends
   ``thinking``) and keeps ``auto`` on every model where forcing would 400.

Hermetic: no API key, no network.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.compliance.compliance_checker import (
    _CHUNK_SUBSET_NOTE,
    _COMPLIANCE_FINAL_TASK_BLOCK,
    _build_compliance_user_message,
    _compliance_system_prompt,
)
from src.core import api_config
from src.core.api_config import (
    DEFAULT_VERIFICATION_MAX_FETCHES,
    MODEL_HAIKU_45,
    MODEL_OPUS_48,
    MODEL_OPUS_5,
    MODEL_SONNET_46,
    MODEL_SONNET_5,
    build_web_fetch_tool,
    model_capabilities,
)
from src.core.code_cycles import CALIFORNIA_2025
from src.cross_check.cross_checker import (
    _CROSS_CHECK_FINAL_TASK_BLOCK,
    _build_cross_check_input,
    _cross_system_prompt,
    _get_cross_check_user_message,
)
from src.input.extractor import ExtractedSpec
from src.modules import get_module
from src.review.prompt_serialization import TAG_ALREADY_IDENTIFIED, TAG_CORPUS
from src.review.reviewer import Finding
from src.review.structured_schemas import triage_tool_choice
from src.verification import triage, verifier
from tests.fixtures.fake_anthropic import FakeMessage, FakeToolUseBlock
from tests.test_golden_datacenter_surfaces import _golden_requirements_profile


def _specs() -> list[ExtractedSpec]:
    return [
        ExtractedSpec(filename="21 13 13 - Wet-Pipe.docx", content="1.01 SUMMARY\n\nA. Wet pipe.", word_count=4),
        ExtractedSpec(filename="21 30 00 - Fire Pumps.docx", content="1.01 SUMMARY\n\nA. Fire pump.", word_count=4),
    ]


def _finding(**overrides) -> Finding:
    base = dict(
        severity="MEDIUM",
        fileName="21 13 13 - Wet-Pipe.docx",
        section="1.01",
        issue="Prior finding text.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
    )
    base.update(overrides)
    return Finding(**base)


# ---------------------------------------------------------------------------
# 1. Cross-check closing block
# ---------------------------------------------------------------------------


class TestCrossCheckFinalTask:
    def test_block_is_last_and_follows_corpus_and_already_identified(self):
        message = _get_cross_check_user_message(
            _build_cross_check_input(_specs(), [_finding()]),
            file_count=2,
            project_context="Some project context.",
        )
        assert message.endswith(_CROSS_CHECK_FINAL_TASK_BLOCK)
        assert message.count("<final_task>") == 1
        corpus_close = message.rindex(f"</{TAG_CORPUS}>")
        already_close = message.rindex(f"</{TAG_ALREADY_IDENTIFIED}>")
        assert corpus_close < already_close < message.index("<final_task>")

    def test_block_present_without_prior_findings_and_without_context(self):
        # Shape does not vary with whether prior findings / context exist.
        message = _get_cross_check_user_message(
            _build_cross_check_input(_specs(), []), file_count=2, project_context=""
        )
        assert message.endswith(_CROSS_CHECK_FINAL_TASK_BLOCK)
        # No <already_identified> section was rendered ...
        assert f"<{TAG_ALREADY_IDENTIFIED}>" not in message.split("<final_task>")[0]
        # ... yet the block still names it, unconditionally, as the system prompt does.
        assert "<already_identified>" in _CROSS_CHECK_FINAL_TASK_BLOCK

    def test_block_restates_system_prompt_rules_only(self):
        # Every judgment the block asks for is already stated in the system
        # prompt — the block is a reminder, not a second rulebook.
        system = _cross_system_prompt(CALIFORNIA_2025)
        assert "submit_cross_check_findings" in system
        for phrase in (
            "coordination is adequate",
            "including zero",
            "entirely within a single spec",
            "literally support",
            "<already_identified>",
        ):
            assert phrase in system, phrase
            assert phrase in _CROSS_CHECK_FINAL_TASK_BLOCK, phrase
        assert "Do not call it twice" in _CROSS_CHECK_FINAL_TASK_BLOCK

    def test_stable_prefix_before_corpus_is_unchanged(self):
        # The instruction prefix (everything before <corpus>) is what the
        # prompt-serialization tests pin; the block lives after it.
        a = _get_cross_check_user_message(
            _build_cross_check_input(_specs(), []), file_count=2
        )
        specs = _specs()
        specs[0] = ExtractedSpec(filename=specs[0].filename, content="different", word_count=1)
        b = _get_cross_check_user_message(_build_cross_check_input(specs, []), file_count=2)
        assert a.split(f"<{TAG_CORPUS}>")[0] == b.split(f"<{TAG_CORPUS}>")[0]
        assert a.split("</final_task>")[-1] == "" == b.split("</final_task>")[-1]


# ---------------------------------------------------------------------------
# 2. Compliance closing block
# ---------------------------------------------------------------------------


class TestComplianceFinalTask:
    def test_block_is_last_after_corpus(self):
        message = _build_compliance_user_message(
            _specs(), _golden_requirements_profile(), [_finding()],
            project_context="Hyperscale program.",
        )
        assert message.endswith(_COMPLIANCE_FINAL_TASK_BLOCK)
        assert message.count("<final_task>") == 1
        assert message.rindex(f"</{TAG_CORPUS}>") < message.index("<final_task>")
        assert message.rindex("</project_requirements_profile>") < message.index("<final_task>")

    def test_block_follows_chunk_subset_note(self):
        message = _build_compliance_user_message(
            _specs(), _golden_requirements_profile(), [], chunk_subset=True,
        )
        assert message.endswith(_COMPLIANCE_FINAL_TASK_BLOCK)
        assert message.index(_CHUNK_SUBSET_NOTE) < message.index("<final_task>")
        # The note is still the ONLY chunk-only section: without the flag it
        # is absent and the block is still last.
        plain = _build_compliance_user_message(_specs(), _golden_requirements_profile(), [])
        assert _CHUNK_SUBSET_NOTE not in plain
        assert plain.endswith(_COMPLIANCE_FINAL_TASK_BLOCK)

    def test_block_restates_system_prompt_rules_only(self):
        system = _compliance_system_prompt(get_module("datacenter_fire").cycle)
        assert "submit_compliance_findings" in system
        for phrase in (
            "<project_requirements_profile>",
            "[PROCESS]",
            "[UNVERIFIED]",
            "REPORT_ONLY",
            "<already_identified>",
            "exactly once",
        ):
            assert phrase in system, phrase
            assert phrase in _COMPLIANCE_FINAL_TASK_BLOCK, phrase

    def test_system_prompt_untouched_by_the_user_message_block(self):
        # The cached system prefix carries no <final_task>; the block rides
        # only the uncached user message, so caching is unaffected.
        assert "<final_task>" not in _compliance_system_prompt(get_module("datacenter_fire").cycle)
        assert "<final_task>" not in _cross_system_prompt(CALIFORNIA_2025)


# ---------------------------------------------------------------------------
# 3. Verifier fetch budget is interpolated, not typed
# ---------------------------------------------------------------------------


class TestVerifierFetchBudgetLine:
    def test_prompt_names_the_enforced_default(self):
        prompt = verifier._get_verification_system_prompt(CALIFORNIA_2025, include_verdict_tool=True)
        assert f"({DEFAULT_VERIFICATION_MAX_FETCHES} fetches by default)" in prompt
        assert build_web_fetch_tool()["max_uses"] == DEFAULT_VERIFICATION_MAX_FETCHES

    def test_retuning_the_constant_moves_the_prompt(self, monkeypatch):
        # The mutation the finding was about: a retuned budget that the
        # prompt no longer reflects. Patch the name the prompt reads.
        monkeypatch.setattr(verifier, "DEFAULT_VERIFICATION_MAX_FETCHES", 7)
        prompt = verifier._get_verification_system_prompt(CALIFORNIA_2025, include_verdict_tool=True)
        assert "(7 fetches by default)" in prompt
        assert "(3 fetches by default)" not in prompt


# ---------------------------------------------------------------------------
# 4. Triage forced tool choice, gated per model
# ---------------------------------------------------------------------------


class TestTriageForcedToolChoice:
    def test_haiku_forces_the_single_triage_tool(self):
        choice = triage_tool_choice(model=MODEL_HAIKU_45)
        assert choice == {
            "type": "tool",
            "name": triage.TRIAGE_TOOL_NAME,
            "disable_parallel_tool_use": True,
        }

    @pytest.mark.parametrize(
        "model",
        [MODEL_OPUS_5, MODEL_SONNET_5, MODEL_OPUS_48, MODEL_SONNET_46, "claude-unknown-9", None],
    )
    def test_every_other_model_keeps_auto(self, model):
        # Opus 5 / Sonnet 5: an omitted ``thinking`` key runs adaptive
        # thinking, and forcing tool_choice then 400s. Opus 4.8 / Sonnet 4.6:
        # deliberately conservative. Unknown / None: the request the API
        # always accepts.
        assert triage_tool_choice(model=model) == {"type": "auto", "disable_parallel_tool_use": True}

    def test_capability_flag_is_haiku_only_today(self):
        flagged = {
            model_id for model_id, caps in api_config._MODEL_CAPABILITIES.items()
            if caps.supports_forced_tool_choice
        }
        assert flagged == {MODEL_HAIKU_45}
        assert model_capabilities("claude-unknown-9").supports_forced_tool_choice is False

    def test_forcing_never_pairs_with_thinking(self):
        # The invariant the gate protects: any model allowed to force must be
        # one the app never sends ``thinking`` to on the triage phase.
        for model_id, caps in api_config._MODEL_CAPABILITIES.items():
            if caps.supports_forced_tool_choice:
                assert api_config.thinking_config_for(model=model_id, phase=api_config.PHASE_TRIAGE) is None

    def test_classify_batch_sends_forced_choice_on_default_model(self, monkeypatch):
        captured: dict = {}

        class Messages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return FakeMessage(
                    content=[
                        FakeToolUseBlock(
                            name=triage.TRIAGE_TOOL_NAME,
                            input={"classifications": [{"index": 0, "classification": "local_skip"}]},
                        )
                    ],
                    stop_reason="tool_use",
                )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(triage, "_get_client", lambda: SimpleNamespace(messages=Messages()))
        eligible = _finding(severity="GRIPES", issue="Typo in paragraph heading.")
        out = triage.classify_findings_with_haiku([eligible], model=MODEL_HAIKU_45)
        assert captured["model"] == MODEL_HAIKU_45
        assert captured["tool_choice"]["type"] == "tool"
        assert captured["tool_choice"]["name"] == triage.TRIAGE_TOOL_NAME
        assert "thinking" not in captured
        assert out == {0: "local_skip"}

    def test_classify_batch_keeps_auto_on_an_adaptive_thinking_override(self, monkeypatch):
        captured: dict = {}

        class Messages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return FakeMessage(content=[], stop_reason="end_turn")

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(triage, "_get_client", lambda: SimpleNamespace(messages=Messages()))
        eligible = _finding(severity="GRIPES", issue="Typo in paragraph heading.")
        out = triage.classify_findings_with_haiku([eligible], model=MODEL_SONNET_5)
        assert captured["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
        # No usable payload ⇒ the safe fallback, unchanged.
        assert out == {}
