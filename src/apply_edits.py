"""Orchestration helpers for applying selected review edits to DOCX specs."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from typing import TYPE_CHECKING

from .edit_locator import locate_edits
from .extractor import ExtractedSpec, extract_text_from_docx
from .reviewer import Finding
from .spec_editor import (
    EditAction,
    EditOutcome,
    EditReport,
    annotate_spec_with_suggestions,
    apply_edits_to_spec,
    build_edit_actions,
)

if TYPE_CHECKING:
    from .diagnostics import DiagnosticsReport


def _ensure_paragraph_maps(specs: list[ExtractedSpec], source_paths: list[Path]) -> list[ExtractedSpec]:
    """Re-extract specs that are missing paragraph maps."""
    hydrated: list[ExtractedSpec] = []
    for spec, path in zip(specs, source_paths):
        if spec.paragraph_map is not None:
            hydrated.append(spec)
            continue

        if path.exists():
            hydrated.append(extract_text_from_docx(path))
            continue

        hydrated.append(spec)
    return hydrated


def _make_output_path(source_path: Path, output_dir: Path) -> Path:
    """Build a non-overwriting output path using _edited suffix (+ timestamp fallback)."""
    preferred = output_dir / f"{source_path.stem}_edited{source_path.suffix}"
    if not preferred.exists():
        return preferred

    timestamp = datetime.now().strftime("%H%M%S")
    return output_dir / f"{source_path.stem}_edited_{timestamp}{source_path.suffix}"


def _build_failure_report(
    *,
    source_path: Path,
    output_path: Path,
    actions: list[EditAction],
    warning: str,
) -> EditReport:
    failed_outcomes = [
        EditOutcome(
            action=action,
            status="failed",
            detail=warning,
            original_text=action.location.matched_text,
            new_text=None,
        )
        for action in actions
    ]
    return EditReport(
        source_path=source_path,
        output_path=output_path,
        total_edits_attempted=len(actions),
        edits_applied=0,
        edits_skipped=0,
        edits_failed=len(actions),
        outcomes=failed_outcomes,
        warnings=[warning],
    )


def execute_edit_plan(
    selected_finding_indices: list[int],
    all_findings: list[Finding],
    cross_check_findings: list[Finding],
    extracted_specs: list[ExtractedSpec],
    source_paths: list[Path],
    output_dir: Path,
    *,
    log: Callable[[str], None] = lambda _: None,
    mode: str = "edit",
    diagnostics: "DiagnosticsReport | None" = None,
) -> list[EditReport]:
    """Run locate -> action build -> apply workflow for selected findings.

    Phase 4.6 — ``mode`` selects how proposals are written:

    * ``"edit"`` (default): mutate paragraph text per the action plan
      (legacy behavior).
    * ``"annotate"``: write a copy of each spec with yellow-highlighted
      suggestion paragraphs inserted after each anchor; the original text
      is never changed. This is the safe option for table cells, header/
      footer text, and richly formatted paragraphs.
    """
    if mode not in {"edit", "annotate"}:
        raise ValueError(f"Unknown edit mode: {mode!r}")
    output_dir.mkdir(parents=True, exist_ok=True)

    mapped_specs = _ensure_paragraph_maps(extracted_specs, source_paths)

    source_by_name = {path.name: path for path in source_paths}
    spec_by_filename = {spec.filename: spec for spec in mapped_specs}
    filename_map: dict[str, tuple[ExtractedSpec, Path]] = {}
    for filename, spec in spec_by_filename.items():
        source_path = source_by_name.get(filename)
        if source_path is not None:
            filename_map[filename] = (spec, source_path)

    merged_findings = list(all_findings) + list(cross_check_findings)
    selected_pairs: list[tuple[int, Finding]] = []
    for idx in selected_finding_indices:
        if 0 <= idx < len(merged_findings):
            selected_pairs.append((idx, merged_findings[idx]))
        else:
            log(f"Skipping out-of-range selected finding index: {idx}")

    # Findings that were merged across multiple files during deduplication
    # carry every affected file in `affected_files`. The display layer keeps a
    # single representative row, but edit application must fan out to every
    # file or the user only edits one of N affected specs (audit Issue 3).
    findings_by_file: dict[str, list[tuple[int, Finding]]] = defaultdict(list)
    for original_index, finding in selected_pairs:
        target_files = list(dict.fromkeys(finding.affected_files)) or (
            [finding.fileName] if finding.fileName else []
        )
        if not target_files:
            log(f"Skipping selected finding #{original_index}: no associated file name.")
            continue
        for file_name in target_files:
            findings_by_file[file_name].append((original_index, finding))

    reports: list[EditReport] = []

    for filename, indexed_findings in findings_by_file.items():
        spec_and_path = filename_map.get(filename)
        if spec_and_path is None:
            log(f"Skipping '{filename}': no matching source file/spec pair was found.")
            continue

        spec, source_path = spec_and_path
        paragraph_map = spec.paragraph_map
        if not paragraph_map:
            log(f"Skipping '{filename}': paragraph map unavailable and re-extraction was not possible.")
            continue

        findings = [finding for _, finding in indexed_findings]
        locator_results = locate_edits(findings, paragraph_map)
        for pair, locator_result in zip(indexed_findings, locator_results):
            original_index, _ = pair
            if locator_result.status == "not_found":
                log(f"[{filename}] Finding #{original_index} not found in document text.")
            elif locator_result.status == "ambiguous":
                log(f"[{filename}] Finding #{original_index} matched multiple locations; skipped — review and apply manually.")
            if locator_result.warning:
                log(f"[{filename}] Finding #{original_index} warning: {locator_result.warning}")
            # Chunk K5: record locator-method telemetry and surface a
            # human-readable trace for id-based matches so a future
            # debugging session can grep the logs for "via id=" and tell
            # which findings carried evidence pointers.
            if locator_result.locations:
                best = max(
                    locator_result.locations,
                    key=lambda loc: loc.match_confidence,
                )
                if diagnostics is not None:
                    diagnostics.record_locator_method(best.match_method)
                if best.match_method == "id":
                    log(
                        f"[{filename}] Finding #{original_index} located via "
                        f"id={best.mapping.element_id!r} "
                        f"(body_index={best.mapping.body_index})."
                    )

        # Annotate mode is intentionally permissive about which actions it
        # accepts: even MANUAL_REVIEW / ambiguous candidates can produce a
        # useful suggestion paragraph, so we build actions with caution
        # allowed and skip only the empty case.
        if mode == "annotate":
            actions = build_edit_actions(locator_results, allow_caution=True)
        else:
            actions = build_edit_actions(locator_results)
        output_path = _make_output_path(source_path, output_dir)

        if not actions:
            warning = "No edits were located for selected findings; skipped writing output."
            log(f"[{filename}] {warning}")
            reports.append(
                EditReport(
                    source_path=source_path,
                    output_path=output_path,
                    total_edits_attempted=0,
                    edits_applied=0,
                    edits_skipped=len(indexed_findings),
                    edits_failed=0,
                    outcomes=[],
                    warnings=[warning],
                )
            )
            continue

        try:
            if mode == "annotate":
                report = annotate_spec_with_suggestions(source_path, output_path, actions)
            else:
                report = apply_edits_to_spec(source_path, output_path, actions)
            reports.append(report)
        except Exception as exc:
            warning = f"Failed to apply edits: {exc}"
            log(f"[{filename}] {warning}")
            reports.append(
                _build_failure_report(
                    source_path=source_path,
                    output_path=output_path,
                    actions=actions,
                    warning=warning,
                )
            )

    return reports
