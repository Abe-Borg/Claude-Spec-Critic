"""Regression tests for the ``EnhancedLog`` activity-log pacing pump.

Locks in the contract that clicking **Clear** fully resets the paced-drain
state machine so the activity log resumes streaming live activity — even if
the pump was mid-cycle or had wedged. See ``EnhancedLog._process_queue`` and
``EnhancedLog.clear`` in ``src/gui/widgets.py``.

Background: the pump drains one queued line per ``after`` tick and uses a
``_processing_queue`` guard so only one drain loop runs. The original
``clear`` cleared the deque and the textbox but left ``_processing_queue``
untouched and never cancelled the in-flight pacing timer. If the guard was
True at that moment (or had been left True by a line that failed to render
between setting the guard and scheduling the next tick), every subsequent
``_queue_log`` saw the guard True and refused to restart the drain — so the
queue filled but never emptied and the log stayed blank for the rest of the
run. Clicking Clear could not recover it.

The widget subclasses ``customtkinter.CTkFrame`` and its methods call Tk
(``self.after``, the textbox). We build an instance via ``object.__new__`` to
bypass ``__init__`` (no Tk root / display required) and stub the Tk-facing
seams, exercising the pure pump logic hermetically.
"""
from __future__ import annotations

from collections import deque

import pytest

# GUI deps are optional in the hermetic suite; skip cleanly when absent
# (mirrors the repo convention that GUI tests skip without tkinter/ctk).
pytest.importorskip("customtkinter")

from src.gui.widgets import EnhancedLog  # noqa: E402


class _FakeInner:
    """Stand-in for the CTkTextbox's underlying tk Text widget."""

    def delete(self, *_args):
        pass

    def index(self, *_args):
        return "1.0"

    def insert(self, *_args):
        pass

    def see(self, *_args):
        pass


class _FakeTextbox:
    def __init__(self):
        self._textbox = _FakeInner()

    def configure(self, **_kwargs):
        pass


def _make_log() -> EnhancedLog:
    """An ``EnhancedLog`` with pump state initialized but no real Tk widgets.

    ``after`` is a deterministic fake: it records the scheduled callback and
    returns an id; the test fires it explicitly via ``_fire``. ``_append_line``
    is replaced by a recorder so no textbox rendering is required.
    """
    log = object.__new__(EnhancedLog)
    log._log_queue = deque()
    log._processing_queue = False
    log._queue_after_id = None
    log._textbox = _FakeTextbox()

    log.rendered = []  # list[tuple[str, str, bool]] of (msg, level, ts)
    log._pending = {}  # after_id -> callback
    log._cancelled = []  # after_ids passed to after_cancel
    log._next_after_id = 0

    def _fake_after(_delay, cb):
        log._next_after_id += 1
        aid = log._next_after_id
        log._pending[aid] = cb
        return aid

    def _fake_after_cancel(aid):
        log._cancelled.append(aid)
        log._pending.pop(aid, None)

    def _fake_append_line(msg, level, ts):
        log.rendered.append((msg, level, ts))

    log.after = _fake_after
    log.after_cancel = _fake_after_cancel
    log._append_line = _fake_append_line
    return log


def _fire(log) -> bool:
    """Fire the single pending pacing callback (FIFO). Returns False if none."""
    if not log._pending:
        return False
    aid = next(iter(log._pending))
    cb = log._pending.pop(aid)
    cb()
    return True


def test_clear_recovers_a_wedged_pump_and_drain_resumes():
    log = _make_log()
    # Simulate the wedged state: guard stuck True with no live timer. New logs
    # queue but never drain — exactly the "log stays blank while processing"
    # symptom.
    log._processing_queue = True
    log._queue_log("stuck one", "info", True, 400)
    log._queue_log("stuck two", "info", True, 400)
    assert log.rendered == []  # nothing drained
    assert len(log._log_queue) == 2

    log.clear()

    # Clear resets the pump, drops the stale backlog, and shows a confirmation.
    assert log._processing_queue is False
    assert len(log._log_queue) == 0
    assert log._queue_after_id is None
    assert log.rendered == [("Activity log cleared.", "muted", True)]

    # The next line restarts the drain — the pump is healthy again.
    log.rendered.clear()
    log.log("live activity")
    assert log.rendered == [("live activity", "info", True)]
    assert log._processing_queue is True
    assert log._queue_after_id is not None  # next tick scheduled


def test_clear_cancels_the_inflight_pacing_timer():
    log = _make_log()
    log.log("a")  # drains "a" synchronously, schedules the next tick
    log.log("b")  # queued behind the pacing timer
    pending_id = log._queue_after_id
    assert pending_id is not None
    assert len(log._log_queue) == 1  # "b" still queued

    log.clear()

    assert pending_id in log._cancelled  # live timer cancelled, no stray tick
    assert log._queue_after_id is None
    assert log._processing_queue is False
    assert len(log._log_queue) == 0


def test_clear_confirmation_then_new_activity_streams_beneath_it():
    log = _make_log()
    log.log("old line")
    log.clear()
    assert log.rendered[-1] == ("Activity log cleared.", "muted", True)

    # Subsequent activity keeps flowing (single, healthy drain loop).
    log.rendered.clear()
    log.log("step 1")  # drains immediately
    log.log("step 2")  # queued
    assert log.rendered == [("step 1", "info", True)]
    assert _fire(log)  # fire the paced tick for "step 2"
    assert log.rendered == [("step 1", "info", True), ("step 2", "info", True)]


def test_failed_render_does_not_wedge_the_pump():
    """A line that fails to render must not freeze the log for the whole run.

    The reschedule lives in a ``finally`` so the guard is never left True with
    no live timer.
    """
    log = _make_log()

    def _boom(_msg, _level, _ts):
        raise RuntimeError("render failed")

    log._append_line = _boom
    with pytest.raises(RuntimeError):
        log.log("will fail")  # first drain raises inside _process_queue

    # Despite the failure the pump rescheduled itself (guard True + live timer).
    assert log._processing_queue is True
    assert log._queue_after_id is not None

    # And it keeps draining once rendering recovers.
    recovered = []
    log._append_line = lambda m, l, t: recovered.append(m)
    log.log("recovers")  # queued behind the rescheduled tick
    assert _fire(log)
    assert "recovers" in recovered
