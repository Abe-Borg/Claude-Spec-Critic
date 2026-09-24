"""Every paid attempt is counted exactly once (plan WP-15).

The cost summary used to be priced from whatever result a pass kept, so every
attempt the pass abandoned vanished from it: a review repair replaced the
truncated primary's usage (usually the largest line of the run, since a
truncation spends the whole 128k output cap), a verification retry dropped
the paused turns it abandoned, a real-time fallback dropped the paid batch
waves before it and was priced at the batch discount, a cross-check parse
retry kept only the last response, and a request that raised was recorded as
zero tokens — as if measured. Recording the same batch twice into one report
counted it twice, a resumed run could not tell the batch it re-read (billed
earlier) from its own spend, a pending repair's usage was simply missing, and
a long run evicted its earliest spend from the estimate along with the old
events that carried it. Haiku triage calls were not recorded at all.

``core.attempt_usage`` now gives every paid request one record — operation,
role, transport, model, usage (known, unknown, or a known zero), identity,
and scope — and diagnostics prices those records, once each. These tests
drive the real collection, verification, triage, and diagnostics code with
every remote boundary faked, and check each acceptance criterion of WP-15:

* primary plus repair cost equals the sum of both attempts, even when the
  repair replaced the primary's findings;
* failed parses with usage stay billable;
* batch discount, cache TTL categories, reads, and search fees each apply
  once;
* repeated collection of one attempt does not duplicate it;
* real-time totals are unchanged for an equivalent scenario;
* shared followers cost nothing extra;
* legacy records stay readable and are visibly limited.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import src.verification.triage as triage
import src.verification.verifier as V
from src.batch.batch import _review_custom_id
from src.core.api_config import CACHE_BREAKDOWN_INCONSISTENT
from src.core.attempt_usage import (
    CATEGORY_LABELS,
    OPERATION_REVIEW,
    OPERATION_TRIAGE,
    OPERATION_VERIFICATION,
    ROLE_ESCALATION,
    ROLE_FALLBACK,
    ROLE_PRIMARY,
    ROLE_REPAIR,
    ROLE_RETRY,
    SCOPE_EARLIER,
    SCOPE_RUN,
    TRANSPORT_BATCH,
    TRANSPORT_REALTIME,
    AttemptUsage,
    attempts_from,
    known_attempt,
    known_totals,
    normalize_transport,
    operation_for_phase,
    spend_category,
    unknown_attempt,
)
from src.core.code_cycles import DEFAULT_CYCLE
from src.core.pricing import estimate_cost_breakdown
from src.orchestration import pipeline as pl
from src.orchestration.diagnostics import (
    ESTIMATE_NOTE,
    DiagnosticsReport,
    cost_summary_lines,
    record_pass_api_call,
    record_verification_findings,
    triage_usage_sink,
)
from src.review import realtime_review as rt
from src.review.reviewer import Finding, ReviewResult
from src.verification.verifier import (
    DEFAULT_VERIFICATION_POLL_POLICY,
    VerificationResult,
    collect_verification_batch_results,
)
from tests.fixtures import batch_service as svc
from tests.fixtures import verification_drivers as vd
from tests.fixtures.fake_anthropic import (
    FakeBatchResult,
    FakeBatchResultEnvelope,
    FakeCacheCreation,
    FakeMessage,
    FakeServerToolUsage,
    FakeTextBlock,
    FakeToolUseBlock,
    FakeUsage,
    batch_verification_result,
)

REPO = Path(__file__).resolve().parents[1]
OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"


def price(input_tokens, output_tokens, *, model=OPUS, batch=True, **extra) -> float:
    return estimate_cost_breakdown(
        input_tokens, output_tokens, model=model, batch=batch, **extra
    ).total


def cost(diag: DiagnosticsReport) -> dict:
    return diag.summary()["cost_summary"]


def total(diag: DiagnosticsReport) -> float:
    return cost(diag)["estimated_cost_usd"]["total"]


def rate_limit() -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError(
        "rate limited", response=httpx2.Response(429, request=request), body=None
    )


# ===========================================================================
# 1. The attempt record
# ===========================================================================


class TestAttemptRecord:
    def test_a_batch_item_is_identified_by_batch_custom_id_and_role(self):
        primary = unknown_attempt(
            operation=OPERATION_REVIEW, transport=TRANSPORT_BATCH,
            batch_id="b1", custom_id="review__A__0",
        )
        repair = unknown_attempt(
            operation=OPERATION_REVIEW, role=ROLE_REPAIR, transport=TRANSPORT_BATCH,
            batch_id="b1", custom_id="review__A__0",
        )
        assert primary.attempt_id == "batch:b1:review__A__0:primary"
        # A repair reusing the primary's custom id is still its own attempt.
        assert repair.attempt_id != primary.attempt_id

    def test_a_response_is_identified_by_its_message_id(self):
        attempt = known_attempt(
            {"input_tokens": 5}, operation=OPERATION_VERIFICATION,
            transport=TRANSPORT_REALTIME, message_id="msg_1",
        )
        assert attempt.attempt_id == "message:msg_1"

    def test_a_request_that_raised_has_no_identity(self):
        attempt = unknown_attempt(operation=OPERATION_REVIEW, transport=TRANSPORT_REALTIME)
        assert attempt.attempt_id == ""

    def test_unknown_usage_carries_no_counters(self):
        with pytest.raises(ValueError):
            AttemptUsage(operation=OPERATION_REVIEW, usage_known=False, input_tokens=1)

    @pytest.mark.parametrize(
        "field, value",
        [("role", "second_try"), ("transport", "stream"), ("scope", "later")],
    )
    def test_closed_vocabularies_are_enforced(self, field, value):
        with pytest.raises(ValueError):
            AttemptUsage(operation=OPERATION_REVIEW, **{field: value})

    def test_an_unlabelled_transport_is_priced_at_standard_rates(self):
        # Never grant a batch discount a request may not have had.
        assert normalize_transport(None) == TRANSPORT_REALTIME
        assert normalize_transport("Batch") == TRANSPORT_BATCH
        assert normalize_transport("something") == TRANSPORT_REALTIME

    def test_a_provider_usage_block_keeps_searches_and_the_ttl_split(self):
        usage = FakeUsage(
            input_tokens=100,
            output_tokens=40,
            cache_creation_input_tokens=3_000,
            cache_read_input_tokens=700,
            cache_creation=FakeCacheCreation(
                ephemeral_5m_input_tokens=1_000, ephemeral_1h_input_tokens=2_000
            ),
            server_tool_use=FakeServerToolUsage(web_search_requests=4, web_fetch_requests=1),
        )
        attempt = known_attempt(usage, operation=OPERATION_VERIFICATION, transport="batch")
        assert (attempt.input_tokens, attempt.output_tokens) == (100, 40)
        assert (attempt.web_search_requests, attempt.web_fetch_requests) == (4, 1)
        assert attempt.cache_creation_5m_input_tokens == 1_000
        assert attempt.cache_creation_1h_input_tokens == 2_000
        assert attempt.cache_creation_unknown_input_tokens == 0
        assert attempt.cache_read_input_tokens == 700

    def test_a_legacy_call_usage_entry_reads_as_a_known_attempt(self):
        legacy = {"model": OPUS, "escalated": True, "input_tokens": 10, "output_tokens": 2}
        attempt = AttemptUsage.from_dict(
            legacy, operation=OPERATION_VERIFICATION, transport=TRANSPORT_BATCH
        )
        assert attempt.role == ROLE_ESCALATION
        assert attempt.model == OPUS
        assert attempt.transport == TRANSPORT_BATCH
        assert attempt.usage_known and attempt.input_tokens == 10

    def test_a_record_marked_unknown_stays_unknown(self):
        data = {"usage_known": False, "input_tokens": 999, "operation": "review"}
        attempt = AttemptUsage.from_dict(data)
        assert not attempt.usage_known
        assert attempt.input_tokens == 0

    def test_round_trip(self):
        attempt = known_attempt(
            {"input_tokens": 7, "output_tokens": 3, "web_search_requests": 2},
            operation=OPERATION_REVIEW, role=ROLE_REPAIR, transport=TRANSPORT_BATCH,
            model=OPUS, batch_id="b", custom_id="c", scope=SCOPE_EARLIER, outcome="ok",
        )
        assert AttemptUsage.from_dict(attempt.to_dict()) == attempt

    def test_known_totals_skip_unknown_and_keep_an_inconsistent_status(self):
        good = known_attempt(
            {"input_tokens": 10, "output_tokens": 5}, operation="review", transport="batch"
        )
        odd = known_attempt(
            {
                "input_tokens": 1,
                "cache_creation_input_tokens": 50,
                "cache_creation_5m_input_tokens": 0,
                "cache_creation_1h_input_tokens": 0,
                "cache_creation_unknown_input_tokens": 50,
                "cache_creation_breakdown_status": CACHE_BREAKDOWN_INCONSISTENT,
            },
            operation="review", transport="batch",
        )
        lost = unknown_attempt(operation="review", transport="batch")
        totals = known_totals([good, odd, lost])
        assert totals["input_tokens"] == 11
        assert totals["cache_creation_input_tokens"] == 50
        assert totals["cache_creation_breakdown_status"] == CACHE_BREAKDOWN_INCONSISTENT

    def test_categories(self):
        assert spend_category(OPERATION_REVIEW, ROLE_REPAIR) == "review_repair"
        assert spend_category(OPERATION_VERIFICATION, ROLE_ESCALATION) == "verification_escalation"
        assert spend_category(OPERATION_VERIFICATION, ROLE_FALLBACK) == "verification"
        assert spend_category("mystery", ROLE_PRIMARY) == "other"
        assert operation_for_phase("batch_collect") == OPERATION_REVIEW
        assert operation_for_phase("cross_check_verification") == OPERATION_VERIFICATION
        assert operation_for_phase("unheard_of") == "other"
        assert set(CATEGORY_LABELS) >= {"review", "review_repair", OPERATION_TRIAGE}


# ===========================================================================
# 2. Review and repair: finding selection is not attempt accounting
# ===========================================================================


def _review_calls(state) -> list[AttemptUsage]:
    return attempts_from(state.review_result.call_usage)


class TestReviewRepairAccounting:
    def test_primary_plus_repair_equals_both_attempts(self, monkeypatch):
        """Acceptance: the truncated primary stays on the books after the
        repair replaces its findings."""
        svc.FakeBatchService(
            monkeypatch,
            primary={svc.PRIMARY_ID: {
                svc.request_id(0): svc.review_ok("A.docx"),
                svc.request_id(1): svc.review_truncated(),
            }},
        )
        diag = DiagnosticsReport()
        state = pl.collect_review_state_headless(
            svc.submission(["A.docx", "B.docx"]), diagnostics=diag
        )

        # The repaired findings were selected...
        assert {f.fileName for f in state.review_result.findings} == {"A.docx", "B.docx"}
        # ...and all three paid requests are on the combined result.
        calls = _review_calls(state)
        assert [(a.batch_id, a.role) for a in calls] == [
            (svc.PRIMARY_ID, ROLE_PRIMARY),
            (svc.PRIMARY_ID, ROLE_PRIMARY),
            ("msgbatch_REPAIR_1", ROLE_REPAIR),
        ]
        assert state.review_result.output_tokens == 400 + 128_000 + 400
        assert state.review_result.input_tokens == 3_000

        summary = cost(diag)
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(
            price(3_000, 128_800), abs=1e-6
        )
        by_category = summary["by_category"]
        assert by_category["review"]["attempts"] == 2
        assert by_category["review"]["total"] == pytest.approx(price(2_000, 128_400), abs=1e-6)
        assert by_category["review_repair"]["attempts"] == 1
        assert by_category["review_repair"]["total"] == pytest.approx(price(1_000, 400), abs=1e-6)

    def test_a_repair_that_fails_again_is_billed_too(self, monkeypatch):
        fake = svc.FakeBatchService(
            monkeypatch,
            primary={svc.PRIMARY_ID: {svc.request_id(0): svc.review_truncated()}},
        )
        fake.primary["msgbatch_REPAIR_1"] = {
            _review_custom_id("A.docx", 0): svc.review_truncated()
        }
        diag = DiagnosticsReport()
        state = pl.collect_review_state_headless(svc.submission(["A.docx"]), diagnostics=diag)

        assert state.truncated_specs == ["A.docx"]
        assert state.review_result.output_tokens == 256_000
        assert total(diag) == pytest.approx(price(2_000, 256_000), abs=1e-6)

    def test_failed_parses_with_usage_remain_billable(self, monkeypatch):
        """Acceptance: a refusal is never repaired, and it was still billed."""
        refused = svc.review_refused()
        refused.input_tokens, refused.output_tokens = 1_000, 30
        svc.FakeBatchService(
            monkeypatch, primary={svc.PRIMARY_ID: {svc.request_id(0): refused}}
        )
        diag = DiagnosticsReport()
        state = pl.collect_review_state_headless(svc.submission(["A.docx"]), diagnostics=diag)

        assert state.truncated_specs == ["A.docx"]
        assert state.review_result.output_tokens == 30
        assert total(diag) == pytest.approx(price(1_000, 30), abs=1e-6)

    def test_an_errored_batch_item_is_a_known_zero_not_unknown(self, monkeypatch):
        # The Message Batches API does not bill errored, canceled, or expired
        # items: their usage is known, and it is zero.
        svc.FakeBatchService(
            monkeypatch,
            primary={svc.PRIMARY_ID: {
                svc.request_id(0): ReviewResult(findings=[], error="Batch request errored"),
            }},
        )
        diag = DiagnosticsReport()
        state = pl.collect_review_state_headless(svc.submission(["A.docx"]), diagnostics=diag)

        primary, repair = _review_calls(state)
        assert primary.usage_known and primary.outcome == "errored"
        assert primary.input_tokens == 0
        assert repair.role == ROLE_REPAIR and repair.usage_known
        assert cost(diag)["unknown_usage_attempts"] == 0
        assert total(diag) == pytest.approx(price(1_000, 400), abs=1e-6)

    def test_a_pending_repair_is_unknown_usage_counted_and_never_priced(self, monkeypatch):
        fake = svc.FakeBatchService(
            monkeypatch,
            primary={svc.PRIMARY_ID: {
                svc.request_id(0): svc.review_ok("A.docx"),
                svc.request_id(1): svc.review_truncated(),
            }},
        )
        fake.default_repair_status = "processing"
        diag = DiagnosticsReport()
        state = pl.collect_review_state_headless(
            svc.submission(["A.docx", "B.docx"]), diagnostics=diag
        )

        repair = [a for a in _review_calls(state) if a.role == ROLE_REPAIR]
        assert len(repair) == 1
        assert not repair[0].usage_known and repair[0].outcome == "pending"
        summary = cost(diag)
        assert summary["unknown_usage_attempts"] == 1
        assert summary["by_category"]["review_repair"]["unknown_usage_attempts"] == 1
        assert summary["phases"]["batch_collect"]["unknown_usage_calls"] == 1
        # Only the two primaries are priced; the pending repair is not zero,
        # it is absent and named.
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(
            price(2_000, 128_400), abs=1e-6
        )
        lines = cost_summary_lines(diag.summary())
        assert any(
            "1 attempt(s) with unknown usage are not in the estimate (review repair 1)" in line
            for line in lines
        )

    @pytest.mark.parametrize("status", ["unreachable"])
    def test_an_unreachable_repair_is_unknown_usage(self, monkeypatch, status):
        fake = svc.FakeBatchService(
            monkeypatch,
            primary={svc.PRIMARY_ID: {svc.request_id(0): svc.review_truncated()}},
        )
        fake.default_repair_status = status
        state = pl.collect_review_state_headless(svc.submission(["A.docx"]))
        repair = [a for a in _review_calls(state) if a.role == ROLE_REPAIR]
        assert [(a.usage_known, a.outcome) for a in repair] == [(False, "unreachable")]


# ===========================================================================
# 3. Duplicates, and whose spend it is
# ===========================================================================


class TestRepeatedCollectionAndScope:
    def test_collecting_the_same_batch_twice_counts_it_once(self, monkeypatch):
        """Acceptance: repeated collection does not duplicate an attempt."""
        svc.FakeBatchService(
            monkeypatch, primary={svc.PRIMARY_ID: {svc.request_id(0): svc.review_ok("A.docx")}}
        )
        diag = DiagnosticsReport()
        for _ in range(2):
            pl.collect_review_state_headless(svc.submission(["A.docx"]), diagnostics=diag)

        summary = diag.summary()
        assert summary["total_input_tokens"] == 1_000
        assert summary["cost_summary"]["duplicate_attempts_ignored"] == 1
        assert summary["cost_summary"]["estimated_cost_usd"]["total"] == pytest.approx(
            price(1_000, 400), abs=1e-6
        )
        assert any("counted once" in line for line in cost_summary_lines(summary))

    @pytest.mark.parametrize("known_first", [True, False])
    def test_a_read_copy_wins_over_an_unread_copy_of_one_attempt(self, known_first):
        read = known_attempt(
            {"input_tokens": 1_000, "output_tokens": 400},
            operation=OPERATION_REVIEW, role=ROLE_REPAIR, transport=TRANSPORT_BATCH,
            model=OPUS, batch_id="rep", custom_id="c1",
        )
        unread = unknown_attempt(
            operation=OPERATION_REVIEW, role=ROLE_REPAIR, transport=TRANSPORT_BATCH,
            model=OPUS, batch_id="rep", custom_id="c1", outcome="pending",
        )
        diag = DiagnosticsReport()
        for attempt in ([read, unread] if known_first else [unread, read]):
            diag.record_api_call(
                phase="batch_collect", model=OPUS, mode="batch",
                operation=OPERATION_REVIEW, attempts=[attempt],
            )
        summary = cost(diag)
        assert summary["unknown_usage_attempts"] == 0
        assert summary["duplicate_attempts_ignored"] == 1
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(
            price(1_000, 400), abs=1e-6
        )

    def test_attempts_without_identity_are_never_deduplicated(self):
        diag = DiagnosticsReport()
        for _ in range(2):
            diag.record_api_call(
                phase="review", model=OPUS, mode="realtime", operation=OPERATION_REVIEW,
                attempts=[unknown_attempt(operation=OPERATION_REVIEW, transport="realtime")],
            )
        assert cost(diag)["unknown_usage_attempts"] == 2
        assert cost(diag)["duplicate_attempts_ignored"] == 0

    def test_a_resumed_batch_is_earlier_spend(self, monkeypatch):
        """Recovery scope: the recovered batch was billed before this
        collection started, and the summary says so separately."""
        svc.FakeBatchService(
            monkeypatch, primary={svc.PRIMARY_ID: {svc.request_id(0): svc.review_ok("A.docx")}}
        )
        resumed = pl.reconstruct_batch_submission(
            batch_id=svc.PRIMARY_ID,
            request_map={svc.request_id(0): {"filename": "A.docx", "index": 0, "type": "review"}},
            review_request_ids=[svc.request_id(0)],
            files_reviewed=["A.docx"],
            input_dir=None,
            files=None,
            model=OPUS,
            project_context="",
            module=pl.get_module("california_k12_mep"),
            cross_check_enabled=False,
            created_at=0.0,
        )
        assert resumed.resumed is True
        diag = DiagnosticsReport()
        pl.collect_review_state_headless(resumed, diagnostics=diag)

        by_scope = cost(diag)["by_scope"]
        assert by_scope[SCOPE_EARLIER]["total"] == pytest.approx(price(1_000, 400), abs=1e-6)
        assert by_scope[SCOPE_RUN]["total"] == 0.0
        lines = cost_summary_lines(diag.summary())
        assert any("Earlier batch spend (billed before this collection started)" in l for l in lines)
        assert any("This collection's own spend: $0.0000" in l for l in lines)

    def test_a_repair_this_collection_submits_is_its_own_spend(self, monkeypatch):
        svc.FakeBatchService(
            monkeypatch,
            primary={svc.PRIMARY_ID: {svc.request_id(0): svc.review_truncated()}},
        )
        submission = svc.submission(["A.docx"])
        submission.resumed = True
        diag = DiagnosticsReport()
        pl.collect_review_state_headless(submission, diagnostics=diag)

        by_scope = cost(diag)["by_scope"]
        assert by_scope[SCOPE_EARLIER]["total"] == pytest.approx(price(1_000, 128_000), abs=1e-6)
        assert by_scope[SCOPE_RUN]["total"] == pytest.approx(price(1_000, 400), abs=1e-6)

    def test_a_reattached_repair_is_earlier_spend(self, monkeypatch):
        fake = svc.FakeBatchService(
            monkeypatch,
            primary={svc.PRIMARY_ID: {svc.request_id(0): svc.review_truncated()}},
        )
        fake.status["msgbatch_SAVED_REPAIR"] = "ended"
        submission = svc.submission(["A.docx"])
        submission.repair_batch_id = "msgbatch_SAVED_REPAIR"
        submission.repair_request_map = {
            _review_custom_id("A.docx", 0): {"filename": "A.docx", "index": 0, "type": "review"}
        }
        diag = DiagnosticsReport()
        state = pl.collect_review_state_headless(submission, diagnostics=diag)

        assert fake.repair_submits == []  # consumed, never replaced
        repair = [a for a in _review_calls(state) if a.role == ROLE_REPAIR]
        assert [(a.batch_id, a.scope, a.usage_known) for a in repair] == [
            ("msgbatch_SAVED_REPAIR", SCOPE_EARLIER, True)
        ]

    @pytest.mark.parametrize("one_report", [False, True])
    def test_a_pending_repair_collected_later_accounts_for_both_attempts(
        self, monkeypatch, one_report
    ):
        """Plan §28 scenario C, its accounting clause: a truncated primary whose
        repair was still running is collected again (after a restart, or into
        the same report); the later collection consumes the same repair and
        the estimate holds both attempts, each once, nothing left unknown."""
        fake = svc.FakeBatchService(
            monkeypatch,
            primary={svc.PRIMARY_ID: {svc.request_id(0): svc.review_truncated()}},
        )
        fake.default_repair_status = "processing"
        submission = svc.submission(["A.docx"])
        first_diag = DiagnosticsReport()
        first = pl.collect_review_state_headless(submission, diagnostics=first_diag)
        assert first.collection_outcome.provisional
        assert cost(first_diag)["unknown_usage_attempts"] == 1

        (repair_id,) = fake.status
        fake.status[repair_id] = "ended"
        if one_report:
            later, diag = submission, first_diag
        else:
            # A restart rebuilds the submission from saved state.
            later, diag = svc.submission(["A.docx"]), DiagnosticsReport()
            later.resumed = True
            later.repair_batch_id = submission.repair_batch_id
            later.repair_request_map = dict(submission.repair_request_map)
        second = pl.collect_review_state_headless(later, diagnostics=diag)

        assert not second.collection_outcome.provisional
        assert len(fake.repair_submits) == 1  # the same repair, consumed
        summary = cost(diag)
        assert summary["unknown_usage_attempts"] == 0
        assert summary["by_category"]["review"]["attempts"] == 1
        assert summary["by_category"]["review_repair"]["attempts"] == 1
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(
            price(1_000, 128_000) + price(1_000, 400), abs=1e-6
        )
        if not one_report:
            # Billed before this collection started: the primary batch, and
            # the repair an earlier collection submitted.
            assert summary["by_scope"][SCOPE_RUN]["total"] == 0.0

    def test_the_estimate_is_labelled_as_one(self, monkeypatch):
        svc.FakeBatchService(
            monkeypatch, primary={svc.PRIMARY_ID: {svc.request_id(0): svc.review_ok("A.docx")}}
        )
        diag = DiagnosticsReport()
        pl.collect_review_state_headless(svc.submission(["A.docx"]), diagnostics=diag)
        summary = diag.summary()
        assert summary["cost_summary"]["estimate_note"] == ESTIMATE_NOTE
        assert "not an invoice" in ESTIMATE_NOTE
        headline = cost_summary_lines(summary)[0]
        assert headline.startswith("Estimated cost (USD): $") and ESTIMATE_NOTE in headline
        assert ESTIMATE_NOTE in diag.to_text()

    def test_no_spend_means_no_cost_lines(self):
        assert cost_summary_lines(DiagnosticsReport().summary()) == []


# ===========================================================================
# 4. One billing input; every price category applied once
# ===========================================================================


class TestPricingAppliedOnce:
    def _attempt(self, **overrides):
        usage = dict(
            input_tokens=2_000,
            output_tokens=900,
            cache_creation_input_tokens=3_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_1h_input_tokens=2_000,
            cache_creation_unknown_input_tokens=0,
            cache_creation_breakdown_status="complete",
            cache_read_input_tokens=5_000,
            web_search_requests=3,
        )
        usage.update(overrides)
        return usage

    @pytest.mark.parametrize("transport", [TRANSPORT_BATCH, TRANSPORT_REALTIME])
    def test_each_line_item_is_priced_once(self, transport):
        usage = self._attempt()
        attempt = known_attempt(
            usage, operation=OPERATION_VERIFICATION, transport=transport,
            model=SONNET, message_id="msg_once",
        )
        diag = DiagnosticsReport()
        # The flat fields ride along as a display summary; the attempt record
        # is the billing input. Pricing both would double every line.
        diag.record_api_call(
            phase="verification", model=SONNET, mode=transport,
            input_tokens=2_000, output_tokens=900,
            cache_creation_input_tokens=3_000, cache_read_input_tokens=5_000,
            web_search_requests=3, operation=OPERATION_VERIFICATION, attempts=[attempt],
        )
        expected = estimate_cost_breakdown(
            2_000, 900, model=SONNET, batch=(transport == TRANSPORT_BATCH),
            cache_creation_input_tokens=3_000,
            cache_creation_5m_input_tokens=1_000,
            cache_creation_1h_input_tokens=2_000,
            cache_creation_unknown_input_tokens=0,
            cache_read_input_tokens=5_000,
            web_search_requests=3,
        )
        lines = cost(diag)["estimated_cost_usd"]
        for key in ("tokens", "cache_writes", "cache_reads", "web_searches", "total"):
            assert lines[key] == pytest.approx(getattr(expected, key), abs=1e-6), key

    def test_flat_fields_never_add_to_attempt_records(self):
        attempt = known_attempt(
            {"input_tokens": 1_000, "output_tokens": 400},
            operation=OPERATION_REVIEW, transport=TRANSPORT_BATCH, model=OPUS,
            batch_id="b", custom_id="c",
        )
        diag = DiagnosticsReport()
        diag.record_api_call(
            phase="batch_collect", model=OPUS, mode="batch",
            input_tokens=999_999, output_tokens=999_999,
            operation=OPERATION_REVIEW, attempts=[attempt],
        )
        assert total(diag) == pytest.approx(price(1_000, 400), abs=1e-6)

    def test_each_attempt_is_priced_on_its_own_transport_and_model(self):
        """A real-time fallback inside a batch run pays standard rates."""
        batch_wave = known_attempt(
            {"input_tokens": 1_000, "output_tokens": 100}, operation=OPERATION_VERIFICATION,
            transport=TRANSPORT_BATCH, model=SONNET, batch_id="w1", custom_id="verify__0",
        )
        fallback = known_attempt(
            {"input_tokens": 1_000, "output_tokens": 100}, operation=OPERATION_VERIFICATION,
            role=ROLE_FALLBACK, transport=TRANSPORT_REALTIME, model=OPUS, message_id="msg_fb",
        )
        diag = DiagnosticsReport()
        diag.record_api_call(
            phase="verification", model=OPUS, mode="batch",
            operation=OPERATION_VERIFICATION, attempts=[batch_wave, fallback],
        )
        assert total(diag) == pytest.approx(
            price(1_000, 100, model=SONNET, batch=True)
            + price(1_000, 100, model=OPUS, batch=False),
            abs=1e-6,
        )


# ===========================================================================
# 5. Real time: totals unchanged, every call recorded
# ===========================================================================


class _Stream:
    def __init__(self, outcome):
        self._outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def text_stream(self):
        return iter(())

    def get_final_message(self):
        return self._outcome


def _scripted_review_client(outcomes: list):
    queue = list(outcomes)
    calls: list[dict] = []

    def stream(**kwargs):
        calls.append(kwargs)
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Stream(outcome)

    return SimpleNamespace(messages=SimpleNamespace(stream=stream)), calls


def _review_message(*, stop_reason="tool_use", input_tokens=2_000, output_tokens=500):
    from tests.fixtures.fake_anthropic import sample_review_findings_payload

    content = (
        [FakeToolUseBlock(name="submit_review_findings", input=sample_review_findings_payload())]
        if stop_reason == "tool_use"
        else [FakeTextBlock(text="Reviewing… (cut off")]
    )
    return FakeMessage(
        content=content,
        stop_reason=stop_reason,
        usage=FakeUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@pytest.fixture
def realtime_review(monkeypatch):
    monkeypatch.setattr(rt, "review_extended_output_count", lambda request_spec: 10)
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)

    def run(outcomes, *, diag):
        client, calls = _scripted_review_client(outcomes)
        monkeypatch.setattr(rt, "_get_client", lambda **_: client)
        specs = [svc.spec("A.docx")]
        results, request_map = rt.run_realtime_review(specs, model=OPUS, diagnostics=diag)
        return results, request_map, calls, specs

    return run


class TestRealtimeReview:
    def test_totals_are_unchanged_for_an_equivalent_run(self, realtime_review):
        """Acceptance: truncated call + inline repair — each call recorded once,
        priced at standard rates, exactly as the runner's rows always were."""
        truncated = _review_message(stop_reason="max_tokens", output_tokens=128_000)
        repaired = _review_message()
        diag = DiagnosticsReport()
        results, request_map, calls, specs = realtime_review([truncated, repaired], diag=diag)

        assert len(calls) == 2
        expected = price(2_000, 128_000, batch=False) + price(2_000, 500, batch=False)
        assert total(diag) == pytest.approx(expected, abs=1e-6)
        assert diag.summary()["total_output_tokens"] == 128_500

        # The spec's result carries both calls; the collect step folds them into
        # the combined carrier (without recording them again).
        rr = next(iter(results.values()))
        assert [a.role for a in attempts_from(rr.call_usage)] == [ROLE_PRIMARY, ROLE_REPAIR]
        submission = svc.submission(["A.docx"], transport="realtime")
        submission.job.request_map = dict(request_map)
        submission.review_request_ids = list(request_map)
        submission.realtime_results = dict(results)
        submission.prepared_specs = specs
        state = pl.collect_review_state_headless(submission, diagnostics=diag)
        assert state.review_result.output_tokens == 128_500
        assert total(diag) == pytest.approx(expected, abs=1e-6)

    def test_each_call_that_raised_is_its_own_unknown_attempt(self, realtime_review):
        rows: list[dict] = []
        diag = SimpleNamespace(record_api_call=lambda **kw: rows.append(kw))
        results, _map, calls, _specs = realtime_review(
            [rate_limit(), rate_limit(), rate_limit()], diag=diag
        )
        assert len(rows) == len(calls) == 3
        for row in rows:
            (attempt,) = row["attempts"]
            assert attempt.usage_known is False
            assert attempt.outcome == "exception"
        rr = next(iter(results.values()))
        assert [a.role for a in attempts_from(rr.call_usage)] == [
            ROLE_PRIMARY, ROLE_RETRY, ROLE_RETRY,
        ]

    def test_a_retry_after_a_raised_call_keeps_both(self, realtime_review):
        diag = DiagnosticsReport()
        realtime_review([rate_limit(), _review_message()], diag=diag)
        summary = cost(diag)
        assert summary["unknown_usage_attempts"] == 1
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(
            price(2_000, 500, batch=False), abs=1e-6
        )


# ===========================================================================
# 6. Cross-check: a parse retry keeps the attempt it abandoned
# ===========================================================================


def test_cross_check_parse_retry_keeps_both_responses(monkeypatch):
    import src.core.tokenizer as tokenizer
    import src.cross_check.cross_checker as CC

    bad = FakeMessage(
        content=[FakeTextBlock(text="not json at all")], stop_reason="end_turn",
        usage=FakeUsage(input_tokens=5_000, output_tokens=900),
    )
    good = FakeMessage(
        content=[FakeTextBlock(text="<cross_check_json>[]</cross_check_json>")],
        stop_reason="end_turn", usage=FakeUsage(input_tokens=5_000, output_tokens=100),
    )
    client, calls = _scripted_review_client([bad, good])
    client.messages.count_tokens = None
    monkeypatch.setattr(CC, "_get_client", lambda **_: client)
    monkeypatch.setattr(tokenizer, "count_tokens", lambda text: len(str(text).split()))
    monkeypatch.setattr(CC, "count_tokens", lambda text: len(str(text).split()))
    monkeypatch.setattr(CC.time, "sleep", lambda _s: None)

    result = CC.run_cross_check([svc.spec("A.docx"), svc.spec("B.docx")], [], max_retries=3)

    assert len(calls) == 2
    assert (result.input_tokens, result.output_tokens) == (10_000, 1_000)


# ===========================================================================
# 7. Verification: abandoned conversations, fallbacks, detaches, escalations
# ===========================================================================


PAUSED = vd.message(vd.search_blocks(), stop_reason="pause_turn")


def _verdict():
    return vd.message(vd.search_blocks() + [vd.verdict_call(vd.verdict_payload())])


def _known_input(result: VerificationResult) -> int:
    return sum(a.input_tokens for a in attempts_from(result.call_usage) if a.usage_known)


def _batch_harness(monkeypatch, waves: list, *, detach_on_poll: int | None = None):
    """Batch primitives answering wave ``n`` with ``waves[n - 1]``.

    Each entry is a message (succeeded) or ``"errored"``. ``detach_on_poll``
    makes that poll (1-based) stop before its wave finished.
    """
    state = {"poll": 0, "wave": 0}

    def fake_poll(batch_id, **_kwargs):
        state["poll"] += 1
        detached = detach_on_poll is not None and state["poll"] == detach_on_poll
        return SimpleNamespace(detached=detached, poll_failed=False)

    def fake_retrieve(job):
        state["wave"] += 1
        outcome = waves[state["wave"] - 1]
        out = {}
        for cid in job.request_map:
            if outcome == "errored":
                envelope = FakeBatchResultEnvelope(
                    type="errored", error=SimpleNamespace(type="api_error", message="overloaded")
                )
            else:
                envelope = FakeBatchResultEnvelope(type="succeeded", message=outcome)
            out[cid] = FakeBatchResult(custom_id=cid, result=envelope)
        return out

    submitted = {"n": 0}

    def fake_submit(requests, request_map, *, extra_headers=None):
        submitted["n"] += 1
        return SimpleNamespace(
            batch_id=f"wave{submitted['n'] + 1}", request_map=request_map, job_type="verify"
        )

    monkeypatch.setattr(V, "poll_batch_bounded", fake_poll)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    monkeypatch.setattr(V, "submit_verification_followup_wave", fake_submit)

    def run(*, max_waves=3, fallback_threshold=0) -> Finding:
        finding = vd.medium_finding()
        job = SimpleNamespace(
            batch_id="wave1", request_map={"verify__0": {"finding_idx": 0}}, job_type="verify"
        )
        collect_verification_batch_results(
            job, [finding], cycle=DEFAULT_CYCLE, poll_policy=DEFAULT_VERIFICATION_POLL_POLICY,
            max_waves=max_waves, realtime_fallback_threshold=fallback_threshold,
        )
        return finding

    return run


class TestVerificationAttempts:
    def test_a_realtime_retry_keeps_the_conversation_it_abandoned(self, monkeypatch):
        # Attempt 1: a paid paused turn, then its continuation raises.
        # Attempt 2: a fresh conversation reaches a verdict.
        script = iter([PAUSED, rate_limit(), _verdict()])
        result, client = vd.run_realtime(monkeypatch, lambda _k: next(script), max_retries=1)

        assert len(client.calls) == 3
        calls = attempts_from(result.call_usage)
        assert [(a.role, a.usage_known) for a in calls] == [
            (ROLE_PRIMARY, True),    # the paused turn that was read
            (ROLE_PRIMARY, False),   # the continuation that raised
            (ROLE_RETRY, True),      # the conversation that reached the verdict
        ]
        assert _known_input(result) == 2 * vd.INPUT_TOKENS
        assert all(a.transport == TRANSPORT_REALTIME for a in calls)

    def test_a_batch_fresh_retry_keeps_the_paused_waves(self, monkeypatch):
        # Wave 1 pauses (paid), wave 2's continuation item errors (not billed),
        # wave 3 retries from scratch and reaches the verdict.
        run = _batch_harness(monkeypatch, [PAUSED, "errored", _verdict()])
        finding = run()

        result = finding.verification
        assert result.verdict == "CONFIRMED"
        calls = attempts_from(result.call_usage)
        assert [(a.batch_id, a.role) for a in calls] == [("wave1", ROLE_PRIMARY), ("wave3", ROLE_RETRY)]
        assert _known_input(result) == 2 * vd.INPUT_TOKENS
        assert all(a.transport == TRANSPORT_BATCH for a in calls)

    def test_a_fallback_keeps_the_paid_batch_wave_and_pays_standard_rates(self, monkeypatch):
        run = _batch_harness(monkeypatch, [PAUSED])
        client = vd.ScriptedStreamClient(lambda _k: _verdict())
        monkeypatch.setattr(V, "_get_client", lambda **_: client)
        finding = run(max_waves=1, fallback_threshold=5)

        result = finding.verification
        assert len(client.calls) == 1
        calls = attempts_from(result.call_usage)
        assert [(a.transport, a.role) for a in calls] == [
            (TRANSPORT_BATCH, ROLE_PRIMARY),
            (TRANSPORT_REALTIME, ROLE_FALLBACK),
        ]
        assert _known_input(result) == 2 * vd.INPUT_TOKENS

        diag = DiagnosticsReport()
        record_verification_findings(diag, [finding], phase="verification", transport="batch")
        extra = dict(
            cache_creation_input_tokens=vd.CACHE_WRITE_TOKENS,
            cache_read_input_tokens=vd.CACHE_READ_TOKENS,
            web_search_requests=2,
        )
        expected = price(
            vd.INPUT_TOKENS, vd.OUTPUT_TOKENS, model=calls[0].model, batch=True, **extra
        ) + price(vd.INPUT_TOKENS, vd.OUTPUT_TOKENS, model=calls[1].model, batch=False, **extra)
        assert total(diag) == pytest.approx(expected, abs=1e-6)

    def test_a_detached_wave_is_unknown_and_the_read_waves_stay_known(self, monkeypatch):
        # Wave 1 pauses and is read; polling stops before wave 2 finishes.
        run = _batch_harness(monkeypatch, [PAUSED, _verdict()], detach_on_poll=2)
        finding = run()

        result = finding.verification
        assert result.verification_failed is True
        calls = attempts_from(result.call_usage)
        assert [(a.batch_id, a.usage_known) for a in calls] == [("wave1", True), ("wave2", False)]
        diag = DiagnosticsReport()
        record_verification_findings(diag, [finding], phase="verification", transport="batch")
        assert cost(diag)["unknown_usage_attempts"] == 1
        assert total(diag) > 0

    def test_an_unread_escalation_batch_is_unknown_not_missing(self, monkeypatch):
        finding = vd.medium_finding(severity="CRITICAL", codeReference="")
        finding.verification = VerificationResult(
            verdict="UNVERIFIED", grounded=False, model_used=SONNET, cache_status="miss",
            input_tokens=900, output_tokens=90, transport=TRANSPORT_BATCH,
        )
        monkeypatch.setattr(
            V, "submit_verification_followup_wave",
            lambda requests, request_map, *, extra_headers=None: SimpleNamespace(
                batch_id="esc-batch", request_map=request_map, job_type="verify"
            ),
        )
        monkeypatch.setattr(
            V, "poll_batch_bounded",
            lambda *a, **k: SimpleNamespace(detached=True, poll_failed=False),
        )
        V._run_batch_escalation_wave(
            [finding], cycle=DEFAULT_CYCLE, cache=None,
            policy=DEFAULT_VERIFICATION_POLL_POLICY,
            log=lambda *a, **k: None, progress=lambda *a: None,
        )
        calls = attempts_from(finding.verification.call_usage)
        assert [(a.role, a.usage_known, a.batch_id) for a in calls] == [
            (ROLE_PRIMARY, True, ""),
            (ROLE_ESCALATION, False, "esc-batch"),
        ]
        diag = DiagnosticsReport()
        record_verification_findings(diag, [finding], phase="verification", transport="batch")
        summary = cost(diag)
        assert summary["by_category"]["verification_escalation"]["unknown_usage_attempts"] == 1
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(
            price(900, 90, model=SONNET, batch=True), abs=1e-6
        )

    def test_an_escalation_is_its_own_category(self, monkeypatch):
        finding = vd.medium_finding(severity="CRITICAL", codeReference="")
        finding.verification = VerificationResult(
            verdict="UNVERIFIED", grounded=False, model_used=SONNET, cache_status="miss",
            input_tokens=900, output_tokens=90, transport=TRANSPORT_BATCH,
        )
        opus_verdict = vd.message(vd.search_blocks() + [vd.verdict_call(vd.verdict_payload())])
        monkeypatch.setattr(
            V, "submit_verification_followup_wave",
            lambda requests, request_map, *, extra_headers=None: SimpleNamespace(
                batch_id="esc-batch", request_map=request_map, job_type="verify"
            ),
        )
        monkeypatch.setattr(
            V, "poll_batch_bounded",
            lambda *a, **k: SimpleNamespace(detached=False, poll_failed=False),
        )
        monkeypatch.setattr(
            V, "retrieve_verification_results_detailed",
            lambda job: {
                cid: batch_verification_result(custom_id=cid, message=opus_verdict)
                for cid in job.request_map
            },
        )
        V._run_batch_escalation_wave(
            [finding], cycle=DEFAULT_CYCLE, cache=None,
            policy=DEFAULT_VERIFICATION_POLL_POLICY,
            log=lambda *a, **k: None, progress=lambda *a: None,
        )
        diag = DiagnosticsReport()
        record_verification_findings(diag, [finding], phase="verification", transport="batch")
        by_category = cost(diag)["by_category"]
        assert by_category["verification"]["attempts"] == 1
        assert by_category["verification_escalation"]["attempts"] == 1
        assert by_category["verification_escalation"]["total"] > 0

    def test_a_worker_crash_is_recorded_as_unknown(self, monkeypatch):
        def crash(*_a, **_k):
            raise RuntimeError("worker died")

        monkeypatch.setattr(pl, "prepare_findings_for_verification", lambda findings, **kw: list(findings))
        monkeypatch.setattr(pl, "verify_finding", crash)
        finding = vd.medium_finding()
        pl.verify_findings_for_run([finding], transport="realtime")
        calls = attempts_from(finding.verification.call_usage)
        assert [(a.usage_known, a.outcome) for a in calls] == [(False, "exception")]


# ===========================================================================
# 8. Shared followers and cache replays cost nothing more
# ===========================================================================


@pytest.mark.parametrize(
    "verdict, follower_status",
    [("CONFIRMED", "hit"), ("UNVERIFIED", "shared")],
)
def test_followers_add_no_spend(monkeypatch, verdict, follower_status):
    """Acceptance: one call for two equivalent findings; the follower — a
    cache replay or an in-process share — bills nothing."""
    from src.verification.verification_cache import VerificationCache

    payload = vd.verdict_payload(verdict)
    if verdict == "UNVERIFIED":
        payload.update(sources=[], source_quote="")
    message = vd.message(vd.search_blocks() + [vd.verdict_call(payload)])
    client = vd.ScriptedStreamClient(lambda _k: message)
    monkeypatch.setattr(V, "_get_client", lambda **_: client)
    monkeypatch.setattr(pl, "prepare_findings_for_verification", lambda findings, **kw: list(findings))
    cache = VerificationCache()  # in memory; nothing is written to disk
    findings = [vd.medium_finding(), vd.medium_finding()]

    pl.verify_findings_for_run(findings, transport="realtime", cache=cache)

    assert len(client.calls) == 1
    statuses = sorted(f.verification.cache_status for f in findings)
    assert follower_status in statuses
    diag = DiagnosticsReport()
    record_verification_findings(diag, findings, phase="verification", transport="realtime")
    leader = next(f for f in findings if f.verification.cache_status == "miss")
    assert total(diag) == pytest.approx(
        price(
            vd.INPUT_TOKENS, vd.OUTPUT_TOKENS, model=leader.verification.model_used,
            batch=False, cache_creation_input_tokens=vd.CACHE_WRITE_TOKENS,
            cache_read_input_tokens=vd.CACHE_READ_TOKENS, web_search_requests=2,
        ),
        abs=1e-6,
    )


# ===========================================================================
# 9. Triage (the Haiku pre-pass) is priced
# ===========================================================================


def _gripe(**overrides) -> Finding:
    fields = dict(
        severity="GRIPES", fileName="A.docx", section="1.01",
        issue="Paragraph 2.1.A and paragraph 3.2.B contradict each other on pipe spacing.",
        actionType="REPORT_ONLY", existingText=None, replacementText=None,
        codeReference="",
    )
    fields.update(overrides)
    return Finding(**fields)


def _triage_client(outcome):
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


def _triage_response(classification="local_skip"):
    return FakeMessage(
        content=[
            FakeToolUseBlock(
                name=triage.TRIAGE_TOOL_NAME,
                input={"classifications": [{"index": 0, "classification": classification}]},
            )
        ],
        stop_reason="tool_use",
        usage=FakeUsage(input_tokens=600, output_tokens=40),
        model=HAIKU,
    )


class TestTriageAccounting:
    def test_each_request_reaches_the_sink(self, monkeypatch):
        response = _triage_response()
        client, calls = _triage_client(response)
        monkeypatch.setattr(triage, "_get_client", lambda **_: client)
        seen: list[AttemptUsage] = []

        out = triage.classify_findings_with_haiku([_gripe()], model=HAIKU, usage_sink=seen.append)

        assert out == {0: "local_skip"}
        (attempt,) = seen
        assert attempt.operation == OPERATION_TRIAGE
        assert attempt.transport == TRANSPORT_REALTIME
        assert (attempt.model, attempt.input_tokens, attempt.output_tokens) == (HAIKU, 600, 40)
        assert attempt.attempt_id == f"message:{response.id}"

    def test_a_request_that_raised_is_unknown(self, monkeypatch):
        client, _calls = _triage_client(rate_limit())
        monkeypatch.setattr(triage, "_get_client", lambda **_: client)
        seen: list[AttemptUsage] = []
        assert triage.classify_findings_with_haiku(
            [_gripe()], model=HAIKU, usage_sink=seen.append
        ) == {}
        assert [(a.usage_known, a.outcome) for a in seen] == [(False, "exception")]

    def test_a_failing_sink_never_changes_routing(self, monkeypatch):
        client, _calls = _triage_client(_triage_response())
        monkeypatch.setattr(triage, "_get_client", lambda **_: client)

        def broken(_attempt):
            raise RuntimeError("diagnostics down")

        assert triage.classify_findings_with_haiku(
            [_gripe()], model=HAIKU, usage_sink=broken
        ) == {0: "local_skip"}

    def test_the_diagnostics_sink_prices_triage_at_standard_rates(self):
        assert triage_usage_sink(None, phase="verification") is None
        diag = DiagnosticsReport()
        sink = triage_usage_sink(diag, phase="verification")
        sink(known_attempt(
            {"input_tokens": 600, "output_tokens": 40}, operation=OPERATION_TRIAGE,
            transport=TRANSPORT_REALTIME, model=HAIKU, message_id="msg_t",
        ))
        sink(unknown_attempt(operation=OPERATION_TRIAGE, transport=TRANSPORT_REALTIME, model=HAIKU))
        summary = cost(diag)
        assert summary["by_category"][OPERATION_TRIAGE]["attempts"] == 2
        assert summary["by_category"][OPERATION_TRIAGE]["unknown_usage_attempts"] == 1
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(
            price(600, 40, model=HAIKU, batch=False), abs=1e-6
        )

    def test_the_pre_pass_hands_triage_to_the_run_sink(self, monkeypatch):
        client, calls = _triage_client(_triage_response())
        monkeypatch.setattr(triage, "_get_client", lambda **_: client)
        diag = DiagnosticsReport()
        finding = _gripe()

        pl.verify_findings_for_run(
            [finding], transport="realtime",
            usage_sink=triage_usage_sink(diag, phase="verification"),
        )

        assert len(calls) == 1
        assert finding.verification.verification_mode == "local_skip"
        assert cost(diag)["by_category"][OPERATION_TRIAGE]["attempts"] == 1

    def test_every_driver_passes_a_sink_to_verification(self):
        """Structural pin: every production ``verify_findings_for_run`` call —
        both rounds, GUI and headless — hands triage a usage sink."""
        found = 0
        for path in [
            REPO / "src" / "gui" / "batch_controller.py",
            REPO / "src" / "orchestration" / "pipeline.py",
        ]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", "")) == "verify_findings_for_run"
                ):
                    found += 1
                    keywords = {kw.arg: kw.value for kw in node.keywords}
                    assert "usage_sink" in keywords, f"{path.name}:{node.lineno}"
                    sink = keywords["usage_sink"]
                    assert isinstance(sink, ast.Call) and getattr(sink.func, "id", "") == "triage_usage_sink"
        assert found == 4


# ===========================================================================
# 10. The ledger: spend survives event caps; legacy records stay readable
# ===========================================================================


class TestLedger:
    def test_evicted_events_keep_their_spend(self):
        diag = DiagnosticsReport()
        record_pass_api_call(
            diag, ReviewResult(model=OPUS, input_tokens=400_000, output_tokens=120_000),
            phase="batch_collect", message="Review results collected", mode="batch",
            operation=OPERATION_REVIEW,
        )
        before = total(diag)
        for i in range(diag.max_events + 10):
            diag.log("batch_poll", "info", f"poll {i}")
        assert diag.events_dropped > 0
        assert total(diag) == pytest.approx(before, abs=1e-9)

    def test_a_byte_capped_event_keeps_its_attempts(self):
        diag = DiagnosticsReport(max_event_data_bytes=512)
        attempt = known_attempt(
            {"input_tokens": 1_000, "output_tokens": 100}, operation=OPERATION_VERIFICATION,
            transport=TRANSPORT_BATCH, model=SONNET, batch_id="b", custom_id="c",
        )
        diag.record_api_call(
            phase="verification", model=SONNET, mode="batch",
            operation=OPERATION_VERIFICATION, attempts=[attempt],
            extra={"padding": "p" * 5_000},
        )
        assert total(diag) == pytest.approx(price(1_000, 100, model=SONNET), abs=1e-6)

    def test_a_legacy_record_is_priced_and_named(self):
        """Acceptance: records without attempt metadata stay readable and are
        visibly limited."""
        diag = DiagnosticsReport()
        diag.record_api_call(
            phase="batch_collect", model=OPUS, mode="batch",
            input_tokens=1_000, output_tokens=400,
        )
        summary = diag.summary()
        assert summary["cost_summary"]["legacy_records"] == 1
        assert summary["cost_summary"]["estimated_cost_usd"]["total"] == pytest.approx(
            price(1_000, 400), abs=1e-6
        )
        assert any("without attempt metadata" in line for line in cost_summary_lines(summary))

    def test_a_legacy_verification_event_is_priced_per_entry(self):
        diag = DiagnosticsReport()
        diag.log(
            "verification", "info", "Verified: a.docx — CONFIRMED",
            {
                "verdict": "CONFIRMED",
                "api_call": True,
                "model": OPUS,
                "call_mode": "batch",
                "input_tokens": 50,
                "call_usage": [
                    {"model": SONNET, "escalated": False, "input_tokens": 1_000, "output_tokens": 100},
                    {"model": OPUS, "escalated": True, "input_tokens": 2_000, "output_tokens": 200},
                ],
            },
        )
        summary = cost(diag)
        assert summary["legacy_records"] == 1
        assert summary["by_category"]["verification_escalation"]["attempts"] == 1
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(
            price(1_000, 100, model=SONNET) + price(2_000, 200, model=OPUS), abs=1e-6
        )

    def test_the_text_export_uses_the_shared_wording(self):
        diag = DiagnosticsReport()
        diag.record_api_call(
            phase="review", model=OPUS, mode="realtime", operation=OPERATION_REVIEW,
            attempts=[unknown_attempt(operation=OPERATION_REVIEW, transport="realtime")],
        )
        text = diag.to_text()
        for line in cost_summary_lines(diag.summary()):
            assert line.strip() in text


def test_a_program_review_result_carries_every_module_attempt():
    """A routed program's combined review result states the same spend as
    its modules: every module's attempt records, and a cache-write split
    that survives the roll-up."""
    from src.orchestration.program_pipeline import _merge_review_results

    def module_result(batch_id: str) -> ReviewResult:
        attempt = known_attempt(
            {
                "input_tokens": 1_000,
                "output_tokens": 400,
                "cache_creation_input_tokens": 600,
                "cache_creation_5m_input_tokens": 200,
                "cache_creation_1h_input_tokens": 400,
                "cache_creation_unknown_input_tokens": 0,
                "cache_creation_breakdown_status": "complete",
            },
            operation=OPERATION_REVIEW, transport=TRANSPORT_BATCH, model=OPUS,
            batch_id=batch_id, custom_id="review__A__0",
        )
        result = ReviewResult(model=OPUS, call_usage=[attempt.to_dict()])
        for key, value in known_totals([attempt]).items():
            setattr(result, key, value)
        return result

    merged = _merge_review_results([("m1", module_result("b1")), ("m2", module_result("b2"))])

    assert [entry["attempt_id"] for entry in merged.call_usage] == [
        "batch:b1:review__A__0:primary",
        "batch:b2:review__A__0:primary",
    ]
    assert merged.input_tokens == 2_000
    assert merged.cache_creation_input_tokens == 1_200
    assert merged.cache_creation_5m_input_tokens == 400
    assert merged.cache_creation_1h_input_tokens == 800
