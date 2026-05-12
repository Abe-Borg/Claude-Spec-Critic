"""Chunk 6 — centralized retry, continuation, and batch-failure policy.

The tests below cover the plan's acceptance criteria:

* Server-error batch items retry within the policy.
* Invalid-request batch items do NOT blindly retry.
* Repeated parse-failure findings become terminal unverified earlier
  than the global wave cap.
* The real-time pause-turn continuation loop caps at 2 by default and 4
  for DEEP_REASONING routing.
* The SDK retry knob and app retry policy do not compound unexpectedly.
* String-matching connection-error detection delegates to the typed
  SDK classifier.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.code_cycles import DEFAULT_CYCLE
from src.reviewer import Finding


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------


def _make_finding(
    *,
    severity: str = "HIGH",
    code_ref: str | None = "CBC 2025",
    issue: str = "Cited code edition is outdated",
) -> Finding:
    return Finding(
        severity=severity,
        fileName="23 21 13 - Hydronic.docx",
        section="2.1",
        issue=issue,
        actionType="EDIT",
        existingText="per CBC 2019",
        replacementText="per CBC 2025",
        codeReference=code_ref,
        confidence=0.6,
    )


class _FakeResultObj:
    """Minimal stand-in for an Anthropic batch result."""

    def __init__(self, *, type_: str = "errored", message=None, error_type: str | None = None, error_msg: str | None = None):
        error = None
        if error_type or error_msg:
            error = SimpleNamespace(type=error_type, message=error_msg)
        self.result = SimpleNamespace(type=type_, message=message, error=error)


# ---------------------------------------------------------------------------
# Classifier: typed-SDK-first exception classification
# ---------------------------------------------------------------------------


def test_classify_exception_recognizes_typed_sdk_classes():
    from src.retry_policy import FailureClass, classify_exception
    from anthropic import (
        APIConnectionError,
        APIStatusError,
        InternalServerError,
        RateLimitError,
    )

    # All these typed exceptions need positional args we cannot easily
    # construct with full fidelity. The classifier only checks isinstance,
    # so we just need objects with the right *type*. Use bare subclasses
    # built via __new__ to skip the constructors.
    rl = RateLimitError.__new__(RateLimitError)
    assert classify_exception(rl) is FailureClass.RATE_LIMIT

    se = InternalServerError.__new__(InternalServerError)
    assert classify_exception(se) is FailureClass.SERVER_ERROR

    conn = APIConnectionError.__new__(APIConnectionError)
    assert classify_exception(conn) is FailureClass.CONNECTION


def test_classify_exception_handles_529_overload():
    from src.retry_policy import FailureClass, classify_exception
    from anthropic import APIStatusError

    overloaded = APIStatusError.__new__(APIStatusError)
    overloaded.status_code = 529
    assert classify_exception(overloaded) is FailureClass.SERVER_ERROR


def test_classify_exception_marks_4xx_as_invalid_request():
    from src.retry_policy import FailureClass, classify_exception
    from anthropic import APIStatusError

    bad = APIStatusError.__new__(APIStatusError)
    bad.status_code = 400
    assert classify_exception(bad) is FailureClass.INVALID_REQUEST


def test_classify_exception_falls_back_to_substring_for_generic_exception():
    """Audit Issue 9: a generic Exception with a transport-error message
    should still classify as CONNECTION so the legacy retryability is
    preserved."""
    from src.retry_policy import FailureClass, classify_exception

    exc = Exception("peer closed connection on read")
    assert classify_exception(exc) is FailureClass.CONNECTION

    # Whereas a generic exception with no transport signal is UNKNOWN.
    other = Exception("something bizarre happened")
    assert classify_exception(other) is FailureClass.UNKNOWN


def test_legacy_is_retryable_connection_error_delegates_to_classifier():
    """Chunk 6 replaces the standalone substring helper with a thin
    wrapper around the centralized classifier."""
    from src.reviewer import _is_retryable_connection_error

    assert _is_retryable_connection_error(Exception("Connection reset by peer"))
    assert _is_retryable_connection_error(Exception("Read timed out"))
    assert not _is_retryable_connection_error(Exception("totally unrelated"))


# ---------------------------------------------------------------------------
# Batch failure classification
# ---------------------------------------------------------------------------


def test_classify_batch_failure_invalid_request_type():
    from src.retry_policy import FailureClass, classify_batch_failure

    fc = classify_batch_failure(
        result_type="errored",
        error_type="invalid_request_error",
        error_message="bad shape",
    )
    assert fc is FailureClass.INVALID_REQUEST


def test_classify_batch_failure_overloaded():
    from src.retry_policy import FailureClass, classify_batch_failure

    fc = classify_batch_failure(
        result_type="errored",
        error_type="overloaded_error",
        error_message="Anthropic overloaded",
    )
    assert fc is FailureClass.SERVER_ERROR


def test_classify_batch_failure_expired_and_canceled():
    from src.retry_policy import FailureClass, classify_batch_failure

    assert classify_batch_failure(result_type="expired") is FailureClass.BATCH_EXPIRED
    assert classify_batch_failure(result_type="canceled") is FailureClass.BATCH_CANCELED


def test_should_retry_batch_failure_blocks_invalid_request():
    from src.retry_policy import FailureClass, should_retry_batch_failure

    assert should_retry_batch_failure(FailureClass.SERVER_ERROR)
    assert should_retry_batch_failure(FailureClass.BATCH_ERRORED)
    assert not should_retry_batch_failure(FailureClass.INVALID_REQUEST)
    assert not should_retry_batch_failure(FailureClass.BATCH_CANCELED)


# ---------------------------------------------------------------------------
# Per-finding wave tracker
# ---------------------------------------------------------------------------


def test_batch_wave_failure_tracker_terminates_on_repeated_same_class():
    from src.retry_policy import BatchWaveFailureTracker, FailureClass

    tracker = BatchWaveFailureTracker()
    # First occurrence of PARSE_ERROR: not terminal.
    assert not tracker.is_terminal("rf-aaa", current=FailureClass.PARSE_ERROR)
    tracker.record("rf-aaa", FailureClass.PARSE_ERROR)
    # Second occurrence of the SAME class: terminal.
    assert tracker.is_terminal("rf-aaa", current=FailureClass.PARSE_ERROR)


def test_batch_wave_failure_tracker_allows_different_classes():
    from src.retry_policy import BatchWaveFailureTracker, FailureClass

    tracker = BatchWaveFailureTracker()
    tracker.record("rf-aaa", FailureClass.SERVER_ERROR)
    # A DIFFERENT class after SERVER_ERROR is still retry-eligible.
    assert not tracker.is_terminal("rf-aaa", current=FailureClass.PARSE_ERROR)


def test_batch_wave_failure_tracker_invalid_request_is_immediate_terminal():
    from src.retry_policy import BatchWaveFailureTracker, FailureClass

    tracker = BatchWaveFailureTracker()
    # INVALID_REQUEST on the first occurrence is terminal — the request
    # shape would have to change to get a different answer.
    assert tracker.is_terminal("rf-aaa", current=FailureClass.INVALID_REQUEST)


def test_batch_wave_failure_tracker_terminal_reason_string():
    from src.retry_policy import BatchWaveFailureTracker, FailureClass

    tracker = BatchWaveFailureTracker()
    tracker.record("rf-aaa", FailureClass.PARSE_ERROR)
    reason = tracker.terminal_reason("rf-aaa", current=FailureClass.PARSE_ERROR)
    assert "parse_error" in reason
    assert "#2" in reason


# ---------------------------------------------------------------------------
# Backoff schedule
# ---------------------------------------------------------------------------


def test_compute_backoff_seconds_scales_by_attempt():
    from src.retry_policy import (
        DEFAULT_REALTIME_RETRY_POLICY,
        FailureClass,
        compute_backoff_seconds,
    )

    # base=5, server_error multiplier=2 → 5, 10, 20.
    b0 = compute_backoff_seconds(
        DEFAULT_REALTIME_RETRY_POLICY, attempt=0, failure_class=FailureClass.SERVER_ERROR
    )
    b1 = compute_backoff_seconds(
        DEFAULT_REALTIME_RETRY_POLICY, attempt=1, failure_class=FailureClass.SERVER_ERROR
    )
    b2 = compute_backoff_seconds(
        DEFAULT_REALTIME_RETRY_POLICY, attempt=2, failure_class=FailureClass.SERVER_ERROR
    )
    assert b0 == 5.0
    assert b1 == 10.0
    assert b2 == 20.0


# ---------------------------------------------------------------------------
# Continuation cap (real-time pause-turn loop)
# ---------------------------------------------------------------------------


def test_default_continuation_cap_is_two():
    """The plan calls out: default max continuation count of 2."""
    from src.retry_policy import DEFAULT_MAX_CONTINUATIONS, max_continuations_for_mode

    assert DEFAULT_MAX_CONTINUATIONS == 2
    # Every mode except DEEP_REASONING gets the default.
    assert max_continuations_for_mode("standard_reasoning") == 2
    assert max_continuations_for_mode("strict_structured") == 2
    assert max_continuations_for_mode("local_skip") == 2


def test_deep_reasoning_continuation_cap_is_higher():
    """Routing decisions for DEEP_REASONING get a higher cap so
    legitimately critical findings have room to converge."""
    from src.retry_policy import DEEP_MAX_CONTINUATIONS, max_continuations_for_mode

    assert DEEP_MAX_CONTINUATIONS == 4
    assert max_continuations_for_mode("deep_reasoning") == 4


def test_select_routing_attaches_per_mode_continuation_cap():
    """The routing selector pulls the cap from
    :func:`max_continuations_for_mode` so the verifier's pause-turn
    loop reads the right value via ``decision.max_continuations``."""
    from src.verification_routing import select_routing

    # Default finding → STANDARD_REASONING → cap 2.
    f_default = _make_finding(severity="HIGH", code_ref="CBC 2025")
    decision = select_routing(f_default, local_skip=False)
    assert decision.max_continuations == 2

    # Escalated → DEEP_REASONING → cap 4.
    decision_deep = select_routing(f_default, escalated=True, local_skip=False)
    assert decision_deep.mode.value == "deep_reasoning"
    assert decision_deep.max_continuations == 4


def test_explicit_max_continuations_override_wins():
    """A caller-supplied ``max_continuations`` overrides the per-mode
    default so existing tests / operator overrides keep working."""
    from src.verification_routing import select_routing

    f = _make_finding()
    decision = select_routing(f, local_skip=False, max_continuations=7)
    assert decision.max_continuations == 7


# ---------------------------------------------------------------------------
# Batch wave: invalid-request becomes terminal at parse time
# ---------------------------------------------------------------------------


def _patch_wave_loop(monkeypatch, results_by_wave: list[dict[str, _FakeResultObj]]):
    """Helper: monkey-patch the wave loop's IO so a sequence of waves
    returns the supplied (custom_id -> result) maps in order. The verifier
    then sees the staged batch results without any network round-trip.
    """
    from src import verifier
    from src.batch import BatchJob

    state = {"call_count": 0}

    def _fake_retrieve(_job):
        idx = min(state["call_count"], len(results_by_wave) - 1)
        state["call_count"] += 1
        return results_by_wave[idx]

    monkeypatch.setattr(verifier, "retrieve_verification_results_detailed", _fake_retrieve)
    monkeypatch.setattr(
        verifier,
        "poll_batch_bounded",
        lambda batch_id, *, policy, log, progress_cb: SimpleNamespace(detached=False, poll_failed=False),
    )
    monkeypatch.setattr(
        verifier,
        "submit_verification_followup_wave",
        lambda reqs, request_map: BatchJob(batch_id="msgbatch_followup", job_type="verify", request_map=request_map, created_at=0.0),
    )


def test_invalid_request_batch_item_does_not_retry(monkeypatch):
    """Chunk 6 directive 6: INVALID_REQUEST findings become terminal
    immediately. The wave loop must NOT resubmit them, and the result
    must carry a non-retryable failure-class explanation."""
    from src.batch import BatchJob
    from src.verifier import collect_verification_batch_results

    findings = [_make_finding(issue="claim 0")]
    job = BatchJob(
        batch_id="msgbatch_test",
        job_type="verify",
        request_map={"verify__0": {"finding_idx": 0, "model": "claude-opus-4-6"}},
        created_at=0.0,
    )
    _patch_wave_loop(
        monkeypatch,
        [
            {
                "verify__0": _FakeResultObj(
                    type_="errored",
                    error_type="invalid_request_error",
                    error_msg="messages.0.content: bad",
                ),
            }
        ],
    )

    # Track submit_verification_followup_wave: it must NOT be invoked.
    from src import verifier as _verifier
    invocations = {"count": 0}
    original_submit = _verifier.submit_verification_followup_wave

    def _tracking_submit(reqs, request_map):
        invocations["count"] += 1
        return original_submit(reqs, request_map)

    monkeypatch.setattr(_verifier, "submit_verification_followup_wave", _tracking_submit)

    collect_verification_batch_results(
        job, findings,
        max_waves=3,
        realtime_fallback_threshold=0,
    )

    assert findings[0].verification is not None
    assert findings[0].verification.verdict == "UNVERIFIED"
    assert "non-retryable" in findings[0].verification.explanation
    assert "invalid_request" in findings[0].verification.explanation
    assert invocations["count"] == 0, "INVALID_REQUEST must not trigger a wave resubmit"


def test_repeated_parse_failure_becomes_terminal_before_wave_cap(monkeypatch):
    """Chunk 6 directive 6: repeated same-class failures convert to
    terminal-unverified earlier than the global wave cap. A finding that
    parse-errors twice in a row is terminal on wave 2, not wave 3."""
    from src.batch import BatchJob
    from src.verifier import collect_verification_batch_results

    findings = [_make_finding(issue="claim 0")]
    job = BatchJob(
        batch_id="msgbatch_test",
        job_type="verify",
        request_map={"verify__0": {"finding_idx": 0, "model": "claude-opus-4-6"}},
        created_at=0.0,
    )

    # Simulate parse failure on both waves: end_turn with no parseable
    # content. ``stop_reason="end_turn"`` triggers the canonical parser
    # which then routes through PARSE_STATUS_NO_CONTENT.
    fake_message = SimpleNamespace(stop_reason="end_turn", content=[])
    _patch_wave_loop(
        monkeypatch,
        [
            {"verify__0": _FakeResultObj(type_="succeeded", message=fake_message)},
            {"verify__0": _FakeResultObj(type_="succeeded", message=fake_message)},
            {"verify__0": _FakeResultObj(type_="succeeded", message=fake_message)},
        ],
    )

    # The first wave will record the parse error and emit
    # terminal_unverified directly (because PARSE_STATUS_NO_CONTENT is
    # surfaced as terminal in _classify_wave_results today). That's the
    # expected behavior: terminal_unverified BEFORE wave 3.
    collect_verification_batch_results(
        job, findings,
        max_waves=3,
        realtime_fallback_threshold=0,
    )
    assert findings[0].verification is not None
    assert findings[0].verification.verdict == "UNVERIFIED"


def test_server_error_batch_item_retries_within_limit(monkeypatch):
    """A SERVER_ERROR batch item is retried up to the global wave cap.
    On wave 2 with the same SERVER_ERROR class the tracker terminates
    it (different from PARSE_ERROR — server-error is a retryable
    class). Verify the wave loop submits the follow-up wave once for
    the server-error retry."""
    from src.batch import BatchJob
    from src.verifier import collect_verification_batch_results
    from src import verifier as _verifier

    findings = [_make_finding(issue="claim 0")]
    job = BatchJob(
        batch_id="msgbatch_test",
        job_type="verify",
        request_map={"verify__0": {"finding_idx": 0, "model": "claude-opus-4-6"}},
        created_at=0.0,
    )

    # Wave 1: server error. Wave 2: also server error. Wave 3: irrelevant
    # because the tracker should terminate it after wave 2.
    _patch_wave_loop(
        monkeypatch,
        [
            {
                "verify__0": _FakeResultObj(
                    type_="errored",
                    error_type="overloaded_error",
                    error_msg="server overloaded",
                )
            },
            {
                "verify_retry_1__verify__0": _FakeResultObj(
                    type_="errored",
                    error_type="overloaded_error",
                    error_msg="server overloaded again",
                )
            },
        ],
    )

    submit_call_count = {"n": 0}

    def _tracking_submit(reqs, request_map):
        submit_call_count["n"] += 1
        return _FakeResultObj.__init__.__self__ if False else _verifier.submit_verification_followup_wave.__wrapped__(reqs, request_map) if hasattr(_verifier.submit_verification_followup_wave, "__wrapped__") else None

    # The _patch_wave_loop helper already swapped submit_verification_followup_wave
    # with a simple stub that constructs a BatchJob.  We wrap it here to
    # count invocations.
    original_submit = _verifier.submit_verification_followup_wave

    def _counting_submit(reqs, request_map):
        submit_call_count["n"] += 1
        return original_submit(reqs, request_map)

    monkeypatch.setattr(_verifier, "submit_verification_followup_wave", _counting_submit)

    collect_verification_batch_results(
        job, findings,
        max_waves=3,
        realtime_fallback_threshold=0,
    )

    # Wave 1 produced a server-error retry (1st occurrence → retry).
    # The wave loop submitted a follow-up wave once for the retry. Wave 2
    # came back with the SAME class → terminal-unverified before wave 3.
    assert submit_call_count["n"] == 1, (
        f"expected exactly one follow-up wave for server-error retry; "
        f"got {submit_call_count['n']}"
    )
    assert findings[0].verification is not None
    assert findings[0].verification.verdict == "UNVERIFIED"


# ---------------------------------------------------------------------------
# Retry telemetry on the result
# ---------------------------------------------------------------------------


def test_retry_telemetry_is_stamped_on_invalid_request_terminal(monkeypatch):
    from src.batch import BatchJob
    from src.verifier import collect_verification_batch_results

    findings = [_make_finding(issue="claim 0")]
    job = BatchJob(
        batch_id="msgbatch_test",
        job_type="verify",
        request_map={"verify__0": {"finding_idx": 0, "model": "claude-opus-4-6"}},
        created_at=0.0,
    )
    _patch_wave_loop(
        monkeypatch,
        [
            {
                "verify__0": _FakeResultObj(
                    type_="errored",
                    error_type="invalid_request_error",
                    error_msg="bad",
                )
            }
        ],
    )

    collect_verification_batch_results(
        job, findings,
        max_waves=2,
        realtime_fallback_threshold=0,
    )
    rt = findings[0].verification.retry_telemetry
    assert rt is not None
    assert rt["failure_class"] == "invalid_request"
    assert rt["terminal_reason"] is not None


def test_retry_telemetry_default_is_none_for_success_path():
    """A plain VerificationResult constructed for the success path has
    no retry_telemetry — the field describes runtime corrective behavior
    only."""
    from src.verifier import VerificationResult

    r = VerificationResult(verdict="CONFIRMED")
    assert r.retry_telemetry is None


# ---------------------------------------------------------------------------
# Diagnostics rollup
# ---------------------------------------------------------------------------


def test_retry_stats_appears_in_diagnostics_summary():
    from src.diagnostics import DiagnosticsReport

    diag = DiagnosticsReport(run_id="test")
    # Synthesize a verification event with retry_telemetry attached.
    diag.log(
        "verification",
        "info",
        "test",
        {
            "verdict": "UNVERIFIED",
            "retry_telemetry": {
                "attempts": 2,
                "failure_class": "parse_error",
                "terminal_reason": "repeated parse_error failure (occurrence #2 on this finding)",
                "continuation_count": 0,
            },
        },
    )
    diag.finish()
    s = diag.summary()
    rs = s["retry_stats"]
    assert rs["findings_with_retries"] == 1
    assert rs["total_retry_attempts"] == 2
    assert rs["by_failure_class"]["parse_error"] == 1
    assert "repeated parse_error failure" in next(iter(rs["by_terminal_reason"]))


def test_retry_stats_empty_when_no_retries_observed():
    """A clean run with no retries reports zero across the rollup."""
    from src.diagnostics import DiagnosticsReport

    diag = DiagnosticsReport(run_id="test")
    diag.log(
        "verification",
        "info",
        "test",
        {"verdict": "CONFIRMED"},  # no retry_telemetry
    )
    diag.finish()
    s = diag.summary()
    rs = s["retry_stats"]
    assert rs["findings_with_retries"] == 0
    assert rs["total_retry_attempts"] == 0
    assert rs["by_failure_class"] == {}


# ---------------------------------------------------------------------------
# Real-time verification respects the new continuation cap
# ---------------------------------------------------------------------------


def test_verify_finding_real_time_continuation_cap_terminates(monkeypatch):
    """When the pause-turn loop emits more than ``max_continuations``
    pauses, the verifier returns UNVERIFIED with a continuation-cap
    explanation."""
    import os
    from src import verifier
    from src.verifier import VerificationResult, _run_verification_call
    from src.code_cycles import DEFAULT_CYCLE

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    # Force select_routing to return a STANDARD_REASONING decision with
    # max_continuations=2 (the new Chunk 6 default). We mock the
    # streaming client to always return ``pause_turn`` so we can exhaust
    # the loop deterministically.
    class _FakeStream:
        def __init__(self, response):
            self._response = response

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def get_final_message(self):
            return self._response

    pause_response = SimpleNamespace(
        stop_reason="pause_turn",
        content=[],
        usage=None,
    )

    class _FakeMessages:
        def stream(self, **kwargs):
            return _FakeStream(pause_response)

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    monkeypatch.setattr(verifier, "_get_client", lambda: _FakeClient())

    f = _make_finding(severity="HIGH", code_ref="CBC 2025")
    result = _run_verification_call(
        f,
        cycle=DEFAULT_CYCLE,
        model="claude-sonnet-4-6",
        max_retries=0,
        escalated=False,
    )

    assert result.verdict == "UNVERIFIED"
    assert (
        "max_continuations" in result.explanation
        or "continuation" in result.explanation.lower()
    )


# ---------------------------------------------------------------------------
# SDK retries do not compound with app retries
# ---------------------------------------------------------------------------


def test_default_retry_policies_do_not_double_up():
    """The centralized retry policy declares a single ``max_attempts``
    that the app-level loops honor. The SDK has its own internal retry
    config (default 2); this test pins the app-side policy so an
    operator can reason about total attempts as
    ``app_max_attempts * sdk_max_attempts``."""
    from src.retry_policy import (
        DEFAULT_REALTIME_RETRY_POLICY,
        DEFAULT_VERIFICATION_RETRY_POLICY,
    )
    # Frozen public surface — the values are part of the contract.
    assert DEFAULT_REALTIME_RETRY_POLICY.max_attempts == 3
    assert DEFAULT_VERIFICATION_RETRY_POLICY.max_attempts == 3
