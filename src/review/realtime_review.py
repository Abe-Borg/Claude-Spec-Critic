"""Real-time (synchronous streaming) per-spec review runner.

The Message Batches path submits every spec's review as one remote batch
(``batch.submit_review_batch``) and retrieves results after polling; this
module is the other transport: one streaming Messages call per spec, fanned
out over a small thread pool, producing the SAME ``{custom_id: ReviewResult}``
and ``request_map`` shapes — so ``pipeline.collect_review_batch_results`` and
everything downstream (anchor validation, dedup + ``rf-`` id stamping,
verification, cross-check, report, failed-review surfacing) is reused
byte-for-byte.

Shape contract with the batch transport (deliberate, tested):

* Requests come from the same central builder (``build_review_request``), so
  the prompt bytes — and therefore the prompt-cache prefix — are identical
  across transports. The only params deltas: ``service_tier`` is omitted
  (``include_service_tier=False``, the proven live streaming shape) and
  extended 300k output is pinned off (``force_allow_extended_output=False``
  — the ``output-300k-2026-03-24`` beta is batch-only by API design, so the
  real-time cap is the 128k phase baseline).
* Responses classify through ``reviewer.review_result_from_message`` — the
  same stop-reason gate / structured-tool parse / tagged-JSON fallback the
  batch retrieval path uses.
* Truncation parity: a truncated or unparseable response gets ONE inline
  repair call carrying ``RETRY_TRUNCATED_REVIEW_INSTRUCTION`` — the same
  instructed retry the batch repair pass
  (``pipeline._recover_retryable_review_batch_results``) gives a failed
  batch item — before the spec is allowed to surface as failed.
* Failure honesty: a spec whose review ultimately fails yields a
  ``ReviewResult`` with ``parse_status`` / ``error`` set (a worker never
  raises), which the shared collect loop buckets into ``truncated_specs`` →
  the Run Diagnostics banner and ``PipelineResult.failed_review_specs``.

Inputs ≥ ``LARGE_REVIEW_INPUT_THRESHOLD`` (the point where the batch path
lifts output to 300k) are refused *before any spend* with an actionable
``ValueError`` — matching the "token preflight raises, not warns" invariant —
because a guaranteed-truncatable full-price 128k review is worse than a
clear error. The gate mirrors ``_resolve_extended_output``'s condition, so
it only fires where batch genuinely offers more (models whitelisted for the
extended-output beta).

Concurrency: ``ThreadPoolExecutor`` capped by
``api_config.realtime_review_max_workers()`` (default 4 — review streams
are the app's heaviest synchronous calls). Workers never call ``log`` or
``diagnostics`` (the research fan-out's worker-thread rule); per-call
telemetry rides each outcome back to the coordinator, which logs per-spec
completion, advances ``progress``, and records
``record_api_call(mode="realtime")`` rows as futures complete. There is no
resume story for this transport — a crash loses in-flight work, which is
the documented trade-off of the mode (the batch path keeps its pending-state
persistence and recovery machinery).
"""
from __future__ import annotations

import time
from collections.abc import Hashable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from ..core.api_config import (
    LARGE_REVIEW_INPUT_THRESHOLD,
    REVIEW_MODEL_DEFAULT,
    model_supports_extended_output_beta,
    realtime_review_max_workers,
)
from ..core.code_cycles import CodeCycle, DEFAULT_CYCLE
from ..tracing import capture_hooks as _trace
from ..verification.retry_policy import (
    DEFAULT_REALTIME_RETRY_POLICY,
    FailureClass,
    classify_exception,
    compute_backoff_seconds,
    is_retryable_failure_class,
)
from .review_request_builder import (
    BuiltReviewRequest,
    RETRY_TRUNCATED_REVIEW_INSTRUCTION,
    ReviewRequestSpec,
    build_review_request,
    estimate_local_request_tokens,
)
from .reviewer import ReviewResult, _get_client, review_result_from_message

LogFn = Callable[..., None]
ProgressFn = Callable[..., None]

# ``BatchJob.batch_id`` stub value for a real-time run. The pipeline keeps
# ``BatchSubmission`` as the single state object across both transports; a
# real-time submission carries a local job stub with this sentinel id so
# ``collect``/``finalize`` reuse ``request_map``/``created_at`` unchanged.
# Branch on ``BatchSubmission.review_transport``, never on this id.
REALTIME_JOB_SENTINEL = "realtime"


def _noop_log(_msg: str, **_kwargs: object) -> None: return


def _noop_progress(_: float, __: str, **_kwargs: object) -> None: return


@dataclass
class _SpecReviewOutcome:
    """One spec's terminal review outcome, carried from worker to coordinator.

    ``telemetry`` is a list of ``DiagnosticsReport.record_api_call`` kwarg
    dicts — one per API call actually made (initial attempt(s) that produced
    a response, plus the optional repair call) or one synthesized error row
    when no response was ever received. Workers run on pool threads and must
    not touch ``log`` / ``diagnostics`` directly; the coordinator applies
    these rows as each future completes.
    """

    job_key: Hashable
    custom_id: str
    filename: str
    display_name: str
    result: ReviewResult
    telemetry: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class RealtimeReviewJob:
    """One independently-routable review request in a shared worker pool.

    ``job_key`` is deliberately opaque and separate from ``custom_id``.
    Child batches mint ``custom_id`` values in their own namespace, so two
    routed modules may both contain ``review__foo__0``.  A program-level
    caller can use ``(module_id, custom_id)`` as the job key, execute all
    modules in one pool, and regroup the results without changing the child
    request ids that downstream collection expects.

    ``trace_parent`` is explicit per job because executor threads do not
    inherit the submitter's tracing context.  API-call spans therefore stay
    attached to the correct child pipeline even when module jobs interleave.
    """

    job_key: Hashable
    custom_id: str
    filename: str
    request_spec: ReviewRequestSpec
    trace_parent: object | None = None
    display_name: str = ""


@dataclass(frozen=True)
class _PreparedRealtimeReviewJob:
    """A job whose initial and repair request bytes are fully materialized."""

    job: RealtimeReviewJob
    built: BuiltReviewRequest
    repair_built: BuiltReviewRequest


def build_realtime_review_jobs(
    specs: Sequence,
    *,
    project_context: str = "",
    model: str = REVIEW_MODEL_DEFAULT,
    cycle: CodeCycle = DEFAULT_CYCLE,
    pre_detected_alerts: dict[str, list[dict]] | None = None,
    job_key_factory: Callable[[str, int], Hashable] | None = None,
    trace_parent=None,
    display_name_factory: Callable[[str, int], str] | None = None,
) -> tuple[list[RealtimeReviewJob], dict[str, dict]]:
    """Describe review jobs without constructing a client or spending.

    The returned ``request_map`` is child-batch compatible.  Callers may
    concatenate jobs from multiple module partitions and pass the combined
    list to :func:`run_realtime_review_jobs`; ``job_key_factory`` supplies
    the collision-free program namespace while each child keeps its original
    ``custom_id``.

    Request payloads are materialized by the execution function so *every*
    job in the combined list can be built and preflighted before the client is
    constructed and before the first paid call starts.
    """
    if not specs:
        raise ValueError("No specs to submit for real-time review")

    jobs: list[RealtimeReviewJob] = []
    request_map: dict[str, dict] = {}
    seen_job_keys: set[Hashable] = set()
    for idx, spec in enumerate(specs):
        custom_id = _review_custom_id_for(spec.filename, idx)
        job_key = (
            job_key_factory(custom_id, idx)
            if job_key_factory is not None
            else idx
        )
        try:
            hash(job_key)
        except TypeError as exc:
            raise TypeError("Real-time review job_key values must be hashable") from exc
        if job_key in seen_job_keys:
            raise ValueError(f"Duplicate real-time review job_key: {job_key!r}")
        seen_job_keys.add(job_key)

        spec_alerts = (
            pre_detected_alerts.get(spec.filename) if pre_detected_alerts else None
        )
        request_spec = ReviewRequestSpec(
            spec_content=spec.content,
            filename=spec.filename,
            model=model,
            cycle=cycle,
            project_context=project_context,
            paragraph_map=spec.paragraph_map,
            pre_detected_alerts=spec_alerts,
            # Real-time pins: extended 300k output is batch-only by API
            # design; ``service_tier`` is a batch-path knob.
            force_allow_extended_output=False,
            include_service_tier=False,
        )
        display_name = (
            display_name_factory(spec.filename, idx)
            if display_name_factory is not None
            else spec.filename
        )
        jobs.append(
            RealtimeReviewJob(
                job_key=job_key,
                custom_id=custom_id,
                filename=spec.filename,
                request_spec=request_spec,
                trace_parent=trace_parent,
                display_name=display_name,
            )
        )
        request_map[custom_id] = {
            "filename": spec.filename,
            "index": idx,
            "type": "review",
        }
    return jobs, request_map


def _prepare_realtime_review_jobs(
    jobs: Sequence[RealtimeReviewJob],
) -> list[_PreparedRealtimeReviewJob]:
    """Build and preflight the complete fan-out before any paid work.

    Both the ordinary request and its possible one-shot repair are built up
    front.  A builder/preflight failure in a later module therefore cannot
    occur after an earlier module has already started streaming.
    """
    if not jobs:
        raise ValueError("No jobs to submit for real-time review")

    prepared: list[_PreparedRealtimeReviewJob] = []
    oversized: list[tuple[str, int]] = []
    seen_job_keys: set[Hashable] = set()
    for job in jobs:
        try:
            hash(job.job_key)
        except TypeError as exc:
            raise TypeError("Real-time review job_key values must be hashable") from exc
        if job.job_key in seen_job_keys:
            raise ValueError(f"Duplicate real-time review job_key: {job.job_key!r}")
        seen_job_keys.add(job.job_key)
        built = build_review_request(job.request_spec)
        repair_spec = replace(
            job.request_spec,
            retry_instruction=RETRY_TRUNCATED_REVIEW_INSTRUCTION,
        )
        repair_built = build_review_request(repair_spec)
        prepared.append(
            _PreparedRealtimeReviewJob(
                job=job,
                built=built,
                repair_built=repair_built,
            )
        )

        if not model_supports_extended_output_beta(job.request_spec.model):
            continue
        # The one-shot repair appends an instruction to the user message, so
        # it is a distinct request for the real-time 200k safety gate.  Check
        # both shapes before constructing the client; otherwise an initial
        # request just below the threshold could spend successfully and only
        # then discover that its repair requires the batch-only 300k path.
        estimate = max(
            estimate_local_request_tokens(job.request_spec),
            estimate_local_request_tokens(repair_spec),
        )
        if estimate >= LARGE_REVIEW_INPUT_THRESHOLD:
            oversized.append((job.display_name or job.filename, estimate))
    if oversized:
        names = "; ".join(f"{name} (~{estimate:,} tokens)" for name, estimate in oversized)
        raise ValueError(
            f"{len(oversized)} spec(s) are too large for real-time review: {names}. "
            f"Inputs at or above {LARGE_REVIEW_INPUT_THRESHOLD:,} tokens need the "
            "300k extended-output path, which is batch-only by API design — run "
            "this project in batch mode (the default), or split the spec."
        )
    return prepared


def _telemetry_row(
    result: ReviewResult,
    *,
    model: str,
    filename: str,
    retry_status: str,
    max_output_tokens: int = 0,
) -> dict:
    ok = result.parse_status == "ok" and not result.error
    return {
        "phase": "review",
        "model": model,
        "mode": "realtime",
        "retry_status": retry_status,
        "message": f"{filename}: parse_status={result.parse_status or 'error'}",
        "level": "info" if ok else "error",
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_creation_input_tokens": result.cache_creation_input_tokens,
        "cache_read_input_tokens": result.cache_read_input_tokens,
        "stop_reason": result.stop_reason,
        "max_output_tokens": max_output_tokens,
        "structured_payload": result.structured_payload,
        "extra": {
            "filename": filename,
            "parse_status": result.parse_status,
            "error": result.error,
            "elapsed_seconds": round(result.elapsed_seconds, 1),
        },
    }


def _open_review_api_span(trace_parent, *, filename: str, model: str, attempt: int, repair: bool = False):
    """Open one api_call span under the pipeline span (cross-check's pattern)."""
    recorder = _trace._get()
    if recorder is None or trace_parent is None:
        return None
    try:
        from ..tracing.spans import KIND_API_CALL

        label = (
            f"api_call: review {filename} (repair)"
            if repair
            else f"api_call: review {filename} (attempt {attempt})"
        )
        return recorder.open_span(
            KIND_API_CALL,
            label,
            parent=trace_parent,
            inputs={
                "phase": "review",
                "mode": "realtime",
                "model": model,
                "filename": filename,
                "attempt": attempt,
                "repair": repair,
            },
        )
    except Exception:
        return None


def _close_review_api_span(handle, result: ReviewResult | None, *, source: str, status: str = "ok", error: str | None = None) -> None:
    if handle is None:
        return
    recorder = _trace._get()
    if recorder is None:
        return
    try:
        outputs: dict[str, Any] = {"source": source}
        if result is not None:
            outputs.update(
                parse_status=result.parse_status,
                stop_reason=result.stop_reason,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                finding_count=len(result.findings),
            )
        recorder.close_span(handle, outputs=outputs, status=status, error=error)
    except Exception:
        pass


def _stream_review_call(client, built, *, model: str, trace_api) -> ReviewResult:
    """One streaming Messages call → classified ``ReviewResult``.

    Streaming is required at this size: the review cap (128k output) is far
    past the SDK's non-streaming ceiling. The review request carries no
    server tools, so there is no ``pause_turn`` loop — the stream ends in a
    single turn and classifies through the shared
    ``review_result_from_message`` core.
    """
    call_start = time.time()
    with client.messages.stream(**built.params) as stream:
        for text in stream.text_stream:
            _trace.capture_stream_chunk(trace_api, text)
        resp = stream.get_final_message()
    _trace.capture_response_content_blocks(trace_api, resp)
    result = review_result_from_message(resp, model=model)
    result.elapsed_seconds = time.time() - call_start
    _trace.capture_parse_attempt(
        trace_api,
        status="ok" if result.parse_status == "ok" else str(result.parse_status),
        source="structured" if result.structured_payload is not None else "text",
    )
    return result


def _review_one_spec(
    client,
    prepared_job: _PreparedRealtimeReviewJob,
) -> _SpecReviewOutcome:
    """One spec's full real-time review lifecycle. Never raises.

    Transport retries follow the shared realtime policy (retryable classes
    back off and re-attempt; non-retryable classes terminate immediately).
    A completed-but-truncated/unparseable response gets exactly one inline
    repair call (batch repair parity) before the better of the two results
    is returned. Every terminal path returns a ``ReviewResult`` — exceptions
    become error results so one spec's crash never takes down the fan-out.
    """
    job = prepared_job.job
    custom_id = job.custom_id
    filename = job.filename
    display_name = job.display_name or filename
    model = job.request_spec.model
    trace_parent = job.trace_parent
    policy = DEFAULT_REALTIME_RETRY_POLICY
    attempts_planned = max(1, policy.max_attempts)
    last_failure_class: FailureClass | None = None
    telemetry: list[dict] = []
    built = prepared_job.built
    max_output = int(built.params.get("max_tokens") or 0)

    for attempt in range(attempts_planned):
        is_last_attempt = attempt == attempts_planned - 1
        trace_api = _open_review_api_span(
            trace_parent, filename=filename, model=model, attempt=attempt + 1
        )
        try:
            result = _stream_review_call(client, built, model=model, trace_api=trace_api)
            telemetry.append(
                _telemetry_row(
                    result,
                    model=model,
                    filename=filename,
                    retry_status="initial" if attempt == 0 else "retry",
                    max_output_tokens=max_output,
                )
            )
            if result.parse_status in ("incomplete", "parse_error"):
                # Inline repair — parity with the batch second-batch repair
                # pass: one instructed retry, then the spec is allowed to
                # surface as failed. ``replace`` keeps every other request
                # input byte-identical.
                _close_review_api_span(trace_api, result, source=str(result.parse_status), status="error", error=result.error)
                repair_built = prepared_job.repair_built
                trace_repair = _open_review_api_span(
                    trace_parent, filename=filename, model=model, attempt=attempt + 1, repair=True
                )
                try:
                    repair_result = _stream_review_call(
                        client, repair_built, model=model, trace_api=trace_repair
                    )
                    telemetry.append(
                        _telemetry_row(
                            repair_result,
                            model=model,
                            filename=filename,
                            retry_status="retry",
                            max_output_tokens=max_output,
                        )
                    )
                    if repair_result.parse_status == "ok":
                        _close_review_api_span(trace_repair, repair_result, source="repair_ok")
                        result = repair_result
                    else:
                        _close_review_api_span(
                            trace_repair, repair_result, source="repair_failed",
                            status="error", error=repair_result.error,
                        )
                except (KeyboardInterrupt, SystemExit):
                    _close_review_api_span(trace_repair, None, source="interrupt", status="error", error="interrupted")
                    raise
                except Exception as repair_exc:  # noqa: BLE001 — keep the original truncated result
                    _close_review_api_span(trace_repair, None, source="repair_exception", status="error", error=str(repair_exc))
                return _SpecReviewOutcome(
                    job_key=job.job_key,
                    custom_id=custom_id,
                    filename=filename,
                    display_name=display_name,
                    result=result,
                    telemetry=telemetry,
                )
            _close_review_api_span(trace_api, result, source="ok")
            return _SpecReviewOutcome(
                job_key=job.job_key,
                custom_id=custom_id,
                filename=filename,
                display_name=display_name,
                result=result,
                telemetry=telemetry,
            )
        except (KeyboardInterrupt, SystemExit):
            _close_review_api_span(trace_api, None, source="interrupt", status="error", error="interrupted")
            raise
        except Exception as e:  # noqa: BLE001 — classified below, never re-raised
            failure_class = classify_exception(e)
            last_failure_class = failure_class
            _close_review_api_span(
                trace_api, None,
                source="non_retryable" if not is_retryable_failure_class(failure_class) else "will_retry",
                status="error", error=str(e),
            )
            if not is_retryable_failure_class(failure_class):
                result = ReviewResult(
                    findings=[],
                    model=model,
                    error=f"Real-time review failed ({failure_class.value}): {e}",
                )
                telemetry.append(
                    _telemetry_row(
                        result, model=model, filename=filename,
                        retry_status="initial" if attempt == 0 else "retry",
                        max_output_tokens=max_output,
                    )
                )
                return _SpecReviewOutcome(
                    job_key=job.job_key,
                    custom_id=custom_id,
                    filename=filename,
                    display_name=display_name,
                    result=result,
                    telemetry=telemetry,
                )
            if is_last_attempt:
                continue
            backoff = compute_backoff_seconds(policy, attempt=attempt, failure_class=failure_class)
            _trace.capture_retry(
                trace_parent, attempt=attempt + 1,
                failure_class=failure_class.value, backoff_seconds=backoff,
            )
            time.sleep(backoff)

    suffix = f" (class={last_failure_class.value})" if last_failure_class is not None else ""
    result = ReviewResult(
        findings=[],
        model=model,
        error=f"Real-time review failed after {attempts_planned} attempts{suffix}.",
    )
    telemetry.append(
        _telemetry_row(
            result, model=model, filename=filename, retry_status="retry",
            max_output_tokens=max_output,
        )
    )
    return _SpecReviewOutcome(
        job_key=job.job_key,
        custom_id=custom_id,
        filename=filename,
        display_name=display_name,
        result=result,
        telemetry=telemetry,
    )


def run_realtime_review_jobs(
    jobs: Sequence[RealtimeReviewJob],
    *,
    max_workers: Optional[int] = None,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    diagnostics=None,
) -> dict[Hashable, ReviewResult]:
    """Execute heterogeneous jobs through one bounded streaming pool.

    The coordinator fully materializes and preflights *all* requests (and
    their possible repair requests) before constructing the Anthropic client.
    Worker threads only perform API/retry/parse work; logging, progress, and
    diagnostics mutation happen here on the coordinator thread.

    Results are keyed by each job's opaque ``job_key``.  ``custom_id`` remains
    untouched as child-batch identity and may legitimately repeat between
    jobs belonging to different routed modules.
    """
    prepared = _prepare_realtime_review_jobs(jobs)
    # Load-bearing ordering: do not move client construction above the full
    # build/preflight above.  A late invalid/oversize program partition must
    # abort before any earlier partition can incur review spend.
    client = _get_client()
    configured = max_workers if max_workers is not None else realtime_review_max_workers()
    workers = max(1, min(int(configured), len(prepared)))
    total = len(prepared)
    log(
        f"Real-time review: streaming {total} review request(s) on "
        f"{workers} concurrent worker(s)...",
        level="step",
    )

    results: dict[Hashable, ReviewResult] = {}
    done = 0
    # Aggregate per explicit trace parent so every child pipeline receives
    # its own completion note even though execution used one global pool.
    trace_groups: dict[int, dict[str, Any]] = {}
    trace_parent_by_job_key: dict[Hashable, object | None] = {}
    for item in prepared:
        parent = item.job.trace_parent
        trace_parent_by_job_key[item.job.job_key] = parent
        key = id(parent)
        group = trace_groups.setdefault(
            key,
            {"parent": parent, "spec_count": 0, "failed": 0},
        )
        group["spec_count"] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_review_one_spec, client, item): item.job.job_key
            for item in prepared
        }
        for future in as_completed(futures):
            outcome = future.result()  # workers never raise ordinary failures
            results[outcome.job_key] = outcome.result
            done += 1
            if diagnostics is not None:
                for row in outcome.telemetry:
                    try:
                        diagnostics.record_api_call(**row)
                    except Exception:
                        pass
            rr = outcome.result
            if rr.parse_status == "ok" and not rr.error:
                log(
                    f"  {outcome.display_name}: {len(rr.findings)} finding(s) "
                    f"({rr.elapsed_seconds:.0f}s)",
                    level="info",
                )
            else:
                parent_group = trace_groups[
                    id(trace_parent_by_job_key[outcome.job_key])
                ]
                parent_group["failed"] += 1
                log(
                    f"  {outcome.display_name}: review failed — "
                    f"{rr.error or rr.parse_status}",
                    level="warning",
                )
            progress(
                done / total * 100.0,
                f"Completed {done}/{total} review requests",
            )

    for group in trace_groups.values():
        _trace.capture_note(
            group["parent"],
            "realtime review completed",
            spec_count=group["spec_count"],
            failed=group["failed"],
            workers=min(workers, group["spec_count"]),
            global_workers=workers,
        )
    return results


def run_realtime_review(
    specs: list,
    *,
    project_context: str = "",
    model: str = REVIEW_MODEL_DEFAULT,
    cycle: CodeCycle = DEFAULT_CYCLE,
    pre_detected_alerts: dict[str, list[dict]] | None = None,
    max_workers: Optional[int] = None,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    diagnostics=None,
    _trace_parent=None,
) -> tuple[dict[str, ReviewResult], dict[str, dict]]:
    """Review one module while preserving the historical public contract.

    This is now a thin compatibility wrapper over the heterogeneous core.
    Returned result and request-map keys remain byte-for-byte identical to
    ``batch.submit_review_batch``.
    """
    jobs, request_map = build_realtime_review_jobs(
        specs,
        project_context=project_context,
        model=model,
        cycle=cycle,
        pre_detected_alerts=pre_detected_alerts,
        trace_parent=_trace_parent,
    )
    by_job_key = run_realtime_review_jobs(
        jobs,
        max_workers=max_workers,
        log=log,
        progress=progress,
        diagnostics=diagnostics,
    )
    results = {job.custom_id: by_job_key[job.job_key] for job in jobs}
    return results, request_map


def _review_custom_id_for(filename: str, idx: int) -> str:
    """Mint the review custom id — same minting as the batch transport.

    Delegates to ``batch._review_custom_id`` so the two transports can never
    drift on id shape (the collect loop, the repair-map, and the bare-batch
    recovery all parse this format). Function-local import: ``batch``
    imports from ``review.reviewer``, so importing it lazily here keeps the
    module graph acyclic no matter how either package evolves.
    """
    from ..batch.batch import _review_custom_id

    return _review_custom_id(filename, idx)
