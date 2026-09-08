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
    clear_pending_batch,
    load_pending_run,
    pending_batch_path,
    thin_submission_from_batch_results,
)
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
    """Return ``(submission, had_saved_state)`` for the requested recovery.

    ``submission`` is a single-module ``BatchSubmission`` or a routed
    ``ProgramSubmission``; ``had_saved_state`` says whether the saved
    pending-state file drove the recovery (and may be cleared on success).
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
            return _saved_single_batch(pending, ns), True
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
                # Not "had_saved_state": clearing on success would delete the
                # whole manifest, stranding the sibling batches.
                return _saved_single_batch(child, ns), False
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
        return submission, False

    if pending is None:
        parser.error(
            "No saved pending batch or program run found at "
            f"{pending_batch_path()}.\nPass --batch-id msgbatch_XXXX --module <id> "
            "to recover a specific batch by id."
        )
    if isinstance(pending, PendingProgramRun):
        return _saved_program_run(pending, ns), True
    _log(
        f"Found saved batch {pending.batch_id} "
        f"({len(pending.files_reviewed)} spec(s), module {pending.module_id}, submitted "
        f"{datetime.fromtimestamp(pending.submitted_at):%Y-%m-%d %H:%M} local).",
        level="info",
    )
    return _saved_single_batch(pending, ns), True


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
    ns = parser.parse_args(argv)

    if ns.cycle is not None:
        _log(
            "--cycle is deprecated and ignored — the code cycle now comes from "
            "the review module (saved state, or --module for a bare batch id).",
            level="warning",
        )

    _ensure_api_key(parser)

    try:
        submission, had_saved_state = _build_submission(parser, ns)
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
        n_specs = submission.routed_request_count
    else:
        batch_ids = {submission.module_id: submission.job.batch_id}
        run_label = submission.job.batch_id
        n_specs = len(submission.review_request_ids)

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
        # terminal too — those won't have usable results, so flag it and avoid
        # silently exporting an empty report as if the run succeeded.
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

    try:
        if is_program:
            result = collect_program_results(submission, log=_log, progress=_progress)
        else:
            result = run_batch_collection_headless(submission, log=_log, progress=_progress)
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

    # Only drop saved state when the recovery actually produced results — an
    # expired / all-failed batch (or a program with an uncollected module)
    # keeps its state so the user can retry rather than losing the only
    # handle to it.
    recovered_ok = (
        all_ended
        and not module_errors
        and (n_specs == 0 or len(failed_specs) < n_specs)
    )
    if had_saved_state and not ns.keep_state:
        if recovered_ok:
            clear_pending_batch()
            _log("Cleared saved pending-batch state.", level="info")
        else:
            _log(
                "Kept saved pending-batch state — recovery produced no usable "
                "findings.",
                level="warning",
            )
    return 0 if recovered_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
