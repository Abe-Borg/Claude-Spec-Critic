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

# v4 (WS-4, D-14): compliance findings (``lc-`` ids, from
# ``PipelineResult.compliance_result``) join the finding sweep, and the
# top level gains two optional keys — ``project`` (the run's
# city/state/country/client identity dict, ``None`` on profile-less runs)
# and ``requirements_coverage`` (the compliance pass's per-requirement
# coverage matrix, ``[]`` when the pass didn't run) — so a downstream
# applier can see what drove location-specific edits.
# v3 fans out multi-file findings: one entry per affected file (was: a single
# entry carrying only the representative file). Entries gain ``affected_files``
# and ``has_per_file_original``, and their ``fileName`` / ``evidenceElementId``
# / ``edit_proposal`` are now the per-file values. v2 dropped the per-entry
# ``suppression_reason`` key along with the cross-check dependency-suppression
# feature that produced it.
SIDECAR_SCHEMA_VERSION = 4


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
    if hasattr(pipeline_result, "module_results") and hasattr(pipeline_result, "program_id"):
        entries: list[dict] = []
        coverage_by_module: dict[str, list[dict]] = {}
        for module_id, child in pipeline_result.module_results.items():
            child_payload = build_edit_instructions(child, report_path=report_path)
            for entry in child_payload["edits"]:
                entries.append({"module_id": module_id, **entry})
            coverage_by_module[module_id] = list(
                child_payload.get("requirements_coverage") or []
            )
        return {
            "schema_version": 5,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "report_file": report_path.name if report_path is not None else None,
            "program_id": pipeline_result.program_id,
            "project": getattr(pipeline_result, "project_profile", None),
            "assignments": [item.to_dict() for item in pipeline_result.assignments],
            "submission_coverage": {
                "submitted_files": list(pipeline_result.files_reviewed),
                "expected_files": list(pipeline_result.expected_files_reviewed),
                "submitted_requests": pipeline_result.routed_request_count,
                "expected_requests": pipeline_result.expected_routed_request_count,
            },
            "module_errors": dict(
                getattr(pipeline_result, "module_errors", None) or {}
            ),
            "requirements_coverage_by_module": coverage_by_module,
            "edit_count": len(entries),
            "edits": entries,
        }
    review = getattr(pipeline_result, "review_result", None)
    cross_check = getattr(pipeline_result, "cross_check_result", None)
    compliance = getattr(pipeline_result, "compliance_result", None)

    findings: list = []
    if review is not None:
        findings.extend(getattr(review, "findings", []) or [])
    if cross_check is not None:
        findings.extend(getattr(cross_check, "findings", []) or [])
    # v4: compliance findings (``lc-`` ids) join the sweep — they are plain
    # findings and fan out per affected file exactly like the others.
    if compliance is not None:
        findings.extend(getattr(compliance, "findings", []) or [])

    entries: list[dict] = []
    for group in group_findings(findings):
        entries.extend(_group_entries(group))

    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_file": report_path.name if report_path is not None else None,
        "cycle_label": getattr(pipeline_result, "cycle_label", None),
        # v4: per-run project identity + the compliance coverage matrix so a
        # downstream applier can see what drove location-specific edits.
        # ``None`` / ``[]`` on profile-less runs.
        "project": getattr(pipeline_result, "project_profile", None),
        "requirements_coverage": list(
            getattr(compliance, "coverage", None) or []
        ) if compliance is not None else [],
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


def build_requirements_profile_export(pipeline_result) -> dict | None:
    """Build the standalone requirements-profile payload (WS-4, D-14 [FT]).

    The field trial re-used the edition table and requirement items outside
    the report within hours (project memory, RFI drafting, hand-offs) — the
    profile is the artifact with the longest half-life and the report must
    not be its only container. Returns ``None`` when the run produced no
    requirements profile (every profile-less run) so no file is written.
    """
    if hasattr(pipeline_result, "module_results") and hasattr(pipeline_result, "program_id"):
        module_profiles = {}
        for module_id, child in pipeline_result.module_results.items():
            exported = build_requirements_profile_export(child)
            if exported is not None:
                module_profiles[module_id] = exported
        if not module_profiles:
            return None
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "program_id": pipeline_result.program_id,
            "project": getattr(pipeline_result, "project_profile", None),
            "module_profiles": module_profiles,
        }
    profile = getattr(pipeline_result, "requirements_profile", None)
    if not isinstance(profile, dict) or not profile:
        return None
    compliance = getattr(pipeline_result, "compliance_result", None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": getattr(pipeline_result, "project_profile", None),
        "module_id": getattr(pipeline_result, "module_id", None),
        "research_date": profile.get("research_date"),
        "requirements_profile": profile,
        "requirements_coverage": list(
            getattr(compliance, "coverage", None) or []
        ) if compliance is not None else [],
        "compliance_status": (
            getattr(compliance, "cross_check_status", None)
            if compliance is not None
            else None
        ),
    }


def write_requirements_profile_sidecar(
    pipeline_result, output_path: Path
) -> Path | None:
    """Write ``<report-stem>.profile.json`` beside the report, if applicable.

    Returns the path written, or ``None`` when the run has no requirements
    profile (profile-less runs write nothing — no empty artifacts).
    """
    data = build_requirements_profile_export(pipeline_result)
    if data is None:
        return None
    output_path = Path(output_path)
    profile_path = output_path.with_name(output_path.stem + ".profile.json")
    profile_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return profile_path
