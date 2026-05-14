"""GUI shell: main app, widgets, controllers, dialogs.

Re-exports ``main`` so ``from src.gui import main`` (used by main.py) keeps
working after the file move.
"""
from .gui import main

__all__ = ["main"]
