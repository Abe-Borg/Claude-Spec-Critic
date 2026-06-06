"""Dependency-free tile geometry for splitting a drawing sheet into a grid.

This module deliberately imports nothing from PyMuPDF (or any image library).
It computes, in PDF coordinate space (points; 72 pt = 1 inch):

- the clip rectangles for an ``rows x cols`` grid of (optionally overlapping)
  tiles, plus
- the render zoom needed to land a given rectangle at a target pixel size.

``render.py`` consumes these to rasterize tiles. Keeping the geometry pure means
it can be unit-tested without a PDF engine, and the render backend can be
swapped without touching the tiling math.

Why these numbers
-----------------
Claude's vision path caps each image at a per-model token budget *and* a max
long-edge in pixels (Opus 4.8: 4784 tokens / 2576 px; other models: 1568 /
1568). Critically, sending **more than 20 images in one request** drops the
per-image dimension cap to 2000 px on the long edge. A 6x6 sheet is 36 tiles +
1 overview = 37 images, so the 2000 px cap applies — which is why the default
target long edge is 2000 px (~273 effective DPI on a 34"x44" E-size sheet, vs.
~49 DPI for the whole sheet sent as one image).
"""
from __future__ import annotations

from dataclasses import dataclass

# Default grid. A 6x6 split of an E-size sheet lands each tile near the vision
# cap at ~273 DPI (crisp for 3/32" note text). Overridable per call.
DEFAULT_GRID_ROWS = 6
DEFAULT_GRID_COLS = 6

# Fractional overlap added to each interior tile edge so a symbol / label that
# straddles a tile boundary still appears whole in at least one tile.
DEFAULT_OVERLAP_FRAC = 0.08

# Long-edge pixel targets, matching the vision dimension caps. When a request
# carries >20 images the API clamps each to 2000 px on the long edge; at <=20
# images Opus accepts up to 2576 px. Rendering to exactly the applicable cap
# maximizes legibility without paying for pixels the API would discard.
TARGET_LONG_EDGE_PX_MANY_IMAGES = 2000
TARGET_LONG_EDGE_PX_FEW_IMAGES = 2576

# The >20-images rule is a hard threshold in the vision docs.
MANY_IMAGES_THRESHOLD = 20


@dataclass(frozen=True)
class TileRect:
    """A single tile's clip rectangle in PDF points, with its grid position.

    ``row`` / ``col`` are zero-based. ``(x0, y0)`` is the top-left and
    ``(x1, y1)`` the bottom-right in PDF coordinate space.
    """

    row: int
    col: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def target_long_edge_px(total_images: int) -> int:
    """Pick the per-image long-edge target given how many images the request carries.

    >20 images forces the 2000 px dimension cap; otherwise Opus accepts 2576 px.
    """
    if total_images > MANY_IMAGES_THRESHOLD:
        return TARGET_LONG_EDGE_PX_MANY_IMAGES
    return TARGET_LONG_EDGE_PX_FEW_IMAGES


def total_images_for_grid(rows: int, cols: int) -> int:
    """Images in one sheet request: ``rows*cols`` tiles plus one overview."""
    return rows * cols + 1


def zoom_for_rect(rect_w_pt: float, rect_h_pt: float, target_px: int) -> float:
    """Render zoom (matrix scale) that lands the rect's long edge at ``target_px``.

    PyMuPDF pixel size = points * zoom, so ``zoom = target_px / longest_edge``.
    Computed per-rect so every tile lands at (or just under) the cap — edge
    tiles, which are slightly smaller after overlap clamping, simply render at a
    marginally higher DPI rather than overshooting the cap.
    """
    longest = max(rect_w_pt, rect_h_pt)
    if longest <= 0:
        return 1.0
    return target_px / longest


def tile_rects(
    page_width_pt: float,
    page_height_pt: float,
    *,
    rows: int = DEFAULT_GRID_ROWS,
    cols: int = DEFAULT_GRID_COLS,
    overlap_frac: float = DEFAULT_OVERLAP_FRAC,
) -> list[TileRect]:
    """Compute ``rows*cols`` overlapping clip rectangles covering the whole page.

    The base grid partitions the page exactly (cell = page / grid); each cell is
    then expanded outward by ``overlap_frac`` of the cell size on every side and
    clamped to the page bounds. Consequences guaranteed (and locked in by tests):

    - exactly ``rows*cols`` rectangles, one per ``(row, col)``;
    - their union is the entire page (the base grid already tiles it; overlap
      only adds), so no region of the sheet is dropped;
    - every rectangle lies within ``[0, W] x [0, H]``.
    """
    if rows < 1 or cols < 1:
        raise ValueError(f"grid must be at least 1x1, got {rows}x{cols}")
    if page_width_pt <= 0 or page_height_pt <= 0:
        raise ValueError(
            f"page dimensions must be positive, got {page_width_pt}x{page_height_pt}"
        )
    overlap_frac = max(0.0, overlap_frac)

    cell_w = page_width_pt / cols
    cell_h = page_height_pt / rows
    ox = cell_w * overlap_frac
    oy = cell_h * overlap_frac

    rects: list[TileRect] = []
    for r in range(rows):
        for c in range(cols):
            base_x0 = c * cell_w
            base_y0 = r * cell_h
            base_x1 = base_x0 + cell_w
            base_y1 = base_y0 + cell_h
            x0 = max(0.0, base_x0 - ox)
            y0 = max(0.0, base_y0 - oy)
            x1 = min(page_width_pt, base_x1 + ox)
            y1 = min(page_height_pt, base_y1 + oy)
            rects.append(TileRect(row=r, col=c, x0=x0, y0=y0, x1=x1, y1=y1))
    return rects


def position_label(rect: TileRect, page_width_pt: float, page_height_pt: float) -> str:
    """A short human-readable placement description for a tile.

    Used in the per-tile text label so the model can place each crop within the
    whole sheet (e.g. "upper-left; ~0-20% across, ~0-17% down"). The model is
    separately told to use the sheet's own grid bubbles / match-lines for
    cross-references — this is a coarse fallback frame.
    """
    cx = (rect.x0 + rect.x1) / 2.0
    cy = (rect.y0 + rect.y1) / 2.0
    horiz = "left" if cx < page_width_pt / 3 else ("right" if cx > 2 * page_width_pt / 3 else "center")
    vert = "upper" if cy < page_height_pt / 3 else ("lower" if cy > 2 * page_height_pt / 3 else "middle")
    quadrant = f"{vert}-{horiz}" if not (vert == "middle" and horiz == "center") else "center"

    def pct(v: float, total: float) -> int:
        return int(round(100.0 * v / total)) if total else 0

    across = f"~{pct(rect.x0, page_width_pt)}-{pct(rect.x1, page_width_pt)}% across"
    down = f"~{pct(rect.y0, page_height_pt)}-{pct(rect.y1, page_height_pt)}% down"
    return f"{quadrant}; {across}, {down}"
