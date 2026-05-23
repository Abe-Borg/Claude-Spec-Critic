"""Agent tracing for Spec Critic.

Public surface:

    get_recorder() -> TraceRecorder | None
        Returns the currently-installed recorder, or None when tracing is off.

    set_recorder(recorder: TraceRecorder | None)
        Install or clear the global recorder.

    current_span() -> SpanHandle | None
        The active span on this task (per-task via contextvars). Used by
        capture hooks that don't have an explicit handle to attach to.

    TraceRecorder(run_id, trace_dir, capture_level)
        The recorder itself. Spin up with .start(); tear down with .stop().

    capture_* (from capture_hooks)
        The integration surface used by the rest of the codebase. Every
        hook is defensive — a tracing failure never escapes.

Config helpers live in ``config``; data types live in ``spans``; the
recorder in ``recorder``; the capture hooks in ``capture_hooks``.

Default-on: ``SPEC_CRITIC_TRACE`` controls the main switch (disable with
``0/false/no/off``). ``SPEC_CRITIC_TRACE_DEEP=1`` opts into deep mode.
"""
from __future__ import annotations

from .config import (
    LEVEL_DEEP,
    LEVEL_DEFAULT,
    LEVEL_OFF,
    current_capture_level,
    default_trace_root,
    trace_deep_enabled,
    trace_dir_for_run,
    trace_enabled,
)
from .recorder import (
    TraceRecorder,
    bind_to_current_context,
    current_span,
    get_recorder,
    set_recorder,
)
from .spans import (
    AgentSpan,
    SpanHandle,
    new_span_id,
)

__all__ = [
    "LEVEL_DEEP",
    "LEVEL_DEFAULT",
    "LEVEL_OFF",
    "current_capture_level",
    "default_trace_root",
    "trace_deep_enabled",
    "trace_dir_for_run",
    "trace_enabled",
    "TraceRecorder",
    "bind_to_current_context",
    "current_span",
    "get_recorder",
    "set_recorder",
    "AgentSpan",
    "SpanHandle",
    "new_span_id",
]
