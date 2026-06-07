"""PyMuPDF rasterization of drawing PDFs into overview + tile images.

This is the ONLY module in the codebase that imports PyMuPDF. Every other
drawing module works with the dependency-free geometry in :mod:`tiling` and the
in-memory :class:`RenderedSheet` / :class:`ImageTile` produced here, so the PDF
backend can be replaced (e.g. with pypdfium2 + Pillow) by rewriting this file
alone.

.. warning::
   PyMuPDF is licensed **AGPL-3.0**. If this application is distributed, review
   the licensing implications or swap this module for a permissively-licensed
   backend. All PyMuPDF usage is contained here precisely to make that swap a
   one-file change.

A PDF *page* is treated as one drawing *sheet* (the standard for construction
sets — one D/E-size sheet per page). A multi-sheet PDF therefore yields multiple
sheets, and several PDFs flatten into one ordered sheet list.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pymupdf  # AGPL-3.0 — see module docstring.

from . import tiling
from .models import ImageTile, RenderedSheet, SheetRef


def list_sheets(pdf_paths: list[Path]) -> list[SheetRef]:
    """Flatten ``pdf_paths`` into an ordered list of sheets (one per page).

    Cheap: opens each PDF only to read its page count. A PDF that cannot be
    opened is skipped (its error surfaces when rendering is attempted), so a
    bad file in a drop never blocks listing the rest.
    """
    refs: list[SheetRef] = []
    for path in pdf_paths:
        path = Path(path)
        try:
            doc = pymupdf.open(str(path))
        except Exception:
            continue
        try:
            count = doc.page_count
            for i in range(count):
                refs.append(
                    SheetRef(
                        pdf_path=path,
                        page_index=i,
                        source_name=path.name,
                        page_count=count,
                    )
                )
        finally:
            doc.close()
    return refs


def _render_clip(
    page: "pymupdf.Page",
    rect: "pymupdf.Rect",
    target_long_edge_px: int,
) -> tuple[bytes, int, int]:
    """Render a clip region of ``page`` to PNG bytes at the target long edge.

    Returns ``(png_bytes, width_px, height_px)``. RGB, no alpha (smaller PNGs and
    all the model needs). Rendering the clip directly (rather than the full page
    then cropping) keeps memory bounded and, for vector PDFs, rasterizes each
    region crisply from the source vectors.
    """
    zoom = tiling.zoom_for_rect(rect.width, rect.height, target_long_edge_px)
    matrix = pymupdf.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, clip=rect, alpha=False)
    return pix.tobytes("png"), pix.width, pix.height


def render_sheet(
    page: "pymupdf.Page",
    ref: SheetRef,
    *,
    rows: int = tiling.DEFAULT_GRID_ROWS,
    cols: int = tiling.DEFAULT_GRID_COLS,
    overlap_frac: float = tiling.DEFAULT_OVERLAP_FRAC,
) -> RenderedSheet:
    """Render one already-open page into an overview + ``rows*cols`` tiles."""
    page_rect = page.rect
    w_pt = float(page_rect.width)
    h_pt = float(page_rect.height)

    total_images = tiling.total_images_for_grid(rows, cols)
    target_px = tiling.target_long_edge_px(total_images)

    overview_png, ow, oh = _render_clip(page, page_rect, target_px)
    overview = ImageTile(
        png_bytes=overview_png, width_px=ow, height_px=oh, kind="overview"
    )

    tiles: list[ImageTile] = []
    for tr in tiling.tile_rects(
        w_pt, h_pt, rows=rows, cols=cols, overlap_frac=overlap_frac
    ):
        clip = pymupdf.Rect(tr.x0, tr.y0, tr.x1, tr.y1)
        png, tw, th = _render_clip(page, clip, target_px)
        tiles.append(
            ImageTile(
                png_bytes=png,
                width_px=tw,
                height_px=th,
                kind="tile",
                row=tr.row,
                col=tr.col,
                label=tiling.position_label(tr, w_pt, h_pt),
            )
        )

    return RenderedSheet(
        ref=ref,
        overview=overview,
        tiles=tiles,
        page_width_pt=w_pt,
        page_height_pt=h_pt,
        rows=rows,
        cols=cols,
    )


def iter_rendered_sheets(
    pdf_paths: list[Path],
    *,
    rows: int = tiling.DEFAULT_GRID_ROWS,
    cols: int = tiling.DEFAULT_GRID_COLS,
    overlap_frac: float = tiling.DEFAULT_OVERLAP_FRAC,
) -> Iterator[RenderedSheet]:
    """Yield a :class:`RenderedSheet` for every page across all ``pdf_paths``.

    Each PDF is opened once and its pages rendered in order, so the dominant
    cost (rasterization) streams sheet-by-sheet — the caller can digest each
    sheet as it arrives and report progress without holding the whole set in
    memory. A PDF that fails to open raises; callers that want best-effort
    behavior should pre-filter via :func:`list_sheets`.
    """
    for path in pdf_paths:
        path = Path(path)
        doc = pymupdf.open(str(path))
        try:
            count = doc.page_count
            for i in range(count):
                ref = SheetRef(
                    pdf_path=path,
                    page_index=i,
                    source_name=path.name,
                    page_count=count,
                )
                yield render_sheet(
                    doc[i], ref, rows=rows, cols=cols, overlap_frac=overlap_frac
                )
        finally:
            doc.close()
