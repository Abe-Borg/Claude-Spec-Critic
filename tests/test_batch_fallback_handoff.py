"""STRUCTURAL_AUDIT P1-2: the batch → real-time fallback handoff is exactly-once.

The default verification path (``collect_verification_batch_results``) submits
unresolved findings across batch waves and, on the *final* wave, flips a small
remaining tail to the real-time ``verify_finding`` path (the "real-time
fallback", gated by ``_REALTIME_FALLBACK_THRESHOLD``). The audit flagged that
it had NOT read this handoff end-to-end and could not rule out two failure
modes on a tail finding:

* **double-processed** — both written back by a batch wave *and* overwritten by
  the real-time fallback (last-writer-wins on ``finding.verification``), or
* **dropped by both** — abandoned by the batch wave loop without the fallback
  (or the post-loop safety net) ever assigning a verdict.

These tests trace the three terminal last-wave paths and assert each finding
ends with **exactly one** ``VerificationResult``:

1. fallback ENABLED  → the tail resolves via the real-time path (once each), a
   finding that already succeeded in a batch wave is NOT re-run, and the
   fallback does not trigger an extra batch submission (the submit path and the
   fallback path are mutually exclusive).
2. fallback DISABLED → the tail resolves via the batch-exhaustion marker (once
   each); the real-time path is never invoked.
3. final wave DETACHES → the post-loop safety net assigns each still-unresolved
   finding exactly one terminal UNVERIFIED (nothing dropped); fallback not run.

The wave fixtures force a retryable ``SERVER_ERROR`` (a "missing batch result")
on findings 1 & 2 across both waves, which the ``BatchWaveFailureTracker``
routes to ``needs_retry`` on wave 1 (so they are genuinely *submitted* to an
in-flight wave 2) and then to ``tracker_terminated`` on wave 2 (so they reach
the unresolved tail) — i.e. exactly the "submitted to a wave AND eligible for
real-time" situation the audit could not rule out.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.tracing import activate_span
from src.tracing.spans import KIND_PIPELINE, SpanHandle
from src.verification.verifier import (
    DEFAULT_VERIFICATION_POLL_POLICY,
    VerificationResult,
    collect_verification_batch_results,
)
from tests.fixtures.fake_anthropic import (
    batch_verification_result,
    sample_verification_verdict_payload,
    verification_tool_use_response,
)

_SENTINEL = "REALTIME_FALLBACK_SENTINEL"
_SAFETY_NET = "No verification result after all batch waves."


def _nolog(*_a, **_k) -> None:
    pass


def _noprog(_p: float, _m: str) -> None:
    pass


def _finding(idx: int) -> Finding:
    """A minimal substantive finding.

    MEDIUM severity (with no ``codeReference``) keeps it out of the post-loop
    escalation wave, so these tests exercise ONLY the wave loop + fallback
    handoff — escalation parity is covered by ``test_batch_escalation``.
    """
    return Finding(
        severity="MEDIUM",
        fileName="23 22 00 - Steam.docx",
        section=f"2.{idx}",
        issue=f"Finding {idx}: pressure rating lookup",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        confidence=0.5,
        codeReference="",
    )


def _grounded_success_message(verdict: str = "CONFIRMED"):
    """A verifier tool-use response the BATCH parser treats as grounded.

    Mirrors ``test_batch_escalation._opus_grounded_message``: the wave parser's
    search gate requires BOTH a ``web_search_tool_result`` block (the fixture
    provides one, with a URL the payload also cites) AND
    ``usage.server_tool_use.web_search_requests > 0``.
    """
    msg = verification_tool_use_response(
        payload=sample_verification_verdict_payload(verdict=verdict)
    )
    msg.usage = SimpleNamespace(
        input_tokens=120,
        output_tokens=60,
        server_tool_use=SimpleNamespace(web_search_requests=2, web_fetch_requests=0),
    )
    return msg


def _results_one_success_rest_missing(custom_id: str):
    """``verify__0`` succeeds; every other id (incl. retry re-stamps) is missing.

    A ``None`` result is treated by ``_classify_wave_results`` as a "missing
    batch result" → retryable ``SERVER_ERROR``, so findings 1 & 2 never resolve
    in the batch and fall through to the last-wave tail.
    """
    if custom_id == "verify__0":
        return batch_verification_result(
            custom_id=custom_id, message=_grounded_success_message("CONFIRMED")
        )
    return None


def _sentinel_result(_finding) -> VerificationResult:
    """An unmistakable stand-in for the real-time ``verify_finding`` verdict."""
    return VerificationResult(
        verdict="CORRECTED",
        explanation=_SENTINEL,
        grounded=True,
        sources=["https://example.test/rt-fallback"],
        accepted_sources=["https://example.test/rt-fallback"],
        model_used="rt-fallback-model",
        cache_status="miss",
        # The real-time path escalates inline and stamps this flag; setting it
        # keeps the post-loop batch escalation wave from re-touching the
        # finding (mirrors production behavior).
        escalation_attempted=True,
    )


def _init_job(findings):
    """The initial verification BatchJob: one custom_id per finding."""
    return SimpleNamespace(
        batch_id="init-batch",
        request_map={f"verify__{i}": {"finding_idx": i} for i in range(len(findings))},
        job_type="verify",
    )


def _install_wave_mocks(
    monkeypatch,
    *,
    results_by_id,
    fallback_factory=None,
    detach_batch_ids=(),
):
    """Patch the batch primitives + the real-time fallback entrypoint.

    * ``results_by_id(custom_id) -> FakeBatchResult | None`` drives each wave's
      retrieval.
    * ``fallback_factory(finding) -> VerificationResult`` stands in for
      ``verify_finding``; when ``None`` the fallback path is asserted-unreached
      (the fake records the call before raising, so a stray invocation is
      caught by the ``fallback_findings`` assertion even though the wave loop
      swallows worker exceptions).
    * ``detach_batch_ids`` forces ``poll_batch_bounded`` to report *detached*
      for those batch ids (to exercise the post-loop safety-net path).

    Returns a ``recorded`` dict with ``submit_calls`` (one per follow-up wave)
    and ``fallback_findings`` (the Finding objects handed to ``verify_finding``).
    """
    import src.verification.verifier as V

    recorded = {
        "submit_calls": [],
        "fallback_findings": [],
        "fallback_trace_parents": [],
    }
    lock = threading.Lock()
    submit_counter = {"n": 0}

    def fake_poll(batch_id, *, policy, log, progress_cb):
        return SimpleNamespace(
            detached=batch_id in detach_batch_ids, poll_failed=False
        )

    def fake_retrieve(job):
        return {cid: results_by_id(cid) for cid in job.request_map}

    def fake_submit(requests, request_map, *, extra_headers=None):
        with lock:
            submit_counter["n"] += 1
            n = submit_counter["n"]
            recorded["submit_calls"].append(request_map)
        return SimpleNamespace(
            batch_id=f"wave{n + 1}-batch", request_map=request_map, job_type="verify"
        )

    def fake_verify_finding(finding, **_kwargs):
        # Record BEFORE any raise so a stray fallback call is observable even
        # though the wave loop catches worker exceptions.
        with lock:
            recorded["fallback_findings"].append(finding)
            recorded["fallback_trace_parents"].append(
                _kwargs.get("_trace_parent")
            )
        if fallback_factory is None:
            raise AssertionError("real-time fallback was invoked unexpectedly")
        return fallback_factory(finding)

    monkeypatch.setattr(V, "poll_batch_bounded", fake_poll)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    monkeypatch.setattr(V, "submit_verification_followup_wave", fake_submit)
    monkeypatch.setattr(V, "verify_finding", fake_verify_finding)
    return recorded


def _collect(
    findings,
    *,
    max_waves=2,
    fallback_threshold=5,
    api_call_semaphore=None,
):
    return collect_verification_batch_results(
        _init_job(findings),
        findings,
        log=_nolog,
        progress=_noprog,
        cycle=DEFAULT_CYCLE,
        poll_policy=DEFAULT_VERIFICATION_POLL_POLICY,
        max_waves=max_waves,
        cache=None,
        realtime_fallback_threshold=fallback_threshold,
        api_call_semaphore=api_call_semaphore,
    )


def _assert_every_finding_has_one_terminal_result(findings):
    """No finding is dropped: each ends with a non-None VerificationResult."""
    assert all(f.verification is not None for f in findings)


def test_tail_flips_to_realtime_exactly_once(monkeypatch):
    """Fallback ENABLED: the tail resolves via the real-time path, once each."""
    findings = [_finding(0), _finding(1), _finding(2)]
    recorded = _install_wave_mocks(
        monkeypatch,
        results_by_id=_results_one_success_rest_missing,
        fallback_factory=_sentinel_result,
    )

    _collect(findings, max_waves=2, fallback_threshold=5)

    _assert_every_finding_has_one_terminal_result(findings)
    # No finding fell through to the post-loop safety net (would mean it was
    # never assigned a verdict by either the wave loop or the fallback).
    assert all(_SAFETY_NET not in (f.verification.explanation or "") for f in findings)

    # finding 0 resolved inside the batch wave → kept its batch verdict and was
    # NEVER handed to the real-time fallback (no double-processing of a finding
    # the batch already resolved).
    assert findings[0].verification.verdict == "CONFIRMED"
    assert findings[0].verification.explanation != _SENTINEL

    # The unresolved tail (findings 1 & 2) resolved via the real-time path.
    assert findings[1].verification.explanation == _SENTINEL
    assert findings[2].verification.explanation == _SENTINEL
    assert findings[1].verification.verdict == "CORRECTED"

    # Exactly-once: each tail finding was handed to verify_finding exactly once,
    # and finding 0 never was (no double-write, no missed write).
    fb_ids = [id(f) for f in recorded["fallback_findings"]]
    assert fb_ids.count(id(findings[1])) == 1
    assert fb_ids.count(id(findings[2])) == 1
    assert id(findings[0]) not in fb_ids
    assert len(recorded["fallback_findings"]) == 2

    # The fallback path did NOT also re-submit a batch wave: exactly one
    # follow-up wave (wave 1 → wave 2) was submitted. The submit path and the
    # fallback path are mutually exclusive on the final wave.
    assert len(recorded["submit_calls"]) == 1


def test_realtime_tail_uses_shared_program_api_permit(monkeypatch):
    """Every synchronous fallback lifecycle runs under the shared permit."""

    class TrackingSemaphore:
        def __init__(self) -> None:
            self._semaphore = threading.BoundedSemaphore(1)
            self._local = threading.local()
            self._lock = threading.Lock()
            self.entries = 0

        def __enter__(self):
            self._semaphore.acquire()
            self._local.held = True
            with self._lock:
                self.entries += 1
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self._local.held = False
            self._semaphore.release()

        def held_by_current_thread(self) -> bool:
            return bool(getattr(self._local, "held", False))

    permits = TrackingSemaphore()

    def guarded_fallback(finding):
        assert permits.held_by_current_thread()
        return _sentinel_result(finding)

    findings = [_finding(0), _finding(1), _finding(2)]
    recorded = _install_wave_mocks(
        monkeypatch,
        results_by_id=_results_one_success_rest_missing,
        fallback_factory=guarded_fallback,
    )

    _collect(
        findings,
        max_waves=2,
        fallback_threshold=5,
        api_call_semaphore=permits,
    )

    assert permits.entries == 2
    assert len(recorded["fallback_findings"]) == 2
    assert all(
        finding.verification.explanation == _SENTINEL
        for finding in findings[1:]
    )


def test_realtime_tail_receives_active_module_trace_parent(monkeypatch):
    """Fallback executor workers keep the routed child pipeline parent."""

    parent = SpanHandle(
        span_id="batch-module-pipeline",
        kind=KIND_PIPELINE,
        started_at=1.0,
    )
    findings = [_finding(0), _finding(1), _finding(2)]
    recorded = _install_wave_mocks(
        monkeypatch,
        results_by_id=_results_one_success_rest_missing,
        fallback_factory=_sentinel_result,
    )

    with activate_span(parent):
        _collect(findings, max_waves=2, fallback_threshold=5)

    assert recorded["fallback_trace_parents"] == [parent, parent]


def test_fallback_disabled_marks_tail_unverified_exactly_once(monkeypatch):
    """Fallback DISABLED (threshold=0): the tail resolves via the batch marker."""
    findings = [_finding(0), _finding(1), _finding(2)]
    recorded = _install_wave_mocks(
        monkeypatch,
        results_by_id=_results_one_success_rest_missing,
        fallback_factory=None,  # the real-time path must NOT run
    )

    _collect(findings, max_waves=2, fallback_threshold=0)

    # The real-time fallback was never invoked.
    assert recorded["fallback_findings"] == []

    _assert_every_finding_has_one_terminal_result(findings)
    assert findings[0].verification.verdict == "CONFIRMED"

    # Each tail finding gets exactly one terminal UNVERIFIED via the
    # batch-exhaustion marker — distinct from the fallback path, never both.
    for tail in (findings[1], findings[2]):
        assert tail.verification.verdict == "UNVERIFIED"
        assert "unresolved after 2 batch waves" in (tail.verification.explanation or "")
        # Repeated SERVER_ERROR is operational → routed to VERIFICATION_FAILED.
        assert tail.verification.verification_failed is True

    assert len(recorded["submit_calls"]) == 1


def test_detached_final_wave_safety_net_exactly_once(monkeypatch):
    """Final wave DETACHES: the post-loop safety net assigns one verdict each."""
    findings = [_finding(0), _finding(1), _finding(2)]
    recorded = _install_wave_mocks(
        monkeypatch,
        results_by_id=_results_one_success_rest_missing,
        fallback_factory=None,  # the detach short-circuits before any fallback
        detach_batch_ids=("wave2-batch",),
    )

    _collect(findings, max_waves=2, fallback_threshold=5)

    # Detach happens before the fallback branch, so the real-time path never ran.
    assert recorded["fallback_findings"] == []

    # finding 0 resolved on wave 1, before the wave-2 detach.
    assert findings[0].verification.verdict == "CONFIRMED"

    # findings 1 & 2 were in-flight on the detached wave 2; the post-loop safety
    # net assigns each exactly one terminal UNVERIFIED (nothing dropped, nothing
    # double-written — the abandoned wave never writes back).
    _assert_every_finding_has_one_terminal_result(findings)
    for tail in (findings[1], findings[2]):
        assert tail.verification.verdict == "UNVERIFIED"
        assert tail.verification.explanation == _SAFETY_NET

    # One follow-up wave was submitted (wave 1 → wave 2) before the detach.
    assert len(recorded["submit_calls"]) == 1
