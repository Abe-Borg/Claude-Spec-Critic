"""Dependency-free data models for the drawing subsystem.

Kept separate from :mod:`render` so consumers (:mod:`digest`, :mod:`pipeline`)
can reference these shapes without transitively importing the PyMuPDF backend.
Only :mod:`render` produces these; everything else just consumes them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SheetRef:
    """Identifies one sheet: a single page within a source PDF."""

    pdf_path: Path
    page_index: int          # zero-based
    source_name: str         # pdf_path.name, for display / provenance
    page_count: int          # pages in the source PDF

    @property
    def display_label(self) -> str:
        return f"{self.source_name} (page {self.page_index + 1}/{self.page_count})"


@dataclass
class ImageTile:
    """A rendered PNG image: either the whole-sheet overview or one grid tile."""

    png_bytes: bytes
    width_px: int
    height_px: int
    kind: str                # "overview" or "tile"
    row: int = -1            # grid row (tiles only; -1 for overview)
    col: int = -1            # grid col (tiles only; -1 for overview)
    label: str = ""          # human placement description (tiles only)


@dataclass
class RenderedSheet:
    """A sheet rendered to an overview image plus a grid of tile images."""

    ref: SheetRef
    overview: ImageTile
    tiles: list[ImageTile]
    page_width_pt: float
    page_height_pt: float
    rows: int
    cols: int

    @property
    def image_sizes(self) -> list[tuple[int, int]]:
        """``(width, height)`` for every image (overview + tiles), for token est."""
        sizes = [(self.overview.width_px, self.overview.height_px)]
        sizes.extend((t.width_px, t.height_px) for t in self.tiles)
        return sizes
