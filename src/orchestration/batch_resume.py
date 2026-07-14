"""Persisted pending-batch state for resume / recovery.

A submitted review batch runs on Anthropic's Message Batches API for up to 24h,
and its results stay retrievable for ~29 days. If the local poller stops early
(closed app, lost network, the no-progress / max-elapsed detach) the batch keeps
running remotely — the work and the API spend are not lost, only stranded. This
module persists the small amount of state needed to *reconnect* to that batch
and finish the run (re-poll → collect → verify → cross-check → report) without
re-submitting and re-paying for the review.

Two design choices make recovery robust:

- The persisted state carries the batch's ``request_map`` verbatim, which fully
  decouples review-result collection from the local files — a detached batch's
  findings come back even if every source file was moved or deleted.
- Spec bodies are never serialized. The inputs needed to re-extract them
  (``input_dir`` / ``files``) are persisted instead; re-extraction is
  deterministic and content-cached, so it reproduces exactly what the model
  reviewed and only re-enables the file-dependent stages (cross-check, repair,
  extraction warnings).

Consumers:

- the GUI startup resume prompt (``gui`` + ``batch_controller``), and
- the standalone ``scripts/recover_batch.py`` recovery tool.

The on-disk file lives next to the other Spec Critic state (``~/.spec_critic/``,
matching the verification cache), overridable via
``SPEC_CRITIC_PENDING_BATCH_PATH``.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.api_config import REVIEW_MODEL_DEFAULT
from ..core.code_cycles import DEFAULT_CYCLE
from ..modules import DEFAULT_MODULE, ReviewModule, get_module
from .pipeline import (
    BatchSubmission,
    LogFn,
    ProgressFn,
    _noop_log,
    _noop_progress,
    reconstruct_batch_submission,
)

# Bump when the persisted shape changes incompatibly; a mismatched/older file is
# ignored on load (treated as "no pending batch") rather than mis-parsed.
_SCHEMA_VERSION = 1


def pending_batch_path() -> Path:
    """On-disk location of the pending-batch state file.

    Overridable via ``SPEC_CRITIC_PENDING_BATCH_PATH`` (``~`` and ``$VAR``
    expanded). Default ``~/.spec_critic/pending_batch.json``.
    """
    override = os.environ.get("SPEC_CRITIC_PENDING_BATCH_PATH")
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    return Path.home() / ".spec_critic" / "pending_batch.json"


@dataclass
class PendingBatch:
    """Everything needed to reconnect to an in-flight / detached review batch."""

    batch_id: str
    model: str
    request_map: dict[str, Any] = field(default_factory=dict)
    review_request_ids: list[str] = field(default_factory=list)
    files_reviewed: list[str] = field(default_factory=list)
    input_dir: str = ""
    files: list[str] = field(default_factory=list)
    cycle_label: str = DEFAULT_CYCLE.label
    # Registry id of the module the batch was submitted under. Persisted so a
    # resumed run reconstructs the SAME module (and thus the same cycle /
    # prompts) it was submitted with. Legacy state files predate this field
    # and load with the default — which resolves to the California module,
    # the only configuration those files could have been written by. No
    # schema bump: the loader is defensive and old readers ignore the key.
    module_id: str = DEFAULT_MODULE.module_id
    # Per-run project identity (city/state/country/client) as a serialized
    # dict, or ``None`` for a profile-less run. Additive like ``module_id``:
    # legacy state files predate the key and load as ``None`` (profile-less,
    # a valid run), so NO schema bump. The profile *text* is separately inside
    # the persisted ``project_context`` once WS-3 splices it, but the typed
    # dict is what a resumed run reconstructs the routing/report inputs from.
    project_profile: dict | None = None
    # Serialized ``RequirementsProfile`` (WS-3 research output), or ``None``
    # when the phase didn't run. Additive — same posture as
    # ``project_profile`` (defensive load, NO schema bump). Research is never
    # re-run on resume: its rendered text is already inside
    # ``project_context``; this dict restores the structured items for the
    # compliance pass / report surfaces (WS-4).
    requirements_profile: dict | None = None
    project_context: str = ""
    cross_check_enabled: bool = False
    submitted_at: float = 0.0
    run_id: str = ""
    app_version: str = ""
    schema_version: int = _SCHEMA_VERSION

    @classmethod
    def from_submission(
        cls,
        submission: BatchSubmission,
        *,
        input_dir: Any = "",
        files: list | None = None,
        run_id: str = "",
        app_version: str = "",
    ) -> "PendingBatch":
        return cls(
            batch_id=submission.job.batch_id,
            model=submission.model,
            request_map=dict(submission.job.request_map or {}),
            review_request_ids=list(submission.review_request_ids),
            files_reviewed=list(submission.files_reviewed),
            input_dir=str(input_dir) if input_dir else "",
            files=[str(f) for f in (files or [])],
            cycle_label=submission.cycle_label,
            module_id=getattr(submission, "module_id", "") or DEFAULT_MODULE.module_id,
            project_profile=getattr(submission, "project_profile", None),
            requirements_profile=getattr(submission, "requirements_profile", None),
            project_context=submission.project_context,
            cross_check_enabled=submission.cross_check_enabled,
            submitted_at=float(submission.job.created_at or time.time()),
            run_id=run_id,
            app_version=app_version,
        )

    def to_submission(
        self, *, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress
    ) -> BatchSubmission:
        module = get_module(self.module_id)
        return reconstruct_batch_submission(
            batch_id=self.batch_id,
            request_map=self.request_map,
            review_request_ids=self.review_request_ids,
            files_reviewed=self.files_reviewed,
            input_dir=self.input_dir or None,
            files=self.files or None,
            model=self.model,
            project_context=self.project_context,
            module=module,
            cross_check_enabled=self.cross_check_enabled,
            created_at=self.submitted_at,
            project_profile=self.project_profile,
            requirements_profile=self.requirements_profile,
            log=log,
            progress=progress,
        )


def save_pending_batch(pending: PendingBatch, *, path: Path | None = None) -> None:
    """Atomically persist ``pending``. Best-effort: never raise on I/O error."""
    target = path or pending_batch_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(pending), indent=2), encoding="utf-8")
        tmp.replace(target)  # atomic rename so a crash mid-write can't truncate
    except OSError:
        pass


def load_pending_batch(*, path: Path | None = None) -> PendingBatch | None:
    """Load persisted pending-batch state, or ``None`` when absent/unusable.

    Defensive on every axis — a missing file, malformed JSON, wrong schema
    version, or a record without a usable ``batch_id`` all read as "no pending
    batch" rather than raising into the startup path.
    """
    target = path or pending_batch_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        return None

    batch_id = data.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        return None

    def _str(key: str, default: str = "") -> str:
        v = data.get(key, default)
        return v if isinstance(v, str) else default

    def _list(key: str) -> list:
        v = data.get(key)
        return list(v) if isinstance(v, list) else []

    submitted = data.get("submitted_at")
    submitted_at = float(submitted) if isinstance(submitted, (int, float)) else 0.0
    request_map = data.get("request_map")
    # Additive, defensive: absent (legacy) or non-dict reads as None (a valid
    # profile-less run). No schema bump.
    profile = data.get("project_profile")
    requirements = data.get("requirements_profile")
    return PendingBatch(
        batch_id=batch_id,
        model=_str("model") or REVIEW_MODEL_DEFAULT,
        request_map=request_map if isinstance(request_map, dict) else {},
        review_request_ids=_list("review_request_ids"),
        files_reviewed=_list("files_reviewed"),
        input_dir=_str("input_dir"),
        files=_list("files"),
        cycle_label=_str("cycle_label", DEFAULT_CYCLE.label) or DEFAULT_CYCLE.label,
        module_id=_str("module_id", DEFAULT_MODULE.module_id) or DEFAULT_MODULE.module_id,
        project_profile=profile if isinstance(profile, dict) else None,
        requirements_profile=requirements if isinstance(requirements, dict) else None,
        project_context=_str("project_context"),
        cross_check_enabled=bool(data.get("cross_check_enabled", False)),
        submitted_at=submitted_at,
        run_id=_str("run_id"),
        app_version=_str("app_version"),
    )


def clear_pending_batch(*, path: Path | None = None) -> None:
    """Remove the pending-batch state file if present. Never raises."""
    target = path or pending_batch_path()
    try:
        target.unlink()
    except (FileNotFoundError, OSError):
        pass


def _parse_review_custom_id(custom_id: str) -> tuple[str, int] | None:
    """Recover ``(stem, index)`` from a review batch ``custom_id``.

    Submitted ids have the shape ``review__<sanitized-stem>__<index>`` (see
    ``batch.submit_review_batch``). The sanitized stem is lossy, so it is used
    only as a best-effort label — each :class:`Finding` already carries its own
    ``fileName`` from the model output. Returns ``None`` for an unrecognized id.
    """
    if not isinstance(custom_id, str) or not custom_id.startswith("review__"):
        return None
    body = custom_id[len("review__"):]
    stem, sep, idx_str = body.rpartition("__")
    if not sep:
        return None
    try:
        return stem, int(idx_str)
    except ValueError:
        return None


def thin_submission_from_batch_results(
    batch_id: str,
    *,
    model: str,
    input_dir: str | None = None,
    files: list[str] | None = None,
    cross_check_enabled: bool = False,
    project_context: str = "",
    module: ReviewModule = DEFAULT_MODULE,
    project_profile: dict | None = None,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> BatchSubmission:
    """Reconstruct a :class:`BatchSubmission` from a bare batch id, no saved state.

    Lists the batch's results to recover the ``request_map`` / order directly
    from the remote batch (their ``custom_id``s), so a batch submitted *before*
    resume-state persistence existed — or one whose state file was lost — can
    still be recovered. The batch must have ended (poll first).

    Findings come back regardless of local files. When ``input_dir`` + ``files``
    are supplied they are re-extracted to additionally enable cross-spec
    coordination; otherwise it is a findings-only recovery.
    """
    from ..batch.batch import _collect_batch_results_with_retry, _sanitize_custom_id

    indexed: list[tuple[int, str, str]] = []  # (index, custom_id, stem)
    for result in _collect_batch_results_with_retry(batch_id).values():
        parsed = _parse_review_custom_id(getattr(result, "custom_id", ""))
        if parsed is None:
            continue
        stem, idx = parsed
        indexed.append((idx, result.custom_id, stem))
    indexed.sort(key=lambda t: t[0])

    # When real source files are supplied, recover each item's REAL filename by
    # re-applying the same sanitizer the submit path used to mint the custom id
    # (``review__<sanitize(stem)>__<idx>``). Storing the real filename (not the
    # lossy sanitized stem) keeps request_map['filename'] equal to the
    # re-extracted spec's filename, so the failed-spec cross-check exclusion and
    # the filename-keyed review-repair fallback both match. Falls back to the
    # stem when no supplied file sanitizes to it (findings-only recovery).
    real_by_key: dict[str, str] = {}
    for f in files or []:
        name = Path(f).name
        real_by_key.setdefault(_sanitize_custom_id(name), name)

    request_map: dict[str, Any] = {}
    review_request_ids: list[str] = []
    resolved_names: list[str] = []
    for idx, custom_id, stem in indexed:
        name = real_by_key.get(stem, stem)
        request_map[custom_id] = {"filename": name, "index": idx, "type": "review"}
        review_request_ids.append(custom_id)
        resolved_names.append(name)

    if not request_map:
        log(
            f"No review items found in batch {batch_id}. It may not be a review "
            "batch, may have expired, or may not have any results yet.",
            level="warning",
        )

    files_reviewed = resolved_names

    return reconstruct_batch_submission(
        batch_id=batch_id,
        request_map=request_map,
        review_request_ids=review_request_ids,
        files_reviewed=files_reviewed,
        input_dir=input_dir,
        files=files,
        model=model,
        project_context=project_context,
        module=module,
        cross_check_enabled=cross_check_enabled and bool(files),
        created_at=time.time(),
        project_profile=project_profile,
        log=log,
        progress=progress,
    )
