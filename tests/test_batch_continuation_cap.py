"""Regression test: the batch continuation cap mirrors the real-time budget.

STRUCTURAL_AUDIT P2-1 flagged the batch wave loop's continuation check
``if continuation_counts[key] > cap`` (``verifier.py``) as a possible
off-by-one — "a finding can consume ``cap + 1`` waves before termination."

It is **not** a bug. The check deliberately mirrors the real-time
pause-turn loop in ``_run_verification_call``, which iterates
``range(max_continuations + 1)`` — i.e. one initial call plus ``cap``
pause/resume rounds. The batch path must grant the same budget: the initial
wave plus ``cap`` follow-up resubmissions before going terminal-UNVERIFIED.
Because the counter is incremented *before* the ``> cap`` test, it only
reaches ``cap + 1`` after that full budget is spent.

This test locks the parity so a well-meaning ``>`` -> ``>=`` "fix" (which
would terminate one resume early and make batch verification stricter than
real-time) fails loudly:

* a finding that pauses on every wave is resubmitted exactly ``cap`` times,
  then terminates on the ``cap + 1``-th wave with ``observed == cap + 1``
  (the upper bound of the budget);
* a finding that pauses ``cap`` times then converges still succeeds — the cap
  allows the full real-time-equivalent budget (the lower bound);
* the real-time fallback is never invoked for a cap-terminated finding.

``MAX_VERIFICATION_WAVES`` (3) == ``DEFAULT_MAX_CONTINUATIONS`` (2) + 1, so a
pure-continuation finding terminates via the cap on the final wave, never the
safety-net tail.
"""
from __future__ import annotations

from src.review.reviewer import Finding
from src.verification import verifier as V
from src.verification.retry_policy import DEFAULT_MAX_CONTINUATIONS
from src.verification.verifier import (
    VerificationItemOutcome,
    collect_verification_batch_results,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeJob:
    """Minimal stand-in for a batch job handle."""

    def __init__(self, batch_id: str, findings):
        self.batch_id = batch_id
        self._findings = findings
        self.custom_ids = [f"verify__{i}" for i in range(len(findings))]


def _mk_finding(i: int) -> Finding:
    return Finding(
        severity="HIGH",
        section=f"Section {i}",
        issue=f"Issue {i}",
        codeReference="NFPA 13",
        actionType="EDIT",
        existingText=f"old {i}",
        replacementText=f"new {i}",
    )


def _install_stubs(monkeypatch, wave_script):
    """Patch the batch seam the wave loop drives.

    ``wave_script`` maps the *stable* original custom_id ->
    ``[outcome_per_wave, ...]``; each wave pops one entry. Outcomes:
      - ("continue", None)      # pause_turn
      - ("success", verdict)

    Lookups key on ``original_custom_id`` (constant across waves), NOT the
    per-wave re-stamped ``custom_id`` (``verify__waveN__idx``), so the script
    survives the follow-up-wave re-stamping the same way the production
    counters do.
    """
    calls = {"verify_finding": 0, "submit_followup": 0}

    def _fake_classify(*, job, findings, request_contexts):
        outcomes = []
        for cid, ctx in request_contexts.items():
            idx = ctx["finding_idx"]
            stable = ctx.get("original_custom_id", cid)
            script = wave_script.get(stable, [])
            if not script:
                # Nothing scripted: resolve cleanly so an over-long run can't
                # spin forever (a failing-loud default for test authoring).
                outcomes.append(
                    VerificationItemOutcome(
                        finding_idx=idx,
                        original_custom_id=cid,
                        classification="success",
                        parsed_verification=V.VerificationResult(
                            verdict="UNVERIFIED", explanation="default-no-script"
                        ),
                    )
                )
                continue
            kind, payload = script.pop(0)
            if kind == "success":
                outcomes.append(
                    VerificationItemOutcome(
                        finding_idx=idx,
                        original_custom_id=cid,
                        classification="success",
                        parsed_verification=V.VerificationResult(
                            verdict=payload, explanation="ok"
                        ),
                    )
                )
            elif kind == "continue":
                outcomes.append(
                    VerificationItemOutcome(
                        finding_idx=idx,
                        original_custom_id=cid,
                        classification="continue",
                        unverified_reason="pause_turn",
                    )
                )
            else:  # pragma: no cover - guards against typos in a script
                raise AssertionError(f"unknown wave outcome kind: {kind!r}")
        return outcomes

    def _fake_poll(batch_id, **kwargs):
        from src.batch.batch_runtime import BatchPollOutcome

        return BatchPollOutcome(
            batch_id=batch_id, detached=False, poll_failed=False
        )

    def _fake_submit_followup(next_requests, **kwargs):
        calls["submit_followup"] += 1
        return _FakeJob("batch-wave-next", [f for _cid, f in next_requests])

    def _fake_verify_finding(finding, **kwargs):
        calls["verify_finding"] += 1
        return V.VerificationResult(
            verdict="CONFIRMED",
            explanation="real-time fallback",
            sources=["https://example.gov/x"],
            grounded=True,
        )

    monkeypatch.setattr(V, "_classify_wave_results", _fake_classify)
    monkeypatch.setattr(V, "poll_batch_bounded", _fake_poll)
    monkeypatch.setattr(
        V, "submit_verification_followup_wave", _fake_submit_followup
    )
    monkeypatch.setattr(V, "verify_finding", _fake_verify_finding)
    return calls


def _contexts(n: int):
    return {
        f"verify__{i}": {
            "finding_idx": i,
            "original_custom_id": f"verify__{i}",
            "routing": {},  # empty -> cap falls back to DEFAULT_MAX_CONTINUATIONS
        }
        for i in range(n)
    }


def _run(findings, request_contexts):
    job = _FakeJob("batch-1", findings)
    return collect_verification_batch_results(
        job,
        findings,
        cycle=None,
        request_contexts=request_contexts,
        log=lambda *a, **k: None,
        progress=lambda *a, **k: None,
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_continuation_cap_allows_initial_plus_cap_resubmits(monkeypatch):
    """Upper bound: pause-every-wave terminates after cap resubmits.

    With ``>`` the finding is resubmitted exactly ``cap`` times (initial wave
    + ``cap`` follow-ups = ``cap + 1`` total runs) before going
    terminal-UNVERIFIED with ``observed == cap + 1`` — matching the real-time
    ``range(cap + 1)`` budget. A ``>=`` regression would resubmit only
    ``cap - 1`` times and report ``observed == cap``, failing both asserts.
    """
    cap = DEFAULT_MAX_CONTINUATIONS  # 2
    # More than enough entries to outlast the cap (it must terminate first).
    wave_script = {"verify__0": [("continue", None)] * (cap + 3)}
    calls = _install_stubs(monkeypatch, wave_script)

    findings = [_mk_finding(0)]
    _run(findings, _contexts(1))

    v = findings[0].verification
    assert v is not None
    assert v.verdict == "UNVERIFIED"
    # Terminated via the continuation cap (not the fallback or safety net).
    assert "maximum" in (v.explanation or "").lower()
    assert f"cap={cap}" in (v.explanation or "")
    assert f"observed={cap + 1}" in (v.explanation or "")
    # Exactly ``cap`` follow-up resubmissions: initial wave + cap follow-ups.
    assert calls["submit_followup"] == cap
    # The cap resolves the finding before the last-wave real-time fallback.
    assert calls["verify_finding"] == 0
    # Structured telemetry agrees with the rendered "observed" value.
    assert (v.retry_telemetry or {}).get("continuation_count") == cap + 1


def test_continuation_converges_within_cap(monkeypatch):
    """Lower bound: pause ``cap`` times then converge still succeeds.

    The cap must permit the full real-time-equivalent budget; a ``>=``
    regression would terminate the finding on its ``cap``-th wave before this
    converging success could land.
    """
    cap = DEFAULT_MAX_CONTINUATIONS  # 2
    wave_script = {
        "verify__0": [("continue", None)] * cap + [("success", "CONFIRMED")]
    }
    calls = _install_stubs(monkeypatch, wave_script)

    findings = [_mk_finding(0)]
    _run(findings, _contexts(1))

    v = findings[0].verification
    assert v is not None
    assert v.verdict == "CONFIRMED"
    assert v.explanation == "ok"
    assert calls["submit_followup"] == cap
    assert calls["verify_finding"] == 0
