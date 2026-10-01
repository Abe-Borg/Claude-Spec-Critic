"""Run-level recorder lifecycle helpers.

Thin wrappers the GUI controllers call to start / stop / reattach the
global :class:`TraceRecorder` around a review run. Kept in the tracing
package (not the GUI) so they import without ``customtkinter`` and stay
unit-testable in a headless environment.

- ``start_run_recorder``: gated on ``SPEC_CRITIC_TRACE``; creates a fresh
  recorder keyed by ``run_id`` (which the caller sources from
  ``DiagnosticsReport.run_id`` so the trace correlates with diagnostics),
  then applies automatic retention to *older* runs
  (``retention.apply_startup_retention`` — never the run just started,
  never raises).
- ``reattach_run_recorder``: reopens an existing trace directory on an
  app-restart batch resume so the resumed work appends to the original
  run's trace rather than starting a new one.
- ``stop_run_recorder``: drains + closes the recorder and clears the global
  recorder when it is still this one.

Tracing is optional (plan WP-13). Starting or reattaching never raises: a
trace directory that cannot be created, a ``run.json`` that cannot be
written, or a writer thread that cannot start disposes whatever was
started, logs one warning (and hands the same sentence to ``warn`` when
the caller gives one), and returns ``None`` — the review runs without a
trace. Stopping never raises either, so a teardown failure can never
replace the error that ended a run, and it clears the global only when the
global is still the recorder being stopped, so a late teardown from an
older run leaves a newer run's recorder in place.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .config import current_capture_level, trace_dir_for_run, trace_enabled
from .recorder import TraceRecorder, clear_recorder, set_recorder
from .retention import apply_startup_retention

_log = logging.getLogger(__name__)

Warn = Callable[[str], None]


def _version() -> str:
    try:
        from .. import __version__
        return __version__
    except Exception:
        return ""


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _abandon(recorder: TraceRecorder | None, action: str, exc: BaseException, warn: Warn | None) -> None:
    """Dispose of a partially started recorder and say so, once."""
    if recorder is not None:
        try:
            recorder.discard()
        except Exception as stop_exc:  # noqa: BLE001 — disposal is best effort
            _log.debug("Disposing a partially started trace recorder failed: %s", stop_exc)
        clear_recorder(recorder)
    message = (
        f"Tracing is off for this run: the trace recorder could not {action} "
        f"({_describe(exc)}). The review continues without a trace."
    )
    _log.warning(message)
    if warn is not None:
        try:
            warn(message)
        except Exception as warn_exc:  # noqa: BLE001 — a warning sink never ends a run
            _log.debug("Trace warning callback failed: %s", warn_exc)


def start_run_recorder(
    *,
    run_id: str,
    mode: str,
    model: str,
    cycle_label: str,
    files: list,
    module_id: str = "",
    project_profile: dict | None = None,
    warn: Warn | None = None,
) -> TraceRecorder | None:
    """Start a recorder for a new run, or return ``None`` when tracing is off.

    Reads ``current_capture_level()`` at call time so a GUI toggle that
    just flipped the env var takes effect on the next run without a
    process restart. Returns ``None`` too when the recorder could not
    start; the failure is logged once and passed to ``warn`` (see the
    module docstring).
    """
    rec: TraceRecorder | None = None
    try:
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
    except Exception as exc:  # noqa: BLE001 — tracing is optional
        _abandon(rec, "start", exc, warn)
        return None
    # Automatic retention: the new run's directory now exists (run.json was
    # written synchronously by start()), so it can be protected by identity
    # while older runs are pruned per SPEC_CRITIC_TRACE_RETENTION_DAYS /
    # SPEC_CRITIC_TRACE_MAX_RUNS. Never raises. A resume
    # (``reattach_run_recorder``) is not a new run and does not prune.
    try:
        apply_startup_retention(current_run_dir=rec.trace_dir, root=rec.trace_dir.parent)
    except Exception as exc:  # noqa: BLE001 — pruning old traces never ends a run
        _log.warning("Trace retention failed: %s", _describe(exc))
    return rec


def reattach_run_recorder(
    trace_meta: dict | None, *, warn: Warn | None = None
) -> TraceRecorder | None:
    """Reopen a recorder against an existing trace dir.

    ``trace_meta`` is a ``{run_id, trace_dir, capture_level}`` dict —
    ``None`` / empty when the original run had tracing off. A second
    ``TraceRecorder.start()`` against the same directory appends to the
    existing JSONL files. A failure to reopen returns ``None`` with one
    warning, as :func:`start_run_recorder` does.
    """
    if not trace_meta or not trace_meta.get("run_id"):
        return None
    rec: TraceRecorder | None = None
    try:
        trace_dir = trace_meta.get("trace_dir") or str(trace_dir_for_run(trace_meta["run_id"]))
        rec = TraceRecorder(
            run_id=trace_meta["run_id"],
            trace_dir=Path(trace_dir),
            capture_level=trace_meta.get("capture_level", "default"),
            spec_critic_version=_version(),
        )
        rec.start()  # appends to existing files; rewrites run.json with resumed_at
        set_recorder(rec)
    except Exception as exc:  # noqa: BLE001 — tracing is optional
        _abandon(rec, "reopen the run's trace", exc, warn)
        return None
    return rec


def stop_run_recorder(recorder: TraceRecorder | None) -> None:
    """Stop ``recorder`` and clear the global if it is still this recorder.

    Never raises: a failed flush is logged, and the global is cleared only
    when it still holds ``recorder`` (never a newer run's).
    """
    if recorder is None:
        return
    try:
        recorder.stop()
    except Exception as exc:  # noqa: BLE001 — teardown never hides the run's own error
        _log.warning("Stopping the trace recorder failed: %s", _describe(exc))
    finally:
        clear_recorder(recorder)
