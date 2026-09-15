"""The headless collection driver prices its own API calls.

``run_batch_collection_headless`` is not only the recovery tool's engine — it
is the engine **every routed multi-module program runs on**, reached through
``program_pipeline.collect_program_results``. It recorded no diagnostics at
all, and the GUI's ``record_api_call`` sites sit on the single-module branch
that a routed run returns before reaching. The consequence, observed on a real
five-spec hyperscale run: the diagnostics report priced the four
requirements-research calls and *nothing else* — not the batch review, not 87
verification calls, not cross-check, not compliance — and presented the result
as the run's estimated cost.

Two properties close that, and both are asserted here:

1. **Every phase that spends reaches the report.** Review, verification round
   one, cross-check, compliance, verification round two, and drawing impact
   each produce a billable event.
2. **Round two is one of them.** It previously logged only a bare "complete"
   line on *both* drivers, so cross-check + compliance verification — and any
   Opus escalation inside it — was unpriced everywhere.

The negative case matters as much: omitting ``diagnostics`` must leave the
driver behaving exactly as before, because that is what makes this additive
for every existing caller.
"""
from __future__ import annotations

import pytest

from src.batch.batch import BatchJob
from src.core.code_cycles import DEFAULT_CYCLE
from src.gui.context_attachment import wrap_attachment
from src.input.drawing_digest import DIGEST_ATTACHMENT_LABEL
from src.input.extractor import ExtractedSpec
from src.orchestration import pipeline as pl
from src.orchestration.diagnostics import DiagnosticsReport
from src.orchestration.pipeline import BatchSubmission, CollectedBatchState
from src.review.reviewer import Finding, ReviewResult
from src.verification.verifier import VerificationResult
import src.drawing_impact as di_pkg
from src.drawing_impact import DrawingImpactResult
from src.verification.verification_cache import VerificationCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding(file_name: str = "21 10 00.docx") -> Finding:
    return Finding(
        severity="HIGH",
        fileName=file_name,
        section="2.1",
        issue="Sprinkler standard edition may not match local adoption.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13",
        confidence=0.7,
    )


def _verified(finding: Finding, **overrides) -> Finding:
    defaults = dict(
        verdict="CONFIRMED",
        explanation="Checked against the adoption table.",
        grounded=True,
        cache_status="miss",
        model_used="claude-sonnet-5",
        input_tokens=1_000,
        output_tokens=200,
        cache_creation_input_tokens=500,
        cache_read_input_tokens=9_000,
        web_search_requests=3,
    )
    defaults.update(overrides)
    finding.verification = VerificationResult(**defaults)
    return finding


def _review_result() -> ReviewResult:
    return ReviewResult(
        findings=[_finding()],
        model="claude-opus-5",
        input_tokens=50_000,
        output_tokens=20_000,
        cache_creation_input_tokens=8_000,
        cache_read_input_tokens=120_000,
        stop_reason="tool_use",
    )


def _submission(project_context: str = "") -> BatchSubmission:
    spec = ExtractedSpec(filename="21 10 00.docx", content="Sprinkler body", word_count=2)
    return BatchSubmission(
        job=BatchJob(batch_id="b1", job_type="review", request_map={}, created_at=0.0),
        cross_check_enabled=True,
        prepared_specs=[spec],
        project_context=project_context,
    )


def _install_collection(monkeypatch, *, with_drawings: bool = False):
    """Patch every remote stage so the driver runs end to end with no network.

    Verification is faked at ``verify_findings_for_run`` — the driver's single
    verification entry point — and stamps a VerificationResult on each finding,
    which is exactly how real verification spend reaches diagnostics.
    """
    context = (
        wrap_attachment(DIGEST_ATTACHMENT_LABEL, "DIGEST [drawings.pdf p.1]")
        if with_drawings
        else ""
    )
    submission = _submission(context)

    # Build the collected state from whichever submission the driver was
    # given, so a routed child keeps its own module identity (a fixed state
    # would hand every module the default module's result).
    def fake_collect(target_submission, **_kw):
        return CollectedBatchState(
            submission=target_submission, review_result=_review_result()
        )

    monkeypatch.setattr(pl, "collect_review_batch_results", fake_collect)

    def fake_verify(findings, **_kw):
        for finding in findings:
            _verified(finding)

    monkeypatch.setattr(pl, "verify_findings_for_run", fake_verify)

    def fake_cross_check(state_in, **_kw):
        state_in.cross_check_result = ReviewResult(
            findings=[_finding("cross.docx")],
            model="claude-sonnet-5",
            input_tokens=30_000,
            output_tokens=4_000,
            cross_check_status="completed",
        )
        return state_in

    def fake_compliance(state_in, **_kw):
        state_in.compliance_result = ReviewResult(
            findings=[_finding("compliance.docx")],
            model="claude-sonnet-5",
            input_tokens=25_000,
            output_tokens=3_000,
            cross_check_status="completed",
        )
        return state_in

    def fake_impact(state_in, **_kw):
        state_in.drawing_impact_result = DrawingImpactResult(
            status="completed",
            model="claude-sonnet-5",
            input_tokens=12_000,
            output_tokens=1_500,
        )
        return state_in

    monkeypatch.setattr(pl, "run_cross_check_for_batch", fake_cross_check)
    monkeypatch.setattr(pl, "run_compliance_for_batch", fake_compliance)
    monkeypatch.setattr(pl, "run_drawing_impact_for_batch", fake_impact)
    return submission


def _billable_phases(report: DiagnosticsReport) -> set[str]:
    return {
        event.phase
        for event in report.events
        if (event.data or {}).get("api_call")
    }


# ---------------------------------------------------------------------------
# 1. Every spending phase reaches the report
# ---------------------------------------------------------------------------


class TestHeadlessDriverRecordsEveryPhase:
    def test_all_spending_phases_produce_billable_events(self, monkeypatch):
        submission = _install_collection(monkeypatch, with_drawings=True)
        report = DiagnosticsReport()

        pl.run_batch_collection_headless(
            submission,
            cache=VerificationCache(),
            include_drawing_impact=True,
            diagnostics=report,
        )

        assert _billable_phases(report) == {
            "batch_collect",
            "verification",
            "cross_check",
            "compliance",
            "cross_check_verification",
            "drawing_impact",
        }

    def test_round_two_verification_is_priced(self, monkeypatch):
        """Cross-check + compliance verification used to log only a bare
        'complete' line, so the round — Opus escalations included — never
        reached the cost summary on any driver."""
        submission = _install_collection(monkeypatch)
        report = DiagnosticsReport()

        pl.run_batch_collection_headless(
            submission, cache=VerificationCache(), diagnostics=report
        )

        round2 = [
            e for e in report.events
            if e.phase == "cross_check_verification" and (e.data or {}).get("api_call")
        ]
        assert round2, "round-two verification recorded no billable call"
        assert sum(e.data["input_tokens"] for e in round2) > 0

    def test_review_tokens_land_in_the_cost_summary(self, monkeypatch):
        submission = _install_collection(monkeypatch)
        report = DiagnosticsReport()

        pl.run_batch_collection_headless(
            submission, cache=VerificationCache(), diagnostics=report
        )
        summary = report.summary()

        cost = summary["cost_summary"]["estimated_cost_usd"]
        assert cost["total"] > 0
        # The figure must be built from priced calls, not silently skipped ones.
        assert cost["priced_calls"] >= 6
        assert cost["unpriced_calls"] == 0
        assert "batch_collect" in summary["phase_telemetry"]

    def test_the_review_event_is_tagged_batch_not_realtime(self, monkeypatch):
        submission = _install_collection(monkeypatch)
        report = DiagnosticsReport()

        pl.run_batch_collection_headless(
            submission, cache=VerificationCache(), diagnostics=report
        )

        review = next(e for e in report.events if e.phase == "batch_collect")
        assert review.data["call_mode"] == "batch"

    def test_realtime_transport_does_not_double_count_the_review(
        self, monkeypatch
    ):
        """The real-time runner records one row per spec as its streams
        complete, so the combined carrier must not be recorded again here."""
        submission = _install_collection(monkeypatch)
        submission.review_transport = "realtime"
        report = DiagnosticsReport()

        pl.run_batch_collection_headless(
            submission, cache=VerificationCache(), diagnostics=report
        )

        assert "batch_collect" not in _billable_phases(report)


# ---------------------------------------------------------------------------
# 2. The negative case: no report ⇒ unchanged behavior
# ---------------------------------------------------------------------------


class TestDiagnosticsRemainOptional:
    def test_driver_runs_unchanged_without_a_report(self, monkeypatch):
        submission = _install_collection(monkeypatch, with_drawings=True)

        result = pl.run_batch_collection_headless(
            submission, cache=VerificationCache(), include_drawing_impact=True
        )

        assert result.cross_check_result is not None
        assert result.compliance_result is not None
        assert result.drawing_impact_result is not None

    def test_results_are_identical_with_and_without_a_report(self, monkeypatch):
        """Recording must be observation only — it may not change the run."""
        submission = _install_collection(monkeypatch)
        without = pl.run_batch_collection_headless(
            submission, cache=VerificationCache()
        )

        submission = _install_collection(monkeypatch)
        with_report = pl.run_batch_collection_headless(
            submission, cache=VerificationCache(), diagnostics=DiagnosticsReport()
        )

        assert len(without.review_result.findings) == len(
            with_report.review_result.findings
        )
        assert (
            without.cross_check_result.cross_check_status
            == with_report.cross_check_result.cross_check_status
        )


# ---------------------------------------------------------------------------
# 3. Routed programs thread the report down to the child engine
# ---------------------------------------------------------------------------


def _program_submission(module_id: str = "datacenter_fire") -> "pp.ProgramSubmission":
    """A minimal one-module routed submission, built the way the real one is."""
    from pathlib import Path

    from src.modules import require_module
    from src.orchestration import program_pipeline as pp
    from src.programs import SpecAssignment
    from src.programs.catalog import AVAILABLE_PROGRAMS
    from src.programs.models import RoutingState, SpecRoutingDecision

    program = AVAILABLE_PROGRAMS["hyperscale_datacenter"]
    name = "21 10 00.docx"
    module = require_module(module_id)
    request_id = f"review__{module_id}__0"
    child = BatchSubmission(
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
        cross_check_enabled=True,
        prepared_specs=[
            ExtractedSpec(filename=name, content="Sprinkler body", word_count=2)
        ],
    )
    assignment = SpecAssignment(
        source_path=str(Path("C:/specs") / name),
        decision=SpecRoutingDecision(
            spec_id=name,
            program_id=program.program_id,
            automatic_state=RoutingState.SUPPORTED,
            automatic_module_ids=(module_id,),
            confidence=0.95,
            evidence=(),
        ),
    )
    return pp.ProgramSubmission(
        program_id=program.program_id,
        assignments=(assignment,),
        partitions={module_id: child},
    )


class TestProgramCollectionThreadsDiagnostics:
    """The routed path is the reason this work exists, so it is driven for
    real rather than pinned by source — a substring pin here matched
    pre-existing ``diagnostics=diagnostics`` occurrences elsewhere in the same
    file and stayed green when the forwarding was deleted."""

    def test_child_module_phases_reach_the_programs_report(self, monkeypatch):
        from src.orchestration import program_pipeline as pp

        _install_collection(monkeypatch)
        report = DiagnosticsReport()

        pp.collect_program_results(_program_submission(), diagnostics=report)

        # The child engine's phases must appear on the PROGRAM's report.
        assert {
            "batch_collect",
            "verification",
            "cross_check",
            "compliance",
            "cross_check_verification",
        } <= _billable_phases(report)
        assert report.summary()["cost_summary"]["estimated_cost_usd"]["total"] > 0

    def test_routed_collection_without_a_report_still_succeeds(self, monkeypatch):
        from src.orchestration import program_pipeline as pp

        _install_collection(monkeypatch)
        result = pp.collect_program_results(_program_submission())
        assert result.module_results
