"""Chunk 4 — unified verification routing and request construction.

Verifies the user-visible contracts laid out in the plan:

* :func:`select_routing` is a single selector consumed by every
  verification request path (real-time, batch initial, batch retry,
  batch continuation). Given the same finding, batch and real-time
  builders produce equivalent ``model`` / ``tools`` / ``thinking`` /
  ``output_config`` / ``max_uses`` policy.
* Local-skip findings short-circuit before the request build — the
  builder refuses to be called with a ``local_skip=True`` decision.
* Escalation (``escalated=True``) changes only the model + depth
  fields. Profile, severity, finding identity, web-search inclusion,
  cache phase do not flip.
* The routing decision is JSON-serializable for diagnostics and
  ``request_map`` / ``request_contexts`` round-trips so the wave
  parser stamps results with the same decision the request was
  built against.
* The wave parser reads the stored routing decision from
  ``request_contexts`` rather than re-deriving from the finding (the
  pre-Chunk-4 path could disagree with what the request actually ran).
"""
from __future__ import annotations

import json

import pytest

from src.api_config import (
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    PHASE_VERIFICATION,
    PHASE_VERIFICATION_CONTINUATION,
    PHASE_VERIFICATION_RETRY,
)
from src.reviewer import Finding
from src.verification_modes import VerificationMode
from src.verification_profiles import VerificationProfile
from src.verification_routing import (
    TRACE_DEFAULT_STANDARD,
    TRACE_ESCALATED,
    TRACE_GRIPES_STRICT,
    TRACE_LOCAL_SKIP,
    VerificationRoutingDecision,
    apply_routing_to_result,
    build_verification_request,
    build_verification_tools_from_decision,
    select_routing,
)


# ---------------------------------------------------------------------------
# Helpers — small finding factories so the assertions read like the plan.
# ---------------------------------------------------------------------------


def _finding(
    *,
    severity: str = "MEDIUM",
    code_ref: str | None = None,
    issue: str = "Generic technical claim about pipe slope.",
    section: str = "2.1",
    action: str = "EDIT",
    filename: str = "23 21 13 - Hydronic.docx",
    existing: str | None = None,
    replacement: str | None = None,
) -> Finding:
    return Finding(
        severity=severity,
        fileName=filename,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_ref,
        confidence=0.6,
    )


# ---------------------------------------------------------------------------
# 1. The decision is fully populated and JSON-serializable.
# ---------------------------------------------------------------------------


class TestDecisionSerialization:
    def test_to_dict_emits_all_fields(self):
        d = select_routing(
            _finding(severity="HIGH", code_ref="CBC 2025 1019.3"),
            local_skip=False,
        )
        payload = d.to_dict()
        # Every field the plan calls out for the routing decision must
        # be present in the serialized form.
        for key in (
            "finding_id",
            "severity",
            "profile",
            "mode",
            "model",
            "thinking_enabled",
            "web_search_enabled",
            "web_search_max_uses",
            "include_verdict_tool",
            "cache_phase",
            "max_continuations",
            "escalation_eligible",
            "local_skip",
            "escalated",
            "trace_reason",
        ):
            assert key in payload, f"Decision dict missing {key}"

    def test_to_dict_is_json_safe(self):
        d = select_routing(_finding(severity="HIGH", code_ref="CBC 2025 1019.3"))
        # The whole point of ``to_dict`` is that the decision can be
        # stashed in request_map or dropped into a diagnostics event —
        # both consume plain JSON. ``json.dumps`` is the canonical
        # cross-check.
        encoded = json.dumps(d.to_dict())
        decoded = json.loads(encoded)
        assert decoded["mode"] == d.mode.value
        assert decoded["profile"] == d.profile.value

    def test_from_dict_round_trips(self):
        original = select_routing(
            _finding(severity="HIGH", code_ref="CBC 2025 1019.3"),
            escalated=True,
        )
        rebuilt = VerificationRoutingDecision.from_dict(original.to_dict())
        # Field-by-field equality is more useful than ``==`` here because
        # a future field addition should surface as a specific assertion
        # failure rather than a giant struct mismatch.
        assert rebuilt.mode is original.mode
        assert rebuilt.profile is original.profile
        assert rebuilt.model == original.model
        assert rebuilt.thinking_enabled == original.thinking_enabled
        assert rebuilt.web_search_max_uses == original.web_search_max_uses
        assert rebuilt.escalated == original.escalated
        assert rebuilt.cache_phase == original.cache_phase

    def test_from_dict_legacy_partial_payload(self):
        # A legacy / pre-Chunk-4 caller might stash an incomplete dict
        # (missing fields). The constructor falls back to safe defaults
        # so the wave parser does not crash on legacy data.
        rebuilt = VerificationRoutingDecision.from_dict({})
        assert rebuilt.mode is VerificationMode.STANDARD_REASONING
        assert rebuilt.profile is VerificationProfile.CONSTRUCTABILITY
        assert rebuilt.local_skip is False


# ---------------------------------------------------------------------------
# 2. Selector produces a single source of truth.
# ---------------------------------------------------------------------------


class TestSelectorContract:
    def test_same_finding_produces_same_decision(self):
        # The plan's primary acceptance criterion: given the same
        # finding, the selector must return the same decision regardless
        # of which caller asks (real-time, batch initial, batch retry).
        f = _finding(severity="HIGH", code_ref="CBC 2025 1019.3")
        d1 = select_routing(f)
        d2 = select_routing(f)
        assert d1.mode is d2.mode
        assert d1.profile is d2.profile
        assert d1.model == d2.model
        assert d1.web_search_max_uses == d2.web_search_max_uses
        assert d1.thinking_enabled == d2.thinking_enabled

    def test_gripes_routes_to_strict_structured(self):
        # The plan's example of where batch used to drift from real-time:
        # a GRIPES-severity finding should get STRICT_STRUCTURED policy,
        # not the default STANDARD_REASONING bundle.
        d = select_routing(_finding(severity="GRIPES", code_ref="CBC 2025"))
        assert d.mode is VerificationMode.STRICT_STRUCTURED
        # STRICT_STRUCTURED opts out of thinking (cheap path), so the
        # decision should record that even on Sonnet which supports it.
        assert d.thinking_enabled is False
        # Half-budget on the CODE_STANDARD/GRIPES tier (ceiling 3) gives
        # round(3*0.5) = 2 with the floor-of-1 fallback.
        assert d.web_search_max_uses == 2

    def test_standard_high_finding_keeps_full_budget(self):
        d = select_routing(_finding(severity="HIGH", code_ref="CBC 2025"))
        assert d.mode is VerificationMode.STANDARD_REASONING
        assert d.thinking_enabled is True
        # CODE_STANDARD HIGH ceiling is 7; STANDARD_REASONING multiplier
        # is 1.0 so the full budget passes through unchanged.
        assert d.web_search_max_uses == 7

    def test_local_skip_routes_to_local_skip_mode(self):
        # The keyword classifier in :mod:`verification_router` routes
        # placeholder GRIPES findings to local-skip. The decision must
        # record that and disable web search so the builder refuses
        # to construct a remote request.
        d = select_routing(
            _finding(severity="GRIPES", issue="placeholder text [INSERT VALUE]"),
            local_skip=True,
        )
        assert d.mode is VerificationMode.LOCAL_SKIP
        assert d.web_search_enabled is False
        assert d.web_search_max_uses == 0
        assert d.local_skip is True

    def test_explicit_local_skip_false_skips_classifier(self):
        # Callers that have already run the local-skip classifier (e.g.
        # ``prepare_findings_for_verification``) should pass
        # ``local_skip=False`` so the selector does not re-run the
        # classifier and incorrectly route a passed-through finding to
        # LOCAL_SKIP. A placeholder finding with explicit
        # ``local_skip=False`` should still flow through the regular
        # routing rules.
        d = select_routing(
            _finding(severity="GRIPES", issue="placeholder text [INSERT VALUE]"),
            local_skip=False,
        )
        # The placeholder keyword should NOT have triggered local-skip
        # because the caller said "don't classify". GRIPES severity
        # still routes the finding to STRICT_STRUCTURED.
        assert d.mode is VerificationMode.STRICT_STRUCTURED
        assert d.local_skip is False

    def test_escalation_forces_deep_reasoning(self):
        # Escalation rewrites the mode + model but does not touch
        # severity, profile, finding identity, web-search inclusion,
        # or cache phase. Use a high-severity finding that would
        # otherwise be STANDARD_REASONING.
        f = _finding(severity="HIGH", code_ref="CBC 2025 1019.3")
        initial = select_routing(f, escalated=False)
        escalated = select_routing(f, escalated=True)

        # Mode + model flip on escalation.
        assert initial.mode is VerificationMode.STANDARD_REASONING
        assert escalated.mode is VerificationMode.DEEP_REASONING
        assert escalated.model != initial.model  # Opus, not Sonnet
        # Trace reason updates.
        assert escalated.trace_reason == TRACE_ESCALATED
        # But the *non-routing* fields stay identical so the request
        # builder produces the same prompt + tools layout.
        assert escalated.profile is initial.profile
        assert escalated.severity == initial.severity
        assert escalated.web_search_enabled is True
        assert escalated.include_verdict_tool == initial.include_verdict_tool
        # Both decisions are eligible to include the verdict tool; only
        # the request builder differs in how it caches the prefix.

    def test_model_override_wins(self):
        # Operator override / test-explicit model must win over the
        # mode's default model. Routing rules otherwise unchanged.
        d = select_routing(
            _finding(severity="HIGH", code_ref="CBC 2025"),
            model_override=MODEL_OPUS_47,
        )
        assert d.model == MODEL_OPUS_47

    def test_trace_reasons_are_legible(self):
        # The trace_reason field is for diagnostics — make sure each
        # of the major routing branches stamps a distinct tag.
        local_skip = select_routing(
            _finding(severity="GRIPES", issue="LEED Gold"), local_skip=True
        )
        assert local_skip.trace_reason == TRACE_LOCAL_SKIP

        gripes = select_routing(_finding(severity="GRIPES"))
        assert gripes.trace_reason == TRACE_GRIPES_STRICT

        std = select_routing(_finding(severity="HIGH", code_ref="CBC 2025"))
        assert std.trace_reason == TRACE_DEFAULT_STANDARD

        esc = select_routing(_finding(severity="HIGH"), escalated=True)
        assert esc.trace_reason == TRACE_ESCALATED


# ---------------------------------------------------------------------------
# 3. Builder produces the production request shape.
# ---------------------------------------------------------------------------


class TestBuilderShape:
    def _decision_for(self, **kwargs) -> VerificationRoutingDecision:
        return select_routing(_finding(**kwargs))

    def test_builder_refuses_local_skip_decision(self):
        # A local-skip decision means "no remote request"; calling the
        # builder with one is a caller bug. The plan acceptance:
        # "Local-skip findings do not build external verification
        # requests."
        decision = select_routing(
            _finding(severity="GRIPES", issue="LEED Gold"), local_skip=True
        )
        with pytest.raises(ValueError, match="local-skip"):
            build_verification_request(
                decision, prompt="verify", system_prompt="system"
            )

    def test_builder_emits_messages_and_max_tokens(self):
        decision = self._decision_for(severity="HIGH", code_ref="CBC 2025")
        params = build_verification_request(
            decision, prompt="verify this", system_prompt="be careful"
        )
        assert params["model"] == decision.model
        assert params["messages"][0]["role"] == "user"
        assert params["messages"][0]["content"] == "verify this"
        assert "max_tokens" in params
        # The decision says thinking is on for STANDARD_REASONING +
        # Sonnet, so the builder must add the key.
        assert params.get("thinking") == {"type": "adaptive"}

    def test_builder_omits_thinking_for_strict_structured(self):
        # STRICT_STRUCTURED opts out of thinking even on a model that
        # supports it. The builder reads the decision's intent, not
        # the model's capability in isolation.
        decision = self._decision_for(severity="GRIPES", code_ref="CBC 2025")
        params = build_verification_request(
            decision, prompt="verify", system_prompt="system"
        )
        assert "thinking" not in params

    def test_builder_adds_assistant_content_for_continuation(self):
        decision = self._decision_for(severity="HIGH", code_ref="CBC 2025")
        params = build_verification_request(
            decision,
            prompt="verify",
            system_prompt="system",
            assistant_content=[{"type": "text", "text": "..."}],
        )
        assert [m["role"] for m in params["messages"]] == ["user", "assistant"]

    def test_builder_attaches_web_search_with_mode_scaled_uses(self):
        decision = self._decision_for(severity="GRIPES", code_ref="CBC 2025")
        params = build_verification_request(
            decision, prompt="verify", system_prompt="system"
        )
        web = next(
            t for t in params["tools"]
            if (t.get("type") or "").startswith("web_search_")
        )
        # STRICT_STRUCTURED on a CODE_STANDARD/GRIPES tier: round(3*0.5) = 2.
        assert web["max_uses"] == decision.web_search_max_uses == 2

    def test_builder_omits_verdict_tool_when_decision_says_so(self):
        # The decision is the source of truth for tool inclusion. If
        # the structured-output flag is on but the decision was built
        # with ``include_verdict_tool=False`` (test override), the
        # builder must respect that.
        decision = self._decision_for(severity="HIGH", code_ref="CBC 2025")
        from dataclasses import replace
        no_verdict = replace(decision, include_verdict_tool=False)
        params = build_verification_request(
            no_verdict, prompt="verify", system_prompt="system"
        )
        tool_names = [t.get("name") for t in params["tools"]]
        assert "submit_verification_verdict" not in tool_names

    def test_builder_attaches_service_tier_for_batch(self, monkeypatch):
        # Batch submissions opt into ``service_tier`` via the
        # ``include_service_tier`` kwarg. Real-time streaming does not.
        from src.api_config import batch_service_tier
        decision = self._decision_for(severity="HIGH", code_ref="CBC 2025")
        params_batch = build_verification_request(
            decision, prompt="verify", system_prompt="system",
            include_service_tier=True,
        )
        params_stream = build_verification_request(
            decision, prompt="verify", system_prompt="system",
            include_service_tier=False,
        )
        if batch_service_tier():
            assert params_batch.get("service_tier") == batch_service_tier()
        assert "service_tier" not in params_stream


# ---------------------------------------------------------------------------
# 4. Real-time and batch produce equivalent shapes for the same finding.
# ---------------------------------------------------------------------------


def _normalize_params_for_compare(params: dict) -> dict:
    """Strip non-routing fields so two request payloads can be compared.

    ``service_tier`` differs between streaming and batch by design (the
    streaming endpoint rejects the field). Cache-control wrappers carry
    runtime-only metadata that does not affect routing equivalence. We
    drop both before comparing so the test fails only on real drift.
    """
    out = {k: v for k, v in params.items() if k != "service_tier"}
    # Drop cache_control blocks — they're pricing hints, not routing.
    sys_val = out.get("system")
    if isinstance(sys_val, list):
        out["system"] = [
            {k: v for k, v in block.items() if k != "cache_control"}
            for block in sys_val
        ]
    tools = out.get("tools")
    if isinstance(tools, list):
        out["tools"] = [
            {k: v for k, v in tool.items() if k != "cache_control"}
            for tool in tools
        ]
    return out


class TestRealtimeAndBatchParity:
    """Plan acceptance: "Given the same finding, batch and real-time
    builders produce equivalent model/tools/thinking/effort/search-
    budget policy."
    """

    def test_same_finding_same_shape(self):
        finding = _finding(severity="HIGH", code_ref="CBC 2025 1019.3")
        decision = select_routing(finding)

        rt = build_verification_request(
            decision,
            prompt="verify this finding",
            system_prompt="verifier system",
            include_service_tier=False,
        )
        bt = build_verification_request(
            decision,
            prompt="verify this finding",
            system_prompt="verifier system",
            include_service_tier=True,
        )
        # Equal up to ``service_tier`` and cache wrappers.
        assert _normalize_params_for_compare(rt) == _normalize_params_for_compare(bt)

    def test_gripes_finding_same_strict_structured_shape(self):
        finding = _finding(severity="GRIPES", code_ref="CBC 2025")
        decision = select_routing(finding)
        rt = build_verification_request(
            decision, prompt="verify", system_prompt="system",
            include_service_tier=False,
        )
        bt = build_verification_request(
            decision, prompt="verify", system_prompt="system",
            include_service_tier=True,
        )
        assert _normalize_params_for_compare(rt) == _normalize_params_for_compare(bt)
        # And both omit thinking because the mode policy says so.
        assert "thinking" not in rt
        assert "thinking" not in bt


# ---------------------------------------------------------------------------
# 5. Tools come back with the correct shape.
# ---------------------------------------------------------------------------


class TestToolsFromDecision:
    def test_web_search_max_uses_matches_decision(self):
        d = select_routing(_finding(severity="HIGH", code_ref="CBC 2025"))
        tools = build_verification_tools_from_decision(d)
        web = next(
            t for t in tools
            if (t.get("type") or "").startswith("web_search_")
        )
        assert web["max_uses"] == d.web_search_max_uses

    def test_verdict_tool_dropped_when_disabled(self):
        d = select_routing(_finding(severity="HIGH", code_ref="CBC 2025"))
        from dataclasses import replace
        no_verdict = replace(d, include_verdict_tool=False)
        tools = build_verification_tools_from_decision(no_verdict)
        tool_names = [t.get("name") for t in tools]
        assert "submit_verification_verdict" not in tool_names

    def test_verdict_tool_included_when_enabled(self):
        d = select_routing(_finding(severity="HIGH", code_ref="CBC 2025"))
        if not d.include_verdict_tool:
            pytest.skip("structured tool output disabled in environment")
        tools = build_verification_tools_from_decision(d)
        tool_names = [t.get("name") for t in tools]
        assert "submit_verification_verdict" in tool_names


# ---------------------------------------------------------------------------
# 6. Result stamping uses the decision.
# ---------------------------------------------------------------------------


class TestResultStamping:
    def test_stamps_mode_and_profile(self):
        from src.verifier import VerificationResult
        d = select_routing(_finding(severity="GRIPES", code_ref="CBC 2025"))
        result = VerificationResult(verdict="UNVERIFIED")
        apply_routing_to_result(d, result)
        assert result.verification_mode == d.mode.value
        assert result.verification_profile == d.profile.value

    def test_stamps_escalated_flag(self):
        from src.verifier import VerificationResult
        d = select_routing(
            _finding(severity="HIGH", code_ref="CBC 2025"), escalated=True
        )
        result = VerificationResult(verdict="UNVERIFIED", escalated=False)
        apply_routing_to_result(d, result)
        assert result.escalated is True

    def test_handles_none_result(self):
        # Defensive — the wave parser sometimes operates on a missing
        # outcome. The stamping helper should no-op rather than crash.
        d = select_routing(_finding(severity="HIGH", code_ref="CBC 2025"))
        apply_routing_to_result(d, None)  # should not raise


# ---------------------------------------------------------------------------
# 7. The retry / continuation builders route through the same selector.
# ---------------------------------------------------------------------------


class TestRetryAndContinuationRouteThroughSelector:
    def test_retry_request_inherits_finding_routing(self, monkeypatch):
        # When a finding is threaded through, the retry builder must
        # apply the same mode/profile policy as the initial call would.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        from src.code_cycles import DEFAULT_CYCLE
        from src.verifier import _build_retry_request

        finding = _finding(severity="GRIPES", code_ref="CBC 2025")
        retry_params = _build_retry_request(
            "verify",
            cycle=DEFAULT_CYCLE,
            finding=finding,
        )
        # STRICT_STRUCTURED → no thinking, max_uses = 2 on CODE_STANDARD/GRIPES.
        assert "thinking" not in retry_params
        web = next(
            t for t in retry_params["tools"]
            if (t.get("type") or "").startswith("web_search_")
        )
        assert web["max_uses"] == 2

    def test_continuation_request_inherits_finding_routing(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        from src.code_cycles import DEFAULT_CYCLE
        from src.verifier import _build_continuation_request

        finding = _finding(severity="HIGH", code_ref="CBC 2025 1019.3")
        params = _build_continuation_request(
            "verify",
            [{"type": "text", "text": "..."}],
            cycle=DEFAULT_CYCLE,
            finding=finding,
        )
        # STANDARD_REASONING on Sonnet → thinking on, full CODE_STANDARD/HIGH budget (7).
        assert params.get("thinking") == {"type": "adaptive"}
        web = next(
            t for t in params["tools"]
            if (t.get("type") or "").startswith("web_search_")
        )
        assert web["max_uses"] == 7
        # Continuation appends an assistant turn.
        assert [m["role"] for m in params["messages"]] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# 8. Submission stashes the routing decision so the wave parser can read it.
# ---------------------------------------------------------------------------


class TestRoutingPersistsInRequestMap:
    def test_submit_verification_batch_stashes_routing_decision(self, monkeypatch):
        """The batch path must serialize the routing decision into
        ``request_map`` so the wave parser can later stamp results
        with the same decision that produced the request.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

        # Lightweight fake client so we don't make any network calls.
        class _FakeBatchesResult:
            id = "fake-batch-id"

        class _FakeBatches:
            def create(self, requests=None, **_):
                return _FakeBatchesResult()

        class _FakeMessages:
            def __init__(self):
                self.batches = _FakeBatches()

        class _FakeClient:
            def __init__(self):
                self.messages = _FakeMessages()

        fake = _FakeClient()
        from src import batch as batch_mod
        monkeypatch.setattr(batch_mod, "_get_client", lambda: fake)

        finding = _finding(severity="GRIPES", code_ref="CBC 2025")

        def _prompt(_):
            return "verify"

        def _system(_):
            return "system"

        job = batch_mod.submit_verification_batch(
            [finding], _prompt, _system,
        )
        # The job's request_map must carry a serialized routing decision
        # for every submitted finding.
        assert len(job.request_map) == 1
        meta = next(iter(job.request_map.values()))
        assert "routing" in meta, (
            "submit_verification_batch must stash the routing decision "
            "in request_map so the wave parser can read it back."
        )
        routing = meta["routing"]
        # Round-trips through the dataclass constructor.
        rebuilt = VerificationRoutingDecision.from_dict(routing)
        assert rebuilt.mode is VerificationMode.STRICT_STRUCTURED
        assert rebuilt.profile is VerificationProfile.CODE_STANDARD


# ---------------------------------------------------------------------------
# 9. Cache phases flow through the decision.
# ---------------------------------------------------------------------------


class TestCachePhaseFlowsThroughDecision:
    def test_initial_decision_uses_verification_phase(self):
        d = select_routing(_finding(severity="HIGH", code_ref="CBC 2025"))
        assert d.cache_phase == PHASE_VERIFICATION

    def test_retry_decision_uses_verification_retry_phase(self):
        d = select_routing(
            _finding(severity="HIGH", code_ref="CBC 2025"),
            cache_phase=PHASE_VERIFICATION_RETRY,
        )
        assert d.cache_phase == PHASE_VERIFICATION_RETRY

    def test_continuation_decision_uses_verification_continuation_phase(self):
        d = select_routing(
            _finding(severity="HIGH", code_ref="CBC 2025"),
            cache_phase=PHASE_VERIFICATION_CONTINUATION,
        )
        assert d.cache_phase == PHASE_VERIFICATION_CONTINUATION
