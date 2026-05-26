"""Batch run orchestration plus shared run-lifecycle helpers.

This module owns:

- input validation
- the run-epoch staleness guard
- the completion / error handlers
- ``reset_ui`` which clears in-flight state after a run

Batch submission/polling lives in ``batch_controller`` and uses these
shared helpers via the SpecReviewApp delegators.
"""
from __future__ import annotations

import os
import threading
from tkinter import messagebox

from ..batch.batch_state_store import delete_batch_state
from ..core.code_cycles import DEFAULT_CYCLE
from ..orchestration.diagnostics import DiagnosticsReport
from ..review.reviewer import MODEL_OPUS_47
from ..core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS
from ..tracing.session import (
    start_run_recorder,
    stop_run_recorder as _stop_recorder,
)


def _maybe_start_recorder(*, run_id: str, mode: str, model: str, cycle_label: str, files: list):
    """Thin wrapper over ``tracing.session.start_run_recorder`` (kept for
    the existing call sites / signature)."""
    return start_run_recorder(
        run_id=run_id, mode=mode, model=model, cycle_label=cycle_label, files=files
    )


def validate_inputs(app) -> bool:
    if not app.api_key_entry.get().strip():
        app.log.log_error("API key is required")
        return False
    if not app._selected_files:
        app.log.log_error("Select .docx specification files")
        return False
    missing = [f for f in app._selected_files if not f.exists()]
    if missing:
        app.log.log_error(f"File not found: {missing[0].name}")
        return False
    if app.file_list_panel.get_selected_count() == 0:
        app.log.log_error("No files selected")
        return False
    ctx = app._get_project_context()
    if ctx:
        from tiktoken import get_encoding
        ctx_tokens = len(get_encoding("cl100k_base").encode(ctx))
        app._project_context_tokens = ctx_tokens
        app._update_context_token_label()
        if ctx_tokens > PROJECT_CONTEXT_MAX_TOKENS:
            app.log.log_error(
                f"Project Context is {ctx_tokens:,} tokens — limit is "
                f"{PROJECT_CONTEXT_MAX_TOKENS:,}. Trim it before running."
            )
            messagebox.showerror(
                "Project Context too large",
                f"Project Context is {ctx_tokens:,} tokens, exceeding the "
                f"{PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
                f"Trim the context (or remove some attachments) before running.",
            )
            return False
    return True


def next_run_epoch(app) -> int:
    app._run_epoch += 1
    return app._run_epoch


def dispatch_if_current(app, epoch: int, fn) -> None:
    app.after(0, lambda: fn() if app._run_epoch == epoch else None)


def start_review(app) -> None:
    if app.is_processing:
        return
    if not validate_inputs(app):
        return

    selected_files = app.file_list_panel.get_selected_files()
    num_specs = len(selected_files)

    app._selected_files_for_review = selected_files
    app._project_context_for_review = app._get_project_context()
    app._cross_check_for_review = app._cross_check_var.get()
    app._selected_cycle_label = DEFAULT_CYCLE.label
    app.is_processing = True
    app.log.log("─" * 40, level="muted", timestamp=False, paced=False)
    app.run_button.set_processing()
    app.progress_bar.pack(fill="x", pady=(8, 0), after=app.run_button)
    app.progress_bar.set(0)
    app.progress_bar.configure(mode="determinate")
    os.environ["ANTHROPIC_API_KEY"] = app.api_key_entry.get().strip()

    app._diagnostics_report = DiagnosticsReport(
        mode="batch",
        model=MODEL_OPUS_47,
        cycle_label=app._selected_cycle_label,
        files_selected=[p.name for p in selected_files],
        project_context_tokens=app._project_context_tokens,
        cross_check_enabled=app._cross_check_for_review,
    )
    app._diagnostics_report.log(
        "init", "info",
        f"Run started: batch mode, {num_specs} files, cycle {app._selected_cycle_label}",
    )
    app.diagnostics_button.configure(state="disabled")

    app.log.log_step(f"Submitting {num_specs} files for batch review (Opus 4.7)...")
    run_epoch = app._next_run_epoch()
    threading.Thread(target=app._submit_batch_thread, args=(run_epoch,), daemon=True).start()


def on_review_complete(app, result) -> None:
    app.progress_bar.set(1.0)
    app._last_result = result
    if result.review_result:
        rv = result.review_result
        has_review_errors = bool(rv.error)
        if has_review_errors:
            app.log.log_warning("Review completed with errors — some specs failed. See report for details.")
            app.log.log_warning(rv.error)
        else:
            app.log.log_success("Review complete!")
        app.log.log(
            f"Findings: {rv.critical_count} critical, {rv.high_count} high, "
            f"{rv.medium_count} medium, {rv.gripe_count} gripes",
            level="info",
        )

        if result.cross_check_result and result.cross_check_result.findings:
            cc = result.cross_check_result
            app.log.log(f"Cross-check: {len(cc.findings)} coordination issues found", level="info")
        total_elapsed = (
            result.total_elapsed_seconds
            if getattr(result, "total_elapsed_seconds", None) is not None
            else rv.elapsed_seconds
        )
        app.log.log(f"Time: {total_elapsed:.1f}s", level="muted")
        export_status = app._export_report_to_file(result)
        if export_status == "canceled":
            app.log.log_warning("Export canceled; results are still available in memory.")
            app._finalize_diagnostics("finalization", "info", "Run completed after export canceled")
        elif export_status == "error":
            app.log.log_warning("Export failed.")
            app._finalize_diagnostics("finalization", "warning", "Run completed with export failure")
        elif export_status == "success":
            app._show_edit_selection_dialog(result)
    delete_batch_state()
    if not result.review_result:
        app._finalize_diagnostics("finalization", "success", "Run completed successfully")
    app.run_button.set_complete()
    app.after(2500, app._reset_ui)


def on_review_error(app, err) -> None:
    app.progress_bar.pack_forget()
    app.log.log_error(f"Review failed: {err}")
    app._finalize_diagnostics("error", "error", f"Run failed: {err}")
    app.run_button.set_ready()
    app.is_processing = False


def reset_ui(app) -> None:
    app.run_button.set_ready()
    app.run_button.configure(text="Submit Batch")
    app.progress_bar.pack_forget()
    app.is_processing = False
    app._batch_submission = None
    # Stop a reattached trace recorder if a resume bailed out early (e.g.
    # invalid resume state) before any terminal path could stop it.
    # Idempotent — the normal completion paths stop it in their own
    # finally blocks, so a double-stop here is a no-op.
    _stop_recorder(getattr(app, "_trace_recorder", None))
    app._trace_recorder = None
