"""Batch-mode escalation wiring — parity with the real-time path.

The real-time path (``verify_finding``) re-runs Sonnet's ungrounded
CRITICAL/HIGH verdicts on Opus and surfaces genuine disagreements as
VERIFIED_CONTESTED. Before this change the batch wave loop produced only
the initial pass, so a batch run never escalated and never contested.

These tests cover the two new pieces:

* ``_apply_escalation_outcome`` — the shared merge/disagreement helper now
  reused by BOTH the real-time and batch escalation paths (so they can't
  drift). Tested as a pure function.
* ``_run_batch_escalation_wave`` — candidate selection, the skip guards
  (already-Opus, already-escalated, low-severity), best-effort failure
  handling, and the end-to-end merge that yields VERIFIED_CONTESTED.
"""
from __future__ import annotations

from types import SimpleNamespace


from src.core.code_cycles import DEFAULT_CYCLE
from src.output.report_status import (
    EditActionLabel,
    ReportStatus,
    classify_edit_action,
    classify_status,
)
from src.review.reviewer import Finding
from src.verification.verification_prescreen import VERIFICATION_ESCALATION_MODEL
from src.verification.verifier import (
    DEFAULT_VERIFICATION_POLL_POLICY,
    VerificationResult,
    _apply_escalation_outcome,
    _run_batch_escalation_wave,
)
from tests.fixtures.fake_anthropic import (
    batch_verification_result,
    sample_verification_verdict_payload,
    verification_tool_use_response,
)

INIT_MODEL = "claude-sonnet-4-6"


def _nolog(*_a, **_k) -> None:
    pass


def _noprog(_p: float, _m: str) -> None:
    pass


def _opus_grounded_message(verdict="CONFIRMED"):
    """A verifier tool-use response that the BATCH parser treats as grounded.

    The wave parser's search gate (``_search_gate_failure``) requires BOTH a
    successful ``web_search_tool_result`` block (the fixture provides one) AND
    ``usage.server_tool_use.web_search_requests > 0`` (the default ``FakeUsage``
    does not set this), so attach a usage that reports a search count. Also
    carries non-zero token usage so the #4 telemetry path has something to
    read.
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


def _vr(verdict, *, grounded, sources=None, model="") -> VerificationResult:
    return VerificationResult(
        verdict=verdict,
        grounded=grounded,
        sources=list(sources or []),
        accepted_sources=list(sources or []),
        model_used=model,
        cache_status="miss",
    )


# ---------------------------------------------------------------------------
# 1. _apply_escalation_outcome — shared merge/disagreement helper
# ---------------------------------------------------------------------------


class TestApplyEscalationOutcome:
    def test_swaps_in_grounded_escalated_result(self):
        initial = _vr("UNVERIFIED", grounded=False, model=INIT_MODEL)
        esc = _vr("CONFIRMED", grounded=True, sources=["https://x"], model="claude-opus-4-7")
        merged = _apply_escalation_outcome(
            initial_result=initial,
            esc_result=esc,
            initial_verdict="UNVERIFIED",
            initial_model=INIT_MODEL,
            initial_grounded=False,
            initial_sources=[],
            escalation_reason="initial_unverified",
        )
        assert merged is esc
        assert merged.verdict == "CONFIRMED"
        assert merged.escalation_attempted is True
        assert merged.initial_model == INIT_MODEL
        assert merged.initial_verdict == "UNVERIFIED"
        assert merged.escalation_changed_verdict is True
        assert merged.escalation_reason == "initial_unverified"
        # Initial was ungrounded → "Sonnet found nothing, Opus did" is the
        # escalation path doing its job, NOT a disagreement.
        assert merged.models_disagreed is False

    def test_keeps_initial_when_escalation_ungrounded(self):
        initial = _vr("DISPUTED", grounded=True, sources=["https://a"], model=INIT_MODEL)
        esc = _vr("UNVERIFIED", grounded=False, model="claude-opus-4-7")
        merged = _apply_escalation_outcome(
            initial_result=initial,
            esc_result=esc,
            initial_verdict="DISPUTED",
            initial_model=INIT_MODEL,
            initial_grounded=True,
            initial_sources=["https://a"],
            escalation_reason="initial_ungrounded",
        )
        # Escalated result is not grounded and initial was not UNVERIFIED, so
        # the grounded first pass is kept.
        assert merged is initial
        assert merged.verdict == "DISPUTED"
        assert merged.escalation_attempted is True
        assert merged.models_disagreed is False

    def test_models_disagreed_when_both_grounded_and_verdicts_differ(self):
        initial = _vr("CORRECTED", grounded=True, sources=["https://a"], model=INIT_MODEL)
        esc = _vr("CONFIRMED", grounded=True, sources=["https://b"], model="claude-opus-4-7")
        merged = _apply_escalation_outcome(
            initial_result=initial,
            esc_result=esc,
            initial_verdict="CORRECTED",
            initial_model=INIT_MODEL,
            initial_grounded=True,
            initial_sources=["https://a"],
            escalation_reason="initial_ungrounded",
        )
        assert merged is esc
        assert merged.models_disagreed is True
        assert merged.initial_sources == ["https://a"]

        f = Finding(
            severity="HIGH", fileName="x", section="1", issue="i",
            actionType="EDIT", existingText="old", replacementText="new",
            codeReference="NFPA 13", confidence=0.5,
        )
        f.verification = merged
        # The disagreement overrides the per-verdict classification.
        assert classify_status(f) == ReportStatus.VERIFIED_CONTESTED
        # ... and a finding with a proposal is labeled EDIT_SUGGESTED
        # (the app emits edit instructions; it never applies them).
        assert classify_edit_action(f) == EditActionLabel.EDIT_SUGGESTED

    def test_no_disagreement_when_same_verdict(self):
        initial = _vr("CONFIRMED", grounded=True, sources=["https://a"], model=INIT_MODEL)
        esc = _vr("CONFIRMED", grounded=True, sources=["https://b"], model="claude-opus-4-7")
        merged = _apply_escalation_outcome(
            initial_result=initial,
            esc_result=esc,
            initial_verdict="CONFIRMED",
            initial_model=INIT_MODEL,
            initial_grounded=True,
            initial_sources=["https://a"],
            escalation_reason="router_decision",
        )
        assert merged.models_disagreed is False
        assert merged.escalation_changed_verdict is False

    def test_initial_sources_recorded_even_for_noncontested(self):
        initial = _vr("UNVERIFIED", grounded=False, model=INIT_MODEL)
        esc = _vr("CONFIRMED", grounded=True, sources=["https://x"], model="claude-opus-4-7")
        merged = _apply_escalation_outcome(
            initial_result=initial,
            esc_result=esc,
            initial_verdict="UNVERIFIED",
            initial_model=INIT_MODEL,
            initial_grounded=False,
            initial_sources=[],
            escalation_reason="initial_unverified",
        )
        # Set unconditionally (empty list here) so the evidence panel can
        # show "Initial: UNVERIFIED, no sources".
        assert merged.initial_sources == []


# ---------------------------------------------------------------------------
# 2. _run_batch_escalation_wave — orchestration
# ---------------------------------------------------------------------------


def _candidate_finding(
    *,
    severity="CRITICAL",
    verdict="UNVERIFIED",
    grounded=False,
    model=INIT_MODEL,
    successful=0,
    errors=0,
    escalation_attempted=False,
    sources=None,
) -> Finding:
    f = Finding(
        severity=severity,
        fileName="22 11 00 - Facility Water.docx",
        section="2.1",
        issue="DSA bulletin lookup did not ground",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        confidence=0.5,
        codeReference="",
    )
    f.verification = VerificationResult(
        verdict=verdict,
        grounded=grounded,
        model_used=model,
        successful_source_count=successful,
        search_error_count=errors,
        sources=list(sources or []),
        accepted_sources=list(sources or []),
        cache_status="miss",
        escalation_attempted=escalation_attempted,
    )
    return f


def _mock_batch_primitives(
    monkeypatch,
    *,
    results_by_id,
    poll_detached=False,
    poll_failed=False,
    submit_raises=False,
):
    """Patch the three batch primitives the escalation wave calls.

    Returns a dict that records what (if anything) was submitted so a test
    can assert the wave did / did not submit a follow-up batch.
    """
    import src.verification.verifier as V

    recorded: dict = {}

    def fake_submit(requests, request_map, *, extra_headers=None):
        if submit_raises:
            raise RuntimeError("submit boom")
        recorded["requests"] = requests
        recorded["request_map"] = request_map
        recorded["extra_headers"] = extra_headers
        return SimpleNamespace(batch_id="esc-batch", request_map=request_map, job_type="verify")

    def fake_poll(batch_id, *, policy, log, progress_cb):
        return SimpleNamespace(detached=poll_detached, poll_failed=poll_failed)

    def fake_retrieve(job):
        return {cid: results_by_id(cid) for cid in job.request_map}

    monkeypatch.setattr(V, "submit_verification_followup_wave", fake_submit)
    monkeypatch.setattr(V, "poll_batch_bounded", fake_poll)
    monkeypatch.setattr(V, "retrieve_verification_results_detailed", fake_retrieve)
    return recorded


def _run(findings):
    _run_batch_escalation_wave(
        findings,
        cycle=DEFAULT_CYCLE,
        cache=None,
        policy=DEFAULT_VERIFICATION_POLL_POLICY,
        log=_nolog,
        progress=_noprog,
    )


class TestRunBatchEscalationWave:
    def test_escalates_ungrounded_critical_finding(self, monkeypatch):
        f = _candidate_finding(severity="CRITICAL")

        def results(cid):
            return batch_verification_result(
                custom_id=cid,
                message=_opus_grounded_message("CONFIRMED"),
            )

        recorded = _mock_batch_primitives(monkeypatch, results_by_id=results)
        _run([f])

        assert f.verification.escalation_attempted is True
        assert f.verification.escalated is True
        assert f.verification.verdict == "CONFIRMED"
        assert f.verification.initial_verdict == "UNVERIFIED"
        assert f.verification.grounded is True
        # Exactly one escalation request, stable custom_id.
        assert len(recorded["requests"]) == 1
        assert recorded["requests"][0]["custom_id"] == "verify_escalation__0"

    def test_skips_finding_already_on_escalation_model(self, monkeypatch):
        # A CRITICAL california_ahj finding ran its INITIAL pass on Opus, so
        # escalating to Opus is a no-op — mirrors the real-time guard.
        f = _candidate_finding(severity="CRITICAL", model=VERIFICATION_ESCALATION_MODEL)
        recorded = _mock_batch_primitives(monkeypatch, results_by_id=lambda cid: None)
        _run([f])
        assert "requests" not in recorded  # submit never called
        assert f.verification.escalation_attempted is False

    def test_skips_already_escalated_finding(self, monkeypatch):
        f = _candidate_finding(severity="CRITICAL", escalation_attempted=True)
        recorded = _mock_batch_primitives(monkeypatch, results_by_id=lambda cid: None)
        _run([f])
        assert "requests" not in recorded

    def test_skips_low_severity_finding(self, monkeypatch):
        # MEDIUM is not in the escalation severities; should_escalate is False.
        f = _candidate_finding(severity="MEDIUM")
        recorded = _mock_batch_primitives(monkeypatch, results_by_id=lambda cid: None)
        _run([f])
        assert "requests" not in recorded
        assert f.verification.escalation_attempted is False

    def test_skips_grounded_confirmed_finding(self, monkeypatch):
        # Grounded CONFIRMED CRITICAL → nothing to escalate.
        f = _candidate_finding(
            severity="CRITICAL", verdict="CONFIRMED", grounded=True,
            successful=2, errors=0, sources=["https://a"],
        )
        recorded = _mock_batch_primitives(monkeypatch, results_by_id=lambda cid: None)
        _run([f])
        assert "requests" not in recorded

    def test_best_effort_keeps_initial_on_submit_failure(self, monkeypatch):
        f = _candidate_finding(severity="CRITICAL")
        _mock_batch_primitives(
            monkeypatch, results_by_id=lambda cid: None, submit_raises=True
        )
        _run([f])
        # Initial verdict untouched; no escalation telemetry stamped.
        assert f.verification.verdict == "UNVERIFIED"
        assert f.verification.escalation_attempted is False

    def test_best_effort_keeps_initial_on_poll_detached(self, monkeypatch):
        f = _candidate_finding(severity="CRITICAL")
        _mock_batch_primitives(
            monkeypatch, results_by_id=lambda cid: None, poll_detached=True
        )
        _run([f])
        assert f.verification.escalation_attempted is False

    def test_operational_failure_on_escalation_keeps_initial(self, monkeypatch):
        # The escalation pass itself errors out for this finding → keep the
        # initial verdict rather than downgrading a usable result.
        from tests.fixtures.fake_anthropic import batch_errored_result

        f = _candidate_finding(severity="CRITICAL")
        _mock_batch_primitives(
            monkeypatch,
            results_by_id=lambda cid: batch_errored_result(custom_id=cid),
        )
        _run([f])
        assert f.verification.verdict == "UNVERIFIED"
        # escalation_attempted stays False because the merge only runs on a
        # successful escalation result.
        assert f.verification.escalation_attempted is False

    def test_contested_when_both_grounded_disagree(self, monkeypatch):
        # Initial is grounded but escalation-eligible via the all-search-errors
        # branch (search_error_count > 0 AND successful_source_count == 0).
        f = _candidate_finding(
            severity="CRITICAL", verdict="CORRECTED", grounded=True,
            successful=0, errors=1, sources=["https://nfpa.org/codes/13"],
        )

        def results(cid):
            return batch_verification_result(
                custom_id=cid,
                message=_opus_grounded_message("CONFIRMED"),
            )

        _mock_batch_primitives(monkeypatch, results_by_id=results)
        _run([f])

        assert f.verification.models_disagreed is True
        assert f.verification.initial_verdict == "CORRECTED"
        assert f.verification.verdict == "CONFIRMED"
        assert classify_status(f) == ReportStatus.VERIFIED_CONTESTED

    def test_empty_findings_no_submit(self, monkeypatch):
        recorded = _mock_batch_primitives(monkeypatch, results_by_id=lambda cid: None)
        _run([])
        assert "requests" not in recorded
