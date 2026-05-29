"""Model capability policy and request-shape coverage.

Unit-level coverage of the capability policy in ``api_config``:
``ModelCapabilities`` records, ``thinking_config_for``, and
``apply_thinking_config``.

These tests pin the load-bearing invariants that prevent the kind of bug
the policy fixes — every request path used to hard-code
``thinking={"type": "adaptive"}``, which produced an API error against
Haiku 4.5. The capability policy is the single source of truth that keeps
unsupported parameters off the wire.
"""
from __future__ import annotations

from src.core.api_config import (
    MODEL_HAIKU_45,
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    PHASE_REVIEW,
    PHASE_TRIAGE,
    apply_thinking_config,
    thinking_config_for,
)


# ---------------------------------------------------------------------------
# thinking_config_for — phase opt-outs and model degradation
# ---------------------------------------------------------------------------


class TestThinkingConfigFor:
    def test_haiku_always_returns_none(self) -> None:
        """Sending ``thinking`` to Haiku returns an API error; the helper
        must return None for Haiku regardless of phase."""
        assert thinking_config_for(model=MODEL_HAIKU_45, phase=PHASE_REVIEW) is None
        assert thinking_config_for(model=MODEL_HAIKU_45, phase=PHASE_TRIAGE) is None

    def test_triage_phase_returns_none_even_on_capable_model(self) -> None:
        """Phase-level opt-out wins over model capability."""
        assert thinking_config_for(model=MODEL_OPUS_47, phase=PHASE_TRIAGE) is None
        assert thinking_config_for(model=MODEL_SONNET_46, phase=PHASE_TRIAGE) is None

    def test_unknown_model_returns_none(self) -> None:
        assert thinking_config_for(model="claude-mystery", phase=PHASE_REVIEW) is None


# ---------------------------------------------------------------------------
# apply_thinking_config
# ---------------------------------------------------------------------------


class TestApplyThinkingConfig:
    def test_omits_key_for_haiku(self) -> None:
        kwargs: dict = {"model": MODEL_HAIKU_45, "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model=MODEL_HAIKU_45, phase=PHASE_TRIAGE)
        assert "thinking" not in result

    def test_adds_key_for_opus(self) -> None:
        kwargs: dict = {"model": MODEL_OPUS_47, "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model=MODEL_OPUS_47, phase=PHASE_REVIEW)
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
        result = apply_thinking_config(kwargs, model=MODEL_HAIKU_45, phase=PHASE_TRIAGE)
        assert "thinking" not in result
        assert result.get("thinking") is None
