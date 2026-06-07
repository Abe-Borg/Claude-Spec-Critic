"""Pricing + drawing cost-estimate tests (Workstream 4). Hermetic — pure math."""
from __future__ import annotations

import pytest

from drawing_analyzer.core.pricing import (
    BATCH_DISCOUNT,
    MODEL_PRICING,
    estimate_request_cost,
    friendly_model_name,
    price_for,
)
from drawing_analyzer.cost import (
    _ASSUMED_OUTPUT_TOKENS_PER_SHEET as OUT_PER_SHEET,
    _ASSUMED_PROMPT_TOKENS_PER_SHEET as PROMPT_PER_SHEET,
    _ASSUMED_SYNTHESIS_OUTPUT_TOKENS as SYNTH_OUT,
    estimate_drawing_set_cost,
    format_drawing_cost_prompt,
)

OPUS = "claude-opus-4-8"


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #


def test_price_for_exact_and_unknown():
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


def test_friendly_model_name():
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


# --------------------------------------------------------------------------- #
# drawing-set estimate
# --------------------------------------------------------------------------- #


def test_drawing_estimate_no_synthesis_token_math():
    est = estimate_drawing_set_cost(10, file_count=2, model=OPUS, synthesize=False)
    assert est.sheet_count == 10 and est.file_count == 2
    assert est.image_tokens > 0
    assert est.input_tokens == est.image_tokens + 10 * PROMPT_PER_SHEET
    assert est.output_tokens == 10 * OUT_PER_SHEET
    assert est.total_cost == pytest.approx(
        estimate_request_cost(est.input_tokens, est.output_tokens, model=OPUS)
    )
    assert est.total_cost > 0


def test_drawing_estimate_synthesis_adds_a_pass():
    base = estimate_drawing_set_cost(10, model=OPUS, synthesize=False)
    synth = estimate_drawing_set_cost(10, model=OPUS, synthesize=True)
    # Synthesis re-reads the digests (10*OUT) + one prompt overhead as input,
    # and emits the overview as output.
    assert synth.output_tokens == base.output_tokens + SYNTH_OUT
    assert synth.input_tokens == base.input_tokens + 10 * OUT_PER_SHEET + PROMPT_PER_SHEET
    assert synth.total_cost > base.total_cost


def test_drawing_estimate_single_sheet_skips_synthesis():
    one = estimate_drawing_set_cost(1, model=OPUS, synthesize=True)
    assert one.output_tokens == 1 * OUT_PER_SHEET  # no synthesis component
    assert one.input_tokens == one.image_tokens + 1 * PROMPT_PER_SHEET


def test_drawing_estimate_unknown_model_keeps_scale_drops_cost():
    est = estimate_drawing_set_cost(5, model="mystery-model", synthesize=False)
    assert est.image_tokens > 0  # tokenizer still estimates image size
    assert est.total_cost is None  # but no dollar figure for an unpriced model


def test_format_prompt_includes_scale_cost_and_proceed():
    est = estimate_drawing_set_cost(8, file_count=3, model=OPUS)
    msg = format_drawing_cost_prompt(est)
    assert "8 drawing sheet(s)" in msg
    assert "from 3 file(s)" in msg
    assert "Opus 4.8" in msg
    assert "$" in msg
    assert "Proceed" in msg


def test_format_prompt_unknown_model_says_unavailable():
    est = estimate_drawing_set_cost(4, model="mystery-model")
    msg = format_drawing_cost_prompt(est)
    assert "unavailable" in msg
    assert "Proceed" in msg


def test_drawing_estimate_batch_halves_cost():
    full = estimate_drawing_set_cost(10, file_count=2, model=OPUS, batch=False)
    batch = estimate_drawing_set_cost(10, file_count=2, model=OPUS, batch=True)
    # Same token math; only the per-token rate is halved by the Batch discount.
    assert batch.input_tokens == full.input_tokens
    assert batch.output_tokens == full.output_tokens
    assert batch.batch is True and full.batch is False
    assert batch.total_cost == pytest.approx(full.total_cost * BATCH_DISCOUNT)


def test_format_prompt_batch_mode_notes_batch_and_latency():
    est = estimate_drawing_set_cost(8, file_count=3, model=OPUS, batch=True)
    msg = format_drawing_cost_prompt(est)
    assert "8 drawing sheet(s)" in msg
    assert "Batch" in msg  # names the batch submission + rate
    assert "Nothing is sent until you confirm" in msg
    assert "Proceed" in msg
