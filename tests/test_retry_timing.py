"""WP-11: respect rate-limit timing and release concurrency during backoff.

The contract (``src/verification/retry_policy.py``, "Retry timing"):

* a failed response's ``retry-after-ms`` / ``retry-after`` (seconds or an
  HTTP date) is a floor no retry goes before; a missing, malformed,
  negative, non-finite, zero, or expired value falls back to the local
  policy;
* local waits are exponential, capped, and jittered from an injected random
  source, so concurrent loops retry apart;
* both the attempt count and the elapsed retry budget stop a loop, and a
  server floor the budget cannot cover is never shortened;
* authentication, permission, not-found, invalid-request, and spend-cap
  failures are never retried;
* the clock, the wait, and the random source are injected, and a wait ends
  as soon as its cancel event is set;
* every app-owned loop runs with SDK retries off, and a concurrency permit
  is taken per outbound call — continuations and escalations included —
  never held while waiting, and never nested.

Nothing here waits in real time except the one cancellation test, which
proves a real ``threading.Event`` cuts a 30-second system wait short.
"""

from __future__ import annotations

import email.utils
import threading
import time
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import src.cross_check.cross_checker as cc
import src.verification.verifier as V
from src.batch import batch as batch_mod
from src.batch import batch_runtime as rt
from src.batch.batch import BatchStatus
from src.batch.batch_runtime import PollPolicy, ensure_batch_ended, poll_batch_bounded
from src.compliance import compliance_checker as comp
from src.core.code_cycles import DEFAULT_CYCLE
from src.core.project_profile import ProjectProfile
from src.drawing_impact import impact_synthesizer as impact
from src.input.extractor import ExtractedSpec
from src.modules import DEFAULT_MODULE, ResearchDimension
from src.orchestration import pipeline as pl
from src.research import requirements_research as rr
from src.research import run_requirements_research
from src.review import realtime_review as rtr
from src.review.reviewer import Finding
from src.review.structured_schemas import CROSS_CHECK_TOOL_NAME, DRAWING_IMPACT_TOOL_NAME
from src.verification import retry_policy as rp
from src.verification import triage
from src.verification.retry_policy import (
    DEFAULT_REALTIME_RETRY_POLICY,
    DEFAULT_VERIFICATION_RETRY_POLICY,
    STOP_ATTEMPTS_EXHAUSTED,
    STOP_CANCELLED,
    STOP_NOT_RETRYABLE,
    STOP_RETRY_BUDGET_SPENT,
    STOP_SERVER_DECLINED,
    STOP_SERVER_DELAY_OVER_BUDGET,
    FailureClass,
    RetryPolicy,
    RetrySchedule,
    classify_exception,
    is_refused_request_class,
    is_retryable_failure_class,
    parse_server_delay,
    server_delay_for,
)
from tests.fixtures.fake_anthropic import (
    FakeMessage,
    FakeToolUseBlock,
    compliance_tool_use_response,
    pause_turn_response,
    research_tool_use_response,
    review_tool_use_response,
)
from tests.fixtures.retry_timing import FAKE_EPOCH, FakeRetryTiming, install_fake_retry_timing
from tests.fixtures.verification_drivers import (
    medium_finding,
    message,
    search_blocks,
    verdict_call,
    verdict_payload,
)

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


# ---------------------------------------------------------------------------
# Error builders (real SDK exception types, real httpx responses)
# ---------------------------------------------------------------------------


def _response(status: int, headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(status, headers=headers or {}, request=_REQUEST)


def rate_limited(headers: dict | None = None, body=None) -> anthropic.RateLimitError:
    return anthropic.RateLimitError("rate limited", response=_response(429, headers), body=body)


def status_error(cls, status: int, headers: dict | None = None, body=None):
    return cls(f"HTTP {status}", response=_response(status, headers), body=body)


def midstream_error(error_type: str, **extra) -> anthropic.APIStatusError:
    """An ``error`` event inside a stream: the SDK raises it on the 200."""
    body = {"type": "error", "error": {"type": error_type, "message": "mid-stream", **extra}}
    return anthropic.APIStatusError("mid-stream", response=_response(200), body=body)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_REQUEST)


SPEND_CAP_BODY = {
    "type": "error",
    "error": {
        "type": "rate_limit_error",
        "message": "You have reached your API usage limits.",
        "details": {"error_code": "enforced_spend_limit_reached"},
    },
}


class ProbeGate:
    """A permit that refuses nested acquisition and records how it was used.

    ``held_by_current_thread`` lets a fake wait assert it holds no permit;
    acquiring it twice on one thread fails the test instead of deadlocking.
    """

    def __init__(self, permits: int = 1) -> None:
        self._semaphore = threading.BoundedSemaphore(permits)
        self._local = threading.local()
        self._lock = threading.Lock()
        self.acquisitions = 0
        self.active = 0
        self.max_active = 0
        self.nested = 0

    def __enter__(self):
        if getattr(self._local, "held", False):
            with self._lock:
                self.nested += 1
            raise AssertionError("the same permit was acquired twice on one thread")
        self._semaphore.acquire()
        self._local.held = True
        with self._lock:
            self.acquisitions += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        return self

    def __exit__(self, *_exc) -> bool:
        with self._lock:
            self.active -= 1
        self._local.held = False
        self._semaphore.release()
        return False

    def held_by_current_thread(self) -> bool:
        return bool(getattr(self._local, "held", False))


class _Stream:
    def __init__(self, outcome) -> None:
        self._outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False

    @property
    def text_stream(self):
        return iter(())

    def get_final_message(self):
        return self._outcome


class ScriptedClient:
    """``client.messages.stream`` / ``create`` answering from a script.

    ``route(kwargs)`` returns a message or an exception instance to raise.
    Each call records whether ``gate`` was held by the calling thread.
    """

    def __init__(self, route, *, gate: ProbeGate | None = None) -> None:
        self._route = route
        self._gate = gate
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self.held_at_call: list[bool] = []
        self.messages = SimpleNamespace(stream=self._stream, create=self._create)

    def _answer(self, kwargs):
        with self._lock:
            self.calls.append(kwargs)
            self.held_at_call.append(
                self._gate.held_by_current_thread() if self._gate is not None else False
            )
        outcome = self._route(kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def _stream(self, **kwargs):
        return _Stream(self._answer(kwargs))

    def _create(self, **kwargs):
        return self._answer(kwargs)


def _sequence(*outcomes):
    """A route that answers in order (one thread)."""
    remaining = list(outcomes)
    return lambda _kwargs: remaining.pop(0)


def _record_client_flavors(monkeypatch, module, client):
    """Patch ``module._get_client`` and record the flavor each call asked for."""
    flavors: list[bool] = []

    def factory(*, sdk_retries: bool = True):
        flavors.append(sdk_retries)
        return client

    monkeypatch.setattr(module, "_get_client", factory)
    return flavors


# ===========================================================================
# 1. Server delay headers
# ===========================================================================


class TestServerDelayHeaders:
    @pytest.mark.parametrize(
        ("headers", "seconds", "form"),
        [
            ({"retry-after": "12"}, 12.0, "seconds"),
            ({"retry-after": " 12 "}, 12.0, "seconds"),
            ({"retry-after": "12.5"}, 12.5, "seconds"),
            ({"retry-after-ms": "1500"}, 1.5, "milliseconds"),
            # The millisecond header is read first when it is valid.
            ({"retry-after-ms": "2500", "retry-after": "30"}, 2.5, "milliseconds"),
            # A malformed millisecond header falls back to retry-after.
            ({"retry-after-ms": "soon", "retry-after": "7"}, 7.0, "seconds"),
            ({"retry-after-ms": "-5", "retry-after": "7"}, 7.0, "seconds"),
            # Header names are case-insensitive, plain mappings included.
            ({"Retry-After": "9"}, 9.0, "seconds"),
        ],
    )
    def test_valid_values_are_floors(self, headers, seconds, form):
        delay = parse_server_delay(headers, now=FAKE_EPOCH)
        assert delay is not None
        assert delay.seconds == pytest.approx(seconds)
        assert delay.form == form

    def test_an_http_date_is_measured_against_the_injected_clock(self):
        when = email.utils.formatdate(FAKE_EPOCH + 90, usegmt=True)
        delay = parse_server_delay({"retry-after": when}, now=FAKE_EPOCH)
        assert delay is not None and delay.form == "http-date"
        assert delay.seconds == pytest.approx(90.0)

    @pytest.mark.parametrize(
        "value",
        [
            "-5",  # negative
            "inf",  # non-finite
            "nan",
            "1e3",  # exponent notation is not a delay value
            "9" * 400,  # overflows to infinity
            "abc",  # malformed
            "",
            "0",  # no wait: the local policy applies
            email.utils.formatdate(FAKE_EPOCH - 60, usegmt=True),  # expired
            email.utils.formatdate(FAKE_EPOCH, usegmt=True),  # not in the future
        ],
    )
    def test_invalid_values_are_no_floor(self, value):
        assert parse_server_delay({"retry-after": value}, now=FAKE_EPOCH) is None

    def test_a_valid_zero_millisecond_header_decides_on_its_own(self):
        # SDK parity: a readable retry-after-ms of 0 means "no wait"; the
        # other header is not consulted to find a longer one.
        headers = {"retry-after-ms": "0", "retry-after": "30"}
        assert parse_server_delay(headers, now=FAKE_EPOCH) is None

    def test_absent_headers_are_no_floor(self):
        assert parse_server_delay({}, now=FAKE_EPOCH) is None
        assert parse_server_delay(None, now=FAKE_EPOCH) is None

    def test_read_from_a_real_sdk_exception(self):
        delay = server_delay_for(rate_limited({"retry-after": "12"}), now=FAKE_EPOCH)
        assert delay is not None and delay.seconds == 12.0

    def test_nothing_is_invented_for_a_failure_without_a_response(self):
        assert server_delay_for(connection_error(), now=FAKE_EPOCH) is None
        assert server_delay_for(ValueError("unparseable payload"), now=FAKE_EPOCH) is None


# ===========================================================================
# 2. Classification: only transient classes are retried
# ===========================================================================


class TestExplicitClassification:
    @pytest.mark.parametrize(
        ("cls", "status"),
        [
            (anthropic.BadRequestError, 400),
            (anthropic.AuthenticationError, 401),
            (anthropic.PermissionDeniedError, 403),
            (anthropic.NotFoundError, 404),
            (anthropic.RequestTooLargeError, 413),
            (anthropic.UnprocessableEntityError, 422),
        ],
    )
    def test_refused_requests_are_never_retried(self, cls, status):
        exc = status_error(cls, status, headers={"retry-after": "5"})
        assert classify_exception(exc) is FailureClass.INVALID_REQUEST
        assert is_refused_request_class(FailureClass.INVALID_REQUEST)
        decision = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=FakeRetryTiming().timing()).decide(
            exc, attempt=0
        )
        assert decision.retry is False and decision.stop == STOP_NOT_RETRYABLE

    def test_the_spend_cap_429_is_not_a_rate_limit(self):
        exc = rate_limited(body=SPEND_CAP_BODY)
        assert classify_exception(exc) is FailureClass.SPEND_LIMIT
        assert not is_retryable_failure_class(FailureClass.SPEND_LIMIT)
        assert is_refused_request_class(FailureClass.SPEND_LIMIT)

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (status_error(anthropic.ConflictError, 409), FailureClass.SERVER_ERROR),
            (status_error(anthropic.APIStatusError, 408), FailureClass.CONNECTION),
        ],
    )
    def test_the_sdks_other_retryable_statuses_stay_retryable(self, exc, expected):
        # The SDK retries a 408 request timeout and a 409 lock timeout; an
        # app-owned loop runs with SDK retries off, so it must retry them too.
        assert classify_exception(exc) is expected
        decision = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=FakeRetryTiming().timing()).decide(
            exc, attempt=0
        )
        assert decision.retry is True

    def test_an_ordinary_429_is_retried(self):
        assert classify_exception(rate_limited()) is FailureClass.RATE_LIMIT
        assert is_retryable_failure_class(FailureClass.RATE_LIMIT)

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (status_error(anthropic.InternalServerError, 500), FailureClass.SERVER_ERROR),
            (status_error(anthropic.OverloadedError, 529), FailureClass.SERVER_ERROR),
            (connection_error(), FailureClass.CONNECTION),
            (anthropic.APITimeoutError(request=_REQUEST), FailureClass.CONNECTION),
        ],
    )
    def test_transient_classes_are_retried(self, exc, expected):
        assert classify_exception(exc) is expected
        assert is_retryable_failure_class(expected)

    @pytest.mark.parametrize(
        ("error_type", "expected"),
        [
            ("overloaded_error", FailureClass.SERVER_ERROR),
            ("api_error", FailureClass.SERVER_ERROR),
            ("rate_limit_error", FailureClass.RATE_LIMIT),
            ("invalid_request_error", FailureClass.INVALID_REQUEST),
            ("authentication_error", FailureClass.INVALID_REQUEST),
            ("something_new", FailureClass.UNKNOWN),
        ],
    )
    def test_a_mid_stream_error_is_classified_by_its_body(self, error_type, expected):
        assert classify_exception(midstream_error(error_type)) is expected

    def test_a_mid_stream_spend_cap_is_not_retried(self):
        exc = midstream_error(
            "rate_limit_error", details={"error_code": "enforced_spend_limit_reached"}
        )
        assert classify_exception(exc) is FailureClass.SPEND_LIMIT

    def test_the_api_can_decline_a_retry(self):
        exc = status_error(anthropic.InternalServerError, 500, headers={"x-should-retry": "false"})
        decision = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=FakeRetryTiming().timing()).decide(
            exc, attempt=0
        )
        assert decision.retry is False and decision.stop == STOP_SERVER_DECLINED
        assert "x-should-retry" in decision.note


# ===========================================================================
# 3. The schedule: floors, jitter, and both bounds
# ===========================================================================


def _schedule(policy=DEFAULT_REALTIME_RETRY_POLICY, *, randoms=None, **kwargs) -> RetrySchedule:
    return RetrySchedule(policy, timing=FakeRetryTiming(randoms=randoms).timing(), **kwargs)


class TestServerFloor:
    @pytest.mark.parametrize("draw", [0.0, 0.25, 0.5, 0.999])
    def test_no_retry_precedes_a_valid_floor(self, draw):
        decision = _schedule(randoms=[draw]).decide(rate_limited({"retry-after": "12"}), attempt=0)
        assert decision.retry is True
        assert decision.server_delay is not None and decision.server_delay.seconds == 12.0
        assert decision.delay_seconds >= 12.0
        # The spread is bounded: max(1 s, 10% of the floor).
        assert decision.delay_seconds <= 12.0 + max(1.0, 1.2)

    def test_the_floor_replaces_a_shorter_local_backoff(self):
        # Attempt 1 of a rate limit would wait 5-10 s locally; the server's
        # 45 s floor governs.
        decision = _schedule(randoms=[0.0]).decide(rate_limited({"retry-after": "45"}), attempt=1)
        assert decision.delay_seconds == pytest.approx(45.0)

    def test_workers_told_the_same_floor_retry_apart(self):
        timing = FakeRetryTiming(randoms=[0.1, 0.9])
        exc = rate_limited({"retry-after": "30"})
        first = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=timing.timing()).decide(exc, attempt=0)
        second = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=timing.timing()).decide(exc, attempt=0)
        assert first.delay_seconds != second.delay_seconds
        assert min(first.delay_seconds, second.delay_seconds) >= 30.0


class TestLocalBackoff:
    def test_waits_are_exponential_and_jittered_within_bounds(self):
        policy = DEFAULT_REALTIME_RETRY_POLICY
        for attempt, nominal in ((0, 5.0), (1, 10.0)):
            for draw in (0.0, 0.5, 0.999):
                decision = _schedule(randoms=[draw]).decide(connection_error(), attempt=attempt, failure_class=FailureClass.RATE_LIMIT)
                assert decision.retry is True and decision.server_delay is None
                assert nominal * (1 - policy.jitter_fraction) <= decision.delay_seconds <= nominal

    def test_a_draw_of_zero_reproduces_the_nominal_schedule(self):
        decision = _schedule(randoms=[0.0]).decide(connection_error(), attempt=0)
        assert decision.delay_seconds == pytest.approx(5.0)

    def test_the_cap_bounds_one_wait(self):
        policy = RetryPolicy(max_attempts=50, max_backoff_seconds=60.0, max_retry_wait_seconds=10_000)
        decision = _schedule(policy, randoms=[0.0]).decide(
            rate_limited(), attempt=40, failure_class=FailureClass.RATE_LIMIT
        )
        assert decision.delay_seconds == pytest.approx(60.0)

    def test_concurrent_loops_receive_different_waits(self):
        # Eight loops failing together, one shared (thread-safe) random
        # source: the nominal wait is identical, the drawn waits are not.
        timing = FakeRetryTiming(seed=11)
        delays: list[float] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def fail_once():
            schedule = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=timing.timing())
            barrier.wait(timeout=2)
            decision = schedule.decide(rate_limited(), attempt=0)
            with lock:
                delays.append(decision.delay_seconds)

        threads = [threading.Thread(target=fail_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert len(delays) == 8
        assert len(set(delays)) == 8
        nominal = rp.compute_backoff_seconds(
            DEFAULT_REALTIME_RETRY_POLICY, attempt=0, failure_class=FailureClass.RATE_LIMIT
        )
        assert all(nominal / 2 <= d <= nominal for d in delays)

    @pytest.mark.parametrize("draw", [float("nan"), 7.5, -3.0])
    def test_an_untrustworthy_random_source_is_clamped(self, draw):
        decision = _schedule(randoms=[draw]).decide(connection_error(), attempt=0)
        assert 2.5 <= decision.delay_seconds <= 5.0


class TestBothBounds:
    def test_the_attempt_limit_stops(self):
        schedule = _schedule(max_attempts=3)
        assert schedule.decide(connection_error(), attempt=1).retry is True
        decision = schedule.decide(connection_error(), attempt=2)
        assert decision.retry is False and decision.stop == STOP_ATTEMPTS_EXHAUSTED
        assert decision.note == ""  # the loop's own "failed after N" wording covers it

    def test_one_attempt_means_no_retry(self):
        decision = _schedule(max_attempts=1).decide(rate_limited({"retry-after": "1"}), attempt=0)
        assert decision.retry is False and decision.stop == STOP_ATTEMPTS_EXHAUSTED

    def test_a_floor_beyond_the_budget_stops_without_being_shortened(self):
        policy = RetryPolicy(max_retry_wait_seconds=30.0)
        decision = _schedule(policy).decide(rate_limited({"retry-after": "45"}), attempt=0)
        assert decision.retry is False
        assert decision.stop == STOP_SERVER_DELAY_OVER_BUDGET
        assert decision.delay_seconds == 0.0  # never a shortened retry
        assert "45s" in decision.note and "retry-after" in decision.note

    def test_the_elapsed_budget_counts_every_wait(self):
        policy = RetryPolicy(max_attempts=10, max_retry_wait_seconds=30.0)
        timing = FakeRetryTiming(randoms=[0.0] * 10)
        schedule = RetrySchedule(policy, timing=timing.timing())
        first = schedule.decide(rate_limited({"retry-after": "25"}), attempt=0)
        assert first.retry is True and schedule.wait(first) is True
        assert schedule.waited_seconds == pytest.approx(25.0)
        # 5 s left: a 10 s floor no longer fits.
        second = schedule.decide(rate_limited({"retry-after": "10"}), attempt=1)
        assert second.stop == STOP_SERVER_DELAY_OVER_BUDGET
        # ... and neither does a 5-10 s local backoff at attempt 1 (draw 0 → 10 s).
        third = schedule.decide(connection_error(), attempt=1, failure_class=FailureClass.RATE_LIMIT)
        assert third.stop == STOP_RETRY_BUDGET_SPENT
        assert timing.waits == [25.0]

    def test_a_floor_that_exactly_fits_is_honored_in_full(self):
        policy = RetryPolicy(max_retry_wait_seconds=12.0)
        decision = _schedule(policy, randoms=[0.999]).decide(rate_limited({"retry-after": "12"}), attempt=0)
        assert decision.retry is True and decision.delay_seconds == pytest.approx(12.0)

    def test_retry_count_defaults_keep_their_meaning(self):
        assert DEFAULT_REALTIME_RETRY_POLICY.max_attempts == 3
        assert DEFAULT_VERIFICATION_RETRY_POLICY.max_attempts == 3

    def test_a_broken_clock_still_honors_a_numeric_floor(self):
        def broken_now():
            raise RuntimeError("no clock")

        timing = rp.RetryTiming(wait=lambda s, c: True, now=broken_now, random=lambda: 0.0)
        schedule = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=timing)
        assert schedule.decide(rate_limited({"retry-after": "8"}), attempt=0).delay_seconds == 8.0
        when = email.utils.formatdate(FAKE_EPOCH + 90, usegmt=True)
        dated = schedule.decide(rate_limited({"retry-after": when}), attempt=0)
        assert dated.server_delay is None  # unreadable without a clock: local policy


# ===========================================================================
# 4. Injected time and cancellation
# ===========================================================================


class TestCancellation:
    def test_a_real_wait_ends_promptly_when_cancelled(self):
        event = threading.Event()
        threading.Timer(0.05, event.set).start()
        started = time.monotonic()
        completed = rp.SYSTEM_RETRY_TIMING.wait(30.0, event)
        assert completed is False
        assert time.monotonic() - started < 5.0

    def test_a_cancelled_schedule_neither_retries_nor_waits(self):
        event = threading.Event()
        event.set()
        timing = FakeRetryTiming()
        schedule = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=timing.timing(), cancel_event=event)
        decision = schedule.decide(rate_limited(), attempt=0)
        assert decision.retry is False and decision.stop == STOP_CANCELLED
        assert timing.waits == []

    def test_cancellation_during_a_wait_is_reported(self):
        event = threading.Event()
        timing = FakeRetryTiming(on_wait=lambda _s, cancel: cancel.set())
        schedule = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=timing.timing(), cancel_event=event)
        decision = schedule.decide(rate_limited(), attempt=0)
        assert decision.retry is True
        assert schedule.wait(decision) is False

    def test_the_default_timing_is_replaceable(self, monkeypatch):
        fake = install_fake_retry_timing(monkeypatch)
        assert RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY).timing.now() == FAKE_EPOCH
        assert fake.waits == []


# ===========================================================================
# 5. Batch polling: the poll loop is the one retry owner
# ===========================================================================


def _status(*, done: bool) -> BatchStatus:
    return BatchStatus(
        status="ended" if done else "in_progress",
        processing=0 if done else 2,
        succeeded=2 if done else 0,
        errored=0,
        canceled=0,
        expired=0,
        total=2,
    )


def _scripted_poll(monkeypatch, *outcomes):
    calls: list[dict] = []
    remaining = list(outcomes)

    def fake_poll(batch_id, **kwargs):
        calls.append(kwargs)
        outcome = remaining.pop(0) if remaining else _status(done=True)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(rt, "poll_batch", fake_poll)
    return calls


def _poll(**policy):
    return poll_batch_bounded(
        "msgbatch_x", policy=PollPolicy(**policy), log=lambda *a, **k: None, progress_cb=lambda _s: None
    )


class TestBatchPolling:
    def test_every_status_read_goes_out_with_sdk_retries_off(self, monkeypatch):
        install_fake_retry_timing(monkeypatch)
        calls = _scripted_poll(monkeypatch, _status(done=False), _status(done=True))
        assert _poll().terminal is True
        assert calls == [{"sdk_retries": False}, {"sdk_retries": False}]

    def test_a_rate_limited_read_waits_the_servers_floor(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        calls = _scripted_poll(monkeypatch, rate_limited({"retry-after": "40"}), _status(done=True))
        assert _poll().terminal is True
        assert len(calls) == 2
        assert len(timing.waits) == 1 and timing.waits[0] >= 40.0

    def test_a_failed_read_without_a_floor_backs_off_with_jitter(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch, randoms=[0.5])
        _scripted_poll(monkeypatch, connection_error(), _status(done=True))
        assert _poll(poll_interval_seconds=15).terminal is True
        # 15 x 2 = 30 s nominal, drawn from [15, 30].
        assert timing.waits == [pytest.approx(22.5)]

    @pytest.mark.parametrize(
        "exc",
        [
            status_error(anthropic.AuthenticationError, 401),
            status_error(anthropic.NotFoundError, 404),
            rate_limited(body=SPEND_CAP_BODY),
        ],
    )
    def test_a_refused_read_stops_at_once(self, monkeypatch, exc):
        timing = install_fake_retry_timing(monkeypatch)
        calls = _scripted_poll(monkeypatch, exc)
        outcome = _poll()
        assert outcome.poll_failed is True
        assert outcome.poll_error.startswith("poll_refused")
        assert len(calls) == 1 and timing.waits == []

    def test_a_floor_near_the_polling_bound_waits_no_longer_than_the_bound(self, monkeypatch):
        # Review finding: the bound was checked against the floor alone, so the
        # spread added to it (up to 10%) could wait ~6 minutes past it.
        monkeypatch.setattr(rt.time, "monotonic", lambda: 0.0)
        timing = install_fake_retry_timing(monkeypatch, randoms=[0.999])
        _scripted_poll(monkeypatch, rate_limited({"retry-after": "3599"}), _status(done=True))
        outcome = _poll(max_elapsed_seconds=3600)
        assert outcome.terminal is True
        assert len(timing.waits) == 1
        assert 3599.0 <= timing.waits[0] <= 3600.0  # the floor, never past the bound

    @pytest.mark.parametrize(
        "exc",
        [
            status_error(anthropic.ConflictError, 409),
            status_error(anthropic.APIStatusError, 408),
        ],
    )
    def test_a_lock_timeout_or_request_timeout_read_is_retried(self, monkeypatch, exc):
        # Review finding: the SDK retries 408 and 409; with its retries off,
        # the poll loop must too, not stop as if the request were refused.
        timing = install_fake_retry_timing(monkeypatch)
        calls = _scripted_poll(monkeypatch, exc, _status(done=True))
        assert _poll().terminal is True
        assert len(calls) == 2 and len(timing.waits) == 1

    def test_a_floor_past_the_polling_bound_detaches_without_waiting(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        _scripted_poll(monkeypatch, rate_limited({"retry-after": "7200"}))
        outcome = _poll(max_elapsed_seconds=3600)
        assert outcome.detached is True
        assert outcome.detach_reason.startswith("retry_after_exceeds_poll_bound")
        assert timing.waits == []

    def test_cancelling_during_the_poll_interval_returns_promptly(self, monkeypatch):
        event = threading.Event()
        install_fake_retry_timing(monkeypatch, on_wait=lambda _s, cancel: cancel.set())
        calls = _scripted_poll(monkeypatch, _status(done=False), _status(done=False))
        outcome = poll_batch_bounded(
            "msgbatch_x",
            policy=PollPolicy(),
            log=lambda *a, **k: None,
            progress_cb=lambda _s: None,
            cancel_event=event,
        )
        assert outcome.user_canceled is True
        assert len(calls) == 1

    def test_cancelling_during_an_error_backoff_returns_promptly(self, monkeypatch):
        event = threading.Event()
        install_fake_retry_timing(monkeypatch, on_wait=lambda _s, cancel: cancel.set())
        calls = _scripted_poll(monkeypatch, connection_error(), _status(done=True))
        outcome = poll_batch_bounded(
            "msgbatch_x",
            policy=PollPolicy(),
            log=lambda *a, **k: None,
            progress_cb=lambda _s: None,
            cancel_event=event,
        )
        assert outcome.user_canceled is True
        assert len(calls) == 1

    def test_the_recovery_pre_check_leaves_retries_to_the_poll_loop(self, monkeypatch):
        install_fake_retry_timing(monkeypatch)
        calls = _scripted_poll(monkeypatch, connection_error(), _status(done=True))
        assert ensure_batch_ended("msgbatch_x", policy=PollPolicy(), log=lambda *a, **k: None).status == "ended"
        assert calls == [{"sdk_retries": False}, {"sdk_retries": False}]


# ===========================================================================
# 6. One transient failure: exactly two API calls, SDK retries off
# ===========================================================================


class _Result:
    def __init__(self, custom_id: str) -> None:
        self.custom_id = custom_id


class TestOneTransientFailureIsOneRetry:
    def test_batch_results_download(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        calls: list[str] = []

        def results(batch_id):
            calls.append(batch_id)
            if len(calls) == 1:
                raise rate_limited({"retry-after": "3"})
            return iter([_Result("a")])

        client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(results=results)))
        flavors = _record_client_flavors(monkeypatch, batch_mod, client)
        assert set(batch_mod._collect_batch_results_with_retry("msgbatch_x")) == {"a"}
        assert len(calls) == 2 and flavors == [False]
        assert len(timing.waits) == 1 and timing.waits[0] >= 3.0

    def test_batch_results_download_stops_when_the_floor_is_beyond_the_budget(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        calls: list[str] = []

        def results(batch_id):
            calls.append(batch_id)
            raise rate_limited({"retry-after": "3600"})

        client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(results=results)))
        _record_client_flavors(monkeypatch, batch_mod, client)
        with pytest.raises(anthropic.RateLimitError):
            batch_mod._collect_batch_results_with_retry("msgbatch_x")
        assert len(calls) == 1 and timing.waits == []

    def test_realtime_review(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        monkeypatch.setattr(rtr, "review_extended_output_count", lambda spec: len(spec.spec_content) // 4)
        client = ScriptedClient(_sequence(rate_limited({"retry-after": "6"}), review_tool_use_response()))
        flavors = _record_client_flavors(monkeypatch, rtr, client)
        spec = ExtractedSpec(filename="a.docx", content="Provide piping per code.", word_count=4)
        results, _ = rtr.run_realtime_review([spec])
        assert results["review__a__0"].parse_status == "ok"
        assert len(client.calls) == 2 and flavors == [False]
        assert len(timing.waits) == 1 and timing.waits[0] >= 6.0

    def test_cross_check(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))
        ok = FakeMessage(
            content=[FakeToolUseBlock(name=CROSS_CHECK_TOOL_NAME, input={"findings": [], "coordination_summary": "ok"})],
            stop_reason="tool_use",
        )
        client = ScriptedClient(_sequence(rate_limited({"retry-after": "7"}), ok))
        client.messages.count_tokens = lambda **_k: (_ for _ in ()).throw(RuntimeError("count offline"))
        flavors = _record_client_flavors(monkeypatch, cc, client)
        specs = [
            ExtractedSpec(filename=f"21 13 1{i} x.docx", content="Provide sprinklers.", word_count=2)
            for i in range(2)
        ]
        result = cc.run_cross_check(specs, [], cycle=DEFAULT_CYCLE)
        assert result.cross_check_status == "completed"
        assert len(client.calls) == 2
        assert set(flavors) == {False}
        assert len(timing.waits) == 1 and timing.waits[0] >= 7.0

    def test_compliance(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        client = ScriptedClient(_sequence(status_error(anthropic.OverloadedError, 529), compliance_tool_use_response()))
        flavors = _record_client_flavors(monkeypatch, comp, client)
        result, _coverage = comp._stream_compliance(
            {"model": "claude-sonnet-5", "max_tokens": 10, "messages": []},
            model="claude-sonnet-5",
            max_retries=3,
            call_gate=None,
            trace_anchor=None,
        )
        assert result.cross_check_status == "completed"
        assert len(client.calls) == 2 and flavors == [False]
        assert len(timing.waits) == 1

    def test_realtime_verification(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        good = message(search_blocks() + [verdict_call(verdict_payload())])
        client = ScriptedClient(_sequence(rate_limited({"retry-after": "4"}), good))
        flavors = _record_client_flavors(monkeypatch, V, client)
        result = V.verify_finding(medium_finding(), max_retries=2, cycle=DEFAULT_CYCLE, cache=None)
        assert result.verdict == "CONFIRMED"
        assert len(client.calls) == 2 and flavors == [False]
        assert len(timing.waits) == 1 and timing.waits[0] >= 4.0

    def test_research(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        client = ScriptedClient(_sequence(connection_error(), research_tool_use_response()))
        profile = run_requirements_research(_research_module("alpha"), _project(), client=client)
        assert profile.completed_dimensions == 1
        assert len(client.calls) == 2
        assert len(timing.waits) == 1

    def test_research_builds_its_own_client_without_sdk_retries(self, monkeypatch):
        install_fake_retry_timing(monkeypatch)
        client = ScriptedClient(_sequence(research_tool_use_response()))
        flavors = _record_client_flavors(monkeypatch, rr, client)
        run_requirements_research(_research_module("alpha"), _project())
        assert flavors == [False]

    def test_drawing_impact(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        ok = FakeMessage(
            content=[
                FakeToolUseBlock(
                    name=DRAWING_IMPACT_TOOL_NAME,
                    input={"impact_level": "minimal", "narrative": "Little.", "finding_links": []},
                )
            ],
            stop_reason="tool_use",
        )
        client = ScriptedClient(_sequence(midstream_error("overloaded_error"), ok))
        flavors = _record_client_flavors(monkeypatch, impact, client)
        result = impact.run_drawing_impact(digest_text="DIGEST", findings=[])
        assert result.status == "completed"
        assert len(client.calls) == 2 and flavors == [False]
        assert len(timing.waits) == 1

    def test_triage(self, monkeypatch):
        timing = install_fake_retry_timing(monkeypatch)
        ok = FakeMessage(
            content=[
                FakeToolUseBlock(
                    name=triage.TRIAGE_TOOL_NAME,
                    input={"classifications": [{"index": 0, "classification": "web_required"}]},
                )
            ],
            stop_reason="tool_use",
        )
        gate = ProbeGate()
        client = ScriptedClient(_sequence(rate_limited({"retry-after": "2"}), ok), gate=gate)
        flavors = _record_client_flavors(monkeypatch, triage, client)
        timing.held_probe = gate.held_by_current_thread
        out = triage.classify_findings_with_haiku([_gripe()], api_call_semaphore=gate)
        assert out == {0: "web_required"}
        assert len(client.calls) == 2 and flavors == [False]
        assert client.held_at_call == [True, True]
        assert [w.held_permit for w in timing.wait_log] == [False]
        assert timing.waits[0] >= 2.0 and gate.acquisitions == 2 and gate.nested == 0


def _gripe() -> Finding:
    return Finding(
        severity="GRIPES",
        fileName="23 05 00 Common.docx",
        section="1.1",
        issue="Paragraph numbering skips 1.03.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        confidence=0.5,
        codeReference="",
    )


def _project() -> ProjectProfile:
    return ProjectProfile(city="Markham", state_or_province="ON", country="Canada", client_name="ExampleCo")


def _research_module(*dimension_ids: str):
    import dataclasses

    return dataclasses.replace(
        DEFAULT_MODULE,
        project_profile_enabled=True,
        research_persona="You are a test research assistant.",
        research_dimensions=tuple(
            ResearchDimension(
                dimension_id=d,
                title=d.title(),
                prompt_template=f"{d.upper()} research brief for {{city}}.",
            )
            for d in dimension_ids
        ),
        compliance_persona="You are a test compliance reviewer.",
        compliance_severity_definitions="- CRITICAL - permit-blocking omission.",
    )


# ===========================================================================
# 7. Retry-count settings keep their meaning, zero included
# ===========================================================================


class TestRetryCountSettings:
    @pytest.mark.parametrize(("max_retries", "calls"), [(0, 1), (1, 2), (2, 3)])
    def test_verification_counts_retries(self, monkeypatch, max_retries, calls):
        install_fake_retry_timing(monkeypatch)
        client = ScriptedClient(lambda _k: connection_error())
        _record_client_flavors(monkeypatch, V, client)
        result = V.verify_finding(medium_finding(), max_retries=max_retries, cycle=DEFAULT_CYCLE, cache=None)
        assert result.verification_failed is True
        assert len(client.calls) == calls

    @pytest.mark.parametrize(("max_retries", "calls"), [(0, 1), (1, 1), (2, 2), (3, 3)])
    def test_cross_check_counts_attempts(self, monkeypatch, max_retries, calls):
        install_fake_retry_timing(monkeypatch)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))
        client = ScriptedClient(lambda _k: connection_error())
        client.messages.count_tokens = lambda **_k: (_ for _ in ()).throw(RuntimeError("count offline"))
        _record_client_flavors(monkeypatch, cc, client)
        specs = [
            ExtractedSpec(filename=f"21 13 1{i} x.docx", content="Provide sprinklers.", word_count=2)
            for i in range(2)
        ]
        result = cc.run_cross_check(specs, [], cycle=DEFAULT_CYCLE, max_retries=max_retries)
        assert result.cross_check_status == "failed"
        assert len(client.calls) == calls
        assert f"Failed after {calls} attempts" in result.error

    @pytest.mark.parametrize(("max_retries", "calls"), [(0, 1), (1, 1), (3, 3)])
    def test_drawing_impact_counts_attempts(self, monkeypatch, max_retries, calls):
        install_fake_retry_timing(monkeypatch)
        client = ScriptedClient(lambda _k: connection_error())
        _record_client_flavors(monkeypatch, impact, client)
        result = impact.run_drawing_impact(digest_text="DIGEST", findings=[], max_retries=max_retries)
        assert result.status == "failed"
        assert len(client.calls) == calls

    @pytest.mark.parametrize(("max_retries", "calls"), [(0, 1), (1, 1), (3, 3)])
    def test_compliance_counts_attempts(self, monkeypatch, max_retries, calls):
        install_fake_retry_timing(monkeypatch)
        client = ScriptedClient(lambda _k: connection_error())
        _record_client_flavors(monkeypatch, comp, client)
        result, _ = comp._stream_compliance(
            {"model": "claude-sonnet-5", "max_tokens": 10, "messages": []},
            model="claude-sonnet-5",
            max_retries=max_retries,
            call_gate=None,
            trace_anchor=None,
        )
        assert result.cross_check_status == "failed"
        assert len(client.calls) == calls


# ===========================================================================
# 8. Permits: per outbound call, released before waiting, never nested
# ===========================================================================


def _marker_route(script: dict[str, list]):
    """Route by a marker in the request text; each marker keeps its own list."""
    remaining = {marker: list(items) for marker, items in script.items()}
    lock = threading.Lock()

    def route(kwargs):
        text = repr(kwargs.get("messages"))
        with lock:
            for marker, items in remaining.items():
                if marker in text:
                    return items.pop(0)
        raise AssertionError(f"no route for request: {text[:120]!r}")

    return route


class TestPermitsAreReleasedWhileWaiting:
    def test_research_another_dimension_calls_while_the_first_backs_off(self, monkeypatch):
        gate = ProbeGate(permits=1)
        beta_called = threading.Event()
        timing = install_fake_retry_timing(monkeypatch, held_probe=gate.held_by_current_thread)
        # Alpha's wait lasts until beta has made its call: with the permit
        # held across the wait, beta could never call and this would time out.
        seen_during_wait: list[bool] = []
        timing.on_wait = lambda _s, _c: seen_during_wait.append(beta_called.wait(timeout=2))
        route = _marker_route(
            {
                "ALPHA": [rate_limited({"retry-after": "3"}), research_tool_use_response()],
                "BETA": [research_tool_use_response()],
            }
        )

        def routed(kwargs):
            outcome = route(kwargs)
            if "BETA" in repr(kwargs.get("messages")):
                beta_called.set()
            return outcome

        client = ScriptedClient(routed, gate=gate)
        profile = run_requirements_research(
            _research_module("alpha", "beta"), _project(), client=client, call_semaphore=gate
        )
        assert profile.completed_dimensions == 2
        assert seen_during_wait == [True]  # beta called during alpha's backoff
        assert len(client.calls) == 3
        assert client.held_at_call == [True, True, True]  # every call under its permit
        assert [w.held_permit for w in timing.wait_log] == [False]
        assert timing.waits[0] >= 3.0
        assert gate.acquisitions == 3 and gate.max_active == 1 and gate.nested == 0

    def test_research_continuations_take_their_own_permit(self, monkeypatch):
        install_fake_retry_timing(monkeypatch)
        gate = ProbeGate(permits=1)
        client = ScriptedClient(
            _sequence(pause_turn_response(), research_tool_use_response()), gate=gate
        )
        profile = run_requirements_research(
            _research_module("alpha"), _project(), client=client, call_semaphore=gate
        )
        assert profile.completed_dimensions == 1
        assert len(client.calls) == 2
        assert client.held_at_call == [True, True]
        assert gate.acquisitions == 2 and gate.active == 0 and gate.nested == 0

    def test_realtime_review_another_spec_streams_while_the_first_backs_off(self, monkeypatch):
        b_streamed = threading.Event()
        timing = install_fake_retry_timing(monkeypatch)
        seen_during_wait: list[bool] = []
        timing.on_wait = lambda _s, _c: seen_during_wait.append(b_streamed.wait(timeout=2))
        monkeypatch.setattr(rtr, "review_extended_output_count", lambda spec: len(spec.spec_content) // 4)
        route = _marker_route(
            {
                "AAA-SPEC": [rate_limited({"retry-after": "5"}), review_tool_use_response()],
                "BBB-SPEC": [review_tool_use_response()],
            }
        )

        def routed(kwargs):
            outcome = route(kwargs)
            if "BBB-SPEC" in repr(kwargs.get("messages")):
                b_streamed.set()
            return outcome

        client = ScriptedClient(routed)
        _record_client_flavors(monkeypatch, rtr, client)
        specs = [
            ExtractedSpec(filename="a.docx", content="AAA-SPEC Provide piping.", word_count=3),
            ExtractedSpec(filename="b.docx", content="BBB-SPEC Provide valves.", word_count=3),
        ]
        results, _ = rtr.run_realtime_review(specs, max_workers=1)
        assert all(r.parse_status == "ok" for r in results.values())
        assert seen_during_wait == [True]  # b streamed during a's backoff
        assert len(client.calls) == 3

    def test_realtime_verification_another_finding_calls_while_the_first_backs_off(self, monkeypatch):
        gate = ProbeGate(permits=1)
        second_called = threading.Event()
        timing = install_fake_retry_timing(monkeypatch, held_probe=gate.held_by_current_thread)
        seen_during_wait: list[bool] = []
        timing.on_wait = lambda _s, _c: seen_during_wait.append(second_called.wait(timeout=2))
        good = message(search_blocks() + [verdict_call(verdict_payload())])
        route = _marker_route(
            {
                "FIRST-CLAIM": [rate_limited({"retry-after": "4"}), good],
                "SECOND-CLAIM": [good],
            }
        )

        def routed(kwargs):
            outcome = route(kwargs)
            if "SECOND-CLAIM" in repr(kwargs.get("messages")):
                second_called.set()
            return outcome

        client = ScriptedClient(routed, gate=gate)
        _record_client_flavors(monkeypatch, V, client)
        monkeypatch.setattr(pl, "prepare_findings_for_verification", lambda findings, **_k: list(findings))
        findings = [
            medium_finding(issue="FIRST-CLAIM spacing exceeds the listed maximum."),
            medium_finding(issue="SECOND-CLAIM hanger spacing exceeds the table."),
        ]
        pl.verify_findings_for_run(findings, transport="realtime", api_call_semaphore=gate)
        assert [f.verification.verdict for f in findings] == ["CONFIRMED", "CONFIRMED"]
        assert seen_during_wait == [True]  # the second called during the first's backoff
        assert client.held_at_call == [True, True, True]
        assert [w.held_permit for w in timing.wait_log] == [False]
        assert gate.acquisitions == 3 and gate.max_active == 1 and gate.nested == 0

    def test_verification_continuations_and_the_escalation_each_take_a_permit(self, monkeypatch):
        install_fake_retry_timing(monkeypatch)
        gate = ProbeGate(permits=1)
        unresolved = message(
            search_blocks() + [verdict_call(verdict_payload("UNVERIFIED", sources=[], source_quote=None))]
        )
        good = message(search_blocks() + [verdict_call(verdict_payload())])
        pause = message(search_blocks(), stop_reason="pause_turn")
        # Initial pass: a pause, then an UNVERIFIED that escalates a HIGH
        # finding; the escalation answers in one call.
        client = ScriptedClient(_sequence(pause, unresolved, good), gate=gate)
        _record_client_flavors(monkeypatch, V, client)
        result = V.verify_finding(
            medium_finding(severity="HIGH"), max_retries=0, cycle=DEFAULT_CYCLE, cache=None, call_gate=gate
        )
        assert result.escalation_attempted is True
        assert len(client.calls) == 3
        assert client.held_at_call == [True, True, True]
        assert gate.acquisitions == 3 and gate.active == 0 and gate.nested == 0

    def test_cross_check_releases_its_permit_before_waiting(self, monkeypatch):
        gate = ProbeGate(permits=1)
        timing = install_fake_retry_timing(monkeypatch, held_probe=gate.held_by_current_thread)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))
        ok = FakeMessage(
            content=[FakeToolUseBlock(name=CROSS_CHECK_TOOL_NAME, input={"findings": [], "coordination_summary": "ok"})],
            stop_reason="tool_use",
        )
        client = ScriptedClient(_sequence(rate_limited({"retry-after": "3"}), ok), gate=gate)
        client.messages.count_tokens = lambda **_k: (_ for _ in ()).throw(RuntimeError("count offline"))
        _record_client_flavors(monkeypatch, cc, client)
        specs = [
            ExtractedSpec(filename=f"21 13 1{i} x.docx", content="Provide sprinklers.", word_count=2)
            for i in range(2)
        ]
        result = cc.run_cross_check(specs, [], cycle=DEFAULT_CYCLE, call_gate=gate)
        assert result.cross_check_status == "completed"
        assert client.held_at_call == [True, True]
        assert [w.held_permit for w in timing.wait_log] == [False]
        assert gate.nested == 0
