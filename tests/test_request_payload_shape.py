"""Chunk A — request-payload shape tests.

Captures the request kwargs the production code passes to the Anthropic SDK
without making any network calls. These tests are the primary safety net
for later chunks that touch request construction (Chunk B model-aware
thinking, Chunk C verification tool payload consistency, Chunk D parser
unification, Chunk E token / output budget enforcement).

Goal: each verified request path is captured into a typed
``CapturedRequest`` so later chunks can add assertions without re-writing
the capture plumbing. If a future change drops the verdict tool from a
verification path, or sends ``thinking`` to a model that does not support
it, these tests should fail at the request-shape layer rather than at the
API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.api_config import (
    BATCH_OUTPUT_BETA,
    MODEL_HAIKU_45,
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    REVIEW_OUTPUT_CAP,
    VERIFICATION_OUTPUT_CAP,
)
from src.code_cycles import DEFAULT_CYCLE
from src.extractor import ExtractedSpec
from src.reviewer import Finding
from src.structured_schemas import (
    CROSS_CHECK_TOOL_NAME,
    REVIEW_TOOL_NAME,
    VERIFICATION_TOOL_NAME,
)


pytestmark = pytest.mark.request_shape


# ---------------------------------------------------------------------------
# Capture plumbing
# ---------------------------------------------------------------------------


@dataclass
class CapturedRequest:
    """One request worth of kwargs captured at the SDK boundary."""
    endpoint: str  # "stream" | "batches.create" | "beta.batches.create"
    kwargs: dict[str, Any] = field(default_factory=dict)

    def messages(self) -> list[dict[str, Any]]:
        return list(self.kwargs.get("messages") or [])

    def tools(self) -> list[dict[str, Any]]:
        return list(self.kwargs.get("tools") or [])

    def tool_names(self) -> list[str]:
        return [t.get("name") or t.get("type") for t in self.tools()]

    def system(self) -> Any:
        return self.kwargs.get("system")

    def thinking(self) -> Any:
        return self.kwargs.get("thinking")


@dataclass
class CapturedBatch:
    """Recording for a single ``messages.batches.create`` call."""
    endpoint: str
    betas: list[str] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def params_for(self, custom_id: str) -> dict[str, Any]:
        for req in self.requests:
            if req.get("custom_id") == custom_id:
                return req.get("params", {})
        raise KeyError(custom_id)

    def first_params(self) -> dict[str, Any]:
        if not self.requests:
            raise AssertionError("No batch requests captured")
        return self.requests[0]["params"]


class _FakeStreamCtx:
    """Context manager returned by ``client.messages.stream(...)``."""
    def __init__(self, final_message: Any):
        self._final = final_message

    def __enter__(self) -> "_FakeStreamCtx":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    @property
    def text_stream(self):
        return iter([])  # no streaming text; tests only care about the final message

    def get_final_message(self):
        return self._final


class FakeClient:
    """Capture Anthropic SDK calls; return fake responses.

    Wires up the three endpoints the production code touches:
      - ``messages.stream(...)`` — used by real-time review/verification/cross-check.
      - ``messages.batches.create(requests=[...])`` — used by batch review/verification.
      - ``beta.messages.batches.create(requests=[...], betas=[...])`` — extended output.

    Each call appends a ``CapturedRequest`` / ``CapturedBatch`` onto
    ``self.captured``. ``next_message`` controls what ``stream.get_final_message``
    returns next so tests can simulate any of the five fake-response cases.
    """

    def __init__(self, default_final_message: Any | None = None):
        self.captured: list[Any] = []
        self.default_final_message = default_final_message
        self._queued_messages: list[Any] = []

    # ----- programming helpers -----------------------------------------

    def queue_response(self, message: Any) -> None:
        self._queued_messages.append(message)

    def _pop_message(self) -> Any:
        if self._queued_messages:
            return self._queued_messages.pop(0)
        return self.default_final_message

    # ----- SDK surface -------------------------------------------------

    @property
    def messages(self):
        return _MessagesNamespace(self)

    @property
    def beta(self):
        return _BetaNamespace(self)


class _MessagesNamespace:
    def __init__(self, client: FakeClient):
        self._client = client

    def stream(self, **kwargs):
        self._client.captured.append(
            CapturedRequest(endpoint="stream", kwargs=kwargs)
        )
        return _FakeStreamCtx(self._client._pop_message())

    @property
    def batches(self):
        return _BatchesNamespace(self._client, beta=False)


class _BetaNamespace:
    def __init__(self, client: FakeClient):
        self._client = client

    @property
    def messages(self):
        return _BetaMessagesNamespace(self._client)


class _BetaMessagesNamespace:
    def __init__(self, client: FakeClient):
        self._client = client

    @property
    def batches(self):
        return _BatchesNamespace(self._client, beta=True)


class _BatchesNamespace:
    def __init__(self, client: FakeClient, *, beta: bool):
        self._client = client
        self._beta = beta

    def create(self, **kwargs):
        endpoint = "beta.batches.create" if self._beta else "batches.create"
        self._client.captured.append(
            CapturedBatch(
                endpoint=endpoint,
                betas=list(kwargs.get("betas") or []),
                requests=list(kwargs.get("requests") or []),
            )
        )
        return type("FakeBatchObject", (), {"id": "batch_fake_1"})()


# ---------------------------------------------------------------------------
# Builders / helpers
# ---------------------------------------------------------------------------


def _spec(content: str = "Sample spec content.", filename: str = "23 21 13 - Hydronic.docx") -> ExtractedSpec:
    return ExtractedSpec(
        filename=filename,
        content=content,
        word_count=len(content.split()),
        source_path="",
        source_format="docx",
        paragraph_map=None,
    )


def _finding(**overrides) -> Finding:
    base = dict(
        severity="HIGH",
        fileName="23 21 13 - Hydronic.docx",
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


@pytest.fixture(autouse=True)
def _stub_count_tokens(monkeypatch):
    """Stub the tiktoken-backed token counter to keep tests offline.

    The real ``tokenizer.count_tokens`` lazily downloads the cl100k_base
    BPE merge tables on first call. We replace it with a cheap word-count
    proxy so request-shape tests work in fully offline environments and
    so they never trigger the lazy download in CI. Each module that did
    ``from .tokenizer import count_tokens`` keeps its own binding, so we
    patch all of them.
    """
    def _fake_count(text: str | None) -> int:
        return len((text or "").split()) * 2  # rough words→tokens proxy
    monkeypatch.setattr("src.tokenizer.count_tokens", _fake_count)
    monkeypatch.setattr("src.batch.count_tokens", _fake_count)
    monkeypatch.setattr("src.cross_checker.count_tokens", _fake_count)
    monkeypatch.setattr("src.pipeline.count_tokens", _fake_count, raising=False)


@pytest.fixture
def fake_client(monkeypatch, fake_anthropic):
    """Yield a FakeClient that backs both ``reviewer._get_client`` and
    ``batch._get_client`` / ``verifier._get_client``."""
    from src import batch as batch_mod
    from src import reviewer as reviewer_mod
    from src import verifier as verifier_mod
    from src import cross_checker as cc_mod

    client = FakeClient(
        default_final_message=fake_anthropic.review_tool_use_response(),
    )

    def _provider() -> FakeClient:
        return client

    monkeypatch.setattr(reviewer_mod, "_get_client", _provider)
    monkeypatch.setattr(batch_mod, "_get_client", _provider)
    monkeypatch.setattr(verifier_mod, "_get_client", _provider)
    monkeypatch.setattr(cc_mod, "_get_client", _provider)
    return client


# ---------------------------------------------------------------------------
# Batch review request shape
# ---------------------------------------------------------------------------


class TestBatchReviewRequestShape:
    def test_emits_one_request_per_spec(self, fake_client):
        from src.batch import submit_review_batch

        specs = [_spec(filename="A.docx"), _spec(filename="B.docx")]
        submit_review_batch(specs, model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)

        assert len(fake_client.captured) == 1
        batch = fake_client.captured[0]
        assert isinstance(batch, CapturedBatch)
        assert len(batch.requests) == 2

    def test_request_carries_review_tool(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        tool_names = [t.get("name") for t in (params.get("tools") or [])]
        assert REVIEW_TOOL_NAME in tool_names

    def test_request_carries_tool_choice_auto(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        choice = params.get("tool_choice")
        # Forcing tool_choice is incompatible with adaptive thinking; the
        # production code documents and tests for "auto".
        assert choice == {"type": "auto", "disable_parallel_tool_use": True}

    def test_request_carries_adaptive_thinking_for_opus(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        # Chunk B will tighten this per-model; Opus continues to receive thinking.
        assert params.get("thinking") == {"type": "adaptive"}

    def test_normal_input_uses_baseline_review_cap(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        assert params["max_tokens"] == REVIEW_OUTPUT_CAP

    def test_small_input_does_not_trigger_extended_output_beta(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        batch = fake_client.captured[0]
        assert batch.endpoint == "batches.create"
        assert BATCH_OUTPUT_BETA not in batch.betas

    def test_system_prompt_is_cache_tagged_when_enabled(self, fake_client, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_PROMPT_CACHE", "1")
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        system = params["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"]["type"] == "ephemeral"

    def test_user_message_is_a_single_text_block(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec(content="Spec body.")], model=MODEL_OPUS_47, cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        messages = params["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert isinstance(messages[0]["content"], str)


# ---------------------------------------------------------------------------
# Batch verification request shape
# ---------------------------------------------------------------------------


class TestBatchVerificationRequestShape:
    def _build(self, fake_client, **kwargs):
        """Call ``submit_verification_batch`` against ``fake_client``."""
        from src.batch import submit_verification_batch

        def _prompt(_finding):
            return "Verify this finding."

        def _system(_cycle):
            return "You are a verification agent."

        return submit_verification_batch(
            [_finding(**kwargs)],
            _prompt,
            _system,
            cycle=DEFAULT_CYCLE,
        )

    def test_request_carries_web_search_tool(self, fake_client):
        self._build(fake_client)
        params = fake_client.captured[0].first_params()
        types = [t.get("type") for t in params["tools"]]
        assert any(t and t.startswith("web_search_") for t in types)

    def test_request_carries_verdict_tool_when_structured_outputs_enabled(
        self, fake_client, monkeypatch
    ):
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", "1")
        # Reload to pick up the env-driven flag.
        import importlib
        from src import structured_schemas as ss
        importlib.reload(ss)
        from src import batch as batch_mod
        importlib.reload(batch_mod)

        # After reload, re-pin the fake client into the reloaded module.
        def _provider() -> FakeClient:
            return fake_client
        monkeypatch.setattr(batch_mod, "_get_client", _provider)

        def _prompt(_f): return "verify"
        def _system(_c): return "system"
        batch_mod.submit_verification_batch(
            [_finding()], _prompt, _system, cycle=DEFAULT_CYCLE,
        )
        params = fake_client.captured[-1].first_params()
        names = [t.get("name") for t in params["tools"]]
        assert VERIFICATION_TOOL_NAME in names

    def test_request_omits_verdict_tool_when_structured_outputs_disabled(
        self, fake_client, monkeypatch
    ):
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", "0")
        import importlib
        from src import structured_schemas as ss
        importlib.reload(ss)
        from src import batch as batch_mod
        importlib.reload(batch_mod)
        def _provider() -> FakeClient:
            return fake_client
        monkeypatch.setattr(batch_mod, "_get_client", _provider)

        def _prompt(_f): return "verify"
        def _system(_c): return "system"
        batch_mod.submit_verification_batch(
            [_finding()], _prompt, _system, cycle=DEFAULT_CYCLE,
        )
        params = fake_client.captured[-1].first_params()
        names = [t.get("name") for t in params["tools"]]
        assert VERIFICATION_TOOL_NAME not in names

    def test_verification_request_uses_verification_cap(self, fake_client):
        self._build(fake_client)
        params = fake_client.captured[0].first_params()
        # Chunk E will assert per-model caps; the baseline is the verification cap.
        assert params["max_tokens"] <= VERIFICATION_OUTPUT_CAP

    def test_web_search_budget_varies_by_severity(self, fake_client):
        # CRITICAL = 7; GRIPES = 3.
        self._build(fake_client, severity="CRITICAL")
        critical_params = fake_client.captured[0].first_params()
        critical_web = next(
            t for t in critical_params["tools"]
            if (t.get("type") or "").startswith("web_search_")
        )
        assert critical_web["max_uses"] == 7

        self._build(fake_client, severity="GRIPES")
        gripes_params = fake_client.captured[-1].first_params()
        gripes_web = next(
            t for t in gripes_params["tools"]
            if (t.get("type") or "").startswith("web_search_")
        )
        assert gripes_web["max_uses"] == 3

    def test_verification_request_messages_shape(self, fake_client):
        self._build(fake_client)
        params = fake_client.captured[0].first_params()
        # Initial verification: single user turn (no assistant/continuation).
        assert [m["role"] for m in params["messages"]] == ["user"]


# ---------------------------------------------------------------------------
# Verifier retry / continuation request shape (Chunk C/D regression surface)
# ---------------------------------------------------------------------------


class TestVerifierRetryAndContinuationShape:
    @pytest.mark.xfail(
        reason="Chunk C — retry verification request currently omits the "
        "verdict tool; the prompt still asks the model to call it, so "
        "structured outputs are unreachable on the retry path.",
        strict=False,
    )
    def test_retry_request_includes_verdict_tool_by_default(self):
        from src.verifier import _build_retry_request

        req = _build_retry_request("prompt body", cycle=DEFAULT_CYCLE)
        names = [t.get("name") for t in req["tools"]]
        assert VERIFICATION_TOOL_NAME in names

    @pytest.mark.xfail(
        reason="Chunk C — same omission as the retry path; the continuation "
        "request mirrors retry and would also need the verdict tool.",
        strict=False,
    )
    def test_continuation_request_includes_verdict_tool_by_default(self):
        from src.verifier import _build_continuation_request

        req = _build_continuation_request(
            "prompt body",
            [{"type": "text", "text": "partial"}],
            cycle=DEFAULT_CYCLE,
        )
        names = [t.get("name") for t in req["tools"]]
        assert VERIFICATION_TOOL_NAME in names

    def test_continuation_request_user_assistant_user_pattern(self):
        from src.verifier import _build_continuation_request

        req = _build_continuation_request(
            "prompt body",
            [{"type": "text", "text": "partial"}],
            cycle=DEFAULT_CYCLE,
        )
        roles = [m["role"] for m in req["messages"]]
        assert roles == ["user", "assistant", "user"]

    def test_retry_uses_verification_cap_not_review_cap(self):
        from src.verifier import _build_retry_request

        req = _build_retry_request("prompt body", cycle=DEFAULT_CYCLE)
        # Verification caps must stay below review caps so retries don't
        # blanket-allocate 128k.
        assert req["max_tokens"] <= VERIFICATION_OUTPUT_CAP


# ---------------------------------------------------------------------------
# Real-time review request shape via _stream_review
# ---------------------------------------------------------------------------


class TestRealtimeReviewRequestShape:
    def test_stream_review_sends_review_tool(self, fake_client, fake_anthropic):
        from src.reviewer import _stream_review

        fake_client.queue_response(fake_anthropic.review_tool_use_response())
        result = _stream_review(
            fake_client,
            system_prompt="system",
            user_message="user",
            model=MODEL_OPUS_47,
            max_retries=1,
        )
        assert result.error is None or result.error == ""
        req = fake_client.captured[-1]
        assert isinstance(req, CapturedRequest)
        assert req.endpoint == "stream"
        names = [t.get("name") for t in req.tools()]
        assert REVIEW_TOOL_NAME in names

    def test_stream_review_adaptive_thinking(self, fake_client, fake_anthropic):
        from src.reviewer import _stream_review

        fake_client.queue_response(fake_anthropic.review_tool_use_response())
        _stream_review(
            fake_client,
            system_prompt="system",
            user_message="user",
            model=MODEL_OPUS_47,
            max_retries=1,
        )
        req = fake_client.captured[-1]
        # Chunk B will make this model-aware; today Opus uses adaptive thinking.
        assert req.thinking() == {"type": "adaptive"}

    def test_stream_review_max_tokens_truncation_path(self, fake_client, fake_anthropic):
        from src.reviewer import _stream_review

        fake_client.queue_response(fake_anthropic.max_tokens_incomplete_response())
        result = _stream_review(
            fake_client,
            system_prompt="system",
            user_message="user",
            model=MODEL_OPUS_47,
            max_retries=1,
        )
        assert result.parse_status == "incomplete"
        assert result.stop_reason == "max_tokens"


# ---------------------------------------------------------------------------
# Cross-check request shape
# ---------------------------------------------------------------------------


class TestCrossCheckRequestShape:
    def test_cross_check_request_carries_cross_check_tool(self, fake_client, fake_anthropic):
        from src.cross_checker import run_cross_check

        cross_check_message = fake_anthropic.review_tool_use_response(
            payload={
                "coordination_summary": "no coordination issues found",
                "findings": [],
            },
        )
        # Repurpose the review builder to emit a cross-check tool block.
        for block in cross_check_message.content:
            if getattr(block, "type", None) == "tool_use":
                block.name = CROSS_CHECK_TOOL_NAME
        fake_client.queue_response(cross_check_message)

        run_cross_check(
            [_spec(content="Spec A body", filename="A.docx"), _spec(content="Spec B body", filename="B.docx")],
            existing_findings=[],
            cycle=DEFAULT_CYCLE,
        )
        req = fake_client.captured[-1]
        names = [t.get("name") for t in req.tools()]
        assert CROSS_CHECK_TOOL_NAME in names

    def test_cross_check_request_carries_adaptive_thinking_for_opus(self, fake_client, fake_anthropic):
        from src.cross_checker import run_cross_check

        cross_check_message = fake_anthropic.review_tool_use_response(
            payload={"coordination_summary": "ok", "findings": []},
        )
        for block in cross_check_message.content:
            if getattr(block, "type", None) == "tool_use":
                block.name = CROSS_CHECK_TOOL_NAME
        fake_client.queue_response(cross_check_message)

        run_cross_check(
            [_spec(content="A", filename="A.docx"), _spec(content="B", filename="B.docx")],
            existing_findings=[],
            cycle=DEFAULT_CYCLE,
            model=MODEL_OPUS_47,
        )
        req = fake_client.captured[-1]
        assert req.thinking() == {"type": "adaptive"}


# ---------------------------------------------------------------------------
# Chunk B regression coverage — model-aware thinking policy
# ---------------------------------------------------------------------------


class TestModelAwareThinkingRequestShape:
    """Pin the per-model thinking behavior across every request builder.

    Before Chunk B every path hard-coded ``thinking={"type": "adaptive"}``,
    which produced an API error when the synthesis pass switched to Haiku.
    These tests ensure unsupported models never carry the key.
    """

    # ----- Batch review ------------------------------------------------

    def test_batch_review_omits_thinking_for_haiku(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model=MODEL_HAIKU_45, cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        assert "thinking" not in params

    def test_batch_review_includes_thinking_for_sonnet(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model=MODEL_SONNET_46, cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        assert params.get("thinking") == {"type": "adaptive"}

    def test_batch_review_omits_thinking_for_unknown_model(self, fake_client):
        from src.batch import submit_review_batch

        submit_review_batch([_spec()], model="claude-future-2030", cycle=DEFAULT_CYCLE)
        params = fake_client.captured[0].first_params()
        assert "thinking" not in params

    # ----- Real-time review --------------------------------------------

    def test_realtime_review_omits_thinking_for_haiku(self, fake_client, fake_anthropic):
        from src.reviewer import _stream_review

        fake_client.queue_response(fake_anthropic.review_tool_use_response())
        _stream_review(
            fake_client,
            system_prompt="system",
            user_message="user",
            model=MODEL_HAIKU_45,
            max_retries=1,
        )
        req = fake_client.captured[-1]
        assert "thinking" not in req.kwargs

    def test_realtime_review_includes_thinking_for_sonnet(self, fake_client, fake_anthropic):
        from src.reviewer import _stream_review

        fake_client.queue_response(fake_anthropic.review_tool_use_response())
        _stream_review(
            fake_client,
            system_prompt="system",
            user_message="user",
            model=MODEL_SONNET_46,
            max_retries=1,
        )
        req = fake_client.captured[-1]
        assert req.thinking() == {"type": "adaptive"}

    # ----- Batch verification ------------------------------------------

    def test_batch_verification_includes_thinking_for_sonnet_default(self, fake_client):
        from src.batch import submit_verification_batch

        def _prompt(_f): return "verify"
        def _system(_c): return "system"

        submit_verification_batch(
            [_finding()], _prompt, _system, cycle=DEFAULT_CYCLE, model=MODEL_SONNET_46,
        )
        params = fake_client.captured[0].first_params()
        assert params.get("thinking") == {"type": "adaptive"}

    def test_batch_verification_omits_thinking_for_haiku(self, fake_client):
        from src.batch import submit_verification_batch

        def _prompt(_f): return "verify"
        def _system(_c): return "system"

        submit_verification_batch(
            [_finding()], _prompt, _system, cycle=DEFAULT_CYCLE, model=MODEL_HAIKU_45,
        )
        params = fake_client.captured[0].first_params()
        assert "thinking" not in params

    # ----- Retry / continuation ----------------------------------------

    def test_retry_request_includes_thinking_for_sonnet(self):
        from src.verifier import _build_retry_request

        req = _build_retry_request("prompt body", cycle=DEFAULT_CYCLE, model=MODEL_SONNET_46)
        assert req.get("thinking") == {"type": "adaptive"}

    def test_retry_request_omits_thinking_for_haiku(self):
        from src.verifier import _build_retry_request

        req = _build_retry_request("prompt body", cycle=DEFAULT_CYCLE, model=MODEL_HAIKU_45)
        assert "thinking" not in req

    def test_continuation_request_includes_thinking_for_sonnet(self):
        from src.verifier import _build_continuation_request

        req = _build_continuation_request(
            "prompt body",
            [{"type": "text", "text": "partial"}],
            cycle=DEFAULT_CYCLE,
            model=MODEL_SONNET_46,
        )
        assert req.get("thinking") == {"type": "adaptive"}

    def test_continuation_request_omits_thinking_for_haiku(self):
        from src.verifier import _build_continuation_request

        req = _build_continuation_request(
            "prompt body",
            [{"type": "text", "text": "partial"}],
            cycle=DEFAULT_CYCLE,
            model=MODEL_HAIKU_45,
        )
        assert "thinking" not in req

    # ----- Cross-check -------------------------------------------------

    def test_cross_check_omits_thinking_for_haiku(self, fake_client, fake_anthropic):
        from src.cross_checker import run_cross_check

        cross_check_message = fake_anthropic.review_tool_use_response(
            payload={"coordination_summary": "ok", "findings": []},
        )
        for block in cross_check_message.content:
            if getattr(block, "type", None) == "tool_use":
                block.name = CROSS_CHECK_TOOL_NAME
        fake_client.queue_response(cross_check_message)

        run_cross_check(
            [_spec(content="A", filename="A.docx"), _spec(content="B", filename="B.docx")],
            existing_findings=[],
            cycle=DEFAULT_CYCLE,
            model=MODEL_HAIKU_45,
        )
        req = fake_client.captured[-1]
        assert "thinking" not in req.kwargs


class TestSynthesisRequestShape:
    """Regression coverage for the headline Chunk B bug: the cross-discipline
    synthesis pass defaulted to Haiku 4.5 while sending ``thinking``, which
    Anthropic rejects. The request must omit ``thinking`` on the Haiku
    default and add it back when an operator overrides synthesis to Opus."""

    def _stub_chunk_results(self, fake_anthropic):
        """Build the minimum input ``_run_cross_discipline_synthesis`` needs
        to actually emit a request: two completed chunks each with one
        finding, so the early-exit guards don't short-circuit the call."""
        from src.reviewer import Finding, ReviewResult

        f1 = Finding(
            severity="HIGH",
            fileName="23 05 00.docx",
            section="2.1",
            issue="HVAC seismic restraint conflict",
            actionType="EDIT",
            existingText="x",
            replacementText="y",
            codeReference=None,
            confidence=0.7,
        )
        f2 = Finding(
            severity="MEDIUM",
            fileName="22 05 00.docx",
            section="3.1",
            issue="Plumbing chase routing overlap",
            actionType="EDIT",
            existingText="a",
            replacementText="b",
            codeReference=None,
            confidence=0.6,
        )
        return [
            ("div_23", ReviewResult(findings=[f1], cross_check_status="completed")),
            ("div_22", ReviewResult(findings=[f2], cross_check_status="completed")),
        ]

    def _queue_synthesis_response(self, fake_client, fake_anthropic):
        message = fake_anthropic.review_tool_use_response(
            payload={"coordination_summary": "ok", "findings": []},
        )
        for block in message.content:
            if getattr(block, "type", None) == "tool_use":
                block.name = CROSS_CHECK_TOOL_NAME
        fake_client.queue_response(message)

    def test_synthesis_omits_thinking_on_haiku_default(self, fake_client, fake_anthropic):
        """REGRESSION: synthesis used to send ``thinking`` to Haiku, which
        produced an API error. The request must omit the key entirely now."""
        from src.cross_checker import _run_cross_discipline_synthesis

        self._queue_synthesis_response(fake_client, fake_anthropic)
        chunk_results = self._stub_chunk_results(fake_anthropic)
        _run_cross_discipline_synthesis(
            chunk_results,
            cycle=DEFAULT_CYCLE,
            # No model override → falls back to SYNTHESIS_MODEL_DEFAULT (Haiku).
        )
        req = fake_client.captured[-1]
        assert "thinking" not in req.kwargs

    def test_synthesis_adds_thinking_when_overridden_to_opus(self, fake_client, fake_anthropic):
        """Capability is positive on Opus; the helper adds the key back."""
        from src.cross_checker import _run_cross_discipline_synthesis

        self._queue_synthesis_response(fake_client, fake_anthropic)
        chunk_results = self._stub_chunk_results(fake_anthropic)
        _run_cross_discipline_synthesis(
            chunk_results,
            cycle=DEFAULT_CYCLE,
            model=MODEL_OPUS_47,
        )
        req = fake_client.captured[-1]
        assert req.thinking() == {"type": "adaptive"}

    def test_synthesis_omits_thinking_for_sonnet_when_phase_intent_overrides(
        self, fake_client, fake_anthropic, monkeypatch
    ):
        """If a future change adds the synthesis phase to ``_PHASES_NO_THINKING``,
        Sonnet would also drop the key. Today the phase is not in the set
        so Sonnet keeps thinking — pin that current behavior so an accidental
        phase-set edit fails this test."""
        from src.cross_checker import _run_cross_discipline_synthesis

        self._queue_synthesis_response(fake_client, fake_anthropic)
        chunk_results = self._stub_chunk_results(fake_anthropic)
        _run_cross_discipline_synthesis(
            chunk_results,
            cycle=DEFAULT_CYCLE,
            model=MODEL_SONNET_46,
        )
        req = fake_client.captured[-1]
        assert req.thinking() == {"type": "adaptive"}


class TestNoLiteralThinkingPayloadsRemain:
    """Repo-wide guard: every Anthropic request path must go through
    ``apply_thinking_config``. A future developer who hand-rolls
    ``"thinking": {"type": "adaptive"}`` into a new path will trip this."""

    def test_no_hardcoded_thinking_payloads_in_src(self):
        import pathlib
        import re

        # api_config defines the literal inside the policy + comments.
        # That's the one allowed location.
        repo_src = pathlib.Path(__file__).resolve().parent.parent / "src"
        offenders: list[str] = []
        pattern = re.compile(r"""thinking["']?\s*[:=]\s*\{["']type["']""")
        for path in repo_src.glob("*.py"):
            if path.name == "api_config.py":
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line) and "type" in line and "adaptive" in line:
                    # Skip comment-only lines (e.g. docstring references).
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith('"'):
                        continue
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
        assert not offenders, (
            "Hardcoded thinking payloads found outside api_config.py: "
            + "; ".join(offenders)
        )
