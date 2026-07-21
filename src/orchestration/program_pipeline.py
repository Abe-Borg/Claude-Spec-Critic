"""Composite orchestration for user-facing programs with routed modules.

Each child ``BatchSubmission`` and ``PipelineResult`` remains strictly
single-module. The composite layer partitions files, prepares every child
before remote submission, and retains module provenance through collection.
"""
from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable

from ..core.api_config import (
    program_collection_max_workers,
    program_prepare_max_workers,
    realtime_collection_max_calls,
    research_max_workers,
)
from ..core.project_profile import ProjectProfile
from ..modules import require_module
from ..programs import (
    RoutingState,
    SpecAssignment,
    partition_assignments,
    require_program,
)
from ..review.realtime_review import (
    build_realtime_review_jobs,
    run_realtime_review_jobs,
)
from ..review.reviewer import ReviewResult
from ..tracing import activate_span, current_span
from ..tracing import capture_hooks as _trace
from ..tracing.spans import KIND_PIPELINE, SpanHandle
from .pipeline import (
    BatchSubmission,
    PipelineResult,
    PreparedBatchReview,
    _make_verification_cache,
    _persist_verification_cache,
    build_realtime_batch_submission,
    prepare_batch_review,
    run_batch_collection_headless,
    submit_prepared_batch_review,
)

LogFn = Callable[..., None]
ProgressFn = Callable[..., None]


def _noop_log(_message: str, **_kwargs: object) -> None:
    return


def _noop_progress(_progress: float, _message: str, **_kwargs: object) -> None:
    return


def _close_abandoned_prepared_trace(
    child: PreparedBatchReview,
    *,
    phase: str,
    error: str,
) -> None:
    """Close a prepared child that can no longer reach collection.

    A prepared pipeline span normally stays open across remote submission and
    closes only after final collection.  Program-level barriers create a few
    honest abandonment paths, though: a sibling can fail preparation before
    any spend, or submission can stop before later prepared children are sent.
    Those children have no ``BatchSubmission`` that could close their span
    later, so the program coordinator must do it here.
    """

    trace_pipeline = getattr(child, "trace_pipeline", None)
    module = getattr(child, "module", None)
    module_id = getattr(module, "module_id", "")
    _trace.capture_pipeline_end_by_id(
        getattr(trace_pipeline, "span_id", ""),
        success=False,
        summary={
            "module_id": module_id,
            "phase": phase,
            "error": error,
        },
    )


class _WeightedProgress:
    """Thread-safe weighted progress with monotone, serialized delivery."""

    def __init__(
        self,
        *,
        weights: dict[str, int],
        labels: dict[str, str],
        base: float,
        span: float,
        local_ceiling: float,
        callback: ProgressFn,
    ) -> None:
        self._weights = {key: max(0, int(value)) for key, value in weights.items()}
        self._labels = dict(labels)
        self._base = float(base)
        self._span = float(span)
        self._local_ceiling = max(float(local_ceiling), 1.0)
        self._callback = callback
        self._high_water = {key: 0.0 for key in self._weights}
        self._total = max(1, sum(self._weights.values()))
        self._last_emitted = self._base
        # The callback is deliberately invoked while this lock is held.  That
        # serializes delivery as well as calculation; compute-under-lock and
        # call-outside can still deliver an older value after a newer one.
        self._lock = threading.RLock()

    def update(
        self,
        module_id: str,
        value: float,
        message: str,
        **kwargs: object,
    ) -> None:
        fraction = max(0.0, min(float(value), self._local_ceiling)) / self._local_ceiling
        with self._lock:
            self._high_water[module_id] = max(
                self._high_water.get(module_id, 0.0), fraction
            )
            weighted = sum(
                self._weights[key] * self._high_water.get(key, 0.0)
                for key in self._weights
            )
            target = self._base + self._span * (weighted / self._total)
            target = max(self._last_emitted, target)
            self._last_emitted = target
            label = self._labels.get(module_id, module_id)
            self._callback(target, f"{label}: {message}", **kwargs)

    def complete(self, module_id: str, message: str, **kwargs: object) -> None:
        self.update(module_id, self._local_ceiling, message, **kwargs)


def _validate_program_membership(
    program_id: str,
    assignments: tuple[SpecAssignment, ...],
    partition_ids: Iterable[str],
) -> None:
    """Reject stale or cross-program child state before any paid work."""
    program = require_program(program_id)
    allowed = set(program.implemented_module_ids)
    seen_specs: set[str] = set()
    expected: set[str] = set()
    for assignment in assignments:
        if assignment.spec_id in seen_specs:
            raise ValueError(f"Duplicate program assignment: {assignment.spec_id!r}")
        seen_specs.add(assignment.spec_id)
        if assignment.decision.program_id != program.program_id:
            raise ValueError(
                f"Assignment {assignment.spec_id!r} belongs to "
                f"{assignment.decision.program_id!r}, not {program.program_id!r}"
            )
        unknown = set(assignment.module_ids) - allowed
        if unknown:
            raise ValueError(
                f"Assignment {assignment.spec_id!r} names module(s) outside "
                f"the program: {', '.join(sorted(unknown))}"
            )
        expected.update(assignment.module_ids)
    partitions = set(partition_ids)
    unknown_partitions = partitions - allowed
    if unknown_partitions:
        raise ValueError(
            "Program contains child partition(s) outside its module catalog: "
            + ", ".join(sorted(unknown_partitions))
        )
    unassigned_partitions = partitions - expected
    if unassigned_partitions:
        raise ValueError(
            "Program contains child partition(s) not selected by any assignment: "
            + ", ".join(sorted(unassigned_partitions))
        )


@dataclass
class PreparedProgramReview:
    program_id: str
    assignments: tuple[SpecAssignment, ...]
    partitions: dict[str, PreparedBatchReview]
    project_profile: dict | None = None
    review_transport: str = "batch"
    prepared_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.assignments = tuple(self.assignments)
        _validate_program_membership(
            self.program_id, self.assignments, self.partitions
        )
        for module_id, child in self.partitions.items():
            if child.module.module_id != module_id:
                raise ValueError(
                    f"Prepared partition {module_id!r} contains "
                    f"{child.module.module_id!r}"
                )

    @property
    def skipped_assignments(self) -> tuple[SpecAssignment, ...]:
        return tuple(item for item in self.assignments if not item.module_ids)


@dataclass
class ProgramSubmission:
    """One user run containing one remote/local child submission per module."""

    program_id: str
    assignments: tuple[SpecAssignment, ...]
    partitions: dict[str, BatchSubmission]
    project_profile: dict | None = None
    review_transport: str = "batch"
    submitted_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.assignments = tuple(self.assignments)
        _validate_program_membership(
            self.program_id, self.assignments, self.partitions
        )
        for module_id, child in self.partitions.items():
            if child.module_id != module_id:
                raise ValueError(
                    f"Submission partition {module_id!r} contains "
                    f"{child.module_id!r}"
                )

    @property
    def selected_files(self) -> list[str]:
        return list(dict.fromkeys(item.spec_id for item in self.assignments))

    @property
    def expected_files_reviewed(self) -> list[str]:
        """Unique specifications that the confirmed routing intends to review."""
        return list(
            dict.fromkeys(item.spec_id for item in self.assignments if item.module_ids)
        )

    @property
    def files_reviewed(self) -> list[str]:
        """Unique specifications represented by actually submitted partitions."""
        submitted_names = {
            Path(name).name
            for child in self.partitions.values()
            for name in child.files_reviewed
        }
        ordered = [
            item.spec_id
            for item in self.assignments
            if Path(item.spec_id).name in submitted_names
        ]
        # Preserve honest child data even for a recovered/legacy submission
        # whose assignment metadata is incomplete.
        ordered.extend(
            name
            for child in self.partitions.values()
            for name in child.files_reviewed
            if Path(name).name not in {Path(item).name for item in ordered}
        )
        return list(dict.fromkeys(ordered))

    @property
    def expected_routed_request_count(self) -> int:
        """Module requests intended by the confirmed routing decision."""
        return sum(len(item.module_ids) for item in self.assignments)

    @property
    def routed_request_count(self) -> int:
        """Module requests represented by actually submitted partitions."""
        return sum(
            len(child.review_request_ids) for child in self.partitions.values()
        )

    @property
    def expected_module_ids(self) -> tuple[str, ...]:
        program = require_program(self.program_id)
        requested = {
            module_id
            for item in self.assignments
            for module_id in item.module_ids
        }
        return tuple(
            module_id
            for module_id in program.implemented_module_ids
            if module_id in requested
        )

    @property
    def missing_module_ids(self) -> tuple[str, ...]:
        submitted = set(self.partitions)
        return tuple(
            module_id
            for module_id in self.expected_module_ids
            if module_id not in submitted
        )

    @property
    def skipped_assignments(self) -> tuple[SpecAssignment, ...]:
        return tuple(item for item in self.assignments if not item.module_ids)

    @property
    def batch_ids(self) -> dict[str, str]:
        return {
            module_id: submission.job.batch_id
            for module_id, submission in self.partitions.items()
        }


class ProgramSubmissionError(RuntimeError):
    """A later child failed after earlier partitions may have been submitted."""

    def __init__(self, message: str, *, partial_submission: ProgramSubmission):
        super().__init__(message)
        self.partial_submission = partial_submission


@dataclass
class ProgramPipelineResult:
    """Composite final result that never flattens away module provenance."""

    program_id: str
    assignments: tuple[SpecAssignment, ...]
    module_results: dict[str, PipelineResult]
    project_profile: dict | None = None
    review_transport: str = "batch"
    drawing_impact_result: object | None = None
    module_errors: dict[str, str] = field(default_factory=dict)
    submitted_files: tuple[str, ...] | None = None
    submitted_request_count: int | None = None
    total_elapsed_seconds: float | None = None

    def __post_init__(self) -> None:
        self.assignments = tuple(self.assignments)
        _validate_program_membership(
            self.program_id, self.assignments, self.module_results
        )
        for module_id, result in self.module_results.items():
            if result.module_id != module_id:
                raise ValueError(
                    f"Program result {module_id!r} contains {result.module_id!r}"
                )
        self.module_errors = {
            str(module_id): str(message)
            for module_id, message in (self.module_errors or {}).items()
        }
        program = require_program(self.program_id)
        allowed = set(program.implemented_module_ids)
        expected = {
            module_id
            for item in self.assignments
            for module_id in item.module_ids
        }
        unknown_errors = set(self.module_errors) - allowed
        if unknown_errors:
            raise ValueError(
                "Program result records errors for module(s) outside its catalog: "
                + ", ".join(sorted(unknown_errors))
            )
        unassigned_errors = set(self.module_errors) - expected
        if unassigned_errors:
            raise ValueError(
                "Program result records errors for unassigned module(s): "
                + ", ".join(sorted(unassigned_errors))
            )
        contradictory = set(self.module_errors).intersection(self.module_results)
        if contradictory:
            raise ValueError(
                "Program result cannot contain both a completed result and a "
                "collection error for: " + ", ".join(sorted(contradictory))
            )
        submitted_modules = set(self.module_results).union(self.module_errors)
        if self.submitted_files is None:
            self.submitted_files = tuple(
                dict.fromkeys(
                    item.spec_id
                    for item in self.assignments
                    if submitted_modules.intersection(item.module_ids)
                )
            )
        else:
            self.submitted_files = tuple(dict.fromkeys(self.submitted_files))
        unknown_files = set(self.submitted_files) - set(self.selected_files)
        if unknown_files:
            raise ValueError(
                "Program result records submitted file(s) outside its assignments: "
                + ", ".join(sorted(unknown_files))
            )
        if self.submitted_request_count is None:
            self.submitted_request_count = sum(
                sum(module_id in submitted_modules for module_id in item.module_ids)
                for item in self.assignments
            )
        self.submitted_request_count = int(self.submitted_request_count)
        if not 0 <= self.submitted_request_count <= self.expected_routed_request_count:
            raise ValueError(
                "Program result submitted_request_count must be between zero and "
                "the expected routed request count"
            )

    @property
    def selected_files(self) -> list[str]:
        return list(dict.fromkeys(item.spec_id for item in self.assignments))

    @property
    def expected_files_reviewed(self) -> list[str]:
        return list(
            dict.fromkeys(item.spec_id for item in self.assignments if item.module_ids)
        )

    @property
    def files_reviewed(self) -> list[str]:
        """Specifications represented by submitted routed module requests."""
        return list(self.submitted_files or ())

    @property
    def expected_routed_request_count(self) -> int:
        return sum(len(item.module_ids) for item in self.assignments)

    @property
    def routed_request_count(self) -> int:
        return int(self.submitted_request_count or 0)

    @property
    def skipped_files(self) -> list[str]:
        return [item.spec_id for item in self.assignments if not item.module_ids]

    @property
    def missing_module_ids(self) -> list[str]:
        expected = {
            module_id
            for item in self.assignments
            for module_id in item.module_ids
        }
        return sorted(expected - set(self.module_results))

    @property
    def failed_review_specs(self) -> list[str]:
        failed: list[str] = []
        for module_id, result in self.module_results.items():
            failed.extend(
                f"{module_id}: {filename}"
                for filename in (result.failed_review_specs or [])
            )
        return failed

    @property
    def status(self) -> str:
        if (
            self.failed_review_specs
            or self.skipped_files
            or self.missing_module_ids
            or self.module_errors
            or self.routed_request_count < self.expected_routed_request_count
        ):
            return "partial"
        return "completed"

    @property
    def review_result(self) -> ReviewResult:
        return _merge_review_results(
            (module_id, result.review_result)
            for module_id, result in self.module_results.items()
            if result.review_result is not None
        )

    @property
    def cross_check_result(self) -> ReviewResult | None:
        pairs = [
            (module_id, result.cross_check_result)
            for module_id, result in self.module_results.items()
            if result.cross_check_result is not None
        ]
        return _merge_review_results(pairs, phase="cross-check") if pairs else None

    @property
    def compliance_result(self) -> ReviewResult | None:
        pairs = [
            (module_id, result.compliance_result)
            for module_id, result in self.module_results.items()
            if result.compliance_result is not None
        ]
        return _merge_review_results(pairs, phase="compliance") if pairs else None


def _merge_review_results(
    pairs: Iterable[tuple[str, ReviewResult]], *, phase: str = "review"
) -> ReviewResult:
    pairs = list(pairs)
    findings = []
    thinking: list[str] = []
    errors: list[str] = []
    coverage: list[dict] = []
    for module_id, result in pairs:
        findings.extend(result.findings or [])
        if result.thinking:
            thinking.append(f"--- {module_id} {phase} ---\n{result.thinking}")
        if result.error:
            errors.append(f"{module_id}: {result.error}")
        coverage.extend(result.coverage or [])
    statuses = [result.cross_check_status for _, result in pairs if result.cross_check_status]
    if not statuses:
        combined_status = None
    elif any(status == "failed" for status in statuses):
        combined_status = "failed"
    elif any(status == "skipped" for status in statuses):
        combined_status = "skipped" if all(s == "skipped" for s in statuses) else "completed"
    else:
        combined_status = "completed"
    return ReviewResult(
        findings=findings,
        thinking="\n\n".join(thinking),
        model=(pairs[0][1].model if pairs else ""),
        input_tokens=sum(result.input_tokens for _, result in pairs),
        output_tokens=sum(result.output_tokens for _, result in pairs),
        cache_creation_input_tokens=sum(
            result.cache_creation_input_tokens for _, result in pairs
        ),
        cache_read_input_tokens=sum(result.cache_read_input_tokens for _, result in pairs),
        elapsed_seconds=max((result.elapsed_seconds for _, result in pairs), default=0.0),
        error="; ".join(errors) if errors else None,
        parse_status="ok" if not errors else "partial",
        cross_check_status=combined_status,
        chunk_failures=sum(result.chunk_failures for _, result in pairs),
        chunk_skips=sum(result.chunk_skips for _, result in pairs),
        coverage=coverage,
    )


def prepare_program_review(
    *,
    program_id: str,
    assignments: Iterable[SpecAssignment],
    input_dir: Path,
    project_context: str = "",
    model: str,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    cross_check_enabled: bool = False,
    project_profile: ProjectProfile | None = None,
    diagnostics=None,
    review_transport: str = "batch",
) -> PreparedProgramReview:
    """Prepare all routed partitions before creating any remote review batch."""

    program = require_program(program_id)
    assignment_tuple = tuple(assignments)
    unresolved = [
        item.spec_id
        for item in assignment_tuple
        if item.decision.state is RoutingState.AMBIGUOUS
    ]
    if unresolved:
        raise ValueError(
            "Ambiguous specification routing must be confirmed before review: "
            + ", ".join(unresolved)
        )
    file_partitions = partition_assignments(assignment_tuple, program=program)
    if not file_partitions:
        raise ValueError("No specifications are routed to an implemented review module")

    active = [
        (module_id, file_partitions[module_id], require_module(module_id))
        for module_id in program.implemented_module_ids
        if file_partitions.get(module_id)
    ]
    weights = {module_id: len(paths) for module_id, paths, _module in active}
    progress_state = _WeightedProgress(
        weights=weights,
        labels={module_id: module.display_name for module_id, _paths, module in active},
        base=0.0,
        span=25.0,
        local_ceiling=25.0,
        callback=progress,
    )
    research_call_semaphore = threading.BoundedSemaphore(research_max_workers())

    def prepare_one(
        module_id: str,
        paths: list[Path],
        module,
        *,
        trace_parent=None,
        explicit_trace_parent: bool = False,
    ) -> PreparedBatchReview:
        def child_progress(value: float, message: str, **kwargs: object) -> None:
            progress_state.update(module_id, value, message, **kwargs)

        log(f"Preparing routed module: {module.display_name}", level="step")
        trace_kwargs = (
            {
                "_trace_parent": trace_parent,
                "_trace_inherit_current_parent": False,
            }
            if explicit_trace_parent
            else {}
        )
        return prepare_batch_review(
            input_dir=input_dir,
            files=paths,
            project_context=project_context,
            model=model,
            log=log,
            progress=child_progress,
            module=module,
            cross_check_enabled=cross_check_enabled,
            project_profile=project_profile,
            diagnostics=diagnostics,
            review_transport=review_transport,
            research_call_semaphore=research_call_semaphore,
            # Do not activate the caller span around the whole preparation.
            # ``capture_pipeline_start`` opens the child pipeline explicitly;
            # it then sits on this worker's local stack and correctly parents
            # research/extraction spans beneath that child. Explicit-root mode
            # also masks a prior module pipeline left on a reused worker.
            **trace_kwargs,
        )

    prepared_by_module: dict[str, PreparedBatchReview] = {}
    preparation_errors: dict[str, Exception] = {}
    # Preserve the single-module execution path exactly: aside from avoiding
    # executor overhead, capture hooks that use a thread-local span stack keep
    # their historical parentage and log/progress timing.
    if len(active) == 1:
        module_id, paths, module = active[0]
        prepared_by_module[module_id] = prepare_one(module_id, paths, module)
        progress_state.complete(module_id, "preparation complete")
    else:
        trace_parent = current_span()
        with ThreadPoolExecutor(
            max_workers=min(program_prepare_max_workers(), len(active)),
            thread_name_prefix="spec-program-prepare",
        ) as pool:
            futures = {
                pool.submit(
                    prepare_one,
                    module_id,
                    paths,
                    module,
                    trace_parent=trace_parent,
                    explicit_trace_parent=True,
                ): module_id
                for module_id, paths, module in active
            }
            for future in as_completed(futures):
                module_id = futures[future]
                try:
                    prepared_by_module[module_id] = future.result()
                except Exception as exc:  # one module must not cancel siblings
                    preparation_errors[module_id] = exc
                    progress_state.complete(module_id, "preparation failed")
                else:
                    progress_state.complete(module_id, "preparation complete")

    if preparation_errors:
        ordered_errors = [
            (module_id, preparation_errors[module_id])
            for module_id, _paths, _module in active
            if module_id in preparation_errors
        ]
        failure_summary = "; ".join(
            f"{module_id}: {exc}" for module_id, exc in ordered_errors
        )
        # Failed children close their own spans inside ``prepare_batch_review``.
        # Successful siblings are nevertheless abandoned by the all-preflight
        # barrier and will never produce a submission/collection object, so
        # close those spans explicitly rather than leaving them open forever.
        for module_id, _paths, _module in active:
            child = prepared_by_module.get(module_id)
            if child is not None:
                _close_abandoned_prepared_trace(
                    child,
                    phase="preparation_aborted",
                    error=f"Sibling preparation failed: {failure_summary}",
                )
        log(
            "Program preparation failed before review submission: "
            + failure_summary,
            level="error",
        )
        # Match the former serial path's exception contract: callers that
        # distinguish ResearchFanoutError/FileNotFoundError still see the
        # original type, while every already-started sibling has been joined.
        raise ordered_errors[0][1]

    prepared_partitions = {
        module_id: prepared_by_module[module_id]
        for module_id in program.implemented_module_ids
        if module_id in prepared_by_module
    }

    return PreparedProgramReview(
        program_id=program.program_id,
        assignments=assignment_tuple,
        partitions=prepared_partitions,
        project_profile=(project_profile.to_dict() if project_profile is not None else None),
        review_transport=review_transport,
    )


def submit_prepared_program_review(
    prepared: PreparedProgramReview,
    *,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    on_partition_submitted: Callable[[ProgramSubmission], None] | None = None,
) -> ProgramSubmission:
    """Submit prepared partitions in program order, retaining partial state."""

    program = require_program(prepared.program_id)
    active_module_ids = [
        module_id
        for module_id in program.implemented_module_ids
        if module_id in prepared.partitions
    ]

    # A routed real-time program must use one account-wide pool.  Calling the
    # historical child submitter in the loop below would block until each
    # module's private pool completed, recreating the module barrier this
    # orchestrator exists to remove.  Keep the one-module path on the legacy
    # wrapper for byte/log/trace compatibility.
    if prepared.review_transport == "realtime" and len(active_module_ids) > 1:
        started_at = time.time()
        jobs = []
        request_maps: dict[str, dict[str, dict]] = {}
        jobs_by_module: dict[str, list] = {}
        diagnostics = None
        try:
            for module_id in active_module_ids:
                child = prepared.partitions[module_id]
                if child.review_transport != "realtime":
                    raise ValueError(
                        f"Prepared program transport is realtime but {module_id!r} "
                        f"uses {child.review_transport!r}"
                    )
                if diagnostics is None:
                    diagnostics = child.diagnostics
                child_jobs, request_map = build_realtime_review_jobs(
                    child.prepared.specs,
                    project_context=child.effective_context,
                    model=child.model,
                    cycle=child.module.cycle,
                    pre_detected_alerts=child.prepared.pre_detected_by_filename,
                    job_key_factory=(
                        lambda custom_id, _index, mid=module_id: (mid, custom_id)
                    ),
                    trace_parent=child.trace_pipeline,
                    display_name_factory=(
                        lambda filename, _index, name=child.module.display_name: (
                            f"{name}: {filename}"
                        )
                    ),
                )
                jobs.extend(child_jobs)
                jobs_by_module[module_id] = child_jobs
                request_maps[module_id] = request_map

            results_by_job = run_realtime_review_jobs(
                jobs,
                log=log,
                progress=lambda value, message: progress(
                    25.0 + (max(0.0, min(float(value), 100.0)) / 100.0) * 30.0,
                    message,
                ),
                diagnostics=diagnostics,
            )
        except BaseException as exc:
            for module_id in active_module_ids:
                _close_abandoned_prepared_trace(
                    prepared.partitions[module_id],
                    phase="submission",
                    error=str(exc),
                )
            # Preserve interpreter cancellation semantics while still closing
            # trace state. Ordinary failures retain the public partial-state
            # exception contract below.
            if not isinstance(exc, Exception):
                raise
            partial = ProgramSubmission(
                program_id=prepared.program_id,
                assignments=prepared.assignments,
                partitions={},
                project_profile=prepared.project_profile,
                review_transport=prepared.review_transport,
                submitted_at=prepared.prepared_at,
            )
            raise ProgramSubmissionError(
                "Could not complete the program-wide real-time review before "
                f"building child submissions: {exc}",
                partial_submission=partial,
            ) from exc

        submitted: dict[str, BatchSubmission] = {}
        for module_id in active_module_ids:
            child = prepared.partitions[module_id]
            child_results = {
                job.custom_id: results_by_job[job.job_key]
                for job in jobs_by_module[module_id]
            }
            submitted[module_id] = build_realtime_batch_submission(
                child,
                realtime_results=child_results,
                request_map=request_maps[module_id],
                started_at=started_at,
            )
            current = ProgramSubmission(
                program_id=prepared.program_id,
                assignments=prepared.assignments,
                partitions=dict(submitted),
                project_profile=prepared.project_profile,
                review_transport=prepared.review_transport,
                submitted_at=prepared.prepared_at,
            )
            if on_partition_submitted is not None:
                on_partition_submitted(current)
        progress(55.0, "All routed modules: real-time review complete")
        return ProgramSubmission(
            program_id=prepared.program_id,
            assignments=prepared.assignments,
            partitions=submitted,
            project_profile=prepared.project_profile,
            review_transport=prepared.review_transport,
            submitted_at=prepared.prepared_at,
        )

    submitted: dict[str, BatchSubmission] = {}
    total = sum(len(part.prepared.specs) for part in prepared.partitions.values())
    completed = 0
    for module_id in program.implemented_module_ids:
        child = prepared.partitions.get(module_id)
        if child is None:
            continue
        child_high_water = 0.0

        def child_progress(value: float, message: str, **kwargs: object) -> None:
            nonlocal child_high_water
            child_high_water = max(
                child_high_water,
                max(0.0, min(float(value), 100.0)) / 100.0,
            )
            aggregate = (
                completed + len(child.prepared.specs) * child_high_water
            ) / max(1, total)
            progress(
                25.0 + aggregate * 30.0,
                f"{child.module.display_name}: {message}",
                **kwargs,
            )
        try:
            submission = submit_prepared_batch_review(
                child,
                log=log,
                progress=child_progress,
            )
        except BaseException as exc:
            failed_index = active_module_ids.index(module_id)
            for abandoned_id in active_module_ids[failed_index:]:
                _close_abandoned_prepared_trace(
                    prepared.partitions[abandoned_id],
                    phase=(
                        "submission"
                        if abandoned_id == module_id
                        else "submission_aborted"
                    ),
                    error=str(exc),
                )
            if not isinstance(exc, Exception):
                raise
            partial = ProgramSubmission(
                program_id=prepared.program_id,
                assignments=prepared.assignments,
                partitions=dict(submitted),
                project_profile=prepared.project_profile,
                review_transport=prepared.review_transport,
                submitted_at=prepared.prepared_at,
            )
            raise ProgramSubmissionError(
                f"Could not submit {module_id!r} after "
                f"{len(submitted)} program partition(s): {exc}",
                partial_submission=partial,
            ) from exc
        submitted[module_id] = submission
        completed += len(child.prepared.specs)
        progress(
            25.0 + (completed / max(1, total)) * 30.0,
            f"{child.module.display_name}: submission complete",
        )
        current = ProgramSubmission(
            program_id=prepared.program_id,
            assignments=prepared.assignments,
            partitions=dict(submitted),
            project_profile=prepared.project_profile,
            review_transport=prepared.review_transport,
            submitted_at=prepared.prepared_at,
        )
        if on_partition_submitted is not None:
            on_partition_submitted(current)
    return ProgramSubmission(
        program_id=prepared.program_id,
        assignments=prepared.assignments,
        partitions=submitted,
        project_profile=prepared.project_profile,
        review_transport=prepared.review_transport,
        submitted_at=prepared.prepared_at,
    )


def start_program_review(**kwargs) -> ProgramSubmission:
    """Compatibility-shaped prepare-then-submit entry point for GUI callers."""
    on_partition_submitted = kwargs.pop("on_partition_submitted", None)
    log = kwargs.get("log", _noop_log)
    progress = kwargs.get("progress", _noop_progress)
    prepared = prepare_program_review(**kwargs)
    return submit_prepared_program_review(
        prepared,
        log=log,
        progress=progress,
        on_partition_submitted=on_partition_submitted,
    )


def collect_program_results(
    submission: ProgramSubmission,
    *,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> ProgramPipelineResult:
    """Collect every child through its unchanged single-module pipeline."""

    program = require_program(submission.program_id)
    cache = _make_verification_cache(log=log)
    results: dict[str, PipelineResult] = {}
    module_errors: dict[str, str] = {}
    active = [
        (module_id, submission.partitions[module_id], require_module(module_id))
        for module_id in program.implemented_module_ids
        if module_id in submission.partitions
    ]
    progress_state = _WeightedProgress(
        weights={
            module_id: len(child.review_request_ids)
            for module_id, child, _module in active
        },
        labels={module_id: module.display_name for module_id, _child, module in active},
        base=55.0,
        span=40.0,
        local_ceiling=100.0,
        callback=progress,
    )
    drawing_impact_result = None
    api_call_semaphore = threading.BoundedSemaphore(
        realtime_collection_max_calls()
    )

    def collect_one(
        module_id: str,
        child: BatchSubmission,
        module,
        *,
        trace_parent: SpanHandle | None = None,
        concurrent: bool,
    ) -> PipelineResult:
        def child_progress(value: float, message: str, **kwargs: object) -> None:
            progress_state.update(module_id, value, message, **kwargs)

        def run() -> PipelineResult:
            log(f"Collecting routed module: {module.display_name}", level="step")
            return run_batch_collection_headless(
                child,
                cache=cache,
                log=log,
                progress=child_progress,
                # Drawings are shared program context. Running this inside every
                # child would multiply spend and produce competing narratives.
                include_drawing_impact=False,
                # Only the multi-module path needs a shared permit pool.  The
                # direct child path stays byte/trace compatible with the
                # historical single-module collection behavior.
                api_call_semaphore=(api_call_semaphore if concurrent else None),
            )

        if trace_parent is None:
            return run()
        with activate_span(trace_parent):
            return run()

    try:
        result_outcomes: dict[str, PipelineResult] = {}
        error_outcomes: dict[str, str] = {}
        if len(active) == 1:
            module_id, child, module = active[0]
            try:
                result_outcomes[module_id] = collect_one(
                    module_id,
                    child,
                    module,
                    concurrent=False,
                )
            except Exception as exc:
                error_outcomes[module_id] = str(exc)
                _trace.capture_pipeline_end_by_id(
                    child.trace_span_id,
                    success=False,
                    summary={
                        "module_id": module_id,
                        "phase": "collection",
                        "error": str(exc),
                    },
                )
                log(
                    f"Could not collect {module.display_name}; retaining completed "
                    f"module results and marking coverage partial: {exc}",
                    level="warning",
                )
            progress_state.complete(
                module_id,
                "collection complete"
                if module_id in result_outcomes
                else "collection failed",
            )
        else:
            with ThreadPoolExecutor(
                max_workers=min(program_collection_max_workers(), len(active)),
                thread_name_prefix="spec-program-collect",
            ) as pool:
                futures = {}
                for module_id, child, module in active:
                    trace_parent = (
                        SpanHandle(
                            span_id=child.trace_span_id,
                            kind=KIND_PIPELINE,
                            started_at=0.0,
                        )
                        if child.trace_span_id
                        else None
                    )
                    future = pool.submit(
                        collect_one,
                        module_id,
                        child,
                        module,
                        trace_parent=trace_parent,
                        concurrent=True,
                    )
                    futures[future] = (module_id, module)

                for future in as_completed(futures):
                    module_id, module = futures[future]
                    try:
                        result_outcomes[module_id] = future.result()
                    except Exception as exc:  # one module never cancels siblings
                        error_outcomes[module_id] = str(exc)
                        failed_child = submission.partitions[module_id]
                        _trace.capture_pipeline_end_by_id(
                            failed_child.trace_span_id,
                            success=False,
                            summary={
                                "module_id": module_id,
                                "phase": "collection",
                                "error": str(exc),
                            },
                        )
                        log(
                            f"Could not collect {module.display_name}; retaining "
                            "completed module results and marking coverage partial: "
                            f"{exc}",
                            level="warning",
                        )
                    progress_state.complete(
                        module_id,
                        "collection complete"
                        if module_id in result_outcomes
                        else "collection failed",
                    )

        # Future completion order is nondeterministic, but it is observable in
        # merged findings/thinking/report order. Rebuild both maps strictly in
        # declared program order before any aggregate or drawing synthesis.
        results = {
            module_id: result_outcomes[module_id]
            for module_id in program.implemented_module_ids
            if module_id in result_outcomes
        }
        module_errors = {
            module_id: error_outcomes[module_id]
            for module_id in program.implemented_module_ids
            if module_id in error_outcomes
        }

        if module_errors and not results:
            details = "; ".join(
                f"{module_id}: {message}"
                for module_id, message in module_errors.items()
            )
            raise RuntimeError(
                "No routed module result could be collected. " + details
            )

        drawing_impact_result = _run_program_drawing_impact(
            program=program,
            submission=submission,
            module_results=results,
            log=log,
        )
    finally:
        # A later child may fail after earlier verification calls completed.
        # Persist their cache entries so resume does not repay for them.
        _persist_verification_cache(cache, log=log)
    return ProgramPipelineResult(
        program_id=submission.program_id,
        assignments=submission.assignments,
        module_results=results,
        project_profile=submission.project_profile,
        review_transport=submission.review_transport,
        drawing_impact_result=drawing_impact_result,
        module_errors=module_errors,
        submitted_files=tuple(submission.files_reviewed),
        submitted_request_count=submission.routed_request_count,
        total_elapsed_seconds=time.time() - submission.submitted_at,
    )


def _run_program_drawing_impact(
    *,
    program,
    submission: ProgramSubmission,
    module_results: dict[str, PipelineResult],
    log: LogFn,
):
    """Run one drawing synthesis over module-qualified program findings."""
    from ..drawing_impact import extract_drawing_digest, run_drawing_impact

    digests: list[str] = []
    for child in submission.partitions.values():
        digest = extract_drawing_digest(child.project_context)
        if digest and digest not in digests:
            digests.append(digest)
    if not digests:
        return None
    if len(digests) > 1:
        log(
            "Routed module contexts contained different drawing digests; "
            "using the first shared submission digest.",
            level="warning",
        )

    qualified_findings = []
    for module_id in program.implemented_module_ids:
        result = module_results.get(module_id)
        if result is None:
            continue
        phase_results = (
            result.review_result,
            result.cross_check_result,
            result.compliance_result,
        )
        for phase_result in phase_results:
            for finding in getattr(phase_result, "findings", None) or []:
                finding_id = str(getattr(finding, "finding_id", "") or "")
                if not finding_id:
                    continue
                # Finding ids are unique inside one module run, not across a
                # program. Qualifying the copies keeps drawing links honest
                # without mutating child results or their edit-sidecar ids.
                qualified_findings.append(
                    replace(finding, finding_id=f"{module_id}::{finding_id}")
                )

    log(
        "Explaining how the construction drawings informed the routed review...",
        level="step",
    )
    return run_drawing_impact(
        digest_text=digests[0],
        findings=qualified_findings,
        module=SimpleNamespace(display_name=program.display_name),
        log=log,
    )
