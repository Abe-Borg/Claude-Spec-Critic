"""Batch submission, polling, collection, finalization, and resume.

Owns every batch-mode-specific code path:

- ``submit_batch_thread`` — the worker that calls ``start_batch_review``
- ``on_batch_submitted`` / ``poll_batch`` / ``poll_and_collect_thread`` —
  bounded polling using ``DEFAULT_REVIEW_POLL_POLICY``
- ``collect_batch_results`` — orchestrates result collection,
  verification (with optional batch wave), cross-check, cross-check
  verification, and finalize. Saves resume state after each phase.
- ``check_pending_batch`` — startup dialog for a saved pending batch
- ``resume_batch`` and the per-phase resume helpers

Threading discipline (run_epoch staleness guard, ``_dispatch_if_current``
for UI updates) is preserved verbatim. ``SpecReviewApp`` keeps thin
delegating methods so existing test/legacy call paths still work.
"""
from __future__ import annotations

import os
import threading
import time

import customtkinter as ctk

from .core.api_key_store import load_api_key_from_file
from .batch.batch import BatchJob, BatchStatus
from .batch.batch_runtime import DEFAULT_REVIEW_POLL_POLICY, poll_batch_bounded
from . import batch_state_store as _batch_state_store


def save_batch_state(state):
    """Late-bound proxy so tests can monkeypatch ``batch_state_store.save_batch_state``."""
    return _batch_state_store.save_batch_state(state)


def load_batch_state():
    return _batch_state_store.load_batch_state()


def delete_batch_state():
    return _batch_state_store.delete_batch_state()


def batch_state_nearing_expiry(created_at):
    return _batch_state_store.batch_state_nearing_expiry(created_at)
from .core.code_cycles import AVAILABLE_CYCLES, DEFAULT_CYCLE
from .orchestration.pipeline import (
    BatchSubmission,
    CollectedBatchState,
    collect_batch_verification_results,
    collect_review_batch_results,
    finalize_batch_result,
    run_cross_check_for_batch,
    start_batch_review,
    start_batch_verification,
    _make_verification_cache,
    _persist_verification_cache,
)
from .orchestration.resume_state import (
    PHASE_CROSS_CHECK,
    PHASE_CROSS_CHECK_VERIFICATION_POLL,
    PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
    PHASE_FINALIZE,
    PHASE_REVIEW_COLLECT,
    PHASE_REVIEW_POLL,
    PHASE_VERIFICATION_POLL,
    PHASE_VERIFICATION_WAVE_POLL,
    SUPPORTED_PHASES,
    build_resume_state,
)
from .review.reviewer import MODEL_OPUS_47
from .widgets import COLORS

_UI_FONT_SIZE = 12
_BATCH_TIMING_COPY = "Usually 45 min to 2 hrs, 24 hrs maximum (Extremely Rare)"


def submit_batch_thread(app, run_epoch: int) -> None:
    diag = app._diagnostics_report
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
        save_batch_state(build_resume_state(phase=PHASE_REVIEW_POLL, submission=submission))
        app._dispatch_if_current(run_epoch, lambda: on_batch_submitted(app, submission))
    except Exception as e:
        import traceback
        err = f"{e}\n{traceback.format_exc()}"
        if diag:
            diag.log("batch_submit", "error", f"Batch submission failed: {e}", {"traceback": traceback.format_exc()})
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
        save_batch_state(build_resume_state(phase=PHASE_REVIEW_POLL, submission=app._batch_submission))
        reason = outcome.detach_reason or outcome.poll_error or "unknown"
        msg = (
            f"Batch polling stopped: {reason}. Batch ID {app._batch_submission.job.batch_id} "
            "may still be running remotely. Resume later to continue."
        )
        app._dispatch_if_current(run_epoch, lambda m=msg: app._on_review_error(m))
        return
    if app._batch_submission is not None:
        save_batch_state(build_resume_state(phase=PHASE_REVIEW_COLLECT, submission=app._batch_submission))
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

            if diag:
                diag.log("batch_collect", "step", "Collecting review batch results")
            review_state = collect_review_batch_results(
                app._batch_submission,
                log=app._make_diag_log("batch_collect", run_epoch),
            )
            rv = review_state.review_result
            if diag:
                # Chunk J: route through ``record_api_call`` so the per-
                # phase rollup gets a consistent ``call_mode="batch"`` tag
                # that distinguishes batch review from real-time review.
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
            verification_completed = False
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
                    verification_completed = True
                else:
                    if diag:
                        diag.log("verification", "info", f"Verification batch submitted: {verification_job.batch_id}", {
                            "batch_id": verification_job.batch_id,
                        })
                    save_batch_state(build_resume_state(
                        phase=PHASE_VERIFICATION_WAVE_POLL,
                        submission=app._batch_submission,
                        review_state=review_state,
                        verification_batch=verification_job,
                        verification_started=True,
                    ))
                    collect_batch_verification_results(
                        verification_job,
                        verifiable_findings,
                        cycle=cycle,
                        log=app._make_diag_log("verification", run_epoch),
                        progress=app._make_diag_progress("verification", run_epoch),
                        cache=cache,
                    )
                    verification_completed = True
                if diag:
                    from .orchestration.diagnostics import bound_structured_payload
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
                                # Chunk I: keep the batch path's
                                # event payload aligned with the
                                # real-time path so diagnostics
                                # totals come out the same shape
                                # in either mode.
                                "verification_mode": f.verification.verification_mode,
                                "verification_profile": f.verification.verification_profile,
                                "grounded": f.verification.grounded,
                                "cache_status": f.verification.cache_status,
                                "escalated": f.verification.escalated,
                                # Chunk D1.3: escalation telemetry. See
                                # the matching block in
                                # ``review_run_controller`` for the
                                # rationale — keeping the batch and
                                # real-time payloads aligned means the
                                # diagnostics summary aggregates the
                                # same shape in either mode.
                                "escalation_attempted": f.verification.escalation_attempted,
                                "initial_model": f.verification.initial_model,
                                "initial_verdict": f.verification.initial_verdict,
                                "escalation_changed_verdict": f.verification.escalation_changed_verdict,
                                "escalation_reason": f.verification.escalation_reason,
                                # Chunk J: tag remote verifications as
                                # batch API calls so the per-phase
                                # rollup's call_mode counters reflect
                                # the path that actually ran.
                                "api_call": f.verification.cache_status not in ("hit", "local_skip"),
                                "call_mode": "batch",
                                "model": f.verification.model_used,
                                "web_search_requests": f.verification.web_search_requests,
                                # Chunk 6: surface retry telemetry so the
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

            save_batch_state(build_resume_state(
                phase=PHASE_CROSS_CHECK,
                submission=app._batch_submission,
                review_state=review_state,
                verification_started=bool(verifiable_findings),
                verification_completed=verification_completed,
            ))
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
                    "Cross-check skipped due to missing resumable extracted specs."
                ))
                if diag:
                    diag.log("cross_check", "warning", "Cross-check skipped: missing resumable extracted specs")
            if diag and review_state.cross_check_result:
                cc = review_state.cross_check_result
                # Chunk J: cross-check after batch review is real-time
                # (the cross-check pass always runs live regardless of
                # the review path), so the call_mode reflects that.
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
                    save_batch_state(build_resume_state(
                        phase=PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
                        submission=app._batch_submission,
                        review_state=review_state,
                        verification_batch=cross_check_verification_job,
                        cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                        verification_started=bool(verifiable_findings),
                        verification_completed=verification_completed,
                    ))
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
            save_batch_state(build_resume_state(
                phase=PHASE_FINALIZE,
                submission=app._batch_submission,
                review_state=review_state,
                cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                verification_started=bool(verifiable_findings),
                verification_completed=verification_completed,
            ))
            app._dispatch_if_current(run_epoch, lambda r=final_result: app._on_review_complete(r))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            if diag:
                diag.log("batch_collect", "error", f"Batch collection failed: {e}", {"traceback": traceback.format_exc()})
            app._dispatch_if_current(run_epoch, lambda: app._on_review_error(err))

    threading.Thread(target=_do_collect, daemon=True).start()


def check_pending_batch(app) -> None:
    loaded = load_batch_state()
    if loaded is None:
        return
    submission = loaded["submission"]
    phase = loaded.get("phase", PHASE_REVIEW_POLL)
    age_str = format_batch_age(submission.job.created_at)
    # Chunk 1: warn before the local 28-day cutoff so the user can resume
    # while the Anthropic result-download window is still open.
    nearing_expiry = batch_state_nearing_expiry(submission.job.created_at)

    dialog = ctk.CTkToplevel(app)
    dialog.title("Pending Batch Found")
    dialog.geometry("480x260" if nearing_expiry else "480x220")
    dialog.configure(fg_color=COLORS["bg_dark"])
    dialog.resizable(False, False)
    dialog.transient(app)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_force()

    inner = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
    inner.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        inner, text="A batch submission is pending",
        font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=16, pady=(16, 4))

    info_text = (
        f"Batch ID: {submission.job.batch_id[:30]}...\n"
        f"Files: {len(submission.files_reviewed)} specs  •  Model: Opus 4.7\n"
        f"Submitted: {age_str}  •  Phase: {phase}"
    )
    ctk.CTkLabel(
        inner, text=info_text, font=ctk.CTkFont(family="Consolas", size=11),
        text_color=COLORS["text_secondary"], justify="left",
    ).pack(anchor="w", padx=16, pady=(0, 12))

    if nearing_expiry:
        ctk.CTkLabel(
            inner,
            text=(
                "Heads up: this batch was submitted more than 25 days ago. "
                "Results expire on the Anthropic side around day 29; "
                "Spec Critic will drop this saved state at day 28. "
                "Resume soon or discard."
            ),
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COLORS["warning"],
            wraplength=440,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 12))

    btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
    btn_frame.pack(fill="x", padx=16, pady=(0, 16))
    btn_kw = {"height": 34, "font": ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), "corner_radius": 6}

    def _resume():
        dialog.destroy()
        app._resume_batch(loaded)

    def _discard():
        dialog.destroy()
        delete_batch_state()
        app.log.log("Discarded pending batch state.", level="muted")

    ctk.CTkButton(
        btn_frame, text="Resume Batch", width=140,
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=_resume, **btn_kw,
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        btn_frame, text="Discard", width=100,
        fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
        border_width=1, border_color=COLORS["border"],
        text_color=COLORS["text_secondary"], command=_discard, **btn_kw,
    ).pack(side="left")


def format_batch_age(created_at: float) -> str:
    try:
        age_seconds = time.time() - created_at
        if age_seconds < 3600:
            return f"{int(age_seconds / 60)} minutes ago"
        elif age_seconds < 86400:
            return f"{age_seconds / 3600:.1f} hours ago"
        else:
            return f"{age_seconds / 86400:.1f} days ago"
    except Exception:
        return "unknown time"


def is_valid_verification_resume_state(loaded_state: dict) -> bool:
    review_state = loaded_state.get("review_state")
    verification_batch = loaded_state.get("verification_batch")
    if review_state is None or not isinstance(verification_batch, BatchJob):
        return False
    batch_id = getattr(verification_batch, "batch_id", None)
    if not isinstance(batch_id, str) or not batch_id.startswith("msgbatch_") or len(batch_id) <= len("msgbatch_"):
        return False
    request_map = getattr(verification_batch, "request_map", None)
    if not isinstance(request_map, dict) or not request_map:
        return False
    return True


def resume_batch(app, loaded_state: dict) -> None:
    submission: BatchSubmission = loaded_state["submission"]
    phase = loaded_state.get("phase", PHASE_REVIEW_POLL)
    api_key = app.api_key_entry.get().strip()
    if not api_key:
        api_key = load_api_key_from_file() or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        app.log.log_error("API key is required to resume batch. Enter your key and try again.")
        return
    if phase not in SUPPORTED_PHASES:
        app.log.log_error("Saved batch state has unsupported phase. Discarding it.")
        delete_batch_state()
        return

    os.environ["ANTHROPIC_API_KEY"] = api_key
    app._batch_submission = submission
    app._cross_check_for_review = getattr(submission, "cross_check_enabled", False)
    cross_check_skipped = False
    if app._cross_check_for_review and not getattr(submission, "prepared_specs", None):
        app.log.log_warning("Cross-check was enabled but spec content could not be restored from saved state. Cross-check will be skipped for this resumed batch.")
        app._cross_check_for_review = False
        cross_check_skipped = True
    app._project_context_for_review = getattr(submission, "project_context", "")
    app._selected_cycle_label = getattr(submission, "cycle_label", DEFAULT_CYCLE.label)
    app._cross_check_var.set(bool(getattr(submission, "cross_check_enabled", False)))
    app.is_processing = True

    app.log.log("─" * 40, level="muted", timestamp=False, paced=False)
    app.log.log_step(f"Resuming batch: {submission.job.batch_id}")
    app.log.log(f"  {len(submission.files_reviewed)} specs • Phase: {phase}", level="muted")

    app.run_button.set_processing()
    app.run_button.configure(text="Polling...")
    app.progress_bar.pack(fill="x", pady=(8, 0), after=app.run_button)
    app.progress_bar.set(0.4)
    app.progress_bar.configure(mode="determinate")
    if phase == PHASE_REVIEW_POLL:
        app._poll_batch()
        return
    if phase == PHASE_REVIEW_COLLECT:
        app._collect_batch_results()
        return
    if phase in (PHASE_VERIFICATION_POLL, PHASE_VERIFICATION_WAVE_POLL):
        if not app._is_valid_verification_resume_state(loaded_state):
            app.log.log_error("Saved verification resume state is incomplete. Discarding it.")
            delete_batch_state()
            app._reset_ui()
            return
        app._resume_verification_poll(loaded_state)
        return
    if phase in (PHASE_CROSS_CHECK_VERIFICATION_POLL, PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL):
        if not app._is_valid_verification_resume_state(loaded_state):
            app.log.log_error("Saved cross-check verification resume state is incomplete. Discarding it.")
            delete_batch_state()
            app._reset_ui()
            return
        app._resume_cross_check_verification_poll(loaded_state)
        return
    if phase == PHASE_FINALIZE:
        review_state: CollectedBatchState | None = loaded_state.get("review_state")
        if review_state is None:
            app.log.log_error("Saved finalize resume state is incomplete. Discarding it.")
            delete_batch_state()
            app._reset_ui()
            return
        if cross_check_skipped:
            review_state.cross_check_skipped_due_to_missing_specs = True
        result = finalize_batch_result(review_state)
        app._on_review_complete(result)
        return
    if phase == PHASE_CROSS_CHECK:
        review_state2: CollectedBatchState | None = loaded_state.get("review_state")
        if review_state2 is None:
            app.log.log_error("Saved cross-check resume state is incomplete. Discarding it.")
            delete_batch_state()
            app._reset_ui()
            return
        run_epoch = app._next_run_epoch()

        def _do_resume_cross_check():
            try:
                cycle = AVAILABLE_CYCLES.get(getattr(app._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)
                cache = _make_verification_cache()

                def _on_progress(pct, msg):
                    app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log_step(m))
                    app._dispatch_if_current(run_epoch, lambda p=pct: app.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

                review_state_local = run_cross_check_for_batch(
                    review_state2,
                    specs=getattr(app._batch_submission, "prepared_specs", None),
                    project_context=getattr(app, "_project_context_for_review", ""),
                    cycle=cycle,
                    log=lambda msg: app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log(m, level="info")),
                )
                cross_check_findings = list(review_state_local.cross_check_result.findings) if review_state_local.cross_check_result and review_state_local.cross_check_result.findings else []
                if cross_check_findings:
                    cross_check_verification_job = start_batch_verification(
                        cross_check_findings,
                        cycle=cycle,
                        log=lambda msg: app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log(m, level="info")),
                        progress=_on_progress,
                        cache=cache,
                    )
                    if cross_check_verification_job is not None:
                        save_batch_state(build_resume_state(
                            phase=PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
                            submission=app._batch_submission,
                            review_state=review_state_local,
                            verification_batch=cross_check_verification_job,
                            cross_check_skipped_due_to_missing_specs=review_state_local.cross_check_skipped_due_to_missing_specs,
                            verification_started=True,
                            verification_completed=True,
                        ))
                        collect_batch_verification_results(
                            cross_check_verification_job,
                            cross_check_findings,
                            cycle=cycle,
                            log=lambda msg: app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log(m, level="info")),
                            progress=_on_progress,
                            cache=cache,
                        )
                _persist_verification_cache(cache)
                result = finalize_batch_result(review_state_local)
                app._dispatch_if_current(run_epoch, lambda r=result: app._on_review_complete(r))
            except Exception as e:
                import traceback
                err = f"{e}\n{traceback.format_exc()}"
                app._dispatch_if_current(run_epoch, lambda: app._on_review_error(err))

        threading.Thread(target=_do_resume_cross_check, daemon=True).start()
        return


def resume_verification_poll(app, loaded_state: dict) -> None:
    run_epoch = app._next_run_epoch()
    review_state: CollectedBatchState = loaded_state["review_state"]
    verification_job = loaded_state["verification_batch"]
    cycle = AVAILABLE_CYCLES.get(getattr(app._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)
    verifiable_findings = list(review_state.review_result.findings)

    def _do_resume_verification():
        try:
            cache = _make_verification_cache()

            def _on_progress(pct, msg):
                app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log_step(m))
                app._dispatch_if_current(run_epoch, lambda p=pct: app.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

            collect_batch_verification_results(
                verification_job,
                verifiable_findings,
                cycle=cycle,
                log=lambda msg: app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log(m, level="info")),
                progress=_on_progress,
                cache=cache,
            )
            save_batch_state(build_resume_state(
                phase=PHASE_CROSS_CHECK,
                submission=app._batch_submission,
                review_state=review_state,
                cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                verification_started=True,
                verification_completed=True,
            ))
            review_state_local = run_cross_check_for_batch(
                review_state,
                specs=getattr(app._batch_submission, "prepared_specs", None),
                project_context=getattr(app, "_project_context_for_review", ""),
                cycle=cycle,
                log=lambda msg: app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log(m, level="info")),
            )
            cross_check_findings = list(review_state_local.cross_check_result.findings) if review_state_local.cross_check_result and review_state_local.cross_check_result.findings else []
            if cross_check_findings:
                cross_check_verification_job = start_batch_verification(
                    cross_check_findings,
                    cycle=cycle,
                    log=lambda msg: app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log(m, level="info")),
                    progress=_on_progress,
                    cache=cache,
                )
                if cross_check_verification_job is not None:
                    save_batch_state(build_resume_state(
                        phase=PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
                        submission=app._batch_submission,
                        review_state=review_state_local,
                        verification_batch=cross_check_verification_job,
                        cross_check_skipped_due_to_missing_specs=review_state_local.cross_check_skipped_due_to_missing_specs,
                        verification_started=True,
                        verification_completed=True,
                    ))
                    collect_batch_verification_results(
                        cross_check_verification_job,
                        cross_check_findings,
                        cycle=cycle,
                        log=lambda msg: app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log(m, level="info")),
                        progress=_on_progress,
                        cache=cache,
                    )
            _persist_verification_cache(cache)
            result = finalize_batch_result(review_state_local)
            app._dispatch_if_current(run_epoch, lambda r=result: app._on_review_complete(r))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            app._dispatch_if_current(run_epoch, lambda: app._on_review_error(err))

    threading.Thread(target=_do_resume_verification, daemon=True).start()


def resume_cross_check_verification_poll(app, loaded_state: dict) -> None:
    run_epoch = app._next_run_epoch()
    review_state: CollectedBatchState = loaded_state["review_state"]
    verification_job = loaded_state["verification_batch"]
    cycle = AVAILABLE_CYCLES.get(getattr(app._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)
    cross_check_findings = list(review_state.cross_check_result.findings) if review_state.cross_check_result and review_state.cross_check_result.findings else []

    def _do_resume_cross_check_verification():
        try:
            cache = _make_verification_cache()

            def _on_progress(pct, msg):
                app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log_step(m))
                app._dispatch_if_current(run_epoch, lambda p=pct: app.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

            if cross_check_findings:
                collect_batch_verification_results(
                    verification_job,
                    cross_check_findings,
                    cycle=cycle,
                    log=lambda msg: app._dispatch_if_current(run_epoch, lambda m=msg: app.log.log(m, level="info")),
                    progress=_on_progress,
                    cache=cache,
                )
            _persist_verification_cache(cache)
            save_batch_state(build_resume_state(
                phase=PHASE_FINALIZE,
                submission=app._batch_submission,
                review_state=review_state,
                cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                verification_started=True,
                verification_completed=True,
            ))
            result = finalize_batch_result(review_state)
            app._dispatch_if_current(run_epoch, lambda r=result: app._on_review_complete(r))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            app._dispatch_if_current(run_epoch, lambda: app._on_review_error(err))

    threading.Thread(target=_do_resume_cross_check_verification, daemon=True).start()
