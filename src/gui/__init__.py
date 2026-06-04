"""GUI shell: main app, widgets, controllers, dialogs.

Re-exports ``main`` so ``from src.gui import main`` keeps working. The
re-export is **lazy** (PEP 562 ``__getattr__``) so importing a lightweight
submodule — e.g. ``src.gui.file_selection_controller`` — does not pull in
``customtkinter`` / ``tkinter`` transitively. That keeps such submodules'
pure helpers importable in headless / hermetic test environments without
the system Tk package installed. ``main.py`` imports ``main`` directly from
``src.gui.gui`` and is unaffected either way.
"""
from __future__ import annotations

__all__ = ["main"]


def __getattr__(name: str):
    if name == "main":
        from .gui import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
