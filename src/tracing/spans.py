"""Span and event types for the agent tracing subsystem.

A span represents one logical agent invocation (review, verification call,
batch wave, etc.). Events are point-in-time markers inside a span — text
chunks, tool calls, parse decisions. Spans nest via ``parent_span_id``.

The dataclass is JSON-serializable via ``dataclasses.asdict`` so the
recorder's writer thread can dump each closed span as one ``spans.jsonl``
line. Events ride out to ``events.jsonl`` as they fire and carry their own
``span_id`` for cross-reference.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# Span ID width matches DiagnosticsReport.run_id (12 hex chars) — same shape
# across the codebase so a span_id stamped in a log line looks like a run_id
# until someone scans it.
_SPAN_ID_LEN = 12


def new_span_id() -> str:
    return uuid.uuid4().hex[:_SPAN_ID_LEN]


# ---- Span kinds --------------------------------------------------------
# Stable string constants used as ``AgentSpan.kind``. The viewer keys
# layout/color on these — adding a new kind requires a viewer update.
KIND_PIPELINE = "pipeline"
KIND_EXTRACTION = "extraction"
KIND_REVIEW = "review"
KIND_CROSS_CHECK = "cross_check"
KIND_CROSS_CHECK_CHUNK = "cross_check_chunk"
KIND_RESEARCH = "research"
KIND_RESEARCH_DIMENSION = "research_dimension"
KIND_TRIAGE = "triage"
KIND_VERIFICATION_INITIAL = "verification_initial"
KIND_VERIFICATION_ESCALATION = "verification_escalation"
KIND_VERIFICATION_CONTINUATION = "verification_continuation"
KIND_VERIFICATION_RETRY = "verification_retry"
KIND_API_CALL = "api_call"
KIND_WEB_SEARCH = "web_search"
KIND_PARSE = "parse"
KIND_CACHE_LOOKUP = "cache_lookup"
KIND_LOCAL_SKIP = "local_skip"
KIND_ROUTING_DECISION = "routing_decision"


# ---- Event types -------------------------------------------------------
EVENT_STREAM_CHUNK = "stream_chunk"  # deep level only
EVENT_THINKING_BLOCK = "thinking_block"
EVENT_TOOL_USE = "tool_use"
EVENT_WEB_SEARCH_QUERY = "web_search_query"
EVENT_WEB_SEARCH_RESULT = "web_search_result"
EVENT_WEB_FETCH_REQUEST = "web_fetch_request"
EVENT_WEB_FETCH_RESULT = "web_fetch_result"
EVENT_RETRY = "retry"
EVENT_PAUSE_TURN = "pause_turn"
EVENT_CONTINUATION_RESUME = "continuation_resume"
EVENT_PARSE_ATTEMPT = "parse_attempt"
EVENT_CACHE_HIT = "cache_hit"
EVENT_CACHE_MISS = "cache_miss"
EVENT_CACHE_DIAGNOSTICS = "cache_diagnostics"  # beta prompt-cache divergence report
EVENT_ESCALATION_DECISION = "escalation_decision"
EVENT_GROUNDING_OUTCOME = "grounding_outcome"
EVENT_BUDGET_EXHAUSTED = "budget_exhausted_marker"
EVENT_NOTE = "note"


# ---- Span status -------------------------------------------------------
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"


@dataclass
class AgentSpan:
    """One logical agent invocation.

    Stored in the recorder's open-spans table while running; serialized to
    ``spans.jsonl`` at close. The ``events`` list is in-memory only for
    debugging — events themselves stream to ``events.jsonl`` as they fire,
    so the on-disk span record intentionally omits the events array (the
    viewer joins by ``span_id``).
    """

    span_id: str
    kind: str
    name: str
    run_id: str
    started_at: float
    parent_span_id: str | None = None
    ended_at: float | None = None
    status: str = STATUS_RUNNING
    error: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_jsonl_dict(self) -> dict[str, Any]:
        """JSON-ready dict for the spans.jsonl writer."""
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "name": self.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "error": self.error,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "metadata": self.metadata,
        }


@dataclass
class SpanHandle:
    """Lightweight reference returned to callers.

    Holds just enough to identify the span without giving callers mutable
    access to the underlying AgentSpan. Recorder methods take a SpanHandle
    and look up the AgentSpan internally.
    """

    span_id: str
    kind: str
    started_at: float
    parent_span_id: str | None = None


def make_span(
    *,
    kind: str,
    name: str,
    run_id: str,
    parent_span_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentSpan:
    return AgentSpan(
        span_id=new_span_id(),
        kind=kind,
        name=name,
        run_id=run_id,
        started_at=time.time(),
        parent_span_id=parent_span_id,
        inputs=dict(inputs or {}),
        metadata=dict(metadata or {}),
    )


def make_event(
    *,
    span_id: str,
    type: str,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """JSON-ready event dict for the events.jsonl writer."""
    payload: dict[str, Any] = {
        "ts": time.time(),
        "span_id": span_id,
        "type": type,
    }
    if fields:
        payload.update(fields)
    return payload


# ---- Per-kind I/O contracts (documentation only) -----------------------
# These docstrings describe the expected shape of ``inputs`` / ``outputs``
# for each span kind. The recorder does NOT validate against them — they
# are a contract between capture sites and the HTML viewer.
#
# review.inputs:
#     filename, model, cycle_label, system_prompt | system_prompt_ref,
#     user_message | user_message_ref, tool_schema_name,
#     paragraph_map_summary {element_count, has_ids},
#     pre_detected_alerts_count
# review.outputs:
#     finding_count, parse_status (structured | text_fallback |
#     parse_error | incomplete), stop_reason,
#     structured_payload | None, raw_response (deep only),
#     thinking_text (deep only),
#     findings: [{finding_id, severity, section, issue_preview, ...}]
#
# verification_initial.inputs:
#     finding_id, routing_decision (full dict — mode, profile,
#     web_search_max_uses, web_fetch enabled, model),
#     prompt | prompt_ref, system_prompt | system_prompt_ref
# verification_initial.outputs:
#     verdict, grounded, accepted_sources, rejected_sources,
#     searched_sources, fetched_sources, web_search_requests,
#     web_fetch_requests, escalation_attempted, escalation_reason,
#     initial_verdict, models_disagreed, initial_sources,
#     budget_exhausted, verification_failed, source_quote,
#     structured_payload (no 4KB cap), cache_status, cache_entry_created_ts
#
# verification_escalation.outputs:
#     same shape as verification_initial.outputs plus
#     replaced_initial_verdict: bool
#
# api_call.inputs:
#     phase, model, max_tokens, thinking_config, effort_config,
#     tools: [{name, description_preview}], tool_choice,
#     system_prompt_hash, user_message_hash
# api_call.events:
#     stream_chunk (deep), thinking_block, tool_use,
#     web_search_tool_result, web_fetch_tool_result, pause_turn
#
# web_search.inputs:
#     query (from server_tool_use block input)
# web_search.outputs:
#     url_count, urls_with_titles: [{url, title, snippet_preview}]
