"""B-16: the diagnostics cost summary prices cache writes, cache reads, and
web searches through the shared estimator (``core.pricing``) and surfaces the
line items in ``summary()`` and ``to_text()`` without changing existing keys.

Hermetic — pure arithmetic over recorded events.
"""
from __future__ import annotations

import pytest

from src.core.pricing import (
    BATCH_DISCOUNT,
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    WEB_SEARCH_USD_PER_1000,
)
from src.orchestration.diagnostics import DiagnosticsReport

OPUS = "claude-opus-4-8"  # $5 / $25 per MTok
LINE_ITEMS = ("tokens", "cache_writes", "cache_reads", "web_searches", "total")


def _cost(summary: dict) -> dict:
    return summary["cost_summary"]["estimated_cost_usd"]


def test_cost_lines_present_and_zero_on_an_empty_run():
    s = DiagnosticsReport().summary()
    est = _cost(s)
    assert {k: est[k] for k in LINE_ITEMS} == {k: 0.0 for k in LINE_ITEMS}
    assert est["priced_calls"] == 0 and est["unpriced_calls"] == 0
    # Existing keys are untouched by the addition.
    for key in (
        "total_input_tokens", "total_output_tokens",
        "total_cache_creation_input_tokens", "total_cache_read_input_tokens",
        "total_web_search_requests", "cache_hit_ratio", "phases",
    ):
        assert key in s["cost_summary"]
    # An empty run prints no cost line at all.
    assert "Est. Cost (USD)" not in DiagnosticsReport().to_text()


def test_line_items_price_tokens_cache_and_searches_separately():
    report = DiagnosticsReport()
    report.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        input_tokens=200_000, output_tokens=50_000,
        cache_creation_input_tokens=100_000, cache_read_input_tokens=400_000,
        web_search_requests=8,
    )
    est = _cost(report.summary())
    assert est["tokens"] == pytest.approx(2.25)
    assert est["cache_writes"] == pytest.approx(0.1 * 5 * CACHE_WRITE_1H_MULTIPLIER)
    assert est["cache_reads"] == pytest.approx(0.4 * 5 * CACHE_READ_MULTIPLIER)
    assert est["web_searches"] == pytest.approx(8 / 1000 * WEB_SEARCH_USD_PER_1000)
    assert est["total"] == pytest.approx(3.53)
    assert est["priced_calls"] == 1 and est["unpriced_calls"] == 0
    # The per-phase rollup carries the same line items for that phase.
    phase = report.summary()["phase_telemetry"]["verification"]["estimated_cost_usd"]
    assert phase["total"] == pytest.approx(3.53)
    assert phase["priced_calls"] == 1


def test_batch_calls_discount_tokens_and_cache_but_not_searches():
    report = DiagnosticsReport()
    report.record_api_call(
        phase="batch_collect", model=OPUS, mode="batch",
        input_tokens=200_000, output_tokens=50_000,
        cache_creation_input_tokens=100_000, cache_read_input_tokens=400_000,
        web_search_requests=8,
    )
    est = _cost(report.summary())
    assert est["tokens"] == pytest.approx(2.25 * BATCH_DISCOUNT)
    assert est["cache_writes"] == pytest.approx(1.0 * BATCH_DISCOUNT)
    assert est["cache_reads"] == pytest.approx(0.2 * BATCH_DISCOUNT)
    assert est["web_searches"] == pytest.approx(0.08)  # never discounted
    assert est["total"] == pytest.approx(1.125 + 0.5 + 0.1 + 0.08)


def test_calls_without_a_mode_are_priced_at_standard_rate():
    # A telemetry row that carries no transport tag is priced conservatively
    # (standard, not batch).
    report = DiagnosticsReport()
    report.record_api_call(phase="cross_check", model=OPUS, input_tokens=1_000_000)
    assert _cost(report.summary())["tokens"] == pytest.approx(5.0)


def test_unknown_model_counts_as_unpriced_not_zero_dollars():
    report = DiagnosticsReport()
    report.record_api_call(phase="triage", model="mystery-model", input_tokens=10, web_search_requests=2)
    report.record_api_call(phase="triage", model=OPUS, input_tokens=1_000_000)
    est = _cost(report.summary())
    assert est["priced_calls"] == 1
    assert est["unpriced_calls"] == 1
    assert est["total"] == pytest.approx(5.0)
    text = report.to_text()
    assert "Est. Cost (USD): $5.0000" in text
    assert "1 call(s) on an unpriced model id excluded" in text


def test_replayed_and_local_skip_verdicts_cost_nothing():
    # ``api_call=False`` marks a cache hit / local skip: it may carry the
    # replayed search count for its evidence panel but nothing was billed.
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "cache_status": "hit", "api_call": False,
        "model": OPUS, "web_search_requests": 5, "input_tokens": 0, "output_tokens": 0,
    })
    est = _cost(report.summary())
    assert est["total"] == 0.0 and est["priced_calls"] == 0 and est["unpriced_calls"] == 0


def test_shared_verdicts_are_not_priced_twice():
    report = DiagnosticsReport()
    base = {"verdict": "UNVERIFIED", "api_call": True, "model": OPUS,
            "web_search_requests": 4, "input_tokens": 100_000, "output_tokens": 0}
    report.log("verification", "info", "verdict", {**base, "cache_status": "miss"})
    report.log("verification", "info", "verdict", {**base, "cache_status": "shared"})
    est = _cost(report.summary())
    assert est["priced_calls"] == 1
    assert est["total"] == pytest.approx(0.5 + 0.04)


SONNET_46 = "claude-sonnet-4-6"  # $3 / $15 per MTok


def _escalated_verification_event(**overrides) -> dict:
    """The per-finding event the GUI emits for an escalated verification.

    The flat fields describe the KEPT (Opus) verdict's call; ``call_usage``
    lists both paid conversations, each on its own model.
    """
    event = {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS, "input_tokens": 1_000_000, "output_tokens": 0,
        "web_search_requests": 8, "escalation_attempted": True,
        "call_usage": [
            {"model": SONNET_46, "escalated": False, "input_tokens": 1_000_000,
             "output_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "web_search_requests": 5,
             "web_fetch_requests": 0},
            {"model": OPUS, "escalated": True, "input_tokens": 1_000_000,
             "output_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "web_search_requests": 8,
             "web_fetch_requests": 0},
        ],
    }
    event.update(overrides)
    return event


def test_escalated_verification_prices_both_calls_on_their_own_models():
    report = DiagnosticsReport()
    report.log("verification", "info", "Verified: a.docx — CONFIRMED",
               _escalated_verification_event())
    s = report.summary()
    est = _cost(s)
    # Sonnet 4.6 pass ($3 + 5 searches) + Opus pass ($5 + 8 searches). The
    # flat (kept-verdict) fields are NOT counted on top of call_usage —
    # that would be $13 plus 8 more searches.
    assert est["tokens"] == pytest.approx(3.0 + 5.0)
    assert est["web_searches"] == pytest.approx(13 / 1000 * WEB_SEARCH_USD_PER_1000)
    assert est["priced_calls"] == 2 and est["unpriced_calls"] == 0
    phase = s["phase_telemetry"]["verification"]
    assert phase["calls"] == 2
    assert phase["models"] == [SONNET_46, OPUS]
    assert phase["input_tokens"] == 2_000_000
    assert phase["web_search_requests"] == 13
    assert s["total_input_tokens"] == 2_000_000
    assert s["total_web_search_requests"] == 13


def test_call_usage_entry_without_a_model_prices_on_the_event_model():
    report = DiagnosticsReport()
    event = _escalated_verification_event()
    event["call_usage"][1]["model"] = ""
    report.log("verification", "info", "verdict", event)
    est = _cost(report.summary())
    assert est["priced_calls"] == 2
    assert est["tokens"] == pytest.approx(8.0)


def test_verification_event_cache_tokens_are_priced():
    # The per-finding event now carries the prompt-cache counters the
    # verifier reads from ``usage`` — previously always absent, so every
    # verification priced at zero cache spend.
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS, "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 100_000, "cache_read_input_tokens": 400_000,
        "web_search_requests": 0,
    })
    est = _cost(report.summary())
    assert est["cache_writes"] == pytest.approx(0.1 * 5 * CACHE_WRITE_1H_MULTIPLIER)
    assert est["cache_reads"] == pytest.approx(0.4 * 5 * CACHE_READ_MULTIPLIER)
    assert est["priced_calls"] == 1


def test_failed_call_with_no_usage_is_neither_priced_nor_unpriced():
    report = DiagnosticsReport()
    report.record_api_call(phase="review", model="mystery-model", stop_reason="error")
    est = _cost(report.summary())
    assert est["priced_calls"] == 0 and est["unpriced_calls"] == 0


def test_to_text_surfaces_the_four_line_items_and_per_phase_cost():
    report = DiagnosticsReport()
    report.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        input_tokens=200_000, output_tokens=50_000,
        cache_creation_input_tokens=100_000, cache_read_input_tokens=400_000,
        web_search_requests=8,
    )
    text = report.to_text()
    assert (
        "Est. Cost (USD): $3.5300  (tokens $2.2500, cache writes $1.0000, "
        "cache reads $0.2000, web searches $0.0800)"
    ) in text
    assert "cost=$3.5300" in text  # the phase-telemetry line


def test_money_values_are_rounded_for_stable_json():
    report = DiagnosticsReport()
    report.record_api_call(phase="review", model=OPUS, input_tokens=1, output_tokens=1)
    est = _cost(report.summary())
    for key in LINE_ITEMS:
        assert est[key] == round(est[key], 6)
