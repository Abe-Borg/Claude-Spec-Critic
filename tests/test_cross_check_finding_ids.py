"""Cross-check (coordination) findings get stable, namespaced ids.

STRUCTURAL_AUDIT P1-1: cross-check findings are produced *after* the review
dedup pass and never flow through ``_deduplicate_findings`` (the only place
review findings are id-stamped), so without ``assign_cross_check_finding_ids``
every coordination finding reaches the edit sidecar with ``finding_id=""`` —
all colliding on the empty key. These tests pin the fix both at the unit
level (the stamping helper) and end-to-end (through ``run_cross_check_for_batch``
into the sidecar).
"""
from __future__ import annotations

from pathlib import Path

from src.batch.batch import BatchJob
from src.input.extractor import ExtractedSpec
from src.orchestration.pipeline import (
    BatchSubmission,
    CollectedBatchState,
    assign_cross_check_finding_ids,
    compute_finding_id,
    finalize_batch_result,
    run_cross_check_for_batch,
)
from src.output.edit_sidecar import build_edit_instructions
from src.review.reviewer import Finding, ReviewResult


def _cc_finding(*, section: str, issue: str = "Coordination conflict") -> Finding:
    """An edit-bearing coordination finding with no id (pre-stamp shape)."""
    return Finding(
        severity="HIGH",
        fileName="",  # coordination findings are cross-spec; no single file
        section=section,
        issue=issue,
        actionType="EDIT",
        existingText="old text",
        replacementText="new text",
        codeReference=None,
        confidence=0.8,
    )


# --- unit: the stamping helper -------------------------------------------


def test_assign_stamps_cf_ids_on_unstamped_findings():
    findings = [_cc_finding(section="2.1"), _cc_finding(section="3.4")]
    out = assign_cross_check_finding_ids(findings)

    assert out is findings  # mutates in place, returns same list for chaining
    assert all(f.finding_id.startswith("cf-") for f in findings)
    assert all(len(f.finding_id) == len("cf-") + 12 for f in findings)
    # Distinct content -> distinct id.
    assert findings[0].finding_id != findings[1].finding_id


def test_assign_is_stable_and_idempotent():
    f1 = _cc_finding(section="2.1")
    assign_cross_check_finding_ids([f1])
    first = f1.finding_id

    # Re-running leaves an already-stamped id untouched.
    assign_cross_check_finding_ids([f1])
    assert f1.finding_id == first

    # Same content on a fresh finding -> same id (stable across runs).
    f2 = _cc_finding(section="2.1")
    assign_cross_check_finding_ids([f2])
    assert f2.finding_id == first


def test_assign_does_not_overwrite_existing_id():
    f = _cc_finding(section="2.1")
    f.finding_id = "rf-deadbeef0000"
    assign_cross_check_finding_ids([f])
    assert f.finding_id == "rf-deadbeef0000"


def test_cf_and_rf_ids_share_digest_but_never_collide():
    # A coordination finding with the SAME content as a review finding hashes
    # to the same digest, but the prefix namespacing keeps the two ids
    # distinct so they never collapse into one sidecar entry.
    review = _cc_finding(section="2.1", issue="Same content")
    review.finding_id = compute_finding_id(review)  # rf-
    coord = _cc_finding(section="2.1", issue="Same content")
    assign_cross_check_finding_ids([coord])  # cf-

    assert review.finding_id.startswith("rf-")
    assert coord.finding_id.startswith("cf-")
    assert review.finding_id != coord.finding_id
    assert review.finding_id[3:] == coord.finding_id[3:]  # shared digest tail


# --- integration: through the batch wiring into the sidecar ---------------


def test_cross_check_finding_ids_flow_to_sidecar(monkeypatch):
    from src.orchestration import pipeline

    # A review finding, id-stamped as it would be post review-collect.
    review_finding = _cc_finding(section="2.1", issue="Same content")
    review_finding.finding_id = compute_finding_id(review_finding)
    review_result = ReviewResult(findings=[review_finding])

    # Two coordination findings with NO id. c1 deliberately shares content
    # with the review finding to exercise the cross-type collision guard.
    c1 = _cc_finding(section="2.1", issue="Same content")
    c2 = _cc_finding(section="9.9", issue="Different coordination issue")

    def _fake_cross_check(specs, existing, **kw):
        return ReviewResult(findings=[c1, c2], cross_check_status="completed")

    monkeypatch.setattr(pipeline, "run_chunked_cross_check", _fake_cross_check)

    spec = ExtractedSpec(filename="23 00 00.docx", content="HVAC body", word_count=2)
    submission = BatchSubmission(
        job=BatchJob(batch_id="b1", job_type="review", request_map={}, created_at=0.0),
        cross_check_enabled=True,
        prepared_specs=[spec],
    )
    state = CollectedBatchState(submission=submission, review_result=review_result)

    state = run_cross_check_for_batch(state, specs=[spec])

    # Every coordination finding now carries a non-empty cf- id.
    assert state.cross_check_result is not None
    assert all(f.finding_id.startswith("cf-") for f in state.cross_check_result.findings)

    result = finalize_batch_result(state)
    payload = build_edit_instructions(result, report_path=Path("r.docx"))

    ids = [e["finding_id"] for e in payload["edits"]]
    assert all(ids), f"empty finding_id leaked into sidecar: {ids}"
    assert len(ids) == len(set(ids)), f"duplicate finding_id in sidecar: {ids}"
    # The review finding (rf-) and the two coordination findings (cf-) all
    # appear; the same-content review/coordination pair does not collide.
    assert sum(1 for i in ids if i.startswith("rf-")) == 1
    assert sum(1 for i in ids if i.startswith("cf-")) == 2
