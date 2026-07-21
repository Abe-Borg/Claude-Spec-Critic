"""Concurrent verification shares only grounded, cacheable verdicts."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.batch.batch import BatchJob
from src.modules import DEFAULT_MODULE
from src.orchestration import pipeline
from src.review.reviewer import Finding
from src.verification.verification_cache import VerificationCache
from src.verification.verifier import VerificationResult


def _finding(issue: str, *, filename: str) -> Finding:
    return Finding(
        severity="HIGH",
        fileName=filename,
        section="2.1",
        issue=issue,
        actionType="REPORT_ONLY",
        existingText="Existing requirement",
        replacementText=None,
        confidence=0.8,
        codeReference="NFPA 13 section 9.3",
    )


def _grounded_result() -> VerificationResult:
    source = "https://example.gov/adopted-standard"
    return VerificationResult(
        verdict="CONFIRMED",
        explanation="Confirmed by the adopted standard.",
        sources=[source],
        grounded=True,
        cache_status="miss",
        searched_sources=[source],
        cited_sources=[source],
        accepted_sources=[source],
        source_quote="The adopted standard requires this condition.",
    )


def _run_together(*calls) -> None:
    gate = threading.Barrier(len(calls))

    def run(call):
        gate.wait(timeout=3)
        call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(run, call) for call in calls]
        for future in futures:
            future.result(timeout=5)


def test_realtime_concurrent_equivalents_share_one_grounded_call(monkeypatch):
    cache = VerificationCache()
    findings = [
        _finding("Check the seismic bracing rule", filename="module-a.docx"),
        _finding("Check the seismic bracing rule", filename="module-b.docx"),
    ]
    monkeypatch.setattr(
        pipeline,
        "prepare_findings_for_verification",
        lambda items, **_kwargs: list(items),
    )

    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    def fake_verify(finding, **kwargs):
        nonlocal calls
        with lock:
            calls += 1
        started.set()
        assert release.wait(timeout=3)
        result = _grounded_result()
        kwargs["cache"].put(
            finding,
            cycle=kwargs["cycle"],
            result=result,
            jurisdiction_fingerprint=kwargs["jurisdiction_fingerprint"],
        )
        return result

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    def run(finding):
        pipeline.verify_findings_for_run(
            [finding], transport="realtime", cache=cache
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        gate = threading.Barrier(2)

        def gated(finding):
            gate.wait(timeout=3)
            run(finding)

        futures = [pool.submit(gated, finding) for finding in findings]
        assert started.wait(timeout=3)
        release.set()
        for future in futures:
            future.result(timeout=5)

    assert calls == 1
    assert sorted(f.verification.cache_status for f in findings) == ["hit", "miss"]
    assert all(f.verification.grounded for f in findings)
    assert cache.singleflight.active_count() == 0


def test_batch_concurrent_equivalents_share_one_grounded_batch(monkeypatch):
    cache = VerificationCache()
    findings = [
        _finding("Check the batch-only claim", filename="module-a.docx"),
        _finding("Check the batch-only claim", filename="module-b.docx"),
    ]
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    submissions = 0

    def fake_start(items, **_kwargs):
        nonlocal submissions
        with lock:
            submissions += 1
        started.set()
        assert release.wait(timeout=3)
        return BatchJob(
            batch_id="verification-batch",
            job_type="verify",
            request_map={},
            created_at=0.0,
            submitted_findings=list(items),
        )

    def fake_collect(job, _items, **kwargs):
        for finding in job.submitted_findings:
            result = _grounded_result()
            finding.verification = result
            kwargs["cache"].put(
                finding,
                cycle=kwargs["module"].cycle,
                result=result,
                jurisdiction_fingerprint=kwargs["jurisdiction_fingerprint"],
            )
        return job.submitted_findings

    monkeypatch.setattr(pipeline, "start_batch_verification", fake_start)
    monkeypatch.setattr(
        pipeline, "collect_batch_verification_results", fake_collect
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        gate = threading.Barrier(2)

        def run(finding):
            gate.wait(timeout=3)
            pipeline.verify_findings_for_run(
                [finding], transport="batch", cache=cache
            )

        futures = [pool.submit(run, finding) for finding in findings]
        assert started.wait(timeout=3)
        release.set()
        for future in futures:
            future.result(timeout=5)

    assert submissions == 1
    assert sorted(f.verification.cache_status for f in findings) == ["hit", "miss"]
    assert all(f.verification.grounded for f in findings)
    assert cache.singleflight.active_count() == 0


def test_non_cacheable_leader_allows_exactly_one_waiter_takeover(monkeypatch):
    cache = VerificationCache()
    findings = [
        _finding("Check the takeover claim", filename="module-a.docx"),
        _finding("Check the takeover claim", filename="module-b.docx"),
    ]
    monkeypatch.setattr(
        pipeline,
        "prepare_findings_for_verification",
        lambda items, **_kwargs: list(items),
    )
    first_started = threading.Event()
    release_first = threading.Event()
    lock = threading.Lock()
    calls = 0

    def fake_verify(finding, **kwargs):
        nonlocal calls
        with lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_started.set()
            assert release_first.wait(timeout=3)
            return VerificationResult(
                verdict="UNVERIFIED",
                explanation="Transient verifier failure.",
                verification_failed=True,
                cache_status="miss",
            )
        result = _grounded_result()
        kwargs["cache"].put(
            finding,
            cycle=kwargs["cycle"],
            result=result,
            jurisdiction_fingerprint=kwargs["jurisdiction_fingerprint"],
        )
        return result

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    with ThreadPoolExecutor(max_workers=2) as pool:
        gate = threading.Barrier(2)

        def run(finding):
            gate.wait(timeout=3)
            pipeline.verify_findings_for_run(
                [finding], transport="realtime", cache=cache
            )

        futures = [pool.submit(run, finding) for finding in findings]
        assert first_started.wait(timeout=3)
        release_first.set()
        for future in futures:
            future.result(timeout=5)

    assert calls == 2
    assert sorted(f.verification.verdict for f in findings) == [
        "CONFIRMED",
        "UNVERIFIED",
    ]
    assert cache.singleflight.active_count() == 0


def test_opposite_key_order_is_deadlock_free_and_deduplicated(monkeypatch):
    cache = VerificationCache()
    a1 = _finding("Claim A", filename="module-a.docx")
    b1 = _finding("Claim B", filename="module-a.docx")
    a2 = _finding("Claim A", filename="module-b.docx")
    b2 = _finding("Claim B", filename="module-b.docx")
    monkeypatch.setattr(
        pipeline,
        "prepare_findings_for_verification",
        lambda items, **_kwargs: list(items),
    )

    both_started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    def fake_verify(finding, **kwargs):
        nonlocal calls
        with lock:
            calls += 1
            if calls == 2:
                both_started.set()
        assert release.wait(timeout=3)
        result = _grounded_result()
        kwargs["cache"].put(
            finding,
            cycle=kwargs["cycle"],
            result=result,
            jurisdiction_fingerprint=kwargs["jurisdiction_fingerprint"],
        )
        return result

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    with ThreadPoolExecutor(max_workers=2) as pool:
        gate = threading.Barrier(2)

        def run(items):
            gate.wait(timeout=3)
            pipeline.verify_findings_for_run(
                items, transport="realtime", cache=cache
            )

        futures = [pool.submit(run, [a1, b1]), pool.submit(run, [b2, a2])]
        assert both_started.wait(timeout=3)
        release.set()
        for future in futures:
            future.result(timeout=5)

    assert calls == 2
    assert sorted(
        f.verification.cache_status for f in (a1, b1, a2, b2)
    ) == ["hit", "hit", "miss", "miss"]
    assert cache.singleflight.active_count() == 0


def test_leader_claim_is_released_when_cache_lookup_raises(monkeypatch):
    cache = VerificationCache()
    findings = [
        _finding("Cache lookup failure A", filename="module-a.docx"),
        _finding("Cache lookup failure B", filename="module-a.docx"),
    ]

    def broken_get(*_args, **_kwargs):
        raise RuntimeError("cache backend failed")

    monkeypatch.setattr(cache, "get", broken_get)

    with pytest.raises(RuntimeError, match="cache backend failed"):
        pipeline.verify_findings_for_run(
            findings, transport="realtime", cache=cache
        )

    assert cache.singleflight.active_count() == 0
