"""Haiku 5.5 rates apply per prompt, including its cached input."""
from __future__ import annotations

import pytest

from src.core.attempt_usage import AttemptUsage, OPERATION_TRIAGE, TRANSPORT_REALTIME
from src.core.pricing import estimate_cost_breakdown, estimate_request_cost, price_for
from src.orchestration.diagnostics import DiagnosticsReport

HAIKU = "claude-haiku-5-5"


@pytest.mark.parametrize("prompt_tokens,rate", [(99_999, 0.10), (100_000, 0.10), (100_001, 0.50)])
@pytest.mark.parametrize("batch", [False, True])
def test_tier_boundary_prices_entire_request(prompt_tokens, rate, batch):
    price = price_for(HAIKU, prompt_tokens=prompt_tokens)
    assert price.input_per_mtok == rate
    assert price.output_per_mtok == rate * 5
    assert estimate_request_cost(prompt_tokens, 2_000, model=HAIKU, batch=batch) == pytest.approx(
        (prompt_tokens * rate + 2_000 * rate * 5) / 1_000_000 * (0.5 if batch else 1)
    )


def test_output_length_does_not_select_input_price_tier():
    assert estimate_request_cost(1_000, 128_000, model=HAIKU) == pytest.approx(0.0641)


def test_tier_resolution_preserves_base_metadata_and_legacy_prices():
    base = price_for(HAIKU)
    assert base.label == "Haiku 5.5"
    assert base.long_context_threshold == 100_000
    high = price_for(HAIKU, prompt_tokens=100_001)
    assert high.cache_read_rate_per_mtok == pytest.approx(0.05)
    assert price_for(HAIKU).input_per_mtok == 0.10  # never mutates the table
    assert price_for(HAIKU + "-test", prompt_tokens=100_001) == high
    assert price_for("claude-haiku-5-50") is None
    assert price_for("unknown", prompt_tokens=100_001) is None
    legacy = price_for("claude-haiku-4-5")
    assert price_for("claude-haiku-4-5", prompt_tokens=200_000) is legacy


@pytest.mark.parametrize("cached_kind", ["read", "write", "components"])
@pytest.mark.parametrize("over_threshold", [False, True])
def test_cached_input_counts_toward_tier_without_double_counting(cached_kind, over_threshold):
    # 50k uncached + 50k cached fits the cheap tier exactly. Cache TTL
    # components partition their aggregate and must not be added a second time.
    cached = 50_000 + int(over_threshold)
    kwargs = {}
    if cached_kind == "read":
        kwargs["cache_read_input_tokens"] = cached
    else:
        kwargs["cache_creation_5m_input_tokens"] = 20_000
        kwargs["cache_creation_1h_input_tokens"] = cached - 20_000
        kwargs["cache_creation_unknown_input_tokens"] = 0
        if cached_kind == "write":
            kwargs["cache_creation_input_tokens"] = cached
    rate = 0.50 if over_threshold else 0.10
    cost = estimate_cost_breakdown(50_000, 1_000, model=HAIKU, **kwargs)
    assert cost.tokens == pytest.approx((50_000 * rate + 1_000 * rate * 5) / 1_000_000)
    if cached_kind == "read":
        assert cost.cache_reads == pytest.approx(cached * rate * 0.1 / 1_000_000)
        assert cost.cache_writes == 0
    else:
        assert cost.cache_writes == pytest.approx(
            (20_000 * rate * 1.25 + (cached - 20_000) * rate * 2) / 1_000_000
        )
        assert cost.cache_reads == 0


@pytest.mark.parametrize("batch", [False, True])
def test_long_context_cache_ttls_batch_discount_and_search_fees(batch):
    # A 110k prompt selects the high tier for every token line, including
    # cached reads/writes, while paid web searches keep their ordinary fee.
    cost = estimate_cost_breakdown(
        10_000, 2_000, model=HAIKU, batch=batch,
        cache_read_input_tokens=60_000,
        cache_creation_input_tokens=40_000,
        cache_creation_5m_input_tokens=10_000,
        cache_creation_1h_input_tokens=20_000,
        cache_creation_unknown_input_tokens=10_000,
        web_search_requests=3,
    )
    factor = 0.5 if batch else 1
    assert cost.tokens == pytest.approx(0.010 * factor)
    assert cost.cache_writes == pytest.approx(0.03625 * factor)
    assert cost.cache_reads == pytest.approx(0.003 * factor)
    assert cost.web_searches == pytest.approx(0.03)


def test_legacy_unsplit_cache_writes_use_total_prompt_length():
    cost = estimate_cost_breakdown(
        1, 1_000, model=HAIKU, cache_creation_input_tokens=100_000
    )
    assert cost.tokens == pytest.approx(0.0025005)
    assert cost.cache_writes == pytest.approx(0.10)


def test_diagnostics_selects_tier_per_attempt_not_run_or_conversation_total():
    attempts = [
        AttemptUsage(
            operation=OPERATION_TRIAGE, transport=TRANSPORT_REALTIME,
            model=HAIKU, input_tokens=60_000, output_tokens=1_000,
            message_id="msg_short_1",
        ),
        AttemptUsage(
            operation=OPERATION_TRIAGE, transport=TRANSPORT_REALTIME,
            model=HAIKU, input_tokens=60_000, output_tokens=1_000,
            message_id="msg_short_2",
        ),
        AttemptUsage(
            operation=OPERATION_TRIAGE, transport=TRANSPORT_REALTIME,
            model=HAIKU, input_tokens=1_000, output_tokens=1_000,
            cache_read_input_tokens=100_000, message_id="msg_long",
        ),
    ]
    report = DiagnosticsReport()
    report.record_api_call(
        phase="triage", model=HAIKU, mode="realtime", operation=OPERATION_TRIAGE,
        input_tokens=121_000, output_tokens=3_000, cache_read_input_tokens=100_000,
        attempts=attempts,
    )
    cost = report.summary()["cost_summary"]["estimated_cost_usd"]
    assert cost["priced_calls"] == 3
    assert cost["unpriced_calls"] == 0
    assert cost["tokens"] == pytest.approx(2 * 0.0065 + 0.003)
    assert cost["cache_reads"] == pytest.approx(0.005)
    assert cost["total"] == pytest.approx(0.021)
