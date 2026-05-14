"""Edit candidate selection and edit application orchestration.

Drives the user-facing edit workflow:

- Re-extracts specs if needed for paragraph maps
- Classifies findings into auto/manual/report-only buckets
- Opens the ``EditSelectionDialog`` for user choice
- Runs ``execute_edit_plan`` on a worker thread
- Routes per-report counts and skip reasons into the diagnostics report
- Hands the final summary off to ``EditSummaryDialog``

The actual dialog widgets stay in ``widgets.py``; this module just
orchestrates them.
"""
from __future__ import annotations

import threading
from pathlib import Path
from tkinter import filedialog

from .apply_edits import execute_edit_plan
from .edit_candidates import classify_edit_candidates
from .input.extractor import extract_text
from .review.reviewer import Finding
from .input.extractor import ExtractedSpec
from .spec_editor import EditReport
from .widgets import EditSelectionDialog, EditSummaryDialog


def show_edit_selection_dialog(app, result) -> None:
    extracted_specs = list(app._extracted_specs)
    source_paths = list(app._selected_files_for_review)

    if not extracted_specs and source_paths:
        app.log.log_step("Re-extracting specs for edit application...")
        extracted_specs = [extract_text(path) for path in source_paths if path.exists()]

    has_maps = any(spec.paragraph_map is not None for spec in extracted_specs)
    has_source_files = all(path.exists() for path in source_paths)
    if not has_maps and not has_source_files:
        app.log.log_warning(
            "Cannot apply edits: original spec files are not accessible and paragraph maps are unavailable."
        )
        return

    review_findings = list(result.review_result.findings) if result.review_result else []
    cross_check_findings = (
        list(result.cross_check_result.findings)
        if result.cross_check_result and result.cross_check_result.findings
        else []
    )

    candidates = classify_edit_candidates(
        review_findings,
        cross_check_findings=cross_check_findings,
    )
    eligible_count = sum(1 for c in candidates if c.eligible)
    ineligible_count = len(candidates) - eligible_count
    app.log.log(
        f"Edit candidates: {eligible_count} eligible, {ineligible_count} ineligible "
        f"(of {len(candidates)} total findings)",
        level="info",
    )
    if app._diagnostics_report:
        app._diagnostics_report.log(
            "edit_selection",
            "info",
            f"Edit candidates classified: {eligible_count} eligible, {ineligible_count} ineligible",
            {"eligible": eligible_count, "ineligible": ineligible_count, "total": len(candidates)},
        )
        for candidate in candidates:
            if candidate.eligible or not candidate.ineligible_reason:
                continue
            app._diagnostics_report.log(
                "edit_selection",
                "info",
                f"Ineligible finding {candidate.finding_index}: {candidate.ineligible_reason}",
                {"finding_index": candidate.finding_index, "reason": candidate.ineligible_reason},
            )

    if not any(candidate.eligible for candidate in candidates):
        reasons = {c.ineligible_reason for c in candidates if c.ineligible_reason}
        reason_str = "; ".join(sorted(reasons)) if reasons else "unknown"
        app.log.log(
            f"No findings eligible for auto-apply ({len(candidates)} total). Reasons: {reason_str}",
            level="muted",
        )
        app._finalize_diagnostics("finalization", "success", "Run completed without eligible auto-edits")
        return

    def on_apply(selected_indices: list[int]):
        apply_selected_edits(
            app,
            selected_indices,
            review_findings,
            cross_check_findings,
            extracted_specs,
            source_paths,
        )

    def on_dismiss():
        app._finalize_diagnostics(
            "finalization", "info",
            "Run completed after edit selection dismissed",
        )

    EditSelectionDialog(
        app, candidates=candidates,
        on_apply=on_apply, on_dismiss=on_dismiss,
    )


def apply_selected_edits(
    app,
    selected_indices: list[int],
    all_findings: list[Finding],
    cross_check_findings: list[Finding],
    extracted_specs: list[ExtractedSpec],
    source_paths: list[Path],
) -> None:
    output_dir = filedialog.askdirectory(
        title="Select output directory for edited specs",
        initialdir=str(source_paths[0].parent) if source_paths else None,
    )
    if not output_dir:
        app.log.log("Edit application canceled.", level="muted")
        app._finalize_diagnostics("finalization", "info", "Run completed after user declined edit application")
        return

    output_path = Path(output_dir)

    run_epoch = app._next_run_epoch()

    if app._diagnostics_report:
        app._diagnostics_report.log(
            "edit_application", "step", f"Applying {len(selected_indices)} edits to specs"
        )

    def _do_apply():
        try:
            reports = execute_edit_plan(
                selected_finding_indices=selected_indices,
                all_findings=all_findings,
                cross_check_findings=cross_check_findings,
                extracted_specs=extracted_specs,
                source_paths=source_paths,
                output_dir=output_path,
                log=lambda msg: app._dispatch_if_current(
                    run_epoch, lambda m=msg: app.log.log(m, level="info")
                ),
                # Chunk K5: forwarding the diagnostics report lets
                # execute_edit_plan tally locator methods so the summary
                # shows how often the id path was used.
                diagnostics=app._diagnostics_report,
            )
            app._dispatch_if_current(
                run_epoch, lambda r=reports: on_edits_applied(app, r)
            )
        except Exception as e:
            import traceback

            err = f"{e}\n{traceback.format_exc()}"
            app._dispatch_if_current(
                run_epoch,
                lambda: app.log.log_error(f"Edit application failed: {err}"),
            )
            app._dispatch_if_current(
                run_epoch,
                lambda: app._finalize_diagnostics("finalization", "warning", "Run completed with edit application failure"),
            )

    threading.Thread(target=_do_apply, daemon=True).start()


def on_edits_applied(app, reports: list[EditReport]) -> None:
    total_applied = sum(report.edits_applied for report in reports)
    total_skipped = sum(report.edits_skipped for report in reports)
    total_failed = sum(report.edits_failed for report in reports)
    app.log.log_success(
        f"Edits complete: {total_applied} applied, {total_skipped} skipped, {total_failed} failed"
    )
    EditSummaryDialog(app, edit_reports=reports)
    if app._diagnostics_report:
        for report in reports:
            app._diagnostics_report.record_edit_report(
                applied=report.edits_applied,
                skipped=report.edits_skipped,
                failed=report.edits_failed,
            )
            for outcome in getattr(report, "outcomes", []) or []:
                if outcome.status in ("skipped", "failed"):
                    reason = (outcome.detail or outcome.status).strip().lower()
                    # Chunk 9 — unsafe-markup refusals get their own bucket
                    # so the diagnostics summary can show how many edits
                    # were refused because of Word structure (hyperlinks,
                    # field codes, drawings, comments, tracked changes,
                    # etc.). Checked first so it wins over the generic
                    # "manual review" suffix in the same detail string.
                    if getattr(outcome, "refused_unsafe_markup", False):
                        bucket = "unsafe_markup"
                    elif "ambiguous" in reason:
                        bucket = "ambiguous"
                    elif "not found" in reason or "not_found" in reason:
                        bucket = "not_found"
                    elif "manual" in reason:
                        bucket = "manual_review"
                    elif outcome.status == "failed":
                        bucket = "failed"
                    else:
                        bucket = "skipped_other"
                    app._diagnostics_report.record_edit_skip(bucket)
            app._diagnostics_report.log(
                "edit_application",
                "info",
                (
                    f"{report.source_path.name}: {report.edits_applied} applied, "
                    f"{report.edits_skipped} skipped, {report.edits_failed} failed"
                ),
                {
                    "file": report.source_path.name,
                    "applied": report.edits_applied,
                    "skipped": report.edits_skipped,
                    "failed": report.edits_failed,
                },
            )
        app._diagnostics_report.log(
            "edit_application",
            "success" if all(report.edits_failed == 0 for report in reports) else "warning",
            "Edit application complete",
        )
    app._finalize_diagnostics("finalization", "success", "Run completed after edit application")
