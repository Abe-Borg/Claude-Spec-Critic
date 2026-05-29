"""Machine-readable edit-instruction sidecar.

Spec Critic emits edit instructions but no longer applies them. After the
Word report is written, this module writes a companion JSON file listing
every finding that carries a structured edit proposal so a separate tool can
ingest and apply them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .report_status import classify_status

# v2 dropped the per-entry ``suppression_reason`` key along with the
# cross-check dependency-suppression feature that produced it.
SIDECAR_SCHEMA_VERSION = 2


def _serialize_edit_proposal(proposal) -> dict | None:
    """Flatten an ``EditProposal`` into the sidecar's JSON shape."""
    if proposal is None:
        return None
    return {
        "action_type": proposal.action_type,
        "existing_text": proposal.existing_text,
        "replacement_text": proposal.replacement_text,
        "anchor_text": proposal.anchor_text,
        "insert_position": proposal.insert_position,
        "target_element_id": proposal.target_element_id,
        "edit_confidence": proposal.edit_confidence,
    }


def _verification_verdict(finding) -> str | None:
    vr = getattr(finding, "verification", None)
    if vr is None:
        return None
    return (getattr(vr, "verdict", "") or "") or None


def _finding_entry(finding) -> dict | None:
    """Build one sidecar entry, or None when the finding has no proposal."""
    proposal = (
        finding.as_edit_proposal()
        if hasattr(finding, "as_edit_proposal")
        else None
    )
    if proposal is None:
        return None
    return {
        "finding_id": getattr(finding, "finding_id", "") or "",
        "fileName": getattr(finding, "fileName", "") or "",
        "section": getattr(finding, "section", "") or "",
        "severity": getattr(finding, "severity", "") or "",
        "issue": getattr(finding, "issue", "") or "",
        "codeReference": getattr(finding, "codeReference", None),
        "evidenceElementId": getattr(finding, "evidenceElementId", None),
        "verification_verdict": _verification_verdict(finding),
        "report_status": classify_status(finding).value,
        "edit_proposal": _serialize_edit_proposal(proposal),
    }


def build_edit_instructions(pipeline_result, *, report_path: Path | None = None) -> dict:
    """Build the sidecar payload from a pipeline result."""
    review = getattr(pipeline_result, "review_result", None)
    cross_check = getattr(pipeline_result, "cross_check_result", None)

    findings: list = []
    if review is not None:
        findings.extend(getattr(review, "findings", []) or [])
    if cross_check is not None:
        findings.extend(getattr(cross_check, "findings", []) or [])

    entries: list[dict] = []
    for finding in findings:
        entry = _finding_entry(finding)
        if entry is not None:
            entries.append(entry)

    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_file": report_path.name if report_path is not None else None,
        "cycle_label": getattr(pipeline_result, "cycle_label", None),
        "edit_count": len(entries),
        "edits": entries,
    }


def write_edit_instructions_sidecar(pipeline_result, output_path: Path) -> Path:
    """Write the edit-instructions JSON next to the ``.docx`` report.

    The sidecar sits beside the report as ``<report-stem>.edits.json``.
    Returns the path written.
    """
    output_path = Path(output_path)
    sidecar_path = output_path.with_name(output_path.stem + ".edits.json")
    data = build_edit_instructions(pipeline_result, report_path=output_path)
    sidecar_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return sidecar_path
