"""Diagnostics accounting fixes (WS5: D2, D3, D4).

* D2 — the run's total elapsed time is measured from ``run_started_at``
  (stamped before research/extraction) rather than submit time, and the
  field persists additively through pending-batch resume state.
* D3 — a phase whose events span 0.0 seconds but carry per-call
  ``elapsed_seconds`` data reports the per-call sum; the text summary
  carries an overlap footnote.
* D4 — program collection records the same collection-stage telemetry rows
  the single-module GUI branch records, via the shared Tk-free recorders in
  ``orchestration.diag_recording``.
"""
from __future__ import annotations

import time
from pathlib import Path

from src.batch.batch import BatchJob
from src.orchestration import program_pipeline as pp
from src.orchestration.diag_recording import (
    record_compliance,
    record_cross_check,
    record_review_collect,
    record_verification_findings,
)
from src.orchestration.diagnostics import DiagnosticsReport
from src.orchestration.pipeline import (
    BatchSubmission,
    CollectedBatchState,
    PipelineResult,
    finalize_batch_result,
)
from src.modules import require_module
from src.programs import (
    HYPERSCALE_DATACENTER_PROGRAM,
    RoutingState,
    SpecAssignment,
    SpecRoutingDecision,
)
from src.review.reviewer import Finding, ReviewResult
from src.verification.verifier import VerificationResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _assignment(name: str, module_ids: tuple[str, ...]) -> SpecAssignment:
    state = RoutingState.SUPPORTED if module_ids else RoutingState.UNSUPPORTED
    return SpecAssignment(
        source_path=str(Path("C:/specs") / name),
        decision=SpecRoutingDecision(
            spec_id=name,
            program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
            automatic_state=state,
            automatic_module_ids=module_ids,
            confidence=0.95,
            evidence=(),
        ),
    )


def _submission(module_id: str, name: str) -> BatchSubmission:
    module = require_module(module_id)
    request_id = f"review__{module_id}__0"
    return BatchSubmission(
        job=BatchJob(
            batch_id=f"msgbatch_{module_id}",
            job_type="review",
            request_map={request_id: {"filename": name, "index": 0, "type": "review"}},
            created_at=1_700_000_000.0,
        ),
        files_reviewed=[name],
        review_request_ids=[request_id],
        model="test-model",
        cycle_label=module.cycle.label,
        module_id=module_id,
    )


def _verified_finding(name: str) -> Finding:
    f = Finding(
        severity="HIGH",
        fileName=name,
        section="1.01",
        issue="A concrete specification conflict.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="",
    )
    f.verification = VerificationResult(
        verdict="CONFIRMED",
        explanation="grounded",
        grounded=True,
        model_used="claude-sonnet-5",
        verification_mode="standard_reasoning",
        web_search_requests=2,
        input_tokens=1200,
        output_tokens=300,
    )
    return f


class _FakeDiag:
    def __init__(self):
        self.api_calls: list[dict] = []
        self.logs: list[tuple[str, str, str, dict | None]] = []

    def record_api_call(self, **kwargs):
        self.api_calls.append(kwargs)

    def log(self, phase, level, message, data=None):
        self.logs.append((phase, level, message, data))

    def phases(self) -> set[str]:
        return {c.get("phase") for c in self.api_calls} | {
            phase for phase, _l, _m, _d in self.logs
        }


# ---------------------------------------------------------------------------
# D3: finish idempotence + phase-duration fallback + footnote
# ---------------------------------------------------------------------------


class TestFinishAndPhaseDurations:
    def test_finish_is_idempotent_and_log_appends_after(self):
        report = DiagnosticsReport(mode="batch", model="m", cycle_label="2025")
        report.finish()
        first_end = report.ended_at
        assert first_end is not None
        time.sleep(0.01)
        report.finish()
        assert report.ended_at == first_end
        report.log("export", "success", "Report exported")
        assert report.events[-1].message == "Report exported"

    def test_single_event_phase_uses_per_call_elapsed(self):
        report = DiagnosticsReport(mode="batch", model="m", cycle_label="2025")
        report.record_api_call(
            phase="batch_collect",
            model="m",
            message="Review results collected",
            extra={"elapsed_seconds": 261.4},
        )
        durations = report.summary()["phase_durations"]
        assert durations["batch_collect"] == 261.4

    def test_multi_event_phase_keeps_span(self):
        report = DiagnosticsReport(mode="batch", model="m", cycle_label="2025")
        report.log("verification", "step", "start")
        report.events[-1].elapsed = 10.0
        report.log("verification", "success", "done")
        report.events[-1].elapsed = 40.0
        durations = report.summary()["phase_durations"]
        assert durations["verification"] == 30.0

    def test_to_text_carries_overlap_footnote(self):
        report = DiagnosticsReport(mode="batch", model="m", cycle_label="2025")
        report.log("init", "info", "Run started")
        text = report.to_text()
        assert "phase windows may overlap" in text


# ---------------------------------------------------------------------------
# D4: shared recorders — pinned row shapes
# ---------------------------------------------------------------------------


_PINNED_VERIFICATION_KEYS = {
    "verdict",
    "finding_severity",
    "confidence",
    "explanation",
    "verification_mode",
    "verification_profile",
    "grounded",
    "cache_status",
    "escalated",
    "escalation_attempted",
    "initial_model",
    "initial_verdict",
    "escalation_changed_verdict",
    "escalation_reason",
    "api_call",
    "call_mode",
    "model",
    "web_search_requests",
    "input_tokens",
    "output_tokens",
    "retry_telemetry",
}


class TestRecorders:
    def test_none_diag_or_result_is_noop(self):
        record_review_collect(None, ReviewResult(findings=[]), transport="batch")
        record_review_collect(_FakeDiag(), None, transport="batch")
        record_cross_check(_FakeDiag(), None)
        record_compliance(_FakeDiag(), None)
        record_verification_findings(_FakeDiag(), [], transport="batch")

    def test_review_collect_batch_records_api_call(self):
        diag = _FakeDiag()
        rv = ReviewResult(findings=[], model="test-model")
        record_review_collect(diag, rv, transport="batch")
        assert len(diag.api_calls) == 1
        call = diag.api_calls[0]
        assert call["phase"] == "batch_collect"
        assert call["mode"] == "batch"
        assert call["extra"]["total_findings"] == 0

    def test_review_collect_realtime_skips_api_call(self):
        # The realtime runner already recorded per-spec rows; the aggregate
        # would double-count tokens in the per-phase rollup.
        diag = _FakeDiag()
        rv = ReviewResult(findings=[], model="test-model")
        record_review_collect(diag, rv, transport="realtime")
        assert diag.api_calls == []
        assert diag.logs[0][0] == "batch_collect"
        assert diag.logs[0][1] == "success"

    def test_verification_rows_match_pinned_keys(self):
        diag = _FakeDiag()
        finding = _verified_finding("a.docx")
        record_verification_findings(diag, [finding], transport="batch")
        info_rows = [d for p, lvl, _m, d in diag.logs if lvl == "info"]
        assert len(info_rows) == 1
        assert set(info_rows[0].keys()) == _PINNED_VERIFICATION_KEYS
        success = [d for _p, lvl, _m, d in diag.logs if lvl == "success"]
        assert success == [{"verdicts": {"CONFIRMED": 1}}]

    def test_round2_phase_override(self):
        diag = _FakeDiag()
        record_verification_findings(
            diag,
            [_verified_finding("a.docx")],
            transport="realtime",
            phase="cross_check_verification",
        )
        assert all(p == "cross_check_verification" for p, _l, _m, _d in diag.logs)

    def test_cross_check_and_compliance_rows(self):
        diag = _FakeDiag()
        cc = ReviewResult(findings=[], cross_check_status="completed", model="m")
        comp = ReviewResult(findings=[], cross_check_status="completed", model="m")
        comp.coverage = [{"requirement_id": "r-1", "status": "represented"}]
        record_cross_check(diag, cc)
        record_compliance(diag, comp)
        phases = [c["phase"] for c in diag.api_calls]
        assert phases == ["cross_check", "compliance"]
        assert diag.api_calls[1]["extra"]["coverage_count"] == 1


# ---------------------------------------------------------------------------
# D4: program collection records collection-stage telemetry
# ---------------------------------------------------------------------------


class TestProgramCollectionTelemetry:
    def test_collect_program_results_records_phases(self, monkeypatch):
        name = "21 13 13 Fire Sprinklers.docx"
        module_id = "datacenter_fire"
        submission = pp.ProgramSubmission(
            program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
            assignments=(_assignment(name, (module_id,)),),
            partitions={module_id: _submission(module_id, name)},
        )

        finding = _verified_finding(name)
        cross_finding = _verified_finding(name)
        module = require_module(module_id)
        child_result = PipelineResult(
            review_result=ReviewResult(findings=[finding], model="test-model"),
            cross_check_result=ReviewResult(
                findings=[cross_finding], cross_check_status="completed", model="m"
            ),
            files_reviewed=[name],
            cycle_label=module.cycle.label,
            module_id=module_id,
        )
        monkeypatch.setattr(
            pp, "run_batch_collection_headless", lambda *a, **kw: child_result
        )
        monkeypatch.setattr(
            pp, "_run_program_drawing_impact", lambda **kw: None
        )
        diag = _FakeDiag()
        result = pp.collect_program_results(submission, diagnostics=diag)
        assert result.module_results[module_id] is child_result
        # Review aggregate + cross-check rows recorded.
        assert {c["phase"] for c in diag.api_calls} == {"batch_collect", "cross_check"}
        # Round-1 and round-2 verification rows recorded.
        log_phases = {p for p, _l, _m, _d in diag.logs}
        assert "verification" in log_phases
        assert "cross_check_verification" in log_phases

    def test_no_diagnostics_still_collects(self, monkeypatch):
        name = "21 13 13 Fire Sprinklers.docx"
        module_id = "datacenter_fire"
        submission = pp.ProgramSubmission(
            program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
            assignments=(_assignment(name, (module_id,)),),
            partitions={module_id: _submission(module_id, name)},
        )
        module = require_module(module_id)
        child_result = PipelineResult(
            review_result=ReviewResult(findings=[], model="test-model"),
            files_reviewed=[name],
            cycle_label=module.cycle.label,
            module_id=module_id,
        )
        monkeypatch.setattr(
            pp, "run_batch_collection_headless", lambda *a, **kw: child_result
        )
        monkeypatch.setattr(pp, "_run_program_drawing_impact", lambda **kw: None)
        result = pp.collect_program_results(submission)
        assert result.module_results[module_id] is child_result


# ---------------------------------------------------------------------------
# D2: run_started_at-based total elapsed time
# ---------------------------------------------------------------------------


class TestRunStartedAt:
    def test_program_total_uses_run_started_at(self, monkeypatch):
        name = "21 13 13 Fire Sprinklers.docx"
        module_id = "datacenter_fire"
        now = 1_700_001_000.0
        monkeypatch.setattr(pp.time, "time", lambda: now)
        submission = pp.ProgramSubmission(
            program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
            assignments=(_assignment(name, (module_id,)),),
            partitions={module_id: _submission(module_id, name)},
            submitted_at=now - 100.0,
            run_started_at=now - 519.0,
        )
        module = require_module(module_id)
        child_result = PipelineResult(
            review_result=ReviewResult(findings=[], model="test-model"),
            files_reviewed=[name],
            cycle_label=module.cycle.label,
            module_id=module_id,
        )
        monkeypatch.setattr(
            pp, "run_batch_collection_headless", lambda *a, **kw: child_result
        )
        monkeypatch.setattr(pp, "_run_program_drawing_impact", lambda **kw: None)
        result = pp.collect_program_results(submission)
        assert result.total_elapsed_seconds == 519.0

    def test_program_total_falls_back_to_submitted_at(self, monkeypatch):
        name = "21 13 13 Fire Sprinklers.docx"
        module_id = "datacenter_fire"
        now = 1_700_001_000.0
        monkeypatch.setattr(pp.time, "time", lambda: now)
        submission = pp.ProgramSubmission(
            program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
            assignments=(_assignment(name, (module_id,)),),
            partitions={module_id: _submission(module_id, name)},
            submitted_at=now - 100.0,
        )
        module = require_module(module_id)
        child_result = PipelineResult(
            review_result=ReviewResult(findings=[], model="test-model"),
            files_reviewed=[name],
            cycle_label=module.cycle.label,
            module_id=module_id,
        )
        monkeypatch.setattr(
            pp, "run_batch_collection_headless", lambda *a, **kw: child_result
        )
        monkeypatch.setattr(pp, "_run_program_drawing_impact", lambda **kw: None)
        result = pp.collect_program_results(submission)
        assert result.total_elapsed_seconds == 100.0

    def test_single_module_finalize_prefers_run_started_at(self, monkeypatch):
        from src.orchestration import pipeline as pl

        now = 1_700_002_000.0
        monkeypatch.setattr(pl.time, "time", lambda: now)
        submission = _submission("datacenter_fire", "a.docx")
        submission.run_started_at = now - 400.0
        submission.job.created_at = now - 90.0
        state = CollectedBatchState(
            submission=submission, review_result=ReviewResult(findings=[])
        )
        result = finalize_batch_result(state)
        assert result.total_elapsed_seconds == 400.0

    def test_single_module_finalize_falls_back_to_created_at(self, monkeypatch):
        from src.orchestration import pipeline as pl

        now = 1_700_002_000.0
        monkeypatch.setattr(pl.time, "time", lambda: now)
        submission = _submission("datacenter_fire", "a.docx")
        submission.job.created_at = now - 90.0
        state = CollectedBatchState(
            submission=submission, review_result=ReviewResult(findings=[])
        )
        result = finalize_batch_result(state)
        assert result.total_elapsed_seconds == 90.0

    def test_prepare_batch_review_stamps_run_started_at(self, monkeypatch, tmp_path):
        from src.orchestration import pipeline as pl

        stub_prepared = pl._PreparedSpecs(
            specs=[], leed_alerts=[], placeholder_alerts=[]
        )
        monkeypatch.setattr(pl, "_prepare_specs", lambda **kw: stub_prepared)
        before = time.time()
        prepared = pl.prepare_batch_review(
            input_dir=tmp_path, files=[tmp_path / "a.docx"]
        )
        after = time.time()
        assert before <= prepared.run_started_at <= after

    def test_pending_batch_round_trips_run_started_at(self):
        from src.orchestration.batch_resume import (
            PendingBatch,
            _pending_batch_from_mapping,
        )
        from dataclasses import asdict

        submission = _submission("datacenter_fire", "a.docx")
        submission.run_started_at = 1_699_999_000.0
        pending = PendingBatch.from_submission(submission)
        assert pending.run_started_at == 1_699_999_000.0
        restored = _pending_batch_from_mapping(asdict(pending))
        assert restored.run_started_at == 1_699_999_000.0

    def test_legacy_pending_batch_defaults_to_zero(self):
        from src.orchestration.batch_resume import _pending_batch_from_mapping

        legacy = {"batch_id": "msgbatch_legacy", "model": "claude-opus-4-8"}
        restored = _pending_batch_from_mapping(legacy)
        assert restored.run_started_at == 0.0
