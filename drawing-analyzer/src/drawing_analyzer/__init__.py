"""Drawing Analyzer — extract structured information from construction drawings.

A standalone vision pipeline that turns construction-drawing PDFs (mechanical /
plumbing / fire-protection sheets, etc.) into a structured *text* digest.

Design:

- **One sheet per request.** Each PDF page is a sheet; each sheet is rendered to
  an overview image plus a 6x6 grid of high-resolution tiles and sent to Claude
  Opus 4.8 in a *single* vision request, so the model sees the whole sheet at
  once (coherence beats the marginal resolution gain of splitting a sheet across
  calls).
- **No sheet limit.** Sheets are processed independently and their text digests
  are concatenated, so a set of any size builds up incrementally — the set never
  has to fit in one context window.
- **Vision -> text.** The output is plain structured text, ready to read, save,
  or feed to any downstream consumer.

Module layout::

    tiling.py    dependency-free tile geometry (clip rects + render zoom)
    render.py    PyMuPDF rasterization (the ONLY module that imports PyMuPDF)
    digest.py    per-sheet vision request -> structured text
    pipeline.py  orchestration: PDFs -> sheets -> digests -> combined text
    gui.py       standalone CustomTkinter window
    __main__.py  ``python -m drawing_analyzer`` launches the GUI
"""
from __future__ import annotations

from .digest import SheetDigest
from .pipeline import (
    DrawingContext,
    estimate_image_tokens_for_set,
    extract_drawing_context,
)

__version__ = "0.1.0"

__all__ = [
    "DrawingContext",
    "SheetDigest",
    "extract_drawing_context",
    "estimate_image_tokens_for_set",
    "__version__",
]
