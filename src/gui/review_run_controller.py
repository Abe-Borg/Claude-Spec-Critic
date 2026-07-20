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
from tkinter import messagebox, simpledialog

from ..modules import require_module
from ..programs import (
    RoutingState,
    SpecAssignment,
    SpecRoutingDecision,
    apply_user_override,
    assignments_for_specs,
    get_program,
    routed_module_ids,
)
from ..orchestration.diagnostics import DiagnosticsReport
from ..review.reviewer import REVIEW_MODEL_DEFAULT
from ..core.tokenizer import count_tokens, PROJECT_CONTEXT_MAX_TOKENS
from ..core.ui_state import save_project_profile
from .project_profile_inputs import completeness_error
from .realtime_cost_gate import should_warn_before_live_run
from ..tracing.session import (
    start_run_recorder,
    stop_run_recorder as _stop_recorder,
)


def _maybe_start_recorder(*, run_id: str, mode: str, model: str, cycle_label: str, files: list, module_id: str = "", project_profile: dict | None = None):
    """Thin wrapper over ``tracing.session.start_run_recorder`` (kept for
    the existing call sites / signature)."""
    return start_run_recorder(
        run_id=run_id, mode=mode, model=model, cycle_label=cycle_label, files=files,
        module_id=module_id, project_profile=project_profile,
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
        ctx_tokens = count_tokens(ctx)
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
    # When the selected module opts into a project profile, block the run
    # until it is complete — a location-aware review must not spend on review
    # with a half-entered jurisdiction. Profile-less modules gather None and
    # skip this check entirely.
    profile = app._gather_project_profile()
    if profile is not None:
        error = completeness_error(profile)
        if error:
            app.log.log_error(error)
            messagebox.showerror("Project details required", error)
            return False
    return True


def next_run_epoch(app) -> int:
    app._run_epoch += 1
    return app._run_epoch


def dispatch_if_current(app, epoch: int, fn) -> None:
    app.after(0, lambda: fn() if app._run_epoch == epoch else None)


def _revert_run_to_batch(app) -> None:
    """Cancel a pending live run and switch the app back to batch mode.

    Used by the run-start cost gate's "Use Batch instead" choice. Nothing has
    started yet (the gate sits before ``is_processing`` is set), so this only
    flips the transport + button and tells the user to re-run in batch.
    """
    apply = getattr(app, "_apply_transport_choice", None)
    if apply is not None:
        apply(False)
    app.log.log("Switched to batch mode — click Submit Batch to run.", level="info")


def _build_program_assignments(app, selected_files: list) -> tuple[SpecAssignment, ...] | None:
    """Route selected specs and obtain explicit confirmation where needed."""
    program = get_program(getattr(app, "_selected_program_id", None))
    selected_names = {path.name for path in selected_files}
    extracted = [
        spec
        for spec in (getattr(app, "_extracted_specs", None) or [])
        if spec.filename in selected_names
    ]
    if len(extracted) != len(selected_files):
        app.log.log_error(
            "Routing could not read every selected specification; reselect the "
            "files and wait for analysis to finish."
        )
        return None

    if len(program.implemented_module_ids) == 1:
        module_id = program.implemented_module_ids[0]
        by_name = {path.name: str(path) for path in selected_files}
        return tuple(
            SpecAssignment(
                source_path=by_name[spec.filename],
                decision=SpecRoutingDecision(
                    spec_id=spec.filename,
                    program_id=program.program_id,
                    automatic_state=RoutingState.SUPPORTED,
                    automatic_module_ids=(module_id,),
                    confidence=1.0,
                    evidence=(),
                ),
            )
            for spec in extracted
        )

    assignments = list(
        assignments_for_specs(extracted, selected_files, program=program)
    )
    for index, assignment in enumerate(assignments):
        if assignment.decision.state is not RoutingState.AMBIGUOUS:
            continue
        candidates = assignment.decision.candidate_module_ids
        candidate_names = [
            require_module(module_id).display_name for module_id in candidates
        ]
        evidence_lines = [
            f"• {item.source.value}: {item.detail}"
            for item in assignment.decision.evidence[:4]
        ]
        evidence_text = (
            "\n\nRouting evidence:\n" + "\n".join(evidence_lines)
            if evidence_lines
            else ""
        )
        if len(candidates) == 1:
            confirmed = messagebox.askyesno(
                "Confirm specification routing",
                f"{assignment.spec_id}\n\n"
                f"Suggested reviewer: {candidate_names[0]}\n"
                f"Router confidence: {assignment.decision.confidence:.0%}"
                f"{evidence_text}\n\n"
                "Choose No to cancel the run.",
            )
            if not confirmed:
                app.log.log(
                    "Review canceled during routing confirmation.", level="muted"
                )
                return None
            chosen = candidates
        else:
            option_lines = [
                f"{choice}. {name}"
                for choice, name in enumerate(candidate_names, start=1)
            ]
            all_choice = len(candidates) + 1
            option_lines.append(
                f"{all_choice}. All listed reviewers (intentional multi-route)"
            )
            choice = simpledialog.askinteger(
                "Resolve specification routing",
                f"{assignment.spec_id}\n\n"
                "The section metadata and text point to different disciplines. "
                "Choose the correct reviewer:\n\n"
                + "\n".join(option_lines)
                + f"\n\nRouter confidence: {assignment.decision.confidence:.0%}"
                + evidence_text,
                parent=app,
                minvalue=1,
                maxvalue=all_choice,
            )
            if choice is None:
                app.log.log(
                    "Review canceled during routing confirmation.", level="muted"
                )
                return None
            chosen = candidates if choice == all_choice else (candidates[choice - 1],)
        assignments[index] = SpecAssignment(
            source_path=assignment.source_path,
            decision=apply_user_override(
                assignment.decision,
                chosen,
                reason="Confirmed in the pre-review routing dialog.",
                program=program,
            ),
        )

    unsupported = [item.spec_id for item in assignments if not item.module_ids]
    if unsupported:
        proceed = messagebox.askyesno(
            "Unsupported specifications",
            "No implemented Architecture or Fire module can safely review:\n\n"
            + "\n".join(f"• {name}" for name in unsupported)
            + "\n\nContinue and record these files as skipped coverage gaps?",
        )
        if not proceed:
            app.log.log(
                "Review canceled because unsupported files were selected.",
                level="muted",
            )
            return None

    routed = [item for item in assignments if item.module_ids]
    if not routed:
        app.log.log_error(
            "None of the selected specifications has an implemented reviewer."
        )
        return None
    route_lines = []
    for item in assignments:
        labels = (
            ", ".join(require_module(mid).display_name for mid in item.module_ids)
            if item.module_ids
            else "Skipped — unsupported"
        )
        route_lines.append(f"• {item.spec_id}: {labels}")
    if not messagebox.askyesno(
        "Confirm hyperscale routing",
        "The program will use these per-spec reviewers:\n\n"
        + "\n".join(route_lines)
        + "\n\nProceed with the review?",
    ):
        app.log.log("Review canceled during routing confirmation.", level="muted")
        return None
    return tuple(assignments)


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
    # Snapshot the review transport for this run (the toggle only affects
    # runs started after it changes). Defensive getattr keeps hand-built
    # test doubles without the checkbox on the batch default.
    realtime_var = getattr(app, "_realtime_var", None)
    app._review_transport_for_review = (
        "realtime" if (realtime_var is not None and realtime_var.get()) else "batch"
    )
    transport = app._review_transport_for_review
    # Run-start cost gate. The Options toggle warns when the user switches
    # into real-time, but an operator whose real-time preference was already
    # persisted starts up with the box checked and never fires that toggle —
    # so warn here too, once per session, before any live spend. "Keep
    # Real-time" re-enters start_review (now past the gate via the session
    # flag); "Use Batch instead" flips the transport back to batch without
    # starting, so the user re-initiates as a batch run.
    if should_warn_before_live_run(app, transport):
        # Lazy import keeps this module's import surface at ``tkinter`` only
        # (the hermetic GUI tests gate on tkinter, not customtkinter).
        from .about_usage_dialogs import show_realtime_cost_warning

        show_realtime_cost_warning(
            app,
            on_keep=lambda: start_review(app),
            on_revert=lambda: _revert_run_to_batch(app),
        )
        return
    assignments = _build_program_assignments(app, selected_files)
    if assignments is None:
        return
    app._routing_assignments_for_review = assignments
    # The module is the single source: resolve the selected id (unknown /
    # unset degrades to the default California module) and derive the cycle
    # label from it so the two app attrs can never disagree.
    program = get_program(getattr(app, "_selected_program_id", None))
    app._selected_program_id_for_review = program.program_id
    active_module_ids = routed_module_ids(assignments, program=program)
    app._routed_module_ids_for_review = active_module_ids
    module = require_module(active_module_ids[0])
    app._selected_module_id = module.module_id
    app._selected_cycle_label = (
        module.cycle.label if len(active_module_ids) == 1 else "per-module"
    )
    # Snapshot the per-run project profile (None for a profile-less module).
    # Persist the entered values per module and echo the parsed location back
    # so a typo is visible before review spend begins (D-1).
    app._project_profile_for_review = app._gather_project_profile()
    if app._project_profile_for_review is not None:
        save_project_profile(
            program.program_id, app._project_profile_for_review.to_dict()
        )
        app.log.log(
            f"Project: {app._project_profile_for_review.display_line()}",
            level="info",
        )
    app.is_processing = True
    if hasattr(app, "module_selector"):
        app.module_selector.configure(state="disabled")
    app.log.log("─" * 40, level="muted", timestamp=False, paced=False)
    app.run_button.set_processing()
    app.progress_bar.pack(fill="x", pady=(8, 0), after=app.run_button)
    app.progress_bar.set(0)
    app.progress_bar.configure(mode="determinate")
    os.environ["ANTHROPIC_API_KEY"] = app.api_key_entry.get().strip()

    app._diagnostics_report = DiagnosticsReport(
        mode=transport,
        model=REVIEW_MODEL_DEFAULT,
        cycle_label=app._selected_cycle_label,
        module_id=(
            active_module_ids[0]
            if len(active_module_ids) == 1
            else ",".join(active_module_ids)
        ),
        program_id=program.program_id,
        module_ids=list(active_module_ids),
        cycle_labels={
            module_id: require_module(module_id).cycle.label
            for module_id in active_module_ids
        },
        project_profile_summary=(
            app._project_profile_for_review.display_line()
            if app._project_profile_for_review is not None
            else ""
        ),
        files_selected=[p.name for p in selected_files],
        project_context_tokens=app._project_context_tokens,
        cross_check_enabled=app._cross_check_for_review,
    )
    app._diagnostics_report.log(
        "init", "info",
        f"Run started: {transport} mode, {num_specs} files, cycle {app._selected_cycle_label}",
    )
    app.diagnostics_button.configure(state="disabled")

    if transport == "realtime":
        app.log.log_step(f"Starting real-time review of {num_specs} files (Opus 4.8)...")
    else:
        app.log.log_step(f"Submitting {num_specs} files for batch review (Opus 4.8)...")
    run_epoch = app._next_run_epoch()
    threading.Thread(target=app._submit_batch_thread, args=(run_epoch,), daemon=True).start()


def on_review_complete(app, result) -> None:
    app.progress_bar.set(1.0)
    app._last_result = result
    # Tracks whether any spec failed review so the terminal UI state (the
    # run button + the finalized diagnostics level) reflects a partial
    # failure instead of presenting the same green "success" as a fully-
    # clean run. ``rv.error`` is the spec-error summary set by
    # ``collect_review_batch_results`` whenever any spec truncated /
    # parse-errored / errored / returned nothing.
    has_review_errors = getattr(result, "status", "") == "partial"
    if result.review_result:
        rv = result.review_result
        has_review_errors = has_review_errors or bool(rv.error)
        if has_review_errors:
            app.log.log_warning(
                "Review completed with partial coverage or errors; see the report for details."
            )
            if rv.error:
                app.log.log_warning(rv.error)
            skipped = list(getattr(result, "skipped_files", None) or [])
            if skipped:
                app.log.log_warning(
                    "Unsupported specifications skipped: " + ", ".join(skipped)
                )
            missing = list(getattr(result, "missing_module_ids", None) or [])
            if missing:
                app.log.log_warning(
                    "No result available for routed module(s): " + ", ".join(missing)
                )
            module_errors = dict(getattr(result, "module_errors", None) or {})
            for module_id, message in module_errors.items():
                app.log.log_warning(
                    f"{module_id} collection failed; pending batch state was retained: "
                    f"{message}"
                )
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
            app._finalize_diagnostics(
                "finalization",
                "warning" if has_review_errors else "info",
                "Run completed with review errors after export canceled"
                if has_review_errors
                else "Run completed after export canceled",
            )
        elif export_status == "error":
            app.log.log_warning("Export failed.")
            app._finalize_diagnostics("finalization", "warning", "Run completed with export failure")
        elif export_status == "success":
            if has_review_errors:
                app._finalize_diagnostics(
                    "finalization",
                    "warning",
                    "Run completed with errors — one or more specs failed review",
                )
            else:
                app._finalize_diagnostics("finalization", "success", "Run completed successfully")
    if not result.review_result:
        app._finalize_diagnostics("finalization", "success", "Run completed successfully")
    # Partial failure gets a distinct amber terminal state; a clean run
    # keeps the celebratory green check-mark.
    if has_review_errors:
        app.run_button.set_complete_with_errors()
    else:
        app.run_button.set_complete()
    app.after(2500, app._reset_ui)


def on_review_error(app, err) -> None:
    app.progress_bar.pack_forget()
    app.log.log_error(f"Review failed: {err}")
    app._finalize_diagnostics("error", "error", f"Run failed: {err}")
    app.run_button.set_ready()
    if hasattr(app, "module_selector"):
        app.module_selector.configure(state="normal")
    app.is_processing = False


def reset_ui(app) -> None:
    app.run_button.set_ready()
    # Mode-aware idle label; hand-built test doubles without the helper
    # keep the legacy batch text.
    idle_text_fn = getattr(app, "_run_button_idle_text", None)
    app.run_button.configure(
        text=idle_text_fn() if callable(idle_text_fn) else "Submit Batch"
    )
    app.progress_bar.pack_forget()
    if hasattr(app, "module_selector"):
        app.module_selector.configure(state="normal")
    app.is_processing = False
    app._batch_submission = None
    # Defensive idempotent net. Every terminal worker path now stops the
    # recorder synchronously on its own thread — submit-failure and
    # poll-failure stop it inline, collect stops it in a finally — so by the
    # time this delayed reset runs the recorder is already stopped and the
    # global cleared (``_stop_recorder(None)`` is then a no-op). This stays
    # as a last-resort net in case a future path reaches reset_ui without
    # having torn the recorder down. (STRUCTURAL_AUDIT P2-4.)
    _stop_recorder(getattr(app, "_trace_recorder", None))
    app._trace_recorder = None
