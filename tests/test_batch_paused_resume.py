"""Paid batch conversations survive pauses, submit reminders, and transport handoff."""
from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

import src.verification.verifier as V
from src.core import api_config
from src.core.code_cycles import DEFAULT_CYCLE
from src.core.pricing import estimate_cost_breakdown
from src.orchestration.diagnostics import DiagnosticsReport, record_verification_findings
from src.verification.verification_routing import build_verification_request, select_routing
from tests.fixtures import verification_drivers as vd
from tests.fixtures.fake_anthropic import (
    FakeContainer, FakeTextBlock, FakeThinkingBlock, batch_errored_result, batch_verification_result,
)
from tests.fixtures.preserved_thinking import preserved_thinking_violations
from tests.fixtures.retry_timing import install_fake_retry_timing
from tests.test_conversation_retries import _error
from tests.test_batch_escalation import INIT_MODEL, _candidate_finding
from tests.test_resend_sanitizer import _fetch_block, _pdf_b64, _pdf_sources_in


def _pause(*, searches=0, signature="pause", blocks=None):
    msg = vd.message(
        [FakeThinkingBlock(thinking="", signature=signature), *(blocks or [])],
        stop_reason="pause_turn", searches=searches,
    )
    msg.container = FakeContainer(id="paused-container")
    return msg


def _verdict():
    return vd.message([vd.verdict_call(vd.verdict_payload())], searches=0)


def _note(*, searches=0):
    return vd.message(
        [*(vd.search_blocks() if searches else []), FakeTextBlock(text="Submitting next.")],
        stop_reason="end_turn", searches=searches,
    )


def _install_batch(monkeypatch, turns, *, fail_followup=False, detach_followup=False):
    turns = iter(turns)
    bodies = []
    exchanges = []
    pending = {}

    def submit(requests, request_map, **_kwargs):
        if fail_followup and bodies:
            raise RuntimeError("follow-up submission failed")
        for request in requests:
            body = copy.deepcopy(request["params"])
            bodies.append(body)
            pending[request["custom_id"]] = body
        return SimpleNamespace(batch_id=f"batch-{len(bodies)}", request_map=request_map)

    def retrieve(job):
        results = {}
        for cid in job.request_map:
            msg = next(turns)
            if msg == "errored":
                exchanges.append((pending[cid], None))
                results[cid] = batch_errored_result(custom_id=cid)
            else:
                exchanges.append((pending[cid], msg.content))
                results[cid] = batch_verification_result(custom_id=cid, message=msg)
        return results

    monkeypatch.setattr(V, "submit_verification_followup_wave", submit)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", retrieve)
    monkeypatch.setattr(V, "poll_batch_bounded", lambda *a, **k: SimpleNamespace(
        detached=detach_followup and len(bodies) > 1, poll_failed=False,
    ))
    return submit, bodies, exchanges


def _collect(monkeypatch, batch_turns, realtime_turns, *, max_waves=1, cap=None, decision=None):
    target = vd.medium_finding()
    decision = decision or select_routing(target, cycle=DEFAULT_CYCLE, local_skip=False)
    if cap is not None:
        decision = replace(decision, max_continuations=cap)
    submit, bodies, exchanges = _install_batch(monkeypatch, batch_turns)
    request = build_verification_request(
        decision,
        prompt=V._build_verification_prompt(target, cycle=DEFAULT_CYCLE),
        system_prompt=V._get_verification_system_prompt(DEFAULT_CYCLE),
        include_service_tier=False,
    )
    job = submit(
        [{"custom_id": "verify__0", "params": request.params}],
        {"verify__0": {"finding_idx": 0, "model": decision.model, "routing": decision.to_dict()}},
    )
    responses = iter(realtime_turns)
    stream_bodies = []

    def stream(kwargs):
        body = copy.deepcopy(kwargs)
        stream_bodies.append(body)
        response = next(responses)
        exchanges.append((body, None if isinstance(response, Exception) else response.content))
        return response

    client = vd.ScriptedStreamClient(stream)
    monkeypatch.setattr(V, "_get_client", lambda **_: client)
    V.collect_verification_batch_results(
        job, [target], max_waves=max_waves, realtime_fallback_threshold=5,
    )
    return target, bodies, stream_bodies, exchanges


@pytest.mark.parametrize("reminded", [False, True])
def test_paused_escalation_continues_and_keeps_its_verdict(monkeypatch, reminded):
    target = _candidate_finding()
    pause = _pause(searches=2, blocks=vd.search_blocks())
    turns = [pause, *([_note()] if reminded else []), _verdict()]
    _, bodies, exchanges = _install_batch(monkeypatch, turns)
    V._run_batch_escalation_wave(
        [target], cycle=DEFAULT_CYCLE, cache=None,
        policy=V.DEFAULT_VERIFICATION_POLL_POLICY,
        log=lambda *a, **k: None, progress=lambda *a: None,
    )
    result = target.verification
    assert result.verdict == "CONFIRMED" and result.grounded
    assert result.escalated and result.initial_model == INIT_MODEL
    assert len(bodies) == len(turns)
    assert all(body["model"] == bodies[0]["model"] for body in bodies)
    assert bodies[1]["container"] == "paused-container"
    assert result.verdict_reminder_sent == reminded
    assert preserved_thinking_violations(exchanges) == []
    assert len(result.call_usage) == 2
    assert result.call_usage[1]["input_tokens"] == len(turns) * vd.INPUT_TOKENS
    assert result.call_usage[1]["role"] == "escalation"


def test_escalation_honors_all_four_deep_continuations(monkeypatch):
    target = _candidate_finding()
    pauses = [_pause(searches=2 if i == 0 else 0, signature=f"pause-{i}",
                     blocks=vd.search_blocks() if i == 0 else []) for i in range(4)]
    _, bodies, exchanges = _install_batch(monkeypatch, [*pauses, _verdict()])
    V._run_batch_escalation_wave(
        [target], cycle=DEFAULT_CYCLE, cache=None,
        policy=V.DEFAULT_VERIFICATION_POLL_POLICY,
        log=lambda *a, **k: None, progress=lambda *a: None,
    )
    assert len(bodies) == 5
    assert target.verification.verdict == "CONFIRMED"
    assert preserved_thinking_violations(exchanges) == []


def test_last_wave_resumes_same_model_container_and_grounding_and_prices_once(monkeypatch):
    pauses = [_pause(searches=2 if i == 0 else 0, signature=f"p-{i}",
                     blocks=vd.search_blocks() if i == 0 else []) for i in range(2)]
    target, batch, live, exchanges = _collect(monkeypatch, pauses, [_verdict()], max_waves=2)
    result = target.verification
    assert result.verdict == "CONFIRMED" and result.grounded
    assert len(live) == 1
    assert live[0]["model"] == batch[0]["model"]
    assert live[0]["container"] == "paused-container"
    assert [m["role"] for m in live[0]["messages"]] == ["user", "assistant"]
    assert preserved_thinking_violations(exchanges) == []
    assert [(a["transport"], a["role"]) for a in result.call_usage] == [
        ("batch", "primary"), ("realtime", "fallback"),
    ]
    assert result.call_usage[0]["input_tokens"] == 2 * vd.INPUT_TOKENS
    assert result.call_usage[1]["input_tokens"] == vd.INPUT_TOKENS
    assert result.input_tokens == 3 * vd.INPUT_TOKENS
    diag = DiagnosticsReport()
    record_verification_findings(diag, [target], phase="verification", transport="batch")
    summary = diag.summary()["cost_summary"]
    expected = sum(estimate_cost_breakdown(
        (2 if is_batch else 1) * vd.INPUT_TOKENS,
        (2 if is_batch else 1) * vd.OUTPUT_TOKENS,
        model=result.model_used, batch=is_batch,
        cache_creation_input_tokens=(2 if is_batch else 1) * vd.CACHE_WRITE_TOKENS,
        cache_read_input_tokens=(2 if is_batch else 1) * vd.CACHE_READ_TOKENS,
        web_search_requests=2 if is_batch else 0,
    ).total for is_batch in [True, False])
    assert summary["duplicate_attempts_ignored"] == 0
    assert summary["estimated_cost_usd"]["total"] == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("batch_pauses", [1, 3, 5])
def test_continuation_cap_is_shared_across_batch_and_realtime(monkeypatch, batch_pauses):
    pauses = [_pause(signature=f"p-{i}") for i in range(5)]
    target, batch, live, _ = _collect(
        monkeypatch, pauses[:batch_pauses], pauses[batch_pauses:],
        max_waves=batch_pauses, cap=4,
    )
    assert len(batch) + len(live) == 5
    assert target.verification.outcome == V.OUTCOME_CONTINUATION_CAP
    assert target.verification.retry_telemetry["continuation_count"] == 5
    assert target.verification.verification_failed is False
    assert sum(a["input_tokens"] for a in target.verification.call_usage) == 5 * vd.INPUT_TOKENS


def test_last_wave_submit_reminder_uses_existing_conversation(monkeypatch):
    target, _, live, exchanges = _collect(monkeypatch, [_note(searches=2)], [_verdict()])
    assert target.verification.verdict == "CONFIRMED"
    assert target.verification.verdict_reminder_sent
    assert [m["role"] for m in live[0]["messages"]] == ["user", "assistant", "user"]
    assert live[0]["messages"][-1]["content"] == V.VERDICT_REMINDER_TOOL
    assert preserved_thinking_violations(exchanges) == []


def test_reminder_already_sent_in_batch_is_not_repeated_in_realtime(monkeypatch):
    target, _, live, exchanges = _collect(
        monkeypatch, [_note(searches=2), _pause()], [_note()], max_waves=2,
    )
    assert len(live) == 1
    assert target.verification.outcome == V.OUTCOME_MALFORMED_VERDICT
    assert target.verification.verdict_reminder_sent
    assert [m["role"] for m in live[0]["messages"]] == ["user", "assistant", "user", "assistant"]
    assert preserved_thinking_violations(exchanges) == []


def test_resume_sanitizes_pdf_and_keeps_original_blocks(monkeypatch):
    pause = _pause(searches=2, blocks=[*vd.search_blocks(), _fetch_block(_pdf_b64(601)),
                                     FakeThinkingBlock(signature="after-pdf")])
    original = copy.deepcopy(pause.content)
    target, _, live, _ = _collect(monkeypatch, [pause], [_verdict()])
    assert target.verification.verdict == "CONFIRMED"
    assert _pdf_sources_in(live[0]["messages"]) == []
    assert pause.content == original
    assert not any(b.get("signature") == "after-pdf"
                   for b in live[0]["messages"][-1]["content"])


@pytest.mark.parametrize("detached", [False, True])
def test_interrupted_escalation_preserves_paid_pause_once(monkeypatch, detached):
    target = _candidate_finding()
    _, bodies, _ = _install_batch(
        monkeypatch, [_pause(searches=2, blocks=vd.search_blocks())],
        fail_followup=not detached, detach_followup=detached,
    )
    V._run_batch_escalation_wave(
        [target], cycle=DEFAULT_CYCLE, cache=None,
        policy=V.DEFAULT_VERIFICATION_POLL_POLICY,
        log=lambda *a, **k: None, progress=lambda *a: None,
    )
    assert target.verification.verdict == "UNVERIFIED"
    attempts = target.verification.call_usage[1:]
    assert sum(a["input_tokens"] for a in attempts) == vd.INPUT_TOKENS
    assert sum(not a["usage_known"] for a in attempts) == int(detached)


@pytest.mark.parametrize("invalid", [False, True])
def test_resumed_stream_retries_history_without_restarting(monkeypatch, invalid):
    install_fake_retry_timing(monkeypatch)
    target, _, live, exchanges = _collect(
        monkeypatch, [_pause(searches=2, blocks=vd.search_blocks())],
        [_error(400 if invalid else 429), _verdict()],
    )
    result = target.verification
    if invalid:
        assert len(live) == 1
        assert result.outcome == V.OUTCOME_TRANSPORT_ERROR
        assert not result.retry_telemetry.get("conversation_restarts")
    else:
        assert len(live) == 2
        assert live[0] == live[1]
        assert result.verdict == "CONFIRMED"
    assert result.call_usage[0]["transport"] == "batch"
    assert result.call_usage[0]["input_tokens"] == vd.INPUT_TOKENS
    assert result.call_usage[1]["usage_known"] is False
    assert len(result.call_usage) == (2 if invalid else 3)
    assert preserved_thinking_violations(exchanges) == []


def test_resumed_pause_can_get_one_streaming_submit_reminder(monkeypatch):
    target, _, live, exchanges = _collect(
        monkeypatch, [_pause(searches=2, blocks=vd.search_blocks())], [_note(), _verdict()],
    )
    assert target.verification.verdict == "CONFIRMED"
    assert target.verification.verdict_reminder_sent
    assert len(live) == 2
    assert live[1]["messages"][:2] == live[0]["messages"]
    assert live[1]["messages"][-1]["content"] == V.VERDICT_REMINDER_TOOL
    assert preserved_thinking_violations(exchanges) == []


def test_escalation_stops_at_deep_cap_without_discarding_spend(monkeypatch):
    target = _candidate_finding()
    _, bodies, _ = _install_batch(monkeypatch, [_pause(signature=f"p-{i}") for i in range(5)])
    V._run_batch_escalation_wave(
        [target], cycle=DEFAULT_CYCLE, cache=None,
        policy=V.DEFAULT_VERIFICATION_POLL_POLICY,
        log=lambda *a, **k: None, progress=lambda *a: None,
    )
    assert len(bodies) == 5
    assert target.verification.verdict == "UNVERIFIED"
    assert len(target.verification.call_usage) == 2
    assert target.verification.call_usage[1]["input_tokens"] == 5 * vd.INPUT_TOKENS



def test_resume_carries_abandoned_batch_attempts_without_repricing_them(monkeypatch):
    first = _pause(searches=2, signature="abandoned", blocks=vd.search_blocks())
    current = _pause(searches=2, signature="current", blocks=vd.search_blocks())
    target, _, live, exchanges = _collect(
        monkeypatch, [first, "errored", current], [_verdict()], max_waves=3,
    )
    result = target.verification
    assert result.verdict == "CONFIRMED"
    assert len(live) == 1
    assert [(a["transport"], a["role"]) for a in result.call_usage] == [
        ("batch", "primary"), ("batch", "retry"), ("realtime", "fallback"),
    ]
    assert [a["batch_id"] for a in result.call_usage[:2]] == ["batch-1", "batch-3"]
    assert sum(a["input_tokens"] for a in result.call_usage) == 3 * vd.INPUT_TOKENS
    assert result.input_tokens == 2 * vd.INPUT_TOKENS  # Only the current conversation's evidence.
    assert preserved_thinking_violations(exchanges) == []


def test_errored_last_wave_still_resumes_the_previous_paid_pause(monkeypatch):
    target, _, live, exchanges = _collect(
        monkeypatch, [_pause(searches=2, blocks=vd.search_blocks()), "errored"],
        [_verdict()], max_waves=2,
    )
    assert target.verification.verdict == "CONFIRMED"
    assert len(live) == 1
    assert live[0]["container"] == "paused-container"
    assert [m["role"] for m in live[0]["messages"]] == ["user", "assistant"]
    assert preserved_thinking_violations(exchanges) == []
    assert len(target.verification.call_usage) == 2
    assert sum(a["input_tokens"] for a in target.verification.call_usage) == 2 * vd.INPUT_TOKENS


@pytest.mark.parametrize("transport", ["batch", "realtime"])
@pytest.mark.parametrize("reminded", [False, True])
@pytest.mark.parametrize("initial_phase", [api_config.PHASE_VERIFICATION, api_config.PHASE_VERIFICATION_RETRY])
def test_resume_uses_continuation_policy_and_retains_stored_routing(
    monkeypatch, transport, reminded, initial_phase,
):
    # Equal production budgets hid the incorrect phase. A different resume
    # budget must apply without re-selecting any of the paused turn's routing.
    monkeypatch.setitem(api_config._PHASE_OUTPUT_BUDGET, initial_phase, 18_000)
    monkeypatch.setitem(api_config._PHASE_OUTPUT_BUDGET, api_config.PHASE_VERIFICATION_CONTINUATION, 9_000)
    stored = replace(
        select_routing(vd.medium_finding(), local_skip=False, cycle=DEFAULT_CYCLE),
        cache_phase=initial_phase, max_continuations=4, web_search_max_uses=7,
        trace_reason="stored_pause_routing",
    )
    routed = []
    build = V.build_verification_request

    def capture_build(decision, **kwargs):
        routed.append(decision.to_dict())
        return build(decision, **kwargs)

    monkeypatch.setattr(V, "build_verification_request", capture_build)
    first = _note(searches=2) if reminded else _pause(searches=2, blocks=vd.search_blocks())
    target, batch, live, exchanges = _collect(
        monkeypatch, [first, _verdict()] if transport == "batch" else [first],
        [_verdict()] if transport == "realtime" else [],
        max_waves=2 if transport == "batch" else 1, decision=stored,
    )
    resumed = batch[1] if transport == "batch" else live[0]
    assert batch[0]["max_tokens"] == 18_000
    assert resumed["max_tokens"] == 9_000
    assert routed == [replace(stored, cache_phase=api_config.PHASE_VERIFICATION_CONTINUATION).to_dict()]
    assert stored.cache_phase == initial_phase
    assert target.verification.verdict == "CONFIRMED"
    assert preserved_thinking_violations(exchanges) == []
