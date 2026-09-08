"""Concurrent verification shares only grounded, cacheable verdicts."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.batch.batch import BatchJob
from src.modules import DEFAULT_MODULE
from src.orchestration import pipeline
from src.output.report_exporter import _cache_entry_age_days
from src.review.reviewer import Finding
from src.verification.verification_cache import VerificationCache
from src.verification.verifier import VerificationResult


def _finding(issue: str, *, filename: str, severity: str = "HIGH") -> Finding:
    return Finding(
        severity=severity,
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


# ---------------------------------------------------------------------------
# In-process sharing of clean ungrounded verdicts (B-6)
#
# The cache persists grounded verdicts only, so before this every follower of
# a clean-UNVERIFIED leader took over a fresh generation and paid for its own
# call — N equivalent findings cost N sequential rounds. A follower now
# inherits the leader's clean ungrounded terminal in-process
# (``cache_status="shared"``); operational failures and budget exhaustion are
# never inherited (each follower gets its own attempt), grounded verdicts still
# flow only through the cache, and re-rounds are capped at depth 1.
# ---------------------------------------------------------------------------


def _unverified_result(**overrides) -> VerificationResult:
    base = dict(
        verdict="UNVERIFIED",
        explanation="No authoritative source located.",
        grounded=False,
        cache_status="miss",
        web_search_requests=2,
        input_tokens=1_200,
        output_tokens=300,
        cache_creation_input_tokens=4_000,
        cache_read_input_tokens=0,
        call_usage=[{"model": "unit-test-verifier", "escalated": False, "input_tokens": 1_200}],
        model_used="unit-test-verifier",
    )
    base.update(overrides)
    return VerificationResult(**base)


def _identity_prepass(monkeypatch) -> None:
    monkeypatch.setattr(
        pipeline,
        "prepare_findings_for_verification",
        lambda items, **_kwargs: list(items),
    )


def test_equivalent_unverified_findings_share_one_leader_call(monkeypatch):
    cache = VerificationCache()
    _identity_prepass(monkeypatch)
    findings = [
        _finding("Shared ungrounded claim", filename=f"module-{i}.docx")
        for i in range(4)
    ]
    calls: list[Finding] = []

    def fake_verify(finding, **_kwargs):
        calls.append(finding)
        return _unverified_result()

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    pipeline.verify_findings_for_run(findings, transport="realtime", cache=cache)

    assert len(calls) == 1
    assert sorted(f.verification.cache_status for f in findings) == [
        "miss", "shared", "shared", "shared",
    ]
    assert all(f.verification.verdict == "UNVERIFIED" for f in findings)
    # In-process only: the entry store never saw an ungrounded verdict.
    assert cache.stats()["size"] == 0
    assert cache.singleflight.active_count() == 0

    leader = next(f.verification for f in findings if f.verification.cache_status == "miss")
    shared = [f.verification for f in findings if f.verification.cache_status == "shared"]
    for clone in shared:
        assert clone is not leader
        # Not a disk replay: no cache-age badge, no force-refresh hint.
        assert clone.cache_entry_created_ts == 0.0
        assert _cache_entry_age_days(clone) is None
        # The leader's spend is not double-counted on the followers — the
        # cache counters and the per-call list included.
        assert clone.input_tokens == 0 and clone.output_tokens == 0
        assert clone.cache_creation_input_tokens == 0 and clone.cache_read_input_tokens == 0
        assert clone.call_usage == []
        assert clone.retry_telemetry is None
        # The verdict's own evidence rides along.
        assert clone.explanation == leader.explanation
        assert clone.web_search_requests == 2
        assert clone.verification_failed is False and clone.budget_exhausted is False


def test_concurrent_follower_inherits_clean_unverified_in_process(monkeypatch):
    cache = VerificationCache()
    _identity_prepass(monkeypatch)
    findings = [
        _finding("Cross-thread ungrounded claim", filename="module-a.docx"),
        _finding("Cross-thread ungrounded claim", filename="module-b.docx"),
    ]
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    def fake_verify(_finding, **_kwargs):
        nonlocal calls
        with lock:
            calls += 1
        started.set()
        assert release.wait(timeout=3)
        return _unverified_result()

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    with ThreadPoolExecutor(max_workers=2) as pool:
        gate = threading.Barrier(2)

        def run(finding):
            gate.wait(timeout=3)
            pipeline.verify_findings_for_run(
                [finding], transport="realtime", cache=cache
            )

        futures = [pool.submit(run, finding) for finding in findings]
        assert started.wait(timeout=3)
        release.set()
        for future in futures:
            future.result(timeout=5)

    assert calls == 1
    assert sorted(f.verification.cache_status for f in findings) == ["miss", "shared"]
    assert cache.stats()["size"] == 0
    assert cache.singleflight.active_count() == 0


def test_batch_transport_shares_clean_unverified(monkeypatch):
    cache = VerificationCache()
    findings = [
        _finding("Batch ungrounded claim", filename=f"module-{i}.docx")
        for i in range(3)
    ]
    submissions = 0

    def fake_start(items, **_kwargs):
        nonlocal submissions
        submissions += 1
        return BatchJob(
            batch_id="verification-batch",
            job_type="verify",
            request_map={},
            created_at=0.0,
            submitted_findings=list(items),
        )

    def fake_collect(job, _items, **_kwargs):
        for finding in job.submitted_findings:
            finding.verification = _unverified_result()
        return job.submitted_findings

    monkeypatch.setattr(pipeline, "start_batch_verification", fake_start)
    monkeypatch.setattr(pipeline, "collect_batch_verification_results", fake_collect)

    pipeline.verify_findings_for_run(findings, transport="batch", cache=cache)

    assert submissions == 1
    assert sorted(f.verification.cache_status for f in findings) == [
        "miss", "shared", "shared",
    ]
    assert cache.singleflight.active_count() == 0


def test_operational_failure_is_never_inherited(monkeypatch):
    cache = VerificationCache()
    _identity_prepass(monkeypatch)
    findings = [
        _finding("Failing claim", filename=f"module-{i}.docx") for i in range(3)
    ]
    calls: list[Finding] = []

    def fake_verify(finding, **_kwargs):
        calls.append(finding)
        return VerificationResult(
            verdict="UNVERIFIED",
            explanation="Transient verifier failure.",
            verification_failed=True,
            cache_status="miss",
        )

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    pipeline.verify_findings_for_run(findings, transport="realtime", cache=cache)

    # Each follower took its own attempt — a failure is never copied.
    assert len(calls) == 3
    assert {id(f) for f in calls} == {id(f) for f in findings}
    assert all(f.verification.verification_failed for f in findings)
    assert all(f.verification.cache_status == "miss" for f in findings)
    assert cache.singleflight.active_count() == 0


def test_budget_exhaustion_is_never_inherited(monkeypatch):
    cache = VerificationCache()
    _identity_prepass(monkeypatch)
    findings = [
        _finding("Budget-bound claim", filename=f"module-{i}.docx") for i in range(3)
    ]
    calls: list[Finding] = []

    def fake_verify(finding, **_kwargs):
        calls.append(finding)
        return _unverified_result(budget_exhausted=True, web_search_requests=7)

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    pipeline.verify_findings_for_run(findings, transport="realtime", cache=cache)

    assert len(calls) == 3
    assert all(f.verification.budget_exhausted for f in findings)
    assert not any(f.verification.cache_status == "shared" for f in findings)


def test_grounded_verdicts_still_flow_only_through_the_cache(monkeypatch):
    cache = VerificationCache()
    _identity_prepass(monkeypatch)
    findings = [
        _finding("Grounded claim", filename=f"module-{i}.docx") for i in range(3)
    ]
    calls: list[Finding] = []

    def fake_verify(finding, **kwargs):
        calls.append(finding)
        result = _grounded_result()
        kwargs["cache"].put(
            finding,
            cycle=kwargs["cycle"],
            result=result,
            jurisdiction_fingerprint=kwargs["jurisdiction_fingerprint"],
        )
        return result

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    pipeline.verify_findings_for_run(findings, transport="realtime", cache=cache)

    assert len(calls) == 1
    assert sorted(f.verification.cache_status for f in findings) == ["hit", "hit", "miss"]
    assert cache.stats()["size"] == 1
    hits = [f.verification for f in findings if f.verification.cache_status == "hit"]
    assert all(v.cache_entry_created_ts > 0.0 for v in hits)


def test_reround_depth_is_capped_at_one_takeover(monkeypatch):
    """A pathological key never turns N findings into N sequential rounds.

    With a verifier that always fails operationally, every finding still gets
    its own attempt (failures are never inherited), but the rounds are bounded:
    one leader, one takeover generation, then the remainder verify directly in
    a single pass.
    """
    cache = VerificationCache()
    _identity_prepass(monkeypatch)
    findings = [
        _finding("Always failing claim", filename=f"module-{i}.docx") for i in range(6)
    ]
    calls: list[Finding] = []

    def fake_verify(finding, **_kwargs):
        calls.append(finding)
        return VerificationResult(
            verdict="UNVERIFIED",
            explanation="Transient verifier failure.",
            verification_failed=True,
            cache_status="miss",
        )

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)
    rounds: list[int] = []
    original = pipeline._execute_verification_attempts

    def counting(items, **kwargs):
        rounds.append(len(items))
        return original(items, **kwargs)

    monkeypatch.setattr(pipeline, "_execute_verification_attempts", counting)

    pipeline.verify_findings_for_run(findings, transport="realtime", cache=cache)

    assert len(calls) == 6
    # Leader round, one takeover round, then the direct pass for the rest.
    assert rounds == [1, 1, 4]
    assert all(f.verification is not None for f in findings)
    assert all(f.verification.verification_failed for f in findings)
    assert cache.singleflight.active_count() == 0


def test_representative_is_the_highest_severity_member(monkeypatch):
    """Budget and escalation grow with severity, so the paid call is made for
    the most demanding member and the lower-severity siblings inherit."""
    cache = VerificationCache()
    _identity_prepass(monkeypatch)
    medium = _finding("Tiered claim", filename="module-a.docx", severity="MEDIUM")
    critical = _finding("Tiered claim", filename="module-b.docx", severity="CRITICAL")
    seen: list[str] = []

    def fake_verify(finding, **_kwargs):
        seen.append(finding.severity)
        return _unverified_result()

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    pipeline.verify_findings_for_run([medium, critical], transport="realtime", cache=cache)

    assert seen == ["CRITICAL"]
    assert critical.verification.cache_status == "miss"
    assert medium.verification.cache_status == "shared"


def test_higher_severity_follower_does_not_inherit_from_a_lower_leader(monkeypatch):
    cache = VerificationCache()
    _identity_prepass(monkeypatch)
    medium = _finding("Tiered cross-thread claim", filename="module-a.docx", severity="MEDIUM")
    high = _finding("Tiered cross-thread claim", filename="module-b.docx", severity="HIGH")
    leader_started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    seen: list[str] = []

    def fake_verify(finding, **_kwargs):
        with lock:
            seen.append(finding.severity)
        if finding.severity == "MEDIUM":
            leader_started.set()
            assert release.wait(timeout=3)
        return _unverified_result()

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader_future = pool.submit(
            pipeline.verify_findings_for_run, [medium], transport="realtime", cache=cache
        )
        assert leader_started.wait(timeout=3)
        follower_future = pool.submit(
            pipeline.verify_findings_for_run, [high], transport="realtime", cache=cache
        )
        release.set()
        leader_future.result(timeout=5)
        follower_future.result(timeout=5)

    # The HIGH follower waited on the MEDIUM leader, then took its own attempt.
    assert sorted(seen) == ["HIGH", "MEDIUM"]
    assert medium.verification.cache_status == "miss"
    assert high.verification.cache_status == "miss"
    assert cache.singleflight.active_count() == 0


# ---------------------------------------------------------------------------
# Bounded follower wait (a leader that dies without ``complete``)
# ---------------------------------------------------------------------------


def test_wait_returns_false_when_the_bound_elapses():
    from src.verification.verification_cache import VerificationSingleFlight

    coordinator = VerificationSingleFlight()
    leader = coordinator.claim_many(["k"])["k"]
    follower = coordinator.claim_many(["k"])["k"]
    assert leader.leader and not follower.leader

    assert coordinator.wait(follower, timeout=0.05) is False
    assert coordinator.active_count() == 1  # the generation is still registered

    coordinator.complete(leader)
    assert coordinator.wait(follower, timeout=0.05) is True
    assert coordinator.active_count() == 0


def test_wait_default_bound_comes_from_the_env(monkeypatch):
    from src.verification.verification_cache import VerificationSingleFlight

    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS", "0.05")
    coordinator = VerificationSingleFlight()
    coordinator.claim_many(["k"])
    follower = coordinator.claim_many(["k"])["k"]
    assert coordinator.wait(follower) is False


def test_follower_times_out_and_verifies_independently_without_double_write(monkeypatch):
    """Leader never completes within the bound → the follower logs a WARNING,
    runs its own verification, and is stamped exactly once (its own result);
    the leader's later completion touches only the leader's finding."""
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS", "0.2")
    cache = VerificationCache()
    leader_finding = _finding("Check the hanger spacing rule", filename="module-a.docx")
    follower_finding = _finding("Check the hanger spacing rule", filename="module-b.docx")
    monkeypatch.setattr(
        pipeline,
        "prepare_findings_for_verification",
        lambda items, **_kwargs: list(items),
    )

    leader_started = threading.Event()
    release_leader = threading.Event()
    lock = threading.Lock()
    calls: list[str] = []

    def fake_verify(finding, **kwargs):
        with lock:
            calls.append(finding.fileName)
        if finding is leader_finding:
            leader_started.set()
            assert release_leader.wait(timeout=10)  # "dead" leader: stuck past the bound
        result = VerificationResult(
            verdict="UNVERIFIED",
            explanation=f"own attempt for {finding.fileName}",
            grounded=False,
            cache_status="miss",
        )
        return result

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    log_lines: list[tuple[str, str]] = []
    log_lock = threading.Lock()

    def log(msg, **kw):
        with log_lock:
            log_lines.append((str(kw.get("level", "info")), str(msg)))

    def run(finding):
        pipeline.verify_findings_for_run(
            [finding], transport="realtime", cache=cache, log=log
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader_future = pool.submit(run, leader_finding)
        assert leader_started.wait(timeout=5)
        follower_future = pool.submit(run, follower_finding)
        # The follower must finish while the leader is still stuck.
        follower_future.result(timeout=10)
        assert not leader_future.done()

        assert follower_finding.verification is not None
        follower_result = follower_finding.verification
        assert follower_result.explanation == "own attempt for module-b.docx"
        assert follower_result.cache_status == "miss"
        assert sorted(calls) == ["module-a.docx", "module-b.docx"]
        assert any(
            level == "warning" and "did not complete within" in msg
            for level, msg in log_lines
        )

        release_leader.set()
        leader_future.result(timeout=10)

    # Exactly-once: the follower's result object is untouched by the leader's
    # completion, and the leader stamped only its own finding.
    assert follower_finding.verification is follower_result
    assert leader_finding.verification.explanation == "own attempt for module-a.docx"
    assert cache.singleflight.active_count() == 0
    assert cache.stats()["size"] == 0  # both ungrounded → nothing cached


def test_follower_that_times_out_still_reuses_a_late_cache_fill(monkeypatch):
    """If the leader filled the cache but never called ``complete`` (crashed
    between the two), the timed-out follower replays the grounded entry
    rather than paying again."""
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS", "0.1")
    cache = VerificationCache()
    finding = _finding("Check the seismic bracing rule", filename="module-b.docx")
    twin = _finding("Check the seismic bracing rule", filename="module-a.docx")
    monkeypatch.setattr(
        pipeline,
        "prepare_findings_for_verification",
        lambda items, **_kwargs: list(items),
    )
    calls = 0

    def fake_verify(*_a, **_k):
        nonlocal calls
        calls += 1
        raise AssertionError("must not be called: a grounded entry exists")

    monkeypatch.setattr(pipeline, "verify_finding", fake_verify)

    # A leader claims the key and fills the cache, then "dies".
    key = pipeline.make_cache_key(twin, cycle=DEFAULT_MODULE.cycle)
    cache.singleflight.claim_many([key])
    cache.put(twin, cycle=DEFAULT_MODULE.cycle, result=_grounded_result())

    pipeline.verify_findings_for_run([finding], transport="realtime", cache=cache)

    assert calls == 0
    assert finding.verification.cache_status == "hit"
    assert finding.verification.grounded
