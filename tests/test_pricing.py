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


# ---------------------------------------------------------------------------
# B-16 — cache writes / reads and web searches are priced line items
# ---------------------------------------------------------------------------

from src.core.pricing import (  # noqa: E402 - appended section
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    WEB_SEARCH_USD_PER_1000,
    CostBreakdown,
    estimate_cost_breakdown,
)


def test_pricing_constants_match_published_rates():
    # 1-hour cache write = 2x base input; cache read = 0.1x; $10 per 1,000
    # searches. Pinned so a rate drift is a deliberate edit, not an accident.
    assert CACHE_WRITE_1H_MULTIPLIER == 2.0
    assert CACHE_READ_MULTIPLIER == 0.1
    assert WEB_SEARCH_USD_PER_1000 == 10.0


def test_cache_write_priced_at_two_times_input_rate():
    # 1M cache-write tokens on Opus ($5 input) = $10.
    assert estimate_request_cost(
        0, 0, model=OPUS, cache_creation_input_tokens=1_000_000
    ) == pytest.approx(10.0)


def test_cache_read_priced_at_tenth_of_input_rate():
    # 1M cache-read tokens on Opus ($5 input) = $0.50.
    assert estimate_request_cost(
        0, 0, model=OPUS, cache_read_input_tokens=1_000_000
    ) == pytest.approx(0.5)


def test_web_searches_priced_per_thousand():
    # 1,000 searches = $10; 8 searches (one CRITICAL verification) = $0.08.
    assert estimate_request_cost(0, 0, model=OPUS, web_search_requests=1_000) == pytest.approx(10.0)
    assert estimate_request_cost(0, 0, model=OPUS, web_search_requests=8) == pytest.approx(0.08)


def test_combined_line_items_sum_exactly():
    # 200k in / 50k out / 100k cache write / 400k cache read / 8 searches on Opus:
    #   tokens       = 0.2*5 + 0.05*25       = 2.25
    #   cache writes = 0.1*5*2               = 1.00
    #   cache reads  = 0.4*5*0.1             = 0.20
    #   searches     = 8/1000*10             = 0.08
    b = estimate_cost_breakdown(
        200_000, 50_000, model=OPUS,
        cache_creation_input_tokens=100_000,
        cache_read_input_tokens=400_000,
        web_search_requests=8,
    )
    assert isinstance(b, CostBreakdown)
    assert b.tokens == pytest.approx(2.25)
    assert b.cache_writes == pytest.approx(1.0)
    assert b.cache_reads == pytest.approx(0.2)
    assert b.web_searches == pytest.approx(0.08)
    assert b.total == pytest.approx(3.53)
    assert b.as_dict() == {
        "tokens": b.tokens,
        "cache_writes": b.cache_writes,
        "cache_reads": b.cache_reads,
        "web_searches": b.web_searches,
        "total": b.total,
    }
    assert estimate_request_cost(
        200_000, 50_000, model=OPUS,
        cache_creation_input_tokens=100_000,
        cache_read_input_tokens=400_000,
        web_search_requests=8,
    ) == pytest.approx(3.53)


def test_batch_discount_applies_to_tokens_and_cache_but_never_to_searches():
    b = estimate_cost_breakdown(
        200_000, 50_000, model=OPUS, batch=True,
        cache_creation_input_tokens=100_000,
        cache_read_input_tokens=400_000,
        web_search_requests=8,
    )
    assert b.tokens == pytest.approx(2.25 * BATCH_DISCOUNT)
    assert b.cache_writes == pytest.approx(1.0 * BATCH_DISCOUNT)
    assert b.cache_reads == pytest.approx(0.2 * BATCH_DISCOUNT)
    # The Batches API charges searches at the same $10 / 1,000 rate.
    assert b.web_searches == pytest.approx(0.08)
    assert b.total == pytest.approx(1.125 + 0.5 + 0.1 + 0.08)
    # Sanity: a searches-only batch request costs exactly the standard rate.
    assert estimate_request_cost(0, 0, model=OPUS, batch=True, web_search_requests=1_000) == pytest.approx(10.0)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("model", [OPUS, OPUS_5, "claude-sonnet-5", "claude-haiku-4-5"])
def test_existing_callers_get_byte_identical_numbers(model: str, batch: bool):
    # The three new keywords default to zero, so a caller that only passes
    # input/output tokens must receive EXACTLY the pre-extension number — the
    # same expression, not merely an approximately equal one.
    price = price_for(model)
    factor = BATCH_DISCOUNT if batch else 1.0
    legacy = (
        (123_456 / 1_000_000) * price.input_per_mtok
        + (7_890 / 1_000_000) * price.output_per_mtok
    ) * factor
    assert estimate_request_cost(123_456, 7_890, model=model, batch=batch) == legacy


def test_breakdown_unknown_model_is_none():
    assert estimate_cost_breakdown(1, 1, model="nope", web_search_requests=5) is None
    assert estimate_request_cost(1, 1, model="nope", cache_read_input_tokens=5) is None
