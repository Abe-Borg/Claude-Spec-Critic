"""Transient failures retry one request, with one budget for the whole loop."""
from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from src.core.api_config import MODEL_OPUS_55, MODEL_SONNET_55, RESUME_TAIL_CACHE_CONTROL
from src.core.attempt_usage import attempts_from
from src.core.code_cycles import DEFAULT_CYCLE
from src.orchestration.diagnostics import DiagnosticsReport
from src.research import requirements_research as R
from src.verification import retry_policy as P
from src.verification import verifier as V
from tests.fixtures.fake_anthropic import (
    FakeContainer,
    FakeThinkingBlock,
    research_tool_use_response,
)
from tests.fixtures.preserved_thinking import preserved_thinking_violations
from tests.fixtures.retry_timing import install_fake_retry_timing
from tests.fixtures import verification_drivers as D
from tests.test_requirements_research import _complete_profile, _dimension, _enabled_module


def _error(status=429, *, headers=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    if status is None:
        return anthropic.APIConnectionError(request=request)
    cls = anthropic.RateLimitError if status == 429 else anthropic.APIStatusError
    return cls(
        "container expired" if status in (400, 422) else f"HTTP {status}",
        response=httpx2.Response(status, request=request, headers=headers), body=None,
    )


def _pause(searches=1, *, signature="sig-1", container=True):
    response = D.message(
        [FakeThinkingBlock(thinking="", signature=signature), *D.search_blocks()],
        searches=searches, stop_reason="pause_turn",
    )
    if container:
        response.container = FakeContainer(id="container_first")
    return response


class _Gate:
    held = False
    acquisitions = 0

    def __enter__(self):
        assert not self.held
        self.held = True
        self.acquisitions += 1

    def __exit__(self, *_):
        self.held = False


@pytest.fixture(params=["research", "verification", "escalation"])
def loop(request, monkeypatch):
    kind = request.param
    gate = _Gate()
    timing = install_fake_retry_timing(monkeypatch, held_probe=lambda: gate.held)
    monkeypatch.setenv("SPEC_CRITIC_CACHE_DIAGNOSTICS", "1")
    if kind == "research":
        final = research_tool_use_response(
            payload={"items": [{
                "category": "governing_code", "topic": "spacing",
                "requirement": "Check sprinkler spacing.", "confidence": 0.9,
                "source_urls": [D.SEARCHED_URL],
            }]}, searched_urls=[],
        )
    else:
        final = D.message([D.verdict_call(D.verdict_payload())], searches=0)

    def run(script, *, retries=2):
        remaining = iter(script)
        bodies = []
        exchanges = []

        def route(kwargs):
            assert gate.held
            body = copy.deepcopy(kwargs)
            bodies.append(body)
            response = next(remaining)
            exchanges.append((body, None if isinstance(response, Exception) else response.content))
            return response

        client = D.ScriptedStreamClient(route)
        if kind == "research":
            monkeypatch.setattr(R, "DEFAULT_REALTIME_RETRY_POLICY", replace(
                R.DEFAULT_REALTIME_RETRY_POLICY, max_attempts=retries + 1,
            ))
            result = R._run_dimension(
                client, module=_enabled_module(), profile=_complete_profile(),
                dimension=_dimension(), corpus_signals_block="", model=MODEL_SONNET_55,
                call_gate=gate,
            )
            searches = result.status.web_search_requests
            succeeded = result.status.status == "completed"
        else:
            monkeypatch.setattr(V, "_get_client", lambda **_: client)
            result = V._run_verification_call(
                D.medium_finding(), cycle=DEFAULT_CYCLE,
                model=MODEL_OPUS_55 if kind == "escalation" else MODEL_SONNET_55,
                max_retries=retries, escalated=kind == "escalation", call_gate=gate,
            )
            searches = result.web_search_requests
            succeeded = not result.verification_failed
        return SimpleNamespace(
            result=result, bodies=bodies, exchanges=exchanges,
            attempts=attempts_from(result.call_usage), searches=searches, succeeded=succeeded,
        )

    return SimpleNamespace(kind=kind, run=run, final=final, gate=gate, timing=timing)


@pytest.mark.parametrize("status", [429, 529, 500, 502, None])
def test_second_continuation_retries_identical_request(loop, status):
    first = _pause(2)
    # A response without a container must retain the container from call 1.
    second = _pause(1, signature="sig-2", container=False)
    run = loop.run([first, second, _error(status), loop.final])

    assert run.succeeded
    assert len(run.bodies) == 4  # Three useful calls, exactly one extra request.
    failed, resent = run.bodies[2:]
    assert failed == resent
    assert len(resent["messages"]) == 3
    assert resent["messages"][:2] == run.bodies[1]["messages"]
    assert resent["container"] == "container_first"
    assert resent["cache_control"] == RESUME_TAIL_CACHE_CONTROL
    if loop.kind != "research":
        assert resent["extra_body"]["diagnostics"]["previous_message_id"] == second.id
    assert preserved_thinking_violations(run.exchanges) == []
    assert run.searches == 3
    assert [a.usage_known for a in run.attempts] == [True, True, False, True]
    assert [a.web_search_requests for a in run.attempts] == [2, 1, 0, 0]
    assert [a.message_id for a in run.attempts] == [first.id, second.id, "", loop.final.id]
    assert [w.held_permit for w in loop.timing.wait_log] == [False]
    assert loop.gate.acquisitions == 4 and not loop.gate.held
    if loop.kind == "research":
        assert run.result.items[0].grounded
        assert run.result.spent["api_requests"] == 4
    else:
        assert run.result.grounded
    if loop.kind == "escalation":
        assert all(a.role == "escalation" and a.model == MODEL_OPUS_55 for a in run.attempts)


def test_retry_budget_is_shared_across_continuations(loop):
    first, second = _pause(), _pause(signature="sig-2", container=False)
    run = loop.run([first, _error(), second, _error(), _error(), loop.final])

    assert not run.succeeded
    assert len(run.bodies) == 5  # Exactly two retries in total, not per request.
    assert run.bodies[1] == run.bodies[2]
    assert run.bodies[3] == run.bodies[4]
    assert run.searches == 2
    assert [a.usage_known for a in run.attempts] == [True, False, True, False, False]
    assert len(loop.timing.wait_log) == 2
    assert not loop.gate.held


def test_reminder_request_retries_without_duplicating_the_reminder(loop):
    silent = _pause(2)
    silent.stop_reason = "end_turn"
    run = loop.run([silent, _error(), loop.final])

    assert run.succeeded
    assert len(run.bodies) == 3
    assert run.bodies[1] == run.bodies[2]
    assert [m["role"] for m in run.bodies[2]["messages"]] == ["user", "assistant", "user"]
    assert run.searches == 2
    assert [a.usage_known for a in run.attempts] == [True, False, True]
    assert preserved_thinking_violations(run.exchanges) == []


def test_retry_does_not_reset_the_continuation_cap(loop, monkeypatch):
    monkeypatch.setattr(R, "RESEARCH_MAX_CONTINUATIONS", 2)
    original_routing = V.select_routing
    monkeypatch.setattr(V, "select_routing", lambda *a, **k: replace(
        original_routing(*a, **k), max_continuations=2,
    ))
    run = loop.run([
        _pause(), _error(), _pause(signature="sig-2"),
        _pause(0, signature="sig-3"), loop.final,
    ])
    assert run.searches == 2
    if loop.kind == "research":
        # The retry did not reset the cap: the third pause spends it and
        # receives a final submit-only turn instead of another continuation.
        assert len(run.bodies) == len(run.attempts) == 5
        assert run.succeeded
        assert run.result.budget_reminder_reason == "continuations"
        assert run.bodies[-1]["messages"][-1]["content"] == R.RESEARCH_BUDGET_SUBMIT_REMINDER
    else:
        assert len(run.bodies) == len(run.attempts) == 4
        assert run.result.outcome == V.OUTCOME_CONTINUATION_CAP
        assert run.result.retry_telemetry["continuation_count"] == 3


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("declined", [False, True])
def test_invalid_resume_can_restart_within_the_existing_budget(loop, caplog, status, declined):
    first = _pause(2)
    fresh_final = loop.final if loop.kind == "research" else D.message(
        D.search_blocks("https://fresh.example.gov/code") + loop.final.content, searches=1,
    )
    invalid = _error(status, headers={"x-should-retry": "false"} if declined else None)
    run = loop.run([first, _error(), invalid, fresh_final])

    assert run.succeeded
    assert len(run.bodies) == 4
    assert run.bodies[1] == run.bodies[2]
    fresh = run.bodies[3]
    assert fresh["messages"] == run.bodies[0]["messages"]
    assert "container" not in fresh and "cache_control" not in fresh
    assert "extra_body" not in fresh
    assert [a.usage_known for a in run.attempts] == [True, False, False, True]
    assert sum(a.web_search_requests for a in run.attempts) == 2 + (loop.kind != "research")
    reasons = (
        run.result.conversation_restarts if loop.kind == "research"
        else run.result.retry_telemetry["conversation_restarts"]
    )
    assert len(reasons) == 1 and "container expired" in reasons[0]
    assert [w.held_permit for w in loop.timing.wait_log] == [False, False]
    if loop.kind != "research":
        # Earlier evidence is not used to ground a fresh conversation.
        assert not run.result.grounded
        assert "starting a fresh conversation" in caplog.text


@pytest.mark.parametrize("status", [400, 422])
def test_invalid_resume_cannot_expand_the_retry_budget(loop, status):
    invalid = _error(status, headers={"x-should-retry": "false"})
    run = loop.run([_pause(), _error(), invalid, loop.final], retries=1)
    assert not run.succeeded
    assert len(run.bodies) == 3
    assert run.bodies[1] == run.bodies[2]
    assert len(run.attempts) == 3
    assert len(loop.timing.wait_log) == 1


def test_invalid_resume_restart_respects_the_wait_budget(loop):
    invalid = _error(400, headers={"x-should-retry": "false", "retry-after": "3600"})
    run = loop.run([_pause(), invalid, loop.final])
    assert not run.succeeded
    assert len(run.bodies) == len(run.attempts) == 2
    assert [a.usage_known for a in run.attempts] == [True, False]
    assert not loop.timing.wait_log


@pytest.mark.parametrize("status", [429, 529, 500])
def test_server_declined_identical_continuation_is_not_retried(loop, status):
    run = loop.run([_pause(), _error(status, headers={"x-should-retry": "false"}), loop.final])
    assert not run.succeeded
    assert len(run.bodies) == len(run.attempts) == 2
    assert [a.usage_known for a in run.attempts] == [True, False]
    assert not loop.timing.wait_log


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_invalid_initial_request_does_not_restart(loop, status):
    run = loop.run([_error(status), loop.final])
    assert not run.succeeded
    assert len(run.bodies) == len(run.attempts) == 1
    assert not run.attempts[0].usage_known
    assert not loop.timing.wait_log


@pytest.mark.parametrize("status", [401, 403, 404])
def test_account_errors_on_a_resume_do_not_restart(loop, status):
    run = loop.run([_pause(), _error(status), loop.final])
    assert not run.succeeded
    assert len(run.bodies) == len(run.attempts) == 2
    assert not loop.timing.wait_log


def test_cancelled_retry_does_not_record_any_request_twice(loop, monkeypatch):
    monkeypatch.setattr(P, "DEFAULT_RETRY_TIMING", replace(
        P.DEFAULT_RETRY_TIMING, wait=lambda *_: False,
    ))
    run = loop.run([_pause(2), _error(), loop.final])
    assert not run.succeeded
    assert len(run.bodies) == len(run.attempts) == 2
    assert [a.usage_known for a in run.attempts] == [True, False]
    assert sum(a.web_search_requests for a in run.attempts) == 2
    assert not loop.gate.held


def test_research_restart_is_logged_and_diagnosed(monkeypatch):
    install_fake_retry_timing(monkeypatch)
    diag = DiagnosticsReport()
    messages = []
    script = iter([_pause(2), _error(), _error(400), research_tool_use_response()])
    client = D.ScriptedStreamClient(lambda _: next(script))
    profile = R.run_requirements_research(
        _enabled_module(), _complete_profile(), client=client, diag=diag,
        log=lambda msg, **_: messages.append(msg),
    )
    assert any("starting a fresh conversation" in msg for msg in messages)
    event, = [e for e in diag.events if e.data and e.data.get("dimension_id") == "alpha"]
    assert len(event.data["conversation_restarts"]) == 1
    assert len(event.data["attempts"]) == 4
    assert profile.run_usage["api_requests"] == 4
    assert profile.run_usage["web_search_requests"] == 3
    assert diag.summary()["cost_summary"]["unknown_usage_attempts"] == 2


@pytest.mark.parametrize("restarted", ["initial", "escalation"])
@pytest.mark.parametrize("keep_escalation", [False, True])
def test_escalation_merge_keeps_restart_diagnostics(restarted, keep_escalation):
    initial = V.VerificationResult(verdict="UNVERIFIED")
    escalation = V.VerificationResult(verdict="CONFIRMED" if keep_escalation else "UNVERIFIED")
    (initial if restarted == "initial" else escalation).retry_telemetry = {
        "conversation_restarts": ["container expired"],
    }
    result = V._apply_escalation_outcome(
        initial_result=initial, esc_result=escalation, initial_verdict="UNVERIFIED",
        initial_model=MODEL_SONNET_55, initial_grounded=False, initial_sources=[],
        escalation_reason="initial_unverified",
    )
    assert result.retry_telemetry["conversation_restarts"] == ["container expired"]
