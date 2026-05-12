"""Phase 7 regression tests: explicit log levels and actionable diagnostics."""

from __future__ import annotations

from src.diagnostics import DiagnosticsReport
from src.pipeline import (
    _log_cross_check_status,
    classify_cross_check_dependencies,
)
from src.reviewer import Finding, ReviewResult
from src.verifier import VerificationResult


def _make_finding(*, file: str = "A.docx", section: str = "1.0", verdict: str | None = None) -> Finding:
    f = Finding(
        severity="HIGH",
        fileName=file,
        section=section,
        issue="issue",
        actionType="EDIT",
        existingText="x",
        replacementText="y",
        codeReference="CBC 1.0",
    )
    if verdict is not None:
        f.verification = VerificationResult(verdict=verdict, explanation="")
    return f


# ---------------------------------------------------------------------------
# Phase 7.1: explicit log levels are passed through (no keyword sniffing)
# ---------------------------------------------------------------------------

def test_log_cross_check_status_emits_explicit_levels():
    """Pipeline emits explicit log levels rather than relying on the GUI to
    keyword-sniff message text. Failures should arrive as ``error`` and
    skips as ``warning`` so the diagnostics report classifies them correctly.
    """
    captured: list[tuple[str, str]] = []

    def log(msg: str, *, level: str = "info") -> None:
        captured.append((level, msg))

    _log_cross_check_status(log, ReviewResult(findings=[], cross_check_status="completed"))
    _log_cross_check_status(log, ReviewResult(findings=[], cross_check_status="skipped", thinking="too small"))
    _log_cross_check_status(log, ReviewResult(findings=[], cross_check_status="failed", error="boom"))

    levels = [lvl for lvl, _msg in captured]
    assert "success" in levels  # completed + zero findings
    assert "warning" in levels  # skipped
    assert "error" in levels    # failed


def test_drop_cross_check_findings_uses_warning_level():
    """The dependency-drop log call should arrive at ``level="warning"``."""
    cross = [_make_finding(file="A.docx", section="2.1")]
    review = [_make_finding(file="A.docx", section="2.1", verdict="DISPUTED")]
    captured: list[tuple[str, str]] = []

    def log(msg: str, *, level: str = "info") -> None:
        captured.append((level, msg))

    kept, _suppressed = classify_cross_check_dependencies(cross, review, log=log)
    assert kept == []
    assert any(lvl == "warning" for lvl, _msg in captured)


# ---------------------------------------------------------------------------
# Phase 7.3: actionable diagnostics
# ---------------------------------------------------------------------------

def test_diagnostics_records_failed_and_skipped_specs():
    report = DiagnosticsReport()
    report.record_failed_spec("alpha.docx")
    report.record_failed_spec("alpha.docx")  # dedupe
    report.record_failed_spec("beta.docx")
    report.record_skipped_spec("gamma.docx")
    report.finish()

    summary = report.summary()
    assert summary["failed_specs"] == ["alpha.docx", "beta.docx"]
    assert summary["skipped_specs"] == ["gamma.docx"]


def test_diagnostics_records_edit_skip_reasons_and_ambiguous_count():
    report = DiagnosticsReport()
    report.record_edit_skip("ambiguous")
    report.record_edit_skip("ambiguous")
    report.record_edit_skip("not_found")
    report.record_edit_report(applied=2, skipped=1, failed=1)
    report.record_edit_report(applied=1, skipped=0, failed=0)
    report.finish()

    summary = report.summary()
    assert summary["edit_skip_reasons"] == {"ambiguous": 2, "not_found": 1}
    assert summary["ambiguous_locator_count"] == 2
    assert summary["edits_applied_total"] == 3
    assert summary["edits_skipped_total"] == 1
    assert summary["edits_failed_total"] == 1


def test_diagnostics_to_text_renders_actionable_section():
    report = DiagnosticsReport()
    report.record_failed_spec("23 0500.docx")
    report.record_edit_skip("ambiguous")
    report.record_edit_report(applied=1, skipped=0, failed=0)
    report.finish()

    text = report.to_text()
    assert "Failed Specs" in text
    assert "23 0500.docx" in text
    assert "Edit Skips" in text
    assert "Ambiguous Locators" in text
    assert "Edit Application" in text


def test_diagnostics_caps_event_log_and_records_truncation():
    """Long-running batch polls should not grow the event list unbounded.
    Old events are dropped FIFO and ``events_dropped`` counts the loss so
    the summary can flag truncation.
    """
    report = DiagnosticsReport(max_events=10)
    for i in range(25):
        report.log("phase", "info", f"event {i}")
    report.finish()

    assert len(report.events) == 10
    assert report.events_dropped == 15
    summary = report.summary()
    assert summary["events_dropped"] == 15
    # Newest events retained, oldest dropped.
    assert report.events[-1].message == "event 24"
    assert report.events[0].message == "event 15"
