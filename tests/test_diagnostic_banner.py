"""Tests for the Run Diagnostics banner.

A styled-table banner right after the title block surfaces operational
health at-a-glance:

* Edit-suggested / Report-only counts (from the
  edit-action histogram already computed for the trust-model summary).
* Cache replays with the oldest entry age (using
  ``cache_entry_created_ts``).
* Verification failures (the ``VERIFICATION_FAILED`` status),
  highlighted red when > 0.
* REPORT_ONLY demotions at parse time (the ``demotion_reason``).
* Spec content extraction warnings (slot reserved for the content-loss
  warning; renders 0 on every run until that lands).
* Cross-spec coordination status — skipped / failed / completed.

A failure recovery hint paragraph appears below the table whenever the
verification-failure count is non-zero so a reviewer can re-run only
those findings.

The plan's success criteria:

* A clean run shows the banner with all counts at expected values.
* A run with simulated verification failures shows red callouts.
* Cross-spec coordination skipped/failed counts appear in the banner.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from docx import Document

from src.modules.registry import require_module
from src.output.report_exporter import (
    _aggregate_run_diagnostics,
    _program_report_title,
    _program_run_diagnostics,
    _summarize_run_diagnostics,
    _write_run_diagnostics_banner,
    export_report,
)
from src.output.report_status import (
    summarize_edit_actions,
    summarize_statuses,
)
from src.review.reviewer import EditProposal, Finding, ReviewResult
from src.verification.verifier import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding(
    *,
    severity: str = "HIGH",
    file: str = "Section_22_1000.docx",
    section: str = "2.1",
    issue: str = "Stale code reference",
    confidence: float = 0.8,
    action: str = "EDIT",
    existing: str | None = "2019 CBC",
    replacement: str | None = "2025 CBC",
    verification: VerificationResult | None = None,
    edit_proposal: EditProposal | None = None,
    demotion_reason: str | None = None,
) -> Finding:
    f = Finding(
        severity=severity,
        fileName=file,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference="CBC §1234",
        confidence=confidence,
        edit_proposal=edit_proposal,
        demotion_reason=demotion_reason,
    )
    f.verification = verification
    return f


def _verified_supported() -> VerificationResult:
    return VerificationResult(
        verdict="CONFIRMED",
        explanation="Verified against CBC §1234.",
        sources=["https://codes.iccsafe.org/content/CBC2025"],
        accepted_sources=["https://codes.iccsafe.org/content/CBC2025"],
        grounded=True,
        cache_status="miss",
        source_quote="The 2025 CBC adopts the 2024 IBC.",
        model_used="claude-sonnet-4-6",
        verification_mode="standard_reasoning",
        web_search_requests=3,
    )


def _failed_verification() -> VerificationResult:
    return VerificationResult(
        verdict="UNVERIFIED",
        explanation="Server overloaded during verification: 529",
        grounded=False,
        verification_failed=True,
    )


def _cache_hit_result(*, age_days: int = 5) -> VerificationResult:
    created_ts = time.time() - (age_days * 86400)
    return VerificationResult(
        verdict="CONFIRMED",
        explanation="Cached verdict from prior run.",
        sources=["https://codes.iccsafe.org/content/CBC2025"],
        accepted_sources=["https://codes.iccsafe.org/content/CBC2025"],
        grounded=True,
        model_used="claude-sonnet-4-6",
        cache_status="hit",
        source_quote="The 2025 CBC adopts the 2024 IBC.",
        cache_entry_created_ts=created_ts,
    )


class _StubPipelineResult:
    """Minimal duck-typed PipelineResult for export_report."""

    def __init__(
        self,
        *,
        review_result: ReviewResult,
        files_reviewed: list[str] | None = None,
        cycle_label: str = "2025",
        cross_check_result: ReviewResult | None = None,
    ):
        self.review_result = review_result
        self.cross_check_result = cross_check_result
        self.files_reviewed = files_reviewed or (
            [review_result.findings[0].fileName] if review_result.findings else ["test.docx"]
        )
        self.leed_alerts = []
        self.placeholder_alerts = []
        self.cycle_label = cycle_label
        self.total_elapsed_seconds = 1.0


def _all_text_from(doc: Document) -> str:
    parts: list[str] = []
    for paragraph in doc.paragraphs:
        parts.append(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _findings_to_summary(
    findings: list[Finding],
    *,
    cross_check_result: ReviewResult | None = None,
    pipeline_result=None,
) -> dict:
    """Convenience: drive _summarize_run_diagnostics with the same
    inputs export_report would feed it."""
    status_counts = summarize_statuses(findings)
    edit_action_counts = summarize_edit_actions(findings)
    return _summarize_run_diagnostics(
        findings=findings,
        status_counts=status_counts,
        edit_action_counts=edit_action_counts,
        cross_check_result=cross_check_result,
        pipeline_result=pipeline_result,
    )


# ---------------------------------------------------------------------------
# 1. _summarize_run_diagnostics — pure function over findings + counts
# ---------------------------------------------------------------------------


class TestSummarizeRunDiagnostics:
    def test_empty_run_returns_zero_counts(self):
        summary = _findings_to_summary([])
        assert summary["edit_suggested"] == 0
        assert summary["report_only"] == 0
        assert summary["verification_failed"] == 0
        assert summary["cache_replay_count"] == 0
        assert summary["oldest_cache_age_days"] is None
        assert summary["demotion_count"] == 0
        assert summary["extraction_warning_count"] == 0
        assert summary["cross_check"] is None

    def test_edit_suggested_count_reflects_proposal(self):
        # Any finding carrying an edit proposal is EDIT_SUGGESTED — the
        # app emits the instruction without gating on confidence.
        f = _finding(verification=_verified_supported(), confidence=0.9)
        summary = _findings_to_summary([f])
        assert summary["edit_suggested"] == 1

    def test_report_only_count_reflects_no_proposal(self):
        f = _finding(
            action="REPORT_ONLY",
            existing=None,
            replacement=None,
            verification=_verified_supported(),
        )
        summary = _findings_to_summary([f])
        assert summary["report_only"] == 1

    def test_verification_failed_count_uses_status_histogram(self):
        f = _finding(verification=_failed_verification())
        summary = _findings_to_summary([f])
        assert summary["verification_failed"] == 1

    def test_cache_replay_counts_only_hit_findings(self):
        hit = _finding(verification=_cache_hit_result(age_days=5))
        miss = _finding(
            file="Section_22_2000.docx",
            verification=_verified_supported(),
        )
        summary = _findings_to_summary([hit, miss])
        assert summary["cache_replay_count"] == 1
        assert summary["oldest_cache_age_days"] == 5

    def test_oldest_cache_age_is_maximum(self):
        # Among several cache hits, the oldest age wins so a reviewer
        # sees the worst-case staleness in the banner.
        ages = [5, 45, 120]
        findings = [
            _finding(
                file=f"Section_22_{i * 1000}.docx",
                verification=_cache_hit_result(age_days=age),
            )
            for i, age in enumerate(ages, start=1)
        ]
        summary = _findings_to_summary(findings)
        assert summary["cache_replay_count"] == 3
        assert summary["oldest_cache_age_days"] == 120

    def test_legacy_cache_hit_counts_but_no_age(self):
        # A cache_status="hit" with cache_entry_created_ts=0.0 (legacy
        # resume payload predating cache-age tracking) counts toward the cache
        # replay total but cannot contribute to the oldest-age display.
        # Note: _enforce_grounding_invariant downgrades verdicts where
        # accepted citation chain is missing, but for a legacy payload
        # the cache_status stays "hit" on the verification result.
        legacy = VerificationResult(
            verdict="CONFIRMED",
            sources=["https://x"],
            accepted_sources=["https://x"],
            grounded=True,
            cache_status="hit",
            source_quote="snippet",
            cache_entry_created_ts=0.0,
        )
        f = _finding(verification=legacy)
        summary = _findings_to_summary([f])
        assert summary["cache_replay_count"] == 1
        assert summary["oldest_cache_age_days"] is None

    def test_demotion_count_reflects_demotion_reason_field(self):
        # Findings with a parse-time demotion are the ones surfaced in
        # this row — they signal model-output shape issues, not a
        # deliberate REPORT_ONLY.
        f = _finding(
            action="REPORT_ONLY",
            existing=None,
            replacement=None,
            demotion_reason="EDIT requested but existingText was empty",
        )
        summary = _findings_to_summary([f])
        assert summary["demotion_count"] == 1

    def test_demotion_count_ignores_empty_or_whitespace_reasons(self):
        # An empty string or pure whitespace must not be counted as a
        # demotion — those represent the "no reason recorded" default.
        f1 = _finding(action="REPORT_ONLY", existing=None, replacement=None, demotion_reason="")
        f2 = _finding(
            action="REPORT_ONLY",
            existing=None,
            replacement=None,
            file="Other.docx",
            demotion_reason="   ",
        )
        summary = _findings_to_summary([f1, f2])
        assert summary["demotion_count"] == 0

    def test_cross_check_state_completed(self):
        cc = ReviewResult(findings=[], cross_check_status="completed")
        summary = _findings_to_summary([], cross_check_result=cc)
        assert summary["cross_check"] is not None
        assert summary["cross_check"]["status"] == "completed"
        assert summary["cross_check"]["finding_count"] == 0

    def test_cross_check_state_skipped(self):
        cc = ReviewResult(
            findings=[],
            cross_check_status="skipped",
            thinking="not enough specs",
        )
        summary = _findings_to_summary([], cross_check_result=cc)
        assert summary["cross_check"]["status"] == "skipped"
        assert summary["cross_check"]["reason"] == "not enough specs"

    def test_cross_check_state_failed(self):
        cc = ReviewResult(
            findings=[],
            cross_check_status="failed",
            error="API timeout",
        )
        summary = _findings_to_summary([], cross_check_result=cc)
        assert summary["cross_check"]["status"] == "failed"
        assert summary["cross_check"]["reason"] == "API timeout"

    def test_cross_check_none_means_not_run(self):
        # No cross-check requested → no cross-check row in the banner.
        # The summary records None so the renderer can suppress the row.
        summary = _findings_to_summary([], cross_check_result=None)
        assert summary["cross_check"] is None

    def test_cross_check_state_carries_chunk_failure_counts(self):
        # A "completed" chunked pass that had a failed/skipped chunk carries
        # the counts so the renderer can flag the partially-incomplete pass
        # (TRUST_AUDIT P1-3 follow-up).
        cc = ReviewResult(
            findings=[],
            cross_check_status="completed",
            chunk_failures=1,
            chunk_skips=2,
        )
        summary = _findings_to_summary([], cross_check_result=cc)
        assert summary["cross_check"]["chunk_failures"] == 1
        assert summary["cross_check"]["chunk_skips"] == 2

    def test_cross_check_chunk_counts_default_zero(self):
        # A non-chunked result (no chunk fields set) reports 0 — the banner
        # row stays unhighlighted, identical to before.
        cc = ReviewResult(findings=[], cross_check_status="completed")
        summary = _findings_to_summary([], cross_check_result=cc)
        assert summary["cross_check"]["chunk_failures"] == 0
        assert summary["cross_check"]["chunk_skips"] == 0


# ---------------------------------------------------------------------------
# 2. Banner rendering — section heading + labels
# ---------------------------------------------------------------------------


class TestBannerRendering:
    def test_banner_heading_appears_in_report(self, tmp_path: Path):
        f = _finding(verification=_verified_supported())
        out = tmp_path / "report.docx"
        export_report(
            _StubPipelineResult(review_result=ReviewResult(findings=[f])), out
        )
        text = _all_text_from(Document(str(out)))
        assert "Run Diagnostics" in text


# ---------------------------------------------------------------------------
# 3. Banner content — counts reflect the input findings
# ---------------------------------------------------------------------------


class TestBannerCounts:
    @pytest.mark.parametrize(
        "n_failures, expected_phrase",
        [
            (1, "1 finding failed verification"),
            (2, "2 findings failed verification"),
        ],
    )
    def test_failure_hint_count_and_pluralization(
        self, tmp_path: Path, n_failures: int, expected_phrase: str
    ):
        # The failure-hint paragraph copies the failure count into its
        # prose and pluralizes "finding" vs "findings" accordingly. One
        # supported finding rides along for the singular case so the
        # banner also reports a non-failure.
        findings = [
            _finding(file=f"Failed_{i}.docx", verification=_failed_verification())
            for i in range(n_failures)
        ]
        if n_failures == 1:
            findings.append(_finding(file="OK.docx", verification=_verified_supported()))
        out = tmp_path / "report.docx"
        export_report(
            _StubPipelineResult(review_result=ReviewResult(findings=findings)),
            out,
        )
        text = _all_text_from(Document(str(out)))
        assert "Verification failures (operational)" in text
        assert expected_phrase in text


# ---------------------------------------------------------------------------
# 4. Cross-check status in the banner
# ---------------------------------------------------------------------------


class TestBannerCrossCheckStatus:
    def test_cross_check_none_omits_row(self, tmp_path: Path):
        # When cross-check wasn't run, no row is rendered (the
        # _StubPipelineResult passes None by default). The renderer's
        # row-suppression branch — the helper class covers the populated
        # states.
        f = _finding(verification=_verified_supported())
        out = tmp_path / "report.docx"
        export_report(
            _StubPipelineResult(review_result=ReviewResult(findings=[f])),
            out,
        )
        text = _all_text_from(Document(str(out)))
        assert "Cross-spec coordination" not in text


# ---------------------------------------------------------------------------
# 5. Visual highlight — verification failures + extraction warnings
# ---------------------------------------------------------------------------


class TestBannerHighlights:
    """The plan calls for verification failures to highlight in red
    when > 0 — we verify by inspecting cell shading on the value
    column. Same treatment applies to extraction warnings."""

    def _value_cell_shading_for_label(self, doc: Document, label: str) -> str | None:
        """Walk every table in the doc; return the value-cell shading
        for the row whose first cell text matches ``label``.

        Returns the hex string (e.g., "FFE5E5") or ``None`` if the row
        has no shading element on its value cell.
        """
        from docx.oxml.ns import qn

        for table in doc.tables:
            for row in table.rows:
                if len(row.cells) < 2:
                    continue
                if row.cells[0].text.strip() != label:
                    continue
                value_cell = row.cells[1]
                tcPr = value_cell._tc.find(qn("w:tcPr"))
                if tcPr is None:
                    return None
                shd = tcPr.find(qn("w:shd"))
                if shd is None:
                    return None
                return shd.get(qn("w:fill"))
        return None

    def test_verification_failure_value_cell_is_red_when_nonzero(
        self, tmp_path: Path
    ):
        f = _finding(verification=_failed_verification())
        out = tmp_path / "report.docx"
        export_report(
            _StubPipelineResult(review_result=ReviewResult(findings=[f])), out
        )
        doc = Document(str(out))
        shading = self._value_cell_shading_for_label(
            doc, "Verification failures (operational)"
        )
        assert shading is not None
        # The light-red shading we apply is FFE5E5; the exact value is
        # an implementation detail but it must be a red-family hex.
        assert shading.upper() == "FFE5E5"

    def test_verification_failure_value_cell_unshaded_when_zero(
        self, tmp_path: Path
    ):
        f = _finding(verification=_verified_supported())
        out = tmp_path / "report.docx"
        export_report(
            _StubPipelineResult(review_result=ReviewResult(findings=[f])), out
        )
        doc = Document(str(out))
        shading = self._value_cell_shading_for_label(
            doc, "Verification failures (operational)"
        )
        # No shading element means we did not apply the highlight.
        assert shading is None

    def test_partial_chunk_failure_reds_the_coordination_row(self, tmp_path: Path):
        # A "completed" chunked pass with a failed chunk must NOT read as a
        # clean green row — it is highlighted red and annotated "not analyzed"
        # so the operator sees that a division's coordination did not run
        # (TRUST_AUDIT P1-3 follow-up).
        f = _finding(verification=_verified_supported())
        cc = ReviewResult(findings=[], cross_check_status="completed", chunk_failures=1)
        out = tmp_path / "report.docx"
        export_report(
            _StubPipelineResult(review_result=ReviewResult(findings=[f]), cross_check_result=cc),
            out,
        )
        doc = Document(str(out))
        shading = self._value_cell_shading_for_label(doc, "Cross-spec coordination")
        assert shading is not None
        assert shading.upper() == "FFE5E5"
        assert "not analyzed" in _all_text_from(doc)

    def test_completed_chunked_pass_without_failures_not_red(self, tmp_path: Path):
        # A fully-completed pass (no failed/skipped chunks) stays unhighlighted.
        f = _finding(verification=_verified_supported())
        cc = ReviewResult(findings=[], cross_check_status="completed", chunk_failures=0)
        out = tmp_path / "report.docx"
        export_report(
            _StubPipelineResult(review_result=ReviewResult(findings=[f]), cross_check_result=cc),
            out,
        )
        doc = Document(str(out))
        shading = self._value_cell_shading_for_label(doc, "Cross-spec coordination")
        assert shading is None
        assert "not analyzed" not in _all_text_from(doc)


# ---------------------------------------------------------------------------
# 6. Tracked-changes advisory (specs read as Accept-All)
# ---------------------------------------------------------------------------


class _SpecStub:
    def __init__(self, tracked: bool):
        self.tracked_changes_detected = tracked
        self.extraction_warnings: list[str] = []


class _PipelineWithSpecs:
    """Minimal pipeline-result double carrying extracted specs."""

    def __init__(self, specs: list[_SpecStub]):
        self.extracted_specs = specs
        self.failed_review_specs: list[str] = []


class TestTrackedChangesAdvisory:
    def test_count_reflects_flagged_specs(self):
        pr = _PipelineWithSpecs([_SpecStub(True), _SpecStub(False), _SpecStub(True)])
        summary = _findings_to_summary([], pipeline_result=pr)
        assert summary["tracked_changes_spec_count"] == 2

    def test_count_zero_without_specs(self):
        summary = _findings_to_summary([])
        assert summary["tracked_changes_spec_count"] == 0

    def test_row_and_hint_render_when_present(self):
        pr = _PipelineWithSpecs([_SpecStub(True), _SpecStub(True)])
        summary = _findings_to_summary([], pipeline_result=pr)
        doc = Document()
        _write_run_diagnostics_banner(doc, summary)
        text = _all_text_from(doc)
        assert "Specs with tracked changes (read as accept-all)" in text
        assert "2 specs contained pending tracked changes" in text
        assert "insertions kept, deletions removed" in text

    def test_singular_pluralization(self):
        pr = _PipelineWithSpecs([_SpecStub(True)])
        summary = _findings_to_summary([], pipeline_result=pr)
        doc = Document()
        _write_run_diagnostics_banner(doc, summary)
        text = _all_text_from(doc)
        assert "1 spec contained pending tracked changes" in text
        assert "It was reviewed as if all changes were accepted" in text

    def test_advisory_absent_when_zero(self):
        pr = _PipelineWithSpecs([_SpecStub(False)])
        summary = _findings_to_summary([], pipeline_result=pr)
        doc = Document()
        _write_run_diagnostics_banner(doc, summary)
        text = _all_text_from(doc)
        # Row and hint are both gated on count > 0 so a clean run is unchanged.
        assert "read as accept-all" not in text
        assert "contained pending tracked changes" not in text

    def test_advisory_value_cell_not_red(self):
        # Informational, not a failure — the row must not carry the red
        # problem-shading used by verification-failure / extraction-warning rows.
        from docx.oxml.ns import qn

        pr = _PipelineWithSpecs([_SpecStub(True)])
        summary = _findings_to_summary([], pipeline_result=pr)
        doc = Document()
        _write_run_diagnostics_banner(doc, summary)
        for table in doc.tables:
            for row in table.rows:
                if len(row.cells) < 2:
                    continue
                if row.cells[0].text.strip() == "Specs with tracked changes (read as accept-all)":
                    tcPr = row.cells[1]._tc.find(qn("w:tcPr"))
                    shd = None if tcPr is None else tcPr.find(qn("w:shd"))
                    assert shd is None  # no red highlight on an informational row
                    return
        raise AssertionError("tracked-changes advisory row not found")


# ---------------------------------------------------------------------------
# 7. Program (routed multi-module) reports — ONE program-level banner
# ---------------------------------------------------------------------------


def _program_result(*, failed_fire_spec: bool = False):
    """A two-module hyperscale program result.

    The fire module reviewed two specs; with ``failed_fire_spec=True`` its
    review of the second one failed (no findings for that reason). The
    electrical module carries one finding whose verification failed, so the
    aggregate banner has a second, differently-sourced red row to sum.
    """
    from src.orchestration.pipeline import PipelineResult
    from src.orchestration.program_pipeline import ProgramPipelineResult
    from src.programs.assignments import SpecAssignment
    from src.programs.models import (
        RoutingEvidence,
        RoutingEvidenceSource,
        RoutingState,
        SpecRoutingDecision,
    )

    def _assignment(spec_id: str, module_id: str) -> SpecAssignment:
        decision = SpecRoutingDecision(
            spec_id=spec_id,
            program_id="hyperscale_datacenter",
            automatic_state=RoutingState.SUPPORTED,
            automatic_module_ids=(module_id,),
            confidence=0.9,
            evidence=(
                RoutingEvidence(
                    source=RoutingEvidenceSource.CSI_SECTION,
                    signal="csi-prefix",
                    detail="division prefix match",
                    module_id=module_id,
                    weight=0.9,
                ),
            ),
        )
        return SpecAssignment(source_path=f"/specs/{spec_id}", decision=decision)

    fire = PipelineResult(
        review_result=ReviewResult(
            findings=[_finding(file="FS-21-1300.docx", verification=_verified_supported())]
        ),
        files_reviewed=["FS-21-1300.docx", "FS-21-2200.docx"],
        failed_review_specs=["FS-21-2200.docx"] if failed_fire_spec else [],
        cycle_label=require_module("datacenter_fire").cycle.label,
        module_id="datacenter_fire",
    )
    electrical = PipelineResult(
        review_result=ReviewResult(
            findings=[_finding(file="EL-26-0500.docx", verification=_failed_verification())]
        ),
        files_reviewed=["EL-26-0500.docx"],
        cycle_label=require_module("datacenter_electrical").cycle.label,
        module_id="datacenter_electrical",
    )
    return ProgramPipelineResult(
        program_id="hyperscale_datacenter",
        assignments=(
            _assignment("FS-21-1300.docx", "datacenter_fire"),
            _assignment("FS-21-2200.docx", "datacenter_fire"),
            _assignment("EL-26-0500.docx", "datacenter_electrical"),
        ),
        module_results={"datacenter_fire": fire, "datacenter_electrical": electrical},
    )


def _banner_table(doc: Document):
    """The (single) Run Diagnostics table: the one whose first row is 'Edit suggested'."""
    tables = [
        table
        for table in doc.tables
        if table.rows and table.rows[0].cells[0].text.strip() == "Edit suggested"
    ]
    assert len(tables) == 1, f"expected exactly one banner table, found {len(tables)}"
    return tables[0]


def _banner_value(doc: Document, label: str) -> str | None:
    for row in _banner_table(doc).rows:
        if row.cells[0].text.strip() == label:
            return row.cells[1].text.strip()
    return None


def _banner_value_shading(doc: Document, label: str) -> str | None:
    from docx.oxml.ns import qn

    for row in _banner_table(doc).rows:
        if row.cells[0].text.strip() != label:
            continue
        tcPr = row.cells[1]._tc.find(qn("w:tcPr"))
        shd = tcPr.find(qn("w:shd")) if tcPr is not None else None
        return None if shd is None else shd.get(qn("w:fill"))
    return None


def _headings(doc: Document) -> list[str]:
    return [
        p.text
        for p in doc.paragraphs
        if p.style.name == "Title" or p.style.name.startswith("Heading")
    ]


class TestAggregateRunDiagnostics:
    """``_aggregate_run_diagnostics`` rolls per-module summaries into one
    summary of the same shape, so the single-module banner renderer draws
    the program banner unchanged."""

    @staticmethod
    def _summary(**overrides) -> dict:
        base = _findings_to_summary([])
        base.update(overrides)
        return base

    def test_empty_input_keeps_the_single_module_shape(self):
        agg = _aggregate_run_diagnostics([])
        assert set(agg) == set(_findings_to_summary([]))
        assert agg["failed_review_count"] == 0
        assert agg["failed_review_specs"] == []
        assert agg["oldest_cache_age_days"] is None
        for key in ("cross_check", "research", "compliance", "drawing_impact"):
            assert agg[key] is None

    def test_counts_sum_and_failed_specs_union_with_module_prefix(self):
        fire = self._summary(
            edit_suggested=2,
            report_only=1,
            verification_failed=1,
            cache_replay_count=1,
            oldest_cache_age_days=5,
            demotion_count=1,
            extraction_warning_count=1,
            tracked_changes_spec_count=1,
            budget_exhausted_count=1,
            failed_review_specs=["a.docx"],
            failed_review_count=1,
        )
        electrical = self._summary(
            edit_suggested=1,
            cache_replay_count=2,
            oldest_cache_age_days=40,
            failed_review_specs=["b.docx", "c.docx"],
            failed_review_count=2,
        )
        agg = _aggregate_run_diagnostics([("Fire", fire), ("Electrical", electrical)])
        assert agg["edit_suggested"] == 3
        assert agg["report_only"] == 1
        assert agg["verification_failed"] == 1
        assert agg["cache_replay_count"] == 3
        assert agg["oldest_cache_age_days"] == 40
        assert agg["demotion_count"] == 1
        assert agg["extraction_warning_count"] == 1
        assert agg["tracked_changes_spec_count"] == 1
        assert agg["budget_exhausted_count"] == 1
        assert agg["failed_review_count"] == 3
        assert agg["failed_review_specs"] == [
            "Fire: a.docx",
            "Electrical: b.docx",
            "Electrical: c.docx",
        ]

    def test_cross_check_reports_worst_status_with_summed_chunk_counts(self):
        completed = {
            "status": "completed",
            "finding_count": 2,
            "chunk_failures": 1,
            "chunk_skips": 0,
            "reason": "",
        }
        skipped = {
            "status": "skipped",
            "finding_count": 0,
            "chunk_failures": 0,
            "chunk_skips": 1,
            "reason": "only one spec routed",
        }
        failed = {
            "status": "failed",
            "finding_count": 0,
            "chunk_failures": 0,
            "chunk_skips": 0,
            "reason": "coordination call errored",
        }
        agg = _aggregate_run_diagnostics(
            [("Fire", self._summary(cross_check=completed)),
             ("Electrical", self._summary(cross_check=skipped))]
        )
        assert agg["cross_check"] == {
            "status": "skipped",
            "finding_count": 2,
            "chunk_failures": 1,
            "chunk_skips": 1,
            "reason": "Electrical: only one spec routed",
        }
        agg = _aggregate_run_diagnostics(
            [("Fire", self._summary(cross_check=skipped)),
             ("Electrical", self._summary(cross_check=failed))]
        )
        assert agg["cross_check"]["status"] == "failed"
        assert agg["cross_check"]["reason"] == "Electrical: coordination call errored"

    def test_conditional_states_stay_none_only_when_no_module_carried_them(self):
        ran = {
            "status": "completed",
            "finding_count": 1,
            "chunk_failures": 0,
            "chunk_skips": 0,
            "reason": "",
        }
        agg = _aggregate_run_diagnostics(
            [("Fire", self._summary()), ("Electrical", self._summary(cross_check=ran))]
        )
        assert agg["cross_check"] == {**ran, "reason": ""}
        assert agg["research"] is None
        assert agg["compliance"] is None

    def test_research_and_compliance_counts_sum(self):
        r1 = {
            "dimensions_total": 4,
            "dimensions_completed": 3,
            "dimensions_failed": 1,
            "item_count": 10,
            "ungrounded_count": 2,
        }
        r2 = {
            "dimensions_total": 3,
            "dimensions_completed": 3,
            "dimensions_failed": 0,
            "item_count": 5,
            "ungrounded_count": 0,
        }
        c1 = {
            "status": "completed",
            "finding_count": 2,
            "missing": 1,
            "contradicted": 0,
            "chunk_failures": 0,
            "chunk_skips": 0,
            "reason": "",
        }
        c2 = {
            "status": "completed",
            "finding_count": 1,
            "missing": 0,
            "contradicted": 1,
            "chunk_failures": 1,
            "chunk_skips": 0,
            "reason": "",
        }
        agg = _aggregate_run_diagnostics(
            [("Fire", self._summary(research=r1, compliance=c1)),
             ("Electrical", self._summary(research=r2, compliance=c2))]
        )
        assert agg["research"] == {
            "dimensions_total": 7,
            "dimensions_completed": 6,
            "dimensions_failed": 1,
            "item_count": 15,
            "ungrounded_count": 2,
        }
        assert agg["compliance"] == {
            "status": "completed",
            "finding_count": 3,
            "missing": 1,
            "contradicted": 1,
            "chunk_failures": 1,
            "chunk_skips": 0,
            "reason": "",
        }

    def test_drawing_impact_prefers_the_program_level_result(self):
        class _Impact:
            status = "completed"
            impact_level = "moderate"
            linked_finding_count = 3
            error = ""

        agg = _aggregate_run_diagnostics(
            [("Fire", self._summary())], drawing_impact_result=_Impact()
        )
        assert agg["drawing_impact"] == {
            "status": "completed",
            "impact_level": "moderate",
            "linked_finding_count": 3,
            "error": "",
        }
        module_level = {
            "status": "failed",
            "impact_level": "",
            "linked_finding_count": 0,
            "error": "boom",
        }
        agg = _aggregate_run_diagnostics(
            [("Fire", self._summary(drawing_impact=module_level))]
        )
        assert agg["drawing_impact"] == module_level

    def test_aggregate_renders_through_the_single_module_banner(self):
        fire = self._summary(
            failed_review_specs=["a.docx"], failed_review_count=1, verification_failed=2
        )
        agg = _aggregate_run_diagnostics([("Fire", fire)])
        doc = Document()
        _write_run_diagnostics_banner(doc, agg)
        text = _all_text_from(doc)
        assert "⚠ 1 spec failed review and was NOT reviewed: Fire: a.docx." in text
        assert "2 findings failed verification" in text


class TestProgramBanner:
    """``export_report`` on a program result renders the same banner + hints
    as a single-module report, once, aggregated across the modules."""

    @staticmethod
    def _export(tmp_path: Path, result) -> Document:
        out = tmp_path / "program.docx"
        export_report(result, out)
        return Document(str(out))

    def test_failed_review_row_red_and_hint_names_module_and_spec(self, tmp_path: Path):
        doc = self._export(tmp_path, _program_result(failed_fire_spec=True))
        text = _all_text_from(doc)
        fire = require_module("datacenter_fire").display_name
        assert "Run Diagnostics" in text
        assert _banner_value(doc, "Specs that failed review (not reviewed)") == "1"
        assert _banner_value_shading(doc, "Specs that failed review (not reviewed)") == "FFE5E5"
        assert (
            f"⚠ 1 spec failed review and was NOT reviewed: {fire}: FS-21-2200.docx."
            in text
        )
        assert "does NOT mean it is compliant" in text
        # The module's own Files Reviewed list still annotates the spec.
        assert "FS-21-2200.docx — review failed (not reviewed)" in text
        # The other module's verification failure aggregates into the same banner.
        assert _banner_value(doc, "Verification failures (operational)") == "1"
        assert _banner_value_shading(doc, "Verification failures (operational)") == "FFE5E5"
        assert "1 finding failed verification" in text

    def test_clean_program_shows_zero_row_and_no_hint(self, tmp_path: Path):
        doc = self._export(tmp_path, _program_result())
        text = _all_text_from(doc)
        assert _banner_value(doc, "Specs that failed review (not reviewed)") == "0"
        assert _banner_value_shading(doc, "Specs that failed review (not reviewed)") is None
        assert "failed review and" not in text

    def test_banner_once_after_title_and_trust_summary_after_program_summary(
        self, tmp_path: Path
    ):
        doc = self._export(tmp_path, _program_result())
        headings = _headings(doc)
        fire = require_module("datacenter_fire").display_name
        assert headings.count("Run Diagnostics") == 1
        assert headings.count("Trust Model Summary") == 1
        assert headings.index("Run Diagnostics") == 1  # right after the title
        assert headings.index("Run Diagnostics") < headings.index(
            "Review Coverage and Routing"
        )
        assert (
            headings.index("Review Coverage and Routing")
            < headings.index("Trust Model Summary")
            < headings.index(fire)
        )

    def test_title_uses_the_program_display_name(self, tmp_path: Path):
        from src.programs import get_program

        doc = self._export(tmp_path, _program_result())
        program = get_program("hyperscale_datacenter")
        assert doc.paragraphs[0].text == _program_report_title(program)
        assert (
            doc.paragraphs[0].text
            == f"Spec Critic — {program.display_name} Specification Review Report"
        )

    def test_banner_rows_equal_the_shared_program_aggregate(self, tmp_path: Path):
        result = _program_result(failed_fire_spec=True)
        doc = self._export(tmp_path, result)
        aggregate, stats = _program_run_diagnostics(result)
        fire = require_module("datacenter_fire").display_name
        assert aggregate["failed_review_specs"] == [f"{fire}: FS-21-2200.docx"]
        assert aggregate["verification_failed"] == 1
        assert _banner_value(doc, "Edit suggested") == str(aggregate["edit_suggested"])
        assert _banner_value(doc, "Report-only") == str(aggregate["report_only"])
        assert sum(stats["status_counts"].values()) == 2
