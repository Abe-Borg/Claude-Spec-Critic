"""Per-TTL prompt-cache-write accounting (CLAUDE.md, "Cache-write accounting").

**The defect this closes.** A five-minute prompt-cache write bills at 1.25x
the model's base input rate; a one-hour write bills at 2x. The app declares a
one-hour TTL on every breakpoint *it* sets, so pricing every write at 2x looked
safe — but server tools insert their **own** five-minute breakpoint after tool
results when the request already uses caching, and those writes concentrate in
verification, the phase that runs the most web searches. The result overstated
that phase's cost. Overstating is the safe direction to be wrong in, which is
why this is an accounting fix and not a billing bug; it is still wrong, and a
figure that silently rounds one way is a bad input to a cost decision.

**The invariant every test here defends** is
``known_5m + known_1h + unknown == aggregate``. Missing detail is *unknown*,
never zero: an unknown token is priced at the conservative 2x and *counted* as
unknown, so a reader can tell how much of a cost figure was measured rather
than assumed. The aggregate is never charged alongside its own components.

Module map: extraction and normalization live in ``src.core.api_config``;
pricing in ``src.core.pricing``; the run-level rollup in
``src.orchestration.diagnostics`` (covered further in
``test_diagnostics_cost_pricing.py``).
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace as NS

from src.core.api_config import (
    CACHE_BREAKDOWN_ABSENT,
    CACHE_BREAKDOWN_COMPLETE,
    CACHE_BREAKDOWN_INCONSISTENT,
    CACHE_BREAKDOWN_NONE,
    CACHE_BREAKDOWN_PARTIAL,
    CACHE_BREAKDOWN_STATUS_KEY,
    CACHE_USAGE_TOKEN_KEYS,
    apply_cache_usage,
    cache_pricing_kwargs,
    cache_usage_from,
    derive_cache_breakdown_status,
    empty_cache_usage,
    extract_cache_usage,
    merge_cache_usage,
)
from src.core.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    CACHE_WRITE_UNKNOWN_MULTIPLIER,
    estimate_cost_breakdown,
)

SONNET = "claude-sonnet-5"
SONNET_INPUT_PER_MTOK = 2.0


def _usage(aggregate=0, read=0, *, m5=None, h1=None, detail_as_dict=False):
    """A provider ``usage`` block. ``m5``/``h1`` at ``None`` means the field is
    absent, which is NOT the same as the provider reporting zero."""
    detail = None
    if m5 is not None or h1 is not None:
        fields = {}
        if m5 is not None:
            fields["ephemeral_5m_input_tokens"] = m5
        if h1 is not None:
            fields["ephemeral_1h_input_tokens"] = h1
        detail = fields if detail_as_dict else NS(**fields)
    return NS(
        cache_creation_input_tokens=aggregate,
        cache_read_input_tokens=read,
        cache_creation=detail,
    )


def _assert_invariant(usage: dict) -> None:
    components = (
        usage["cache_creation_5m_input_tokens"]
        + usage["cache_creation_1h_input_tokens"]
        + usage["cache_creation_unknown_input_tokens"]
    )
    assert components == usage["cache_creation_input_tokens"], usage


# ---------------------------------------------------------------------------
# 1. Extraction: what the provider says, and what it declines to say
# ---------------------------------------------------------------------------


class TestExtraction:
    def test_no_writes_is_none_not_a_zero_split(self):
        out = extract_cache_usage(_usage())
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_NONE
        _assert_invariant(out)

    def test_full_detail_is_complete(self):
        out = extract_cache_usage(_usage(1_000, 40, m5=400, h1=600))
        assert out["cache_creation_5m_input_tokens"] == 400
        assert out["cache_creation_1h_input_tokens"] == 600
        assert out["cache_creation_unknown_input_tokens"] == 0
        assert out["cache_read_input_tokens"] == 40
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_COMPLETE
        _assert_invariant(out)

    def test_no_detail_block_makes_every_token_unknown(self):
        """The legacy shape, and the one that must never read as free or as a
        complete split of zero."""
        out = extract_cache_usage(_usage(1_000, 0))
        assert out["cache_creation_unknown_input_tokens"] == 1_000
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_ABSENT
        _assert_invariant(out)

    def test_one_ttl_reported_alone_still_completes(self):
        """A provider that reports only the TTL that was actually written is
        giving complete detail, not partial."""
        out = extract_cache_usage(_usage(1_000, 0, h1=1_000))
        assert out["cache_creation_1h_input_tokens"] == 1_000
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_COMPLETE
        _assert_invariant(out)

    def test_explicit_zero_is_not_missing(self):
        """Both TTLs explicitly zero against a non-zero aggregate is detail
        that under-counts — ``partial``, with the shortfall carried as
        unknown rather than silently dropped."""
        out = extract_cache_usage(_usage(1_000, 0, m5=0, h1=0))
        assert out["cache_creation_unknown_input_tokens"] == 1_000
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_PARTIAL
        _assert_invariant(out)

    def test_detail_under_counting_the_aggregate_is_partial(self):
        out = extract_cache_usage(_usage(1_000, 0, m5=400, h1=100))
        assert out["cache_creation_5m_input_tokens"] == 400
        assert out["cache_creation_1h_input_tokens"] == 100
        assert out["cache_creation_unknown_input_tokens"] == 500
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_PARTIAL
        _assert_invariant(out)

    def test_detail_over_counting_the_aggregate_is_discarded(self):
        """Trust the aggregate — that is the billed number. Inventing 1,000
        tokens of spend the provider never charged for is worse than losing
        the breakdown."""
        out = extract_cache_usage(_usage(1_000, 0, m5=4_000, h1=6_000))
        assert out["cache_creation_input_tokens"] == 1_000
        assert out["cache_creation_unknown_input_tokens"] == 1_000
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_INCONSISTENT
        _assert_invariant(out)

    @pytest.mark.parametrize("bad", [-1, "many", None, object(), True, 1.5j])
    def test_untrustworthy_component_degrades_to_unknown_not_zero(self, bad):
        """A negative / non-numeric component is a signal the detail cannot be
        relied on. It must not read as zero, which would price those tokens as
        if the other TTL covered them. ``True`` is in the list deliberately:
        Python would coerce it to 1, and a boolean in a token field is a shape
        error, not a count of one."""
        out = extract_cache_usage(_usage(1_000, 0, m5=bad, h1=600))
        assert out["cache_creation_input_tokens"] == 1_000
        assert out["cache_creation_1h_input_tokens"] == 600
        assert out["cache_creation_unknown_input_tokens"] == 400
        _assert_invariant(out)

    def test_a_numeric_string_is_coerced_rather_than_discarded(self):
        """Deliberate: a count that round-tripped through JSON as a string is
        still a count. Only values that cannot be read as a non-negative
        integer are treated as untrustworthy."""
        out = extract_cache_usage(_usage(1_000, "40", m5="400", h1=600))
        assert out["cache_read_input_tokens"] == 40
        assert out["cache_creation_5m_input_tokens"] == 400
        assert out["cache_creation_1h_input_tokens"] == 600
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_COMPLETE
        _assert_invariant(out)

    def test_detail_that_is_entirely_untrustworthy_reads_as_absent(self):
        """A block whose only reported component is unusable offered no usable
        detail at all — ``absent``. Calling it ``partial`` would claim the
        provider gave a split that under-counts, when in fact it gave nothing
        we could read; the distinction is what the status exists to record."""
        out = extract_cache_usage(_usage(1_000, 0, m5=-1))
        assert out["cache_creation_unknown_input_tokens"] == 1_000
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_ABSENT
        _assert_invariant(out)

    def test_negative_aggregate_is_not_negative_spend(self):
        out = extract_cache_usage(_usage(-500, -20))
        assert out["cache_creation_input_tokens"] == 0
        assert out["cache_read_input_tokens"] == 0
        _assert_invariant(out)

    def test_missing_usage_block_is_zeroed(self):
        for absent in (None, NS(), {}):
            out = extract_cache_usage(absent)
            assert out == empty_cache_usage()

    def test_dict_shaped_usage_reads_the_same_as_an_sdk_object(self):
        """The batch retrieval path hands back plain dicts."""
        obj = extract_cache_usage(_usage(1_000, 40, m5=400, h1=600))
        as_dict = extract_cache_usage(
            {
                "cache_creation_input_tokens": 1_000,
                "cache_read_input_tokens": 40,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 400,
                    "ephemeral_1h_input_tokens": 600,
                },
            }
        )
        assert obj == as_dict


# ---------------------------------------------------------------------------
# 2. Carriers: reading a split back off a result object
# ---------------------------------------------------------------------------


class TestCarrierReading:
    def test_a_raw_usage_block_is_recognized_and_delegated(self):
        """``cache_usage_from`` serves carriers, but a raw provider block
        carries its split under ``cache_creation``. Reading the carrier field
        names off it would yield "aggregate N, split complete at zero" — the
        invariant broken AND every write priced at the wrong rate."""
        out = cache_usage_from(_usage(1_000, 0, m5=400, h1=600))
        assert out["cache_creation_5m_input_tokens"] == 400
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_COMPLETE
        _assert_invariant(out)

    def test_a_carrier_that_only_set_the_aggregate_reads_absent(self):
        """The ordinary legacy shape: dataclass defaults of 0 for the split.
        It must be ``absent`` (detail was never reported), not
        ``inconsistent`` (detail contradicts itself) — otherwise the
        accounting warning fires on every ordinary call and means nothing."""
        carrier = NS(
            cache_creation_input_tokens=3_000,
            cache_read_input_tokens=0,
            cache_creation_5m_input_tokens=0,
            cache_creation_1h_input_tokens=0,
            cache_creation_unknown_input_tokens=0,
            cache_creation_breakdown_status=CACHE_BREAKDOWN_NONE,
        )
        out = cache_usage_from(carrier)
        assert out["cache_creation_unknown_input_tokens"] == 3_000
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_ABSENT
        _assert_invariant(out)

    def test_a_carrier_whose_split_contradicts_its_own_status_is_flagged(self):
        carrier = NS(
            cache_creation_input_tokens=3_000,
            cache_read_input_tokens=0,
            cache_creation_5m_input_tokens=40,
            cache_creation_1h_input_tokens=60,
            cache_creation_unknown_input_tokens=0,
            cache_creation_breakdown_status=CACHE_BREAKDOWN_COMPLETE,
        )
        out = cache_usage_from(carrier)
        assert out["cache_creation_unknown_input_tokens"] == 3_000
        assert out[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_INCONSISTENT
        _assert_invariant(out)

    def test_apply_stamps_every_key_on_a_carrier(self):
        carrier = NS()
        applied = apply_cache_usage(carrier, _usage(1_000, 40, m5=400, h1=600))
        for key in CACHE_USAGE_TOKEN_KEYS:
            assert getattr(carrier, key) == applied[key]
        assert carrier.cache_creation_breakdown_status == CACHE_BREAKDOWN_COMPLETE

    def test_apply_accepts_an_already_extracted_dict_unchanged(self):
        extracted = extract_cache_usage(_usage(1_000, 0, m5=400, h1=600))
        carrier = NS()
        assert apply_cache_usage(carrier, extracted) == extracted
        assert carrier.cache_creation_5m_input_tokens == 400


# ---------------------------------------------------------------------------
# 3. Merging: the invariant has to survive summation
# ---------------------------------------------------------------------------


class TestMerging:
    def test_components_sum_and_the_invariant_holds(self):
        merged = merge_cache_usage(
            extract_cache_usage(_usage(1_000, 10, m5=400, h1=600)),
            extract_cache_usage(_usage(500, 5)),
            extract_cache_usage(_usage(200, 0, m5=200)),
        )
        assert merged["cache_creation_input_tokens"] == 1_700
        assert merged["cache_read_input_tokens"] == 15
        assert merged["cache_creation_5m_input_tokens"] == 600
        assert merged["cache_creation_1h_input_tokens"] == 600
        assert merged["cache_creation_unknown_input_tokens"] == 500
        _assert_invariant(merged)

    def test_complete_plus_absent_is_honestly_partial(self):
        merged = merge_cache_usage(
            extract_cache_usage(_usage(100, 0, m5=100)),
            extract_cache_usage(_usage(100, 0)),
        )
        assert merged[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_PARTIAL

    def test_inconsistent_is_sticky_across_a_merge(self):
        """An accounting warning raised on one call must stay visible in the
        total. No counter can reconstruct it, so it is carried explicitly."""
        merged = merge_cache_usage(
            extract_cache_usage(_usage(100, 0, m5=4_000, h1=6_000)),
            extract_cache_usage(_usage(100, 0, m5=100)),
        )
        assert merged[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_INCONSISTENT

    def test_merging_nothing_is_the_empty_shape(self):
        assert merge_cache_usage() == empty_cache_usage()

    def test_merge_is_order_independent(self):
        parts = [
            extract_cache_usage(_usage(100, 1, m5=100)),
            extract_cache_usage(_usage(200, 2)),
            extract_cache_usage(_usage(300, 3, h1=300)),
        ]
        assert merge_cache_usage(*parts) == merge_cache_usage(*reversed(parts))

    def test_merge_accepts_mixed_carriers_and_raw_usage_blocks(self):
        carrier = NS(
            cache_creation_input_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_5m_input_tokens=100,
            cache_creation_1h_input_tokens=0,
            cache_creation_unknown_input_tokens=0,
            cache_creation_breakdown_status=CACHE_BREAKDOWN_COMPLETE,
        )
        merged = merge_cache_usage(carrier, _usage(100, 0, h1=100))
        assert merged["cache_creation_5m_input_tokens"] == 100
        assert merged["cache_creation_1h_input_tokens"] == 100
        _assert_invariant(merged)


class TestStatusDerivation:
    @pytest.mark.parametrize(
        "aggregate,m5,h1,unknown,expected",
        [
            (0, 0, 0, 0, CACHE_BREAKDOWN_NONE),
            (100, 100, 0, 0, CACHE_BREAKDOWN_COMPLETE),
            (100, 0, 100, 0, CACHE_BREAKDOWN_COMPLETE),
            (100, 0, 0, 100, CACHE_BREAKDOWN_ABSENT),
            (100, 40, 0, 60, CACHE_BREAKDOWN_PARTIAL),
        ],
    )
    def test_status_follows_the_counters(self, aggregate, m5, h1, unknown, expected):
        assert (
            derive_cache_breakdown_status(
                aggregate=aggregate, known_5m=m5, known_1h=h1, unknown=unknown
            )
            == expected
        )


# ---------------------------------------------------------------------------
# 4. Pricing — the numerical acceptance table
# ---------------------------------------------------------------------------


def _write_cost(**cache) -> float:
    breakdown = estimate_cost_breakdown(0, 0, model=SONNET, **cache)
    assert breakdown is not None
    return breakdown.cache_writes


class TestNumericalAcceptance:
    """Sonnet 5 input at USD 2 per million tokens; cache-write cost only."""

    def test_1000_known_five_minute_tokens(self):
        assert _write_cost(
            cache_creation_input_tokens=1_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_unknown_input_tokens=0,
        ) == pytest.approx(0.0025)

    def test_1000_known_one_hour_tokens(self):
        assert _write_cost(
            cache_creation_input_tokens=1_000,
            cache_creation_1h_input_tokens=1_000,
            cache_creation_unknown_input_tokens=0,
        ) == pytest.approx(0.004)

    def test_1000_unknown_legacy_tokens_stay_conservative(self):
        """The legacy call shape — aggregate only, no split supplied — keeps
        its existing figure, so historical numbers do not move. The
        uncertainty becomes visible through the unknown count, not through a
        changed dollar amount."""
        assert _write_cost(cache_creation_input_tokens=1_000) == pytest.approx(0.004)

    def test_mixed_split(self):
        assert _write_cost(
            cache_creation_input_tokens=6_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_1h_input_tokens=2_000,
            cache_creation_unknown_input_tokens=3_000,
        ) == pytest.approx(0.0225)

    def test_mixed_split_on_batch_transport_is_halved(self):
        breakdown = estimate_cost_breakdown(
            0,
            0,
            model=SONNET,
            batch=True,
            cache_creation_input_tokens=6_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_1h_input_tokens=2_000,
            cache_creation_unknown_input_tokens=3_000,
        )
        assert breakdown is not None
        assert breakdown.cache_writes == pytest.approx(0.01125)

    def test_the_aggregate_is_never_charged_alongside_its_components(self):
        """6,000 aggregate tokens split 1k/2k/3k must cost what 1k/2k/3k
        costs — not that plus another 6,000 tokens of write."""
        split = _write_cost(
            cache_creation_input_tokens=6_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_1h_input_tokens=2_000,
            cache_creation_unknown_input_tokens=3_000,
        )
        components_only = _write_cost(
            cache_creation_input_tokens=0,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_1h_input_tokens=2_000,
            cache_creation_unknown_input_tokens=3_000,
        )
        assert split == pytest.approx(components_only)


class TestIncompleteBreakdownIsNotDoubleCharged:
    """A caller that breaks out one TTL but omits the unknown count must not
    pay for those tokens twice — once at their own rate, and again inside the
    aggregate. The default for ``cache_creation_unknown_input_tokens`` is the
    *remainder*, not the whole aggregate."""

    def test_a_supplied_component_is_not_also_charged_as_unknown(self):
        assert _write_cost(
            cache_creation_input_tokens=1_000,
            cache_creation_5m_input_tokens=1_000,
        ) == pytest.approx(0.0025)

    def test_a_half_supplied_split_charges_the_remainder_as_unknown(self):
        # 400 at 1.25x + 600 at 2x on USD 2/MTok.
        assert _write_cost(
            cache_creation_input_tokens=1_000,
            cache_creation_5m_input_tokens=400,
        ) == pytest.approx(0.001 + 0.0024)

    def test_supplying_no_component_still_prices_the_whole_aggregate(self):
        """The legacy path falls out of the same expression: with both
        components at their zero default the remainder *is* the aggregate."""
        assert _write_cost(
            cache_creation_input_tokens=1_000
        ) == pytest.approx(0.004)

    def test_components_exceeding_the_aggregate_price_only_what_was_declared(self):
        """Clamped at zero rather than going negative and crediting spend."""
        assert _write_cost(
            cache_creation_input_tokens=100,
            cache_creation_1h_input_tokens=1_000,
        ) == pytest.approx(0.004)

    def test_the_request_cost_wrapper_applies_the_same_rule(self):
        from src.core.pricing import estimate_request_cost

        assert estimate_request_cost(
            0, 0, model=SONNET,
            cache_creation_input_tokens=1_000,
            cache_creation_5m_input_tokens=1_000,
        ) == pytest.approx(0.0025)


class TestPricingContract:
    def test_multipliers_match_the_published_rates(self):
        assert CACHE_WRITE_5M_MULTIPLIER == 1.25
        assert CACHE_WRITE_1H_MULTIPLIER == 2.0
        assert CACHE_READ_MULTIPLIER == 0.1

    def test_unknown_ttl_is_priced_at_the_conservative_one_hour_rate(self):
        """Deliberate, not incidental: under-stating a bill on missing data is
        the failure that surprises someone."""
        assert CACHE_WRITE_UNKNOWN_MULTIPLIER == CACHE_WRITE_1H_MULTIPLIER

    def test_a_five_minute_write_costs_less_than_a_one_hour_write(self):
        cheap = _write_cost(
            cache_creation_input_tokens=1_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_unknown_input_tokens=0,
        )
        dear = _write_cost(
            cache_creation_input_tokens=1_000,
            cache_creation_1h_input_tokens=1_000,
            cache_creation_unknown_input_tokens=0,
        )
        assert cheap < dear

    def test_missing_usage_is_unavailable_not_free_for_an_unknown_model(self):
        assert (
            estimate_cost_breakdown(
                0,
                0,
                model="some-model-that-does-not-exist",
                cache_creation_input_tokens=1_000,
                cache_creation_5m_input_tokens=1_000,
                cache_creation_unknown_input_tokens=0,
            )
            is None
        )

    def test_searches_are_priced_alongside_the_split_and_never_discounted(self):
        standard = estimate_cost_breakdown(
            0, 0, model=SONNET, web_search_requests=10,
            cache_creation_input_tokens=1_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_unknown_input_tokens=0,
        )
        batched = estimate_cost_breakdown(
            0, 0, model=SONNET, batch=True, web_search_requests=10,
            cache_creation_input_tokens=1_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_unknown_input_tokens=0,
        )
        assert standard is not None and batched is not None
        assert standard.web_searches == batched.web_searches == pytest.approx(0.10)
        assert batched.cache_writes == pytest.approx(standard.cache_writes / 2)

    def test_legacy_callers_are_byte_identical(self):
        """Passing no split at all must reproduce the pre-breakdown number for
        every mix of the other line items — the guarantee that this change
        moves no historical figure it should not move."""
        legacy = estimate_cost_breakdown(
            10_000, 2_000, model=SONNET,
            cache_creation_input_tokens=5_000,
            cache_read_input_tokens=50_000,
            web_search_requests=3,
        )
        assert legacy is not None
        assert legacy.cache_writes == pytest.approx(
            (5_000 / 1_000_000) * SONNET_INPUT_PER_MTOK * CACHE_WRITE_1H_MULTIPLIER
        )

    def test_pricing_kwargs_carry_every_token_key_and_no_status(self):
        kwargs = cache_pricing_kwargs(
            extract_cache_usage(_usage(1_000, 40, m5=400, h1=600))
        )
        assert set(kwargs) == set(CACHE_USAGE_TOKEN_KEYS)
        assert CACHE_BREAKDOWN_STATUS_KEY not in kwargs
        # It splats straight into the estimator — the point of the helper.
        assert estimate_cost_breakdown(0, 0, model=SONNET, **kwargs) is not None


# ---------------------------------------------------------------------------
# 5. The review phase's own spend reaches the combined result
#
# The batch review is the app's largest cached prefix, so its cache-write and
# cache-read tokens are the biggest single line item the split exists to price
# correctly. They travel to diagnostics on exactly one carrier — the combined
# ``ReviewResult`` that ``collect_review_batch_results`` builds — and that
# carrier previously accumulated only input/output tokens.
# ---------------------------------------------------------------------------


def _review_submission(request_ids):
    import time as _time

    from src.batch.batch import BatchJob
    from src.orchestration.pipeline import BatchSubmission

    job = BatchJob(
        batch_id="batch-1",
        job_type="review",
        request_map={
            rid: {"filename": f"{rid}.docx", "index": i, "type": "review"}
            for i, rid in enumerate(request_ids)
        },
        created_at=_time.time(),
    )
    return BatchSubmission(
        job=job,
        files_reviewed=[f"{rid}.docx" for rid in request_ids],
        review_request_ids=list(request_ids),
        model="claude-opus-4-8",
        prepared_specs=None,
    )


def _review_result(**cache):
    from src.review.reviewer import ReviewResult

    return ReviewResult(
        findings=[], parse_status="ok", input_tokens=100, output_tokens=50, **cache
    )


class TestReviewPhaseSpendReachesTheCombinedResult:
    def test_cache_usage_is_summed_across_specs_with_the_split_intact(
        self, monkeypatch
    ):
        import src.orchestration.pipeline as pl
        from src.orchestration.pipeline import collect_review_batch_results

        results = {
            "a": _review_result(
                cache_creation_input_tokens=1_000,
                cache_read_input_tokens=200,
                cache_creation_5m_input_tokens=400,
                cache_creation_1h_input_tokens=600,
                cache_creation_unknown_input_tokens=0,
                cache_creation_breakdown_status=CACHE_BREAKDOWN_COMPLETE,
            ),
            # The second spec's provider reported no per-TTL detail.
            "b": _review_result(
                cache_creation_input_tokens=500, cache_read_input_tokens=100
            ),
        }
        monkeypatch.setattr(
            pl, "retrieve_review_results", lambda job, *, model: results
        )

        combined = collect_review_batch_results(
            _review_submission(["a", "b"])
        ).review_result

        assert combined.cache_creation_input_tokens == 1_500
        assert combined.cache_read_input_tokens == 300
        assert combined.cache_creation_5m_input_tokens == 400
        assert combined.cache_creation_1h_input_tokens == 600
        assert combined.cache_creation_unknown_input_tokens == 500
        assert combined.cache_creation_breakdown_status == CACHE_BREAKDOWN_PARTIAL
        _assert_invariant(cache_usage_from(combined))

    def test_a_failed_review_still_reports_the_tokens_it_was_billed_for(
        self, monkeypatch
    ):
        """A truncated review is the most expensive kind of failure — it ran
        the output cap to the limit. Skipping its usage made the review phase
        look cheaper the worse it went."""
        import src.orchestration.pipeline as pl
        from src.orchestration.pipeline import collect_review_batch_results

        truncated = _review_result(
            cache_creation_input_tokens=800,
            cache_creation_1h_input_tokens=800,
            cache_creation_unknown_input_tokens=0,
            cache_creation_breakdown_status=CACHE_BREAKDOWN_COMPLETE,
        )
        truncated.parse_status = "incomplete"
        truncated.stop_reason = "max_tokens"
        truncated.output_tokens = 128_000
        monkeypatch.setattr(
            pl, "retrieve_review_results", lambda job, *, model: {"a": truncated}
        )

        state = collect_review_batch_results(_review_submission(["a"]))

        # Still surfaced as a failed review...
        assert state.truncated_specs == ["a.docx"]
        # ...and still counted as the spend it was.
        assert state.review_result.output_tokens == 128_000
        assert state.review_result.cache_creation_input_tokens == 800
        assert state.review_result.cache_creation_1h_input_tokens == 800

    def test_a_spec_with_no_result_contributes_nothing(self, monkeypatch):
        import src.orchestration.pipeline as pl
        from src.orchestration.pipeline import collect_review_batch_results

        monkeypatch.setattr(pl, "retrieve_review_results", lambda job, *, model: {})

        combined = collect_review_batch_results(
            _review_submission(["a"])
        ).review_result

        assert combined.cache_creation_input_tokens == 0
        assert combined.input_tokens == 0
        assert combined.cache_creation_breakdown_status == CACHE_BREAKDOWN_NONE
