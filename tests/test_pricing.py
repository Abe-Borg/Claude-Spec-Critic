"""Model pricing tests. Hermetic — pure math."""
from __future__ import annotations

import pytest

from src.core.pricing import (
    BATCH_DISCOUNT,
    MODEL_PRICING,
    estimate_request_cost,
    friendly_model_name,
    price_for,
)

OPUS_5 = "claude-opus-5"
OPUS = "claude-opus-4-8"


def test_price_for_exact_and_unknown():
    assert price_for(OPUS_5) == MODEL_PRICING[OPUS_5]
    assert price_for(OPUS) == MODEL_PRICING[OPUS]
    assert price_for("claude-sonnet-4-6").input_per_mtok == 3.0
    assert price_for("claude-haiku-4-5").output_per_mtok == 5.0
    assert price_for("totally-made-up") is None
    assert price_for("") is None


def test_price_for_resolves_suffixed_variant():
    # Dated / fast variants (delimited by "-") resolve to the base model's price.
    assert price_for("claude-haiku-4-5-20251001") == MODEL_PRICING["claude-haiku-4-5"]
    assert price_for("claude-opus-4-8-fast") == MODEL_PRICING[OPUS]


def test_price_for_requires_delimiter_not_bare_prefix():
    # A different model that merely starts with a known id must NOT inherit its
    # price — only a "-"-delimited variant resolves (Codex P2).
    assert price_for("claude-opus-4-80") is None
    assert price_for("claude-opus-4-8x") is None


def test_opus_5_matches_opus_48_rates():
    # The Opus 4.8 -> Opus 5 upgrade is cost-neutral per token ($5/$25); the
    # About/Usage cost estimates must not shift on the model bump alone.
    assert price_for(OPUS_5).input_per_mtok == 5.0
    assert price_for(OPUS_5).output_per_mtok == 25.0
    assert price_for(OPUS_5).input_per_mtok == price_for(OPUS).input_per_mtok
    assert price_for(OPUS_5).output_per_mtok == price_for(OPUS).output_per_mtok


def test_review_default_model_is_priced():
    # The About / Usage dialogs resolve their label through price_for(), so an
    # unpriced review default would render the raw model id to the operator.
    from src.core.api_config import REVIEW_MODEL_DEFAULT

    assert price_for(REVIEW_MODEL_DEFAULT) is not None
    assert friendly_model_name(REVIEW_MODEL_DEFAULT) != REVIEW_MODEL_DEFAULT


def test_friendly_model_name():
    assert friendly_model_name(OPUS_5) == "Opus 5"
    assert friendly_model_name(OPUS) == "Opus 4.8"
    assert friendly_model_name("claude-sonnet-4-6") == "Sonnet 4.6"
    assert friendly_model_name("mystery") == "mystery"  # falls back to the id


def test_estimate_request_cost_opus():
    # 1M in + 1M out = $5 + $25 = $30.
    assert estimate_request_cost(1_000_000, 1_000_000, model=OPUS) == pytest.approx(30.0)
    # 200k in / 50k out = 0.2*5 + 0.05*25 = 1.0 + 1.25 = 2.25.
    assert estimate_request_cost(200_000, 50_000, model=OPUS) == pytest.approx(2.25)


def test_estimate_request_cost_batch_is_half():
    full = estimate_request_cost(1_000_000, 1_000_000, model=OPUS)
    batch = estimate_request_cost(1_000_000, 1_000_000, model=OPUS, batch=True)
    assert batch == pytest.approx(full * BATCH_DISCOUNT)
    assert batch == pytest.approx(15.0)


def test_estimate_request_cost_unknown_model_is_none():
    assert estimate_request_cost(1_000, 1_000, model="nope") is None
