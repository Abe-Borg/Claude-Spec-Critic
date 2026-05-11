"""Chunk J — phase-aware prompt caching policy and per-phase telemetry.

Two halves:

1. **Cache policy.** The pre-Chunk-J helpers always wrapped system prompts and
   tool lists with cache_control whenever ``SPEC_CRITIC_PROMPT_CACHE`` was
   on, even for tiny one-off prompts that could never produce a cache hit
   (synthesis ~425 tokens, triage ~375 tokens — both under Anthropic's
   1024-token cache minimum). Chunk J adds a centralized per-phase policy
   so caching is opt-in by phase, with the directive-driven defaults baked
   into the registry (synthesis off, triage off, everything else on).

2. **Telemetry.** ``DiagnosticsReport.record_api_call`` standardizes the
   per-call event payload so the per-phase rollup in ``summary()`` can
   answer the questions the directive asks ("which phases cost the most?",
   "which phases get cache hits?", "how many retries / continuations
   occurred?"). The ``to_text()`` rendering surfaces the same data so
   operators can spot hot-spots from a saved report.
"""
from __future__ import annotations

import importlib

import pytest

from src import api_config
from src.api_config import (
    CachePolicy,
    PHASE_BATCH_REVIEW,
    PHASE_CROSS_CHECK,
    PHASE_REVIEW,
    PHASE_SYNTHESIS,
    PHASE_TRIAGE,
    PHASE_VERIFICATION,
    PHASE_VERIFICATION_CONTINUATION,
    PHASE_VERIFICATION_RETRY,
    cache_policy_for,
    system_prompt_with_cache,
    tools_with_cache,
)
from src.diagnostics import DiagnosticsReport


# ---------------------------------------------------------------------------
# Cache policy registry
# ---------------------------------------------------------------------------


class TestCachePolicyDefaults:
    """The default registry encodes the directive-driven decisions."""

    @pytest.mark.parametrize(
        "phase",
        [
            PHASE_REVIEW,
            PHASE_BATCH_REVIEW,
            PHASE_CROSS_CHECK,
            PHASE_VERIFICATION,
            PHASE_VERIFICATION_RETRY,
            PHASE_VERIFICATION_CONTINUATION,
        ],
    )
    def test_high_value_phases_cache_both_system_and_tools(self, phase):
        policy = cache_policy_for(phase)
        assert policy.cache_system is True, f"expected {phase} to cache system prompt"
        assert policy.cache_tools is True, f"expected {phase} to cache tools"

    @pytest.mark.parametrize("phase", [PHASE_SYNTHESIS, PHASE_TRIAGE])
    def test_short_one_off_phases_skip_caching(self, phase):
        policy = cache_policy_for(phase)
        assert policy.cache_system is False, f"expected {phase} to skip system caching"
        assert policy.cache_tools is False, f"expected {phase} to skip tool caching"
        assert not policy.caches_anything

    def test_unknown_phase_falls_back_to_default(self):
        # Re-import via attribute lookup so a sibling test that reloaded
        # ``src.api_config`` (test_chunk_i does this) doesn't leave us
        # comparing against a stale class identity.
        policy = api_config.cache_policy_for("__brand_new_phase__")
        # Default is "cache it" so a forgotten registry entry doesn't lose
        # the cache opportunity. Triage / synthesis must register explicitly.
        assert policy.cache_system is True
        assert policy.cache_tools is True
        assert hasattr(policy, "ttl")

    def test_none_phase_returns_default(self):
        # Legacy callers that have not been migrated to phase-aware caching
        # still get the pre-Chunk-J behavior.
        legacy = cache_policy_for(None)
        assert legacy.cache_system is True
        assert legacy.cache_tools is True


class TestCachePolicyEnvOverride:
    """``SPEC_CRITIC_CACHE_DISABLE`` lets operators turn off individual phases."""

    def test_unset_env_leaves_defaults(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_CACHE_DISABLE", raising=False)
        policy = cache_policy_for(PHASE_REVIEW)
        assert policy.cache_system is True

    def test_disable_single_phase(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_CACHE_DISABLE", "review")
        policy = cache_policy_for(PHASE_REVIEW)
        assert policy.cache_system is False
        assert policy.cache_tools is False
        # Other phases unaffected.
        assert cache_policy_for(PHASE_VERIFICATION).cache_system is True

    def test_disable_multiple_phases_with_whitespace_and_case(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_CACHE_DISABLE", " VERIFICATION , Cross_Check ")
        assert cache_policy_for(PHASE_VERIFICATION).cache_system is False
        assert cache_policy_for(PHASE_CROSS_CHECK).cache_system is False
        # Review still cached.
        assert cache_policy_for(PHASE_REVIEW).cache_system is True


# ---------------------------------------------------------------------------
# Cache helper integration with the policy
# ---------------------------------------------------------------------------


class TestSystemPromptWithCachePhaseAware:
    """The helper consults the phase policy when the keyword is supplied."""

    def test_review_phase_returns_cache_blocks(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        payload = system_prompt_with_cache("system text", phase=PHASE_REVIEW)
        assert isinstance(payload, list)
        assert payload[0]["text"] == "system text"
        assert payload[0]["cache_control"]["type"] == "ephemeral"

    def test_synthesis_phase_returns_plain_string(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        # Synthesis is below the cache minimum and runs once per run.
        payload = system_prompt_with_cache("system text", phase=PHASE_SYNTHESIS)
        assert payload == "system text"

    def test_triage_phase_returns_plain_string(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        payload = system_prompt_with_cache("system text", phase=PHASE_TRIAGE)
        assert payload == "system text"

    def test_global_disable_short_circuits_phase_policy(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "0")
        # Phase policy says cache, but the global flag is off.
        payload = system_prompt_with_cache("system text", phase=PHASE_REVIEW)
        assert payload == "system text"

    def test_no_phase_keyword_keeps_legacy_behavior(self, monkeypatch):
        # Backward-compat: a caller that hasn't been migrated still gets the
        # cache-by-default behavior so the wiring rollout is incremental.
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        payload = system_prompt_with_cache("system text")
        assert isinstance(payload, list)
        assert payload[0]["cache_control"]["type"] == "ephemeral"


class TestToolsWithCachePhaseAware:
    def test_review_phase_marks_last_tool(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        tools = tools_with_cache(
            [{"name": "a"}, {"name": "b"}], phase=PHASE_BATCH_REVIEW
        )
        assert "cache_control" not in tools[0]
        assert tools[1]["cache_control"]["type"] == "ephemeral"

    def test_synthesis_phase_no_cache_tag(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        tools = tools_with_cache([{"name": "a"}], phase=PHASE_SYNTHESIS)
        assert "cache_control" not in tools[0]

    def test_triage_phase_no_cache_tag(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        tools = tools_with_cache([{"name": "a"}], phase=PHASE_TRIAGE)
        assert "cache_control" not in tools[0]

    def test_env_override_disables_review_caching(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        monkeypatch.setenv("SPEC_CRITIC_CACHE_DISABLE", "batch_review")
        tools = tools_with_cache([{"name": "a"}], phase=PHASE_BATCH_REVIEW)
        assert "cache_control" not in tools[0]


# ---------------------------------------------------------------------------
# Wiring: every production call site requests the right phase policy
# ---------------------------------------------------------------------------


class TestProductionCallSiteWiring:
    """Smoke check: each production module routes through the phase parameter.

    These tests exercise the request-builder seams (not full end-to-end calls)
    and assert that the constructed payload reflects the per-phase policy.
    A future refactor that drops a ``phase=`` keyword would silently revert
    that call site to the legacy default; this catches the regression.
    """

    def test_triage_request_omits_cache_control(self, monkeypatch):
        # Triage builder: rebuild it without monkey-patching the API key.
        # We only need the ``request_kwargs`` shape.
        from src import triage as triage_mod
        from src.reviewer import Finding

        captured: dict = {}

        class _FakeMessages:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                # Return a no-op response shape; the test only inspects kwargs.
                class _Resp:
                    content = []
                return _Resp()

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        monkeypatch.setattr(triage_mod, "_get_client", lambda: _FakeClient())

        finding = Finding(
            severity="GRIPES", fileName="x.docx", section="1.0",
            issue="placeholder",
            actionType="EDIT", existingText=None, replacementText=None,
            codeReference=None,
        )
        triage_mod._classify_batch([(0, finding)], model="claude-haiku-4-5")

        # System should be the plain prompt string (not a cache block).
        assert isinstance(captured["system"], str)
        # The single tool entry should have no cache_control.
        assert captured["tools"]
        assert "cache_control" not in captured["tools"][0]

    def test_synthesis_request_omits_cache_control(self, fake_client_for_phase):
        from src.cross_checker import _run_cross_discipline_synthesis
        from src.code_cycles import DEFAULT_CYCLE
        from src.reviewer import Finding, ReviewResult

        # Two completed chunks with a finding apiece — synthesis only runs
        # when there are at least two chunks to correlate.
        chunk_a = ReviewResult(
            findings=[Finding(
                severity="HIGH", fileName="22.docx", section="2.1",
                issue="finding A", actionType="EDIT",
                existingText=None, replacementText=None, codeReference=None,
            )],
            cross_check_status="completed",
            thinking="alpha",
        )
        chunk_b = ReviewResult(
            findings=[Finding(
                severity="HIGH", fileName="23.docx", section="2.1",
                issue="finding B", actionType="EDIT",
                existingText=None, replacementText=None, codeReference=None,
            )],
            cross_check_status="completed",
            thinking="beta",
        )
        _run_cross_discipline_synthesis(
            [("div_22", chunk_a), ("div_23", chunk_b)],
            cycle=DEFAULT_CYCLE,
        )

        # The fake client captured exactly one stream call; system payload
        # should be a plain string and tools should carry no cache_control.
        captured = fake_client_for_phase.captured
        assert captured, "synthesis call did not reach the fake client"
        kwargs = captured[-1].kwargs
        assert isinstance(kwargs["system"], str), \
            "synthesis system payload should be a plain string (no cache block)"
        for tool in kwargs.get("tools") or []:
            assert "cache_control" not in tool, (
                "synthesis tools should not be cache-tagged"
            )

    def test_batch_review_request_carries_cache_blocks(self, fake_client_for_phase):
        from src.batch import submit_review_batch
        from src.code_cycles import DEFAULT_CYCLE
        from src.extractor import ExtractedSpec
        from src.api_config import MODEL_OPUS_47

        spec = ExtractedSpec(
            filename="A.docx", content="body", word_count=1,
            source_path="", source_format="docx", paragraph_map=None,
        )
        submit_review_batch([spec], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        captured = fake_client_for_phase.captured[-1]
        params = captured.requests[0]["params"]
        # System prompt cached.
        assert isinstance(params["system"], list)
        assert params["system"][0]["cache_control"]["type"] == "ephemeral"
        # Tools cached too (last entry).
        assert params["tools"][-1]["cache_control"]["type"] == "ephemeral"


# ---------------------------------------------------------------------------
# Telemetry: record_api_call + per-phase rollup
# ---------------------------------------------------------------------------


class TestRecordApiCall:
    def test_records_normalized_event(self):
        report = DiagnosticsReport()
        report.record_api_call(
            phase="review",
            model="claude-opus-4-7",
            input_tokens=1000,
            output_tokens=200,
            cache_creation_input_tokens=400,
            cache_read_input_tokens=600,
            web_search_requests=2,
            max_output_tokens=128_000,
            stop_reason="end_turn",
            mode="realtime",
            retry_status="initial",
        )
        assert len(report.events) == 1
        e = report.events[0]
        assert e.phase == "review"
        assert e.data["model"] == "claude-opus-4-7"
        assert e.data["input_tokens"] == 1000
        assert e.data["output_tokens"] == 200
        assert e.data["cache_creation_input_tokens"] == 400
        assert e.data["cache_read_input_tokens"] == 600
        assert e.data["web_search_requests"] == 2
        assert e.data["max_output_tokens"] == 128_000
        assert e.data["stop_reason"] == "end_turn"
        assert e.data["call_mode"] == "realtime"
        assert e.data["retry_status"] == "initial"
        assert e.data["api_call"] is True

    def test_extra_fields_merge_without_clobbering_standard_keys(self):
        report = DiagnosticsReport()
        report.record_api_call(
            phase="review",
            model="opus",
            input_tokens=10,
            extra={
                "severity_counts": {"HIGH": 1},
                # Should NOT override the standard model field (setdefault).
                "model": "ignored",
            },
        )
        e = report.events[0]
        assert e.data["model"] == "opus"
        assert e.data["severity_counts"] == {"HIGH": 1}


class TestPerPhaseRollup:
    def test_summary_buckets_calls_by_phase(self):
        report = DiagnosticsReport()
        report.record_api_call(
            phase="review", model="opus",
            input_tokens=1000, output_tokens=100,
            cache_creation_input_tokens=400, cache_read_input_tokens=200,
            mode="batch", retry_status="initial",
        )
        report.record_api_call(
            phase="review", model="opus",
            input_tokens=900, output_tokens=120,
            cache_creation_input_tokens=0, cache_read_input_tokens=900,
            mode="batch", retry_status="initial",
        )
        report.record_api_call(
            phase="verification", model="sonnet",
            input_tokens=300, output_tokens=80,
            web_search_requests=3,
            mode="batch", retry_status="initial",
        )
        report.record_api_call(
            phase="verification", model="opus",
            input_tokens=400, output_tokens=120,
            web_search_requests=5,
            mode="batch", retry_status="retry",
        )

        summary = report.summary()
        per_phase = summary["phase_telemetry"]
        assert set(per_phase.keys()) == {"review", "verification"}

        review = per_phase["review"]
        assert review["calls"] == 2
        assert review["input_tokens"] == 1900
        assert review["output_tokens"] == 220
        assert review["cache_creation_input_tokens"] == 400
        assert review["cache_read_input_tokens"] == 1100
        # 1100 / (1100 + 400) = 0.7333...
        assert review["cache_hit_ratio"] == pytest.approx(0.7333, rel=1e-3)
        assert review["batch_calls"] == 2
        assert review["realtime_calls"] == 0
        assert review["models"] == ["opus"]

        ver = per_phase["verification"]
        assert ver["calls"] == 2
        assert ver["web_search_requests"] == 8
        assert ver["retries"] == 1
        # Order-of-appearance dedup.
        assert ver["models"] == ["sonnet", "opus"]

    def test_continuation_status_increments_continuations_counter(self):
        report = DiagnosticsReport()
        report.record_api_call(
            phase="verification", model="sonnet",
            input_tokens=1, output_tokens=1,
            mode="realtime", retry_status="continuation",
        )
        per_phase = report.summary()["phase_telemetry"]
        assert per_phase["verification"]["continuations"] == 1
        assert per_phase["verification"]["retries"] == 0

    def test_truncated_call_is_counted_in_phase_bucket(self):
        report = DiagnosticsReport()
        report.record_api_call(
            phase="review", model="opus",
            input_tokens=1, output_tokens=1,
            stop_reason="max_tokens",
            mode="realtime", retry_status="initial",
        )
        per_phase = report.summary()["phase_telemetry"]
        assert per_phase["review"]["truncated_calls"] == 1

    def test_non_api_log_events_do_not_inflate_phase_calls(self):
        report = DiagnosticsReport()
        report.log("review", "info", "starting")  # no token data
        report.log("review", "step", "midstep")
        report.record_api_call(
            phase="review", model="opus",
            input_tokens=5, output_tokens=5,
            mode="realtime", retry_status="initial",
        )
        per_phase = report.summary()["phase_telemetry"]
        # Only the recorded API call should count toward calls.
        assert per_phase["review"]["calls"] == 1

    def test_cost_summary_aggregates_across_phases(self):
        report = DiagnosticsReport()
        report.record_api_call(
            phase="review", model="opus",
            input_tokens=1000, output_tokens=100,
            cache_creation_input_tokens=300, cache_read_input_tokens=700,
        )
        report.record_api_call(
            phase="verification", model="sonnet",
            input_tokens=200, output_tokens=50,
            web_search_requests=4,
        )
        cs = report.summary()["cost_summary"]
        assert cs["total_input_tokens"] == 1200
        assert cs["total_output_tokens"] == 150
        assert cs["total_cache_creation_input_tokens"] == 300
        assert cs["total_cache_read_input_tokens"] == 700
        assert cs["total_web_search_requests"] == 4
        # 700 / (700 + 300)
        assert cs["cache_hit_ratio"] == 0.7
        assert set(cs["phases"].keys()) == {"review", "verification"}

    def test_summary_total_search_requests_aggregates(self):
        report = DiagnosticsReport()
        report.record_api_call(phase="verification", web_search_requests=2)
        report.record_api_call(phase="verification", web_search_requests=5)
        summary = report.summary()
        assert summary["total_web_search_requests"] == 7


class TestToTextRollup:
    def test_to_text_renders_phase_telemetry_section(self):
        report = DiagnosticsReport()
        report.record_api_call(
            phase="review", model="opus",
            input_tokens=1000, output_tokens=100,
            cache_creation_input_tokens=200, cache_read_input_tokens=800,
            mode="batch",
        )
        report.record_api_call(
            phase="verification", model="sonnet",
            input_tokens=300, output_tokens=60,
            web_search_requests=3, mode="realtime", retry_status="retry",
        )
        report.finish()
        text = report.to_text()
        assert "Phase Telemetry" in text
        assert "review" in text
        assert "verification" in text
        # Cache hit ratio is rendered as a percent.
        assert "80%" in text
        # Retry counter shown when non-zero.
        assert "retries=1" in text
        # Web search count shown when non-zero.
        assert "searches=3" in text

    def test_to_text_renders_cache_hit_ratio_in_summary(self):
        report = DiagnosticsReport()
        report.record_api_call(
            phase="review", model="opus",
            input_tokens=1000,
            cache_creation_input_tokens=250, cache_read_input_tokens=750,
        )
        report.finish()
        text = report.to_text()
        # 750 / (750 + 250) = 75%
        assert "Cache Hit Ratio: 75.0%" in text


# ---------------------------------------------------------------------------
# Fake-client fixture for the synthesis / batch wiring tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_client_for_phase(monkeypatch, fake_anthropic):
    """Pin a capturing client into the modules used by phase-wiring tests.

    Mirrors the ``fake_client`` fixture in ``test_request_payload_shape``
    but local to this file so this test module doesn't depend on cross-file
    fixture order.
    """
    from tests.test_request_payload_shape import FakeClient
    from src import batch as batch_mod
    from src import cross_checker as cc_mod
    from src import reviewer as reviewer_mod
    from src import verifier as verifier_mod

    client = FakeClient(
        default_final_message=fake_anthropic.review_tool_use_response(),
    )

    def _provider() -> FakeClient:
        return client

    monkeypatch.setattr(batch_mod, "_get_client", _provider)
    monkeypatch.setattr(cc_mod, "_get_client", _provider)
    monkeypatch.setattr(reviewer_mod, "_get_client", _provider)
    monkeypatch.setattr(verifier_mod, "_get_client", _provider)

    # Also stub the lazy tokenizer download (mirrors the autouse stub in
    # the other request-shape file).
    def _fake_count(text):
        return len((text or "").split()) * 2
    monkeypatch.setattr("src.tokenizer.count_tokens", _fake_count)
    monkeypatch.setattr("src.batch.count_tokens", _fake_count)
    monkeypatch.setattr("src.cross_checker.count_tokens", _fake_count)
    monkeypatch.setattr("src.pipeline.count_tokens", _fake_count, raising=False)

    return client
