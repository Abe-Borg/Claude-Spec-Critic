#!/usr/bin/env python3
"""Recover a Spec Critic review batch the desktop app stopped polling.

A submitted review batch runs on Anthropic's Message Batches API for up to 24h
and its results stay retrievable for ~29 days. If the app's poller detached
(closed app, lost network, the no-progress / max-elapsed timeout) the batch
kept running remotely — this tool reconnects to it and finishes the run
(poll -> collect -> verify -> cross-check -> Word report + edit sidecar) without
re-submitting or re-paying for the review.

USAGE

  # Resume the most recent run the app saved when it submitted (the common
  # case going forward — full recovery, including cross-spec coordination).
  # Works for a single-module batch AND for a routed multi-module program run
  # (e.g. Hyperscale Data Centers), whose saved manifest carries one child
  # batch per module; every child is polled, collected under its own module,
  # and combined into one program report:
  python scripts/recover_batch.py

  # Recover a batch by id when there is no saved state (e.g. a batch submitted
  # before this feature existed). A bare batch id does not carry its review
  # discipline, so --module is REQUIRED here — defaulting it would collect,
  # cross-check, and verify the batch under the wrong prompts and code basis.
  # Findings-only unless you also point it at the source folder so the specs
  # can be re-read for cross-check:
  python scripts/recover_batch.py --batch-id msgbatch_XXXX --module california_k12_mep
  python scripts/recover_batch.py --batch-id msgbatch_XXXX --module datacenter_fire --input-dir /path/to/specs

  # Choose where the report goes:
  python scripts/recover_batch.py -o ~/Desktop/recovered-report.docx

The Anthropic API key is read from ANTHROPIC_API_KEY, or from the key file the
desktop app saves (so if you have used the app on this machine, no flag needed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Make ``src`` importable when this file is run directly from the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.batch.batch_runtime import (  # noqa: E402
    BatchNotFinishedError,
    DEFAULT_REVIEW_POLL_POLICY,
    PollOutcome,
    ensure_batch_ended,
    poll_batch_bounded,
)
from src.core.api_config import REVIEW_MODEL_DEFAULT  # noqa: E402
from src.core.api_key_store import load_api_key_from_file  # noqa: E402
from src.modules import AVAILABLE_MODULES, get_module  # noqa: E402
from src.orchestration.batch_resume import (  # noqa: E402
    PendingBatch,
    PendingProgramRun,
    apply_saved_state_cleanup,
    load_pending_run,
    pending_batch_path,
    thin_submission_from_batch_results,
)
from src.orchestration.collection_outcome import provisional_notice  # noqa: E402
from src.orchestration.diagnostics import DiagnosticsReport, cost_summary_lines  # noqa: E402
from src.orchestration.pipeline import _get_spec_files, run_batch_collection_headless  # noqa: E402
from src.orchestration.program_pipeline import (  # noqa: E402
    ProgramSubmission,
    collect_program_results,
)
from src.output.edit_sidecar import (  # noqa: E402
    write_edit_instructions_sidecar,
    write_requirements_profile_sidecar,
)
from src.output.report_exporter import export_report  # noqa: E402

_LEVEL_TAG = {"step": "·", "info": " ", "success": "✓", "warning": "!", "error": "✗"}


def _report_collection_cost(diagnostics: DiagnosticsReport, json_path: str | None) -> None:
    """Print what this recovery could account for, and optionally save it.

    **What the figure covers, exactly** (plan WP-15). Two kinds of spend, kept
    apart because a reader deciding whether a recovery was expensive must not
    mistake the one for the other:

    * **Earlier batch spend** — the recovered review batch, and any repair
      batch an earlier collection submitted. ``collect_review_batch_results``
      reads their usage off the retrieved results, but it was billed when
      those batches ran, before this recovery started.
    * **This recovery's own spend** — the calls this process made: a repair
      batch it submitted, verification rounds one and two, cross-check,
      compliance, drawing impact.

    What is missing is the original session's pre-submission work: the
    requirements-research fan-out, and any drawing-digest vision pass. Those
    were live calls whose usage the pending state does not persist, so no
    recovery can reconstruct them — and the figure is an estimate from list
    prices, not the account's invoice. Attempts whose usage was never read
    (a repair batch still running) are counted and named, never priced.

    Never raises — a recovery that produced a report must not fail at the last
    step over telemetry.
    """
    try:
        diagnostics.finish()
        summary = diagnostics.summary()
        lines = cost_summary_lines(summary)
        if lines:
            _log(lines[0], level="info")
            for line in lines[1:]:
                _log(line.strip(), level="info")
            _log(
                "Not in the estimate: the original run's location research and "
                "any drawing digest (their usage is never saved).",
                level="info",
            )
        if json_path:
            path = Path(json_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(summary, indent=2, default=str), encoding="utf-8"
            )
            _log(f"Diagnostics saved: {path}", level="success")
    except Exception as exc:  # noqa: BLE001 — telemetry must not sink a recovery
        _log(f"Diagnostics not reported: {exc}", level="warning")


def _configure_utf8_stdio() -> None:
    """Best-effort: make ``stdout`` / ``stderr`` UTF-8 so the log glyphs never raise.

    ``_log`` prefixes lines with ``· ✓ ✗``; a legacy Windows console or a
    redirected pipe hands Python a cp1252 stream on which ``✓`` raises
    ``UnicodeEncodeError`` — in the middle of a recovery, after the batch was
    already collected. ``reconfigure`` exists only on ``TextIOWrapper``
    streams (never on a ``None`` stream), so anything else is left alone, and
    ``errors="replace"`` degrades an unencodable character to ``?`` rather
    than a traceback. Never raises.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if stream is None or reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a console we cannot reconfigure keeps its encoding
            pass


def _log(msg: str, *, level: str = "info") -> None:
    print(f" {_LEVEL_TAG.get(level, ' ')} {msg}", flush=True)


def _progress(_pct: float, msg: str) -> None:
    if msg:
        _log(msg, level="info")


def _is_not_found(exc: Exception) -> bool:
    """True for an Anthropic 'batch not found' (typo'd / expired id)."""
    if getattr(exc, "status_code", None) == 404:
        return True
    text = str(exc).lower()
    return "not_found" in text or "not found" in text


def _ensure_api_key(parser: argparse.ArgumentParser) -> None:
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return
    key = (load_api_key_from_file() or "").strip()
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
        return
    parser.error(
        "No Anthropic API key found. Set ANTHROPIC_API_KEY, or save a key via the "
        "desktop app first."
    )


def _discover_specs(input_dir: Path) -> list[str]:
    # Reuse the pipeline's discovery so behavior matches a normal run — notably
    # it excludes Word "~$" lock files, which a naive glob would pick up.
    return [str(p) for p in _get_spec_files(input_dir)]


def _note_ignored_module_flag(ns: argparse.Namespace, saved_module_id: str) -> None:
    """Saved state is authoritative for the module; say so if --module differs."""
    if ns.module and ns.module != saved_module_id:
        _log(
            f"Ignoring --module {ns.module}: the saved state records this batch "
            f"under module {saved_module_id}, which is authoritative.",
            level="warning",
        )


def _note_repair_batch(pending: PendingBatch) -> None:
    repair_id = getattr(pending, "repair_batch_id", None)
    if repair_id:
        _log(
            f"Saved state also records review repair batch {repair_id} for this "
            "batch (submitted by an earlier collect attempt). The collect step "
            "re-attaches to it instead of submitting a new repair batch.",
            level="info",
        )


def _saved_single_batch(
    pending: PendingBatch, ns: argparse.Namespace
):
    _note_ignored_module_flag(ns, pending.module_id)
    _note_repair_batch(pending)
    if ns.no_cross_check:
        pending.cross_check_enabled = False
    # The record's own inputs, should the settle step in ``main`` have to
    # write this run's record again (plan WP-14).
    ns.recovery_input_dir = pending.input_dir
    ns.recovery_files = list(pending.files)
    return pending.to_submission(log=_log, progress=_progress)


def _saved_program_run(
    pending: PendingProgramRun, ns: argparse.Namespace
) -> ProgramSubmission:
    n = len(pending.assignments)
    spec_word = "spec" if n == 1 else "specs"
    when = (
        f"submitted {datetime.fromtimestamp(pending.submitted_at):%Y-%m-%d %H:%M} local"
        if pending.submitted_at
        else "from a previous session"
    )
    children = ", ".join(
        f"{module_id}: {batch_id}" for module_id, batch_id in pending.batch_ids.items()
    )
    _log(
        f"Found saved program run {pending.program_id} ({n} {spec_word}, "
        f"{len(pending.partitions)} module batch(es), {when}): {children}",
        level="info",
    )
    if ns.module:
        _log(
            f"Ignoring --module {ns.module}: a saved program run records the "
            "module of every child batch.",
            level="warning",
        )
    if ns.no_cross_check:
        for child in pending.partitions.values():
            if isinstance(child, dict):
                child["cross_check_enabled"] = False
    return pending.to_submission(log=_log, progress=_progress)


def _build_submission(parser: argparse.ArgumentParser, ns: argparse.Namespace):
    """Return the submission for the requested recovery.

    A single-module ``BatchSubmission`` or a routed ``ProgramSubmission``.
    Whether the saved state is cleared afterwards is not decided here: the
    shared cleanup rule clears only a record that names this run (plan
    WP-14), so recovering one child of a saved program, or a batch by id
    while another run's record is on disk, leaves that record in place.
    """
    # Program-aware loader: a routed program run persists a manifest
    # (``record_type == "program"``) that the single-batch loader reads as
    # "no pending batch" — which is exactly the gap that used to make this
    # tool report "No saved pending batch found" after a hyperscale run
    # detached, even though the manifest was sitting on disk.
    pending = load_pending_run()

    if ns.batch_id:
        if isinstance(pending, PendingBatch) and pending.batch_id == ns.batch_id:
            _log(
                f"Using saved state for batch {ns.batch_id} (module {pending.module_id}).",
                level="info",
            )
            return _saved_single_batch(pending, ns)
        if isinstance(pending, PendingProgramRun):
            child = pending.child_batch(ns.batch_id)
            if child is not None:
                _log(
                    f"Batch {ns.batch_id} is the {child.module_id} partition of saved "
                    f"program run {pending.program_id}; recovering that module from "
                    "its saved state. The program manifest is kept so the other "
                    "module batches stay resumable.",
                    level="info",
                )
                # The manifest names every child, so the cleanup rule's
                # identity check keeps it for the siblings.
                return _saved_single_batch(child, ns)
        # No matching saved state: reconstruct from the remote batch directly.
        # A batch id carries no discipline, so the module must be explicit —
        # the thin reconstruction is what ``PendingBatch.to_submission`` guards
        # against degrading into the default module.
        if not ns.module:
            parser.error(
                f"--module is required to recover batch {ns.batch_id}: no saved "
                "state names its review module, and a batch id does not carry its "
                "discipline. Defaulting would collect, cross-check, and verify the "
                "batch under the wrong prompts and code basis (e.g. a data-center "
                "batch reviewed as California K-12). Pass --module with one of: "
                + ", ".join(sorted(AVAILABLE_MODULES))
                + "."
            )
        input_dir = None
        files = None
        if ns.input_dir:
            input_dir = str(Path(ns.input_dir).expanduser())
            files = _discover_specs(Path(input_dir))
            # Kept for the saved record ``main`` writes if the repair is
            # still outstanding after collection (plan WP-14).
            ns.recovery_input_dir = input_dir
            ns.recovery_files = list(files)
            if files:
                _log(
                    f"Found {len(files)} spec file(s) in {input_dir} — cross-check enabled.",
                    level="info",
                )
            else:
                _log(f"No .docx specs found in {input_dir}; recovering findings only.", level="warning")
        module = get_module(ns.module)
        # The thin reconstruction below reads the batch's *results* stream to
        # rebuild the request map — impossible while the batch is still
        # processing (no results_url yet, and the SDK raises). Poll it to
        # completion first; an already-ended batch clears the single status
        # check with no waiting.
        ensure_batch_ended(
            ns.batch_id,
            policy=DEFAULT_REVIEW_POLL_POLICY,
            log=_log,
            progress_cb=lambda s: _log(
                f"  {s.succeeded}/{s.total} done, {s.processing} processing, {s.errored} errored",
                level="info",
            ),
        )
        _log(
            f"Reconstructing batch {ns.batch_id} from the remote results under "
            f"module {module.module_id}...",
            level="step",
        )
        submission = thin_submission_from_batch_results(
            ns.batch_id,
            model=ns.model or REVIEW_MODEL_DEFAULT,
            input_dir=input_dir,
            files=files,
            cross_check_enabled=bool(files) and not ns.no_cross_check,
            module=module,
            log=_log,
            progress=_progress,
        )
        return submission

    if pending is None:
        parser.error(
            "No saved pending batch or program run found at "
            f"{pending_batch_path()}.\nPass --batch-id msgbatch_XXXX --module <id> "
            "to recover a specific batch by id."
        )
    if isinstance(pending, PendingProgramRun):
        return _saved_program_run(pending, ns)
    _log(
        f"Found saved batch {pending.batch_id} "
        f"({len(pending.files_reviewed)} spec(s), module {pending.module_id}, submitted "
        f"{datetime.fromtimestamp(pending.submitted_at):%Y-%m-%d %H:%M} local).",
        level="info",
    )
    return _saved_single_batch(pending, ns)


def _default_output_path(label: str) -> Path:
    short = label.replace("msgbatch_", "")[:24] or "batch"
    return Path.cwd() / f"spec-critic-recovered-{short}-{datetime.now():%Y-%m-%d}.docx"


def _poll_batches(batch_ids: dict[str, str]) -> dict[str, PollOutcome | Exception]:
    """Poll every batch in ``batch_ids`` (``{label: batch_id}``) to a terminal state.

    A single batch polls inline; a program's child batches poll concurrently
    (the GUI's shape) so the wall-clock wait is the slowest child, not the
    sum. One child's poll exception never stops the others.
    """

    def poll_one(label: str, batch_id: str) -> PollOutcome:
        prefix = f"[{label}] " if len(batch_ids) > 1 else ""
        return poll_batch_bounded(
            batch_id,
            policy=DEFAULT_REVIEW_POLL_POLICY,
            log=_log,
            progress_cb=lambda s: _log(
                f"  {prefix}{s.succeeded}/{s.total} done, {s.processing} processing, "
                f"{s.errored} errored",
                level="info",
            ),
        )

    outcomes: dict[str, PollOutcome | Exception] = {}
    if len(batch_ids) <= 1:
        for label, batch_id in batch_ids.items():
            outcomes[label] = poll_one(label, batch_id)
        return outcomes
    with ThreadPoolExecutor(max_workers=len(batch_ids)) as pool:
        futures = {
            pool.submit(poll_one, label, batch_id): label
            for label, batch_id in batch_ids.items()
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                outcomes[label] = future.result()
            except Exception as exc:  # noqa: BLE001 — keep the other batches resumable
                outcomes[label] = exc
    return outcomes


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Recover / finish a Spec Critic review batch the app stopped polling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--batch-id",
        help="Batch id to recover (default: read the saved pending-batch / program-run state).",
    )
    parser.add_argument(
        "--input-dir",
        help="Folder of the source .docx specs. Only needed with --batch-id when "
        "there is no saved state and you want cross-spec coordination in the report.",
    )
    parser.add_argument(
        "--model",
        help="Review model id used for the batch (affects re-extraction labeling only).",
    )
    parser.add_argument(
        "--module",
        default=None,
        choices=sorted(AVAILABLE_MODULES),
        help="Review module id the batch was submitted under. REQUIRED when "
        "recovering a bare --batch-id with no saved state (a batch id does not "
        "carry its discipline, and there is deliberately no default); ignored "
        "when saved state supplies the module.",
    )
    # Deprecated pre-module flag, kept as a hidden no-op alias so existing
    # recovery invocations (`--cycle 2025`) keep working. It was already a
    # no-op: every value resolved through the single-entry cycle registry to
    # the same default. The module's cycle is authoritative now.
    parser.add_argument(
        "--cycle",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-o", "--output",
        help="Path for the .docx report (default: spec-critic-recovered-<id>-<date>.docx in CWD).",
    )
    parser.add_argument(
        "--no-cross-check", action="store_true",
        help="Skip cross-spec coordination even if it was enabled for the batch.",
    )
    parser.add_argument(
        "--keep-state", action="store_true",
        help="Do not delete the saved pending-batch state on success.",
    )
    parser.add_argument(
        "--diagnostics-json",
        default=None,
        help=(
            "Write the run's diagnostics report (cost summary, per-phase "
            "telemetry, event timeline) to this path as JSON."
        ),
    )
    ns = parser.parse_args(argv)

    if ns.cycle is not None:
        _log(
            "--cycle is deprecated and ignored — the code cycle now comes from "
            "the review module (saved state, or --module for a bare batch id).",
            level="warning",
        )

    _ensure_api_key(parser)

    try:
        submission = _build_submission(parser, ns)
    except BatchNotFinishedError as exc:
        _log(str(exc), level="error")
        _log("Re-run this tool later to try again.", level="info")
        return 2
    except Exception as exc:  # noqa: BLE001 — turn API errors into a clean message
        if _is_not_found(exc):
            _log(
                f"Batch '{ns.batch_id}' was not found. Double-check the id (they "
                "look like msgbatch_…, case-sensitive); it may also have expired "
                "(results are kept ~29 days).",
                level="error",
            )
            return 2
        raise

    is_program = isinstance(submission, ProgramSubmission)
    if is_program:
        batch_ids = dict(submission.batch_ids)
        run_label = submission.program_id
    else:
        batch_ids = {submission.module_id: submission.job.batch_id}
        run_label = submission.job.batch_id

    ids_text = ", ".join(batch_ids.values())
    _log(f"Polling batch {ids_text} until it finishes (Ctrl-C to stop)...", level="step")
    outcomes = _poll_batches(batch_ids)
    unfinished: list[str] = []
    terminal_statuses: dict[str, str] = {}
    for label, outcome in outcomes.items():
        if isinstance(outcome, Exception):
            unfinished.append(f"{batch_ids[label]}: {outcome}")
            continue
        if not outcome.terminal:
            reason = outcome.detach_reason or outcome.poll_error or (
                "canceled" if outcome.user_canceled else "unknown"
            )
            unfinished.append(f"{batch_ids[label]}: {reason}")
            continue
        terminal_statuses[label] = outcome.terminal_status or "ended"
    if unfinished:
        _log(
            "Batch did not finish locally (" + "; ".join(unfinished) + "); it may "
            "still be running. Re-run this tool later to try again.",
            level="error",
        )
        return 2

    all_ended = all(status == "ended" for status in terminal_statuses.values())
    if not all_ended:
        # poll_batch_bounded reports `expired` / `failed` / `canceled` as
        # terminal too — those may lack usable results, so say so. Whether
        # the saved state is kept is the shared cleanup rule's call, from what
        # the collection actually returned (an all-failed run is kept).
        odd = ", ".join(
            f"{batch_ids[label]}: {status}"
            for label, status in terminal_statuses.items()
            if status != "ended"
        )
        _log(
            f"Batch ended with status '{odd}' — results may be incomplete or unavailable.",
            level="warning",
        )
    else:
        _log("Batch finished. Collecting results and finishing the run...", level="success")

    # A recovered run used to produce no cost summary at all: this driver
    # recorded nothing, so a recovery contributed no evidence to the
    # prompt-cache / research-cache decisions that depend on measured
    # repetition. The collection phases now price themselves like any other
    # run's.
    diagnostics = DiagnosticsReport()
    diagnostics.mode = "batch"
    diagnostics.module_id = getattr(submission, "module_id", "") or ""
    try:
        if is_program:
            result = collect_program_results(
                submission, log=_log, progress=_progress, diagnostics=diagnostics
            )
        else:
            result = run_batch_collection_headless(
                submission, log=_log, progress=_progress, diagnostics=diagnostics
            )
    except Exception as exc:  # noqa: BLE001 — keep state on any collection failure
        _log(f"Could not collect results for batch {ids_text}: {exc}", level="error")
        _log("Saved pending-batch state kept — re-run this tool to retry.", level="info")
        return 2

    output_path = Path(ns.output).expanduser() if ns.output else _default_output_path(run_label)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_report(result, output_path)
    _log(f"Report saved: {output_path}", level="success")
    try:
        sidecar = write_edit_instructions_sidecar(result, output_path)
        _log(f"Edit instructions saved: {sidecar}", level="success")
    except Exception as exc:  # noqa: BLE001 — sidecar is a nice-to-have
        _log(f"Edit-instructions sidecar not written: {exc}", level="warning")
    try:
        profile_path = write_requirements_profile_sidecar(result, output_path)
        if profile_path is not None:
            _log(f"Requirements profile saved: {profile_path}", level="success")
    except Exception as exc:  # noqa: BLE001 — sidecar is a nice-to-have
        _log(f"Requirements-profile sidecar not written: {exc}", level="warning")

    rv = result.review_result
    if rv is not None:
        _log(
            f"Findings: {rv.critical_count} critical, {rv.high_count} high, "
            f"{rv.medium_count} medium, {rv.gripe_count} gripes.",
            level="info",
        )
    failed_specs = list(result.failed_review_specs or [])
    if failed_specs:
        _log(
            f"{len(failed_specs)} spec(s) failed review and were not "
            "analyzed — see the report's Run Diagnostics banner.",
            level="warning",
        )
    module_errors = dict(getattr(result, "module_errors", None) or {})
    for module_id, message in module_errors.items():
        _log(f"Module {module_id} could not be collected: {message}", level="warning")
    notice = provisional_notice(result)
    if notice:
        _log(notice, level="warning")

    _report_collection_cost(diagnostics, ns.diagnostics_json)

    # The one keep-or-clear rule the GUI uses too: the saved record goes only
    # when the run is complete (no repair outstanding, every module collected,
    # not every spec failed) and only if it is this run's record — so
    # recovering one child of a saved program, or a batch by id while another
    # run's record is on disk, never deletes that record. A kept record is
    # made to name every repair batch this run created (plan WP-14): a run
    # recovered by batch id alone whose repair is still outstanding gets a
    # record when the state file is free, and a repair id whose first save
    # failed is re-stamped — otherwise the next run of this tool would pay
    # for a second repair.
    decision, _status = apply_saved_state_cleanup(
        result,
        submission=submission,
        keep_requested=ns.keep_state,
        input_dir=getattr(ns, "recovery_input_dir", "") or "",
        files=list(getattr(ns, "recovery_files", None) or []),
        log=_log,
    )
    return 0 if decision.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
