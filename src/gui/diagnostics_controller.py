"""Diagnostics callbacks and window lifecycle.

Builds the log/progress callbacks the pipeline calls into during a run,
and owns the diagnostics window opening/closing. Stays GUI-thread-safe by
routing widget updates through ``app.after`` via the existing
``_dispatch_if_current`` helper.
"""
from __future__ import annotations

from .widgets import DiagnosticsWindow

_UI_LEVEL_MAP = {
    "info": "info",
    "success": "success",
    "warning": "warning",
    "error": "error",
    "step": "step",
    "muted": "muted",
    "debug": "muted",
}


def make_diag_log(app, phase: str, run_epoch: int):
    """Return a log callback that writes to both the EnhancedLog and the
    diagnostics report.

    Pipeline code passes explicit ``level`` and ``phase`` keywords; the
    constructed ``phase`` is used as the default. When a caller (e.g., the
    verifier path) supplies ``phase=``, it overrides on a per-call basis.
    """
    default_phase = phase

    def _log(msg: str, *, level: str = "info", phase: str | None = None, **_extra):
        ui_level = _UI_LEVEL_MAP.get(level, "info")
        app._dispatch_if_current(run_epoch, lambda m=msg, lv=ui_level: app.log.log(m, level=lv))
        if app._diagnostics_report:
            app._diagnostics_report.log(phase or default_phase, level, msg)

    return _log


def make_diag_progress(app, phase: str, run_epoch: int):
    """Return a progress callback that writes to both UI and diagnostics."""
    default_phase = phase

    def _on_progress(pct, msg, *, phase: str | None = None, **_extra):
        app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log_step(m))
        app._dispatch_if_current(run_epoch, lambda p=pct: app.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))
        if app._diagnostics_report:
            app._diagnostics_report.log(phase or default_phase, "step", msg, {"progress_pct": round(pct, 1)})

    return _on_progress


def finalize_diagnostics(app, phase: str, level: str, message: str) -> None:
    if app._diagnostics_report:
        app._diagnostics_report.log(phase, level, message)
        app._diagnostics_report.finish()
    app.diagnostics_button.configure(state="normal")


def open_diagnostics_window(app) -> None:
    if app._diagnostics_report is None:
        return
    if app._diagnostics_window is not None:
        try:
            app._diagnostics_window.destroy()
        except Exception:
            pass
    app._diagnostics_window = DiagnosticsWindow(app, report=app._diagnostics_report)
