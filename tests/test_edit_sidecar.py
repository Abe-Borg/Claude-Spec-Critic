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

from src.orchestration.pipeline import _deduplicate_findings
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


# ---------------------------------------------------------------------------
# Per-file fan-out (TRUST_AUDIT P0-1 / P0-2)
#
# When _deduplicate_findings collapses the same defect across N templated specs
# into one merged finding, the sidecar must still emit an actionable edit
# instruction for EVERY affected file — not just the representative — each with
# that file's own locator, while display/verification fields stay sourced from
# the (post-dedup, verified) representative.
# ---------------------------------------------------------------------------


def _edit_finding(
    *,
    file_name: str,
    issue: str = "Stale code reference",
    section: str = "2.1",
    severity: str = "HIGH",
    confidence: float = 0.9,
    action: str = "EDIT",
    existing: str | None = "2019 CBC",
    replacement: str | None = "2025 CBC",
    code_ref: str | None = "CBC 2025",
    anchor: str | None = None,
    insert_pos: str | None = None,
    evidence_id: str | None = None,
) -> Finding:
    """A finding built from legacy fields (no pre-set ``edit_proposal``), the
    shape ``_deduplicate_findings`` actually merges in production."""
    return Finding(
        severity=severity,
        fileName=file_name,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_ref,
        confidence=confidence,
        anchorText=anchor,
        insertPosition=insert_pos,
        evidenceElementId=evidence_id,
    )


class TestMultiFileFanOut:
    def test_merged_multifile_finding_emits_one_entry_per_file(self):
        # P0-1: identical defect in two specs collapses to one merged finding
        # for display, but the sidecar emits an instruction for EACH file.
        merged = _deduplicate_findings(
            [_edit_finding(file_name="a.docx"), _edit_finding(file_name="b.docx")]
        )
        assert len(merged) == 1  # collapsed for the report
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=merged))
        )

        assert payload["edit_count"] == 2
        assert {e["fileName"] for e in payload["edits"]} == {"a.docx", "b.docx"}
        for e in payload["edits"]:
            assert sorted(e["affected_files"]) == ["a.docx", "b.docx"]
            assert e["edit_proposal"]["existing_text"] == "2019 CBC"
            assert e["edit_proposal"]["replacement_text"] == "2025 CBC"
        # Entries from one finding share its content id; (finding_id, fileName)
        # is the unique per-entry key.
        ids = {e["finding_id"] for e in payload["edits"]}
        assert len(ids) == 1 and next(iter(ids))
        keys = [(e["finding_id"], e["fileName"]) for e in payload["edits"]]
        assert len(keys) == len(set(keys))

    def test_per_file_anchor_survives_merge_to_sidecar(self):
        # P0-2: anchorText is NOT in the dedup key, so files can carry
        # different anchors. The merge keeps only the representative's, but the
        # sidecar must emit each file's OWN anchor via executable_finding().
        merged = _deduplicate_findings(
            [
                _edit_finding(file_name="a.docx", anchor="after Part 1 General"),
                _edit_finding(file_name="b.docx", anchor="after Part 2 Products"),
            ]
        )
        assert len(merged) == 1
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=merged))
        )

        anchors = {
            e["fileName"]: e["edit_proposal"]["anchor_text"] for e in payload["edits"]
        }
        assert anchors == {
            "a.docx": "after Part 1 General",
            "b.docx": "after Part 2 Products",
        }
        assert all(e["has_per_file_original"] for e in payload["edits"])

    def test_verification_fields_come_from_representative_for_every_file(self):
        # Verification runs AFTER dedup, so only the merged representative
        # carries a verdict; per-file originals have none. Every per-file entry
        # must still report the representative's verdict/status, not NOT_CHECKED.
        merged = _deduplicate_findings(
            [_edit_finding(file_name="a.docx"), _edit_finding(file_name="b.docx")]
        )
        merged[0].verification = VerificationResult(
            verdict="CORRECTED", grounded=True, sources=["https://example.gov/cbc"]
        )
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=merged))
        )

        assert payload["edit_count"] == 2
        for e in payload["edits"]:
            assert e["verification_verdict"] == "CORRECTED"
            assert e["report_status"] == "VERIFIED_CONTRADICTED"

    def test_report_only_multifile_finding_emits_nothing(self):
        # A REPORT_ONLY finding that merged across files still produces zero
        # sidecar entries — consistent with the report, which renders the rep.
        merged = _deduplicate_findings(
            [
                _edit_finding(
                    file_name="a.docx",
                    action="REPORT_ONLY",
                    existing=None,
                    replacement=None,
                    code_ref=None,
                ),
                _edit_finding(
                    file_name="b.docx",
                    action="REPORT_ONLY",
                    existing=None,
                    replacement=None,
                    code_ref=None,
                ),
            ]
        )
        assert len(merged) == 1
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=merged))
        )
        assert payload["edit_count"] == 0
        assert payload["edits"] == []

    def test_legacy_multifile_without_originals_flags_fallback(self):
        # A finding with affected_files but no per-file originals (legacy /
        # resume payload): every file still gets an entry, but non-
        # representative files are flagged has_per_file_original=False so a
        # downstream applier knows the locator is borrowed from the rep.
        f = _edit_finding(file_name="a.docx", anchor="after Part 1")
        f.affected_files = ["a.docx", "b.docx"]
        f.finding_id = "rf-deadbeef0000"
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=[f]))
        )

        assert payload["edit_count"] == 2
        by_file = {e["fileName"]: e for e in payload["edits"]}
        assert by_file["a.docx"]["has_per_file_original"] is True
        assert by_file["b.docx"]["has_per_file_original"] is False
        # b borrows the representative's anchor (no per-file original to use).
        assert by_file["b.docx"]["edit_proposal"]["anchor_text"] == "after Part 1"
        assert by_file["a.docx"]["finding_id"] == by_file["b.docx"]["finding_id"]
