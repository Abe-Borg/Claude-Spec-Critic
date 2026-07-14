"""Batch submission, polling, collection, and finalization.

Owns every batch-mode-specific code path:

- ``submit_batch_thread`` — the worker that calls ``start_batch_review``
- ``on_batch_submitted`` / ``poll_batch`` / ``poll_and_collect_thread`` —
  bounded polling using ``DEFAULT_REVIEW_POLL_POLICY``
- ``collect_batch_results`` — orchestrates result collection,
  verification (with optional batch wave), cross-check, cross-check
  verification, and finalize.

Threading discipline (run_epoch staleness guard, ``_dispatch_if_current``
for UI updates) is preserved verbatim. The flow is forward-only — a batch
runs start-to-report in a single process. ``SpecReviewApp`` keeps thin
delegating methods so existing test/legacy call paths still work.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from tkinter import messagebox

from .. import __version__
from ..batch.batch import BatchStatus
from ..batch.batch_runtime import DEFAULT_REVIEW_POLL_POLICY, poll_batch_bounded
from ..core.project_profile import ProjectProfile
from ..modules import DEFAULT_MODULE, get_module
from ..orchestration.batch_resume import (
    PendingBatch,
    clear_pending_batch,
    load_pending_batch,
    save_pending_batch,
    thin_submission_from_batch_results,
)
from ..orchestration.diagnostics import DiagnosticsReport
from ..orchestration.pipeline import (
    BatchSubmission,
    collect_batch_verification_results,
    collect_review_batch_results,
    finalize_batch_result,
    run_cross_check_for_batch,
    start_batch_review,
    start_batch_verification,
    _make_verification_cache,
    _persist_verification_cache,
)
from ..review.reviewer import REVIEW_MODEL_DEFAULT
from .review_run_controller import _maybe_start_recorder, _stop_recorder

_BATCH_TIMING_COPY = "Usually 45 min to 2 hrs, 24 hrs maximum (Extremely Rare)"


def submit_batch_thread(app, run_epoch: int) -> None:
    diag = app._diagnostics_report
    # Start the trace recorder if tracing is enabled. The recorder lives
    # for the entire batch lifecycle (submit → poll → collect → verify →
    # finalize) and is stopped after collect_batch_results completes.
    # Store on the app so the collect path can reach it.
    module = get_module(getattr(app, "_selected_module_id", None))
    profile = getattr(app, "_project_profile_for_review", None)
    profile_dict = profile.to_dict() if profile is not None else None
    app._trace_recorder = _maybe_start_recorder(
        run_id=diag.run_id if diag is not None else "no_run_id",
        mode="batch",
        model=REVIEW_MODEL_DEFAULT,
        cycle_label=module.cycle.label,
        module_id=module.module_id,
        files=app._selected_files_for_review,
        project_profile=profile_dict,
    )
    try:
        if diag:
            diag.log("batch_submit", "step", "Preparing batch submission")

        # WS-3 UI nicety: when the engine will run the research phase (module
        # opted in + complete profile + dimensions defined), name it on the
        # run button. The authoritative gate lives in start_batch_review;
        # this only mirrors it for display, and profile-less runs never hit it.
        if (
            module.project_profile_enabled
            and profile is not None
            and profile.is_complete()
            and module.research_dimensions
        ):
            app._dispatch_if_current(
                run_epoch,
                lambda: app.run_button.configure(
                    text="Researching location requirements..."
                ),
            )

        submission = start_batch_review(
            input_dir=app.input_dir,
            files=app._selected_files_for_review,
            project_context=app._project_context_for_review,
            model=REVIEW_MODEL_DEFAULT,
            module=module,
            cross_check_enabled=app._cross_check_for_review,
            project_profile=profile,
            log=app._make_diag_log("batch_submit", run_epoch),
            progress=app._make_diag_progress("batch_submit", run_epoch),
            # WS-3: per-dimension research telemetry rolls into the run
            # diagnostics (phase="location_research"). The research phase
            # itself runs inside start_batch_review, gated on the module
            # flag + a complete profile — profile-less runs are untouched.
            diagnostics=diag,
        )
        if diag:
            diag.log("batch_submit", "success", f"Batch submitted: {submission.job.batch_id}", {
                "batch_id": submission.job.batch_id,
                "files_queued": len(submission.files_reviewed),
            })
        # Persist enough state to reconnect to this batch if the poller
        # detaches (closed app, lost network, no-progress / max-elapsed
        # timeout). The batch keeps running remotely; on next launch the user
        # is offered to resume it. Best-effort — never block the run.
        save_pending_batch(
            PendingBatch.from_submission(
                submission,
                input_dir=app.input_dir,
                files=app._selected_files_for_review,
                run_id=diag.run_id if diag is not None else "",
                app_version=__version__,
            )
        )
        app._dispatch_if_current(run_epoch, lambda: on_batch_submitted(app, submission))
    except Exception as e:
        import traceback
        err = f"{e}\n{traceback.format_exc()}"
        if diag:
            diag.log("batch_submit", "error", f"Batch submission failed: {e}", {"traceback": traceback.format_exc()})
        # Stop the recorder on submission failure so its files get flushed.
        _stop_recorder(getattr(app, "_trace_recorder", None))
        app._trace_recorder = None
        app._dispatch_if_current(run_epoch, lambda: app._on_review_error(err))


def on_batch_submitted(app, submission: BatchSubmission) -> None:
    app._batch_submission = submission
    app.progress_bar.set(0.4)
    app.log.log_success(f"Batch submitted: {submission.job.batch_id}")
    app.log.log(f"  {len(submission.files_reviewed)} specs queued • 50% cost savings", level="muted")
    app.log.log_step(f"Polling for results ({_BATCH_TIMING_COPY})...")
    app.run_button.configure(text="Polling...")
    app._poll_batch()


def poll_batch(app) -> None:
    if app._batch_submission is None:
        return
    run_epoch = app._next_run_epoch()
    threading.Thread(target=app._poll_and_collect_thread, args=(run_epoch,), daemon=True).start()


def update_poll_progress(app, status: BatchStatus) -> None:
    diag = app._diagnostics_report
    batch_pct = 0.40 + (status.progress_pct / 100.0) * 0.55
    app.progress_bar.set(min(batch_pct, 0.95))
    app.log.log(
        f"  Batch: {status.succeeded} done, {status.processing} processing, "
        f"{status.errored} errors • {status.progress_pct:.0f}%",
        level="info", paced=False,
    )
    if diag:
        diag.log("batch_poll", "info", f"Poll: {status.succeeded}/{status.total} done, {status.errored} errors", {
            "succeeded": status.succeeded,
            "processing": status.processing,
            "errored": status.errored,
            "canceled": status.canceled,
            "expired": status.expired,
            "total": status.total,
            "progress_pct": round(status.progress_pct, 1),
        })


def poll_and_collect_thread(app, run_epoch: int) -> None:
    if app._batch_submission is None:
        return
    outcome = poll_batch_bounded(
        app._batch_submission.job.batch_id,
        policy=DEFAULT_REVIEW_POLL_POLICY,
        log=app._make_diag_log("batch_poll", run_epoch),
        progress_cb=lambda status: app._dispatch_if_current(run_epoch, lambda s=status: app._update_poll_progress(s)),
    )
    if outcome.detached or outcome.poll_failed:
        reason = outcome.detach_reason or outcome.poll_error or "unknown"
        msg = (
            f"Batch polling stopped: {reason}. Batch ID {app._batch_submission.job.batch_id} "
            "may still be running remotely."
        )
        # Terminal failure for this run: there is no collect phase to hand
        # off to (and thus no ``_do_collect`` finally to reach), so stop the
        # trace recorder here on the worker thread — mirroring the
        # submit-failure path above. ``on_review_error`` resets
        # ``is_processing`` immediately without scheduling ``reset_ui``, so
        # without this the recorder would leak: its writer thread never gets
        # the shutdown sentinel (run trace left unflushed) and it stays the
        # installed module-global recorder while a fresh run is already
        # permitted to start. (STRUCTURAL_AUDIT P2-4.)
        _stop_recorder(getattr(app, "_trace_recorder", None))
        app._trace_recorder = None
        app._dispatch_if_current(run_epoch, lambda m=msg: app._on_review_error(m))
        return
    app._dispatch_if_current(run_epoch, lambda: app.log.log_success("Batch complete — collecting results..."))
    app._dispatch_if_current(run_epoch, app._collect_batch_results)


def collect_batch_results(app) -> None:
    run_epoch = app._next_run_epoch()
    diag = app._diagnostics_report

    def _do_collect():
        try:
            if app._batch_submission is None:
                raise RuntimeError("No active batch submission to collect.")
            module = get_module(getattr(app._batch_submission, "module_id", None))

            # NOTE: this collect → verify → cross-check → verify → finalize
            # sequence is mirrored, UI-free, by
            # ``pipeline.run_batch_collection_headless`` (used by the recovery
            # tool). Keep the two stage orders in lockstep; a future refactor
            # should collapse them onto one shared core (see PR discussion).
            if diag:
                diag.log("batch_collect", "step", "Collecting review batch results")
            review_state = collect_review_batch_results(
                app._batch_submission,
                log=app._make_diag_log("batch_collect", run_epoch),
            )
            rv = review_state.review_result
            if diag:
                # Route through ``record_api_call`` so the per-
                # phase rollup gets a consistent ``call_mode="batch"`` tag
                # for the review phase.
                diag.record_api_call(
                    phase="batch_collect",
                    model=rv.model,
                    level="success",
                    message="Review results collected",
                    input_tokens=rv.input_tokens,
                    output_tokens=rv.output_tokens,
                    cache_creation_input_tokens=rv.cache_creation_input_tokens,
                    cache_read_input_tokens=rv.cache_read_input_tokens,
                    stop_reason=rv.stop_reason,
                    mode="batch",
                    retry_status="initial",
                    structured_payload=rv.structured_payload,
                    extra={
                        "elapsed_seconds": round(rv.elapsed_seconds, 2),
                        "parse_status": rv.parse_status,
                        "severity_counts": {
                            "CRITICAL": rv.critical_count, "HIGH": rv.high_count,
                            "MEDIUM": rv.medium_count, "GRIPES": rv.gripe_count,
                        },
                        "total_findings": rv.total_count,
                    },
                )
                if rv.error:
                    diag.log("batch_collect", "error", f"Review errors: {rv.error}")

            verifiable_findings = list(rv.findings)
            cache = _make_verification_cache(log=app._make_diag_log("verification", run_epoch))
            if review_state.truncated_specs:
                if diag:
                    for spec_name in review_state.truncated_specs:
                        diag.record_failed_spec(spec_name)
                for spec_name in review_state.truncated_specs:
                    app._dispatch_if_current(
                        run_epoch,
                        lambda n=spec_name: app.log.log_warning(f"⚠ Review failed for {n} — see report for details"),
                    )
            if verifiable_findings:
                app._dispatch_if_current(run_epoch, lambda: app.run_button.configure(text="Verifying findings..."))
                if diag:
                    diag.log("verification", "step", f"Starting verification batch for {len(verifiable_findings)} findings")
                verification_job = start_batch_verification(
                    verifiable_findings,
                    module=module,
                    log=app._make_diag_log("verification", run_epoch),
                    progress=app._make_diag_progress("verification", run_epoch),
                    cache=cache,
                )
                if verification_job is None:
                    if diag:
                        diag.log("verification", "info", "Verification: all findings resolved locally; no batch submitted.")
                else:
                    if diag:
                        diag.log("verification", "info", f"Verification batch submitted: {verification_job.batch_id}", {
                            "batch_id": verification_job.batch_id,
                        })
                    collect_batch_verification_results(
                        verification_job,
                        verifiable_findings,
                        module=module,
                        log=app._make_diag_log("verification", run_epoch),
                        progress=app._make_diag_progress("verification", run_epoch),
                        cache=cache,
                    )
                if diag:
                    from ..orchestration.diagnostics import bound_structured_payload
                    verdicts = {}
                    for f in verifiable_findings:
                        if f.verification:
                            v = f.verification.verdict
                            verdicts[v] = verdicts.get(v, 0) + 1
                            event_data = {
                                "verdict": f.verification.verdict,
                                "finding_severity": f.severity,
                                "confidence": f.confidence,
                                "explanation": f.verification.explanation or "",
                                # Surface the routing decision
                                # so the diagnostics summary can report
                                # how many findings each mode handled.
                                "verification_mode": f.verification.verification_mode,
                                "verification_profile": f.verification.verification_profile,
                                "grounded": f.verification.grounded,
                                "cache_status": f.verification.cache_status,
                                "escalated": f.verification.escalated,
                                # Escalation telemetry —
                                # whether a second pass ran and whether
                                # it changed the verdict, so the summary
                                # can report "did escalation pay off?".
                                "escalation_attempted": f.verification.escalation_attempted,
                                "initial_model": f.verification.initial_model,
                                "initial_verdict": f.verification.initial_verdict,
                                "escalation_changed_verdict": f.verification.escalation_changed_verdict,
                                "escalation_reason": f.verification.escalation_reason,
                                # Tag remote verifications as
                                # batch API calls so the per-phase
                                # rollup's call_mode counters reflect
                                # the path that actually ran.
                                "api_call": f.verification.cache_status not in ("hit", "local_skip"),
                                "call_mode": "batch",
                                "model": f.verification.model_used,
                                "web_search_requests": f.verification.web_search_requests,
                                # Token usage so the per-phase diagnostics
                                # rollup reports real verification spend
                                # (previously absent, so verification showed
                                # in=0/out=0). Cache-hit / local-skip results
                                # carry 0 here (no API call ran), which is the
                                # correct contribution to this-run spend.
                                "input_tokens": f.verification.input_tokens,
                                "output_tokens": f.verification.output_tokens,
                                # Surface retry telemetry so the
                                # per-phase diagnostics rollup can answer
                                # "which findings burned retries / hit
                                # the continuation cap?".
                                "retry_telemetry": f.verification.retry_telemetry,
                            }
                            bounded_payload = bound_structured_payload(f.verification.structured_payload)
                            if bounded_payload is not None:
                                event_data["structured_payload"] = bounded_payload
                            diag.log("verification", "info",
                                f"Verified: {f.fileName} — {f.verification.verdict}", event_data)
                    diag.log("verification", "success", "Verification complete", {"verdicts": verdicts})

            if diag:
                diag.log("cross_check", "step", "Running cross-spec coordination check")
            app._dispatch_if_current(run_epoch, lambda: app.run_button.configure(text="Cross-check (live API)..."))
            app._dispatch_if_current(run_epoch, lambda: app.log.log_step("Running cross-spec coordination check (live API)..."))
            review_state = run_cross_check_for_batch(
                review_state,
                specs=getattr(app._batch_submission, "prepared_specs", None),
                project_context=getattr(app, "_project_context_for_review", ""),
                log=app._make_diag_log("cross_check", run_epoch),
            )
            if review_state.cross_check_skipped_due_to_missing_specs:
                app._dispatch_if_current(run_epoch, lambda: app.log.log_warning(
                    "Cross-check skipped due to missing extracted specs."
                ))
                if diag:
                    diag.log("cross_check", "warning", "Cross-check skipped: missing extracted specs")
            if diag and review_state.cross_check_result:
                cc = review_state.cross_check_result
                # The cross-check pass always runs as a live
                # (synchronous) call, so the call_mode reflects that
                # rather than the batch review phase.
                diag.record_api_call(
                    phase="cross_check",
                    model=cc.model,
                    message=f"Cross-check: {cc.cross_check_status}",
                    input_tokens=cc.input_tokens,
                    output_tokens=cc.output_tokens,
                    cache_creation_input_tokens=cc.cache_creation_input_tokens,
                    cache_read_input_tokens=cc.cache_read_input_tokens,
                    stop_reason=cc.stop_reason,
                    mode="realtime",
                    retry_status="initial",
                    structured_payload=cc.structured_payload,
                    extra={"finding_count": len(cc.findings)},
                )

            cross_check_findings = list(review_state.cross_check_result.findings) if review_state.cross_check_result and review_state.cross_check_result.findings else []
            if cross_check_findings:
                app._dispatch_if_current(run_epoch, lambda: app.run_button.configure(text="Verifying cross-check..."))
                if diag:
                    diag.log("cross_check_verification", "step", f"Verifying {len(cross_check_findings)} cross-check findings")
                cross_check_verification_job = start_batch_verification(
                    cross_check_findings,
                    module=module,
                    log=app._make_diag_log("cross_check_verification", run_epoch),
                    progress=app._make_diag_progress("cross_check_verification", run_epoch),
                    cache=cache,
                )
                if cross_check_verification_job is None:
                    if diag:
                        diag.log("cross_check_verification", "info", "Cross-check verification: all findings resolved locally; no batch submitted.")
                else:
                    collect_batch_verification_results(
                        cross_check_verification_job,
                        cross_check_findings,
                        module=module,
                        log=app._make_diag_log("cross_check_verification", run_epoch),
                        progress=app._make_diag_progress("cross_check_verification", run_epoch),
                        cache=cache,
                    )
                    if diag:
                        diag.log("cross_check_verification", "success", "Cross-check verification complete")

            _persist_verification_cache(cache, log=app._make_diag_log("finalization", run_epoch))
            if diag:
                diag.log("finalization", "step", "Finalizing batch results")
            final_result = finalize_batch_result(review_state)
            # The run finished start-to-report, so the saved pending-batch
            # state is no longer needed — drop it so the next launch doesn't
            # offer to resume an already-collected batch. Only the success path
            # clears it: a detach / collect error leaves it on disk so the user
            # can still resume.
            clear_pending_batch()
            app._dispatch_if_current(run_epoch, lambda r=final_result: app._on_review_complete(r))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            if diag:
                diag.log("batch_collect", "error", f"Batch collection failed: {e}", {"traceback": traceback.format_exc()})
            app._dispatch_if_current(run_epoch, lambda: app._on_review_error(err))
        finally:
            # Stop the trace recorder once batch collection is fully done
            # (or has errored out).
            _stop_recorder(getattr(app, "_trace_recorder", None))
            app._trace_recorder = None

    threading.Thread(target=_do_collect, daemon=True).start()


# ---------------------------------------------------------------------------
# Resume / reconnect to a detached batch
# ---------------------------------------------------------------------------


def offer_batch_resume(app) -> None:
    """Offer to resume an unfinished batch persisted by a prior session.

    Called shortly after startup. If a pending-batch state file exists, prompt
    the user to resume polling for its results (the batch kept running remotely)
    or discard it. A no-op when there is no pending batch or a run is already in
    flight.
    """
    if getattr(app, "is_processing", False):
        return
    pending = load_pending_batch()
    if pending is None:
        return
    n = len(pending.files_reviewed)
    spec_word = "spec" if n == 1 else "specs"
    when = ""
    if pending.submitted_at:
        from datetime import datetime
        when = datetime.fromtimestamp(pending.submitted_at).strftime("%b %d, %I:%M %p")
    detail = f"submitted {when}" if when else "from a previous session"
    resume = messagebox.askyesno(
        "Resume unfinished batch?",
        f"An unfinished batch review {detail} was found "
        f"({n} {spec_word}).\n\n"
        "The batch most likely finished on Anthropic's servers. Resume polling "
        "and finish the run (verification, cross-check, and report)?\n\n"
        "Choose No to discard it.",
    )
    if not resume:
        clear_pending_batch()
        app.log.log("Discarded the unfinished batch.", level="muted")
        return
    start_batch_resume(app, pending)


def start_batch_resume(app, pending: PendingBatch) -> None:
    """Resume an unfinished batch persisted by a prior session."""
    _begin_reconnect_run(
        app,
        reconstruct_fn=lambda log, progress: pending.to_submission(log=log, progress=progress),
        model=pending.model,
        cycle_label=pending.cycle_label,
        module_id=pending.module_id,
        project_context=pending.project_context,
        project_profile=pending.project_profile,
        cross_check_enabled=pending.cross_check_enabled,
        files_for_review=[Path(f) for f in pending.files],
        input_dir=pending.input_dir,
        run_id=pending.run_id,
        files_reviewed_label=list(pending.files_reviewed),
        batch_label=pending.batch_id,
        verb="Resuming",
    )


def recover_batch_dialog(app) -> None:
    """Prompt for a batch id and recover it (poll -> collect -> verify -> report).

    The manual counterpart to the startup resume prompt: recovers a batch the
    app never saved — one submitted before resume state existed, from another
    machine, or whose state file was lost. The findings come back regardless of
    local files; if spec files are currently checked they are reused so cross-
    spec coordination can run, otherwise it is a findings-only recovery.
    """
    if getattr(app, "is_processing", False):
        messagebox.showinfo(
            "Busy", "A run is already in progress — wait for it to finish, then recover."
        )
        return
    from tkinter import simpledialog

    batch_id = simpledialog.askstring(
        "Recover batch",
        "Enter the batch id to recover (it looks like msgbatch_…):",
        parent=app,
    )
    if not batch_id or not batch_id.strip():
        return
    batch_id = batch_id.strip()

    try:
        selected = app.file_list_panel.get_selected_files()
    except Exception:
        selected = []
    files = [Path(f) for f in (selected or [])]
    file_strs = [str(f) for f in files]
    input_dir = str(files[0].parent) if files else ""
    module = get_module(getattr(app, "_selected_module_id", None))
    cycle_label = module.cycle.label
    project_context = app._get_project_context() if hasattr(app, "_get_project_context") else ""
    # Re-gather the project profile from the live widgets (like project_context)
    # so a recovered location-aware run carries the same routing/report inputs.
    profile = app._gather_project_profile() if hasattr(app, "_gather_project_profile") else None
    profile_dict = profile.to_dict() if profile is not None else None
    cross_check_enabled = bool(files) and bool(
        getattr(app, "_cross_check_var", None) and app._cross_check_var.get()
    )
    if not files:
        app.log.log(
            "No spec files selected — recovering findings only (select the source "
            "files first to include cross-spec coordination).",
            level="muted",
        )

    def _reconstruct(log, progress):
        return thin_submission_from_batch_results(
            batch_id,
            model=REVIEW_MODEL_DEFAULT,
            input_dir=input_dir or None,
            files=file_strs or None,
            cross_check_enabled=cross_check_enabled,
            module=module,
            project_context=project_context,
            project_profile=profile_dict,
            log=log,
            progress=progress,
        )

    _begin_reconnect_run(
        app,
        reconstruct_fn=_reconstruct,
        model=REVIEW_MODEL_DEFAULT,
        cycle_label=cycle_label,
        module_id=module.module_id,
        project_context=project_context,
        project_profile=profile_dict,
        cross_check_enabled=cross_check_enabled,
        files_for_review=files,
        input_dir=input_dir,
        files_reviewed_label=[f.name for f in files],
        batch_label=batch_id,
        verb="Recovering",
    )


def _begin_reconnect_run(
    app,
    *,
    reconstruct_fn,
    model: str,
    cycle_label: str,
    project_context: str,
    cross_check_enabled: bool,
    files_for_review: list,
    module_id: str = "",
    input_dir: str = "",
    run_id: str = "",
    project_profile: dict | None = None,
    files_reviewed_label: list | None = None,
    batch_label: str = "",
    verb: str = "Resuming",
) -> None:
    """Shared lifecycle for reconnecting to an already-submitted batch.

    Mirrors ``review_run_controller.start_review``'s setup (diagnostics report,
    UI processing state, API key in env) but, instead of submitting a new batch,
    runs ``reconstruct_fn(log, progress) -> BatchSubmission`` on a worker thread
    and re-enters the existing poll -> collect path via ``on_batch_submitted``.
    Used by both the startup resume prompt and the manual "Recover batch…"
    action.
    """
    if getattr(app, "is_processing", False):
        return
    key = app.api_key_entry.get().strip()
    if not key:
        messagebox.showerror(
            "API key required",
            "Enter your Anthropic API key, then try again.",
        )
        return
    os.environ["ANTHROPIC_API_KEY"] = key

    app._selected_cycle_label = cycle_label
    app._selected_module_id = module_id or DEFAULT_MODULE.module_id
    app._project_context_for_review = project_context
    # Restore the per-run profile snapshot so the reconnected run's report /
    # tracing / routing see the same location the original submission used.
    app._project_profile_for_review = ProjectProfile.from_dict(project_profile)
    app._cross_check_for_review = cross_check_enabled
    app._selected_files_for_review = list(files_for_review or [])
    if input_dir:
        app.input_dir = input_dir

    app.is_processing = True
    app.run_button.set_processing()
    app.run_button.configure(text=f"{verb}...")
    app.progress_bar.pack(fill="x", pady=(8, 0), after=app.run_button)
    app.progress_bar.set(0.4)
    app.progress_bar.configure(mode="determinate")

    diag_kwargs = dict(
        mode="batch",
        model=model,
        cycle_label=cycle_label,
        module_id=app._selected_module_id,
        project_profile_summary=(
            app._project_profile_for_review.display_line()
            if app._project_profile_for_review is not None
            else ""
        ),
        files_selected=list(files_reviewed_label or []),
        project_context_tokens=0,
        cross_check_enabled=cross_check_enabled,
    )
    # Reuse the original run id (when known) so the trace recorder appends to
    # the original run rather than starting a disconnected one; otherwise let
    # DiagnosticsReport mint a fresh id.
    if run_id:
        diag_kwargs["run_id"] = run_id
    app._diagnostics_report = DiagnosticsReport(**diag_kwargs)
    app._diagnostics_report.log("init", "info", f"{verb} batch {batch_label}")
    app.diagnostics_button.configure(state="disabled")
    app.log.log("─" * 40, level="muted", timestamp=False, paced=False)
    app.log.log_step(f"{verb} batch {batch_label}...")

    run_epoch = app._next_run_epoch()
    threading.Thread(
        target=lambda: _reconnect_worker(
            app, reconstruct_fn, model, cycle_label, app._selected_module_id,
            run_id, run_epoch, batch_label, project_profile
        ),
        daemon=True,
    ).start()


def _reconnect_worker(app, reconstruct_fn, model, cycle_label, module_id, run_id, run_epoch, batch_label, project_profile=None) -> None:
    diag = app._diagnostics_report
    app._trace_recorder = _maybe_start_recorder(
        run_id=diag.run_id if diag is not None else (run_id or "no_run_id"),
        mode="batch",
        model=model,
        cycle_label=cycle_label,
        module_id=module_id,
        project_profile=project_profile,
        files=app._selected_files_for_review,
    )
    try:
        submission = reconstruct_fn(
            app._make_diag_log("batch_resume", run_epoch),
            app._make_diag_progress("batch_resume", run_epoch),
        )
        # Re-enter the standard poll -> collect path: on_batch_submitted stores
        # the submission, flips the UI to "Polling...", and starts polling.
        app._dispatch_if_current(run_epoch, lambda: on_batch_submitted(app, submission))
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        msg = str(e)
        if getattr(e, "status_code", None) == 404 or "not_found" in msg.lower() or "not found" in msg.lower():
            friendly = (
                f"Batch '{batch_label}' was not found. Double-check the id (they "
                "look like msgbatch_…); it may also have expired (results are kept "
                "~29 days)."
            )
        else:
            friendly = f"Recovery failed: {e}"
        if diag:
            diag.log("batch_resume", "error", friendly, {"traceback": tb})
        # No collect phase will run, so stop the recorder here (mirrors the
        # submit-failure path) and surface the error.
        _stop_recorder(getattr(app, "_trace_recorder", None))
        app._trace_recorder = None
        app._dispatch_if_current(run_epoch, lambda m=friendly: app._on_review_error(m))
