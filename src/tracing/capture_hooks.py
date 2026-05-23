"""Capture-hook shims.

The integration surface. Every call site in the rest of the codebase goes
through one of these functions. Each hook:

    1. Calls ``get_recorder()`` and returns immediately if no recorder is
       installed.
    2. Wraps the recorder call in ``try/except Exception`` so a tracing
       failure never escapes into pipeline code.
    3. Logs the first failure of each (exception type, first frame) pair
       once via ``_log_once`` — repeated failures are silent.

Capture sites should treat the return values as fire-and-forget. Hooks
that "open a span" return a ``SpanHandle | None``; ``None`` means
tracing is disabled and the caller should not pass the handle to
``close_*`` hooks (the close hooks also tolerate ``None`` for symmetry).
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .recorder import (
    SpanHandle,
    TraceRecorder,
    current_span,
    get_recorder,
)
from .spans import (
    EVENT_BUDGET_EXHAUSTED,
    EVENT_CACHE_HIT,
    EVENT_CACHE_MISS,
    EVENT_CONTINUATION_RESUME,
    EVENT_ESCALATION_DECISION,
    EVENT_GROUNDING_OUTCOME,
    EVENT_NOTE,
    EVENT_PARSE_ATTEMPT,
    EVENT_PAUSE_TURN,
    EVENT_RETRY,
    EVENT_STREAM_CHUNK,
    EVENT_THINKING_BLOCK,
    EVENT_TOOL_RESULT,
    EVENT_TOOL_USE,
    EVENT_WEB_FETCH_REQUEST,
    EVENT_WEB_FETCH_RESULT,
    EVENT_WEB_SEARCH_QUERY,
    EVENT_WEB_SEARCH_RESULT,
    KIND_API_CALL,
    KIND_CROSS_CHECK,
    KIND_CROSS_CHECK_CHUNK,
    KIND_EXTRACTION,
    KIND_PIPELINE,
    KIND_REVIEW,
    KIND_TRIAGE,
    KIND_VERIFICATION_CONTINUATION,
    KIND_VERIFICATION_ESCALATION,
    KIND_VERIFICATION_INITIAL,
    KIND_VERIFICATION_RETRY,
    KIND_WEB_SEARCH,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
)

_log = logging.getLogger(__name__)

# One-shot warning suppression keyed by (exc-type-name, first-frame
# filename:lineno). A pathological capture site that fails on every call
# logs once and stays quiet.
_LOG_ONCE_SEEN: set[tuple[str, str]] = set()
_LOG_ONCE_LOCK = threading.Lock()


def _log_once(message: str, *, exc: BaseException) -> None:
    tb = exc.__traceback__
    frame_key = ""
    while tb is not None:
        frame_key = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}"
        tb = tb.tb_next
    key = (type(exc).__name__, frame_key)
    with _LOG_ONCE_LOCK:
        if key in _LOG_ONCE_SEEN:
            return
        _LOG_ONCE_SEEN.add(key)
    _log.warning("%s: %s (further occurrences suppressed)", message, exc)


def _safe(fn):
    """Decorator that swallows exceptions and routes them through _log_once."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            _log_once(f"tracing capture {fn.__name__} failed", exc=exc)
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _get() -> TraceRecorder | None:
    return get_recorder()


# ---- Pipeline-level hooks ----------------------------------------------
@_safe
def capture_pipeline_start(
    *,
    mode: str,
    model: str,
    cycle_label: str,
    files: list[str],
    name: str = "",
) -> SpanHandle | None:
    """Open the root pipeline span. Call once at run entry."""
    recorder = _get()
    if recorder is None:
        return None
    span_name = name or f"pipeline: {mode}"
    return recorder.open_span(
        KIND_PIPELINE,
        span_name,
        inputs={
            "mode": mode,
            "model": model,
            "cycle_label": cycle_label,
            "files": list(files),
        },
        metadata={"run_id": recorder.run_id},
    )


@_safe
def capture_pipeline_end(
    handle: SpanHandle | None,
    *,
    success: bool,
    summary: dict[str, Any] | None = None,
) -> None:
    recorder = _get()
    if recorder is None or handle is None:
        return
    recorder.close_span(
        handle,
        outputs={"success": success, "summary": summary or {}},
        status=STATUS_OK if success else STATUS_ERROR,
    )


@_safe
def capture_pipeline_end_by_id(
    span_id: str,
    *,
    success: bool,
    summary: dict[str, Any] | None = None,
) -> None:
    """Close a pipeline span when only the span_id is available.

    Used by batch mode: ``start_batch_review`` opens the span and carries
    the ID through ``BatchSubmission`` / ``CollectedBatchState`` to
    ``finalize_batch_result``, which closes it via this helper. The
    SpanHandle isn't conveniently storable on those dataclasses (clean
    JSON shape for resume_state).
    """
    recorder = _get()
    if recorder is None or not span_id:
        return
    handle = SpanHandle(span_id=span_id, kind="pipeline", started_at=0.0)
    recorder.close_span(
        handle,
        outputs={"success": success, "summary": summary or {}},
        status=STATUS_OK if success else STATUS_ERROR,
    )


@_safe
def capture_note(handle: SpanHandle | None, message: str, **fields: Any) -> None:
    """Stamp a free-form note event on the given span (or the current one)."""
    recorder = _get()
    if recorder is None:
        return
    target = handle if handle is not None else current_span()
    recorder.add_event(target, EVENT_NOTE, message=message, **fields)


# ---- Extraction --------------------------------------------------------
@_safe
def capture_extraction_span(
    *,
    filename: str,
    parent: SpanHandle | None = None,
) -> SpanHandle | None:
    recorder = _get()
    if recorder is None:
        return None
    return recorder.open_span(
        KIND_EXTRACTION,
        f"extract: {filename}",
        parent=parent,
        inputs={"filename": filename},
    )


@_safe
def capture_extraction_end(
    handle: SpanHandle | None,
    *,
    success: bool,
    paragraph_count: int = 0,
    extraction_warnings: list[str] | None = None,
    error: str | None = None,
) -> None:
    recorder = _get()
    if recorder is None or handle is None:
        return
    recorder.close_span(
        handle,
        outputs={
            "paragraph_count": paragraph_count,
            "extraction_warnings": list(extraction_warnings or []),
        },
        status=STATUS_OK if success else STATUS_ERROR,
        error=error,
    )


# ---- Review ------------------------------------------------------------
@_safe
def capture_review_call(
    *,
    filename: str,
    model: str,
    cycle_label: str,
    system_prompt: str = "",
    user_message: str = "",
    paragraph_map_summary: dict[str, Any] | None = None,
    pre_detected_alerts_count: int = 0,
    parent: SpanHandle | None = None,
) -> SpanHandle | None:
    recorder = _get()
    if recorder is None:
        return None
    inputs: dict[str, Any] = {
        "filename": filename,
        "model": model,
        "cycle_label": cycle_label,
        "paragraph_map_summary": paragraph_map_summary or {},
        "pre_detected_alerts_count": pre_detected_alerts_count,
    }
    if system_prompt:
        inputs["system_prompt"] = recorder.prompt_ref("review_system", system_prompt)
    if user_message:
        inputs["user_message"] = recorder.prompt_ref("review_user", user_message)
    return recorder.open_span(
        KIND_REVIEW,
        f"review: {filename}",
        parent=parent,
        inputs=inputs,
    )


@_safe
def capture_review_end(
    handle: SpanHandle | None,
    *,
    finding_count: int,
    parse_status: str,
    stop_reason: str | None,
    structured_payload: dict[str, Any] | None = None,
    raw_response: str = "",
    thinking_text: str = "",
    findings_summary: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    recorder = _get()
    if recorder is None or handle is None:
        return
    outputs: dict[str, Any] = {
        "finding_count": finding_count,
        "parse_status": parse_status,
        "stop_reason": stop_reason,
        "structured_payload": structured_payload,
        "findings": list(findings_summary or []),
    }
    if recorder.is_deep:
        outputs["raw_response"] = raw_response
        outputs["thinking_text"] = thinking_text
    recorder.close_span(
        handle,
        outputs=outputs,
        status=STATUS_OK if error is None else STATUS_ERROR,
        error=error,
    )


# ---- Cross-check -------------------------------------------------------
@_safe
def capture_cross_check_start(
    *,
    spec_count: int,
    chunked: bool,
    parent: SpanHandle | None = None,
) -> SpanHandle | None:
    recorder = _get()
    if recorder is None:
        return None
    return recorder.open_span(
        KIND_CROSS_CHECK,
        f"cross_check ({spec_count} specs)",
        parent=parent,
        inputs={"spec_count": spec_count, "chunked": chunked},
    )


@_safe
def capture_cross_check_chunk_start(
    *,
    chunk_name: str,
    spec_count: int,
    finding_count: int,
    parent: SpanHandle | None = None,
) -> SpanHandle | None:
    recorder = _get()
    if recorder is None:
        return None
    return recorder.open_span(
        KIND_CROSS_CHECK_CHUNK,
        f"cross_check chunk: {chunk_name}",
        parent=parent,
        inputs={"chunk_name": chunk_name, "spec_count": spec_count, "finding_count": finding_count},
        metadata={"chunk_name": chunk_name},
    )


@_safe
def capture_cross_check_end(
    handle: SpanHandle | None,
    *,
    finding_count: int,
    status: str = "ok",
    error: str | None = None,
) -> None:
    recorder = _get()
    if recorder is None or handle is None:
        return
    recorder.close_span(
        handle,
        outputs={"finding_count": finding_count, "cross_check_status": status},
        status=STATUS_OK if error is None else STATUS_ERROR,
        error=error,
    )


# ---- Verification ------------------------------------------------------
@_safe
def capture_verification_call(
    *,
    finding_id: str,
    routing_decision: dict[str, Any],
    prompt: str = "",
    system_prompt: str = "",
    escalation: bool = False,
    parent: SpanHandle | None = None,
) -> SpanHandle | None:
    recorder = _get()
    if recorder is None:
        return None
    inputs: dict[str, Any] = {
        "finding_id": finding_id,
        "routing_decision": routing_decision,
    }
    if prompt:
        inputs["prompt"] = recorder.prompt_ref(
            "verification_escalation_user" if escalation else "verification_user", prompt
        )
    if system_prompt:
        inputs["system_prompt"] = recorder.prompt_ref(
            "verification_escalation_system" if escalation else "verification_system",
            system_prompt,
        )
    kind = KIND_VERIFICATION_ESCALATION if escalation else KIND_VERIFICATION_INITIAL
    return recorder.open_span(
        kind,
        f"{'verify-escalation' if escalation else 'verify'}: {finding_id}",
        parent=parent,
        inputs=inputs,
        metadata={"finding_id": finding_id, "mode": routing_decision.get("mode") if isinstance(routing_decision, dict) else None},
    )


@_safe
def capture_verification_wave_call(
    *,
    finding_id: str,
    wave_kind: str,  # "retry" | "continuation"
    wave_index: int,
    routing_decision: dict[str, Any],
    parent: SpanHandle | None = None,
) -> SpanHandle | None:
    recorder = _get()
    if recorder is None:
        return None
    kind = (
        KIND_VERIFICATION_RETRY if wave_kind == "retry" else KIND_VERIFICATION_CONTINUATION
    )
    return recorder.open_span(
        kind,
        f"verify-{wave_kind} #{wave_index}: {finding_id}",
        parent=parent,
        inputs={
            "finding_id": finding_id,
            "wave_kind": wave_kind,
            "wave_index": wave_index,
            "routing_decision": routing_decision,
        },
        metadata={"finding_id": finding_id, "wave_kind": wave_kind},
    )


@_safe
def capture_verification_end(
    handle: SpanHandle | None,
    *,
    verification_result: Any = None,
    error: str | None = None,
) -> None:
    """Close a verification span with the full VerificationResult fields.

    Captures all five Chunks 11-13 telemetry fields explicitly so the
    trace can reconstruct VERIFIED_CONTESTED, budget exhaustion, and
    fetched-source grounding without re-walking events.
    """
    recorder = _get()
    if recorder is None or handle is None:
        return
    outputs: dict[str, Any] = {}
    if verification_result is not None:
        outputs = _verification_outputs(verification_result, deep=recorder.is_deep)
    recorder.close_span(
        handle,
        outputs=outputs,
        status=STATUS_OK if error is None else STATUS_ERROR,
        error=error,
    )


@_safe
def capture_escalation_decision(
    parent: SpanHandle | None,
    *,
    fired: bool,
    reason: str,
    initial_verdict: str,
    final_verdict: str | None = None,
    models_disagreed: bool = False,
) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(
        parent if parent is not None else current_span(),
        EVENT_ESCALATION_DECISION,
        fired=fired,
        reason=reason,
        initial_verdict=initial_verdict,
        final_verdict=final_verdict,
        models_disagreed=models_disagreed,
    )


@_safe
def capture_grounding_outcome(
    handle: SpanHandle | None,
    *,
    accepted: list[str],
    rejected: list[str],
    downgraded_to_unverified: bool,
    budget_exhausted: bool = False,
) -> None:
    recorder = _get()
    if recorder is None:
        return
    target = handle if handle is not None else current_span()
    recorder.add_event(
        target,
        EVENT_GROUNDING_OUTCOME,
        accepted=list(accepted),
        rejected=list(rejected),
        downgraded_to_unverified=downgraded_to_unverified,
        budget_exhausted=budget_exhausted,
    )
    if budget_exhausted:
        recorder.add_event(target, EVENT_BUDGET_EXHAUSTED)


# ---- Stream / tool / API-call events -----------------------------------
@_safe
def capture_stream_chunk(handle: SpanHandle | None, text: str) -> None:
    """Per-chunk event. Deep mode only — default mode no-ops to save bytes."""
    recorder = _get()
    if recorder is None or not recorder.is_deep:
        return
    recorder.add_event(handle, EVENT_STREAM_CHUNK, text=text)


@_safe
def capture_thinking_block(handle: SpanHandle | None, text: str) -> None:
    recorder = _get()
    if recorder is None:
        return
    # Default mode keeps the consolidated text on the parent review/verify
    # span (caller does that via capture_review_end / capture_verification_end);
    # the per-block event still fires so the timeline view can render
    # individual thinking moments.
    recorder.add_event(handle, EVENT_THINKING_BLOCK, text=text)


@_safe
def capture_tool_use_block(handle: SpanHandle | None, *, tool_name: str, tool_input: Any, tool_use_id: str | None = None) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(
        handle,
        EVENT_TOOL_USE,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
    )


@_safe
def capture_tool_result_block(
    handle: SpanHandle | None,
    *,
    tool_use_id: str | None,
    content: Any,
    is_error: bool = False,
) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(
        handle,
        EVENT_TOOL_RESULT,
        tool_use_id=tool_use_id,
        content=content,
        is_error=is_error,
    )


@_safe
def capture_web_search_query(handle: SpanHandle | None, *, query: str) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(handle, EVENT_WEB_SEARCH_QUERY, query=query)


@_safe
def capture_web_search_result(
    handle: SpanHandle | None,
    *,
    urls_with_titles: list[dict[str, Any]],
    snippet_bodies_included: bool,
) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(
        handle,
        EVENT_WEB_SEARCH_RESULT,
        urls_with_titles=list(urls_with_titles),
        snippet_bodies_included=snippet_bodies_included,
    )


@_safe
def capture_web_fetch_request(handle: SpanHandle | None, *, url: str) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(handle, EVENT_WEB_FETCH_REQUEST, url=url)


@_safe
def capture_web_fetch_result(
    handle: SpanHandle | None,
    *,
    url: str,
    title: str = "",
    content_preview: str = "",
    is_error: bool = False,
) -> None:
    recorder = _get()
    if recorder is None:
        return
    fields = {"url": url, "title": title, "is_error": is_error}
    if recorder.is_deep:
        fields["content_preview"] = content_preview
    recorder.add_event(handle, EVENT_WEB_FETCH_RESULT, **fields)


@_safe
def capture_parse_attempt(
    handle: SpanHandle | None,
    *,
    status: str,
    source: str,
    payload_preview: str = "",
) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(
        handle,
        EVENT_PARSE_ATTEMPT,
        status=status,
        source=source,
        payload_preview=payload_preview,
    )


@_safe
def capture_pause_turn(handle: SpanHandle | None, *, continuation_count: int) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(handle, EVENT_PAUSE_TURN, continuation_count=continuation_count)


@_safe
def capture_continuation_resume(handle: SpanHandle | None, *, continuation_index: int) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(handle, EVENT_CONTINUATION_RESUME, continuation_index=continuation_index)


@_safe
def capture_retry(
    handle: SpanHandle | None,
    *,
    attempt: int,
    failure_class: str = "",
    backoff_seconds: float = 0.0,
) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.add_event(
        handle,
        EVENT_RETRY,
        attempt=attempt,
        failure_class=failure_class,
        backoff_seconds=backoff_seconds,
    )


# ---- Cache / local-skip ------------------------------------------------
@_safe
def capture_cache_lookup(
    parent: SpanHandle | None,
    *,
    finding_id: str,
    hit: bool,
    cache_status: str,
    cache_entry_age_days: float | None = None,
) -> None:
    recorder = _get()
    if recorder is None:
        return
    target = parent if parent is not None else current_span()
    recorder.add_event(
        target,
        EVENT_CACHE_HIT if hit else EVENT_CACHE_MISS,
        finding_id=finding_id,
        cache_status=cache_status,
        cache_entry_age_days=cache_entry_age_days,
    )


@_safe
def capture_local_skip(
    parent: SpanHandle | None,
    *,
    finding_id: str,
    reason: str,
    requires_elevated_confidence: bool = False,
) -> None:
    recorder = _get()
    if recorder is None:
        return
    target = parent if parent is not None else current_span()
    recorder.add_event(
        target,
        EVENT_NOTE,
        kind="local_skip",
        finding_id=finding_id,
        reason=reason,
        requires_elevated_confidence=requires_elevated_confidence,
    )


# ---- Triage ------------------------------------------------------------
@_safe
def capture_triage_start(
    *,
    finding_count: int,
    model: str,
    parent: SpanHandle | None = None,
) -> SpanHandle | None:
    recorder = _get()
    if recorder is None:
        return None
    return recorder.open_span(
        KIND_TRIAGE,
        f"triage ({finding_count} findings)",
        parent=parent,
        inputs={"finding_count": finding_count, "model": model},
    )


@_safe
def capture_triage_end(
    handle: SpanHandle | None,
    *,
    classifications: dict[int, str],
    error: str | None = None,
) -> None:
    recorder = _get()
    if recorder is None or handle is None:
        return
    web_required = sum(1 for v in classifications.values() if v == "web_required")
    local_skip = sum(1 for v in classifications.values() if v == "local_skip")
    recorder.close_span(
        handle,
        outputs={
            "total": len(classifications),
            "web_required": web_required,
            "local_skip": local_skip,
        },
        status=STATUS_OK if error is None else STATUS_ERROR,
        error=error,
    )


# ---- Finding terminal snapshot ----------------------------------------
@_safe
def capture_finding_terminal(finding: Any) -> None:
    recorder = _get()
    if recorder is None:
        return
    recorder.record_finding_snapshot(finding)


# ---- Response content-block walker ------------------------------------
def _block_attr(block: Any, name: str) -> Any:
    """Tolerant attribute lookup — Anthropic SDK objects expose attrs;
    legacy/mocked variants and the batch-retrieval path may hand back
    plain dicts. Falls back to ``__getitem__`` for dict-shaped blocks."""
    if hasattr(block, name):
        return getattr(block, name)
    if isinstance(block, dict):
        return block.get(name)
    return None


@_safe
def capture_response_content_blocks(handle: SpanHandle | None, response: Any) -> None:
    """Walk an Anthropic response's content blocks and emit trace events.

    Captures every block kind that carries forensic signal:
      - ``thinking`` → ``thinking_block`` event (text)
      - ``tool_use`` → ``tool_use`` event (tool name + input)
      - ``server_tool_use`` (name=web_search) → ``web_search_query`` event
      - ``web_search_tool_result`` → ``web_search_result`` event
        (URL + title pairs; snippet bodies in deep mode)
      - ``server_tool_use`` (name=web_fetch) → ``web_fetch_request`` event
      - ``web_fetch_tool_result`` → ``web_fetch_result`` event

    Defensive: if the response has no ``content``, this is a no-op. Any
    block whose shape doesn't match is skipped silently — better to drop
    one event than crash the trace.
    """
    recorder = _get()
    if recorder is None:
        return
    content = _block_attr(response, "content")
    if content is None and isinstance(response, dict):
        content = response.get("content")
    if not content:
        return
    deep = recorder.is_deep
    for block in content:
        btype = _block_attr(block, "type")
        if btype == "thinking":
            text = _block_attr(block, "thinking") or _block_attr(block, "text") or ""
            recorder.add_event(handle, EVENT_THINKING_BLOCK, text=text)
        elif btype == "tool_use":
            recorder.add_event(
                handle,
                EVENT_TOOL_USE,
                tool_name=_block_attr(block, "name"),
                tool_input=_block_attr(block, "input"),
                tool_use_id=_block_attr(block, "id"),
            )
        elif btype == "server_tool_use":
            name = _block_attr(block, "name") or ""
            tool_input = _block_attr(block, "input")
            if name == "web_search":
                query = ""
                if isinstance(tool_input, dict):
                    query = tool_input.get("query", "") or ""
                recorder.add_event(handle, EVENT_WEB_SEARCH_QUERY, query=query)
            elif name == "web_fetch":
                url = ""
                if isinstance(tool_input, dict):
                    url = tool_input.get("url", "") or ""
                recorder.add_event(handle, EVENT_WEB_FETCH_REQUEST, url=url)
            else:
                # Unknown server tool — log as generic tool_use for visibility.
                recorder.add_event(
                    handle,
                    EVENT_TOOL_USE,
                    tool_name=name,
                    tool_input=tool_input,
                    tool_use_id=_block_attr(block, "id"),
                )
        elif btype == "web_search_tool_result":
            urls = _extract_web_search_urls(_block_attr(block, "content"), deep=deep)
            recorder.add_event(
                handle,
                EVENT_WEB_SEARCH_RESULT,
                urls_with_titles=urls,
                snippet_bodies_included=deep,
                is_error=False,
            )
        elif btype == "web_search_tool_result_error":
            recorder.add_event(handle, EVENT_WEB_SEARCH_RESULT, is_error=True, urls_with_titles=[])
        elif btype == "web_fetch_tool_result":
            fetched = _block_attr(block, "content")
            url = ""
            title = ""
            content_text = ""
            if isinstance(fetched, dict):
                url = fetched.get("url", "") or ""
                title = fetched.get("title", "") or ""
                if deep:
                    body = fetched.get("content")
                    if isinstance(body, str):
                        content_text = body[:8000]
            recorder.add_event(
                handle,
                EVENT_WEB_FETCH_RESULT,
                url=url,
                title=title,
                content_preview=content_text if deep else "",
            )


def _extract_web_search_urls(content: Any, *, deep: bool) -> list[dict[str, Any]]:
    """Pull URL + title + snippet from web_search_tool_result content.

    Snippets are intentionally dropped at default level — they can be
    multi-KB per result and a single verification call may produce 7+
    results. Deep mode keeps a 500-char preview per snippet.
    """
    if not content:
        return []
    out: list[dict[str, Any]] = []
    for item in content:
        item_type = _block_attr(item, "type") or "web_search_result"
        if item_type not in (None, "web_search_result"):
            continue
        url = _block_attr(item, "url") or ""
        title = _block_attr(item, "title") or ""
        entry: dict[str, Any] = {"url": url, "title": title}
        if deep:
            snippet = _block_attr(item, "encrypted_content") or _block_attr(item, "snippet") or ""
            if isinstance(snippet, str):
                entry["snippet_preview"] = snippet[:500]
        out.append(entry)
    return out


# ---- Helpers -----------------------------------------------------------
def _verification_outputs(verification: Any, *, deep: bool) -> dict[str, Any]:
    """Pull the fields we care about off a VerificationResult dataclass.

    Defensive ``getattr`` lookups — if a future field is added/removed,
    the trace gracefully degrades rather than crashing.
    """
    def g(name: str, default: Any = None) -> Any:
        return getattr(verification, name, default)

    out: dict[str, Any] = {
        "verdict": g("verdict", ""),
        "grounded": g("grounded", False),
        "model_used": g("model_used", ""),
        "accepted_sources": list(g("sources", []) or []),
        "rejected_sources": list(g("rejected_sources", []) or []),
        "searched_sources": list(g("searched_sources", []) or []),
        "web_search_requests": int(g("web_search_requests", 0) or 0),
        "successful_source_count": int(g("successful_source_count", 0) or 0),
        # Escalation telemetry
        "escalation_attempted": bool(g("escalation_attempted", False)),
        "escalated": bool(g("escalated", False)),
        "escalation_changed_verdict": bool(g("escalation_changed_verdict", False)),
        "escalation_reason": g("escalation_reason", "") or "",
        "initial_model": g("initial_model", "") or "",
        "initial_verdict": g("initial_verdict", "") or "",
        # Chunk 12 disagreement surfacing
        "models_disagreed": bool(g("models_disagreed", False)),
        "initial_sources": list(g("initial_sources", []) or []),
        # Chunk 11 web_fetch
        "web_fetch_requests": int(g("web_fetch_requests", 0) or 0),
        "fetched_sources": list(g("fetched_sources", []) or []),
        # Chunk 13 budget exhaustion
        "budget_exhausted": bool(g("budget_exhausted", False)),
        # Operational-failure sentinel
        "verification_failed": bool(g("verification_failed", False)),
        # Source-quote evidence (Chunk 2)
        "source_quote": g("source_quote", "") or "",
        # Cache telemetry
        "cache_status": g("cache_status", "none") or "none",
        "cache_entry_created_ts": float(g("cache_entry_created_ts", 0.0) or 0.0),
        # Elevated-confidence flag (Chunk 10)
        "requires_elevated_confidence": bool(g("requires_elevated_confidence", False)),
    }
    # Structured payload — traces are the place to keep the full thing,
    # no 4KB cap like diagnostics applies.
    payload = g("structured_payload", None)
    if payload is not None:
        out["structured_payload"] = payload
    # Retry telemetry
    retry_telemetry = g("retry_telemetry", None)
    if retry_telemetry is not None:
        out["retry_telemetry"] = retry_telemetry
    # Deep mode also gets the raw rationale text if we have it.
    if deep:
        rationale = g("rationale", "") or ""
        if rationale:
            out["rationale"] = rationale
    return out
