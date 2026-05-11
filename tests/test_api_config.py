"""Phase 2 (API modernization) regression tests.

Covers the centralized api_config module: prompt caching, dynamic output
caps, the 300k batch fail-fast guard, web-search tool builder, and the
Anthropic token-counting preflight helper.
"""
from __future__ import annotations

import os

import pytest

from src import api_config
from src.api_config import (
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_OUTPUT_BETA,
    DEFAULT_VERIFICATION_MAX_USES,
    MAX_OUTPUT_TOKENS_OPUS,
    MAX_OUTPUT_TOKENS_SONNET,
    MODEL_OPUS_46,
    MODEL_SONNET_46,
    REVIEW_OUTPUT_CAP,
    VERIFICATION_OUTPUT_CAP,
    WEB_SEARCH_TOOL,
    assert_extended_output_allowed,
    build_web_search_tool,
    cross_check_max_tokens,
    extract_cache_usage,
    output_cap_for_model,
    prompt_caching_enabled,
    review_max_tokens,
    system_prompt_with_cache,
    tools_with_cache,
    verification_max_tokens,
)


# ---------------------------------------------------------------------------
# Prompt caching
# ---------------------------------------------------------------------------


class TestPromptCaching:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_PROMPT_CACHE", raising=False)
        assert prompt_caching_enabled() is True

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "0")
        assert prompt_caching_enabled() is False

    def test_system_prompt_with_cache_returns_blocks_when_enabled(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        payload = system_prompt_with_cache("system text")
        assert isinstance(payload, list)
        assert len(payload) == 1
        block = payload[0]
        assert block["type"] == "text"
        assert block["text"] == "system text"
        # Default TTL is 1h; cache_control must always carry type=ephemeral.
        cc = block["cache_control"]
        assert cc["type"] == "ephemeral"
        assert cc.get("ttl") == "1h"

    def test_system_prompt_with_cache_returns_string_when_disabled(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "0")
        payload = system_prompt_with_cache("system text")
        assert payload == "system text"

    def test_tools_with_cache_marks_last_only(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        cached = tools_with_cache([{"name": "a"}, {"name": "b"}])
        assert "cache_control" not in cached[0]
        cc = cached[1]["cache_control"]
        assert cc["type"] == "ephemeral"
        assert cc.get("ttl") == "1h"
        # Original list/dicts should not be mutated.
        assert cached[1] is not cached[0]

    def test_tools_with_cache_no_op_when_disabled(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "0")
        original = [{"name": "a"}]
        result = tools_with_cache(original)
        assert result == original
        assert "cache_control" not in result[0]

    def test_tools_with_cache_handles_empty_list(self):
        assert tools_with_cache([]) == []


# ---------------------------------------------------------------------------
# Dynamic output caps
# ---------------------------------------------------------------------------


class TestOutputCaps:
    def test_opus_cap_within_ceiling(self):
        cap = output_cap_for_model(MODEL_OPUS_46, requested=200_000)
        assert cap == MAX_OUTPUT_TOKENS_OPUS

    def test_sonnet_cap_within_ceiling(self):
        cap = output_cap_for_model(MODEL_SONNET_46, requested=200_000)
        assert cap == MAX_OUTPUT_TOKENS_SONNET

    def test_review_cap_is_unified_across_modes(self):
        # Real-time and batch share the same baseline cap so findings cannot
        # diverge between modes on normal-size specs.
        realtime = review_max_tokens(batch=False, model=MODEL_OPUS_46)
        batch = review_max_tokens(batch=True, model=MODEL_OPUS_46)
        assert realtime == batch == REVIEW_OUTPUT_CAP

    def test_batch_review_extended_output(self):
        cap = review_max_tokens(
            batch=True, model=MODEL_OPUS_46, allow_extended_output=True
        )
        assert cap == BATCH_MAX_OUTPUT_TOKENS

    def test_realtime_cannot_use_extended_output(self):
        # The 300k beta header is a batch-only API capability — real-time
        # never returns the extended cap even if asked.
        cap = review_max_tokens(
            batch=False, model=MODEL_OPUS_46, allow_extended_output=True
        )
        assert cap == REVIEW_OUTPUT_CAP

    def test_review_sonnet_clamped_to_sonnet_ceiling(self):
        cap = review_max_tokens(batch=True, model=MODEL_SONNET_46)
        assert cap == MAX_OUTPUT_TOKENS_SONNET

    def test_cross_check_cap_below_opus_ceiling(self):
        cap = cross_check_max_tokens(model=MODEL_OPUS_46)
        assert cap < MAX_OUTPUT_TOKENS_OPUS

    def test_verification_cap_is_modest(self):
        cap = verification_max_tokens()
        assert cap == VERIFICATION_OUTPUT_CAP
        # Verification verdicts are short — cap should not match the
        # 128k blanket value the prior code used.
        assert cap < MAX_OUTPUT_TOKENS_OPUS


# ---------------------------------------------------------------------------
# 300k batch fail-fast guard
# ---------------------------------------------------------------------------


class TestExtendedOutputGuard:
    def test_allows_standard_output_without_beta(self):
        assert_extended_output_allowed(max_tokens=128_000, betas=None)
        assert_extended_output_allowed(max_tokens=128_000, betas=[])

    def test_rejects_extended_output_without_beta(self):
        with pytest.raises(ValueError) as exc:
            assert_extended_output_allowed(max_tokens=300_000, betas=[])
        assert BATCH_OUTPUT_BETA in str(exc.value)

    def test_rejects_extended_output_with_unrelated_beta(self):
        with pytest.raises(ValueError):
            assert_extended_output_allowed(
                max_tokens=300_000, betas=["some-other-beta"]
            )

    def test_allows_extended_output_with_beta(self):
        assert_extended_output_allowed(
            max_tokens=300_000, betas=[BATCH_OUTPUT_BETA]
        )


# ---------------------------------------------------------------------------
# Web search tool builder
# ---------------------------------------------------------------------------


class TestWebSearchTool:
    def test_default_tool_uses_lower_max_uses(self):
        # Plan section 6.8: default max_uses lowered from 10.
        assert WEB_SEARCH_TOOL["max_uses"] == DEFAULT_VERIFICATION_MAX_USES
        assert WEB_SEARCH_TOOL["max_uses"] <= 10

    def test_tool_has_blocked_only_no_allowed(self):
        # Plan section 6.8: avoid combining allowed_domains and blocked_domains.
        assert "blocked_domains" in WEB_SEARCH_TOOL
        assert "allowed_domains" not in WEB_SEARCH_TOOL

    def test_tool_has_california_user_location(self):
        loc = WEB_SEARCH_TOOL["user_location"]
        assert loc["country"] == "US"
        assert loc["region"] == "California"

    def test_build_with_custom_max_uses(self):
        custom = build_web_search_tool(max_uses=2)
        assert custom["max_uses"] == 2


# ---------------------------------------------------------------------------
# Cache usage extractor
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestExtractCacheUsage:
    def test_handles_missing_usage(self):
        result = extract_cache_usage(None)
        assert result == {
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def test_extracts_both_fields(self):
        usage = _FakeUsage(
            cache_creation_input_tokens=1000,
            cache_read_input_tokens=4500,
        )
        result = extract_cache_usage(usage)
        assert result["cache_creation_input_tokens"] == 1000
        assert result["cache_read_input_tokens"] == 4500

    def test_handles_missing_fields_gracefully(self):
        usage = _FakeUsage(input_tokens=100)
        result = extract_cache_usage(usage)
        assert result["cache_creation_input_tokens"] == 0
        assert result["cache_read_input_tokens"] == 0


# ---------------------------------------------------------------------------
# Token-count preflight wiring
# ---------------------------------------------------------------------------


class TestTokenCountPreflight:
    def test_enabled_by_default(self, monkeypatch):
        # Phase 2.3 (audit Section 6.3): default is now ON so the pipeline
        # always runs the moment-of-truth API count before submission.
        monkeypatch.delenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", raising=False)
        assert api_config.token_count_preflight_enabled() is True

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "0")
        assert api_config.token_count_preflight_enabled() is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "1")
        assert api_config.token_count_preflight_enabled() is True


class TestCountTokensViaApi:
    def test_returns_none_on_failure_no_block(self, monkeypatch):
        # No client and no API key → should log a warning and return None.
        from src import tokenizer

        class _BoomClient:
            class messages:
                @staticmethod
                def count_tokens(**kwargs):
                    raise RuntimeError("network down")

        result = tokenizer.count_tokens_via_api(
            model=MODEL_OPUS_46,
            system="x",
            messages=[{"role": "user", "content": "hi"}],
            client=_BoomClient(),
        )
        assert result is None

    def test_returns_int_on_success(self):
        from src import tokenizer

        class _Result:
            input_tokens = 1234

        class _Messages:
            @staticmethod
            def count_tokens(**kwargs):
                return _Result()

        class _OkClient:
            messages = _Messages()

        result = tokenizer.count_tokens_via_api(
            model=MODEL_OPUS_46,
            system="x",
            messages=[{"role": "user", "content": "hi"}],
            client=_OkClient(),
        )
        assert result == 1234


# ---------------------------------------------------------------------------
# Wiring: review/cross-check/verification request shapes carry cache controls
# when the feature is enabled.
# ---------------------------------------------------------------------------


class TestRequestShapeWiring:
    def test_verifier_retry_request_uses_cache(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        from src.code_cycles import DEFAULT_CYCLE
        from src.verifier import _build_retry_request

        req = _build_retry_request("prompt body", cycle=DEFAULT_CYCLE)
        # System should be a list of cache-tagged blocks.
        assert isinstance(req["system"], list)
        sys_cc = req["system"][0]["cache_control"]
        assert sys_cc["type"] == "ephemeral"
        # Last tool should carry cache_control.
        tool_cc = req["tools"][-1]["cache_control"]
        assert tool_cc["type"] == "ephemeral"
        # Verification cap should be the modest value, not 128k.
        assert req["max_tokens"] == VERIFICATION_OUTPUT_CAP

    def test_verifier_continuation_request_uses_cache(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        from src.code_cycles import DEFAULT_CYCLE
        from src.verifier import _build_continuation_request

        req = _build_continuation_request(
            "prompt body",
            [{"type": "text", "text": "partial"}],
            cycle=DEFAULT_CYCLE,
        )
        assert isinstance(req["system"], list)
        sys_cc = req["system"][0]["cache_control"]
        assert sys_cc["type"] == "ephemeral"
        # Chunk D1.1: server-tool ``pause_turn`` resumption resends the
        # assistant content as-is — no synthetic ``"continue"`` user turn.
        # The prior payload shape (user/assistant/user) wasted tokens.
        roles = [m["role"] for m in req["messages"]]
        assert roles == ["user", "assistant"]

    def test_verifier_system_prompt_no_longer_mentions_code_execution(self):
        from src.code_cycles import DEFAULT_CYCLE
        from src.verifier import _get_verification_system_prompt

        prompt = _get_verification_system_prompt(DEFAULT_CYCLE)
        assert "code_execution" not in prompt
