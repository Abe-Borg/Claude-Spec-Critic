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
    MODEL_OPUS_5,
    MODEL_OPUS_48,
    MODEL_SONNET_46,
    MODEL_SONNET_5,
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
# Opus 5 whitelisting — the current review / escalation tier
# ---------------------------------------------------------------------------


class TestOpus5Whitelisted:
    """Opus 5 backs review and verification escalation, so it must resolve to
    full capabilities. Falling through to the conservative unknown-model
    defaults would strip adaptive thinking, effort, strict tools and the 300k
    batch beta from every review, and clamp output to 64k — a silently
    crippled run behind a single WARNING line. Flags are pinned to Anthropic's
    models overview and the Opus 4.8 → Opus 5 migration guide."""

    def test_registered_with_full_capabilities(self) -> None:
        caps = model_capabilities(MODEL_OPUS_5)
        assert caps.supports_adaptive_thinking is True
        assert caps.supports_effort is True
        assert caps.supports_xhigh_effort is True
        assert caps.supports_strict_tools is True
        assert caps.context_window == 1_000_000
        assert caps.max_output_tokens == 128_000

    def test_extended_output_beta_supported(self) -> None:
        # The models overview names Opus 5 explicitly in the
        # output-300k-2026-03-24 supported set, so this is confirmed rather
        # than the conservative placeholder Sonnet 5 originally carried.
        assert model_capabilities(MODEL_OPUS_5).supports_extended_output_beta is True

    def test_in_opus_models_set(self) -> None:
        """Membership drives the high-effort verification-escalation tier,
        which is keyed off ``OPUS_MODELS`` rather than the capability record —
        so a new Opus id must be added in both places or escalation silently
        drops from ``high`` to ``medium`` effort."""
        assert MODEL_OPUS_5 in OPUS_MODELS

    def test_in_hires_vision_set(self) -> None:
        # Opus 5 is in the high-resolution vision tier (2576px long edge,
        # ~4784-token image cap) alongside Opus 4.8; omitting it would
        # silently downgrade drawing-digest image-token estimates.
        assert MODEL_OPUS_5 in api_config.HIRES_VISION_MODELS

    def test_gets_opus_output_ceiling_not_sonnet(self) -> None:
        assert output_cap_for_model(MODEL_OPUS_5, requested=300_000) == 128_000

    def test_capability_helpers_agree(self) -> None:
        assert model_supports_adaptive_thinking(MODEL_OPUS_5) is True
        assert model_supports_effort(MODEL_OPUS_5) is True
        assert model_supports_extended_output_beta(MODEL_OPUS_5) is True

    def test_thinking_enabled_for_review(self) -> None:
        assert thinking_config_for(model=MODEL_OPUS_5, phase=PHASE_REVIEW) == {
            "type": "adaptive"
        }

    def test_review_runs_xhigh_effort_natively(self) -> None:
        # Opus 5 accepts the full effort ladder, so review's declared xhigh
        # survives ``_clamp_effort_for_model`` untouched.
        assert effort_config_for(model=MODEL_OPUS_5, phase=PHASE_REVIEW) == {
            "effort": "xhigh"
        }

    def test_high_effort_on_verification_escalation(self) -> None:
        assert effort_config_for(model=MODEL_OPUS_5, phase=PHASE_VERIFICATION) == {
            "effort": "high"
        }

    def test_never_sends_disabled_thinking(self) -> None:
        """Opus 5 rejects ``thinking={"type": "disabled"}`` at effort
        ``xhigh``/``max`` with a 400. The policy never emits ``disabled`` — it
        omits the key entirely — so the two can never be paired. Pinned
        because the review phase runs at ``xhigh``."""
        kwargs: dict = {"model": MODEL_OPUS_5, "max_tokens": 1000}
        result = apply_thinking_config(kwargs, model=MODEL_OPUS_5, phase=PHASE_REVIEW)
        assert result["thinking"] == {"type": "adaptive"}
        # And for a phase that opts out, the key is absent rather than
        # "disabled" — on Opus 5 that means thinking stays on, which is
        # documented behavior, not an API error.
        opted_out: dict = {"model": MODEL_OPUS_5, "max_tokens": 1000}
        result = apply_thinking_config(
            opted_out, model=MODEL_OPUS_5, phase=PHASE_TRIAGE
        )
        assert "thinking" not in result


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
                MODEL_OPUS_5,
                MODEL_OPUS_48,
                MODEL_SONNET_5,
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
    (Anthropic's recommended starting point for coding/agentic work);
    verification stays medium (Sonnet) / high (Opus escalation) so the
    verdict envelope doesn't balloon. ``xhigh`` is gated per model via
    ``supports_xhigh_effort`` (Opus 5, Opus 4.8 and Sonnet 5 accept it;
    Sonnet 4.6 clamps to ``high`` — see ``TestXhighClampGating``)."""

    def test_review_uses_xhigh(self) -> None:
        # Review defaults to Opus 5, which accepts xhigh.
        assert effort_config_for(model=MODEL_OPUS_5, phase=api_config.PHASE_REVIEW) == {
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
# xhigh is capability-gated — models without supports_xhigh_effort clamp
# ---------------------------------------------------------------------------


class TestXhighClampGating:
    """Regression: ``xhigh`` is rejected at submit (HTTP 400 "This model does
    not support effort level 'xhigh'. Supported levels: high, low, max,
    medium.") by models without the ``supports_xhigh_effort`` capability —
    Sonnet 4.6 among them. When the cross-check phase (``xhigh`` default) ran
    on Sonnet 4.6, every cross-spec coordination pass used to 400 at submit
    and produce zero findings. ``effort_config_for`` must clamp ``xhigh``
    down to ``high`` on any model whose capability entry lacks the flag —
    while passing it through natively on Opus 4.8 and Sonnet 5."""

    def test_cross_check_on_sonnet_46_clamps_to_high(self) -> None:
        # The historical bug: this returned {"effort": "xhigh"} → 400. A
        # pinned SPEC_CRITIC override to Sonnet 4.6 must still clamp.
        assert effort_config_for(
            model=MODEL_SONNET_46, phase=api_config.PHASE_CROSS_CHECK
        ) == {"effort": "high"}

    def test_cross_check_default_model_runs_xhigh_natively(self) -> None:
        # Pin the real wiring: cross-check's default model (Sonnet 5) carries
        # supports_xhigh_effort, so the phase's declared xhigh survives — and
        # is only ever sent to a model whose whitelist entry vouches for it.
        cfg = effort_config_for(
            model=api_config.CROSS_CHECK_MODEL_DEFAULT,
            phase=api_config.PHASE_CROSS_CHECK,
        )
        assert cfg == {"effort": "xhigh"}
        assert model_capabilities(
            api_config.CROSS_CHECK_MODEL_DEFAULT
        ).supports_xhigh_effort is True

    def test_review_on_sonnet_46_override_clamps_to_high(self) -> None:
        # Latent variant: SPEC_CRITIC_REVIEW_MODEL=claude-sonnet-4-6 would
        # otherwise 400.
        assert effort_config_for(
            model=MODEL_SONNET_46, phase=api_config.PHASE_REVIEW
        ) == {"effort": "high"}

    def test_xhigh_capable_models_keep_xhigh_on_deep_phases(self) -> None:
        # Opus 5, Opus 4.8 and Sonnet 5 all accept xhigh — the clamp must
        # not strip it.
        for model in (MODEL_OPUS_5, MODEL_OPUS_48, MODEL_SONNET_5):
            for phase in (
                api_config.PHASE_REVIEW,
                api_config.PHASE_CROSS_CHECK,
                api_config.PHASE_COMPLIANCE,
            ):
                assert effort_config_for(model=model, phase=phase) == {
                    "effort": "xhigh"
                }

    def test_unknown_model_clamps_xhigh(self) -> None:
        # Conservative default capabilities leave supports_xhigh_effort off.
        assert api_config._clamp_effort_for_model("xhigh", "claude-mystery-9") == "high"

    def test_clamp_helper_is_targeted(self) -> None:
        # Only xhigh is clamped, and only on non-flag models; other levels
        # pass through everywhere.
        assert api_config._clamp_effort_for_model("xhigh", MODEL_SONNET_46) == "high"
        assert api_config._clamp_effort_for_model("xhigh", MODEL_OPUS_5) == "xhigh"
        assert api_config._clamp_effort_for_model("xhigh", MODEL_OPUS_48) == "xhigh"
        assert api_config._clamp_effort_for_model("xhigh", MODEL_SONNET_5) == "xhigh"
        assert api_config._clamp_effort_for_model("high", MODEL_SONNET_46) == "high"
        assert api_config._clamp_effort_for_model("medium", MODEL_SONNET_46) == "medium"


# ---------------------------------------------------------------------------
# Sonnet 5 whitelisting — the default Sonnet tier resolves full capabilities
# ---------------------------------------------------------------------------


class TestSonnet5Whitelisted:
    """Sonnet 5 backs verification / cross-check / compliance / research by
    default, so it must resolve to full capabilities — falling through to the
    conservative unknown-model defaults would strip adaptive thinking, effort,
    and strict tools from every one of those phases and clamp output to 64k."""

    def test_registered_not_unknown(self) -> None:
        assert MODEL_SONNET_5 in api_config._MODEL_CAPABILITIES

    def test_capability_helpers_agree(self) -> None:
        assert model_supports_adaptive_thinking(MODEL_SONNET_5) is True
        assert model_supports_effort(MODEL_SONNET_5) is True
        caps = model_capabilities(MODEL_SONNET_5)
        assert caps.supports_strict_tools is True
        assert caps.supports_xhigh_effort is True
        assert caps.context_window == 1_000_000

    def test_output_ceiling_is_128k(self) -> None:
        # Sonnet 5 is the first Sonnet at the Opus ceiling. The legacy
        # family-set dispatch would have clamped it to the 64k
        # previous-generation-Sonnet ceiling.
        assert output_cap_for_model(MODEL_SONNET_5, requested=300_000) == 128_000

    def test_sonnet_46_ceiling_unchanged(self) -> None:
        # The capability-registry refactor of output_cap_for_model must not
        # move any previously-registered model's ceiling.
        assert output_cap_for_model(MODEL_SONNET_46, requested=300_000) == 64_000
        assert output_cap_for_model(MODEL_HAIKU_45, requested=300_000) == 64_000
        assert output_cap_for_model("claude-mystery-9", requested=300_000) == 64_000

    def test_extended_output_beta_confirmed_supported(self) -> None:
        # Previously left False as a conservative placeholder "pending
        # confirmation against the beta's supported-model list". Anthropic's
        # models overview now confirms it: "on the Message Batches API, Claude
        # Opus 5, Opus 4.8, Opus 4.7, Opus 4.6, Sonnet 5, and Sonnet 4.6
        # support up to 300k output tokens by using the
        # output-300k-2026-03-24 beta header."
        assert model_supports_extended_output_beta(MODEL_SONNET_5) is True


# ---------------------------------------------------------------------------
# Default models track the newest generation of each tier
# ---------------------------------------------------------------------------


class TestDefaultModels:
    """Review / escalation default to Opus 5; the Sonnet-tier phases
    (verification initial, cross-check, compliance, research) default to
    Sonnet 5. Pinned so a future model bump is a deliberate, reviewed edit."""

    def test_review_default_is_opus_5(self) -> None:
        # Holds when SPEC_CRITIC_REVIEW_MODEL is unset (the test harness env).
        assert api_config.REVIEW_MODEL_DEFAULT == MODEL_OPUS_5

    def test_escalation_default_is_opus_5(self) -> None:
        assert api_config.VERIFICATION_ESCALATION_MODEL == MODEL_OPUS_5

    def test_initial_verifier_is_sonnet_5(self) -> None:
        # Escalation only fires when initial != escalation model; keep them
        # distinct so the escalation tier stays meaningful.
        assert api_config.VERIFICATION_MODEL_DEFAULT == MODEL_SONNET_5
        assert api_config.VERIFICATION_MODEL_DEFAULT != api_config.VERIFICATION_ESCALATION_MODEL

    def test_sonnet_tier_defaults_are_sonnet_5(self) -> None:
        assert api_config.CROSS_CHECK_MODEL_DEFAULT == MODEL_SONNET_5
        assert api_config.COMPLIANCE_MODEL_DEFAULT == MODEL_SONNET_5
        assert api_config.RESEARCH_MODEL_DEFAULT == MODEL_SONNET_5
