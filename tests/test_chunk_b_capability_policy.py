"""Chunk B — model capability policy and request-shape coverage.

Centralizes the unit-level coverage of the capability policy added to
``api_config``: ``ModelCapabilities`` records, ``thinking_config_for``,
and ``apply_thinking_config``.

These tests pin the load-bearing invariants that prevent the kind of bug
the chunk fixes — every request path used to hard-code
``thinking={"type": "adaptive"}``, which produced an API error the moment
the synthesis pass switched to Haiku 4.5. The capability policy is the
single source of truth that keeps unsupported parameters off the wire.

Request-shape regressions for the new behavior live alongside the Chunk A
shape tests in ``test_request_payload_shape.py``.
"""
from __future__ import annotations

import pytest

from src.api_config import (
    MODEL_HAIKU_45,
    MODEL_OPUS_46,
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    MAX_OUTPUT_TOKENS_HAIKU,
    MAX_OUTPUT_TOKENS_OPUS,
    MAX_OUTPUT_TOKENS_SONNET,
    PHASE_BATCH_REVIEW,
    PHASE_CROSS_CHECK,
    PHASE_REVIEW,
    PHASE_SYNTHESIS,
    PHASE_TRIAGE,
    PHASE_VERIFICATION,
    PHASE_VERIFICATION_CONTINUATION,
    PHASE_VERIFICATION_RETRY,
    ModelCapabilities,
    apply_thinking_config,
    model_capabilities,
    model_supports_adaptive_thinking,
    thinking_config_for,
)


# ---------------------------------------------------------------------------
# ModelCapabilities registry
# ---------------------------------------------------------------------------


class TestModelCapabilitiesRegistry:
    """Each registered model must have a stable capability record."""

    def test_opus_46_supports_thinking(self) -> None:
        caps = model_capabilities(MODEL_OPUS_46)
        assert caps.supports_adaptive_thinking is True
        assert caps.max_output_tokens == MAX_OUTPUT_TOKENS_OPUS
        assert caps.supports_extended_output_beta is True

    def test_opus_47_supports_thinking(self) -> None:
        caps = model_capabilities(MODEL_OPUS_47)
        assert caps.supports_adaptive_thinking is True
        assert caps.max_output_tokens == MAX_OUTPUT_TOKENS_OPUS
        assert caps.supports_extended_output_beta is True

    def test_sonnet_46_supports_thinking_and_extended_output(self) -> None:
        caps = model_capabilities(MODEL_SONNET_46)
        assert caps.supports_adaptive_thinking is True
        assert caps.max_output_tokens == MAX_OUTPUT_TOKENS_SONNET
        # Chunk 1: Sonnet 4.6 supports the ``output-300k-2026-03-24`` beta
        # on Message Batches. Prior to Chunk 1 the registry incorrectly
        # marked this False, and the batch path relied on a family-style
        # ``model in OPUS_MODELS`` check that silently excluded Sonnet.
        assert caps.supports_extended_output_beta is True

    def test_haiku_45_does_not_support_thinking(self) -> None:
        """Regression: synthesis defaulted to Haiku while sending thinking,
        which produced an API error. Capability policy must record this."""
        caps = model_capabilities(MODEL_HAIKU_45)
        assert caps.supports_adaptive_thinking is False
        assert caps.max_output_tokens == MAX_OUTPUT_TOKENS_HAIKU
        assert caps.supports_extended_output_beta is False

    def test_unknown_model_degrades_safely(self) -> None:
        """Plan directive: unknown models should degrade safely. Every
        capability flag defaults to False so we never send an invalid
        request shape on a model identifier we haven't classified."""
        caps = model_capabilities("claude-future-model-2030")
        assert caps.supports_adaptive_thinking is False
        assert caps.supports_extended_output_beta is False
        # Output cap still has a sensible value so calls don't max_tokens=0.
        assert caps.max_output_tokens > 0

    def test_capabilities_record_is_frozen(self) -> None:
        """``ModelCapabilities`` is a frozen dataclass so accidental in-place
        mutation cannot bleed across request paths."""
        caps = model_capabilities(MODEL_OPUS_47)
        with pytest.raises(Exception):
            caps.supports_adaptive_thinking = False  # type: ignore[misc]

    def test_capabilities_helper_returns_modelcapabilities(self) -> None:
        caps = model_capabilities(MODEL_OPUS_47)
        assert isinstance(caps, ModelCapabilities)


# ---------------------------------------------------------------------------
# model_supports_adaptive_thinking
# ---------------------------------------------------------------------------


class TestModelSupportsAdaptiveThinking:
    @pytest.mark.parametrize(
        "model", [MODEL_OPUS_46, MODEL_OPUS_47, MODEL_SONNET_46]
    )
    def test_opus_and_sonnet_support_thinking(self, model: str) -> None:
        assert model_supports_adaptive_thinking(model) is True

    def test_haiku_does_not_support_thinking(self) -> None:
        assert model_supports_adaptive_thinking(MODEL_HAIKU_45) is False

    def test_unknown_model_does_not_support_thinking(self) -> None:
        assert model_supports_adaptive_thinking("claude-mystery") is False


# ---------------------------------------------------------------------------
# thinking_config_for
# ---------------------------------------------------------------------------


class TestThinkingConfigFor:
    @pytest.mark.parametrize(
        "phase",
        [
            PHASE_REVIEW,
            PHASE_BATCH_REVIEW,
            PHASE_CROSS_CHECK,
            PHASE_VERIFICATION,
            PHASE_VERIFICATION_RETRY,
            PHASE_VERIFICATION_CONTINUATION,
            PHASE_SYNTHESIS,
        ],
    )
    def test_opus_returns_adaptive_for_thinking_phases(self, phase: str) -> None:
        assert thinking_config_for(model=MODEL_OPUS_47, phase=phase) == {
            "type": "adaptive"
        }

    @pytest.mark.parametrize(
        "phase",
        [
            PHASE_REVIEW,
            PHASE_BATCH_REVIEW,
            PHASE_CROSS_CHECK,
            PHASE_VERIFICATION,
            PHASE_VERIFICATION_RETRY,
            PHASE_VERIFICATION_CONTINUATION,
            PHASE_SYNTHESIS,
        ],
    )
    def test_sonnet_returns_adaptive_for_thinking_phases(self, phase: str) -> None:
        assert thinking_config_for(model=MODEL_SONNET_46, phase=phase) == {
            "type": "adaptive"
        }

    @pytest.mark.parametrize(
        "phase",
        [
            PHASE_REVIEW,
            PHASE_BATCH_REVIEW,
            PHASE_CROSS_CHECK,
            PHASE_VERIFICATION,
            PHASE_VERIFICATION_RETRY,
            PHASE_VERIFICATION_CONTINUATION,
            PHASE_SYNTHESIS,
            PHASE_TRIAGE,
        ],
    )
    def test_haiku_always_returns_none(self, phase: str) -> None:
        """Haiku 4.5 does not support adaptive thinking; the helper must
        return None for every phase so the key is omitted entirely."""
        assert thinking_config_for(model=MODEL_HAIKU_45, phase=phase) is None

    def test_triage_phase_returns_none_even_on_capable_model(self) -> None:
        """The triage phase opts out of adaptive thinking regardless of model
        capability — classification doesn't benefit from extended reasoning."""
        assert thinking_config_for(model=MODEL_OPUS_47, phase=PHASE_TRIAGE) is None
        assert thinking_config_for(model=MODEL_SONNET_46, phase=PHASE_TRIAGE) is None

    def test_unknown_model_returns_none(self) -> None:
        assert thinking_config_for(model="claude-mystery", phase=PHASE_REVIEW) is None

    def test_returned_dict_is_a_fresh_copy(self) -> None:
        """Callers might mutate the returned dict (e.g. add ``display``);
        the helper should return a fresh dict each time, not a shared one."""
        a = thinking_config_for(model=MODEL_OPUS_47, phase=PHASE_REVIEW)
        b = thinking_config_for(model=MODEL_OPUS_47, phase=PHASE_REVIEW)
        assert a == b
        assert a is not b


# ---------------------------------------------------------------------------
# apply_thinking_config
# ---------------------------------------------------------------------------


class TestApplyThinkingConfig:
    def test_omits_key_for_haiku(self) -> None:
        kwargs: dict = {"model": MODEL_HAIKU_45, "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model=MODEL_HAIKU_45, phase=PHASE_SYNTHESIS)
        assert "thinking" not in result

    def test_adds_key_for_opus(self) -> None:
        kwargs: dict = {"model": MODEL_OPUS_47, "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model=MODEL_OPUS_47, phase=PHASE_REVIEW)
        assert result["thinking"] == {"type": "adaptive"}

    def test_adds_key_for_sonnet(self) -> None:
        kwargs: dict = {"model": MODEL_SONNET_46, "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model=MODEL_SONNET_46, phase=PHASE_VERIFICATION)
        assert result["thinking"] == {"type": "adaptive"}

    def test_omits_key_for_unknown_model(self) -> None:
        kwargs: dict = {"model": "claude-mystery", "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model="claude-mystery", phase=PHASE_REVIEW)
        assert "thinking" not in result

    def test_omits_key_for_triage_phase_on_opus(self) -> None:
        """Phase-level opt-out wins over model capability."""
        kwargs: dict = {"model": MODEL_OPUS_47}
        result = apply_thinking_config(kwargs, model=MODEL_OPUS_47, phase=PHASE_TRIAGE)
        assert "thinking" not in result

    def test_never_sets_thinking_to_none(self) -> None:
        """Anthropic API rejects ``thinking=null``; the key must be omitted
        entirely. This guards against a future regression where someone
        ``kwargs["thinking"] = None``-s the absent case."""
        kwargs: dict = {"model": MODEL_HAIKU_45}
        result = apply_thinking_config(kwargs, model=MODEL_HAIKU_45, phase=PHASE_SYNTHESIS)
        assert "thinking" not in result
        assert result.get("thinking") is None  # absent reads as None, not present-as-None

    def test_returns_same_dict_for_fluent_chaining(self) -> None:
        kwargs: dict = {"model": MODEL_OPUS_47}
        result = apply_thinking_config(kwargs, model=MODEL_OPUS_47, phase=PHASE_REVIEW)
        assert result is kwargs

    def test_preserves_other_kwargs(self) -> None:
        kwargs: dict = {
            "model": MODEL_OPUS_47,
            "max_tokens": 8000,
            "system": "system payload",
            "messages": [{"role": "user", "content": "x"}],
        }
        apply_thinking_config(kwargs, model=MODEL_OPUS_47, phase=PHASE_REVIEW)
        assert kwargs["max_tokens"] == 8000
        assert kwargs["system"] == "system payload"
        assert kwargs["messages"] == [{"role": "user", "content": "x"}]


# ---------------------------------------------------------------------------
# Phase identifier constants
# ---------------------------------------------------------------------------


class TestPhaseConstants:
    """Pin the phase identifier values so renaming one in api_config without
    a corresponding update in callers triggers a test failure rather than a
    silently-broken policy decision."""

    def test_phase_constants_are_distinct_strings(self) -> None:
        phases = {
            PHASE_REVIEW,
            PHASE_BATCH_REVIEW,
            PHASE_CROSS_CHECK,
            PHASE_SYNTHESIS,
            PHASE_VERIFICATION,
            PHASE_VERIFICATION_RETRY,
            PHASE_VERIFICATION_CONTINUATION,
            PHASE_TRIAGE,
        }
        # All 8 phases should be distinct strings.
        assert len(phases) == 8
        for p in phases:
            assert isinstance(p, str) and p
