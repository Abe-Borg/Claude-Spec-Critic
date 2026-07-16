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
``api_config.realtime_review_max_workers()`` (default 2 — review streams
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

    custom_id: str
    filename: str
    result: ReviewResult
    telemetry: list[dict] = field(default_factory=list)


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
    request_spec: ReviewRequestSpec,
    *,
    custom_id: str,
    filename: str,
    model: str,
    trace_parent=None,
) -> _SpecReviewOutcome:
    """One spec's full real-time review lifecycle. Never raises.

    Transport retries follow the shared realtime policy (retryable classes
    back off and re-attempt; non-retryable classes terminate immediately).
    A completed-but-truncated/unparseable response gets exactly one inline
    repair call (batch repair parity) before the better of the two results
    is returned. Every terminal path returns a ``ReviewResult`` — exceptions
    become error results so one spec's crash never takes down the fan-out.
    """
    policy = DEFAULT_REALTIME_RETRY_POLICY
    attempts_planned = max(1, policy.max_attempts)
    last_failure_class: FailureClass | None = None
    telemetry: list[dict] = []
    built = build_review_request(request_spec)
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
                repair_built = build_review_request(
                    replace(request_spec, retry_instruction=RETRY_TRUNCATED_REVIEW_INSTRUCTION)
                )
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
                    custom_id=custom_id, filename=filename, result=result, telemetry=telemetry
                )
            _close_review_api_span(trace_api, result, source="ok")
            return _SpecReviewOutcome(
                custom_id=custom_id, filename=filename, result=result, telemetry=telemetry
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
                    custom_id=custom_id, filename=filename, result=result, telemetry=telemetry
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
    return _SpecReviewOutcome(custom_id=custom_id, filename=filename, result=result, telemetry=telemetry)


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
    """Review every spec through the synchronous streaming transport.

    Returns ``(results_by_custom_id, request_map)`` with keying identical to
    ``batch.submit_review_batch``: ``custom_id = _review_custom_id(filename,
    idx)`` and ``request_map[custom_id] = {"filename", "index", "type":
    "review"}`` — so the caller can hang both on a ``BatchSubmission`` (with
    a local job stub) and reuse the batch collect path unchanged.

    ``progress`` receives the 0-100 completion percentage of the fan-out
    plus a human line; the pipeline maps it into its own progress band.
    ``diagnostics`` is an optional duck-typed ``DiagnosticsReport``; one
    ``record_api_call(mode="realtime")`` row lands per API call made.
    """
    if not specs:
        raise ValueError("No specs to submit for real-time review")

    # Build one frozen request spec per input spec — the same construction
    # ``submit_review_batch`` performs, minus the batch-only knobs.
    prepared: list[tuple[str, Any, ReviewRequestSpec]] = []
    request_map: dict[str, dict] = {}
    for idx, spec in enumerate(specs):
        custom_id = _review_custom_id_for(spec.filename, idx)
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
        prepared.append((custom_id, spec, request_spec))
        request_map[custom_id] = {"filename": spec.filename, "index": idx, "type": "review"}

    # Preflight gate, before any spend: refuse inputs the batch transport
    # would have lifted to 300k output. Mirrors ``_resolve_extended_output``
    # (local count vs LARGE_REVIEW_INPUT_THRESHOLD, on beta-whitelisted
    # models only) so the gate fires exactly where batch genuinely offers
    # more output headroom. "Preflight raises, not warns."
    if model_supports_extended_output_beta(model):
        oversized = []
        for _cid, spec, request_spec in prepared:
            estimate = estimate_local_request_tokens(request_spec)
            if estimate >= LARGE_REVIEW_INPUT_THRESHOLD:
                oversized.append((spec.filename, estimate))
        if oversized:
            names = "; ".join(f"{fn} (~{est:,} tokens)" for fn, est in oversized)
            raise ValueError(
                f"{len(oversized)} spec(s) are too large for real-time review: {names}. "
                f"Inputs at or above {LARGE_REVIEW_INPUT_THRESHOLD:,} tokens need the "
                "300k extended-output path, which is batch-only by API design — run "
                "this project in batch mode (the default), or split the spec."
            )

    client = _get_client()
    configured = max_workers if max_workers is not None else realtime_review_max_workers()
    workers = max(1, min(int(configured), len(prepared)))
    total = len(prepared)
    log(
        f"Real-time review: streaming {total} spec(s) on {workers} worker(s)...",
        level="step",
    )

    results: dict[str, ReviewResult] = {}
    failed = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _review_one_spec,
                client,
                request_spec,
                custom_id=custom_id,
                filename=spec.filename,
                model=model,
                trace_parent=_trace_parent,
            ): custom_id
            for custom_id, spec, request_spec in prepared
        }
        for future in as_completed(futures):
            outcome = future.result()  # workers never raise
            results[outcome.custom_id] = outcome.result
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
                    f"  {outcome.filename}: {len(rr.findings)} finding(s) "
                    f"({rr.elapsed_seconds:.0f}s)",
                    level="info",
                )
            else:
                failed += 1
                log(
                    f"  {outcome.filename}: review failed — {rr.error or rr.parse_status}",
                    level="warning",
                )
            progress(done / total * 100.0, f"Reviewed {done}/{total} specs")

    _trace.capture_note(
        _trace_parent,
        "realtime review completed",
        spec_count=total,
        failed=failed,
        workers=workers,
    )
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
