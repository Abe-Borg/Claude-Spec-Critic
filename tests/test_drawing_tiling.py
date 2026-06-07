"""Tile-geometry tests for the drawing subsystem.

Pure geometry — no PyMuPDF, no network. Locks in the completeness invariant
(every region of the sheet lands in a tile) and the vision-cap-aware long-edge
target selection.
"""
from __future__ import annotations

import pytest

from src.drawings import tiling

# E-size sheet in PDF points (landscape 44"x34" * 72 pt/in).
E_W = 44 * 72
E_H = 34 * 72


def test_grid_count_is_rows_times_cols():
    rects = tiling.tile_rects(E_W, E_H, rows=6, cols=6)
    assert len(rects) == 36
    # every (row, col) appears exactly once
    coords = {(r.row, r.col) for r in rects}
    assert coords == {(r, c) for r in range(6) for c in range(6)}


def test_union_covers_whole_sheet():
    rects = tiling.tile_rects(E_W, E_H, rows=6, cols=6, overlap_frac=0.08)
    assert min(r.x0 for r in rects) == pytest.approx(0.0)
    assert min(r.y0 for r in rects) == pytest.approx(0.0)
    assert max(r.x1 for r in rects) == pytest.approx(E_W)
    assert max(r.y1 for r in rects) == pytest.approx(E_H)


def test_all_rects_within_page_bounds():
    rects = tiling.tile_rects(E_W, E_H, rows=6, cols=6, overlap_frac=0.08)
    for r in rects:
        assert 0.0 <= r.x0 < r.x1 <= E_W
        assert 0.0 <= r.y0 < r.y1 <= E_H


def test_zero_overlap_tiles_exactly():
    rects = tiling.tile_rects(E_W, E_H, rows=6, cols=6, overlap_frac=0.0)
    area = sum(r.width * r.height for r in rects)
    assert area == pytest.approx(E_W * E_H)


def test_overlap_increases_total_area_and_adjacent_tiles_overlap():
    rects = tiling.tile_rects(E_W, E_H, rows=6, cols=6, overlap_frac=0.08)
    area = sum(r.width * r.height for r in rects)
    # Overlap double-counts the shared bands, so total area exceeds the sheet.
    assert area > E_W * E_H
    by_pos = {(r.row, r.col): r for r in rects}
    left = by_pos[(2, 2)]
    right = by_pos[(2, 3)]
    # Horizontally adjacent interior tiles share an overlapping band.
    assert left.x1 > right.x0


def test_target_long_edge_respects_20_image_threshold():
    # <=20 images: full Opus long edge; >20 images: a margin under the hard cap.
    assert tiling.target_long_edge_px(10) == tiling.TARGET_LONG_EDGE_PX_FEW_IMAGES
    assert tiling.target_long_edge_px(20) == tiling.TARGET_LONG_EDGE_PX_FEW_IMAGES
    assert tiling.target_long_edge_px(21) == tiling.TARGET_LONG_EDGE_PX_MANY_IMAGES
    # A 6x6 sheet (36 tiles + overview = 37 images) lands in the clamped regime.
    assert tiling.total_images_for_grid(6, 6) == 37
    assert tiling.target_long_edge_px(37) == tiling.TARGET_LONG_EDGE_PX_MANY_IMAGES


def test_many_image_target_is_strictly_under_the_hard_cap():
    # The many-image target must sit BELOW the API's hard reject cap, so that
    # rasterizer rounding (which can round a tile UP by ~1 px) never produces an
    # image that exceeds the cap and gets the whole request rejected (HTTP 400).
    assert (
        tiling.TARGET_LONG_EDGE_PX_MANY_IMAGES
        < tiling.MANY_IMAGES_LONG_EDGE_CAP_PX
    )
    # The margin must comfortably exceed the proven <=1 px rounding overshoot.
    margin = tiling.MANY_IMAGES_LONG_EDGE_CAP_PX - tiling.TARGET_LONG_EDGE_PX_MANY_IMAGES
    assert margin >= 2


def _mupdf_round_pixels(origin_pt: float, extent_pt: float, zoom: float) -> int:
    """Pixel extent PyMuPDF produces for a clip edge, mirroring ``fz_round_rect``.

    MuPDF builds the pixmap's integer rectangle from the *transformed* clip by
    flooring the low corner and ceiling the high corner (with a 0.001 epsilon),
    so the rendered pixel count can exceed ``extent_pt * zoom`` by up to ~1 px
    when the edge doesn't fall on a whole-pixel boundary. This is the exact
    arithmetic that made every 2000 px-target tile render at 2001 px and get the
    drawing batch rejected.
    """
    import math

    lo = math.floor(origin_pt * zoom + 0.001)
    hi = math.ceil((origin_pt + extent_pt) * zoom - 0.001)
    return hi - lo


def test_rendered_tile_never_exceeds_hard_cap_under_rounding():
    # Regression for the 2000 px many-image rejection: simulate the rasterizer's
    # whole-pixel rounding across many sub-pixel tile origins and assert that NO
    # rendered edge exceeds the hard cap, for the real 6x6 grid on an E-size
    # sheet plus a near-square aspect ratio (the worst case for the short edge).
    cap = tiling.MANY_IMAGES_LONG_EDGE_CAP_PX
    target = tiling.target_long_edge_px(tiling.total_images_for_grid(6, 6))
    for page_w, page_h in [(E_W, E_H), (E_H, E_W), (3024.0, 3024.0), (2448.0, 3168.0)]:
        for rect in tiling.tile_rects(page_w, page_h, rows=6, cols=6, overlap_frac=0.08):
            zoom = tiling.zoom_for_rect(rect.width, rect.height, target)
            w_px = _mupdf_round_pixels(rect.x0, rect.width, zoom)
            h_px = _mupdf_round_pixels(rect.y0, rect.height, zoom)
            assert w_px <= cap, (page_w, page_h, rect.row, rect.col, w_px)
            assert h_px <= cap, (page_w, page_h, rect.row, rect.col, h_px)
        # The overview (whole page at the same target) must also stay under cap.
        zoom = tiling.zoom_for_rect(page_w, page_h, target)
        assert _mupdf_round_pixels(0.0, page_w, zoom) <= cap
        assert _mupdf_round_pixels(0.0, page_h, zoom) <= cap


def test_zoom_for_rect_hits_target_long_edge():
    assert tiling.zoom_for_rect(1000, 500, 2000) == pytest.approx(2.0)
    assert tiling.zoom_for_rect(500, 1000, 2000) == pytest.approx(2.0)


def test_invalid_grid_and_dimensions_raise():
    with pytest.raises(ValueError):
        tiling.tile_rects(E_W, E_H, rows=0, cols=6)
    with pytest.raises(ValueError):
        tiling.tile_rects(0, E_H, rows=6, cols=6)


def test_position_label_describes_placement():
    rects = tiling.tile_rects(E_W, E_H, rows=6, cols=6)
    label = tiling.position_label(rects[0], E_W, E_H)
    assert "across" in label and "down" in label
    # top-left tile reads as "upper-left"
    assert "upper" in label and "left" in label
