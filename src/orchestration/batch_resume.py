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
from ..modules import DEFAULT_MODULE, ReviewModule, require_module
from ..programs import SpecAssignment, require_program
from .program_pipeline import ProgramSubmission
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
_SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1


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
    # Wall-clock start of the whole run (pre-research/extraction; D2).
    # Additive — legacy state files load the 0.0 default, and the elapsed
    # math falls back to submit time.
    run_started_at: float = 0.0
    run_id: str = ""
    app_version: str = ""
    # Keep the established single-batch format at v1. Schema v2 is reserved
    # for the composite program manifest, so older builds can still resume a
    # newly-written traditional one-module batch.
    schema_version: int = _LEGACY_SCHEMA_VERSION

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
        # A real-time run has no remote batch to reconnect to — its reviews
        # ran to completion inside start_batch_review and die with the
        # process. Persisting one would seed the startup resume prompt with
        # a sentinel batch_id the API has never heard of. Callers gate on
        # review_transport before persisting; this guard is the backstop.
        if getattr(submission, "review_transport", "batch") == "realtime":
            raise ValueError(
                "Real-time review runs have no pending-batch resume state; "
                "refusing to persist one."
            )
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
            run_started_at=float(getattr(submission, "run_started_at", 0.0) or 0.0),
            run_id=run_id,
            app_version=app_version,
        )

    def to_submission(
        self, *, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress
    ) -> BatchSubmission:
        # A missing module field in a true legacy v1 record was normalized to
        # DEFAULT_MODULE by the loader. Any other unknown id is stale explicit
        # state and must never degrade into a California K-12 review.
        module = require_module(self.module_id)
        if self.cycle_label != module.cycle.label:
            raise ValueError(
                f"Pending batch {self.batch_id!r} was submitted under cycle "
                f"{self.cycle_label!r}, but module {module.module_id!r} now uses "
                f"{module.cycle.label!r}. Resume is blocked to avoid verifying "
                "results under a different code basis."
            )
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
            run_started_at=self.run_started_at,
            log=log,
            progress=progress,
        )


@dataclass
class PendingProgramRun:
    """Serializable manifest for every child batch in one routed program."""

    program_id: str
    assignments: list[dict] = field(default_factory=list)
    partitions: dict[str, dict] = field(default_factory=dict)
    project_profile: dict | None = None
    review_transport: str = "batch"
    submitted_at: float = 0.0
    # Wall-clock start of the whole run (pre-research/extraction; D2).
    # Additive — legacy manifests load the 0.0 default.
    run_started_at: float = 0.0
    run_id: str = ""
    app_version: str = ""
    schema_version: int = _SCHEMA_VERSION
    record_type: str = "program"

    @classmethod
    def from_submission(
        cls,
        submission: ProgramSubmission,
        *,
        input_dir: Any = "",
        files: list | None = None,
        run_id: str = "",
        app_version: str = "",
    ) -> "PendingProgramRun":
        if submission.review_transport == "realtime":
            raise ValueError(
                "Real-time program runs have no pending-batch resume state"
            )
        children: dict[str, dict] = {}
        for module_id, child in submission.partitions.items():
            pending = PendingBatch.from_submission(
                child,
                input_dir=input_dir,
                files=[
                    item.source_path
                    for item in submission.assignments
                    if module_id in item.module_ids
                ],
                run_id=run_id,
                app_version=app_version,
            )
            children[module_id] = asdict(pending)
        return cls(
            program_id=submission.program_id,
            assignments=[item.to_dict() for item in submission.assignments],
            partitions=children,
            project_profile=submission.project_profile,
            review_transport=submission.review_transport,
            submitted_at=submission.submitted_at,
            run_started_at=float(getattr(submission, "run_started_at", 0.0) or 0.0),
            run_id=run_id,
            app_version=app_version,
        )

    def to_submission(
        self, *, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress
    ) -> ProgramSubmission:
        program = require_program(self.program_id)
        assignments = tuple(SpecAssignment.from_dict(item) for item in self.assignments)
        for assignment in assignments:
            if assignment.decision.program_id != program.program_id:
                raise ValueError(
                    f"Pending assignment {assignment.spec_id!r} belongs to "
                    f"{assignment.decision.program_id!r}, not {program.program_id!r}"
                )
            unknown = set(assignment.module_ids) - set(program.implemented_module_ids)
            if unknown:
                raise ValueError(
                    f"Pending assignment {assignment.spec_id!r} names module(s) "
                    f"outside the program: {', '.join(sorted(unknown))}"
                )
        children: dict[str, BatchSubmission] = {}
        for module_id, data in self.partitions.items():
            if module_id not in program.implemented_module_ids:
                raise ValueError(
                    f"Pending program partition {module_id!r} is not a member of "
                    f"{program.program_id!r}"
                )
            module = require_module(module_id)
            child_pending = _pending_batch_from_mapping(data)
            if child_pending.module_id != module.module_id:
                raise ValueError(
                    f"Pending program partition {module_id!r} contains module "
                    f"{child_pending.module_id!r}"
                )
            children[module_id] = child_pending.to_submission(log=log, progress=progress)
        return ProgramSubmission(
            program_id=self.program_id,
            assignments=assignments,
            partitions=children,
            project_profile=self.project_profile,
            review_transport=self.review_transport,
            submitted_at=self.submitted_at,
            run_started_at=self.run_started_at,
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


def save_pending_program_run(
    pending: PendingProgramRun, *, path: Path | None = None
) -> None:
    """Atomically persist a routed program manifest. Best-effort."""
    target = path or pending_batch_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(pending), indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass


def save_pending_run(
    pending: PendingBatch | PendingProgramRun, *, path: Path | None = None
) -> None:
    if isinstance(pending, PendingProgramRun):
        save_pending_program_run(pending, path=path)
    else:
        save_pending_batch(pending, path=path)


def _pending_batch_from_mapping(data: object) -> PendingBatch:
    if not isinstance(data, dict):
        raise ValueError("pending batch entry must be an object")

    def _str(key: str, default: str = "") -> str:
        value = data.get(key, default)
        return value if isinstance(value, str) else default

    def _list(key: str) -> list:
        value = data.get(key)
        return list(value) if isinstance(value, list) else []

    batch_id = _str("batch_id").strip()
    if not batch_id:
        raise ValueError("pending batch has no usable batch_id")
    submitted = data.get("submitted_at")
    run_started = data.get("run_started_at")
    request_map = data.get("request_map")
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
        submitted_at=(
            float(submitted) if isinstance(submitted, (int, float)) else 0.0
        ),
        run_started_at=(
            float(run_started) if isinstance(run_started, (int, float)) else 0.0
        ),
        run_id=_str("run_id"),
        app_version=_str("app_version"),
        schema_version=(
            int(data.get("schema_version"))
            if data.get("schema_version") in (_LEGACY_SCHEMA_VERSION, _SCHEMA_VERSION)
            else _LEGACY_SCHEMA_VERSION
        ),
    )


def _read_pending_mapping(path: Path | None = None) -> dict | None:
    target = path or pending_batch_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("schema_version")
    if version not in (_LEGACY_SCHEMA_VERSION, _SCHEMA_VERSION):
        return None
    return data


def load_pending_batch(*, path: Path | None = None) -> PendingBatch | None:
    """Load persisted pending-batch state, or ``None`` when absent/unusable.

    Defensive on every axis — a missing file, malformed JSON, wrong schema
    version, or a record without a usable ``batch_id`` all read as "no pending
    batch" rather than raising into the startup path.
    """
    data = _read_pending_mapping(path)
    if data is None or data.get("record_type") == "program":
        return None
    try:
        return _pending_batch_from_mapping(data)
    except (TypeError, ValueError):
        return None


def load_pending_run(
    *, path: Path | None = None
) -> PendingBatch | PendingProgramRun | None:
    """Load either a routed-program manifest or a legacy/single batch."""
    data = _read_pending_mapping(path)
    if data is None:
        return None
    if data.get("record_type") != "program":
        try:
            return _pending_batch_from_mapping(data)
        except (TypeError, ValueError):
            return None
    program_id = data.get("program_id")
    assignments = data.get("assignments")
    partitions = data.get("partitions")
    if (
        not isinstance(program_id, str)
        or not program_id.strip()
        or not isinstance(assignments, list)
        or not isinstance(partitions, dict)
        or not partitions
    ):
        return None
    profile = data.get("project_profile")
    submitted = data.get("submitted_at")
    try:
        program = require_program(program_id)
        # Validate assignment/child shapes now; resume should fail before the
        # GUI offers an unusable manifest.
        for item in assignments:
            assignment = SpecAssignment.from_dict(item)
            if assignment.decision.program_id != program.program_id:
                raise ValueError("assignment belongs to a different program")
            if set(assignment.module_ids) - set(program.implemented_module_ids):
                raise ValueError("assignment names a module outside the program")
        for module_id, child in partitions.items():
            if module_id not in program.implemented_module_ids:
                raise ValueError("partition module is outside the program")
            require_module(str(module_id))
            child_pending = _pending_batch_from_mapping(child)
            if child_pending.module_id != module_id:
                raise ValueError("partition module does not match child state")
    except (KeyError, TypeError, ValueError):
        return None
    return PendingProgramRun(
        program_id=program_id,
        assignments=list(assignments),
        partitions=dict(partitions),
        project_profile=profile if isinstance(profile, dict) else None,
        review_transport=(
            str(data.get("review_transport"))
            if data.get("review_transport") in ("batch", "realtime")
            else "batch"
        ),
        submitted_at=(
            float(submitted) if isinstance(submitted, (int, float)) else 0.0
        ),
        run_started_at=(
            float(data.get("run_started_at"))
            if isinstance(data.get("run_started_at"), (int, float))
            else 0.0
        ),
        run_id=str(data.get("run_id") or ""),
        app_version=str(data.get("app_version") or ""),
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
    still be recovered. The batch must have ended: a still-running batch has
    no results stream and the SDK raises ("No ``results_url`` for the given
    batch"). Callers poll first via ``batch_runtime.ensure_batch_ended``.

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
