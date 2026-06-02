#!/usr/bin/env python3
"""Recover a Spec Critic review batch the desktop app stopped polling.

A submitted review batch runs on Anthropic's Message Batches API for up to 24h
and its results stay retrievable for ~29 days. If the app's poller detached
(closed app, lost network, the no-progress / max-elapsed timeout) the batch
kept running remotely — this tool reconnects to it and finishes the run
(poll -> collect -> verify -> cross-check -> Word report + edit sidecar) without
re-submitting or re-paying for the review.

USAGE

  # Resume the most recent batch the app saved when it submitted (the common
  # case going forward — full recovery, including cross-spec coordination):
  python scripts/recover_batch.py

  # Recover a batch by id when there is no saved state (e.g. a batch submitted
  # before this feature existed). Findings-only unless you also point it at the
  # source folder so the specs can be re-read for cross-check:
  python scripts/recover_batch.py --batch-id msgbatch_XXXX
  python scripts/recover_batch.py --batch-id msgbatch_XXXX --input-dir /path/to/specs

  # Choose where the report goes:
  python scripts/recover_batch.py -o ~/Desktop/recovered-report.docx

The Anthropic API key is read from ANTHROPIC_API_KEY, or from the key file the
desktop app saves (so if you have used the app on this machine, no flag needed).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Make ``src`` importable when this file is run directly from the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.batch.batch_runtime import DEFAULT_REVIEW_POLL_POLICY, poll_batch_bounded  # noqa: E402
from src.core.api_config import REVIEW_MODEL_DEFAULT  # noqa: E402
from src.core.api_key_store import load_api_key_from_file  # noqa: E402
from src.core.code_cycles import AVAILABLE_CYCLES, DEFAULT_CYCLE  # noqa: E402
from src.orchestration.batch_resume import (  # noqa: E402
    clear_pending_batch,
    load_pending_batch,
    pending_batch_path,
    thin_submission_from_batch_results,
)
from src.orchestration.pipeline import _get_spec_files, run_batch_collection_headless  # noqa: E402
from src.output.edit_sidecar import write_edit_instructions_sidecar  # noqa: E402
from src.output.report_exporter import export_report  # noqa: E402

_LEVEL_TAG = {"step": "·", "info": " ", "success": "✓", "warning": "!", "error": "✗"}


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


def _build_submission(parser: argparse.ArgumentParser, ns: argparse.Namespace):
    """Return ``(submission, had_saved_state)`` for the requested recovery."""
    if ns.batch_id:
        pending = load_pending_batch()
        if pending is not None and pending.batch_id == ns.batch_id:
            _log(f"Using saved state for batch {ns.batch_id}.", level="info")
            return pending.to_submission(log=_log, progress=_progress), True
        # No matching saved state: reconstruct from the remote batch directly.
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
        cycle = AVAILABLE_CYCLES.get(ns.cycle, DEFAULT_CYCLE)
        _log(f"Reconstructing batch {ns.batch_id} from the remote results...", level="step")
        submission = thin_submission_from_batch_results(
            ns.batch_id,
            model=ns.model or REVIEW_MODEL_DEFAULT,
            input_dir=input_dir,
            files=files,
            cross_check_enabled=bool(files) and not ns.no_cross_check,
            cycle=cycle,
            log=_log,
            progress=_progress,
        )
        return submission, False

    pending = load_pending_batch()
    if pending is None:
        parser.error(
            "No saved pending batch found at "
            f"{pending_batch_path()}.\nPass --batch-id msgbatch_XXXX to recover a "
            "specific batch by id."
        )
    _log(
        f"Found saved batch {pending.batch_id} "
        f"({len(pending.files_reviewed)} spec(s), submitted "
        f"{datetime.fromtimestamp(pending.submitted_at):%Y-%m-%d %H:%M} local).",
        level="info",
    )
    if ns.no_cross_check:
        pending.cross_check_enabled = False
    return pending.to_submission(log=_log, progress=_progress), True


def _default_output_path(batch_id: str) -> Path:
    short = batch_id.replace("msgbatch_", "")[:12] or "batch"
    return Path.cwd() / f"spec-critic-recovered-{short}-{datetime.now():%Y-%m-%d}.docx"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover / finish a Spec Critic review batch the app stopped polling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--batch-id",
        help="Batch id to recover (default: read the saved pending-batch state).",
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
        "--cycle",
        default=DEFAULT_CYCLE.label,
        help=f"Code cycle label (default: {DEFAULT_CYCLE.label}).",
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

    _ensure_api_key(parser)

    try:
        submission, had_saved_state = _build_submission(parser, ns)
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
    batch_id = submission.job.batch_id

    _log(f"Polling batch {batch_id} until it finishes (Ctrl-C to stop)...", level="step")
    outcome = poll_batch_bounded(
        batch_id,
        policy=DEFAULT_REVIEW_POLL_POLICY,
        log=_log,
        progress_cb=lambda s: _log(
            f"  {s.succeeded}/{s.total} done, {s.processing} processing, {s.errored} errored",
            level="info",
        ),
    )
    if not outcome.terminal:
        reason = outcome.detach_reason or outcome.poll_error or (
            "canceled" if outcome.user_canceled else "unknown"
        )
        _log(
            f"Batch did not finish locally ({reason}); it may still be running. "
            "Re-run this tool later to try again.",
            level="error",
        )
        return 2

    terminal_status = outcome.terminal_status or "ended"
    if terminal_status != "ended":
        # poll_batch_bounded reports `expired` / `failed` / `canceled` as
        # terminal too — those won't have usable results, so flag it and avoid
        # silently exporting an empty report as if the run succeeded.
        _log(
            f"Batch ended with status '{terminal_status}' — results may be "
            "incomplete or unavailable.",
            level="warning",
        )
    else:
        _log("Batch finished. Collecting results and finishing the run...", level="success")

    try:
        result = run_batch_collection_headless(submission, log=_log, progress=_progress)
    except Exception as exc:  # noqa: BLE001 — keep state on any collection failure
        _log(f"Could not collect results for batch {batch_id}: {exc}", level="error")
        _log("Saved pending-batch state kept — re-run this tool to retry.", level="info")
        return 2

    output_path = Path(ns.output).expanduser() if ns.output else _default_output_path(batch_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_report(result, output_path)
    _log(f"Report saved: {output_path}", level="success")
    try:
        sidecar = write_edit_instructions_sidecar(result, output_path)
        _log(f"Edit instructions saved: {sidecar}", level="success")
    except Exception as exc:  # noqa: BLE001 — sidecar is a nice-to-have
        _log(f"Edit-instructions sidecar not written: {exc}", level="warning")

    rv = result.review_result
    if rv is not None:
        _log(
            f"Findings: {rv.critical_count} critical, {rv.high_count} high, "
            f"{rv.medium_count} medium, {rv.gripe_count} gripes.",
            level="info",
        )
    if result.failed_review_specs:
        _log(
            f"{len(result.failed_review_specs)} spec(s) failed review and were not "
            "analyzed — see the report's Run Diagnostics banner.",
            level="warning",
        )

    # Only drop saved state when the recovery actually produced results — an
    # expired / all-failed batch keeps its state so the user can retry rather
    # than losing the only handle to it.
    n_specs = len(submission.review_request_ids)
    recovered_ok = terminal_status == "ended" and (n_specs == 0 or len(result.failed_review_specs) < n_specs)
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
