"""Pure decision helper for the real-time review cost gate (tkinter-free).

Kept out of ``review_run_controller`` (which imports ``tkinter`` at module
scope) so the once-per-session + suppression decision can be unit-tested
hermetically, without a display or ``customtkinter``. Mirrors the
tkinter-free split already used by ``gui/context_attachment.py``.
"""
from __future__ import annotations

from ..core.ui_state import load_suppress_realtime_cost_warning

REALTIME_WORKER_TRADEOFF_TEXT = (
    "More workers usually finish sooner, but spend API budget faster and can "
    "increase throttling-related retry costs."
)


def should_warn_before_live_run(app, transport: str) -> bool:
    """Whether to show the real-time cost warning before a run starts.

    True only for a **real-time** run whose warning the operator has neither
    permanently dismissed (the persisted ``suppress_realtime_cost_warning``
    flag) nor already seen this session (``app._realtime_cost_warning_shown_this_session``).

    This closes the upgrade-path gap where a persisted real-time preference
    leaves the Options checkbox checked at startup, so the toggle handler
    never fires and its warning is skipped.
    """
    if transport != "realtime":
        return False
    if load_suppress_realtime_cost_warning():
        return False
    return not getattr(app, "_realtime_cost_warning_shown_this_session", False)
