"""Owner binding for tkinter dialogs (``messagebox`` / ``filedialog`` / ``simpledialog``).

Every dialog under ``src/gui`` names its owning window with ``parent=`` so it
is transient for — centered over, and modal to — the app window or the
Toplevel that spawned it, rather than for whatever ``tkinter._default_root``
happens to be. ``tests/test_gui_dialog_ownership.py`` scans every controller
for a dialog call that forgot.

Two helpers keep that rule cheap to follow:

- :func:`owner_window` resolves the right owner for a flow that may run either
  against the main window or inside a Toplevel (the Project Context modal's
  "Attach Files…" reuses the inline flow against its own textbox).
- :func:`dialog_owner` returns the ``parent=`` keyword as a dict —
  ``{"parent": owner}`` for a live Tk widget, ``{}`` for a headless double —
  for the one controller whose dialogs are driven by hermetic tests with a
  ``SimpleNamespace`` app and fixed-arity dialog fakes
  (``review_run_controller``): ``messagebox.askyesno(title, text,
  **dialog_owner(app))`` binds the real window at runtime while a test fake
  that accepts only ``(title, message)`` keeps working. Everywhere else the
  literal ``parent=`` keyword is used.
"""
from __future__ import annotations

import tkinter as tk


def owner_window(app, widget=None):
    """The window a dialog should be owned by: ``widget``'s toplevel, else ``app``."""
    if widget is not None:
        try:
            return widget.winfo_toplevel()
        except Exception:  # noqa: BLE001 — a torn-down widget falls back to the app
            pass
    return app


def dialog_owner(owner) -> dict:
    """``{"parent": owner}`` for a live Tk widget; ``{}`` for a test double."""
    if isinstance(owner, tk.Misc):
        return {"parent": owner}
    return {}
