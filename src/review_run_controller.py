"""Real-time review orchestration plus shared run-lifecycle helpers.

This module owns:

- input validation
- the run-epoch staleness guard
- the real-time cost-confirmation dialog
- the real-time worker thread (``run_review_thread``)
- the completion / error handlers (used by both real-time and batch)
- ``reset_ui`` which clears in-flight state after a run

Batch submission/polling lives in ``batch_controller`` and uses these
shared helpers via the SpecReviewApp delegators.
"""
from __future__ import annotations

import os
import threading
from tkinter import messagebox

import customtkinter as ctk

from .api_config import review_max_tokens as _review_max_tokens
from .batch_state_store import delete_batch_state
from .code_cycles import AVAILABLE_CYCLES, DEFAULT_CYCLE
from .diagnostics import DiagnosticsReport
from .pipeline import run_review
from .reviewer import MODEL_OPUS_47
from .tokenizer import PROJECT_CONTEXT_MAX_TOKENS
from .widgets import COLORS

_UI_FONT_SIZE = 12
_BATCH_TIMING_COPY = "Usually 45 min to 2 hrs, 24 hrs maximum (Extremely Rare)"
_MODE_BATCH = "Batch (SLOW: Cheap!)"


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


def confirm_realtime_cost(app, num_specs: int) -> bool:
    """Show a confirmation dialog warning about real-time mode costs.

    Returns True if the user confirms, False if they cancel. Blocks via
    ``wait_window`` until the dialog is closed.
    """
    app._realtime_confirmed = False

    dialog = ctk.CTkToplevel(app)
    dialog.title("Real-Time Mode — Cost Warning")
    dialog.geometry("520x340")
    dialog.configure(fg_color=COLORS["bg_dark"])
    dialog.resizable(False, False)
    dialog.transient(app)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_force()

    inner = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
    inner.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        inner, text="⚠  Real-Time Mode Cost Warning",
        font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
        text_color=COLORS["warning"],
    ).pack(anchor="w", padx=16, pady=(16, 8))

    warning_text = (
        f"You are about to run a real-time review of {num_specs} spec{'s' if num_specs != 1 else ''} "
        f"using Claude Opus 4.7.\n\n"
        f"Real-time mode uses full-price API calls for every stage: "
        f"per-spec review, verification (one call per finding), and "
        f"cross-spec coordination (if enabled). Depending on the number "
        f"of specs and findings, this can cost anywhere from dozens to "
        f"hundreds to thousands of dollars.\n\n"
    )

    if num_specs > 5:
        warning_text += (
            f"⚠  You have {num_specs} specs selected. For more than 5 specs, "
            f"batch mode is strongly recommended — identical prompts, models, "
            f"and review logic at 50% lower pricing. Findings should be equivalent."
        )
    else:
        warning_text += (
            f"Batch mode uses the same prompts, models, and review logic at 50% "
            f"lower pricing — findings should be equivalent. "
            f"({_BATCH_TIMING_COPY} instead of immediate in-session processing.)"
        )

    ctk.CTkLabel(
        inner, text=warning_text,
        font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
        text_color=COLORS["text_secondary"],
        wraplength=460, justify="left",
    ).pack(anchor="w", padx=16, pady=(0, 16))

    btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
    btn_frame.pack(fill="x", padx=16, pady=(0, 16))
    btn_kw = {
        "height": 36,
        "font": ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE, weight="bold"),
        "corner_radius": 6,
    }

    def _confirm():
        app._realtime_confirmed = True
        dialog.destroy()

    def _cancel():
        app._realtime_confirmed = False
        dialog.destroy()

    ctk.CTkButton(
        btn_frame, text="Switch to Batch Mode", width=180,
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=_cancel, **btn_kw,
    ).pack(side="left", padx=(0, 8))

    ctk.CTkButton(
        btn_frame, text="Proceed (Real-Time)", width=160,
        fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
        border_width=1, border_color=COLORS["warning"],
        text_color=COLORS["warning"], command=_confirm, **btn_kw,
    ).pack(side="left")

    dialog.wait_window()
    return app._realtime_confirmed


def start_review(app) -> None:
    if app.is_processing:
        return
    if not validate_inputs(app):
        return

    selected_files = app.file_list_panel.get_selected_files()
    num_specs = len(selected_files)

    if not app._is_batch_mode:
        confirmed = confirm_realtime_cost(app, num_specs)
        if not confirmed:
            app.mode_selector.set(_MODE_BATCH)
            app._on_mode_change(_MODE_BATCH)
            app.log.log("Switched to batch mode.", level="info")
            return

    app._selected_files_for_review = selected_files
    app._project_context_for_review = app._get_project_context()
    app._cross_check_for_review = app._cross_check_var.get()
    app._verbose_for_review = app._verbose_var.get()
    app._export_mode_for_review = app._is_export_mode
    app._selected_cycle_label = DEFAULT_CYCLE.label
    # Capture the segmented control's current value on the UI thread
    # before kicking off the background submission.
    app._review_mode_for_review = app._get_selected_review_mode()
    app.is_processing = True
    app._close_report_window()
    app.log.log("─" * 40, level="muted", timestamp=False, paced=False)
    app.run_button.set_processing()
    app.progress_bar.pack(fill="x", pady=(8, 0), after=app.run_button)
    app.progress_bar.set(0)
    app.progress_bar.configure(mode="determinate")
    os.environ["ANTHROPIC_API_KEY"] = app.api_key_entry.get().strip()

    mode = "batch" if app._is_batch_mode else "real-time"
    app._diagnostics_report = DiagnosticsReport(
        mode=mode,
        model=MODEL_OPUS_47,
        cycle_label=app._selected_cycle_label,
        files_selected=[p.name for p in selected_files],
        project_context_tokens=app._project_context_tokens,
        cross_check_enabled=app._cross_check_for_review,
        export_mode=app._export_mode_for_review,
    )
    app._diagnostics_report.log(
        "init", "info",
        f"Run started: {mode} mode, {num_specs} files, cycle {app._selected_cycle_label}",
    )
    app.diagnostics_button.configure(state="disabled")

    n = num_specs
    output_label = " → Export Report" if app._export_mode_for_review else ""
    if app._is_batch_mode:
        app.log.log_step(f"Submitting {n} files for batch review (Opus 4.7){output_label}...")
        run_epoch = app._next_run_epoch()
        threading.Thread(target=app._submit_batch_thread, args=(run_epoch,), daemon=True).start()
    else:
        app.log.log_step(f"Reviewing {n} files (Opus 4.7){output_label}...")
        run_epoch = app._next_run_epoch()
        threading.Thread(target=app._run_review_thread, args=(run_epoch,), daemon=True).start()


def run_review_thread(app, run_epoch: int) -> None:
    diag = app._diagnostics_report
    try:
        n = len(app._selected_files_for_review)
        app._dispatch_if_current(run_epoch, lambda: app.log.log_step("Starting per-spec review..."))
        cross_check_note = " + cross-check" if app._cross_check_for_review else ""
        mode_info = f"Model: Opus 4.7  •  {n} specs •  1 API call per spec  •  verification enabled{cross_check_note}"
        app._dispatch_if_current(run_epoch, lambda: app.log.log(mode_info, level="muted"))
        if diag:
            diag.log("review", "step", f"Starting real-time review of {n} specs")

        review_log = app._make_diag_log("review", run_epoch)
        review_progress = app._make_diag_progress("review", run_epoch)
        result = run_review(
            input_dir=app.input_dir,
            files=app._selected_files_for_review,
            project_context=app._project_context_for_review,
            model=MODEL_OPUS_47,
            verify=True,
            cross_check=app._cross_check_for_review,
            dry_run=False, verbose=False,
            cycle=AVAILABLE_CYCLES.get(app._selected_cycle_label, DEFAULT_CYCLE),
            mode=app._review_mode_for_review,
            log=review_log,
            progress=review_progress,
        )
        if diag and result.review_result:
            rv = result.review_result
            review_cap = _review_max_tokens(batch=False, model=rv.model)
            diag.log("review", "success", "Review completed", {
                "input_tokens": rv.input_tokens,
                "output_tokens": rv.output_tokens,
                "cache_creation_input_tokens": rv.cache_creation_input_tokens,
                "cache_read_input_tokens": rv.cache_read_input_tokens,
                "elapsed_seconds": round(rv.elapsed_seconds, 2),
                "stop_reason": rv.stop_reason,
                "parse_status": rv.parse_status,
                "max_output_tokens": review_cap,
                "severity_counts": {
                    "CRITICAL": rv.critical_count,
                    "HIGH": rv.high_count,
                    "MEDIUM": rv.medium_count,
                    "GRIPES": rv.gripe_count,
                },
                "total_findings": rv.total_count,
            })

            if rv.error:
                diag.log("review", "error", f"Review error: {rv.error}")
                diag.log("review", "warning", "One or more specs failed during review — check Reviewer's Notes for details.")

            if result.cross_check_result:
                cc = result.cross_check_result
                diag.log("cross_check", "info", f"Cross-check: {cc.cross_check_status}", {
                    "finding_count": len(cc.findings),
                    "input_tokens": cc.input_tokens,
                    "output_tokens": cc.output_tokens,
                    "cache_creation_input_tokens": cc.cache_creation_input_tokens,
                    "cache_read_input_tokens": cc.cache_read_input_tokens,
                })
            for f in rv.findings:
                if f.verification:
                    v = f.verification
                    event_data = {
                        "verdict": v.verdict,
                        "finding_severity": f.severity,
                        "confidence": f.confidence,
                        "explanation": v.explanation or "",
                        "grounded": v.grounded,
                        "model_used": v.model_used,
                        "escalated": v.escalated,
                        "cache_status": v.cache_status,
                        "web_search_requests": v.web_search_requests,
                        "successful_source_count": v.successful_source_count,
                        "search_error_count": v.search_error_count,
                    }
                    if v.sources:
                        event_data["sources"] = v.sources[:3]
                    if v.correction:
                        event_data["correction"] = v.correction
                    diag.log("verification", "info", f"Verified: {f.fileName} — {v.verdict}", event_data)
            unverified = [f for f in rv.findings if f.verification and f.verification.verdict == "UNVERIFIED"]
            if unverified:
                failure_reasons = list(set(
                    (f.verification.explanation or "No explanation provided")
                    for f in unverified
                ))
                diag.log(
                    "verification", "warning",
                    f"{len(unverified)}/{len(rv.findings)} findings UNVERIFIED",
                    {"failure_reasons": failure_reasons},
                )
            if result.leed_alerts:
                diag.log("preprocessing", "warning", f"LEED alerts: {len(result.leed_alerts)}")
            if result.placeholder_alerts:
                diag.log("preprocessing", "warning", f"Placeholder alerts: {len(result.placeholder_alerts)}")
        app._dispatch_if_current(run_epoch, lambda: app._on_review_complete(result))
    except Exception as e:
        import traceback
        err = f"{e}\n{traceback.format_exc()}"
        if diag:
            diag.log("review", "error", f"Review failed: {e}", {"traceback": traceback.format_exc()})
        app._dispatch_if_current(run_epoch, lambda: app._on_review_error(err))


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
        if getattr(app, "_export_mode_for_review", False):
            export_status = app._export_report_to_file(result)
            if export_status == "canceled":
                app.log.log_warning("Export canceled; results are still available in memory.")
                app._finalize_diagnostics("finalization", "info", "Run completed after export canceled")
            elif export_status == "error":
                app.log.log_warning("Export failed. Retry export or switch output mode to 'View in App' to open the report window.")
                app._finalize_diagnostics("finalization", "warning", "Run completed with export failure")
            elif export_status == "success":
                app._show_edit_selection_dialog(result)
        else:
            app._open_report_window(
                rv,
                result.files_reviewed,
                result.leed_alerts,
                result.placeholder_alerts,
                result.cross_check_result,
                verbose=getattr(app, "_verbose_for_review", True),
            )
    delete_batch_state()
    if not (getattr(app, "_export_mode_for_review", False) and result.review_result):
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
    if app._is_batch_mode:
        app.run_button.configure(text="Submit Batch")
    app.progress_bar.pack_forget()
    app.is_processing = False
    app._batch_submission = None
