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

import logging

import pytest

from src.core import api_config
from src.core.api_config import (
    MODEL_HAIKU_45,
    MODEL_OPUS_48,
    MODEL_SONNET_46,
    OPUS_MODELS,
    PHASE_REVIEW,
    PHASE_TRIAGE,
    PHASE_VERIFICATION,
    apply_thinking_config,
    effort_config_for,
    model_capabilities,
    model_supports_adaptive_thinking,
    model_supports_effort,
    model_supports_extended_output_beta,
    output_cap_for_model,
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
        assert thinking_config_for(model=MODEL_OPUS_48, phase=PHASE_TRIAGE) is None
        assert thinking_config_for(model=MODEL_SONNET_46, phase=PHASE_TRIAGE) is None

    def test_unknown_model_returns_none(self) -> None:
        assert thinking_config_for(model="claude-mystery", phase=PHASE_REVIEW) is None


# ---------------------------------------------------------------------------
# apply_thinking_config
# ---------------------------------------------------------------------------


class TestApplyThinkingConfig:
    @pytest.mark.parametrize(
        "model, phase",
        [
            # Haiku never carries thinking (API would reject it).
            (MODEL_HAIKU_45, PHASE_TRIAGE),
            # Unknown models degrade to safe defaults.
            ("claude-mystery", PHASE_REVIEW),
            # Phase-level opt-out wins over a capable model.
            (MODEL_OPUS_48, PHASE_TRIAGE),
        ],
    )
    def test_omits_key(self, model: str, phase: str) -> None:
        kwargs: dict = {"model": model, "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model=model, phase=phase)
        assert "thinking" not in result

    def test_adds_key_for_opus(self) -> None:
        kwargs: dict = {"model": MODEL_OPUS_48, "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model=MODEL_OPUS_48, phase=PHASE_REVIEW)
        assert result["thinking"] == {"type": "adaptive"}

    def test_never_sets_thinking_to_none(self) -> None:
        """Anthropic API rejects ``thinking=null``; the key must be omitted
        entirely. This guards against a future regression where someone
        ``kwargs["thinking"] = None``-s the absent case."""
        kwargs: dict = {"model": MODEL_HAIKU_45}
        result = apply_thinking_config(kwargs, model=MODEL_HAIKU_45, phase=PHASE_TRIAGE)
        assert "thinking" not in result
        assert result.get("thinking") is None


# ---------------------------------------------------------------------------
# Opus 4.8 whitelisting (TRUST_AUDIT P0-3)
# ---------------------------------------------------------------------------


class TestOpus48Whitelisted:
    """Opus 4.8 must resolve to full capabilities, not the conservative
    unknown-model defaults that quietly under-power a deliberately-selected
    newer model (no extended thinking, 64k output cap, 200k context, no
    effort, no 300k batch beta). Capability flags are pinned to the values
    Anthropic's "What's new in Claude Opus 4.8" / models overview document."""

    def test_registered_with_full_capabilities(self) -> None:
        caps = model_capabilities(MODEL_OPUS_48)
        assert caps.supports_adaptive_thinking is True
        assert caps.supports_extended_output_beta is True
        assert caps.supports_effort is True
        assert caps.context_window == 1_000_000
        assert caps.max_output_tokens == 128_000

    def test_in_opus_models_set(self) -> None:
        """Membership drives the 128k output ceiling and the high-effort
        verification-escalation tier — both keyed off ``OPUS_MODELS``, not the
        capability record, so the id must appear in both places."""
        assert MODEL_OPUS_48 in OPUS_MODELS

    def test_gets_opus_output_ceiling_not_sonnet(self) -> None:
        # Were Opus 4.8 missing from OPUS_MODELS it would clamp to the Sonnet
        # 64k ceiling instead of the Opus 128k one.
        assert output_cap_for_model(MODEL_OPUS_48, requested=300_000) == 128_000

    def test_capability_helpers_agree(self) -> None:
        assert model_supports_adaptive_thinking(MODEL_OPUS_48) is True
        assert model_supports_effort(MODEL_OPUS_48) is True
        assert model_supports_extended_output_beta(MODEL_OPUS_48) is True

    def test_thinking_enabled_for_review(self) -> None:
        assert thinking_config_for(model=MODEL_OPUS_48, phase=PHASE_REVIEW) == {
            "type": "adaptive"
        }

    def test_high_effort_on_verification_escalation(self) -> None:
        # Opus on a verification phase is the escalation tier → high effort.
        assert effort_config_for(model=MODEL_OPUS_48, phase=PHASE_VERIFICATION) == {
            "effort": "high"
        }


# ---------------------------------------------------------------------------
# Unknown-model degradation is loud, not silent (TRUST_AUDIT P0-3)
# ---------------------------------------------------------------------------


class TestUnknownModelWarnsLoudly:
    """Unknown ids still degrade to safe defaults (never an invalid request),
    but now emit a one-time WARNING so a stale whitelist that under-powers a
    newer/better model is visible to the operator instead of silent."""

    def test_unknown_model_degrades_and_warns_once(self, caplog) -> None:
        model = "claude-imaginary-9-9"
        # The warning is deduped via a module-level set that persists across
        # calls (and tests); reset just this id so the assertion is isolated.
        api_config._WARNED_UNKNOWN_MODELS.discard(model)
        with caplog.at_level(logging.WARNING):
            caps_first = model_capabilities(model)
            caps_second = model_capabilities(model)

        # Still degrades to the conservative defaults.
        assert caps_first is api_config._DEFAULT_CAPABILITIES
        assert caps_second is api_config._DEFAULT_CAPABILITIES

        # ...and warned exactly once despite two lookups.
        matching = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and model in r.getMessage()
        ]
        assert len(matching) == 1
        assert "capability whitelist" in matching[0].getMessage()

    def test_known_models_never_warn(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            for model in (
                MODEL_OPUS_48,
                MODEL_SONNET_46,
                MODEL_HAIKU_45,
            ):
                model_capabilities(model)
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


# ---------------------------------------------------------------------------
# Effort policy — review/cross-check use xhigh; verification stays bounded
# ---------------------------------------------------------------------------


class TestEffortPolicy:
    """Per-phase effort levels. Review and cross-check default to ``xhigh``
    (Anthropic's recommended starting point for coding/agentic work on Opus
    4.8); verification stays medium (Sonnet) / high (Opus escalation) so
    the verdict envelope doesn't balloon. ``xhigh`` is Opus-only, so any phase
    that defaults to it clamps to ``high`` on a non-Opus model (see
    ``TestXhighClampsOnNonOpus``)."""

    def test_review_uses_xhigh(self) -> None:
        # Review defaults to Opus 4.8, which accepts xhigh.
        assert effort_config_for(model=MODEL_OPUS_48, phase=api_config.PHASE_REVIEW) == {
            "effort": "xhigh"
        }

    def test_sonnet_verification_stays_medium(self) -> None:
        # The xhigh bump must not leak into verification — the initial pass is
        # a bounded verdict, not deep reasoning.
        assert effort_config_for(
            model=MODEL_SONNET_46, phase=PHASE_VERIFICATION
        ) == {"effort": "medium"}

    def test_opus_escalation_stays_high(self) -> None:
        assert effort_config_for(
            model=MODEL_OPUS_48, phase=PHASE_VERIFICATION
        ) == {"effort": "high"}

    def test_haiku_omits_effort_everywhere(self) -> None:
        # Haiku does not support effort; the helper must omit the field.
        assert effort_config_for(model=MODEL_HAIKU_45, phase=api_config.PHASE_REVIEW) is None


# ---------------------------------------------------------------------------
# xhigh is Opus-only — every phase clamps it to high on a non-Opus model
# ---------------------------------------------------------------------------


class TestXhighClampsOnNonOpus:
    """Regression: ``xhigh`` is an Opus-4.8-only effort level. Sonnet 4.6
    rejects it at submit with HTTP 400 ("This model does not support effort
    level 'xhigh'. Supported levels: high, low, max, medium."). Because the
    cross-check phase defaults to ``xhigh`` but *always* runs on Sonnet 4.6
    (``CROSS_CHECK_MODEL_DEFAULT``), every cross-spec coordination pass used to
    400 at submit and produce zero findings. ``effort_config_for`` must clamp
    ``xhigh`` down to ``high`` on any non-Opus model."""

    def test_cross_check_on_sonnet_clamps_to_high(self) -> None:
        # The bug: this used to return {"effort": "xhigh"} → 400 on Sonnet.
        assert effort_config_for(
            model=MODEL_SONNET_46, phase=api_config.PHASE_CROSS_CHECK
        ) == {"effort": "high"}

    def test_cross_check_default_model_does_not_400(self) -> None:
        # Pin the real wiring: the phase's default model must never be asked
        # for an effort level it rejects. Guards against someone flipping
        # CROSS_CHECK_MODEL_DEFAULT back to a non-xhigh model without the clamp.
        cfg = effort_config_for(
            model=api_config.CROSS_CHECK_MODEL_DEFAULT,
            phase=api_config.PHASE_CROSS_CHECK,
        )
        assert cfg is not None
        assert cfg["effort"] != "xhigh"

    def test_review_on_sonnet_override_clamps_to_high(self) -> None:
        # Latent variant: SPEC_CRITIC_REVIEW_MODEL=sonnet would otherwise 400.
        assert effort_config_for(
            model=MODEL_SONNET_46, phase=api_config.PHASE_REVIEW
        ) == {"effort": "high"}

    def test_opus_keeps_xhigh_on_both_deep_phases(self) -> None:
        # Opus 4.8 accepts xhigh — the clamp must not strip it.
        for phase in (api_config.PHASE_REVIEW, api_config.PHASE_CROSS_CHECK):
            assert effort_config_for(model=MODEL_OPUS_48, phase=phase) == {
                "effort": "xhigh"
            }

    def test_clamp_helper_is_targeted(self) -> None:
        # Only xhigh is clamped, and only off-Opus; other levels pass through.
        assert api_config._clamp_effort_for_model("xhigh", MODEL_SONNET_46) == "high"
        assert api_config._clamp_effort_for_model("xhigh", MODEL_OPUS_48) == "xhigh"
        assert api_config._clamp_effort_for_model("high", MODEL_SONNET_46) == "high"
        assert api_config._clamp_effort_for_model("medium", MODEL_SONNET_46) == "medium"


# ---------------------------------------------------------------------------
# Default models track the newest Opus generation (4.8)
# ---------------------------------------------------------------------------


class TestDefaultModelsAreOpus48:
    """Review and verification-escalation default to Opus 4.8, the flagship
    Opus generation. Pinned so a future model bump is a deliberate, reviewed
    edit."""

    def test_review_default_is_opus_48(self) -> None:
        # Holds when SPEC_CRITIC_REVIEW_MODEL is unset (the test harness env).
        assert api_config.REVIEW_MODEL_DEFAULT == MODEL_OPUS_48

    def test_escalation_default_is_opus_48(self) -> None:
        assert api_config.VERIFICATION_ESCALATION_MODEL == MODEL_OPUS_48

    def test_initial_verifier_still_sonnet(self) -> None:
        # Escalation only fires when initial != escalation model; keep them
        # distinct so the escalation tier stays meaningful.
        assert api_config.VERIFICATION_MODEL_DEFAULT == MODEL_SONNET_46
        assert api_config.VERIFICATION_MODEL_DEFAULT != api_config.VERIFICATION_ESCALATION_MODEL

    def test_cross_check_still_sonnet(self) -> None:
        assert api_config.CROSS_CHECK_MODEL_DEFAULT == MODEL_SONNET_46
