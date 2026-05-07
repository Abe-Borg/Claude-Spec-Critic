"""Persistent batch-state JSON storage.

Reads, writes, and deletes the saved batch resume state at
``app_paths.batch_state_path()``. Enforces a maximum state age, handles
corrupt/unsupported saved state gracefully, and preserves backward
compatibility with the pre-resume-state (v1) payload shape used by older
installed versions.

This module knows nothing about GUI widgets — it returns plain dicts (or
``None``) and lets the controller decide how to display messages.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .app_paths import BATCH_STATE_MAX_AGE_HOURS, batch_state_path
from .batch import BatchJob


def _batch_state_path():
    """Indirection so tests can monkeypatch the path location.

    The default delegates to ``app_paths.batch_state_path()``; tests patch
    this function directly to redirect persistence to a tmp location.
    """
    return batch_state_path()

from .code_cycles import DEFAULT_CYCLE
from .extractor import ExtractedSpec
from .pipeline import BatchSubmission
from .resume_state import PHASE_REVIEW_POLL, deserialize_resume_state
from .reviewer import MODEL_OPUS_47


def save_batch_state(state: dict) -> None:
    try:
        _batch_state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
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
        # Intentionally retained for upgrade continuity with older installed versions
        # that persisted pre-resume-state (v1) payloads.
        try:
            batch_id = state.get("batch_id", "")
            if not isinstance(batch_id, str) or not batch_id.startswith("msgbatch_"):
                delete_batch_state()
                return None
            legacy_submission = BatchSubmission(
                job=BatchJob(
                    batch_id=batch_id,
                    job_type=state.get("job_type", "review"),
                    request_map=state["request_map"],
                    created_at=state["created_at"],
                ),
                files_reviewed=state.get("files_reviewed", []),
                review_request_ids=state.get("review_request_ids", []),
                leed_alerts=state.get("leed_alerts", []),
                placeholder_alerts=state.get("placeholder_alerts", []),
                model=MODEL_OPUS_47,
                project_context=state.get("project_context", ""),
                cycle_label=state.get("code_cycle", DEFAULT_CYCLE.label),
                cross_check_enabled=state.get("cross_check_enabled", False),
                prepared_specs=[ExtractedSpec(**s) for s in (state.get("prepared_specs") or [])] if state.get("prepared_specs") else None,
            )
            phase = state.get("phase", "review")
            phase_map = {"review": PHASE_REVIEW_POLL}
            migrated_phase = phase_map.get(phase, phase)
            return {"phase": migrated_phase, "submission": legacy_submission, "resume_flags": {}}
        except Exception:
            delete_batch_state()
            return None


def delete_batch_state() -> None:
    try:
        path = _batch_state_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass
