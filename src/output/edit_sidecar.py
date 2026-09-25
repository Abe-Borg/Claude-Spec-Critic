"""Machine-readable edit-instruction sidecar.

Spec Critic emits edit instructions but no longer applies them. After the
Word report is written, this module writes a companion JSON file listing
every place a finding's edit applies, so a separate program (the
in-repository ``applier/``) can ingest and apply them.

**One entry per occurrence (schemas 6 and 7, plan WP-06B).** The report shows
a finding once — "this issue, found in these files" — and each place its edit
applies is an *occurrence*: one file, one element, one instruction
(:func:`occurrences.edit_occurrences`). The sidecar lists every occurrence of
every finding that proposes an edit. The same fix needed at p4 and at p8 of
one file is two entries; a duplicate emission of one edit at one place is
one; a defect deduplicated across N templated specs gives one entry per place
in each of them, each with its own file, element, and instruction. Schemas 4
and 5, which this writer emitted before, listed one entry per affected *file*,
so a file's second location of a fix never reached them. The applier still
reads them; nothing writes them any more, and a new sidecar is never written
in the old shape, which would drop locations.

**Entry identity.** Every entry carries an ``occurrence_id`` (``oc-`` and 12
hex characters, derived from the module, the finding id, the file, the
element, and the instruction, so it never depends on input order or on a
presentation counter) and the ``module_id`` it was minted under. The unique
key is ``(module_id, occurrence_id)``: no two entries of a sidecar share one,
and two modules' identical findings in a program never collide. The entries
of one finding share its ``finding_id``.

**Which finding supplies which field.** Display and verification fields
(``issue`` / ``severity`` / ``section`` / ``codeReference`` /
``verification_verdict`` / ``report_status``) come from the finding the report
shows, because verification runs *after* dedup on that finding alone.
Executable fields come from the occurrence's own pre-merge original, never
from another place: ``fileName``, ``evidenceElementId`` (the element the
occurrence targets, ``null`` when it has none), and ``edit_proposal``
(:meth:`occurrences.FindingOccurrence.executable_proposal`: this place's own
instruction, targeting its element). Where the occurrence has no location of
its own, nothing is borrowed: a file with no recorded original gets the
group's shared edit text with no element and no anchor (an EDIT or DELETE is
then found by its text; an ADD has no place, and the applier says so), and a
place whose own finding proposes no usable edit has ``edit_proposal: null``
and is still listed, so the applier accounts for it instead of reading the
place as clean.

**How the location was established.** ``location_basis`` is ``validated`` (the
element exists in the reviewed text of that file and contains the edit's
locator text), ``claimed`` (the review named the element and no reviewed text
was available to check it), ``unresolved`` (no usable element: none named, or
one that failed that check; the applier finds the text or refuses it as
ambiguous), or ``missing_original`` (no original was recorded for the file).
``location_note`` says why a location is uncertain, and is empty otherwise.

**Several findings, one occurrence.** Two content-identical findings with one
id (the same coordination finding returned twice) report the same occurrence.
It is listed once, and its ``report_status`` and ``verification_verdict`` are
those of the least trusted of them (``report_status.STATUS_TRUST_ORDER``), so
an applier gates the one instruction on the most cautious verification any of
them received.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..orchestration.occurrences import (
    FindingOccurrence,
    edit_occurrences,
    element_index_from_specs,
)
from .report_status import STATUS_TRUST_ORDER, classify_status

# Two record types, two schema constants. ``sidecar_schema_version_for``
# picks the right one from the result's shape; ``build_edit_instructions``
# uses them directly.
#
# - ``SIDECAR_SCHEMA_VERSION`` (6) applies to a **single-module**
#   ``PipelineResult``: a flat ``edits`` list, one entry per occurrence, keyed
#   by ``(module_id, occurrence_id)`` (see the module docstring), plus the
#   run's ``cycle_label`` / ``project`` / ``requirements_coverage`` /
#   ``requirements_coverage_completeness`` / ``provisional`` / ``collection``.
#   Each entry carries every schema 4 entry key except
#   ``has_per_file_original`` (the location basis supersedes it: only a
#   ``missing_original`` entry lacks a location of its own), plus
#   ``occurrence_id``, ``module_id``, ``location_basis``, and
#   ``location_note``.
# - ``PROGRAM_SIDECAR_SCHEMA_VERSION`` (7) applies to a **routed-program**
#   ``ProgramPipelineResult`` (one payload for every module the program ran):
#   the same entries, each naming its module, and the top level carries
#   ``program_id`` / ``assignments`` / ``submission_coverage`` /
#   ``module_errors`` / ``integrity_warnings`` /
#   ``requirements_coverage_by_module`` /
#   ``requirements_coverage_completeness_by_module`` / ``provisional`` /
#   ``collection_by_module`` / ``deferred_program_stages`` in place of the
#   single-module run keys.
#
# History. v6 / v7 (plan WP-06B, chunk S12): one entry per occurrence instead
# of one per affected file; the applier (``applier/sidecar.py``) learned to
# read them first, in S11. v4 / v5 (their predecessors, still read): one
# entry per affected file, keyed by ``(finding_id, fileName)``, with
# ``has_per_file_original`` false where a file's locator was borrowed from
# the finding the report shows. Additive within v4 / v5, and kept by v6 / v7:
# the compliance findings (``lc-`` ids) in the sweep and the top-level
# ``project`` / ``requirements_coverage`` (WS-4, D-14), the coverage rows'
# ``origin`` / ``assessment`` / ``reason`` / ``also_reported`` and
# ``requirements_coverage_completeness`` (plan WP-09), and ``provisional`` /
# ``collection`` (plan WP-14). v3 fanned multi-file findings out, one entry
# per affected file; v2 dropped the per-entry ``suppression_reason`` key.
#
# The two numbers are independent — bumping one never bumps the other.
SIDECAR_SCHEMA_VERSION = 6
PROGRAM_SIDECAR_SCHEMA_VERSION = 7


def _is_program_result(pipeline_result) -> bool:
    """Whether ``pipeline_result`` is a routed-program result (vs. one module)."""
    return hasattr(pipeline_result, "module_results") and hasattr(
        pipeline_result, "program_id"
    )


def sidecar_schema_version_for(pipeline_result) -> int:
    """The ``schema_version`` a sidecar built from ``pipeline_result`` carries.

    :data:`PROGRAM_SIDECAR_SCHEMA_VERSION` for a routed-program result,
    :data:`SIDECAR_SCHEMA_VERSION` for a single-module result.
    """
    if _is_program_result(pipeline_result):
        return PROGRAM_SIDECAR_SCHEMA_VERSION
    return SIDECAR_SCHEMA_VERSION


def _coverage_completeness(compliance) -> dict | None:
    """The compliance pass's completeness record as JSON, or ``None``.

    ``None`` when the pass never ran (every profile-less run) or the result
    carries no record; a consumer must not read ``None`` as complete.
    """
    record = getattr(compliance, "coverage_completeness", None)
    return record.to_dict() if record is not None else None


def _collection(pipeline_result) -> dict | None:
    """The run's collection outcome as a dict (plan WP-14), or ``None``."""
    outcome = getattr(pipeline_result, "collection_outcome", None)
    to_dict = getattr(outcome, "to_dict", None)
    return to_dict() if callable(to_dict) else None


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


_TRUST_RANK = {status: rank for rank, status in enumerate(STATUS_TRUST_ORDER)}


def _least_trusted_reporter(occurrence: FindingOccurrence):
    """The finding whose status and verdict an occurrence's entry carries.

    Usually the finding the report shows. When content-identical findings
    report the same occurrence (``also_reported_by``), the one with the least
    trusted status; on a tie the first, in the content order
    :func:`occurrences.edit_occurrences` lists them in.
    """
    reporters = (occurrence.finding, *occurrence.also_reported_by)
    return min(reporters, key=lambda finding: _TRUST_RANK[classify_status(finding)])


def _occurrence_entry(occurrence: FindingOccurrence) -> dict:
    """One sidecar entry: one file, one place, one instruction."""
    representative = occurrence.finding
    reporter = _least_trusted_reporter(occurrence)
    return {
        "finding_id": getattr(representative, "finding_id", "") or "",
        "occurrence_id": occurrence.occurrence_id,
        "module_id": occurrence.module_id,
        "fileName": occurrence.file_name or "",
        "affected_files": _affected_files(representative),
        "location_basis": occurrence.location,
        "location_note": occurrence.location_note,
        "section": getattr(representative, "section", "") or "",
        "severity": getattr(representative, "severity", "") or "",
        "issue": getattr(representative, "issue", "") or "",
        "codeReference": getattr(representative, "codeReference", None),
        "evidenceElementId": occurrence.element_id,
        "verification_verdict": _verification_verdict(reporter),
        "report_status": classify_status(reporter).value,
        "edit_proposal": _serialize_edit_proposal(occurrence.executable_proposal()),
    }


def result_findings(pipeline_result) -> list:
    """Every finding a single-module result reports: review, then
    cross-check, then compliance (``lc-`` ids, WS-4), in the report's order."""
    findings: list = []
    for phase in ("review_result", "cross_check_result", "compliance_result"):
        phase_result = getattr(pipeline_result, phase, None)
        if phase_result is not None:
            findings.extend(getattr(phase_result, "findings", None) or [])
    return findings


def result_occurrences(pipeline_result, *, module_id: str | None) -> list[FindingOccurrence]:
    """A single-module result's executable occurrences, as the sidecar lists them.

    Element ids are validated against the text the review read (the result's
    ``extracted_specs``); a result that carries none — a recovery whose
    source files moved — keeps them as ``claimed``. ``module_id`` qualifies
    every occurrence id, so the report and the sidecar name one place alike.
    """
    element_index = element_index_from_specs(
        getattr(pipeline_result, "extracted_specs", None)
    )
    return edit_occurrences(
        result_findings(pipeline_result),
        module_id=module_id,
        element_index=element_index,
    )


def _result_entries(pipeline_result, *, module_id: str | None) -> list[dict]:
    return [
        _occurrence_entry(occurrence)
        for occurrence in result_occurrences(pipeline_result, module_id=module_id)
    ]


def build_edit_instructions(pipeline_result, *, report_path: Path | None = None) -> dict:
    """Build the sidecar payload from a pipeline result.

    A routed-program result emits the ``PROGRAM_SIDECAR_SCHEMA_VERSION``
    shape; a single-module result emits the ``SIDECAR_SCHEMA_VERSION`` shape
    (see the constants' comment for the two layouts).
    """
    if _is_program_result(pipeline_result):
        entries: list[dict] = []
        coverage_by_module: dict[str, list[dict]] = {}
        completeness_by_module: dict[str, dict | None] = {}
        collection_by_module: dict[str, dict | None] = {}
        for module_id, child in pipeline_result.module_results.items():
            # The module key names every child entry, so a child result that
            # carries no module id of its own is still attributed.
            entries.extend(_result_entries(child, module_id=module_id))
            compliance = getattr(child, "compliance_result", None)
            coverage_by_module[module_id] = (
                list(getattr(compliance, "coverage", None) or [])
                if compliance is not None
                else []
            )
            completeness_by_module[module_id] = _coverage_completeness(compliance)
            collection_by_module[module_id] = _collection(child)
        return {
            "schema_version": PROGRAM_SIDECAR_SCHEMA_VERSION,
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
            # Additive: the coverage figures in ``submission_coverage`` were
            # normalized from inconsistent saved state when this is
            # non-empty (see ``ProgramPipelineResult.integrity_warnings``),
            # so an applier can treat them as unverified. Empty on a clean
            # result.
            "integrity_warnings": [
                str(w)
                for w in (getattr(pipeline_result, "integrity_warnings", None) or [])
            ],
            "requirements_coverage_by_module": coverage_by_module,
            # Plan WP-09 (additive): per-module coverage completeness, so an
            # applier can tell whether a module's coverage was fully assessed.
            "requirements_coverage_completeness_by_module": completeness_by_module,
            # Plan WP-14 (additive): ``provisional`` is true while a module's
            # review repair batch is outstanding — no finding was verified and
            # the dependent stages were deferred, so an applier should hold
            # every edit. Per-module collection outcomes and the program's own
            # deferred stages ride beside it.
            "provisional": bool(getattr(pipeline_result, "provisional", False)),
            "collection_by_module": collection_by_module,
            "deferred_program_stages": list(
                getattr(pipeline_result, "deferred_program_stages", None) or ()
            ),
            "edit_count": len(entries),
            "edits": entries,
        }
    compliance = getattr(pipeline_result, "compliance_result", None)
    entries = _result_entries(
        pipeline_result, module_id=getattr(pipeline_result, "module_id", None)
    )
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_file": report_path.name if report_path is not None else None,
        "cycle_label": getattr(pipeline_result, "cycle_label", None),
        # Per-run project identity + the compliance coverage matrix (WS-4),
        # so a downstream applier can see what drove location-specific edits.
        # ``None`` / ``[]`` on profile-less runs.
        "project": getattr(pipeline_result, "project_profile", None),
        "requirements_coverage": list(
            getattr(compliance, "coverage", None) or []
        ) if compliance is not None else [],
        # Plan WP-09 (additive): whether that coverage was fully assessed.
        "requirements_coverage_completeness": _coverage_completeness(compliance),
        # Plan WP-14 (additive): see the program branch above. ``collection``
        # is the run's ``CollectionOutcome.to_dict()``, ``None`` on a result
        # that recorded none.
        "provisional": bool(getattr(pipeline_result, "provisional", False)),
        "collection": _collection(pipeline_result),
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
    if _is_program_result(pipeline_result):
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
        # Plan WP-09: the profile export is the longest-lived artifact, so it
        # must say when the coverage above was only partly assessed.
        "requirements_coverage_completeness": _coverage_completeness(compliance),
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
