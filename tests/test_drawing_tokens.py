"""Image-token estimator tests.

Locks the estimator to Anthropic's published vision cost tables: tokens =
ceil(w*h/750) after resize to the model's native long edge, clamped to the
per-model token cap.
"""
from __future__ import annotations

from src.core.tokenizer import estimate_image_tokens, estimate_image_tokens_total

OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-4-6"


def test_opus_matches_published_cost_table():
    # Values from the Opus 4.7/4.8 vision cost table (within rounding).
    assert estimate_image_tokens(200, 200, model=OPUS) == 54
    assert estimate_image_tokens(1000, 1000, model=OPUS) == 1334
    assert estimate_image_tokens(1920, 1080, model=OPUS) == 2765
    assert estimate_image_tokens(2000, 1500, model=OPUS) == 4000


def test_opus_caps_at_4784():
    # An image far above native resolution is resized then clamped to the cap.
    assert estimate_image_tokens(8000, 8000, model=OPUS) == 4784


def test_sonnet_caps_at_1568():
    # Sonnet resizes to a 1568 px long edge and caps tokens at 1568.
    assert estimate_image_tokens(2000, 1500, model=SONNET) == 1568
    # Below native resolution, it uses the raw formula.
    assert estimate_image_tokens(1000, 1000, model=SONNET) == 1334


def test_unknown_model_uses_conservative_default_caps():
    # Unknown / None models fall back to the default (Sonnet-tier) caps.
    assert estimate_image_tokens(2000, 1500, model=None) == 1568
    assert estimate_image_tokens(2000, 1500, model="some-future-model") == 1568


def test_nonpositive_sizes_are_zero():
    assert estimate_image_tokens(0, 0, model=OPUS) == 0
    assert estimate_image_tokens(-5, 100, model=OPUS) == 0


def test_total_sums_each_image():
    sizes = [(1000, 1000), (200, 200)]
    assert estimate_image_tokens_total(sizes, model=OPUS) == 1334 + 54
