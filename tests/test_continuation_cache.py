"""Message-level cache breakpoints on pause_turn continuation resumes (B4).

Every continuation re-sends the whole growing conversation; without a
message-level ``cache_control`` the accumulated turns re-bill as uncached
input on every resume. ``mark_continuation_cache_breakpoint`` places exactly
one breakpoint on the last eligible block of the last assistant message
(strip-then-mark, copy-on-write, SDK objects never mutated).

Integration: the research fan-out loop and the verifier realtime loop both
mark after ``sanitize_messages_for_resend`` so the breakpoint survives PDF
elision rebuilds.
"""
from __future__ import annotations

import pytest

from src.core.continuation_cache import mark_continuation_cache_breakpoint

from tests.fixtures.fake_anthropic import (
    FakeMessage,
    FakeServerToolUseBlock,
    FakeTextBlock,
    FakeToolUseBlock,
    pause_turn_response,
    research_tool_use_response,
    verification_tool_use_response,
)


def _cache_controls(messages) -> list[tuple[int, int]]:
    """(msg_idx, block_idx) of every message-level cache_control marker."""
    found = []
    for m_idx, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for b_idx, block in enumerate(content):
            if isinstance(block, dict) and block.get("cache_control"):
                found.append((m_idx, b_idx))
    return found


def _user(text: str = "verify this") -> dict:
    return {"role": "user", "content": text}


class TestMarkBreakpoint:
    def test_marks_last_eligible_block_of_last_assistant(self):
        messages = [
            _user(),
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "reasoning so far"},
                    {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}},
                ],
            },
        ]
        marked = mark_continuation_cache_breakpoint(messages)
        assert _cache_controls(marked) == [(1, 0)]
        block = marked[1]["content"][0]
        assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        # Copy-on-write: the input messages are untouched.
        assert _cache_controls(messages) == []

    def test_trailing_thinking_block_is_skipped(self):
        messages = [
            _user(),
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "analysis"},
                    {"type": "thinking", "thinking": "...", "signature": "sig"},
                ],
            },
        ]
        marked = mark_continuation_cache_breakpoint(messages)
        assert _cache_controls(marked) == [(1, 0)]
        assert "cache_control" not in marked[1]["content"][1]

    def test_server_tool_result_blocks_are_skipped(self):
        messages = [
            _user(),
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "grep", "input": {}},
                    {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
                    {"type": "web_fetch_tool_result", "tool_use_id": "f1", "content": {}},
                ],
            },
        ]
        marked = mark_continuation_cache_breakpoint(messages)
        assert _cache_controls(marked) == [(1, 0)]

    def test_second_call_strips_first_breakpoint(self):
        messages = [
            _user(),
            {"role": "assistant", "content": [{"type": "text", "text": "turn 1"}]},
        ]
        first = mark_continuation_cache_breakpoint(messages)
        grown = [
            *first,
            {"role": "assistant", "content": [{"type": "text", "text": "turn 2"}]},
        ]
        second = mark_continuation_cache_breakpoint(grown)
        # Exactly one marker across the whole conversation, on the newest turn.
        assert _cache_controls(second) == [(2, 0)]

    def test_no_eligible_block_returns_same_list(self):
        messages = [
            _user(),
            {
                "role": "assistant",
                "content": [
                    {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}},
                ],
            },
        ]
        assert mark_continuation_cache_breakpoint(messages) is messages

    def test_no_assistant_message_returns_same_list(self):
        messages = [_user()]
        assert mark_continuation_cache_breakpoint(messages) is messages

    def test_string_content_assistant_is_ignored(self):
        messages = [_user(), {"role": "assistant", "content": "plain string"}]
        assert mark_continuation_cache_breakpoint(messages) is messages

    def test_sdk_shaped_blocks_are_not_mutated(self):
        pause = pause_turn_response()
        text_block = FakeTextBlock(text="analysis so far")
        content = [text_block, *pause.content]
        messages = [_user(), {"role": "assistant", "content": content}]
        marked = mark_continuation_cache_breakpoint(messages)
        assert _cache_controls(marked) == [(1, 0)]
        # The dataclass fixture (stand-in for the SDK object) is untouched;
        # the marked copy is a plain dict.
        assert not hasattr(text_block, "cache_control") or not getattr(
            text_block, "cache_control"
        )
        assert isinstance(marked[1]["content"][0], dict)
        # Non-target blocks survive conversion with their type intact.
        assert marked[1]["content"][1]["type"] == "server_tool_use"

    def test_marker_lands_on_latest_assistant_only(self):
        messages = [
            _user(),
            {"role": "assistant", "content": [{"type": "text", "text": "turn 1"}]},
            _user("continue"),
            {"role": "assistant", "content": [{"type": "text", "text": "turn 2"}]},
        ]
        marked = mark_continuation_cache_breakpoint(messages)
        assert _cache_controls(marked) == [(3, 0)]


class TestResearchLoopIntegration:
    def test_second_stream_call_carries_exactly_one_breakpoint(self):
        """Drive the research fan-out through a pause_turn: the resumed
        request's messages must carry exactly one message-level
        cache_control, on an eligible (non-thinking, non-server) block."""
        from tests.test_requirements_research import (
            FakeResearchClient,
            _complete_profile,
            _enabled_module,
            _route_by_marker,
        )
        from src.research.requirements_research import run_requirements_research

        pause = FakeMessage(
            content=[
                FakeTextBlock(text="searching municipal amendments"),
                FakeServerToolUseBlock(name="web_search", input={"query": "x"}),
            ],
            stop_reason="pause_turn",
        )
        client = FakeResearchClient(
            _route_by_marker({"ALPHA": [pause, research_tool_use_response()]})
        )
        run_requirements_research(
            _enabled_module(), _complete_profile(), client=client
        )
        assert len(client.calls) == 2
        resumed_messages = client.calls[1]["messages"]
        markers = _cache_controls(resumed_messages)
        assert len(markers) == 1
        m_idx, b_idx = markers[0]
        block = resumed_messages[m_idx][
            "content"
        ][b_idx]
        assert block["type"] == "text"


class TestVerifierLoopIntegration:
    def test_pause_turn_resume_carries_exactly_one_breakpoint(self, monkeypatch):
        from src.review.reviewer import Finding
        from src.verification import verifier as vf

        calls: list[dict] = []

        class _Stream:
            def __init__(self, message):
                self._message = message

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_final_message(self):
                return self._message

        class _Messages:
            def __init__(self, script):
                self._script = list(script)

            def stream(self, **kwargs):
                calls.append(kwargs)
                return _Stream(self._script.pop(0))

        class _Client:
            def __init__(self, script):
                self.messages = _Messages(script)

        pause = FakeMessage(
            content=[
                FakeTextBlock(text="verifying against the code"),
                FakeServerToolUseBlock(name="web_search", input={"query": "x"}),
            ],
            stop_reason="pause_turn",
        )
        client = _Client([pause, verification_tool_use_response()])
        monkeypatch.setattr(vf, "_get_client", lambda: client)

        finding = Finding(
            severity="MEDIUM",
            fileName="a.docx",
            section="2.1",
            issue="Cites CPC 603.4.2 for backflow assembly testing",
            actionType="REPORT_ONLY",
            existingText=None,
            replacementText=None,
            codeReference="CPC 603.4.2",
        )
        result = vf.verify_finding(finding, max_retries=0)
        assert result is not None
        assert len(calls) == 2
        resumed_messages = calls[1]["messages"]
        markers = _cache_controls(resumed_messages)
        assert len(markers) == 1
        m_idx, b_idx = markers[0]
        assert resumed_messages[m_idx]["content"][b_idx]["type"] == "text"
