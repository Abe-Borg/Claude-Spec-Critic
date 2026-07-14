"""Run-level recorder lifecycle helpers.

Thin wrappers the GUI controllers call to start / stop / reattach the
global :class:`TraceRecorder` around a review run. Kept in the tracing
package (not the GUI) so they import without ``customtkinter`` and stay
unit-testable in a headless environment.

- ``start_run_recorder``: gated on ``SPEC_CRITIC_TRACE``; creates a fresh
  recorder keyed by ``run_id`` (which the caller sources from
  ``DiagnosticsReport.run_id`` so the trace correlates with diagnostics).
- ``reattach_run_recorder``: reopens an existing trace directory on an
  app-restart batch resume so the resumed work appends to the original
  run's trace rather than starting a new one.
- ``stop_run_recorder``: drains + closes and clears the global recorder.
"""
from __future__ import annotations

from pathlib import Path

from .config import current_capture_level, trace_dir_for_run, trace_enabled
from .recorder import TraceRecorder, set_recorder


def _version() -> str:
    try:
        from .. import __version__
        return __version__
    except Exception:
        return ""


def start_run_recorder(
    *,
    run_id: str,
    mode: str,
    model: str,
    cycle_label: str,
    files: list,
    module_id: str = "",
    project_profile: dict | None = None,
) -> TraceRecorder | None:
    """Start a recorder for a new run, or return ``None`` when tracing is off.

    Reads ``current_capture_level()`` at call time so a GUI toggle that
    just flipped the env var takes effect on the next run without a
    process restart.
    """
    if not trace_enabled():
        return None
    rec = TraceRecorder(
        run_id=run_id,
        trace_dir=trace_dir_for_run(run_id),
        capture_level=current_capture_level(),
        spec_critic_version=_version(),
    )
    rec.start(
        mode=mode,
        model=model,
        cycle_label=cycle_label,
        module_id=module_id,
        files_reviewed=[p.name if hasattr(p, "name") else str(p) for p in files],
        project_profile=project_profile,
    )
    set_recorder(rec)
    return rec


def reattach_run_recorder(trace_meta: dict | None) -> TraceRecorder | None:
    """Reopen a recorder against an existing trace dir.

    ``trace_meta`` is a ``{run_id, trace_dir, capture_level}`` dict —
    ``None`` / empty when the original run had tracing off. A second
    ``TraceRecorder.start()`` against the same directory appends to the
    existing JSONL files.
    """
    if not trace_meta or not trace_meta.get("run_id"):
        return None
    trace_dir = trace_meta.get("trace_dir") or str(trace_dir_for_run(trace_meta["run_id"]))
    rec = TraceRecorder(
        run_id=trace_meta["run_id"],
        trace_dir=Path(trace_dir),
        capture_level=trace_meta.get("capture_level", "default"),
        spec_critic_version=_version(),
    )
    rec.start()  # appends to existing files; rewrites run.json with resumed_at
    set_recorder(rec)
    return rec


def stop_run_recorder(recorder: TraceRecorder | None) -> None:
    if recorder is None:
        return
    try:
        recorder.stop()
    finally:
        set_recorder(None)
