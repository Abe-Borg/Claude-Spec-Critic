"""Resource-pressure telemetry: was the run starved of capacity?

The diagnostics report counted retries, truncations, and exhausted search
budgets, but never recorded how long a run stood still or why. The
``src/core/resource_pressure.py`` ledger records four kinds of waiting —
retry waits after rate limits / overload / connection failures (and the calls
given up on after them), the app's own permit contention, shared-verification
follower waits, and batch queue delays — plus every HTTP response's status
and the API's reported rate-limit headroom. These tests pin:

* **Observation only.** Recording never changes a wait, a decision, or a
  permit; with no recorder installed, every record call is a no-op.
* **Attribution.** Each retry loop reports under its own label; each permit
  pool under its own name; a stop that *could* have retried a capacity
  failure is a call given up on, a refused request is not.
* **One wording.** ``resource_pressure_lines`` is what the text export, the
  GUI window, and the recovery CLI print — a verdict, then the evidence with
  its denominators.
* **The headroom hook.** The observer attaches to the SDK's HTTP client once,
  is shared by ``with_options`` views, reads headers case-insensitively, and
  never raises.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from src.batch import batch_runtime as rt
from src.batch.batch import BatchStatus
from src.batch.batch_runtime import PollPolicy, poll_batch_bounded
from src.core import resource_pressure as rp
from src.orchestration import pipeline as pl
from src.orchestration.diagnostics import DiagnosticsReport, resource_pressure_lines
from src.review import reviewer
from src.verification.retry_policy import (
    DEFAULT_REALTIME_RETRY_POLICY,
    STOP_ATTEMPTS_EXHAUSTED,
    FailureClass,
    RetryDecision,
    RetrySchedule,
    ServerDelay,
)
from src.verification.verification_cache import VerificationCache
from tests.fixtures.retry_timing import FakeRetryTiming, install_fake_retry_timing


_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _response(status: int, headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(status, headers=headers or {}, request=_REQUEST)


def _rate_limited(headers: dict | None = None) -> anthropic.RateLimitError:
    return anthropic.RateLimitError("rate limited", response=_response(429, headers), body=None)


def _schedule(**kwargs) -> RetrySchedule:
    return RetrySchedule(
        DEFAULT_REALTIME_RETRY_POLICY, timing=FakeRetryTiming().timing(), **kwargs
    )


@pytest.fixture(autouse=True)
def _no_installed_recorder():
    """Every test starts and ends with no process-wide recorder."""
    rp.install(None)
    yield
    rp.install(None)


# ---------------------------------------------------------------------------
# The active recorder
# ---------------------------------------------------------------------------


class TestActiveRecorder:
    def test_without_a_recorder_every_record_is_a_noop(self):
        assert rp.current() is None
        rp.record_retry_wait(label="x", seconds=1.0, failure_class="rate_limit")
        rp.record_retry_abandoned(label="x", failure_class="rate_limit", stop="attempts_exhausted")
        rp.record_permit_wait(pool="x", seconds=0.5)
        rp.record_singleflight_wait(seconds=1.0, timed_out=True)
        rp.record_batch_poll(seconds=1.0, terminal_status="ended", expired=1)
        rp.observe_http_response(_response(429))
        assert rp.current() is None

    def test_install_and_uninstall_are_identity_checked(self):
        first = rp.PressureRecorder()
        second = rp.PressureRecorder()
        rp.install(first)
        assert rp.current() is first
        assert rp.uninstall(second) is False
        assert rp.current() is first
        assert rp.uninstall(first) is True
        assert rp.current() is None
        assert rp.uninstall(None) is False

    def test_a_bound_recorder_wins_over_the_installed_one(self):
        installed = rp.PressureRecorder()
        bound = rp.PressureRecorder()
        rp.install(installed)
        with rp.recording(bound):
            assert rp.current() is bound
            rp.record_permit_wait(pool="p", seconds=0.0)
        assert rp.current() is installed
        assert bound.summary()["permit_waits"]["acquisitions"] == 1
        assert installed.summary()["permit_waits"]["acquisitions"] == 0

    def test_install_rejects_other_objects(self):
        with pytest.raises(TypeError):
            rp.install(object())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            with rp.recording(object()):  # type: ignore[arg-type]
                pass

    def test_a_failing_event_sink_never_reaches_the_caller(self):
        def boom(*_args):
            raise RuntimeError("sink down")

        recorder = rp.PressureRecorder(on_event=boom)
        recorder.record_retry_wait(label="review", seconds=2.0, failure_class="rate_limit")
        recorder.record_batch_poll(seconds=10.0, terminal_status="ended", expired=3)
        assert recorder.summary()["retry_waits"]["count"] == 1
        assert recorder.summary()["batch_polls"]["expired_requests"] == 3


# ---------------------------------------------------------------------------
# Retry loops report their waits and the calls they give up on
# ---------------------------------------------------------------------------


class TestRetrySchedule:
    def test_a_wait_is_recorded_under_the_loops_label_and_class(self):
        recorder = rp.PressureRecorder()
        schedule = _schedule(label="review")
        with rp.recording(recorder):
            decision = schedule.decide(None, attempt=0, failure_class=FailureClass.RATE_LIMIT)
            assert decision.retry is True
            assert schedule.wait(decision) is True
        waits = recorder.summary()["retry_waits"]
        assert waits["count"] == 1
        assert waits["total_seconds"] == pytest.approx(decision.delay_seconds, abs=0.001)
        assert waits["max_seconds"] == pytest.approx(decision.delay_seconds, abs=0.001)
        assert waits["server_floor_count"] == 0
        assert waits["by_failure_class"] == {
            "rate_limit": {"count": 1, "total_seconds": pytest.approx(decision.delay_seconds, abs=0.001)}
        }
        assert list(waits["by_label"]) == ["review"]
        assert rp.SIGNAL_PROVIDER_THROTTLING in recorder.summary()["signals"]
        # The schedule's own accounting is what it always was.
        assert schedule.waits == [decision.delay_seconds]

    def test_recording_does_not_change_the_wait(self):
        timing_without = FakeRetryTiming(randoms=[0.3])
        timing_with = FakeRetryTiming(randoms=[0.3])
        bare = RetrySchedule(DEFAULT_REALTIME_RETRY_POLICY, timing=timing_without.timing())
        bare.wait(bare.decide(None, attempt=0, failure_class=FailureClass.SERVER_ERROR))
        recorded = RetrySchedule(
            DEFAULT_REALTIME_RETRY_POLICY, timing=timing_with.timing(), label="compliance"
        )
        with rp.recording(rp.PressureRecorder()):
            recorded.wait(recorded.decide(None, attempt=0, failure_class=FailureClass.SERVER_ERROR))
        assert timing_without.waits == timing_with.waits

    def test_the_servers_floor_is_marked(self):
        recorder = rp.PressureRecorder()
        schedule = _schedule(label="verification")
        with rp.recording(recorder):
            decision = schedule.decide(_rate_limited({"retry-after": "12"}), attempt=0)
            assert decision.server_delay is not None
            schedule.wait(decision)
        waits = recorder.summary()["retry_waits"]
        assert waits["server_floor_count"] == 1
        assert waits["max_seconds"] >= 12.0

    def test_running_out_of_attempts_on_a_capacity_class_is_a_call_given_up(self):
        recorder = rp.PressureRecorder()
        schedule = _schedule(max_attempts=1, label="cross_check")
        with rp.recording(recorder):
            decision = schedule.decide(None, attempt=0, failure_class=FailureClass.RATE_LIMIT)
        assert decision.retry is False and decision.stop == STOP_ATTEMPTS_EXHAUSTED
        abandoned = recorder.summary()["retries_abandoned"]
        assert abandoned["count"] == 1
        assert abandoned["by_failure_class"] == {"rate_limit": 1}
        assert abandoned["by_stop"] == {STOP_ATTEMPTS_EXHAUSTED: 1}
        assert abandoned["by_label"] == {"cross_check": 1}
        assert rp.SIGNAL_PROVIDER_THROTTLING in recorder.summary()["signals"]

    def test_a_connection_failure_given_up_is_its_own_signal(self):
        recorder = rp.PressureRecorder()
        schedule = _schedule(max_attempts=1, label="research")
        with rp.recording(recorder):
            schedule.decide(None, attempt=0, failure_class=FailureClass.CONNECTION)
        summary = recorder.summary()
        assert summary["signals"] == [rp.SIGNAL_CONNECTION_ERRORS]

    def test_a_refused_request_is_not_starvation(self):
        recorder = rp.PressureRecorder()
        schedule = _schedule(max_attempts=1, label="review")
        with rp.recording(recorder):
            for cls in (FailureClass.INVALID_REQUEST, FailureClass.SPEND_LIMIT, FailureClass.UNKNOWN):
                decision = schedule.decide(None, attempt=0, failure_class=cls)
                assert decision.retry is False
            # Cross-check's one re-request for an unreadable answer is a
            # retry override on a non-capacity class: not starvation either.
            parse = schedule.decide(
                None, attempt=0, failure_class=FailureClass.PARSE_ERROR, retryable=True
            )
            assert parse.retry is False
        summary = recorder.summary()
        assert summary["retries_abandoned"]["count"] == 0
        assert summary["observed"] is False

    def test_a_cancelled_loop_is_not_starvation(self):
        recorder = rp.PressureRecorder()
        cancel = threading.Event()
        cancel.set()
        schedule = RetrySchedule(
            DEFAULT_REALTIME_RETRY_POLICY,
            timing=FakeRetryTiming().timing(),
            cancel_event=cancel,
            label="review",
        )
        with rp.recording(recorder):
            decision = schedule.decide(None, attempt=0, failure_class=FailureClass.RATE_LIMIT)
            assert decision.retry is False
            assert schedule.wait(decision) is False
        assert recorder.summary()["retries_abandoned"]["count"] == 0
        assert recorder.summary()["retry_waits"]["count"] == 0

    def test_a_wait_cut_short_by_cancellation_is_counted_as_cancelled(self):
        recorder = rp.PressureRecorder()
        cancel = threading.Event()
        timing = FakeRetryTiming(on_wait=lambda _s, _e: cancel.set())
        schedule = RetrySchedule(
            DEFAULT_REALTIME_RETRY_POLICY, timing=timing.timing(), cancel_event=cancel, label="x"
        )
        with rp.recording(recorder):
            decision = schedule.decide(None, attempt=0, failure_class=FailureClass.RATE_LIMIT)
            assert schedule.wait(decision) is False
        waits = recorder.summary()["retry_waits"]
        assert waits["count"] == 1 and waits["cancelled"] == 1

    def test_every_app_loop_labels_its_schedule(self):
        """Each retry loop names itself, so a stall is attributable."""
        import inspect

        from src.batch import batch as batch_mod
        from src.compliance import compliance_checker
        from src.coordination import adjudication
        from src.cross_check import cross_checker
        from src.drawing_impact import impact_synthesizer
        from src.input import drawing_digest
        from src.research import requirements_research
        from src.review import realtime_review
        from src.verification import triage, verifier

        expected = {
            batch_mod: "batch_results",
            compliance_checker: "compliance",
            adjudication: "coordination",
            cross_checker: "cross_check",
            impact_synthesizer: "drawing_impact",
            drawing_digest: "drawing_digest",
            requirements_research: "research",
            realtime_review: "review",
            triage: "triage",
            verifier: "verification",
        }
        for module, label in expected.items():
            source = inspect.getsource(module)
            assert f'label="{label}"' in source, module.__name__
            assert "RetrySchedule(" in source, module.__name__


# ---------------------------------------------------------------------------
# Permit pools measure their own contention
# ---------------------------------------------------------------------------


class TestMeteredSemaphore:
    def test_contention_is_measured_per_pool(self):
        recorder = rp.PressureRecorder()
        gate = rp.MeteredSemaphore(1, pool="verification")
        released = threading.Event()

        def hold():
            with rp.recording(recorder):
                with gate:
                    released.wait(2.0)

        holder = threading.Thread(target=hold)
        with rp.recording(recorder):
            holder.start()
            # Give the holder the permit first.
            deadline = time.monotonic() + 2.0
            while recorder.summary()["permit_waits"]["acquisitions"] < 1:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            timer = threading.Timer(0.15, released.set)
            timer.start()
            started = time.monotonic()
            with gate:
                blocked = time.monotonic() - started
        holder.join(2.0)
        assert blocked >= 0.1
        permits = recorder.summary()["permit_waits"]
        assert permits["acquisitions"] == 2
        assert permits["waited"] == 1
        assert permits["max_seconds"] >= 0.1
        pool = permits["by_pool"]["verification"]
        assert pool["acquisitions"] == 2 and pool["waited"] == 1
        assert pool["max_seconds"] == permits["max_seconds"]

    def test_an_uncontended_acquisition_is_counted_but_not_a_wait(self):
        recorder = rp.PressureRecorder()
        gate = rp.MeteredSemaphore(2, pool="review")
        with rp.recording(recorder):
            with gate:
                pass
            with gate:
                pass
        permits = recorder.summary()["permit_waits"]
        assert permits["acquisitions"] == 2
        assert permits["waited"] == 0
        assert rp.SIGNAL_LOCAL_CONCURRENCY not in recorder.summary()["signals"]

    def test_a_long_permit_wait_is_the_local_concurrency_signal(self):
        recorder = rp.PressureRecorder()
        recorder.record_permit_wait(pool="collection", seconds=rp.PERMIT_SIGNAL_SECONDS)
        assert recorder.summary()["signals"] == [rp.SIGNAL_LOCAL_CONCURRENCY]
        short = rp.PressureRecorder()
        short.record_permit_wait(pool="collection", seconds=rp.PERMIT_SIGNAL_SECONDS / 2)
        assert short.summary()["signals"] == []

    def test_it_is_a_bounded_semaphore(self):
        gate = rp.MeteredSemaphore(1, pool="x")
        with gate:
            assert gate.acquire(blocking=False) is False
        with pytest.raises(ValueError):
            gate.release()
        assert repr(gate) == "MeteredSemaphore(value=1, pool='x')"

    def test_the_reservoir_keeps_exact_counts_past_its_cap(self):
        recorder = rp.PressureRecorder()
        for index in range(3000):
            recorder.record_permit_wait(pool="review", seconds=index / 1000.0)
        permits = recorder.summary()["permit_waits"]
        assert permits["acquisitions"] == 3000
        assert permits["max_seconds"] == pytest.approx(2.999)
        assert permits["total_seconds"] == pytest.approx(sum(i / 1000.0 for i in range(3000)), abs=0.01)
        assert 0.0 < permits["p50_seconds"] < permits["p95_seconds"] <= permits["max_seconds"]

    def test_the_pipelines_pools_are_metered(self):
        import inspect

        from src.orchestration import program_pipeline
        from src.research import requirements_research
        from src.review import realtime_review
        from src.input import drawing_digest

        for module, pool in (
            (pl, "verification"),
            (program_pipeline, "research"),
            (program_pipeline, "collection"),
            (requirements_research, "research"),
            (realtime_review, "review"),
            (drawing_digest, "drawing_digest"),
        ):
            source = inspect.getsource(module)
            assert f'pool="{pool}"' in source, (module.__name__, pool)
            assert "threading.BoundedSemaphore(" not in source, module.__name__


# ---------------------------------------------------------------------------
# HTTP responses: throttling statuses and rate-limit headroom
# ---------------------------------------------------------------------------


class TestHttpResponses:
    def test_headers_are_read_case_insensitively(self):
        recorder = rp.PressureRecorder()
        recorder.record_http_response(
            status_code=429,
            headers={
                "Anthropic-Ratelimit-Tokens-Limit": "400000",
                "anthropic-ratelimit-tokens-remaining": "0",
                "anthropic-ratelimit-tokens-reset": "2026-10-06T19:00:05Z",
                "Anthropic-Ratelimit-Requests-Limit": "50",
                "anthropic-ratelimit-requests-remaining": "48",
                "Retry-After": "12",
            },
        )
        summary = recorder.summary()
        http = summary["http_responses"]
        assert http == {
            "count": 1, "status_429": 1, "status_529": 0, "status_5xx_other": 0, "retry_after_count": 1,
        }
        headroom = summary["rate_limit_headroom"]
        assert headroom["responses_with_headers"] == 1
        assert headroom["exhausted_responses"] == 1
        assert headroom["low_responses"] == 1
        assert headroom["lowest"] == {
            "dimension": "tokens", "fraction": 0.0, "remaining": 0, "limit": 400000,
            "reset": "2026-10-06T19:00:05Z", "status_code": 429,
        }
        assert headroom["by_dimension"]["requests"]["lowest_fraction"] == 0.96
        assert headroom["by_dimension"]["requests"]["exhausted_responses"] == 0
        assert summary["signals"] == [rp.SIGNAL_PROVIDER_THROTTLING]

    def test_statuses_are_bucketed(self):
        recorder = rp.PressureRecorder()
        for status in (200, 200, 429, 529, 500, 503):
            recorder.record_http_response(status_code=status, headers={})
        http = recorder.summary()["http_responses"]
        assert http["count"] == 6
        assert (http["status_429"], http["status_529"], http["status_5xx_other"]) == (1, 1, 2)

    def test_unreadable_header_values_are_ignored(self):
        recorder = rp.PressureRecorder()
        recorder.record_http_response(
            status_code=200,
            headers={
                "anthropic-ratelimit-tokens-limit": "lots",
                "anthropic-ratelimit-tokens-remaining": "5",
                "anthropic-ratelimit-requests-limit": "0",
                "anthropic-ratelimit-requests-remaining": "0",
            },
        )
        headroom = recorder.summary()["rate_limit_headroom"]
        assert headroom["responses_with_headers"] == 0
        assert headroom["lowest"] is None
        assert recorder.summary()["observed"] is False
        # Not a mapping at all: counted as a response, nothing else.
        recorder.record_http_response(status_code=200, headers=object())  # type: ignore[arg-type]
        assert recorder.summary()["http_responses"]["count"] == 2

    def test_the_lowest_headroom_is_kept_across_responses(self):
        recorder = rp.PressureRecorder()
        for remaining in ("300000", "20000", "150000"):
            recorder.record_http_response(
                status_code=200,
                headers={
                    "anthropic-ratelimit-tokens-limit": "400000",
                    "anthropic-ratelimit-tokens-remaining": remaining,
                },
            )
        headroom = recorder.summary()["rate_limit_headroom"]
        assert headroom["lowest"]["remaining"] == 20000
        assert headroom["lowest"]["fraction"] == 0.05
        assert headroom["low_responses"] == 1
        assert headroom["exhausted_responses"] == 0
        assert recorder.summary()["observed"] is False

    def test_exhausted_headroom_events_are_capped(self):
        events: list[tuple] = []
        recorder = rp.PressureRecorder(on_event=lambda *args: events.append(args))
        for _ in range(5):
            recorder.record_http_response(
                status_code=200,
                headers={
                    "anthropic-ratelimit-tokens-limit": "100",
                    "anthropic-ratelimit-tokens-remaining": "0",
                },
            )
        assert len(events) == 3
        assert all(event[3]["pressure"] == "headroom_exhausted" for event in events)
        assert recorder.summary()["rate_limit_headroom"]["exhausted_responses"] == 5

    def test_the_observer_fires_on_a_real_httpx_response(self):
        def handler(_request):
            return httpx2.Response(
                429,
                headers={
                    "anthropic-ratelimit-tokens-limit": "1000",
                    "anthropic-ratelimit-tokens-remaining": "0",
                    "retry-after": "3",
                },
            )

        http = httpx2.Client(transport=httpx2.MockTransport(handler))
        client = SimpleNamespace(_client=http)
        assert rp.observe_sdk_client(client) is True
        assert rp.observe_sdk_client(client) is True
        assert http.event_hooks["response"].count(rp.observe_http_response) == 1
        recorder = rp.PressureRecorder()
        with rp.recording(recorder):
            http.get("https://api.anthropic.com/v1/messages")
        http.get("https://api.anthropic.com/v1/messages")  # no recorder: not counted
        summary = recorder.summary()
        assert summary["http_responses"]["status_429"] == 1
        assert summary["rate_limit_headroom"]["exhausted_responses"] == 1

    def test_the_observer_attaches_to_the_sdk_client_and_its_views(self):
        client = anthropic.Anthropic(api_key="not-a-real-key")
        assert rp.observe_sdk_client(client) is True
        hooks = client._client.event_hooks["response"]
        assert hooks.count(rp.observe_http_response) == 1
        view = client.with_options(max_retries=0)
        assert view._client is client._client

    def test_an_object_without_an_http_client_is_left_alone(self):
        assert rp.observe_sdk_client(object()) is False
        assert rp.observe_sdk_client(SimpleNamespace(_client=SimpleNamespace(event_hooks=None))) is False

    def test_the_client_factory_attaches_the_observer(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "factory-key-not-real")
        monkeypatch.setattr(reviewer, "_cached_client", None)
        monkeypatch.setattr(reviewer, "_cached_key", None)
        client = reviewer._get_client()
        assert rp.observe_http_response in client._client.event_hooks["response"]
        no_retry = reviewer._get_client(sdk_retries=False)
        assert no_retry._client is client._client


# ---------------------------------------------------------------------------
# Shared verification and batch polling
# ---------------------------------------------------------------------------


class TestSharedVerificationAndBatches:
    def test_a_follower_wait_that_times_out_is_a_coordination_signal(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS", "0.05")
        cache = VerificationCache()
        leader = cache.singleflight.claim_many(["k"])["k"]
        follower = cache.singleflight.claim_many(["k"])["k"]
        assert leader.leader and not follower.leader
        recorder = rp.PressureRecorder()
        with rp.recording(recorder):
            assert pl._wait_for_singleflight_leader(cache, follower, findings=2) is False
            cache.singleflight.complete(leader)
            assert pl._wait_for_singleflight_leader(cache, follower, findings=1) is True
        shared = recorder.summary()["singleflight_waits"]
        assert shared["count"] == 2
        assert shared["timed_out"] == 1
        assert shared["findings_orphaned"] == 2
        assert shared["max_seconds"] >= 0.05
        assert rp.SIGNAL_COORDINATION_WAIT in recorder.summary()["signals"]

    @staticmethod
    def _status(*, done: bool, expired: int = 0) -> BatchStatus:
        return BatchStatus(
            status="ended" if done else "in_progress",
            processing=0 if done else 2,
            succeeded=(2 - expired) if done else 0,
            errored=0,
            canceled=0,
            expired=expired if done else 0,
            total=2,
        )

    def test_a_poll_records_its_error_waits_and_expired_items(self, monkeypatch):
        install_fake_retry_timing(monkeypatch)
        outcomes = [_rate_limited({"retry-after": "4"}), self._status(done=False), self._status(done=True, expired=2)]

        def fake_poll(batch_id, **kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(rt, "poll_batch", fake_poll)
        recorder = rp.PressureRecorder()
        with rp.recording(recorder):
            outcome = poll_batch_bounded(
                "msgbatch_x", policy=PollPolicy(), log=lambda *a, **k: None, progress_cb=lambda _s: None
            )
        assert outcome.terminal is True
        summary = recorder.summary()
        waits = summary["retry_waits"]
        assert waits["count"] == 1
        assert waits["by_label"] == {"batch_poll": {"count": 1, "total_seconds": waits["total_seconds"]}}
        assert waits["server_floor_count"] == 1
        assert waits["by_failure_class"] == {"rate_limit": {"count": 1, "total_seconds": waits["total_seconds"]}}
        batch = summary["batch_polls"]
        assert batch["count"] == 1
        assert batch["expired_requests"] == 2
        assert batch["terminal"] == {"ended": 1}
        assert rp.SIGNAL_BATCH_QUEUE in summary["signals"]

    def test_a_detach_for_no_progress_is_a_batch_queue_signal(self, monkeypatch):
        install_fake_retry_timing(monkeypatch)
        monkeypatch.setattr(rt, "poll_batch", lambda batch_id, **kwargs: self._status(done=False))
        clock = {"now": 0.0}
        monkeypatch.setattr(rt.time, "monotonic", lambda: clock["now"])
        original_wait = rt.poll_batch  # noqa: F841 — the fake timing's wait advances nothing here

        def advancing_wait(seconds, cancel_event=None):
            clock["now"] += float(seconds)
            return True

        from src.verification import retry_policy

        monkeypatch.setattr(
            retry_policy,
            "DEFAULT_RETRY_TIMING",
            retry_policy.RetryTiming(wait=advancing_wait, now=lambda: 0.0, random=lambda: 0.0),
        )
        recorder = rp.PressureRecorder()
        with rp.recording(recorder):
            outcome = poll_batch_bounded(
                "msgbatch_y",
                policy=PollPolicy(max_no_progress_seconds=60, poll_interval_seconds=30),
                log=lambda *a, **k: None,
                progress_cb=lambda _s: None,
            )
        assert outcome.detached is True and outcome.detach_reason == "no_progress"
        batch = recorder.summary()["batch_polls"]
        assert batch["detached"] == {"no_progress": 1}
        assert recorder.summary()["signals"] == [rp.SIGNAL_BATCH_QUEUE]

    def test_a_cancelled_poll_is_not_recorded(self, monkeypatch):
        install_fake_retry_timing(monkeypatch)
        monkeypatch.setattr(rt, "poll_batch", lambda batch_id, **kwargs: self._status(done=False))
        cancel = threading.Event()
        cancel.set()
        recorder = rp.PressureRecorder()
        with rp.recording(recorder):
            outcome = poll_batch_bounded(
                "msgbatch_z", policy=PollPolicy(), log=lambda *a, **k: None,
                progress_cb=lambda _s: None, cancel_event=cancel,
            )
        assert outcome.user_canceled is True
        assert recorder.summary()["batch_polls"]["count"] == 0


# ---------------------------------------------------------------------------
# The diagnostics report: ledger, summary, wording, lifecycle
# ---------------------------------------------------------------------------


class TestDiagnosticsReport:
    def test_a_clean_report_says_none_observed(self):
        report = DiagnosticsReport()
        summary = report.summary()
        block = summary["resource_pressure"]
        assert block["observed"] is False and block["signals"] == []
        json.dumps(summary)
        lines = resource_pressure_lines(summary)
        assert lines[0] == "Resource pressure: none observed."
        assert "  Retry waits: none" in lines
        assert "  HTTP responses: none observed (rate-limit headroom unknown)" in lines
        assert "  Local concurrency: no permit acquisitions recorded" in lines
        assert "Resource pressure: none observed." in report.to_text()

    def test_a_report_without_the_block_yields_no_lines(self):
        assert resource_pressure_lines({}) == []
        assert resource_pressure_lines({"resource_pressure": {}}) == []

    def test_the_report_records_through_the_installed_recorder(self):
        report = DiagnosticsReport()
        report.start_pressure_recording()
        assert rp.current() is report.pressure
        try:
            rp.record_retry_wait(
                label="verification", seconds=32.0, failure_class="rate_limit", server_floor=True, attempt=1
            )
            rp.record_retry_abandoned(
                label="verification", failure_class="rate_limit", stop="retry_budget_spent", attempts=3
            )
        finally:
            report.finish()
        assert rp.current() is None
        summary = report.summary()
        block = summary["resource_pressure"]
        assert block["observed"] is True
        assert block["signals"] == [rp.SIGNAL_PROVIDER_THROTTLING]
        # The timeline got the two warning lines; neither reads as an API call.
        pressure_events = [e for e in report.events if e.data and e.data.get("pressure")]
        assert [e.data["pressure"] for e in pressure_events] == ["retry_wait", "retry_abandoned"]
        assert all(e.level == "warning" and e.phase == "verification" for e in pressure_events)
        assert summary["phase_telemetry"] == {}
        assert summary["cost_summary"]["estimated_cost_usd"]["priced_calls"] == 0
        text = report.to_text()
        assert "Resource pressure: observed — API throttling (rate limits or overload)." in text
        assert "Retry waits: 1 totalling 32.0s (longest 32.0s; 1 set by the API's retry-after) — rate_limit=1" in text
        assert "Given up after capacity failures: 1 request(s) — rate_limit=1 (retry_budget_spent=1)" in text
        json.dumps(summary)

    def test_finish_removes_only_this_reports_recorder(self):
        older = DiagnosticsReport()
        older.start_pressure_recording()
        newer = DiagnosticsReport()
        newer.start_pressure_recording()
        assert rp.current() is newer.pressure
        older.finish()
        assert rp.current() is newer.pressure
        newer.finish()
        assert rp.current() is None

    def test_the_wording_covers_every_source(self):
        recorder = rp.PressureRecorder()
        recorder.record_retry_wait(label="review", seconds=5.0, failure_class="rate_limit")
        recorder.record_retry_wait(label="research", seconds=90.0, failure_class="connection")
        recorder.record_retry_abandoned(label="research", failure_class="connection", stop="attempts_exhausted")
        recorder.record_permit_wait(pool="review", seconds=0.0)
        recorder.record_permit_wait(pool="review", seconds=2.5)
        recorder.record_singleflight_wait(seconds=12.0, timed_out=False)
        recorder.record_http_response(status_code=200, headers={})
        recorder.record_http_response(
            status_code=429,
            headers={"anthropic-ratelimit-tokens-limit": "100", "anthropic-ratelimit-tokens-remaining": "0"},
        )
        recorder.record_batch_poll(seconds=5400.0, terminal_status="ended", expired=1, batch_id="msgbatch_q")
        lines = resource_pressure_lines({"resource_pressure": recorder.summary()})
        assert lines[0] == (
            "Resource pressure: observed — API throttling (rate limits or overload); "
            "connection errors; the app's own concurrency limits; batch queue delays."
        )
        joined = "\n".join(lines)
        assert "Retry waits: 2 totalling 1.6m (longest 1.5m; 0 set by the API's retry-after) — connection=1, rate_limit=1" in joined
        assert "Given up after capacity failures: 1 request(s) — connection=1 (attempts_exhausted=1)" in joined
        assert "HTTP responses: 2 — 429=1, 529=0, other 5xx=0, with retry-after=0" in joined
        assert "Rate-limit headroom: lowest 0% of tokens (0 of 100 remaining); 1 response(s) at zero, 1 below 10%, across 1 with headers" in joined
        assert "Local concurrency: 2 permit acquisition(s), 1 waited (2.5s total, longest 2.5s, p95 2.5s) — review 1/2 waited (2.5s)" in joined
        assert "Shared verification: 1 follower wait(s) totalling 12.0s (longest 12.0s); 0 timed out, 0 finding(s) then verified alone" in joined
        assert "Batch processing: 1 poll(s), longest 1.5h; 1 request(s) expired unprocessed; detached: none" in joined
        assert lines[-1].startswith("  A wait here is time the run spent stalled")

    def test_summary_is_byte_identical_with_and_without_a_recorder_installed(self):
        """Installing a recorder that saw nothing changes no summary byte."""
        quiet = DiagnosticsReport(run_id="fixed", started_at=1_700_000_000.0)
        quiet.log("review", "info", "a line")
        quiet.ended_at = 1_700_000_100.0
        plain = json.dumps(quiet.summary(), sort_keys=True, default=str)
        installed = DiagnosticsReport(run_id="fixed", started_at=1_700_000_000.0)
        installed.start_pressure_recording()
        installed.log("review", "info", "a line")
        installed.finish()
        installed.ended_at = 1_700_000_100.0
        assert json.dumps(installed.summary(), sort_keys=True, default=str) == plain
