"""Trace-recorder teardown on every terminal batch path (STRUCTURAL_AUDIT P2-4).

The trace recorder is a module-global singleton installed at run start. It
must be torn down — drained (writer thread sent its shutdown sentinel) and
cleared from the global — when a run reaches *any* terminal state, before
``is_processing`` is reset and a fresh run is permitted. Otherwise run-1's
recorder leaks: the writer thread never gets the sentinel (so the trace is
left unflushed) and the stale recorder stays installed as the global while
run-2 starts over it.

The normal collect path stops it in ``_do_collect``'s ``finally`` and the
submit-failure path stops it inline. This file locks in the previously
missing **poll-failure / detach** path: that branch dispatches
``on_review_error`` (which resets ``is_processing`` immediately, *without*
scheduling ``reset_ui``), so it had no teardown at all. It also guards the
**success** path — the poll worker must NOT stop the recorder out from
under the collect phase that inherits it.

Imports ``src.gui.batch_controller`` (→ ``tkinter`` at module scope), so it
is registered in ``conftest._GUI_DEPENDENT_TESTS`` and skipped on hosts
without the ``python3-tk`` system package.
"""
from __future__ import annotations

import types

import pytest

from src.batch.batch_runtime import PollOutcome
from src.gui import batch_controller
from src.tracing.recorder import get_recorder, set_recorder


class _FakeRecorder:
    """Minimal stand-in that records whether ``stop()`` was called."""

    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self, **_kwargs) -> None:
        self.stop_calls += 1


class _FakeApp:
    """Only the surface ``poll_and_collect_thread`` touches."""

    def __init__(self) -> None:
        self._batch_submission = types.SimpleNamespace(
            job=types.SimpleNamespace(batch_id="batch_test_123")
        )
        self._trace_recorder: object | None = None
        self.dispatched: list = []
        self.error_calls: list[str] = []
        self.collect_calls = 0

    def _make_diag_log(self, _phase, _epoch):
        return lambda *a, **k: None

    def _dispatch_if_current(self, _epoch, fn):
        # Record the dispatch without running it: the teardown under test
        # happens synchronously on the worker thread *before* the error is
        # dispatched, so observing the global state after the call is
        # enough — we never need to execute the UI-thread lambda.
        self.dispatched.append(fn)

    def _on_review_error(self, msg):
        self.error_calls.append(msg)

    def _collect_batch_results(self):
        self.collect_calls += 1

    def _update_poll_progress(self, _status):
        pass


@pytest.fixture(autouse=True)
def _clear_global_recorder():
    """Isolate the module-global recorder around each test in this file."""
    set_recorder(None)
    yield
    set_recorder(None)


def _install_recorder(app: _FakeApp) -> _FakeRecorder:
    rec = _FakeRecorder()
    set_recorder(rec)
    app._trace_recorder = rec
    return rec


def _run_with_outcome(monkeypatch, app: _FakeApp, outcome: PollOutcome) -> None:
    monkeypatch.setattr(batch_controller, "poll_batch_bounded", lambda *a, **k: outcome)
    batch_controller.poll_and_collect_thread(app, run_epoch=1)


class TestPollFailureTeardown:
    def test_detached_poll_stops_and_clears_recorder(self, monkeypatch):
        app = _FakeApp()
        rec = _install_recorder(app)
        _run_with_outcome(
            monkeypatch, app, PollOutcome(detached=True, detach_reason="max_elapsed")
        )
        assert rec.stop_calls == 1
        assert get_recorder() is None
        assert app._trace_recorder is None

    def test_failed_poll_stops_and_clears_recorder(self, monkeypatch):
        app = _FakeApp()
        rec = _install_recorder(app)
        _run_with_outcome(
            monkeypatch, app, PollOutcome(poll_failed=True, poll_error="threshold")
        )
        assert rec.stop_calls == 1
        assert get_recorder() is None
        assert app._trace_recorder is None

    def test_teardown_precedes_error_dispatch(self, monkeypatch):
        # The recorder must be gone *before* on_review_error could run (and
        # thus before is_processing is reset). Because teardown is
        # synchronous on the worker thread, the global is already None by
        # the time the error handler is queued for dispatch.
        app = _FakeApp()
        _install_recorder(app)
        _run_with_outcome(
            monkeypatch, app, PollOutcome(detached=True, detach_reason="no_progress")
        )
        assert get_recorder() is None
        # The error handler was still dispatched (one queued callable).
        assert len(app.dispatched) == 1

    def test_no_recorder_is_safe(self, monkeypatch):
        # Tracing disabled: app._trace_recorder is None and the global is
        # None. Teardown must be a no-op, not a crash.
        app = _FakeApp()  # deliberately no _install_recorder
        _run_with_outcome(
            monkeypatch, app, PollOutcome(poll_failed=True, poll_error="x")
        )
        assert get_recorder() is None
        assert app._trace_recorder is None


class TestSuccessPathLeavesRecorder:
    def test_successful_poll_does_not_stop_recorder(self, monkeypatch):
        # On a successful poll the worker hands off to the collect phase,
        # which owns teardown in its own finally. The poll worker must NOT
        # stop the recorder out from under collect.
        app = _FakeApp()
        rec = _install_recorder(app)
        _run_with_outcome(monkeypatch, app, PollOutcome(terminal=True))
        assert rec.stop_calls == 0
        assert get_recorder() is rec
        assert app._trace_recorder is rec
        # Collect was the dispatched hand-off (success log + collect = 2).
        assert len(app.dispatched) == 2
