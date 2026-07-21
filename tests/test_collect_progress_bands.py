"""Collect-stage progress bands (WS2).

The collect sequence owns the 55→100 slice of the run's progress bar and
each stage (review collect, verification round 1, cross-check, compliance,
verification round 2, drawing impact, finalize) owns a fixed fraction of
whatever span the driver hands it. Every emission carries a ``stage=``
kwarg so a GUI driver can caption the run button without parsing message
text.

Locked in here:

* ``collect_stage_band`` band math at the default (55, 100) span and the
  program child's raw (0, 100) span.
* ``verify_findings_for_run`` (realtime arm) emits within its band, starts
  at ``band[0]``, ends at ``band[1]``, all tagged with the stage.
* ``run_cross_check_for_batch`` / ``run_compliance_for_batch`` emit their
  band endpoints (including on skip branches) with stage kwargs.
* ``run_batch_collection_headless`` produces a monotone sequence over both
  the default span and a program child's (0, 100) span.
* ``submit_prepared_batch_review``'s ``progress_band`` maps the realtime
  runner's 0-100 fan-out into the caller's band (C4 double-banding fix).
"""
from __future__ import annotations

import time

import pytest

from src.batch.batch import BatchJob
from src.input.extractor import ExtractedSpec
from src.orchestration import pipeline as pl
from src.orchestration.pipeline import (
    BatchSubmission,
    COLLECT_PROGRESS_SPAN,
    CollectedBatchState,
    collect_stage_band,
    run_compliance_for_batch,
    run_cross_check_for_batch,
    verify_findings_for_run,
)
from src.review.realtime_review import REALTIME_JOB_SENTINEL
from src.review.reviewer import Finding, ReviewResult
from src.verification.verifier import VerificationResult


class _ProgressRecorder:
    def __init__(self):
        self.calls: list[tuple[float, str, str | None]] = []

    def __call__(self, pct, msg, *, stage=None, **_kwargs):
        self.calls.append((float(pct), str(msg), stage))

    @property
    def values(self) -> list[float]:
        return [pct for pct, _msg, _stage in self.calls]

    @property
    def stages(self) -> list[str | None]:
        return [stage for _pct, _msg, stage in self.calls]


def _finding(issue: str = "needs verification", filename: str = "a.docx") -> Finding:
    return Finding(
        severity="MEDIUM",
        fileName=filename,
        section="2.1",
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="",
    )


def _spec(filename: str = "a.docx") -> ExtractedSpec:
    content = "Section 23 21 13. Hydronic piping shall comply with the governing code."
    return ExtractedSpec(filename=filename, content=content, word_count=len(content.split()))


def _realtime_submission(specs, results, request_map, **overrides) -> BatchSubmission:
    job = BatchJob(
        batch_id=REALTIME_JOB_SENTINEL,
        job_type="review",
        request_map=dict(request_map),
        created_at=time.time(),
        status="completed",
    )
    ordered = [cid for cid, _ in sorted(request_map.items(), key=lambda i: i[1]["index"])]
    kwargs = dict(
        job=job,
        files_reviewed=[s.filename for s in specs],
        review_request_ids=ordered,
        prepared_specs=list(specs),
        review_transport="realtime",
        realtime_results=dict(results),
    )
    kwargs.update(overrides)
    return BatchSubmission(**kwargs)


# ===========================================================================
# Band math
# ===========================================================================


class TestCollectStageBand:
    def test_default_span_partitions_55_to_100(self):
        stages = [
            "review_collect",
            "verify_round1",
            "cross_check",
            "compliance",
            "verify_round2",
            "drawing_impact",
            "finalize",
        ]
        bands = [collect_stage_band(s) for s in stages]
        assert bands[0][0] == pytest.approx(55.0)
        assert bands[-1][1] == pytest.approx(100.0)
        # Contiguous, ordered partition: each stage starts where the
        # previous ended, and every band is non-inverted.
        for (prev_lo, prev_hi), (lo, hi) in zip(bands, bands[1:]):
            assert prev_hi == pytest.approx(lo)
            assert prev_lo < prev_hi
            assert lo < hi

    def test_program_child_span_partitions_0_to_100(self):
        band = collect_stage_band("verify_round1", (0.0, 100.0))
        assert band == (pytest.approx(10.0), pytest.approx(50.0))
        assert collect_stage_band("finalize", (0.0, 100.0))[1] == pytest.approx(100.0)

    def test_unknown_stage_gets_whole_span(self):
        assert collect_stage_band("future_stage", (55.0, 100.0)) == (55.0, 100.0)


# ===========================================================================
# verify_findings_for_run (realtime arm)
# ===========================================================================


class TestVerifyFindingsBand:
    def _run(self, monkeypatch, *, band, stage):
        findings = [_finding(f"issue {i}") for i in range(3)]
        monkeypatch.setattr(
            pl, "prepare_findings_for_verification", lambda fs, **kw: list(fs)
        )
        monkeypatch.setattr(
            pl,
            "verify_finding",
            lambda f, **kw: VerificationResult(verdict="CONFIRMED", explanation="ok"),
        )
        progress = _ProgressRecorder()
        verify_findings_for_run(
            findings, transport="realtime", progress=progress, band=band, stage=stage
        )
        return progress

    def test_emissions_within_band_and_staged(self, monkeypatch):
        band = collect_stage_band("verify_round2")  # (90.1, 97.75)
        progress = self._run(monkeypatch, band=band, stage="verify_round2")
        assert progress.calls, "expected progress emissions"
        assert progress.values[0] == pytest.approx(band[0])
        assert progress.values[-1] == pytest.approx(band[1])
        assert all(band[0] <= v <= band[1] + 1e-9 for v in progress.values)
        assert set(progress.stages) == {"verify_round2"}

    def test_default_band_preserves_legacy_values(self, monkeypatch):
        progress = self._run(monkeypatch, band=(60.0, 95.0), stage="verify_round1")
        assert progress.values[0] == pytest.approx(60.0)
        assert progress.values[-1] == pytest.approx(95.0)

    def test_all_resolved_locally_emits_band_start(self, monkeypatch):
        monkeypatch.setattr(
            pl, "prepare_findings_for_verification", lambda fs, **kw: []
        )
        progress = _ProgressRecorder()
        verify_findings_for_run(
            [_finding()],
            transport="realtime",
            progress=progress,
            band=(10.0, 50.0),
            stage="verify_round1",
        )
        assert progress.calls == [
            (
                10.0,
                "Verification: all findings resolved locally / cached.",
                "verify_round1",
            )
        ]

    def test_batch_arm_threads_band_through(self, monkeypatch):
        seen: dict = {}

        def fake_start(findings, **kwargs):
            seen["start"] = kwargs
            return None

        monkeypatch.setattr(pl, "start_batch_verification", fake_start)
        verify_findings_for_run(
            [_finding()],
            transport="batch",
            band=(78.0, 95.0),
            stage="verify_round2",
        )
        assert seen["start"]["band"] == (78.0, 95.0)
        assert seen["start"]["stage"] == "verify_round2"


# ===========================================================================
# Cross-check / compliance pass emissions
# ===========================================================================


class TestPassEmissions:
    def test_cross_check_skip_branch_emits_band_endpoints(self):
        submission = _realtime_submission([], {}, {}, cross_check_enabled=True)
        submission.prepared_specs = []
        state = CollectedBatchState(
            submission=submission, review_result=ReviewResult(findings=[])
        )
        progress = _ProgressRecorder()
        band = collect_stage_band("cross_check", (0.0, 100.0))
        run_cross_check_for_batch(state, progress=progress, band=band)
        assert progress.values[0] == pytest.approx(band[0])
        assert progress.values[-1] == pytest.approx(band[1])
        assert set(progress.stages) == {"cross_check"}

    def test_cross_check_disabled_emits_nothing(self):
        submission = _realtime_submission([], {}, {}, cross_check_enabled=False)
        state = CollectedBatchState(
            submission=submission, review_result=ReviewResult(findings=[])
        )
        progress = _ProgressRecorder()
        run_cross_check_for_batch(state, progress=progress)
        assert progress.calls == []

    def test_compliance_flag_off_emits_nothing(self):
        # CA (default module) has the profile flag off — the pass must stay
        # a byte-identical no-op, including zero progress emissions.
        submission = _realtime_submission([_spec()], {}, {})
        state = CollectedBatchState(
            submission=submission, review_result=ReviewResult(findings=[])
        )
        progress = _ProgressRecorder()
        run_compliance_for_batch(state, progress=progress)
        assert progress.calls == []
        assert state.compliance_result is None


# ===========================================================================
# Headless driver: monotone sequence over both spans
# ===========================================================================


class TestHeadlessMonotoneProgress:
    def _drive(self, monkeypatch, tmp_path, *, progress_band):
        monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
        spec = _spec("a.docx")
        finding = _finding(filename="a.docx")
        request_map = {"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}}
        results = {"review__a__0": ReviewResult(findings=[finding], parse_status="ok")}
        submission = _realtime_submission(
            [spec], results, request_map, cross_check_enabled=True
        )
        monkeypatch.setattr(
            pl, "prepare_findings_for_verification", lambda fs, **kw: list(fs)
        )
        monkeypatch.setattr(
            pl,
            "verify_finding",
            lambda f, **kw: VerificationResult(verdict="CONFIRMED", explanation="ok"),
        )
        cross = ReviewResult(findings=[], cross_check_status="completed")
        monkeypatch.setattr(pl, "run_chunked_cross_check", lambda *a, **kw: cross)
        progress = _ProgressRecorder()
        result = pl.run_batch_collection_headless(
            submission, progress=progress, progress_band=progress_band
        )
        assert result.review_result is not None
        return progress

    def test_default_span_is_monotone_55_to_100(self, monkeypatch, tmp_path):
        progress = self._drive(monkeypatch, tmp_path, progress_band=COLLECT_PROGRESS_SPAN)
        values = progress.values
        assert values, "expected emissions"
        assert values == sorted(values), f"non-monotone sequence: {values}"
        assert values[0] == pytest.approx(55.0)
        assert values[-1] == pytest.approx(100.0)
        # The stage tags cover the whole sequence.
        assert None not in progress.stages

    def test_program_child_span_is_monotone_0_to_100(self, monkeypatch, tmp_path):
        progress = self._drive(monkeypatch, tmp_path, progress_band=(0.0, 100.0))
        values = progress.values
        assert values == sorted(values), f"non-monotone sequence: {values}"
        assert values[0] == pytest.approx(0.0)
        assert values[-1] == pytest.approx(100.0)


# ===========================================================================
# C4: submit_prepared_batch_review progress_band pass-through
# ===========================================================================


class TestSubmitProgressBand:
    def _prepared(self):
        from src.modules import DEFAULT_MODULE

        prepared_specs = pl._PreparedSpecs(
            specs=[_spec("a.docx")],
            leed_alerts=[],
            placeholder_alerts=[],
            code_cycle_alerts=[],
            structural_alerts=[],
            naming_alerts=[],
            template_marker_alerts=[],
            invalid_code_cycle_alerts=[],
            duplicate_paragraph_alerts=[],
            polity_alerts=[],
            pre_detected_by_filename={},
        )
        return pl.PreparedBatchReview(
            module=DEFAULT_MODULE,
            prepared=prepared_specs,
            effective_context="",
            requirements_profile=None,
            project_profile=None,
            model="claude-opus-4-8",
            cross_check_enabled=False,
            review_transport="realtime",
        )

    def _run(self, monkeypatch, **submit_kwargs):
        def fake_runner(specs, **kwargs):
            runner_progress = kwargs["progress"]
            runner_progress(50.0, "Reviewed 1/2 specs")
            runner_progress(100.0, "Reviewed 2/2 specs")
            request_map = {
                "review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}
            }
            return (
                {"review__a__0": ReviewResult(findings=[], parse_status="ok")},
                request_map,
            )

        monkeypatch.setattr(pl, "run_realtime_review", fake_runner)
        progress = _ProgressRecorder()
        pl.submit_prepared_batch_review(
            self._prepared(), progress=progress, **submit_kwargs
        )
        return progress

    def test_default_band_maps_25_to_55(self, monkeypatch):
        progress = self._run(monkeypatch)
        assert progress.values == [pytest.approx(40.0), pytest.approx(55.0)]

    def test_program_child_band_is_raw_0_to_100(self, monkeypatch):
        # The program submit mapper re-bands into 25→55 itself; the child
        # must emit raw fractions or the value gets banded twice (C4).
        progress = self._run(monkeypatch, progress_band=(0.0, 100.0))
        assert progress.values == [pytest.approx(50.0), pytest.approx(100.0)]
