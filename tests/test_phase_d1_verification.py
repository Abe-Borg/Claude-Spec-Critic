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


# ===========================================================================
# Chunk D1.2 — model-aware output_config.effort policy
# ===========================================================================


class TestEffortConfigForHelper:
    """Unit-level coverage for the centralized policy helper. The request-
    shape tests below assert that wiring delivers the same answers."""

    def test_sonnet_verification_default_is_medium(self):
        from src.api_config import (
            MODEL_SONNET_46,
            PHASE_VERIFICATION,
            effort_config_for,
        )

        cfg = effort_config_for(model=MODEL_SONNET_46, phase=PHASE_VERIFICATION)
        assert cfg == {"effort": "medium"}

    def test_opus_verification_is_high_for_escalation(self):
        from src.api_config import (
            MODEL_OPUS_47,
            PHASE_VERIFICATION,
            effort_config_for,
        )

        cfg = effort_config_for(model=MODEL_OPUS_47, phase=PHASE_VERIFICATION)
        assert cfg == {"effort": "high"}

    def test_review_phase_uses_high(self):
        from src.api_config import (
            MODEL_OPUS_47,
            PHASE_REVIEW,
            effort_config_for,
        )

        assert effort_config_for(model=MODEL_OPUS_47, phase=PHASE_REVIEW) == {
            "effort": "high"
        }

    def test_batch_review_phase_uses_high(self):
        from src.api_config import (
            MODEL_OPUS_47,
            PHASE_BATCH_REVIEW,
            effort_config_for,
        )

        assert effort_config_for(model=MODEL_OPUS_47, phase=PHASE_BATCH_REVIEW) == {
            "effort": "high"
        }

    def test_cross_check_phase_uses_high(self):
        from src.api_config import (
            MODEL_OPUS_47,
            PHASE_CROSS_CHECK,
            effort_config_for,
        )

        assert effort_config_for(model=MODEL_OPUS_47, phase=PHASE_CROSS_CHECK) == {
            "effort": "high"
        }

    def test_haiku_models_omit_effort(self):
        """Directive: do not pass effort to Haiku."""
        from src.api_config import (
            MODEL_HAIKU_45,
            PHASE_TRIAGE,
            PHASE_SYNTHESIS,
            PHASE_VERIFICATION,
            effort_config_for,
        )

        assert effort_config_for(model=MODEL_HAIKU_45, phase=PHASE_TRIAGE) is None
        assert effort_config_for(model=MODEL_HAIKU_45, phase=PHASE_SYNTHESIS) is None
        # Even if a future code path pointed Haiku at the verification
        # phase, the per-model capability flag must short-circuit.
        assert (
            effort_config_for(model=MODEL_HAIKU_45, phase=PHASE_VERIFICATION) is None
        )

    def test_unknown_model_omits_effort(self):
        """Directive 1: whitelist of models that support effort."""
        from src.api_config import (
            PHASE_REVIEW,
            PHASE_VERIFICATION,
            effort_config_for,
        )

        assert effort_config_for(model="future-fancy-model", phase=PHASE_REVIEW) is None
        assert (
            effort_config_for(model="future-fancy-model", phase=PHASE_VERIFICATION)
            is None
        )

    def test_synthesis_and_triage_omit_effort_even_on_supporting_model(self):
        """Synthesis / triage are intentionally not in the phase effort map.
        Even if an operator overrides those phases to Opus / Sonnet (which
        the capability flag does permit), the policy still omits effort
        because the workloads are small classification passes."""
        from src.api_config import (
            MODEL_OPUS_47,
            PHASE_SYNTHESIS,
            PHASE_TRIAGE,
            effort_config_for,
        )

        assert effort_config_for(model=MODEL_OPUS_47, phase=PHASE_SYNTHESIS) is None
        assert effort_config_for(model=MODEL_OPUS_47, phase=PHASE_TRIAGE) is None

    def test_env_disable_flag_omits_effort_everywhere(self, monkeypatch):
        from src.api_config import (
            MODEL_OPUS_47,
            MODEL_SONNET_46,
            PHASE_REVIEW,
            PHASE_VERIFICATION,
            effort_config_for,
        )

        monkeypatch.setenv("SPEC_CRITIC_EFFORT_POLICY", "0")
        assert effort_config_for(model=MODEL_OPUS_47, phase=PHASE_REVIEW) is None
        assert (
            effort_config_for(model=MODEL_SONNET_46, phase=PHASE_VERIFICATION) is None
        )

    def test_env_override_forces_level(self, monkeypatch):
        from src.api_config import (
            MODEL_OPUS_47,
            PHASE_VERIFICATION,
            effort_config_for,
        )

        monkeypatch.setenv("SPEC_CRITIC_EFFORT_OVERRIDE", "low")
        # Even though Opus on verification would normally bump to "high",
        # the override wins.
        assert effort_config_for(model=MODEL_OPUS_47, phase=PHASE_VERIFICATION) == {
            "effort": "low"
        }

    def test_env_override_invalid_value_raises(self, monkeypatch):
        from src.api_config import (
            MODEL_OPUS_47,
            PHASE_VERIFICATION,
            effort_config_for,
        )

        monkeypatch.setenv("SPEC_CRITIC_EFFORT_OVERRIDE", "bananas")
        with pytest.raises(ValueError):
            effort_config_for(model=MODEL_OPUS_47, phase=PHASE_VERIFICATION)

    def test_unknown_model_with_override_still_omits(self, monkeypatch):
        """Override only applies when the model supports effort. An override
        on an unsupported model still omits the field — sending it would
        return a 400 from the API."""
        from src.api_config import PHASE_REVIEW, effort_config_for

        monkeypatch.setenv("SPEC_CRITIC_EFFORT_OVERRIDE", "high")
        assert effort_config_for(model="future-fancy-model", phase=PHASE_REVIEW) is None


class TestApplyEffortConfig:
    """``apply_effort_config`` mirrors ``apply_thinking_config``: it sets
    the key only when applicable, and omits it entirely otherwise."""

    def test_inserts_output_config_for_supported(self):
        from src.api_config import (
            MODEL_SONNET_46,
            PHASE_VERIFICATION,
            apply_effort_config,
        )

        kwargs: dict = {"model": MODEL_SONNET_46}
        apply_effort_config(kwargs, model=MODEL_SONNET_46, phase=PHASE_VERIFICATION)
        assert kwargs["output_config"] == {"effort": "medium"}

    def test_omits_key_for_unsupported(self):
        from src.api_config import (
            MODEL_HAIKU_45,
            PHASE_SYNTHESIS,
            apply_effort_config,
        )

        kwargs: dict = {"model": MODEL_HAIKU_45}
        apply_effort_config(kwargs, model=MODEL_HAIKU_45, phase=PHASE_SYNTHESIS)
        assert "output_config" not in kwargs


# ===========================================================================
# Chunk D1.2 — request-shape wiring assertions
# ===========================================================================
#
# Directive 5: tests assert the actual request payload shape, not just the
# helper return values. The helpers below build the real request kwargs
# that the production code would send to the Anthropic SDK.


class TestEffortPolicyWiredIntoVerifierBuilders:
    def test_retry_request_carries_effort_medium_on_sonnet(self):
        from src.api_config import MODEL_SONNET_46
        from src.verifier import _build_retry_request

        req = _build_retry_request("prompt", cycle=DEFAULT_CYCLE, model=MODEL_SONNET_46)
        assert req.get("output_config") == {"effort": "medium"}

    def test_retry_request_carries_effort_high_on_opus(self):
        from src.api_config import MODEL_OPUS_47
        from src.verifier import _build_retry_request

        req = _build_retry_request("prompt", cycle=DEFAULT_CYCLE, model=MODEL_OPUS_47)
        assert req.get("output_config") == {"effort": "high"}

    def test_continuation_request_carries_effort(self):
        from src.api_config import MODEL_SONNET_46
        from src.verifier import _build_continuation_request

        req = _build_continuation_request(
            "prompt",
            [{"type": "text", "text": "..."}],
            cycle=DEFAULT_CYCLE,
            model=MODEL_SONNET_46,
        )
        assert req.get("output_config") == {"effort": "medium"}


class TestEffortPolicyWiredIntoBatchVerificationBuilder:
    def test_batch_verification_request_carries_effort_medium_on_sonnet(self):
        from src.api_config import MODEL_SONNET_46
        from src.batch import _build_verification_request_params

        params = _build_verification_request_params(
            prompt="verify",
            system_prompt="system",
            model=MODEL_SONNET_46,
            severity="HIGH",
        )
        assert params.get("output_config") == {"effort": "medium"}

    def test_batch_verification_request_carries_effort_high_on_opus(self):
        from src.api_config import MODEL_OPUS_47
        from src.batch import _build_verification_request_params

        params = _build_verification_request_params(
            prompt="verify",
            system_prompt="system",
            model=MODEL_OPUS_47,
            severity="HIGH",
        )
        assert params.get("output_config") == {"effort": "high"}

    def test_disabled_globally_omits_field(self, monkeypatch):
        from src.api_config import MODEL_SONNET_46
        from src.batch import _build_verification_request_params

        monkeypatch.setenv("SPEC_CRITIC_EFFORT_POLICY", "0")
        params = _build_verification_request_params(
            prompt="verify",
            system_prompt="system",
            model=MODEL_SONNET_46,
            severity="HIGH",
        )
        assert "output_config" not in params


class _InlineStreamCtx:
    """Inline mini-version of the fake stream context. Used to keep the
    D1.2 wiring tests self-contained — the broader ``FakeClient`` and
    ``fake_client`` fixture live in ``test_request_payload_shape.py``
    and are not exposed via ``conftest.py``."""

    def __init__(self, final_message):
        self._final = final_message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter([])

    def get_final_message(self):
        return self._final


class _InlineFakeClient:
    """Minimal stand-in capturing stream / batch kwargs."""

    def __init__(self, next_message=None):
        self.captured_stream: list[dict] = []
        self.captured_batches: list[dict] = []
        self._next_message = next_message

    def queue(self, msg):
        self._next_message = msg

    class _MessagesNamespace:
        def __init__(self, outer):
            self._outer = outer

        def stream(self, **kwargs):
            self._outer.captured_stream.append(dict(kwargs))
            return _InlineStreamCtx(self._outer._next_message)

        @property
        def batches(self):
            return _InlineFakeClient._BatchesNamespace(self._outer)

    class _BatchesNamespace:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.captured_batches.append(dict(kwargs))
            return SimpleNamespace(id="batch_fake_1")

    @property
    def messages(self):
        return _InlineFakeClient._MessagesNamespace(self)


@pytest.fixture
def _patched_fake_count_tokens(monkeypatch):
    def _fake_count(text):
        return len((text or "").split()) * 2
    monkeypatch.setattr("src.tokenizer.count_tokens", _fake_count)
    monkeypatch.setattr("src.batch.count_tokens", _fake_count)
    monkeypatch.setattr("src.cross_checker.count_tokens", _fake_count)
    monkeypatch.setattr("src.pipeline.count_tokens", _fake_count, raising=False)


class TestEffortPolicyWiredIntoBatchReview:
    def test_batch_review_carries_effort_high_on_opus(
        self, monkeypatch, _patched_fake_count_tokens
    ):
        from src.api_config import MODEL_OPUS_47, REVIEW_OUTPUT_CAP
        from src.batch import submit_review_batch
        from src import batch as batch_mod
        from src.extractor import ExtractedSpec

        client = _InlineFakeClient()
        monkeypatch.setattr(batch_mod, "_get_client", lambda: client)

        spec = ExtractedSpec(
            filename="A.docx",
            content="Spec body.",
            word_count=2,
            source_path="",
            source_format="docx",
            paragraph_map=None,
        )
        submit_review_batch([spec], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        assert client.captured_batches
        first_params = client.captured_batches[0]["requests"][0]["params"]
        assert first_params.get("output_config") == {"effort": "high"}
        # Sanity check on cap so the test fails noisily if wiring also
        # broke the unrelated request shape.
        assert first_params["max_tokens"] == REVIEW_OUTPUT_CAP


class TestEffortPolicyWiredIntoRealtimeReview:
    def test_realtime_review_request_carries_effort_high(
        self, monkeypatch, fake_anthropic, _patched_fake_count_tokens
    ):
        from src.api_config import MODEL_OPUS_47
        from src.reviewer import _stream_review
        from src import reviewer as reviewer_mod

        client = _InlineFakeClient(next_message=fake_anthropic.review_tool_use_response())
        monkeypatch.setattr(reviewer_mod, "_get_client", lambda: client)
        _stream_review(
            client,
            system_prompt="sys",
            user_message="user",
            model=MODEL_OPUS_47,
        )
        assert client.captured_stream
        params = client.captured_stream[0]
        assert params.get("output_config") == {"effort": "high"}


# ===========================================================================
# Chunk D1.3 — verification escalation telemetry
# ===========================================================================


class TestEscalationTelemetryStampsLiveResult:
    """The verifier records before-and-after fields whenever the Opus
    escalation path fires. ``escalation_attempted`` is True regardless
    of whether the escalated result was the one we kept; the remaining
    fields describe the transition for diagnostics aggregation."""

    def _patch_escalation_path(
        self,
        monkeypatch,
        *,
        initial: VerificationResult,
        escalated: VerificationResult,
        do_escalate: bool = True,
    ):
        from src import verifier as verifier_mod

        # Force ``classify_finding_for_verification`` to return ``web_required``
        # so the local-skip short-circuit doesn't fire.
        monkeypatch.setattr(
            verifier_mod, "classify_finding_for_verification", lambda _f: "web_required"
        )
        # Pretend the escalation router fires (so we exercise the branch).
        monkeypatch.setattr(
            verifier_mod, "should_escalate_verification",
            lambda *_a, **_k: do_escalate,
        )
        # Stage two _run_verification_call returns: first the initial
        # Sonnet pass, then the Opus escalation pass.
        queue = [initial, escalated]

        def _fake_run(_finding, *, cycle, model, max_retries, escalated):
            return queue.pop(0)

        monkeypatch.setattr(verifier_mod, "_run_verification_call", _fake_run)
        # Pin both routing endpoints so this test is robust to other
        # tests that ``importlib.reload`` api_config / verification_modes
        # (which leaves the verifier module's bound names stale).
        monkeypatch.setattr(
            verifier_mod, "initial_verification_model", lambda: "claude-sonnet-4-6"
        )
        monkeypatch.setattr(
            verifier_mod, "escalation_verification_model", lambda: "claude-opus-4-7"
        )

    def test_escalation_changed_verdict_records_before_and_after(self, monkeypatch):
        from src.verifier import verify_finding

        initial = VerificationResult(
            verdict="UNVERIFIED",
            explanation="Initial Sonnet failed to ground",
            grounded=False,
            model_used="claude-sonnet-4-6",
            escalated=False,
            cache_status="miss",
            web_search_requests=2,
            successful_source_count=0,
            search_error_count=0,
        )
        escalated = VerificationResult(
            verdict="CONFIRMED",
            explanation="Opus found the citation",
            grounded=True,
            model_used="claude-opus-4-7",
            escalated=True,
            cache_status="miss",
            web_search_requests=3,
            successful_source_count=1,
            search_error_count=0,
            sources=["https://example.gov/x"],
        )
        self._patch_escalation_path(monkeypatch, initial=initial, escalated=escalated)

        # Pass ``model=`` explicitly so the function bypasses the
        # mode-policy lookup (which other tests in the suite may have
        # invalidated by reloading verification_modes).
        result = verify_finding(
            _finding(severity="CRITICAL"),
            max_retries=0,
            model="claude-sonnet-4-6",
        )
        assert result.escalation_attempted is True
        assert result.initial_model == "claude-sonnet-4-6"
        assert result.initial_verdict == "UNVERIFIED"
        # The kept result IS the escalated one (it grounded).
        assert result.verdict == "CONFIRMED"
        assert result.escalation_changed_verdict is True
        # initial_unverified is the canonical reason tag (router branch:
        # verdict == UNVERIFIED).
        assert result.escalation_reason == "initial_unverified"

    def test_escalation_no_change_still_records_attempt(self, monkeypatch):
        """Escalation that did not change the verdict still records
        ``escalation_attempted=True`` and ``escalation_changed_verdict=False``.
        This is the metric for "wasted escalation budget."""
        from src.verifier import verify_finding

        initial = VerificationResult(
            verdict="UNVERIFIED",
            explanation="Initial Sonnet failed",
            grounded=False,
            model_used="claude-sonnet-4-6",
            escalated=False,
            cache_status="miss",
            web_search_requests=2,
            successful_source_count=0,
            search_error_count=0,
        )
        # Escalation also produced UNVERIFIED, ungrounded — the verifier
        # falls back to the initial result.
        escalated = VerificationResult(
            verdict="UNVERIFIED",
            explanation="Opus also failed to ground",
            grounded=False,
            model_used="claude-opus-4-7",
            escalated=True,
            cache_status="miss",
            web_search_requests=4,
            successful_source_count=0,
            search_error_count=0,
        )
        self._patch_escalation_path(monkeypatch, initial=initial, escalated=escalated)

        result = verify_finding(
            _finding(severity="HIGH"),
            max_retries=0,
            model="claude-sonnet-4-6",
        )
        assert result.escalation_attempted is True
        # Verdict did not change.
        assert result.initial_verdict == "UNVERIFIED"
        assert result.verdict == "UNVERIFIED"
        assert result.escalation_changed_verdict is False

    def test_no_escalation_leaves_telemetry_empty(self, monkeypatch):
        """When the router does not request escalation, the new
        telemetry fields stay at their defaults (no false positives)."""
        from src.verifier import verify_finding

        initial = VerificationResult(
            verdict="CONFIRMED",
            explanation="Sonnet got it on the first pass",
            grounded=True,
            model_used="claude-sonnet-4-6",
            escalated=False,
            cache_status="miss",
            web_search_requests=2,
            successful_source_count=1,
            search_error_count=0,
            sources=["https://example.gov/x"],
        )
        # The escalated value should never be consumed; using a sentinel
        # makes a misrouted code path obvious.
        sentinel = VerificationResult(verdict="DISPUTED")
        self._patch_escalation_path(
            monkeypatch, initial=initial, escalated=sentinel, do_escalate=False
        )

        result = verify_finding(
            _finding(severity="HIGH"),
            max_retries=0,
            model="claude-sonnet-4-6",
        )
        assert result.escalation_attempted is False
        assert result.initial_model == ""
        assert result.initial_verdict == ""
        assert result.escalation_changed_verdict is False
        assert result.escalation_reason == ""

    def test_escalation_reason_classifies_router_branch(self):
        """The escalation reason tag mirrors which router branch fired."""
        from src.verifier import _classify_escalation_reason

        unverified = VerificationResult(
            verdict="UNVERIFIED", grounded=False, search_error_count=0
        )
        ungrounded = VerificationResult(
            verdict="CONFIRMED", grounded=False, search_error_count=0
        )
        all_errors = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            search_error_count=2,
            successful_source_count=0,
        )

        assert _classify_escalation_reason(unverified) == "initial_unverified"
        assert _classify_escalation_reason(ungrounded) == "initial_ungrounded"
        assert _classify_escalation_reason(all_errors) == "initial_all_search_errors"


class TestEscalationTelemetryDiagnostics:
    """The diagnostics summary rolls up the new fields into an
    ``escalation_stats`` block so reports can answer "is escalation
    actually paying off?"."""

    def test_no_escalation_attempts_produces_zero_stats(self):
        from src.diagnostics import DiagnosticsReport

        diag = DiagnosticsReport()
        diag.log(
            "verification",
            "info",
            "Verified",
            {
                "verdict": "CONFIRMED",
                "finding_severity": "HIGH",
                "escalation_attempted": False,
                "verification_mode": "standard_reasoning",
            },
        )
        stats = diag.summary()["escalation_stats"]
        assert stats["attempts"] == 0
        assert stats["change_rate"] == 0.0

    def test_attempts_with_change_aggregate(self):
        from src.diagnostics import DiagnosticsReport

        diag = DiagnosticsReport()
        # One CRITICAL escalation that changed the verdict.
        diag.log(
            "verification",
            "info",
            "Verified",
            {
                "verdict": "CONFIRMED",
                "finding_severity": "CRITICAL",
                "escalation_attempted": True,
                "escalation_changed_verdict": True,
                "initial_verdict": "UNVERIFIED",
                "escalation_reason": "initial_unverified",
            },
        )
        # One HIGH escalation that did NOT change the verdict.
        diag.log(
            "verification",
            "info",
            "Verified",
            {
                "verdict": "UNVERIFIED",
                "finding_severity": "HIGH",
                "escalation_attempted": True,
                "escalation_changed_verdict": False,
                "initial_verdict": "UNVERIFIED",
                "escalation_reason": "initial_unverified",
            },
        )

        stats = diag.summary()["escalation_stats"]
        assert stats["attempts"] == 2
        assert stats["changed_verdict"] == 1
        assert stats["no_change"] == 1
        assert stats["change_rate"] == 0.5
        assert stats["by_reason"] == {"initial_unverified": 2}
        assert stats["by_severity"] == {"CRITICAL": 1, "HIGH": 1}
        assert stats["by_initial_verdict"] == {"UNVERIFIED": 2}
        assert stats["by_final_verdict"] == {"CONFIRMED": 1, "UNVERIFIED": 1}

    def test_to_text_renders_escalation_block_when_attempted(self):
        from src.diagnostics import DiagnosticsReport

        diag = DiagnosticsReport()
        diag.log(
            "verification",
            "info",
            "Verified",
            {
                "verdict": "CONFIRMED",
                "finding_severity": "CRITICAL",
                "escalation_attempted": True,
                "escalation_changed_verdict": True,
                "initial_verdict": "UNVERIFIED",
                "escalation_reason": "initial_unverified",
            },
        )

        text = diag.to_text()
        assert "Escalation:" in text
        assert "attempts=1" in text
        assert "changed=1" in text
        # change rate should render as a percentage.
        assert "100.0%" in text

    def test_to_text_omits_escalation_block_when_no_attempts(self):
        """Hide the escalation section on runs with no escalation activity
        so the diagnostics output stays compact."""
        from src.diagnostics import DiagnosticsReport

        diag = DiagnosticsReport()
        diag.log(
            "verification",
            "info",
            "Verified",
            {
                "verdict": "CONFIRMED",
                "finding_severity": "HIGH",
                "escalation_attempted": False,
            },
        )
        assert "Escalation:" not in diag.to_text()


class TestEffortPolicyWiredIntoCrossChecker:
    def test_cross_check_request_carries_effort_high(
        self, monkeypatch, fake_anthropic, _patched_fake_count_tokens
    ):
        from src.api_config import MODEL_OPUS_47
        from src import cross_checker as cc
        from src.extractor import ExtractedSpec

        client = _InlineFakeClient(next_message=fake_anthropic.review_tool_use_response())
        monkeypatch.setattr(cc, "_get_client", lambda: client)

        specs = [
            ExtractedSpec(
                filename="A.docx",
                content="A " * 50,
                word_count=50,
                source_path="",
                source_format="docx",
                paragraph_map=None,
            ),
            ExtractedSpec(
                filename="B.docx",
                content="B " * 50,
                word_count=50,
                source_path="",
                source_format="docx",
                paragraph_map=None,
            ),
        ]
        cc.run_cross_check(specs, existing_findings=[], model=MODEL_OPUS_47)
        assert client.captured_stream, "cross_checker should have issued a request"
        params = client.captured_stream[0]
        assert params.get("output_config") == {"effort": "high"}
