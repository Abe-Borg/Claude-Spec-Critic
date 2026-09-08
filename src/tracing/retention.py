"""Trace-run retention: the single prune implementation.

Both callers route through :func:`select_prune_candidates` and
:func:`delete_run_dirs` so they cannot drift:

- ``python -m src.tracing prune`` (``cli.cmd_prune``) — operator-driven, one
  knob at a time (``--keep-last N`` or ``--older-than DURATION``), with a
  confirmation prompt.
- :func:`apply_startup_retention` — automatic, invoked from
  ``session.start_run_recorder`` right after a new run's directory has been
  created. It applies **both** knobs from the environment
  (``SPEC_CRITIC_TRACE_RETENTION_DAYS`` / ``SPEC_CRITIC_TRACE_MAX_RUNS``,
  ``0`` disables each), never deletes the run being started, and never
  raises — a retention hiccup must not cost the operator a review run.

Cost model: one directory listing of the trace root plus one small
``run.json`` read per run directory (to order runs by ``started_at``). With
the default 50-run ceiling that is a few dozen tiny reads, so it runs
synchronously on the submit path.

A directory without a ``run.json`` is never treated as a run and is never
deleted — retention only ever removes what the recorder wrote.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .config import default_trace_root, trace_max_runs, trace_retention_days
from .recorder import FILE_RUN_META


_log = logging.getLogger(__name__)


# ---- run-directory discovery -------------------------------------------
def load_run_meta(run_dir: Path) -> dict | None:
    """Parse ``run.json`` for one run directory; ``None`` when absent/broken."""
    run_path = Path(run_dir) / FILE_RUN_META
    if not run_path.exists():
        return None
    try:
        return json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def iter_run_dirs(root: Path) -> Iterator[Path]:
    """Yield every run directory under ``root`` (has a ``run.json``), sorted by name."""
    root = Path(root)
    if not root.exists():
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / FILE_RUN_META).exists():
            yield child


def run_started_at(run_dir: Path) -> float:
    """``started_at`` from ``run.json``, falling back to the directory mtime."""
    meta = load_run_meta(run_dir) or {}
    started = meta.get("started_at")
    if isinstance(started, (int, float)) and not isinstance(started, bool) and started:
        return float(started)
    try:
        return Path(run_dir).stat().st_mtime
    except OSError:
        return 0.0


def _dir_key(path: Path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path).absolute())


# ---- selection ---------------------------------------------------------
@dataclass(frozen=True)
class PruneSelection:
    """Outcome of :func:`select_prune_candidates`.

    ``kept`` and ``candidates`` are both newest-first. ``protected`` lists the
    runs a knob *would* have deleted but which were shielded via ``protect``
    (the run being started) — surfaced so callers can log/inspect it.
    """

    kept: tuple[Path, ...]
    candidates: tuple[Path, ...]
    protected: tuple[Path, ...]


def select_prune_candidates(
    root: Path,
    *,
    keep_last: int | None = None,
    older_than_seconds: float | None = None,
    protect: Iterable[Path] = (),
    now: float | None = None,
) -> PruneSelection:
    """Pick the run directories a prune should delete (nothing is deleted here).

    - ``keep_last=N`` keeps the ``N`` most recent runs (``0`` keeps none;
      ``None`` disables the count limit).
    - ``older_than_seconds=S`` deletes runs whose ``started_at`` predates
      ``now - S`` (``None`` disables the age limit).
    - Both limits may be active at once; the result is their union.
    - Any run in ``protect`` (matched by resolved path or by directory name)
      is never a candidate, whatever the limits say.

    Ordering is newest-first by ``started_at`` (stable on the name-sorted
    listing), which is what the CLI has always printed.
    """
    runs = list(iter_run_dirs(root))
    started = {d: run_started_at(d) for d in runs}
    runs.sort(key=lambda d: started[d], reverse=True)

    doomed: set[Path] = set()
    if keep_last is not None:
        doomed.update(runs[max(int(keep_last), 0):])
    if older_than_seconds is not None:
        cutoff = (time.time() if now is None else now) - float(older_than_seconds)
        doomed.update(d for d in runs if started[d] < cutoff)

    protected_keys = {_dir_key(p) for p in protect}
    protected_names = {Path(p).name for p in protect}
    candidates: list[Path] = []
    shielded: list[Path] = []
    for d in runs:
        if d not in doomed:
            continue
        if d.name in protected_names or _dir_key(d) in protected_keys:
            shielded.append(d)
        else:
            candidates.append(d)
    kept = [d for d in runs if d not in candidates]
    return PruneSelection(kept=tuple(kept), candidates=tuple(candidates), protected=tuple(shielded))


# ---- deletion ----------------------------------------------------------
def delete_run_dirs(dirs: Iterable[Path]) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Delete each directory; never raises.

    Returns ``(deleted, failed)`` where ``failed`` pairs each directory that
    could not be removed with a short reason. A failure on one directory
    does not stop the others from being attempted; a partially removed
    directory is retried naturally on the next prune.
    """
    deleted: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for d in dirs:
        try:
            shutil.rmtree(d)
        except Exception as exc:  # noqa: BLE001 — retention must never raise
            failed.append((Path(d), f"{type(exc).__name__}: {exc}"))
        else:
            deleted.append(Path(d))
    return deleted, failed


@dataclass(frozen=True)
class PruneResult:
    selection: PruneSelection
    deleted: tuple[Path, ...]
    failed: tuple[tuple[Path, str], ...]


def prune_trace_runs(
    root: Path,
    *,
    keep_last: int | None = None,
    older_than_seconds: float | None = None,
    protect: Iterable[Path] = (),
    now: float | None = None,
) -> PruneResult:
    """Select + delete in one step (the non-interactive composition)."""
    selection = select_prune_candidates(
        root,
        keep_last=keep_last,
        older_than_seconds=older_than_seconds,
        protect=protect,
        now=now,
    )
    deleted, failed = delete_run_dirs(selection.candidates)
    return PruneResult(selection=selection, deleted=tuple(deleted), failed=tuple(failed))


# ---- startup policy ----------------------------------------------------
def apply_startup_retention(
    *,
    current_run_dir: Path,
    root: Path | None = None,
    now: float | None = None,
) -> PruneResult | None:
    """Apply the env-configured retention policy when a run starts.

    Reads ``SPEC_CRITIC_TRACE_RETENTION_DAYS`` (default 30) and
    ``SPEC_CRITIC_TRACE_MAX_RUNS`` (default 50) — malformed values fall back
    to the defaults, ``0`` disables the matching knob, both ``0`` returns
    ``None`` without touching the disk. ``current_run_dir`` is always
    protected. Failures are logged and swallowed: this function never
    raises.
    """
    try:
        days = trace_retention_days()
        max_runs = trace_max_runs()
        if days == 0 and max_runs == 0:
            _log.debug("Trace retention disabled (both knobs are 0)")
            return None
        root_path = Path(root) if root is not None else default_trace_root()
        result = prune_trace_runs(
            root_path,
            keep_last=max_runs if max_runs > 0 else None,
            older_than_seconds=days * 86400.0 if days > 0 else None,
            protect=(Path(current_run_dir),),
            now=now,
        )
        if result.deleted:
            _log.info(
                "Trace retention: deleted %d old trace run(s) under %s "
                "(retention_days=%d, max_runs=%d)",
                len(result.deleted), root_path, days, max_runs,
            )
        for d, reason in result.failed:
            _log.warning("Trace retention: could not delete %s: %s", d, reason)
        return result
    except Exception as exc:  # noqa: BLE001 — retention must never raise
        _log.warning("Trace retention skipped: %s", exc)
        return None
