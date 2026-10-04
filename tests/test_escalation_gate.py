"""Escalation gates: mode eligibility and operational failures.

Only STANDARD_REASONING may escalate; STRICT_STRUCTURED findings stay on
their initial tier even when a high-stakes verdict is UNVERIFIED.

``should_escalate_verification`` used to read verdict / grounded / counts but
not ``verification_failed``, so a CRITICAL finding whose initial call hit a
rate limit (UNVERIFIED, ungrounded, ``verification_failed=True``) paid for an
escalation-tier re-issue of the very same request. Both call sites — the
real-time ``verify_finding`` and the batch ``_run_batch_escalation_wave`` —
now thread the flag, and the gate returns False when it is set.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.api_config import VERIFICATION_ESCALATION_MODEL, VERIFICATION_MODEL_DEFAULT
from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.verification_modes import VerificationMode
from src.verification.verification_prescreen import should_escalate_verification
import src.verification.verifier as V
from src.verification.verifier import (
    DEFAULT_VERIFICATION_POLL_POLICY,
    VerificationResult,
    _run_batch_escalation_wave,
)

pytestmark = pytest.mark.skipif(
    VERIFICATION_MODEL_DEFAULT == VERIFICATION_ESCALATION_MODEL,
    reason="escalation is inert when the initial verifier is already the escalation model",
)


def _critical(severity: str = "CRITICAL") -> Finding:
    # Jurisdiction-neutral on purpose: a CRITICAL finding with jurisdiction
    # signals routes its *initial* pass to the escalation tier, and the
    # "already on the escalation model" guard would then mask the gate
    # under test. A code-standard claim rides STANDARD_REASONING first.
    return Finding(
        severity=severity,
        fileName="21 13 13 - Wet-Pipe Sprinkler.docx",
        section="2.4",
        issue="Sprinkler spacing cited exceeds the maximum for the listed hazard class.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13 §10.2.4",
        confidence=0.5,
    )


def _gate(finding: Finding, **overrides) -> bool:
    kwargs = dict(
        verdict="UNVERIFIED",
        grounded=False,
        successful_source_count=0,
        search_error_count=0,
    )
    kwargs.update(overrides)
    return should_escalate_verification(finding, **kwargs)


# ---------------------------------------------------------------------------
# 1. The policy gate itself
# ---------------------------------------------------------------------------


class TestShouldEscalateAfterOperationalFailure:
    def test_evidentiary_unverified_still_escalates(self) -> None:
        # Control: the pre-existing behavior for a pass that ran and could
        # not ground the claim.
        assert _gate(_critical()) is True
        assert _gate(_critical(), verification_failed=False) is True

    def test_operational_failure_never_escalates(self) -> None:
        assert _gate(_critical(), verification_failed=True) is False

    def test_failed_flag_beats_every_other_trigger(self) -> None:
        # Each of the three positive triggers on its own says "escalate";
        # the operational-failure input overrides all of them.
        for trigger in (
            dict(verdict="UNVERIFIED"),
            dict(verdict="CONFIRMED", grounded=False),
            dict(verdict="CONFIRMED", grounded=True, search_error_count=2, successful_source_count=0),
        ):
            assert _gate(_critical(), **trigger) is True, trigger
            assert _gate(_critical(), verification_failed=True, **trigger) is False, trigger

    def test_flag_is_keyword_only_and_defaults_off(self) -> None:
        # Legacy callers that do not pass the flag keep their behavior.
        assert _gate(_critical("HIGH")) is True
        assert _gate(_critical("MEDIUM")) is False  # severity gate unchanged


# ---------------------------------------------------------------------------
# 2. Real-time path: verify_finding
# ---------------------------------------------------------------------------


def _scripted_run(calls: list[dict], *, failed: bool):
    def fake_run(
        finding,
        *,
        cycle,
        model,
        max_retries,
        escalated,
        user_location=None,
        governing_basis=None,
        trace_parent=None,
        call_gate=None,
    ):
        calls.append({"model": model, "escalated": escalated})
        return VerificationResult(
            verdict="UNVERIFIED",
            explanation="Rate limited during verification." if failed else "No evidence found.",
            grounded=False,
            model_used=model,
            escalated=escalated,
            cache_status="miss",
            verification_failed=failed,
        )

    return fake_run


class TestRealtimeNoEscalationAfterFailure:
    def test_rate_limited_initial_pass_is_not_escalated(self, monkeypatch) -> None:
        calls: list[dict] = []
        monkeypatch.setattr(V, "_run_verification_call", _scripted_run(calls, failed=True))
        result = V.verify_finding(_critical(), max_retries=0, cycle=DEFAULT_CYCLE, cache=None)
        # Exactly one call, and it was the initial pass — no escalation-tier
        # re-issue of a request that died operationally.
        assert [c["escalated"] for c in calls] == [False]
        assert result.verification_failed is True
        assert result.escalation_attempted is False
        assert result.escalated is False

    def test_evidentiary_unverified_still_escalates(self, monkeypatch) -> None:
        calls: list[dict] = []
        monkeypatch.setattr(V, "_run_verification_call", _scripted_run(calls, failed=False))
        result = V.verify_finding(_critical(), max_retries=0, cycle=DEFAULT_CYCLE, cache=None)
        assert [c["escalated"] for c in calls] == [False, True]
        assert calls[1]["model"] == VERIFICATION_ESCALATION_MODEL
        assert result.escalation_attempted is True


# ---------------------------------------------------------------------------
# 3. Batch path: _run_batch_escalation_wave
# ---------------------------------------------------------------------------


def _candidate(*, verification_failed: bool) -> Finding:
    f = _critical()
    f.verification = VerificationResult(
        verdict="UNVERIFIED",
        explanation="Rate limited during verification." if verification_failed else "",
        grounded=False,
        model_used=VERIFICATION_MODEL_DEFAULT,
        cache_status="miss",
        verification_failed=verification_failed,
    )
    return f


def _mock_batch_primitives(monkeypatch) -> dict:
    """Patch the three batch primitives; record whether a wave was submitted."""
    recorded: dict = {}

    def fake_submit(requests, request_map, *, extra_headers=None):
        recorded["requests"] = requests
        return SimpleNamespace(batch_id="esc-batch", request_map=request_map, job_type="verify")

    def fake_poll(batch_id, *, policy, log, progress_cb):
        return SimpleNamespace(detached=False, poll_failed=False)

    def fake_retrieve(job):
        # A submitted wave whose results are all errored keeps the initial
        # verdict (best-effort); the tests here only care whether the wave
        # was submitted at all.
        from tests.fixtures.fake_anthropic import batch_errored_result

        return {cid: batch_errored_result(custom_id=cid) for cid in job.request_map}

    monkeypatch.setattr(V, "submit_verification_followup_wave", fake_submit)
    monkeypatch.setattr(V, "poll_batch_bounded", fake_poll)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    return recorded


def _run(findings: list[Finding]) -> None:
    _run_batch_escalation_wave(
        findings,
        cycle=DEFAULT_CYCLE,
        cache=None,
        policy=DEFAULT_VERIFICATION_POLL_POLICY,
        log=lambda *_a, **_k: None,
        progress=lambda _p, _m: None,
    )


class TestBatchWaveNoEscalationAfterFailure:
    def test_failed_wave_item_is_not_resubmitted(self, monkeypatch) -> None:
        f = _candidate(verification_failed=True)
        recorded = _mock_batch_primitives(monkeypatch)
        _run([f])
        assert "requests" not in recorded  # submit never called
        assert f.verification.escalation_attempted is False
        assert f.verification.verification_failed is True

    def test_evidentiary_unverified_is_still_submitted(self, monkeypatch) -> None:
        f = _candidate(verification_failed=False)
        recorded = _mock_batch_primitives(monkeypatch)
        _run([f])
        assert len(recorded["requests"]) == 1
        assert recorded["requests"][0]["custom_id"] == "verify_escalation__0"

    def test_mixed_wave_submits_only_the_evidentiary_one(self, monkeypatch) -> None:
        failed = _candidate(verification_failed=True)
        clean = _candidate(verification_failed=False)
        recorded = _mock_batch_primitives(monkeypatch)
        _run([failed, clean])
        assert [r["custom_id"] for r in recorded["requests"]] == ["verify_escalation__1"]


@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL"])
@pytest.mark.parametrize("transport", ["realtime", "batch"])
@pytest.mark.parametrize("internal_coordination", [True, False])
def test_only_standard_reasoning_escalates(
    monkeypatch, severity, transport, internal_coordination
) -> None:
    finding = _critical(severity)
    if internal_coordination:
        finding.issue = "Internal contradiction: 2.2.B specifies 5 ft, 4.1.A specifies 8 ft."
        finding.codeReference = ""
    expected_mode = (
        VerificationMode.STRICT_STRUCTURED
        if internal_coordination else VerificationMode.STANDARD_REASONING
    )
    decision = V.select_routing(finding, local_skip=False, cycle=DEFAULT_CYCLE)
    assert decision.mode is expected_mode
    # Both findings satisfy the content gate; only the mode may block escalation.
    assert _gate(finding) is True

    if transport == "realtime":
        calls: list[dict] = []
        monkeypatch.setattr(V, "_run_verification_call", _scripted_run(calls, failed=False))
        result = V.verify_finding(finding, max_retries=0, cycle=DEFAULT_CYCLE, cache=None)
        assert [c["escalated"] for c in calls] == (
            [False] if internal_coordination else [False, True]
        )
        assert result.escalation_attempted is (not internal_coordination)
        assert result.verdict == "UNVERIFIED"
    else:
        initial = VerificationResult(
            verdict="UNVERIFIED",
            grounded=False,
            model_used=decision.model,
            cache_status="miss",
        )
        finding.verification = initial
        recorded = _mock_batch_primitives(monkeypatch)
        _run([finding])
        assert len(recorded.get("requests", [])) == (0 if internal_coordination else 1)
        if internal_coordination:
            assert finding.verification is initial
            assert initial.escalation_attempted is False
