"""Persistent batch-state JSON storage.

Reads, writes, and deletes the saved batch resume state at
``app_paths.batch_state_path()``. Enforces a maximum state age and handles
corrupt/unsupported saved state gracefully.

This module knows nothing about GUI widgets — it returns plain dicts (or
``None``) and lets the controller decide how to display messages.

Chunk D7.1 — writes are atomic: the resume-state JSON is rendered to a
temp file in the same directory, flushed + fsynced, and then
``os.replace``\\d into place. A crash or partial write therefore cannot
leave a truncated resume-state file; the previously valid target is
preserved verbatim. The pattern mirrors ``verification_cache.save_to_disk``
and adds the fsync step required by the delta plan.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

from ..core.app_paths import (
    BATCH_STATE_MAX_AGE_HOURS,
    BATCH_STATE_WARNING_AGE_HOURS,
    batch_state_path,
)


def _batch_state_path():
    """Indirection so tests can monkeypatch the path location.

    The default delegates to ``app_paths.batch_state_path()``; tests patch
    this function directly to redirect persistence to a tmp location.
    """
    return batch_state_path()

from ..orchestration.resume_state import deserialize_resume_state


def save_batch_state(state: dict) -> None:
    """Atomically persist the resume state.

    Chunk D7.1: write a temp file in the same directory, flush + fsync,
    then ``os.replace`` to the target. A crash mid-write leaves the
    previous valid target untouched and the half-written temp file is
    removed on failure. Exceptions from any step are caught and logged
    (the same swallow-and-warn behavior the pre-D7.1 implementation
    had) because resume-state persistence is best-effort: a save
    failure should not crash an in-flight batch run.
    """
    target = _batch_state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2)
    except Exception as e:
        print(f"[SpecCritic] Warning: Could not save batch state: {e}")
        return

    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".batch_state.",
            suffix=".tmp",
            dir=str(target.parent),
        )
    except Exception as e:
        print(f"[SpecCritic] Warning: Could not save batch state: {e}")
        return

    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, str(target))
    except Exception as e:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        print(f"[SpecCritic] Warning: Could not save batch state: {e}")


def load_batch_state() -> Optional[dict]:
    path = _batch_state_path()
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        delete_batch_state()
        return None
    try:
        saved_at = datetime.fromisoformat(state["saved_at"])
        age_hours = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600
        if age_hours > BATCH_STATE_MAX_AGE_HOURS:
            delete_batch_state()
            return None
    except Exception:
        delete_batch_state()
        return None
    try:
        restored = deserialize_resume_state(state)
        submission = restored["submission"]
        if not isinstance(submission.job.batch_id, str) or not submission.job.batch_id.startswith("msgbatch_"):
            delete_batch_state()
            return None
        return restored
    except (KeyError, TypeError, ValueError):
        delete_batch_state()
        return None


def delete_batch_state() -> None:
    try:
        path = _batch_state_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass


def batch_state_nearing_expiry(created_at: float) -> bool:
    """Whether a batch submitted at ``created_at`` is close to local expiry.

    Chunk 1: returns ``True`` once the submission age crosses
    :data:`BATCH_STATE_WARNING_AGE_HOURS` (25 days) so the GUI can warn the
    user before the 28-day local cutoff drops the state. ``created_at`` is
    a unix timestamp consistent with :attr:`BatchJob.created_at`.
    """
    try:
        age_seconds = datetime.now(timezone.utc).timestamp() - float(created_at)
    except (TypeError, ValueError):
        return False
    age_hours = age_seconds / 3600
    return age_hours >= BATCH_STATE_WARNING_AGE_HOURS
