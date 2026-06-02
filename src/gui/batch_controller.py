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
from ..core.code_cycles import AVAILABLE_CYCLES, DEFAULT_CYCLE
from ..orchestration.batch_resume import (
    PendingBatch,
    clear_pending_batch,
    load_pending_batch,
    save_pending_batch,
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
from ..review.reviewer import MODEL_OPUS_47
from .review_run_controller import _maybe_start_recorder, _stop_recorder

_BATCH_TIMING_COPY = "Usually 45 min to 2 hrs, 24 hrs maximum (Extremely Rare)"


def submit_batch_thread(app, run_epoch: int) -> None:
    diag = app._diagnostics_report
    # Start the trace recorder if tracing is enabled. The recorder lives
    # for the entire batch lifecycle (submit → poll → collect → verify →
    # finalize) and is stopped after collect_batch_results completes.
    # Store on the app so the collect path can reach it.
    app._trace_recorder = _maybe_start_recorder(
        run_id=diag.run_id if diag is not None else "no_run_id",
        mode="batch",
        model=MODEL_OPUS_47,
        cycle_label=app._selected_cycle_label,
        files=app._selected_files_for_review,
    )
    try:
        if diag:
            diag.log("batch_submit", "step", "Preparing batch submission")

        submission = start_batch_review(
            input_dir=app.input_dir,
            files=app._selected_files_for_review,
            project_context=app._project_context_for_review,
            model=MODEL_OPUS_47,
            cycle=AVAILABLE_CYCLES.get(app._selected_cycle_label, DEFAULT_CYCLE),
            cross_check_enabled=app._cross_check_for_review,
            log=app._make_diag_log("batch_submit", run_epoch),
            progress=app._make_diag_progress("batch_submit", run_epoch),
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
            cycle = AVAILABLE_CYCLES.get(getattr(app._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)

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
                    cycle=cycle,
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
                        cycle=cycle,
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
                cycle=cycle,
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
                    cycle=cycle,
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
                        cycle=cycle,
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
    """Set up the run lifecycle for a resumed batch, then reconnect and poll.

    Mirrors ``review_run_controller.start_review``'s lifecycle setup
    (diagnostics report, UI processing state, API key in env) but, instead of
    submitting a new batch, reconstructs the :class:`BatchSubmission` for the
    already-submitted one on a worker thread and re-enters the existing
    poll -> collect path via ``on_batch_submitted``.
    """
    if getattr(app, "is_processing", False):
        return
    key = app.api_key_entry.get().strip()
    if not key:
        messagebox.showerror(
            "API key required",
            "Enter your Anthropic API key, then resume the batch.",
        )
        return
    os.environ["ANTHROPIC_API_KEY"] = key

    app._selected_cycle_label = pending.cycle_label
    app._project_context_for_review = pending.project_context
    app._cross_check_for_review = pending.cross_check_enabled
    app._selected_files_for_review = [Path(f) for f in pending.files]
    if pending.input_dir:
        app.input_dir = pending.input_dir

    app.is_processing = True
    app.run_button.set_processing()
    app.run_button.configure(text="Resuming...")
    app.progress_bar.pack(fill="x", pady=(8, 0), after=app.run_button)
    app.progress_bar.set(0.4)
    app.progress_bar.configure(mode="determinate")

    diag_kwargs = dict(
        mode="batch",
        model=pending.model,
        cycle_label=pending.cycle_label,
        files_selected=list(pending.files_reviewed),
        project_context_tokens=0,
        cross_check_enabled=pending.cross_check_enabled,
    )
    # Reuse the original run id (when known) so the trace recorder appends to
    # the original run rather than starting a disconnected one; otherwise let
    # DiagnosticsReport mint a fresh id.
    if pending.run_id:
        diag_kwargs["run_id"] = pending.run_id
    app._diagnostics_report = DiagnosticsReport(**diag_kwargs)
    app._diagnostics_report.log(
        "init", "info",
        f"Resuming batch {pending.batch_id} ({len(pending.files_reviewed)} files)",
    )
    app.diagnostics_button.configure(state="disabled")
    app.log.log("─" * 40, level="muted", timestamp=False, paced=False)
    app.log.log_step(f"Resuming batch {pending.batch_id}...")

    run_epoch = app._next_run_epoch()
    threading.Thread(
        target=lambda: _resume_reconstruct_thread(app, pending, run_epoch),
        daemon=True,
    ).start()


def _resume_reconstruct_thread(app, pending: PendingBatch, run_epoch: int) -> None:
    diag = app._diagnostics_report
    app._trace_recorder = _maybe_start_recorder(
        run_id=diag.run_id if diag is not None else (pending.run_id or "no_run_id"),
        mode="batch",
        model=pending.model,
        cycle_label=pending.cycle_label,
        files=app._selected_files_for_review,
    )
    try:
        submission = pending.to_submission(
            log=app._make_diag_log("batch_resume", run_epoch),
            progress=app._make_diag_progress("batch_resume", run_epoch),
        )
        # Re-enter the standard poll -> collect path: on_batch_submitted stores
        # the submission, flips the UI to "Polling...", and starts polling.
        app._dispatch_if_current(run_epoch, lambda: on_batch_submitted(app, submission))
    except Exception as e:
        import traceback
        err = f"{e}\n{traceback.format_exc()}"
        if diag:
            diag.log("batch_resume", "error", f"Resume failed: {e}", {"traceback": traceback.format_exc()})
        # No collect phase will run, so stop the recorder here (mirrors the
        # submit-failure path) and surface the error.
        _stop_recorder(getattr(app, "_trace_recorder", None))
        app._trace_recorder = None
        app._dispatch_if_current(run_epoch, lambda m=err: app._on_review_error(m))
