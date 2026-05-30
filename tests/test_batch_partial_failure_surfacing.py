"""Tests for batch partial-failure surfacing (TRUST_AUDIT P1-2).

The audit question: when a verification batch partially fails or is *canceled*,
are the affected findings clearly marked (VERIFICATION_FAILED / NOT_CHECKED)
and **never silently dropped** from the report / sidecar? Losing a finding on a
batch hiccup would be a trust failure.

Investigation found NO drop gap — `collect_verification_batch_results` ends
every finding with exactly one `VerificationResult` (the post-loop safety net),
and `finalize_batch_result` concatenates findings without filtering on
verification status. These tests lock in the two properties that were *not*
yet covered by a regression test:

1. **Cancellation is terminal, not dropped or retried-forever.** A canceled
   batch item is classified `terminal_unverified` with `BATCH_CANCELED` (the
   wave loop turns that into `verification_failed=True`); a *missing* item is a
   transient `retry` (re-submitted, not dropped). Driven through the real
   `_classify_wave_results`.
2. **A partial failure survives end-to-end into the report and the edit
   sidecar.** A mix of verified / verification-failed / unchecked findings all
   reach `PipelineResult` (count preserved) and the sidecar, each carrying the
   correct `report_status` — the failed finding is labeled VERIFICATION_FAILED,
   not omitted.
"""
from __future__ import annotations

import time

from src.batch.batch import BatchJob
from src.orchestration import pipeline as pl
from src.orchestration.pipeline import (
    BatchSubmission,
    collect_review_batch_results,
    finalize_batch_result,
)
from src.output.edit_sidecar import build_edit_instructions
from src.output.report_status import ReportStatus, classify_status
from src.review.reviewer import Finding, ReviewResult
from src.verification.retry_policy import FailureClass
from src.verification.verifier import VerificationResult, _classify_wave_results
from tests.fixtures.fake_anthropic import FakeBatchResult, FakeBatchResultEnvelope


def _finding(issue: str = "pressure rating lookup") -> Finding:
    return Finding(
        severity="MEDIUM",
        fileName="22 11 00 - Water.docx",
        section="2.1",
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="",
    )


# ===========================================================================
# 1. Cancellation / missing results at the wave parser (verifier level)
# ===========================================================================


def _classify_single(monkeypatch, detailed: dict):
    """Drive the REAL `_classify_wave_results` for one finding and return the
    single outcome. ``detailed`` is what the (patched) batch retrieval yields."""
    import src.verification.verifier as V

    custom_id = "verify__0"
    job = BatchJob(
        batch_id="cancel-test",
        job_type="verify",
        request_map={custom_id: {"model": "claude-sonnet-4-6"}},
        created_at=0.0,
    )
    contexts = {custom_id: {"finding_idx": 0, "model": "claude-sonnet-4-6", "escalated": False}}
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", lambda _job: dict(detailed))
    outcomes = V._classify_wave_results(job=job, findings=[_finding()], request_contexts=contexts)
    assert len(outcomes) == 1
    return outcomes[0]


class TestCanceledBatchItemIsTerminal:
    def test_canceled_item_is_terminal_not_retried(self, monkeypatch):
        # A canceled batch is non-retryable (resubmitting yields the same
        # cancellation), so the wave parser marks it terminal — the wave loop
        # then stamps verification_failed=True (→ VERIFICATION_FAILED).
        canceled = FakeBatchResult(
            custom_id="verify__0",
            result=FakeBatchResultEnvelope(type="canceled"),
        )
        outcome = _classify_single(monkeypatch, {"verify__0": canceled})
        assert outcome.classification == "terminal_unverified"
        assert outcome.failure_class == FailureClass.BATCH_CANCELED

    def test_missing_item_is_retried_not_dropped(self, monkeypatch):
        # Nothing came back for the finding — a transient "missing batch
        # result". It must be retried (a later wave / the tail resolves it),
        # never silently dropped.
        outcome = _classify_single(monkeypatch, {})  # empty: id not present
        assert outcome.classification == "retry"
        assert outcome.failure_class == FailureClass.SERVER_ERROR
        # Still bound to the finding — an outcome was produced, not skipped.
        assert outcome.finding_idx == 0


# ===========================================================================
# 2. Partial failure survives end-to-end into the report + sidecar
# ===========================================================================


class TestPartialFailureSurvivesToReportAndSidecar:
    def _submission(self) -> BatchSubmission:
        job = BatchJob(
            batch_id="batch-1",
            job_type="review",
            request_map={"review__a__0": {"filename": "22 11 00 - Water.docx", "index": 0, "type": "review"}},
            created_at=time.time(),
        )
        return BatchSubmission(
            job=job,
            files_reviewed=["22 11 00 - Water.docx"],
            review_request_ids=["review__a__0"],
            model="claude-opus-4-7",
            prepared_specs=None,
        )

    def _edit_finding(self, issue: str, *, old: str, new: str) -> Finding:
        # An EDIT finding with distinct existing/replacement text carries a
        # real edit proposal (so the sidecar emits it) and a distinct dedup
        # key (so the three never merge).
        return Finding(
            severity="HIGH",
            fileName="22 11 00 - Water.docx",
            section="2.1",
            issue=issue,
            actionType="EDIT",
            existingText=old,
            replacementText=new,
            codeReference="CBC 422",
        )

    def test_mixed_verification_states_all_preserved_and_labeled(self, monkeypatch):
        rr = ReviewResult(
            findings=[
                self._edit_finding("supported finding", old="old A", new="new A"),
                self._edit_finding("failed finding", old="old B", new="new B"),
                self._edit_finding("unchecked finding", old="old C", new="new C"),
            ],
            parse_status="complete",
        )
        monkeypatch.setattr(pl, "retrieve_review_results", lambda job, *, model: {"review__a__0": rr})

        state = collect_review_batch_results(self._submission())
        by_issue = {f.issue: f for f in state.review_result.findings}
        assert set(by_issue) == {"supported finding", "failed finding", "unchecked finding"}, (
            "a review finding was dropped or merged before verification"
        )

        # Simulate the post-verification state of a partially-failed batch:
        by_issue["supported finding"].verification = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://example.test/a"],
            accepted_sources=["https://example.test/a"],
        )
        by_issue["failed finding"].verification = VerificationResult(
            verdict="UNVERIFIED",
            verification_failed=True,
            explanation="Batch request canceled (non-retryable: batch_canceled)",
        )
        by_issue["unchecked finding"].verification = None  # verification never ran

        result = finalize_batch_result(state)

        # (a) No finding is dropped on the way to the PipelineResult.
        assert len(result.review_result.findings) == 3
        statuses = {f.issue: classify_status(f) for f in result.review_result.findings}
        assert statuses["supported finding"] is ReportStatus.VERIFIED_SUPPORTED
        assert statuses["failed finding"] is ReportStatus.VERIFICATION_FAILED
        assert statuses["unchecked finding"] is ReportStatus.NOT_CHECKED

        # (b) The edit sidecar emits all three — the failed finding is NOT
        # omitted, and each entry carries the correct report_status.
        sidecar = build_edit_instructions(result)
        assert sidecar["edit_count"] == 3, "a finding was dropped from the sidecar"
        by_entry = {e["issue"]: e for e in sidecar["edits"]}
        assert by_entry["failed finding"]["report_status"] == "VERIFICATION_FAILED"
        assert by_entry["supported finding"]["report_status"] == "VERIFIED_SUPPORTED"
        assert by_entry["unchecked finding"]["report_status"] == "NOT_CHECKED"
