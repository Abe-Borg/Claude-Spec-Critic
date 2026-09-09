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
    CACHE_WRITE_5M_MULTIPLIER,
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


# ---------------------------------------------------------------------------
# Per-TTL cache-write accounting (CLAUDE.md, "Cache-write accounting")
#
# A five-minute cache write bills at 1.25x the input rate and a one-hour write
# at 2x. The app declares a one-hour TTL on its own breakpoints, but server
# tools insert their own five-minute breakpoint after tool results — which
# lands hardest in verification, the phase that runs the most searches. These
# pin that the split survives from the recorded event through to the dollars.
# ---------------------------------------------------------------------------


def test_five_minute_writes_are_priced_below_one_hour_writes():
    """The whole point: the same 100k tokens cost less at the five-minute
    rate, and the summary has to reflect which one the provider reported."""
    five_minute = DiagnosticsReport()
    five_minute.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        cache_creation_input_tokens=100_000,
        cache_creation_5m_input_tokens=100_000,
        cache_creation_1h_input_tokens=0,
        cache_creation_unknown_input_tokens=0,
        cache_creation_breakdown_status="complete",
    )
    one_hour = DiagnosticsReport()
    one_hour.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        cache_creation_input_tokens=100_000,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=100_000,
        cache_creation_unknown_input_tokens=0,
        cache_creation_breakdown_status="complete",
    )
    assert _cost(five_minute.summary())["cache_writes"] == pytest.approx(
        0.1 * 5 * CACHE_WRITE_5M_MULTIPLIER
    )
    assert _cost(one_hour.summary())["cache_writes"] == pytest.approx(
        0.1 * 5 * CACHE_WRITE_1H_MULTIPLIER
    )


def test_a_legacy_event_without_a_split_keeps_its_conservative_figure():
    """Historical numbers must not move. An event carrying only the aggregate
    is unknown-TTL and stays priced at 2x — the pre-change figure exactly."""
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS, "cache_creation_input_tokens": 100_000,
    })
    summary = report.summary()
    assert _cost(summary)["cache_writes"] == pytest.approx(
        0.1 * 5 * CACHE_WRITE_1H_MULTIPLIER
    )
    breakdown = summary["cost_summary"]["cache_write_breakdown"]
    assert breakdown["unknown_tokens"] == 100_000
    assert breakdown["status"] == "absent"


def test_the_aggregate_is_never_billed_alongside_its_own_components():
    """6,000 aggregate tokens split 1k/2k/3k cost what 1k/2k/3k cost — the
    single most damaging way to get this wrong is to charge both."""
    report = DiagnosticsReport()
    report.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        cache_creation_input_tokens=6_000,
        cache_creation_5m_input_tokens=1_000,
        cache_creation_1h_input_tokens=2_000,
        cache_creation_unknown_input_tokens=3_000,
        cache_creation_breakdown_status="partial",
    )
    expected = (
        (1_000 / 1_000_000) * 5 * CACHE_WRITE_5M_MULTIPLIER
        + (5_000 / 1_000_000) * 5 * CACHE_WRITE_1H_MULTIPLIER
    )
    assert _cost(report.summary())["cache_writes"] == pytest.approx(expected)


def test_run_totals_report_the_split_and_how_much_was_measured():
    """The accounting warning: a reader can tell what share of the write cost
    was measured rather than conservatively assumed."""
    report = DiagnosticsReport()
    report.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        cache_creation_input_tokens=1_000,
        cache_creation_5m_input_tokens=1_000,
        cache_creation_1h_input_tokens=0,
        cache_creation_unknown_input_tokens=0,
        cache_creation_breakdown_status="complete",
    )
    report.record_api_call(
        phase="review", model=OPUS, mode="batch",
        cache_creation_input_tokens=2_000,
    )
    summary = report.summary()
    breakdown = summary["cost_summary"]["cache_write_breakdown"]
    assert breakdown["5m_tokens"] == 1_000
    assert breakdown["unknown_tokens"] == 2_000
    assert breakdown["status"] == "partial"
    assert breakdown["status_counts"] == {"complete": 1, "absent": 1}
    # Additive: the aggregate total is unchanged and is NOT the sum of the
    # components plus itself.
    assert summary["total_cache_creation_input_tokens"] == 3_000


def test_phase_rollup_carries_the_split_per_phase():
    report = DiagnosticsReport()
    report.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        cache_creation_input_tokens=1_000,
        cache_creation_5m_input_tokens=1_000,
        cache_creation_1h_input_tokens=0,
        cache_creation_unknown_input_tokens=0,
        cache_creation_breakdown_status="complete",
    )
    report.record_api_call(
        phase="review", model=OPUS, mode="batch", cache_creation_input_tokens=2_000,
    )
    phases = report.summary()["cost_summary"]["phases"]
    assert phases["verification"]["cache_creation_5m_input_tokens"] == 1_000
    assert phases["verification"]["cache_creation_breakdown_status"] == "complete"
    assert phases["review"]["cache_creation_unknown_input_tokens"] == 2_000
    assert phases["review"]["cache_creation_breakdown_status"] == "absent"


def test_an_escalated_verification_prices_each_calls_own_ttl_split():
    """``call_usage`` is authoritative when present. Each paid conversation
    carries its own split and is priced on its own model AND its own TTL."""
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS,
        # Flat fields describe only the kept call and must NOT be counted
        # again on top of call_usage.
        "cache_creation_input_tokens": 999_999,
        "call_usage": [
            {
                "model": OPUS, "escalated": False,
                "cache_creation_input_tokens": 1_000,
                "cache_creation_5m_input_tokens": 1_000,
                "cache_creation_1h_input_tokens": 0,
                "cache_creation_unknown_input_tokens": 0,
                "cache_creation_breakdown_status": "complete",
            },
            {
                "model": OPUS, "escalated": True,
                "cache_creation_input_tokens": 1_000,
                "cache_creation_5m_input_tokens": 0,
                "cache_creation_1h_input_tokens": 1_000,
                "cache_creation_unknown_input_tokens": 0,
                "cache_creation_breakdown_status": "complete",
            },
        ],
    })
    expected = (
        (1_000 / 1_000_000) * 5 * CACHE_WRITE_5M_MULTIPLIER
        + (1_000 / 1_000_000) * 5 * CACHE_WRITE_1H_MULTIPLIER
    )
    summary = report.summary()
    assert _cost(summary)["cache_writes"] == pytest.approx(expected)
    assert summary["total_cache_creation_input_tokens"] == 2_000


def test_a_legacy_call_usage_entry_reads_as_unknown_not_as_a_zero_split():
    """A ``call_usage`` entry written before the split existed carries only
    the aggregate. It must price conservatively, not free."""
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS,
        "call_usage": [
            {"model": OPUS, "escalated": False, "cache_creation_input_tokens": 1_000},
        ],
    })
    assert _cost(report.summary())["cache_writes"] == pytest.approx(
        (1_000 / 1_000_000) * 5 * CACHE_WRITE_1H_MULTIPLIER
    )


def test_shared_verdicts_contribute_no_write_spend_or_breakdown():
    """A single-flight follower made no call; its cloned counters are zeroed,
    so it must add nothing to the run's write totals or its status histogram."""
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "cache_status": "shared", "model": OPUS,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "cache_creation_unknown_input_tokens": 0,
        "cache_creation_breakdown_status": "none",
    })
    summary = report.summary()
    assert summary["cost_summary"]["cache_write_breakdown"]["status_counts"] == {}
    assert _cost(summary)["cache_writes"] == 0.0


def test_to_text_reports_the_split_when_any_of_it_was_measured():
    report = DiagnosticsReport()
    report.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        cache_creation_input_tokens=1_000,
        cache_creation_5m_input_tokens=600,
        cache_creation_1h_input_tokens=0,
        cache_creation_unknown_input_tokens=400,
        cache_creation_breakdown_status="partial",
    )
    text = report.to_text()
    assert "Cache Writes:    5m=600  1h=0  unknown=400 (partial;" in text
    assert "conservative 1-hour rate" in text


def test_a_call_usage_entrys_accounting_warning_survives_into_the_summary():
    """``inconsistent`` is a statement about detail the extractor already
    discarded, so no counter can reconstruct it — the label has to be carried
    through ``_billable_calls`` explicitly or the warning is lost the moment a
    call is rolled up."""
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS,
        "call_usage": [
            {
                "model": OPUS, "escalated": False,
                "cache_creation_input_tokens": 1_000,
                "cache_creation_5m_input_tokens": 0,
                "cache_creation_1h_input_tokens": 0,
                "cache_creation_unknown_input_tokens": 1_000,
                "cache_creation_breakdown_status": "inconsistent",
            },
        ],
    })
    counts = report.summary()["cost_summary"]["cache_write_breakdown"]["status_counts"]
    assert counts == {"inconsistent": 1}


def test_a_partially_supplied_split_keeps_the_component_it_reported():
    """An aggregate plus half a split is read the way a provider block is
    read: the reported half stands and the remainder is unknown. Treating it
    as a carrier whose numbers fail to reconcile would throw away measured
    detail in order to price it conservatively."""
    report = DiagnosticsReport()
    report.record_api_call(
        phase="verification", model=OPUS, mode="realtime",
        cache_creation_input_tokens=1_000,
        cache_creation_5m_input_tokens=400,
    )
    event = report.events[-1].data
    assert event["cache_creation_5m_input_tokens"] == 400
    assert event["cache_creation_unknown_input_tokens"] == 600
    assert event["cache_creation_breakdown_status"] == "partial"
    expected = (
        (400 / 1_000_000) * 5 * CACHE_WRITE_5M_MULTIPLIER
        + (600 / 1_000_000) * 5 * CACHE_WRITE_1H_MULTIPLIER
    )
    assert _cost(report.summary())["cache_writes"] == pytest.approx(expected)


def test_not_supplied_and_explicitly_zero_are_different_facts():
    """``None`` means the caller reported no split (unknown-TTL, priced
    conservatively); an explicit zero alongside an explicit unknown is a
    complete accounting. Collapsing the two at the recorder would make every
    legacy call indistinguishable from one that measured a zero split."""
    not_supplied = DiagnosticsReport()
    not_supplied.record_api_call(
        phase="review", model=OPUS, cache_creation_input_tokens=1_000,
    )
    explicit = DiagnosticsReport()
    explicit.record_api_call(
        phase="review", model=OPUS,
        cache_creation_input_tokens=1_000,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=1_000,
        cache_creation_unknown_input_tokens=0,
        cache_creation_breakdown_status="complete",
    )
    assert not_supplied.events[-1].data["cache_creation_breakdown_status"] == "absent"
    assert explicit.events[-1].data["cache_creation_breakdown_status"] == "complete"
    assert not_supplied.events[-1].data["cache_creation_unknown_input_tokens"] == 1_000
    assert explicit.events[-1].data["cache_creation_unknown_input_tokens"] == 0


def test_the_status_histogram_counts_billed_calls_not_events():
    """An escalated verification is two paid conversations, each reporting its
    own TTL detail. Counting once on their merged status would report one call
    instead of two and label a complete-plus-absent pair "partial" — a claim
    about neither call. The histogram exists to say how many calls were
    measured, so it has to count calls."""
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS,
        "call_usage": [
            {
                "model": OPUS, "escalated": False,
                "cache_creation_input_tokens": 1_000,
                "cache_creation_5m_input_tokens": 1_000,
                "cache_creation_1h_input_tokens": 0,
                "cache_creation_unknown_input_tokens": 0,
                "cache_creation_breakdown_status": "complete",
            },
            # The escalated pass reported no split at all.
            {"model": OPUS, "escalated": True, "cache_creation_input_tokens": 500},
        ],
    })
    breakdown = report.summary()["cost_summary"]["cache_write_breakdown"]
    assert breakdown["status_counts"] == {"complete": 1, "absent": 1}
    # The token rollup is unaffected — it was already per-call correct.
    assert breakdown["5m_tokens"] == 1_000
    assert breakdown["unknown_tokens"] == 500


def test_two_complete_calls_in_one_event_count_as_two():
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS,
        "call_usage": [
            {
                "model": OPUS, "escalated": escalated,
                "cache_creation_input_tokens": 1_000,
                "cache_creation_5m_input_tokens": 1_000,
                "cache_creation_1h_input_tokens": 0,
                "cache_creation_unknown_input_tokens": 0,
                "cache_creation_breakdown_status": "complete",
            }
            for escalated in (False, True)
        ],
    })
    counts = report.summary()["cost_summary"]["cache_write_breakdown"]["status_counts"]
    assert counts == {"complete": 2}


def test_a_call_with_no_writes_is_not_counted_in_the_histogram():
    """Only calls that actually wrote to the cache belong in a histogram about
    how much write spend was measured."""
    report = DiagnosticsReport()
    report.log("verification", "info", "verdict", {
        "verdict": "CONFIRMED", "api_call": True, "call_mode": "realtime",
        "model": OPUS,
        "call_usage": [
            {"model": OPUS, "escalated": False, "cache_creation_input_tokens": 1_000},
            {"model": OPUS, "escalated": True, "input_tokens": 50},
        ],
    })
    counts = report.summary()["cost_summary"]["cache_write_breakdown"]["status_counts"]
    assert counts == {"absent": 1}
