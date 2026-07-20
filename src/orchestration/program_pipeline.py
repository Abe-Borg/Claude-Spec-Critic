"""Composite orchestration for user-facing programs with routed modules.

Each child ``BatchSubmission`` and ``PipelineResult`` remains strictly
single-module. The composite layer partitions files, prepares every child
before remote submission, and retains module provenance through collection.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable

from ..core.project_profile import ProjectProfile
from ..modules import require_module
from ..programs import (
    RoutingState,
    SpecAssignment,
    partition_assignments,
    require_program,
)
from ..review.reviewer import ReviewResult
from .pipeline import (
    BatchSubmission,
    PipelineResult,
    PreparedBatchReview,
    _make_verification_cache,
    _persist_verification_cache,
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

    total_requests = sum(len(paths) for paths in file_partitions.values())
    prepared_partitions: dict[str, PreparedBatchReview] = {}
    completed_requests = 0
    for module_id in program.implemented_module_ids:
        paths = file_partitions.get(module_id)
        if not paths:
            continue
        module = require_module(module_id)
        weight = len(paths)

        def child_progress(value: float, message: str, **kwargs: object) -> None:
            local_fraction = max(0.0, min(float(value), 25.0)) / 25.0
            aggregate = (completed_requests + weight * local_fraction) / total_requests
            progress(aggregate * 25.0, f"{module.display_name}: {message}", **kwargs)

        log(f"Preparing routed module: {module.display_name}", level="step")
        prepared_partitions[module_id] = prepare_batch_review(
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
        )
        completed_requests += weight

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
        except Exception as exc:
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
    total = max(1, sum(len(s.review_request_ids) for s in submission.partitions.values()))
    completed = 0
    drawing_impact_result = None
    try:
        for module_id in program.implemented_module_ids:
            child = submission.partitions.get(module_id)
            if child is None:
                continue
            module = require_module(module_id)
            count = len(child.review_request_ids)
            child_high_water = 0.0

            def child_progress(value: float, message: str, **kwargs: object) -> None:
                nonlocal child_high_water
                child_high_water = max(
                    child_high_water,
                    max(0.0, min(float(value), 100.0)) / 100.0,
                )
                aggregate = (completed + count * child_high_water) / total
                progress(
                    55.0 + aggregate * 40.0,
                    f"{module.display_name}: {message}",
                    **kwargs,
                )

            log(f"Collecting routed module: {module.display_name}", level="step")
            try:
                result = run_batch_collection_headless(
                    child,
                    cache=cache,
                    log=log,
                    progress=child_progress,
                    # Drawings are shared program context. Running this inside every
                    # child would multiply spend and produce competing narratives.
                    include_drawing_impact=False,
                )
            except Exception as exc:
                module_errors[module_id] = str(exc)
                log(
                    f"Could not collect {module.display_name}; retaining completed "
                    f"module results and marking coverage partial: {exc}",
                    level="warning",
                )
            else:
                results[module_id] = result
            completed += count
            progress(
                55.0 + (completed / total) * 40.0,
                (
                    f"{module.display_name}: collection complete"
                    if module_id in results
                    else f"{module.display_name}: collection failed"
                ),
            )

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
