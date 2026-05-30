"""STRUCTURAL_AUDIT P2-1: the batch continuation cap is real-time parity, not an off-by-one.

The audit flagged ``verifier.py``'s batch continuation check
``if continuation_counts[stable_key] > cap`` as a suspected off-by-one and
suggested ``>=``. Reading both paths end-to-end shows ``>`` is *correct*: it
gives the batch wave loop EXACT parity with the real-time pause-turn loop's
budget, and ``>=`` would be the regression (one fewer continuation than
real-time).

Parity proof (default ``max_continuations == 2``):

* Real-time (``verifier.py`` ~:1697): ``for _ in range(max_continuations + 1)``
  → one initial call + up to ``max_continuations`` resumes; terminal on pause
  #(cap+1). Equivalently: it submits a resume for pause #k iff ``k <= cap``.
* Batch (``> cap``): when pause #k is observed (``continuation_counts == k``),
  it submits a follow-up wave iff ``k > cap`` is False, i.e. iff ``k <= cap``.
  Identical rule → a pause-turn-only finding rides ``cap + 1`` waves (one
  initial + ``cap`` continuations) before terminating, matching real-time.

So with the default cap of 2, a finding that pause_turns on every wave is
submitted to exactly 2 follow-up waves (3 total attempts) and then goes
terminal UNVERIFIED with a "continuation cap exceeded" reason. Under ``>=``
it would be submitted to only 1 follow-up wave (2 attempts) — these tests
assert the parity counts so a flip to ``>=`` fails loudly.

The cap is separately clamped by ``MAX_VERIFICATION_WAVES`` (3); the tests use
a larger ``max_waves`` so the *cap* (not wave exhaustion) is provably the
binding constraint — the terminal reason is "continuation cap exceeded", not
"unresolved after N batch waves".
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from src.core.code_cycles import DEFAULT_CYCLE
from src.output.report_status import ReportStatus, classify_status, is_budget_exhausted
from src.review.reviewer import Finding
from src.verification.retry_policy import DEFAULT_MAX_CONTINUATIONS
from src.verification.verifier import (
    DEFAULT_VERIFICATION_POLL_POLICY,
    VerificationResult,
    collect_verification_batch_results,
)
from tests.fixtures.fake_anthropic import (
    sample_verification_verdict_payload,
    verification_tool_use_response,
)
from tests.fixtures.fake_anthropic import FakeBatchResult, FakeBatchResultEnvelope

_SAFETY_NET = "No verification result after all batch waves."


def _nolog(*_a, **_k) -> None:
    pass


def _noprog(_p: float, _m: str) -> None:
    pass


def _finding(idx: int = 0) -> Finding:
    """A minimal substantive finding.

    MEDIUM severity with no ``codeReference`` keeps it out of the post-loop
    escalation wave (mirrors ``test_batch_fallback_handoff._finding``), so
    these tests exercise ONLY the wave loop's continuation accounting.
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


def _pause_message():
    """A verifier message the BATCH parser classifies as ``continue``.

    ``stop_reason="pause_turn"`` → ``classify_verification_stop_reason`` →
    ``STOP_CLASS_PAUSE`` → ``_classify_wave_results`` emits a ``continue``
    outcome (the content blocks are carried into the next wave's continuation
    request; their contents are irrelevant to the cap accounting under test).
    """
    return verification_tool_use_response(
        payload=sample_verification_verdict_payload(),
        stop_reason="pause_turn",
    )


def _pause_result(custom_id: str) -> FakeBatchResult:
    """Every id (initial ``verify__0`` and each ``verify_cont_N__...`` re-stamp)
    resolves to a pause_turn, so the finding never completes in the batch."""
    return FakeBatchResult(
        custom_id=custom_id,
        result=FakeBatchResultEnvelope(type="succeeded", message=_pause_message()),
    )


def _init_job(findings):
    """Initial verification BatchJob: one custom_id per finding, no stored
    routing (so wave 0 derives the cap from ``DEFAULT_MAX_CONTINUATIONS``)."""
    return SimpleNamespace(
        batch_id="init-batch",
        request_map={f"verify__{i}": {"finding_idx": i} for i in range(len(findings))},
        job_type="verify",
    )


def _install_mocks(monkeypatch):
    """Patch the batch primitives so every wave returns a pause_turn.

    ``verify_finding`` (the real-time fallback / escalation entrypoint) is
    patched to raise — a pause-turn-only finding must terminate inside the
    wave loop via the cap, never via the real-time path, so a stray call is a
    bug. Returns a ``recorded`` dict with ``submit_calls`` (one per follow-up
    wave submitted).
    """
    import src.verification.verifier as V

    recorded = {"submit_calls": []}
    lock = threading.Lock()
    counter = {"n": 0}

    def fake_poll(batch_id, *, policy, log, progress_cb):
        return SimpleNamespace(detached=False, poll_failed=False)

    def fake_retrieve(job):
        return {cid: _pause_result(cid) for cid in job.request_map}

    def fake_submit(requests, request_map, *, extra_headers=None):
        with lock:
            counter["n"] += 1
            n = counter["n"]
            recorded["submit_calls"].append(request_map)
        return SimpleNamespace(
            batch_id=f"wave{n + 1}-batch", request_map=request_map, job_type="verify"
        )

    def fake_verify_finding(finding, **_kwargs):
        raise AssertionError("real-time path must not run for a pause-turn-only finding")

    monkeypatch.setattr(V, "poll_batch_bounded", fake_poll)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    monkeypatch.setattr(V, "submit_verification_followup_wave", fake_submit)
    monkeypatch.setattr(V, "verify_finding", fake_verify_finding)
    return recorded


def _collect(findings, *, max_waves, fallback_threshold=0):
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
    )


def test_pause_turn_finding_rides_cap_plus_one_waves(monkeypatch):
    """Default cap=2: a finding pausing every wave gets exactly 2 follow-up
    waves (3 attempts) and then terminates via the cap — real-time parity.

    Uses ``max_waves=6`` so the CAP, not wave exhaustion, is provably the
    binding constraint. A flip to ``>=`` would submit only 1 follow-up wave
    and report ``observed=2``; removing the cap entirely would submit 5 and
    report "unresolved after 6 batch waves".
    """
    cap = DEFAULT_MAX_CONTINUATIONS
    assert cap == 2  # guards the literal counts asserted below
    findings = [_finding(0)]
    recorded = _install_mocks(monkeypatch)

    _collect(findings, max_waves=6, fallback_threshold=0)

    # Exactly ``cap`` follow-up waves were submitted (one initial wave +
    # ``cap`` continuations = cap+1 total attempts), matching the real-time
    # ``range(max_continuations + 1)`` budget.
    assert len(recorded["submit_calls"]) == cap

    v = findings[0].verification
    # Exactly one terminal result, and not the post-loop safety net (the cap
    # branch resolved it inside the wave loop).
    assert v is not None
    assert v.verdict == "UNVERIFIED"
    assert _SAFETY_NET not in (v.explanation or "")

    # The CAP fired (parity budget spent), not max_waves exhaustion.
    assert "maximum continuation attempts" in (v.explanation or "")
    assert f"cap={cap}" in (v.explanation or "")
    assert f"observed={cap + 1}" in (v.explanation or "")
    assert "unresolved after" not in (v.explanation or "")

    # Telemetry corroborates the parity counts and attributes the cause.
    tel = v.retry_telemetry or {}
    assert tel.get("continuation_count") == cap + 1
    assert tel.get("failure_class") == "pause_turn"
    assert tel.get("terminal_reason") == f"continuation cap exceeded ({cap})"


def test_cap_terminal_finding_is_honest_insufficient_evidence(monkeypatch):
    """A pause-turn-only terminal is a clean INSUFFICIENT_EVIDENCE, not an
    operational failure and not a budget-exhausted sub-label.

    The model kept needing to continue and never grounded a verdict — that is
    "verifier ran cleanly but couldn't ground a claim" (INSUFFICIENT_EVIDENCE),
    distinct from VERIFICATION_FAILED (operational) and from the separate
    web_search ``budget_exhausted`` sentinel. The report must not mislabel it
    as a transient failure or as supported.
    """
    findings = [_finding(0)]
    _install_mocks(monkeypatch)

    _collect(findings, max_waves=6, fallback_threshold=0)

    v = findings[0].verification
    assert v is not None
    # PAUSE_TURN is a model-did-not-converge outcome, NOT an operational error,
    # so the cap branch leaves verification_failed at its False default.
    assert v.verification_failed is False
    # The cap is a continuation budget, distinct from the web_search
    # budget-exhaustion sentinel — that flag stays unset.
    assert is_budget_exhausted(findings[0]) is False
    # End-to-end status classification: honest "couldn't ground it", not a
    # failure and not supported.
    assert classify_status(findings[0]) == ReportStatus.INSUFFICIENT_EVIDENCE
