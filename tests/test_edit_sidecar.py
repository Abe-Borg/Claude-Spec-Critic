"""Tests for the machine-readable edit-instructions sidecar.

Spec Critic emits edit instructions but no longer applies them. After the
Word report is written, ``edit_sidecar.write_edit_instructions_sidecar``
drops a ``<report-stem>.edits.json`` file beside the report listing every
finding that carries an edit proposal, for a downstream applier to ingest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.output.edit_sidecar import (
    SIDECAR_SCHEMA_VERSION,
    build_edit_instructions,
    write_edit_instructions_sidecar,
)
from src.review.reviewer import EditProposal, Finding, ReviewResult
from src.verification.verifier import VerificationResult


@dataclass
class _StubPipelineResult:
    review_result: ReviewResult | None = None
    cross_check_result: ReviewResult | None = None
    cycle_label: str = "California 2025"


def _finding_with_edit(**kw) -> Finding:
    return Finding(
        severity=kw.get("severity", "HIGH"),
        fileName=kw.get("fileName", "Section_23_0000.docx"),
        section=kw.get("section", "2.1"),
        issue=kw.get("issue", "Stale code reference"),
        actionType="EDIT",
        existingText="2019 CBC",
        replacementText="2025 CBC",
        codeReference="CBC 2025",
        confidence=0.9,
        edit_proposal=EditProposal(
            action_type="EDIT",
            existing_text="2019 CBC",
            replacement_text="2025 CBC",
            edit_confidence=0.9,
        ),
    )


def _report_only_finding() -> Finding:
    return Finding(
        severity="MEDIUM",
        fileName="Section_23_0000.docx",
        section="3.0",
        issue="Coordination concern with structural.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
        confidence=0.7,
    )


def test_proposal_finding_emitted_in_payload():
    f = _finding_with_edit()
    f.verification = VerificationResult(verdict="CORRECTED", grounded=True, sources=["https://x"])
    result = _StubPipelineResult(review_result=ReviewResult(findings=[f]))
    payload = build_edit_instructions(result, report_path=Path("report.docx"))

    assert payload["schema_version"] == SIDECAR_SCHEMA_VERSION
    assert payload["report_file"] == "report.docx"
    assert payload["cycle_label"] == "California 2025"
    assert payload["edit_count"] == 1
    entry = payload["edits"][0]
    assert entry["fileName"] == "Section_23_0000.docx"
    assert entry["edit_proposal"]["existing_text"] == "2019 CBC"
    assert entry["edit_proposal"]["replacement_text"] == "2025 CBC"
    assert entry["verification_verdict"] == "CORRECTED"
    assert entry["report_status"] == "VERIFIED_CONTRADICTED"


def test_report_only_finding_omitted():
    result = _StubPipelineResult(
        review_result=ReviewResult(findings=[_report_only_finding()])
    )
    payload = build_edit_instructions(result)
    assert payload["edit_count"] == 0
    assert payload["edits"] == []


def test_cross_check_findings_included_in_payload():
    cc_finding = _finding_with_edit(section="4.0")
    cc = ReviewResult(findings=[cc_finding], cross_check_status="completed")
    result = _StubPipelineResult(
        review_result=ReviewResult(findings=[]), cross_check_result=cc
    )
    payload = build_edit_instructions(result)
    assert payload["edit_count"] == 1
    assert payload["edits"][0]["section"] == "4.0"


def test_write_sidecar_creates_file_next_to_report(tmp_path: Path):
    f = _finding_with_edit()
    result = _StubPipelineResult(review_result=ReviewResult(findings=[f]))
    report_path = tmp_path / "spec-critic-report-2026-05-27.docx"

    sidecar = write_edit_instructions_sidecar(result, report_path)

    assert sidecar == tmp_path / "spec-critic-report-2026-05-27.edits.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["edit_count"] == 1
    assert data["report_file"] == report_path.name
