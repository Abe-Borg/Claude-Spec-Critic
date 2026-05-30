"""Machine-readable edit-instruction sidecar.

Spec Critic emits edit instructions but no longer applies them. After the
Word report is written, this module writes a companion JSON file listing
every finding that carries a structured edit proposal so a separate tool can
ingest and apply them.

**Per-file fan-out.** When ``_deduplicate_findings`` collapses the same defect
across N spec files (common for templated DSA master specs), the merged
``Finding`` carries ``affected_files=[a, b, c]`` and the per-file pre-merge
members in ``Finding.occurrence_originals``. The sidecar emits **one entry per
affected file** — expanded through :func:`pipeline.group_findings` and
:meth:`pipeline.FindingOccurrence.executable_finding` — so a downstream applier
receives an actionable instruction for *every* file the defect touches, each
with that file's own locator. Without this, the applier would fix only the
representative file ``a`` and silently skip the identical defect in ``b`` / ``c``.

**Which finding supplies which field.** Display / verification fields
(``issue`` / ``severity`` / ``section`` / ``codeReference`` /
``verification_verdict`` / ``report_status``) come from the merged
*representative*, because verification runs *after* dedup and only the
representative carries a ``VerificationResult``. Edit / locator fields
(``fileName`` / ``evidenceElementId`` / ``edit_proposal``, which includes the
per-file ``anchor_text`` / ``insert_position`` / ``target_element_id``) come
from each file's own ``executable_finding()`` so a representative's anchor is
never fanned across files whose original anchor differed.

**Entry identity.** Entries fanned out from one merged finding share its
content-addressed ``finding_id`` and list the whole group in
``affected_files``; the natural unique key for a single entry is therefore
``(finding_id, fileName)``. ``has_per_file_original`` is ``True`` when the
entry's edit/locator fields are this file's own (a tracked per-file original or
a singleton) and ``False`` when they fall back to the representative's — a
signal for a downstream applier to confirm the locator before applying.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..orchestration.pipeline import group_findings
from .report_status import classify_status

# v3 fans out multi-file findings: one entry per affected file (was: a single
# entry carrying only the representative file). Entries gain ``affected_files``
# and ``has_per_file_original``, and their ``fileName`` / ``evidenceElementId``
# / ``edit_proposal`` are now the per-file values. v2 dropped the per-entry
# ``suppression_reason`` key along with the cross-check dependency-suppression
# feature that produced it.
SIDECAR_SCHEMA_VERSION = 3


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


def _affected_files(representative) -> list[str]:
    """The full set of files this finding touches, order-preserving.

    Falls back to ``[fileName]`` for a finding that never went through the
    cross-file merge (singletons, coordination findings), and to ``[]`` when
    there is no file at all (a cross-spec coordination finding with an empty
    ``fileName``).
    """
    files = list(dict.fromkeys(getattr(representative, "affected_files", None) or []))
    if files:
        return files
    name = getattr(representative, "fileName", "") or ""
    return [name] if name else []


def _occurrence_entry(group, occurrence, base_proposal) -> dict | None:
    """Build one per-file sidecar entry for an occurrence of a finding.

    Display / verification fields come from the group representative; edit and
    locator fields come from this file's ``executable_finding()``. The proposal
    falls back to ``base_proposal`` (the representative's) when a per-file
    original carries none — by dedup-key construction the edit *text* is
    identical across the group, so the fallback only borrows the
    representative's locator, which ``has_per_file_original=False`` flags.
    Returns ``None`` only when no usable proposal can be resolved.
    """
    representative = group.representative
    exec_finding = occurrence.executable_finding()
    proposal = (
        exec_finding.as_edit_proposal()
        if hasattr(exec_finding, "as_edit_proposal")
        else None
    ) or base_proposal
    if proposal is None:
        return None
    return {
        "finding_id": getattr(representative, "finding_id", "") or "",
        "fileName": occurrence.file_name or "",
        "affected_files": _affected_files(representative),
        "has_per_file_original": occurrence.has_original(),
        "section": getattr(representative, "section", "") or "",
        "severity": getattr(representative, "severity", "") or "",
        "issue": getattr(representative, "issue", "") or "",
        "codeReference": getattr(representative, "codeReference", None),
        "evidenceElementId": getattr(exec_finding, "evidenceElementId", None),
        "verification_verdict": _verification_verdict(representative),
        "report_status": classify_status(representative).value,
        "edit_proposal": _serialize_edit_proposal(proposal),
    }


def _group_entries(group) -> list[dict]:
    """Expand one finding group into per-affected-file sidecar entries.

    Gated on the *representative* carrying an edit proposal so a REPORT_ONLY
    finding produces no entries — identical to the report, which renders the
    representative. A multi-file finding yields one entry per affected file.
    """
    representative = group.representative
    base_proposal = (
        representative.as_edit_proposal()
        if hasattr(representative, "as_edit_proposal")
        else None
    )
    if base_proposal is None:
        return []
    entries: list[dict] = []
    for occurrence in group.occurrences:
        entry = _occurrence_entry(group, occurrence, base_proposal)
        if entry is not None:
            entries.append(entry)
    return entries


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
    for group in group_findings(findings):
        entries.extend(_group_entries(group))

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
