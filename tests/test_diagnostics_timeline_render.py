"""The diagnostics EVENT TIMELINE renders in bounded chunks, not one loop.

``DiagnosticsWindow._render_timeline_section`` used to insert 3-5 text runs
per event inside one synchronous loop, so a long run's timeline (thousands of
events) froze the window while it built. Now ``_render_timeline_events``
inserts ``TIMELINE_CHUNK_EVENTS`` events synchronously and schedules the rest
with ``after(0, ...)``; the visible result is byte-identical to the old loop.

The window subclasses ``customtkinter.CTkToplevel``; we build an instance via
``object.__new__`` (no Tk root / display) and drive the pure rendering method
against a fake textbox, mirroring ``tests/test_activity_log_pump.py``.
"""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("customtkinter")

from src.gui import widgets  # noqa: E402
from src.gui.widgets import (  # noqa: E402
    TIMELINE_CHUNK_EVENTS,
    DiagnosticsWindow,
    timeline_event_runs,
)
from src.orchestration.diagnostics import DiagnosticEvent  # noqa: E402


class _FakeInnerText:
    def __init__(self):
        self.inserts: list[tuple[str, tuple]] = []

    def insert(self, _index, text, tags=()):
        self.inserts.append((text, tuple(tags)))


class _FakeTextbox:
    def __init__(self):
        self._textbox = _FakeInnerText()
        self.states: list[str] = []

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.states.append(kwargs["state"])


def _make_window() -> DiagnosticsWindow:
    window = object.__new__(DiagnosticsWindow)
    window.scheduled: list[tuple[int, object]] = []

    def _fake_after(delay, callback):
        window.scheduled.append((delay, callback))
        return len(window.scheduled)

    window.after = _fake_after
    return window


def _drain(window) -> int:
    """Fire scheduled continuations FIFO; return how many ran."""
    ran = 0
    while window.scheduled:
        _delay, callback = window.scheduled.pop(0)
        callback()
        ran += 1
    return ran


def _events(n: int) -> list[DiagnosticEvent]:
    levels = ["info", "success", "warning", "error", "step", "unknown-level"]
    out = []
    for i in range(n):
        out.append(
            DiagnosticEvent(
                timestamp=1_700_000_000.0 + i,
                elapsed=i * 0.25,
                phase="review" if i % 3 else "",
                level=levels[i % len(levels)],
                message=f"event {i}",
                data={"k": i, "url": "https://x"} if i % 4 == 0 else None,
            )
        )
    return out


def _legacy_synchronous_runs(events) -> list[tuple[str, tuple]]:
    """The pre-chunking loop, transcribed verbatim, as the parity oracle."""
    level_icons = {"info": "  ", "success": "+ ", "warning": "! ", "error": "X ", "step": "> "}
    runs: list[tuple[str, tuple]] = []
    for i, e in enumerate(events):
        if i > 0:
            runs.append(("\n", ()))
        ts = datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S")
        elapsed = f"{e.elapsed:7.1f}s"
        icon = level_icons.get(e.level, "  ")
        phase_str = f"[{e.phase}]" if e.phase else ""
        runs.append((f"{ts} {elapsed} ", ("info",)))
        runs.append((icon, (e.level,)))
        if phase_str:
            runs.append((f"{phase_str:20s} ", ("phase_tag",)))
        runs.append((e.message, (e.level,)))
        if e.data:
            for k, v in e.data.items():
                runs.append((f"\n{'':38s}{k}: {v}", ("data_tag",)))
    return runs


def test_event_runs_match_the_legacy_loop_per_event():
    events = _events(37)
    expected = _legacy_synchronous_runs(events)
    actual = [
        run for i, e in enumerate(events) for run in timeline_event_runs(e, first=(i == 0))
    ]
    assert actual == expected


def test_five_thousand_events_render_through_the_chunked_path():
    events = _events(5_000)
    window = _make_window()
    textbox = _FakeTextbox()

    window._render_timeline_events(textbox, events)

    # Synchronous part: exactly one chunk, never the whole timeline.
    first_chunk_runs = list(textbox._textbox.inserts)
    rendered_events = sum(1 for text, tags in first_chunk_runs if text == "\n" and tags == ()) + 1
    assert rendered_events == TIMELINE_CHUNK_EVENTS
    assert TIMELINE_CHUNK_EVENTS < len(events)
    # One continuation is pending, scheduled with a zero delay, and the
    # textbox was re-disabled before yielding to the event loop.
    assert [d for d, _ in window.scheduled] == [0]
    assert textbox.states[-1] == "disabled"

    ran = _drain(window)

    assert ran == -(-len(events) // TIMELINE_CHUNK_EVENTS) - 1  # ceil(n / chunk) - 1
    assert textbox._textbox.inserts == _legacy_synchronous_runs(events)
    assert textbox.states[-1] == "disabled"
    assert textbox.states.count("normal") == textbox.states.count("disabled") == ran + 1
    assert window.scheduled == []


def test_a_short_timeline_renders_synchronously_with_no_continuation():
    events = _events(TIMELINE_CHUNK_EVENTS)
    window = _make_window()
    textbox = _FakeTextbox()

    window._render_timeline_events(textbox, events)

    assert window.scheduled == []
    assert textbox._textbox.inserts == _legacy_synchronous_runs(events)
    assert textbox.states == ["normal", "disabled"]


def test_empty_timeline_renders_nothing_and_schedules_nothing():
    window = _make_window()
    textbox = _FakeTextbox()
    window._render_timeline_events(textbox, [])
    assert textbox._textbox.inserts == []
    assert window.scheduled == []


def test_destroyed_window_stops_the_continuation_silently():
    from tkinter import TclError

    events = _events(TIMELINE_CHUNK_EVENTS * 2)
    window = _make_window()
    textbox = _FakeTextbox()
    window._render_timeline_events(textbox, events)
    assert len(window.scheduled) == 1

    def _dead_configure(**_kwargs):
        raise TclError('invalid command name ".!ctktoplevel"')

    textbox.configure = _dead_configure
    _drain(window)  # must not raise
    assert window.scheduled == []


def test_section_method_defers_to_the_chunked_renderer():
    """``_render_timeline_section`` builds the widgets then hands the events
    to ``_render_timeline_events`` — the only insert loop left in the file."""
    import ast
    import inspect
    import textwrap

    src = inspect.getsource(widgets.DiagnosticsWindow._render_timeline_section)
    tree = ast.parse(textwrap.dedent(src))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_render_timeline_events" in calls
    # No ``insert`` directly inside the section builder: the loop is gone.
    assert "insert" not in calls
