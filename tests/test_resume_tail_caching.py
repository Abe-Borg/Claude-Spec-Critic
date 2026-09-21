"""Prompt-cache read point for the conversation tail on ``pause_turn`` resumes.

Every explicit ``cache_control`` breakpoint the app sets is on ``system`` or
on the trailing tool definition. A ``pause_turn`` resume re-sends the whole
accumulated assistant turn — thinking, server-tool uses, search results — and
the next resume re-sends it again unchanged, but with no breakpoint at or
after it the conversation tail has no read point and is re-priced at full
input rate on every resume (2026-09 prompt review, Finding 2).

The fix is the request-level automatic ``cache_control`` field, not an
explicit marker on the last block: that block is usually a server-tool result
or a thinking block, neither of which accepts a marker, and the API walks back
to the nearest eligible block itself. Three properties are load-bearing:

1. **The first call of every conversation is byte-identical.** The field is
   written only when ``messages`` already carries an assistant turn, so the
   common single-shot verification never pays a cache write on a user
   message nothing will re-read.
2. **The tail breakpoint uses the five-minute default TTL**, not the app's
   one-hour house rule: a resume follows its pause within seconds, and under
   five minutes the one-hour TTL buys nothing but the doubled write price.
3. **It is scoped to the two real-time resume loops.** The batch wave path
   is untouched — waves can be hours apart, past any TTL, and cache hits
   between batch items are not guaranteed anyway.
"""
from __future__ import annotations

import pytest

import src.verification.verifier as V
from src.core.api_config import (
    RESUME_TAIL_CACHE_CONTROL,
    apply_resume_cache_config,
)
from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.verification_routing import (
    build_verification_request,
    select_routing,
)
from tests.fixtures.fake_anthropic import (
    pause_turn_response,
    research_tool_use_response,
    verification_tool_use_response,
)
from tests.test_server_tool_container import (
    SEARCHED_URL,
    _enabled_module,
    _FakeClient,
    _finding,
    _profile,
    _research_payload,
    _run_realtime,
    _run_research,
    _sequential,
)


# ---------------------------------------------------------------------------
# 1. The helper
# ---------------------------------------------------------------------------


class TestApplyResumeCacheConfig:
    def test_first_call_writes_nothing(self):
        params: dict = {"model": "m"}
        apply_resume_cache_config(params, [{"role": "user", "content": "q"}])
        assert params == {"model": "m"}

    def test_empty_or_missing_messages_write_nothing(self):
        for messages in ([], None):
            params: dict = {}
            apply_resume_cache_config(params, messages)
            assert params == {}

    def test_resume_gets_the_request_level_field(self):
        params: dict = {}
        apply_resume_cache_config(
            params,
            [{"role": "user", "content": "q"}, {"role": "assistant", "content": []}],
        )
        assert params["cache_control"] == {"type": "ephemeral"}

    def test_five_minute_default_ttl_is_deliberate(self):
        # A resume follows its pause within seconds; the 1h TTL would only
        # double the write price. Pinned so the house rule is not applied
        # here by reflex.
        assert "ttl" not in RESUME_TAIL_CACHE_CONTROL

    def test_written_value_is_a_copy(self):
        params: dict = {}
        apply_resume_cache_config(
            params, [{"role": "user", "content": "q"}, {"role": "assistant", "content": []}]
        )
        params["cache_control"]["ttl"] = "1h"
        assert "ttl" not in RESUME_TAIL_CACHE_CONTROL

    def test_reads_role_off_objects_as_well_as_dicts(self):
        class Msg:
            def __init__(self, role):
                self.role = role

        params: dict = {}
        apply_resume_cache_config(params, [Msg("user"), Msg("assistant")])
        assert "cache_control" in params


# ---------------------------------------------------------------------------
# 2. Real-time verifier loop
# ---------------------------------------------------------------------------


class TestRealtimeVerifierResumeCaching:
    def test_first_call_has_no_field_and_the_resume_does(self, monkeypatch):
        paused = pause_turn_response(searched_urls=[SEARCHED_URL])
        _result, client = _run_realtime(
            monkeypatch, [paused, verification_tool_use_response()]
        )

        assert len(client.calls) == 2
        assert "cache_control" not in client.calls[0]
        assert client.calls[1]["cache_control"] == {"type": "ephemeral"}

    def test_every_resume_carries_it(self, monkeypatch):
        first = pause_turn_response(searched_urls=[SEARCHED_URL])
        second = pause_turn_response()
        _result, client = _run_realtime(
            monkeypatch, [first, second, verification_tool_use_response()]
        )

        assert len(client.calls) == 3
        assert "cache_control" not in client.calls[0]
        assert client.calls[1]["cache_control"] == {"type": "ephemeral"}
        assert client.calls[2]["cache_control"] == {"type": "ephemeral"}

    def test_resume_still_resends_the_assistant_turn(self, monkeypatch):
        # The field only adds a read point; the resume contract (assistant
        # content back, no synthetic user turn) is unchanged.
        paused = pause_turn_response(searched_urls=[SEARCHED_URL])
        _result, client = _run_realtime(
            monkeypatch, [paused, verification_tool_use_response()]
        )
        roles = [m["role"] for m in client.calls[1]["messages"]]
        assert roles == ["user", "assistant"]

    def test_single_shot_verification_is_byte_identical(self, monkeypatch):
        _result, client = _run_realtime(monkeypatch, [verification_tool_use_response()])
        assert len(client.calls) == 1
        assert "cache_control" not in client.calls[0]


# ---------------------------------------------------------------------------
# 3. Research fan-out loop
# ---------------------------------------------------------------------------


class TestResearchResumeCaching:
    def test_first_call_has_no_field_and_the_resume_does(self):
        paused = pause_turn_response(searched_urls=[SEARCHED_URL])
        done = research_tool_use_response(
            payload=_research_payload(), searched_urls=[SEARCHED_URL]
        )
        _profile_out, client = _run_research([paused, done])

        assert len(client.calls) == 2
        assert "cache_control" not in client.calls[0]
        assert client.calls[1]["cache_control"] == {"type": "ephemeral"}

    def test_single_call_dimension_is_byte_identical(self):
        done = research_tool_use_response(
            payload=_research_payload(), searched_urls=[SEARCHED_URL]
        )
        _profile_out, client = _run_research([done])
        assert len(client.calls) == 1
        assert "cache_control" not in client.calls[0]


# ---------------------------------------------------------------------------
# 4. Batch continuation builder is deliberately untouched
# ---------------------------------------------------------------------------


class TestBatchContinuationIsOutOfScope:
    def test_continuation_request_body_carries_no_tail_cache_field(self):
        decision = select_routing(_finding(), cycle=DEFAULT_CYCLE)
        request = build_verification_request(
            decision,
            prompt="verify this",
            system_prompt="system",
            include_service_tier=True,
            assistant_content=[{"type": "text", "text": "partial"}],
        )
        assert "cache_control" not in request.params

    def test_initial_request_body_carries_no_tail_cache_field(self):
        decision = select_routing(_finding(), cycle=DEFAULT_CYCLE)
        request = build_verification_request(
            decision, prompt="verify this", system_prompt="system", include_service_tier=True
        )
        assert "cache_control" not in request.params
