"""Phase D1 tests: verification loop corrections and request-policy refinements.

Covers:

- Chunk D1.1 — server-tool ``pause_turn`` continuation semantics. Anthropic's
  stop_reason docs say the correct way to resume a paused server-tool turn is
  to re-send the assistant content as-is, with no new user turn. The prior
  code path appended a synthetic ``{"role": "user", "content": "continue"}``
  message, which wasted tokens and interfered with thinking / tool-state
  continuity.

- Chunk D1.2 — model-aware ``output_config.effort`` policy. The Anthropic API
  accepts an effort parameter on supported models that controls token
  eagerness. The policy is centralized in :mod:`src.api_config` and applied
  to every verifier / reviewer / cross-check request via a single helper so
  unsupported models never receive the field.

- Chunk D1.3 — escalation telemetry. Whenever Sonnet → Opus verification
  escalation fires, the result records the initial / escalated models and
  whether the verdict actually changed, so :mod:`src.diagnostics` can answer
  "is escalation paying off?".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.code_cycles import DEFAULT_CYCLE
from src.reviewer import Finding
from src.verifier import (
    VerificationResult,
    _build_continuation_request,
)


def _finding(**overrides) -> Finding:
    base = dict(
        severity="HIGH",
        fileName="spec.docx",
        section="2.1",
        issue="Cited code is outdated",
        actionType="EDIT",
        existingText="CBC 2019",
        replacementText="CBC 2025",
        codeReference="CBC 2025",
        confidence=0.6,
    )
    base.update(overrides)
    return Finding(**base)


# ===========================================================================
# Chunk D1.1 — pause_turn continuation semantics
# ===========================================================================


class TestPauseTurnContinuationSemantics:
    """Server-tool ``pause_turn`` is resumed by re-sending the assistant
    response content. No synthetic ``"continue"`` user turn is appended."""

    def test_batch_continuation_request_has_no_synthetic_continue_turn(self):
        req = _build_continuation_request(
            "user prompt",
            [{"type": "text", "text": "thinking..."}],
            cycle=DEFAULT_CYCLE,
        )
        assert [m["role"] for m in req["messages"]] == ["user", "assistant"]
        # No literal "continue" text block anywhere.
        for msg in req["messages"]:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        assert block.get("text") != "continue"

    def test_batch_continuation_preserves_assistant_content_exactly(self):
        """The assistant blocks (including thinking blocks / tool_use_ids
        if present) must round-trip into the continuation request without
        modification — that is how the model resumes server-tool state."""
        thinking_block = {"type": "thinking", "thinking": "weighing evidence", "signature": "sig123"}
        tool_use_block = {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "CPC 2025"},
        }
        blocks = [thinking_block, tool_use_block]
        req = _build_continuation_request("prompt", blocks, cycle=DEFAULT_CYCLE)
        assert req["messages"][1]["role"] == "assistant"
        assert req["messages"][1]["content"] == blocks

    def test_batch_continuation_preserves_request_policy(self):
        """Continuation must reuse the same model, tools, system prompt, and
        thinking config as the initial request so the model resumes with the
        same capabilities — not a freshly negotiated session."""
        from src.api_config import MODEL_SONNET_46

        req = _build_continuation_request(
            "prompt",
            [{"type": "text", "text": "..."}],
            cycle=DEFAULT_CYCLE,
            model=MODEL_SONNET_46,
        )
        assert req["model"] == MODEL_SONNET_46
        tool_names = [t.get("name") for t in (req.get("tools") or [])]
        assert "web_search" in tool_names
        # Thinking config preserved (Sonnet supports adaptive thinking).
        assert req.get("thinking") == {"type": "adaptive"}

    def test_realtime_pause_turn_resumes_without_synthetic_user_turn(
        self, monkeypatch, fake_anthropic
    ):
        """Drive the real-time verifier through a pause_turn / terminal pair
        and capture the second request payload. The second request must
        contain exactly one user message (the original prompt) followed by
        the assistant content — no second user turn."""
        from src import verifier as verifier_mod

        # Pause turn first: an assistant response with web_search blocks +
        # ``pause_turn``. The grounding helpers want a successful search
        # block on either response so this carries one.
        pause_msg = fake_anthropic.verification_tool_use_response(
            stop_reason="pause_turn",
            include_web_search_blocks=True,
            payload=fake_anthropic.sample_verification_verdict_payload(),
        )
        # Terminal response: structured verdict with grounded search.
        terminal_msg = fake_anthropic.verification_tool_use_response(
            stop_reason="tool_use",
            include_web_search_blocks=True,
        )

        captured_messages: list = []

        class _Ctx:
            def __init__(self, message):
                self._msg = message

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return self._msg

        # Each call records its messages payload then returns the next
        # queued response.
        queue = [pause_msg, terminal_msg]

        class _FakeMessages:
            def stream(self, **kwargs):
                captured_messages.append([dict(m) for m in kwargs["messages"]])
                return _Ctx(queue.pop(0))

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(verifier_mod, "_get_client", lambda: _FakeClient())
        # Force the routing to skip local-skip so the call actually fires.
        monkeypatch.setattr(
            verifier_mod, "classify_finding_for_verification", lambda _f: "web_required"
        )
        # Short-circuit the escalation check so the test only exercises
        # the pause_turn → terminal path.
        monkeypatch.setattr(
            verifier_mod, "should_escalate_verification", lambda *_a, **_k: False
        )

        result = verifier_mod.verify_finding(_finding(), max_retries=0)

        # Two requests must have been issued: the initial and the resume.
        assert len(captured_messages) == 2
        initial, resumed = captured_messages

        # Initial request: just the user prompt.
        assert [m["role"] for m in initial] == ["user"]

        # Resumed request: user + assistant, with no synthetic continue.
        assert [m["role"] for m in resumed] == ["user", "assistant"]
        for msg in resumed:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        assert block.get("text") != "continue"

        # The verdict should have come through the terminal message.
        assert result.verdict == "CONFIRMED"

    def test_pause_turn_message_carries_complete_assistant_content(
        self, monkeypatch, fake_anthropic
    ):
        """The assistant content from the paused response must be copied
        into the next request verbatim (so the model can resume tool state).
        """
        from src import verifier as verifier_mod

        pause_msg = fake_anthropic.verification_tool_use_response(
            stop_reason="pause_turn",
            include_web_search_blocks=True,
        )
        terminal_msg = fake_anthropic.verification_tool_use_response(
            stop_reason="tool_use",
            include_web_search_blocks=True,
        )
        pause_content = pause_msg.content

        captured_messages: list = []
        queue = [pause_msg, terminal_msg]

        class _Ctx:
            def __init__(self, message):
                self._msg = message

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return self._msg

        class _FakeMessages:
            def stream(self, **kwargs):
                captured_messages.append([dict(m) for m in kwargs["messages"]])
                return _Ctx(queue.pop(0))

        class _FakeClient:
            messages = _FakeMessages()

        monkeypatch.setattr(verifier_mod, "_get_client", lambda: _FakeClient())
        monkeypatch.setattr(
            verifier_mod, "classify_finding_for_verification", lambda _f: "web_required"
        )
        monkeypatch.setattr(
            verifier_mod, "should_escalate_verification", lambda *_a, **_k: False
        )

        verifier_mod.verify_finding(_finding(), max_retries=0)

        # Resumed request — second call's messages list — must carry the
        # original assistant content unchanged.
        resumed = captured_messages[1]
        assert resumed[1]["role"] == "assistant"
        assert resumed[1]["content"] is pause_content
