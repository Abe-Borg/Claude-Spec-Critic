"""B-1: STRICT_STRUCTURED is cheap through *effort*, not through "thinking off".

On the current model generation an omitted ``thinking`` key means adaptive
thinking ON, so the mode's historical ``thinking_enabled=False`` never made
it cheap — it thought at the phase default (``medium``) inside the 16k
verification cap. Sending ``{"type": "disabled"}`` is not the fix (documented
tool-call-as-text failure modes on the escalation tier, 400 above effort
``high``). The fix is a mode-level effort: STRICT_STRUCTURED pins ``low``
via ``ModePolicy.effort``; every other mode keeps the phase default; the
override still rides through the per-model capability gate and clamp.
"""
from __future__ import annotations

import pytest

from src.core.api_config import (
    EFFORT_LOW,
    MODEL_HAIKU_45,
    MODEL_OPUS_5,
    MODEL_SONNET_5,
    PHASE_VERIFICATION,
    PHASE_VERIFICATION_CONTINUATION,
    PHASE_VERIFICATION_RETRY,
    VERIFICATION_ESCALATION_MODEL,
)
from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.verification_modes import VerificationMode, mode_policy
from src.verification.verification_routing import (
    TRACE_GRIPES_STRICT,
    TRACE_INTERNAL_COORD_STRICT,
    build_verification_request,
    select_routing,
)


def _finding(
    severity: str,
    *,
    code_ref: str | None = "NFPA 13 §10.2.4",
    issue: str = "Sprinkler spacing cited exceeds the maximum for the listed hazard class.",
) -> Finding:
    return Finding(
        severity=severity,
        fileName="21 13 13 - Wet-Pipe Sprinkler.docx",
        section="2.4",
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=code_ref,
        confidence=0.5,
    )


def _request(finding: Finding, **routing_kwargs):
    decision = select_routing(
        finding, local_skip=False, cycle=DEFAULT_CYCLE, **routing_kwargs
    )
    request = build_verification_request(
        decision, prompt="verify this", system_prompt="you verify"
    )
    return decision, request.params


# ---------------------------------------------------------------------------
# 1. Mode policy table
# ---------------------------------------------------------------------------


class TestModePolicyEffort:
    def test_strict_structured_pins_low(self) -> None:
        assert EFFORT_LOW == "low"
        assert mode_policy(VerificationMode.STRICT_STRUCTURED).effort == EFFORT_LOW

    @pytest.mark.parametrize(
        "mode",
        [
            VerificationMode.LOCAL_SKIP,
            VerificationMode.STANDARD_REASONING,
            VerificationMode.DEEP_REASONING,
        ],
    )
    def test_other_modes_defer_to_the_phase_default(self, mode) -> None:
        assert mode_policy(mode).effort is None

    def test_no_mode_declares_above_high(self) -> None:
        # Same ceiling ``test_no_phase_exceeds_high`` pins for phases.
        allowed = {None, "low", "medium", "high"}
        for mode in VerificationMode:
            assert mode_policy(mode).effort in allowed, mode

    def test_strict_structured_does_not_request_explicit_thinking(self) -> None:
        # The flag means "omit the key", which on current models still runs
        # adaptive thinking — the cheapness comes from ``effort``.
        policy = mode_policy(VerificationMode.STRICT_STRUCTURED)
        assert policy.thinking_enabled is False
        assert policy.effort == "low"

    def test_unknown_mode_string_falls_back_with_no_override(self) -> None:
        assert mode_policy("not-a-mode").effort is None


# ---------------------------------------------------------------------------
# 2. Request-level pins through the single request builder
# ---------------------------------------------------------------------------


class TestRequestEffort:
    def test_strict_structured_request_carries_low_effort(self) -> None:
        decision, params = _request(_finding("GRIPES"))
        assert decision.mode is VerificationMode.STRICT_STRUCTURED
        assert decision.trace_reason == TRACE_GRIPES_STRICT
        assert params["model"] == MODEL_SONNET_5
        assert params["output_config"] == {"effort": "low"}
        # The key is omitted (adaptive stays on) — never ``disabled``.
        assert "thinking" not in params

    def test_internal_coordination_strict_also_low(self) -> None:
        decision, params = _request(
            _finding(
                "HIGH",
                code_ref="",
                issue="Internal contradiction between paragraph 2.1 and 2.4 "
                "on the required pipe schedule.",
            )
        )
        assert decision.mode is VerificationMode.STRICT_STRUCTURED
        assert decision.trace_reason == TRACE_INTERNAL_COORD_STRICT
        assert params["output_config"] == {"effort": "low"}
        assert "thinking" not in params

    def test_standard_reasoning_keeps_phase_default(self) -> None:
        decision, params = _request(_finding("HIGH"))
        assert decision.mode is VerificationMode.STANDARD_REASONING
        assert params["output_config"] == {"effort": "medium"}
        assert params["thinking"] == {"type": "adaptive"}

    def test_deep_reasoning_keeps_high(self) -> None:
        decision, params = _request(_finding("CRITICAL"), escalated=True)
        assert decision.mode is VerificationMode.DEEP_REASONING
        assert params["model"] == VERIFICATION_ESCALATION_MODEL
        assert params["output_config"] == {"effort": "high"}
        assert params["thinking"] == {"type": "adaptive"}

    @pytest.mark.parametrize(
        "phase", [PHASE_VERIFICATION, PHASE_VERIFICATION_RETRY, PHASE_VERIFICATION_CONTINUATION]
    )
    def test_mode_effort_survives_every_verification_phase(self, phase) -> None:
        # Retry-wave and continuation requests are built through the same
        # builder with a different cache phase; the mode override must not
        # silently revert to the phase default on those paths.
        _, strict = _request(_finding("GRIPES"), cache_phase=phase)
        _, standard = _request(_finding("HIGH"), cache_phase=phase)
        assert strict["output_config"] == {"effort": "low"}
        assert standard["output_config"] == {"effort": "medium"}

    def test_mode_effort_wins_over_the_verification_opus_bump(self) -> None:
        # An operator override to the escalation-tier model on a
        # STRICT_STRUCTURED finding is still the cheap path: the mode, not
        # the model, decides the effort. (Only reachable via overrides —
        # the mode policy itself pins the initial-tier model.)
        _, params = _request(_finding("GRIPES"), model_override=MODEL_OPUS_5)
        assert params["model"] == MODEL_OPUS_5
        assert params["output_config"] == {"effort": "low"}

    def test_mode_effort_respects_the_capability_gate(self) -> None:
        # A model that does not support effort omits ``output_config``
        # entirely — the override never bypasses the whitelist.
        _, params = _request(_finding("GRIPES"), model_override=MODEL_HAIKU_45)
        assert "output_config" not in params
        assert "thinking" not in params

    def test_no_request_ever_sends_disabled_thinking(self) -> None:
        for finding, kwargs in (
            (_finding("GRIPES"), {}),
            (_finding("HIGH"), {}),
            (_finding("CRITICAL"), {"escalated": True}),
        ):
            _, params = _request(finding, **kwargs)
            assert params.get("thinking") != {"type": "disabled"}
