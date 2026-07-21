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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import messagebox

from .. import __version__
from ..batch.batch import BatchStatus
from ..batch.batch_runtime import (
    BatchNotFinishedError,
    DEFAULT_REVIEW_POLL_POLICY,
    ensure_batch_ended,
    poll_batch_bounded,
)
from ..core.project_profile import ProjectProfile
from ..modules import DEFAULT_MODULE, get_module, require_module
from ..programs import SpecAssignment, get_program, routed_module_ids
from ..orchestration.batch_resume import (
    PendingBatch,
    PendingProgramRun,
    clear_pending_batch,
    load_pending_run,
    save_pending_batch,
    save_pending_program_run,
    thin_submission_from_batch_results,
)
from ..orchestration.program_pipeline import (
    ProgramSubmission,
    ProgramSubmissionError,
    collect_program_results,
    start_program_review,
)
from ..orchestration.diagnostics import DiagnosticsReport
from ..orchestration.diag_recording import (
    record_compliance,
    record_cross_check,
    record_review_collect,
    record_verification_findings,
)
from ..orchestration.pipeline import (
    BatchSubmission,
    collect_review_batch_results,
    collect_stage_band,
    finalize_batch_result,
    location_inputs_for_submission,
    run_compliance_for_batch,
    run_cross_check_for_batch,
    run_drawing_impact_for_batch,
    start_batch_review,
    verify_findings_for_run,
    _make_verification_cache,
    _persist_verification_cache,
)
from ..review.reviewer import REVIEW_MODEL_DEFAULT
from .review_run_controller import _maybe_start_recorder, _stop_recorder

_BATCH_TIMING_COPY = "Usually 45 min to 2 hrs, 24 hrs maximum (Extremely Rare)"


def _continue_partial_program(app, submission: ProgramSubmission) -> None:
    """Resume the correct lifecycle after a later child submission fails."""
    if submission.review_transport == "realtime":
        on_realtime_reviewed(app, submission)
    else:
        on_batch_submitted(app, submission)


def _resolve_recovery_module(program, choice: str):
    """Resolve an index, id, display name, or unambiguous discipline name."""
    value = (choice or "").strip()
    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(program.implemented_module_ids):
            return require_module(program.implemented_module_ids[index])
    folded = value.casefold()
    for module_id in program.implemented_module_ids:
        module = require_module(module_id)
        if folded in {module_id.casefold(), module.display_name.casefold()}:
            return module
    discipline_matches = [
        require_module(module_id)
        for module_id in program.implemented_module_ids
        if folded == module_id.casefold().rsplit("_", 1)[-1]
    ]
    if len(discipline_matches) == 1:
        return discipline_matches[0]
    return None


def _start_selected_review(app, program, run_epoch: int, diag, **kwargs):
    """Dispatch a one-module program or a routed multi-module program."""
    if len(program.implemented_module_ids) == 1:
        return start_batch_review(**kwargs)

    module = kwargs.pop("module", None)
    del module  # the routed program resolves every partition strictly
    # ``files`` is the legacy single-module execution surface.  A routed
    # program derives each child file list from the confirmed assignments;
    # forwarding the scalar list would both bypass that contract and be an
    # unexpected argument to ``prepare_program_review``.
    kwargs.pop("files", None)
    transport = kwargs.get("review_transport", "batch") or "batch"

    def persist_partition(current: ProgramSubmission) -> None:
        if transport != "batch":
            return
        save_pending_program_run(
            PendingProgramRun.from_submission(
                current,
                input_dir=app.input_dir,
                files=app._selected_files_for_review,
                run_id=diag.run_id if diag is not None else "",
                app_version=__version__,
            )
        )

    return start_program_review(
        program_id=program.program_id,
        assignments=app._routing_assignments_for_review,
        on_partition_submitted=persist_partition,
        **kwargs,
    )


def submit_batch_thread(app, run_epoch: int) -> None:
    diag = app._diagnostics_report
    transport = getattr(app, "_review_transport_for_review", "batch") or "batch"
    # Start the trace recorder if tracing is enabled. The recorder lives
    # for the entire run lifecycle (submit → [poll] → collect → verify →
    # finalize) and is stopped after collect_batch_results completes.
    # Store on the app so the collect path can reach it.
    program = get_program(
        getattr(app, "_selected_program_id_for_review", None)
        or getattr(app, "_selected_program_id", None)
    )
    active_module_ids = tuple(
        getattr(app, "_routed_module_ids_for_review", None) or ()
    )
    if not active_module_ids:
        active_module_ids = routed_module_ids(
            getattr(app, "_routing_assignments_for_review", None) or (),
            program=program,
        )
    if not active_module_ids:
        active_module_ids = (program.implemented_module_ids[0],)
    module = require_module(active_module_ids[0])
    profile = getattr(app, "_project_profile_for_review", None)
    profile_dict = profile.to_dict() if profile is not None else None
    app._trace_recorder = _maybe_start_recorder(
        run_id=diag.run_id if diag is not None else "no_run_id",
        mode=transport,
        model=REVIEW_MODEL_DEFAULT,
        cycle_label=(
            module.cycle.label if len(active_module_ids) == 1 else "per-module"
        ),
        module_id=(
            module.module_id
            if len(active_module_ids) == 1
            else ",".join(active_module_ids)
        ),
        files=app._selected_files_for_review,
        project_profile=profile_dict,
    )
    try:
        if diag:
            diag.log(
                "batch_submit", "step",
                "Starting real-time review" if transport == "realtime"
                else "Preparing batch submission",
            )
        if transport == "realtime":
            # The reviews run to completion INSIDE start_batch_review on
            # this transport — reflect that on the button for the duration.
            # (A research-enabled run overwrites this with its own label
            # below; the reviews still follow within the same call.)
            app._dispatch_if_current(
                run_epoch,
                lambda: app.run_button.configure(text="Reviewing specs (live)..."),
            )

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

        submission = _start_selected_review(app, program, run_epoch, diag,
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
            review_transport=transport,
        )
        if transport == "realtime":
            # The reviews already ran to completion — nothing is pending
            # remotely, so there is no pending-batch state to persist (a
            # crash mid-run loses in-flight work by design) and no polling
            # phase; hand straight off to collection.
            if diag:
                diag.log("batch_submit", "success", "Real-time review complete", {
                    "files_reviewed": len(submission.files_reviewed),
                    "files_expected": (
                        len(submission.expected_files_reviewed)
                        if isinstance(submission, ProgramSubmission)
                        else len(submission.files_reviewed)
                    ),
                    "routed_requests": (
                        submission.routed_request_count
                        if isinstance(submission, ProgramSubmission)
                        else len(submission.review_request_ids)
                    ),
                    "expected_routed_requests": (
                        submission.expected_routed_request_count
                        if isinstance(submission, ProgramSubmission)
                        else len(submission.review_request_ids)
                    ),
                    "unsupported_skipped": (
                        len(submission.skipped_assignments)
                        if isinstance(submission, ProgramSubmission)
                        else 0
                    ),
                })
            app._dispatch_if_current(run_epoch, lambda: on_realtime_reviewed(app, submission))
            return
        if diag:
            batch_label = (
                ", ".join(submission.batch_ids.values())
                if isinstance(submission, ProgramSubmission)
                else submission.job.batch_id
            )
            routed_requests = (
                submission.routed_request_count
                if isinstance(submission, ProgramSubmission)
                else len(submission.review_request_ids)
            )
            skipped_count = (
                len(submission.skipped_assignments)
                if isinstance(submission, ProgramSubmission)
                else 0
            )
            diag.log("batch_submit", "success", f"Batch submitted: {batch_label}", {
                "batch_ids": (
                    submission.batch_ids
                    if isinstance(submission, ProgramSubmission)
                    else {submission.module_id: submission.job.batch_id}
                ),
                "files_queued": len(submission.files_reviewed),
                "files_expected": (
                    len(submission.expected_files_reviewed)
                    if isinstance(submission, ProgramSubmission)
                    else len(submission.files_reviewed)
                ),
                "routed_requests": routed_requests,
                "expected_routed_requests": (
                    submission.expected_routed_request_count
                    if isinstance(submission, ProgramSubmission)
                    else routed_requests
                ),
                "unsupported_skipped": skipped_count,
            })
        # Persist enough state to reconnect to this batch if the poller
        # detaches (closed app, lost network, no-progress / max-elapsed
        # timeout). The batch keeps running remotely; on next launch the user
        # is offered to resume it. Best-effort — never block the run.
        if isinstance(submission, ProgramSubmission):
            save_pending_program_run(
                PendingProgramRun.from_submission(
                    submission,
                    input_dir=app.input_dir,
                    files=app._selected_files_for_review,
                    run_id=diag.run_id if diag is not None else "",
                    app_version=__version__,
                )
            )
        else:
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
    except ProgramSubmissionError as e:
        partial = e.partial_submission
        if not partial.partitions:
            if diag:
                diag.log("batch_submit", "error", f"Program submission failed: {e}")
            _stop_recorder(getattr(app, "_trace_recorder", None))
            app._trace_recorder = None
            app._dispatch_if_current(
                run_epoch,
                lambda msg=str(e): app._on_review_error(msg),
            )
            return
        if diag:
            diag.log(
                "batch_submit",
                "warning",
                f"Partial program submission: {e}",
                {"batch_ids": partial.batch_ids},
            )
        app._dispatch_if_current(
            run_epoch,
            lambda msg=str(e): app.log.log_warning(
                f"{msg} Completed or submitted module work will be retained; the "
                "combined report will mark missing coverage."
            ),
        )
        app._dispatch_if_current(
            run_epoch, lambda s=partial: _continue_partial_program(app, s)
        )
    except Exception as e:
        import traceback
        err = f"{e}\n{traceback.format_exc()}"
        if diag:
            diag.log("batch_submit", "error", f"Batch submission failed: {e}", {"traceback": traceback.format_exc()})
        # Stop the recorder on submission failure so its files get flushed.
        _stop_recorder(getattr(app, "_trace_recorder", None))
        app._trace_recorder = None
        app._dispatch_if_current(run_epoch, lambda: app._on_review_error(err))


def on_batch_submitted(app, submission: BatchSubmission | ProgramSubmission) -> None:
    app._batch_submission = submission
    app.progress_bar.set(0.4)
    batch_label = (
        ", ".join(
            f"{require_module(module_id).display_name}: {batch_id}"
            for module_id, batch_id in submission.batch_ids.items()
        )
        if isinstance(submission, ProgramSubmission)
        else submission.job.batch_id
    )
    if isinstance(submission, ProgramSubmission) and submission.missing_module_ids:
        app.log.log_warning(f"Partial batch submission retained: {batch_label}")
    else:
        app.log.log_success(f"Batch submitted: {batch_label}")
    if isinstance(submission, ProgramSubmission):
        if submission.missing_module_ids:
            app.log.log_warning(
                f"  Partial submission: {len(submission.files_reviewed)} of "
                f"{len(submission.expected_files_reviewed)} routed spec(s), "
                f"{submission.routed_request_count} of "
                f"{submission.expected_routed_request_count} module request(s) "
                "submitted."
            )
        else:
            app.log.log(
                f"  {len(submission.files_reviewed)} specs routed as "
                f"{submission.routed_request_count} module request(s) • "
                f"{len(submission.skipped_assignments)} unsupported/skipped • "
                "50% cost savings",
                level="muted",
            )
    else:
        app.log.log(
            f"  {len(submission.files_reviewed)} specs queued • 50% cost savings",
            level="muted",
        )
    app.log.log_step(f"Polling for results ({_BATCH_TIMING_COPY})...")
    app.run_button.configure(text="Polling...")
    app._poll_batch()


def on_realtime_reviewed(
    app, submission: BatchSubmission | ProgramSubmission
) -> None:
    """Real-time counterpart to ``on_batch_submitted``: no polling phase.

    The streaming reviews already ran inside ``start_batch_review``, so the
    submission arrives carrying its results — store it and go straight to
    the shared collect sequence.
    """
    app._batch_submission = submission
    app.progress_bar.set(0.55)
    if isinstance(submission, ProgramSubmission) and submission.missing_module_ids:
        app.log.log_warning(
            "Real-time review partially complete — "
            f"{submission.routed_request_count} of "
            f"{submission.expected_routed_request_count} routed module request(s) "
            "completed"
        )
    else:
        app.log.log_success(
            f"Real-time review complete — {len(submission.files_reviewed)} spec(s) reviewed"
        )
    if isinstance(submission, ProgramSubmission):
        app.log.log(
            f"  {submission.routed_request_count} module request(s) • "
            f"{len(submission.skipped_assignments)} unsupported/skipped • "
            "streamed live • standard API pricing",
            level="muted",
        )
    else:
        app.log.log("  streamed live • standard API pricing", level="muted")
    app._collect_batch_results()


def poll_batch(app) -> None:
    if app._batch_submission is None:
        return
    run_epoch = app._next_run_epoch()
    threading.Thread(target=app._poll_and_collect_thread, args=(run_epoch,), daemon=True).start()


def update_poll_progress(app, status: BatchStatus) -> None:
    diag = app._diagnostics_report
    # Polling owns the 40→55 slice of the bar; the collect stages own
    # 55→100 (COLLECT_PROGRESS_SPAN), so the bar stays monotone across the
    # poll→collect handoff. The caption/log keep the real batch percentages.
    batch_pct = 0.40 + (status.progress_pct / 100.0) * 0.15
    app.progress_bar.set(min(batch_pct, 0.55))
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


def _poll_program_partitions(app, submission: ProgramSubmission, run_epoch: int):
    """Poll all child batch ids concurrently and aggregate request progress."""
    statuses: dict[str, BatchStatus] = {
        module_id: BatchStatus(
            status="in_progress",
            processing=len(child.review_request_ids),
            succeeded=0,
            errored=0,
            canceled=0,
            expired=0,
            total=len(child.review_request_ids),
        )
        for module_id, child in submission.partitions.items()
    }
    lock = threading.Lock()

    def aggregate() -> BatchStatus:
        values = list(statuses.values())
        return BatchStatus(
            status=("ended" if all(v.completed >= v.total for v in values) else "in_progress"),
            processing=sum(v.processing for v in values),
            succeeded=sum(v.succeeded for v in values),
            errored=sum(v.errored for v in values),
            canceled=sum(v.canceled for v in values),
            expired=sum(v.expired for v in values),
            total=sum(v.total for v in values),
        )

    def poll_one(module_id: str, child: BatchSubmission):
        def on_progress(status: BatchStatus) -> None:
            with lock:
                statuses[module_id] = status
                combined = aggregate()
            app._dispatch_if_current(
                run_epoch, lambda s=combined: app._update_poll_progress(s)
            )

        return poll_batch_bounded(
            child.job.batch_id,
            policy=DEFAULT_REVIEW_POLL_POLICY,
            log=app._make_diag_log("batch_poll", run_epoch),
            progress_cb=on_progress,
        )

    outcomes = {}
    with ThreadPoolExecutor(max_workers=max(1, len(submission.partitions))) as pool:
        futures = {
            pool.submit(poll_one, module_id, child): module_id
            for module_id, child in submission.partitions.items()
        }
        for future in as_completed(futures):
            module_id = futures[future]
            try:
                outcomes[module_id] = future.result()
            except Exception as exc:  # keep other remote batches resumable
                outcomes[module_id] = exc
    failures = []
    for module_id, outcome in outcomes.items():
        if isinstance(outcome, Exception):
            failures.append(f"{module_id}: {outcome}")
        elif outcome.detached or outcome.poll_failed:
            reason = outcome.detach_reason or outcome.poll_error or "unknown"
            failures.append(f"{module_id}: {reason}")
    return failures


def poll_and_collect_thread(app, run_epoch: int) -> None:
    if app._batch_submission is None:
        return
    if isinstance(app._batch_submission, ProgramSubmission):
        failures = _poll_program_partitions(
            app, app._batch_submission, run_epoch
        )
        if failures:
            batch_ids = ", ".join(app._batch_submission.batch_ids.values())
            msg = (
                "Program batch polling stopped: " + "; ".join(failures)
                + f". Batch IDs {batch_ids} may still be running remotely."
            )
            _stop_recorder(getattr(app, "_trace_recorder", None))
            app._trace_recorder = None
            app._dispatch_if_current(run_epoch, lambda m=msg: app._on_review_error(m))
            return
        app._dispatch_if_current(
            run_epoch,
            lambda: app.log.log_success(
                "All module batches complete — collecting results..."
            ),
        )
        app._dispatch_if_current(run_epoch, app._collect_batch_results)
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


# Run-button captions for the collect stages, keyed by the ``stage=`` kwarg
# every collect progress emission now carries. Used by the program collect
# branch (whose stage transitions happen inside the Tk-free pipeline); the
# single-module branch sets its captions inline at each stage boundary.
_STAGE_CAPTIONS: dict[str, str] = {
    "review_collect": "Collecting results...",
    "verify_round1": "Verifying findings...",
    "cross_check": "Cross-check (live API)...",
    "compliance": "Compliance check (live API)...",
    "verify_round2": "Verifying cross-check...",
    "drawing_impact": "Analyzing drawing impact (live API)...",
}


def _make_program_collect_progress(app, run_epoch: int):
    """Wrap the diag progress callback with stage→run-button captioning."""
    base_progress = app._make_diag_progress("batch_collect", run_epoch)
    last_caption: list[str] = [""]

    def _progress(pct, msg, *, stage: str | None = None, **kwargs):
        base_progress(pct, msg, **kwargs)
        caption = _STAGE_CAPTIONS.get(stage or "")
        if caption and caption != last_caption[0]:
            last_caption[0] = caption
            app._dispatch_if_current(
                run_epoch, lambda c=caption: app.run_button.configure(text=c)
            )

    return _progress


def collect_batch_results(app) -> None:
    run_epoch = app._next_run_epoch()
    diag = app._diagnostics_report

    def _do_collect():
        try:
            if app._batch_submission is None:
                raise RuntimeError("No active batch submission to collect.")
            if isinstance(app._batch_submission, ProgramSubmission):
                if diag:
                    diag.log(
                        "batch_collect",
                        "step",
                        "Collecting routed module results",
                        {"batch_ids": app._batch_submission.batch_ids},
                    )
                final_result = collect_program_results(
                    app._batch_submission,
                    log=app._make_diag_log("batch_collect", run_epoch),
                    progress=_make_program_collect_progress(app, run_epoch),
                    diagnostics=diag,
                )
                if (
                    final_result.review_transport == "batch"
                    and not final_result.module_errors
                ):
                    clear_pending_batch()
                app._dispatch_if_current(
                    run_epoch, lambda r=final_result: app._on_review_complete(r)
                )
                return
            module = get_module(getattr(app._batch_submission, "module_id", None))
            transport = getattr(app._batch_submission, "review_transport", "batch") or "batch"

            # NOTE: this collect → verify → cross-check → verify → finalize
            # sequence is mirrored, UI-free, by
            # ``pipeline.run_batch_collection_headless`` (used by the recovery
            # tool). Keep the two stage orders in lockstep; the transport
            # branches live in the shared pipeline helpers
            # (``collect_review_batch_results`` / ``verify_findings_for_run``),
            # not here. A future refactor should collapse the two sequences
            # onto one shared core (see PR discussion).
            if diag:
                diag.log("batch_collect", "step", "Collecting review batch results")
            review_state = collect_review_batch_results(
                app._batch_submission,
                log=app._make_diag_log("batch_collect", run_epoch),
            )
            rv = review_state.review_result
            # Recording rows live in the shared Tk-free recorders
            # (``orchestration.diag_recording``) so the program collect path
            # records the SAME telemetry over each child result. The
            # real-time double-count guard (per-spec rows already recorded by
            # the runner) lives inside the recorder.
            record_review_collect(diag, rv, transport=transport)

            verifiable_findings = list(rv.findings)
            cache = _make_verification_cache(log=app._make_diag_log("verification", run_epoch))
            # WS-4 location-aware verification (D-9): derived once from the
            # submission's persisted profile; (None, None) on profile-less
            # runs keeps request bytes and cache keys unchanged. Mirrored in
            # run_batch_collection_headless.
            user_location, jurisdiction_fp = location_inputs_for_submission(
                app._batch_submission
            )
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
                    diag.log(
                        "verification", "step",
                        f"Verifying {len(verifiable_findings)} findings ({transport})",
                    )
                # Transport-routed: batch waves on a batch run, streaming
                # pool on a real-time run (zero batch polling end-to-end).
                # Submission / all-resolved-locally details arrive through
                # the log callback from inside the helper.
                verify_findings_for_run(
                    verifiable_findings,
                    module=module,
                    transport=transport,
                    log=app._make_diag_log("verification", run_epoch),
                    progress=app._make_diag_progress("verification", run_epoch),
                    cache=cache,
                    user_location=user_location,
                    jurisdiction_fingerprint=jurisdiction_fp,
                    band=collect_stage_band("verify_round1"),
                    stage="verify_round1",
                )
                record_verification_findings(
                    diag, verifiable_findings, transport=transport
                )

            if diag:
                diag.log("cross_check", "step", "Running cross-spec coordination check")
            app._dispatch_if_current(run_epoch, lambda: app.run_button.configure(text="Cross-check (live API)..."))
            app._dispatch_if_current(run_epoch, lambda: app.log.log_step("Running cross-spec coordination check (live API)..."))
            review_state = run_cross_check_for_batch(
                review_state,
                specs=getattr(app._batch_submission, "prepared_specs", None),
                # ``None`` falls back to ``submission.project_context`` — the
                # EFFECTIVE context the batch was submitted with (WS-3 splices
                # the requirements profile into it). The app attribute holds
                # the raw pre-splice user context on a fresh run, which would
                # hide the profile from cross-check on profile-enabled runs;
                # profile-less runs are identical either way.
                project_context=None,
                log=app._make_diag_log("cross_check", run_epoch),
                progress=app._make_diag_progress("cross_check", run_epoch),
                band=collect_stage_band("cross_check"),
            )
            if review_state.cross_check_skipped_due_to_missing_specs:
                app._dispatch_if_current(run_epoch, lambda: app.log.log_warning(
                    "Cross-check skipped due to missing extracted specs."
                ))
                if diag:
                    diag.log("cross_check", "warning", "Cross-check skipped: missing extracted specs")
            record_cross_check(diag, review_state.cross_check_result)

            # WS-4 compliance pass: after cross-check, before verification
            # round 2 (mirrored in ``run_batch_collection_headless`` — keep
            # the two stage orders in lockstep). No-op for flag-off modules.
            module_wants_compliance = getattr(module, "project_profile_enabled", False)
            if module_wants_compliance:
                if diag:
                    diag.log("compliance", "step", "Running local-code compliance check")
                app._dispatch_if_current(run_epoch, lambda: app.run_button.configure(text="Compliance check (live API)..."))
            review_state = run_compliance_for_batch(
                review_state,
                log=app._make_diag_log("compliance", run_epoch),
                progress=app._make_diag_progress("compliance", run_epoch),
                band=collect_stage_band("compliance"),
            )
            record_compliance(diag, review_state.compliance_result)

            cross_check_findings = list(review_state.cross_check_result.findings) if review_state.cross_check_result and review_state.cross_check_result.findings else []
            compliance_findings = list(review_state.compliance_result.findings) if review_state.compliance_result and review_state.compliance_result.findings else []
            # Verification round 2: cross-check + compliance findings in ONE
            # batch (WS-4 step 5) — both are plain findings lists.
            round2_findings = cross_check_findings + compliance_findings
            if round2_findings:
                app._dispatch_if_current(run_epoch, lambda: app.run_button.configure(text="Verifying cross-check..."))
                if diag:
                    diag.log(
                        "cross_check_verification",
                        "step",
                        f"Verifying {len(cross_check_findings)} cross-check + "
                        f"{len(compliance_findings)} compliance findings ({transport})",
                    )
                verify_findings_for_run(
                    round2_findings,
                    module=module,
                    transport=transport,
                    log=app._make_diag_log("cross_check_verification", run_epoch),
                    progress=app._make_diag_progress("cross_check_verification", run_epoch),
                    cache=cache,
                    user_location=user_location,
                    jurisdiction_fingerprint=jurisdiction_fp,
                    band=collect_stage_band("verify_round2"),
                    stage="verify_round2",
                )
                if diag:
                    diag.log("cross_check_verification", "success", "Cross-check verification complete")

            # WS-5 drawing-impact synthesis: the LAST pass, so it can link
            # findings that only just picked up verdicts in round-2
            # verification. Self-gates on a drawing digest being present in
            # Project Context — a no-op (no log line, no API call) otherwise,
            # so a run without attached drawings is unaffected.
            from ..drawing_impact import extract_drawing_digest
            if extract_drawing_digest(getattr(review_state.submission, "project_context", "")):
                app._dispatch_if_current(run_epoch, lambda: app.run_button.configure(text="Analyzing drawing impact (live API)..."))
                if diag:
                    diag.log("drawing_impact", "step", "Explaining how the drawings informed the review")
            review_state = run_drawing_impact_for_batch(
                review_state,
                project_context=None,
                log=app._make_diag_log("drawing_impact", run_epoch),
            )
            if diag and review_state.drawing_impact_result is not None:
                di = review_state.drawing_impact_result
                diag.record_api_call(
                    phase="drawing_impact",
                    model=di.model,
                    message=f"Drawing impact: {di.status}",
                    input_tokens=di.input_tokens,
                    output_tokens=di.output_tokens,
                    cache_creation_input_tokens=di.cache_creation_input_tokens,
                    cache_read_input_tokens=di.cache_read_input_tokens,
                    stop_reason=di.stop_reason,
                    mode="realtime",
                    retry_status="initial",
                    structured_payload=di.structured_payload,
                    extra={
                        "impact_level": di.impact_level,
                        "linked_finding_count": di.linked_finding_count,
                    },
                )

            _persist_verification_cache(cache, log=app._make_diag_log("finalization", run_epoch))
            if diag:
                diag.log("finalization", "step", "Finalizing batch results")
            final_result = finalize_batch_result(review_state)
            # The run finished start-to-report, so the saved pending-batch
            # state is no longer needed — drop it so the next launch doesn't
            # offer to resume an already-collected batch. Only the success path
            # clears it: a detach / collect error leaves it on disk so the user
            # can still resume. Batch runs only — a real-time run never saved
            # pending state, and clearing here would delete a DIFFERENT
            # (earlier, detached) batch's resumable state.
            if transport == "batch":
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
    pending = load_pending_run()
    if pending is None:
        return
    n = (
        len(pending.assignments)
        if isinstance(pending, PendingProgramRun)
        else len(pending.files_reviewed)
    )
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


def start_batch_resume(app, pending: PendingBatch | PendingProgramRun) -> None:
    """Resume an unfinished batch persisted by a prior session."""
    if isinstance(pending, PendingProgramRun):
        first_module_id, first = next(iter(pending.partitions.items()))
        files = list(dict.fromkeys(
            str(item.get("source_path"))
            for item in pending.assignments
            if isinstance(item, dict) and item.get("source_path")
        ))
        batch_ids = [
            str(child.get("batch_id"))
            for child in pending.partitions.values()
            if isinstance(child, dict) and child.get("batch_id")
        ]
        _begin_reconnect_run(
            app,
            reconstruct_fn=lambda log, progress: pending.to_submission(
                log=log, progress=progress
            ),
            model=str(first.get("model") or REVIEW_MODEL_DEFAULT),
            cycle_label=str(first.get("cycle_label") or "per-module"),
            module_id=first_module_id,
            program_id=pending.program_id,
            project_context=str(first.get("project_context") or ""),
            project_profile=pending.project_profile,
            cross_check_enabled=any(
                bool(child.get("cross_check_enabled", False))
                for child in pending.partitions.values()
                if isinstance(child, dict)
            ),
            files_for_review=[Path(path) for path in files],
            input_dir=str(first.get("input_dir") or ""),
            run_id=pending.run_id,
            routing_assignments=tuple(
                SpecAssignment.from_dict(item) for item in pending.assignments
            ),
            files_reviewed_label=[Path(path).name for path in files],
            batch_label=", ".join(batch_ids),
            verb="Resuming",
        )
        return
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
    program = get_program(
        getattr(app, "_selected_program_id", None)
        or getattr(app, "_selected_module_id", None)
    )
    if len(program.implemented_module_ids) == 1:
        module = require_module(program.implemented_module_ids[0])
    else:
        option_lines = [
            f"{index}. {require_module(module_id).display_name}"
            for index, module_id in enumerate(
                program.implemented_module_ids, start=1
            )
        ]
        choice = simpledialog.askstring(
            "Select batch reviewer",
            "A bare batch id does not contain its discipline. Enter the number "
            "of the module that originally submitted this batch:\n\n"
            + "\n".join(option_lines),
            parent=app,
        )
        if choice is None:
            return
        module = _resolve_recovery_module(program, choice)
        if module is None:
            messagebox.showerror(
                "Reviewer required",
                "The batch reviewer was not recognized. Recovery was canceled "
                "so the batch cannot be verified under the wrong discipline.",
            )
            return
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
        # A bare-id recovery rebuilds the request map from the batch's
        # *results* stream, which does not exist until the batch ends — the
        # SDK fails on a still-running batch with the raw "No `results_url`
        # for the given batch" error (observed live on a slow batch ~4h into
        # processing). Poll it to completion first under the standard review
        # policy; an already-ended batch clears the single status check with
        # no waiting, and a typo'd id still fails fast on that same check.
        ensure_batch_ended(
            batch_id,
            policy=DEFAULT_REVIEW_POLL_POLICY,
            log=log,
            progress_cb=lambda s: log(
                f"Poll: {s.succeeded}/{s.total} done, {s.errored} errors",
                level="info",
            ),
        )
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
        program_id=program.program_id,
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
    program_id: str = "",
    routing_assignments: tuple = (),
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

    app._selected_module_id = module_id or DEFAULT_MODULE.module_id
    selected_program = get_program(program_id or app._selected_module_id)
    active_module_ids = (
        routed_module_ids(routing_assignments, program=selected_program)
        if routing_assignments
        else (app._selected_module_id,)
    )
    if not active_module_ids:
        active_module_ids = (app._selected_module_id,)
    app._routed_module_ids_for_review = active_module_ids
    effective_cycle_label = (
        require_module(active_module_ids[0]).cycle.label
        if len(active_module_ids) == 1
        else "per-module"
    )
    diagnostic_module_id = (
        active_module_ids[0]
        if len(active_module_ids) == 1
        else ",".join(active_module_ids)
    )
    app._selected_cycle_label = effective_cycle_label
    app._selected_program_id = selected_program.program_id
    app._selected_program_id_for_review = app._selected_program_id
    selector_var = getattr(app, "_module_selector_var", None)
    if selector_var is not None:
        selector_var.set(selected_program.display_name)
    subtitle = getattr(app, "_header_subtitle", None)
    if subtitle is not None:
        subtitle.configure(text=app._module_subtitle())
    update_profile_visibility = getattr(app, "_update_project_profile_visibility", None)
    if callable(update_profile_visibility):
        update_profile_visibility()
    app._routing_assignments_for_review = tuple(routing_assignments or ())
    app._project_context_for_review = project_context
    # Restore the per-run profile snapshot so the reconnected run's report /
    # tracing / routing see the same location the original submission used.
    app._project_profile_for_review = ProjectProfile.from_dict(project_profile)
    app._cross_check_for_review = cross_check_enabled
    app._selected_files_for_review = list(files_for_review or [])
    if input_dir:
        app.input_dir = input_dir

    app.is_processing = True
    if hasattr(app, "module_selector"):
        app.module_selector.configure(state="disabled")
    app.run_button.set_processing()
    app.run_button.configure(text=f"{verb}...")
    app.progress_bar.pack(fill="x", pady=(8, 0), after=app.run_button)
    app.progress_bar.set(0.4)
    app.progress_bar.configure(mode="determinate")

    diag_kwargs = dict(
        mode="batch",
        model=model,
        cycle_label=effective_cycle_label,
        module_id=diagnostic_module_id,
        program_id=selected_program.program_id,
        module_ids=list(active_module_ids),
        cycle_labels={
            active_id: require_module(active_id).cycle.label
            for active_id in active_module_ids
        },
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
            app, reconstruct_fn, model, effective_cycle_label, diagnostic_module_id,
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
        elif isinstance(e, BatchNotFinishedError) or "results_url" in msg:
            # The batch is still processing. ``ensure_batch_ended`` raises the
            # typed error after the bounded poll gives up; the ``results_url``
            # message match is defense-in-depth for any path that still reads
            # the results stream of an unfinished batch.
            friendly = (
                f"Batch '{batch_label}' is still processing on Anthropic's servers "
                "and didn't finish within this session's polling window. It keeps "
                "running remotely (batches complete within 24h of submission; "
                "results are kept ~29 days) — run Recover batch… again later to "
                "finish the report at no extra cost."
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
