"""One usage record per paid attempt (plan WP-15).

An *attempt* is one request the app sent that the API may have billed: a
review batch item, its repair, a streaming review call, one verification
conversation (an initial call plus its ``pause_turn`` resumes), and so on.
Finding selection and attempt accounting are separate questions. When a
repair replaces a failed review's findings, the failed review was still paid
for, so its attempt stays on the books; a result the app keeps and the spend
it cost are recorded apart.

Every attempt record carries:

* **what** was done — ``operation`` (review, verification, cross-check, …)
  and ``role`` (the primary attempt, a repair, a retry, an escalation, or a
  real-time fallback);
* **how** it ran — ``transport`` (``batch`` requests take the 50% Batches
  discount; ``realtime`` requests pay standard rates) and ``model``;
* **what it used** — token, prompt-cache (with the per-TTL write split), web
  search, and web fetch counts;
* **who it is** — a stable identity: ``batch:<batch id>:<custom id>:<role>``
  for a batch item, ``message:<message id>`` for a request whose response was
  read, and none for a request that raised before any response arrived;
* **whose spend it is** — ``scope``: ``run`` for spend this run caused,
  ``earlier`` for spend billed before this collection started (the primary
  review batch of a resumed run, or a repair batch an earlier collection
  submitted).

**Known, unknown, and unbilled.** A response that was read has *known* usage:
its ``usage`` block. A batch item the API reports as errored, canceled, or
expired has known *zero* usage — the Message Batches API does not bill them.
A request that raised before its response was read, and a batch item whose
result was never read (still processing, unreachable, or missing), has
*unknown* usage: ``usage_known`` is ``False`` and every counter is zero by
construction. Unknown usage is never priced and never shown as zero; the
cost summary counts it and says the estimate excludes it.

**One billing input per operation.** A diagnostics record is priced either
from its attempt records or from an aggregate (a pass that records one
combined total, such as cross-check), never both. When attempts are present,
the record's flat token fields are display only.

Stdlib and ``core.api_config`` only, so every producer — the reviewer, the
verifier, the pipeline, diagnostics — can build the same record without an
import cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping

from .api_config import (
    CACHE_BREAKDOWN_NONE,
    CACHE_BREAKDOWN_STATUS_KEY,
    cache_usage_from,
    empty_cache_usage,
    merge_cache_usage,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

OPERATION_REVIEW = "review"
OPERATION_VERIFICATION = "verification"
OPERATION_CROSS_CHECK = "cross_check"
OPERATION_COMPLIANCE = "compliance"
OPERATION_DRAWING_IMPACT = "drawing_impact"
OPERATION_RESEARCH = "research"
OPERATION_DRAWING_DIGEST = "drawing_digest"
OPERATION_TRIAGE = "triage"
#: An operation this build does not recognize (a legacy record whose phase
#: maps to nothing). Still priced; reported under its own category.
OPERATION_OTHER = "other"

#: The first attempt at a piece of work.
ROLE_PRIMARY = "primary"
#: A review batch's instructed repair of a failed primary item.
ROLE_REPAIR = "repair"
#: A re-request of the same work after an attempt failed or was abandoned.
ROLE_RETRY = "retry"
#: A verification's escalated second opinion on the escalation-tier model.
ROLE_ESCALATION = "escalation"
#: A real-time verification that took over after paid batch waves.
ROLE_FALLBACK = "fallback"

ROLES: tuple[str, ...] = (
    ROLE_PRIMARY,
    ROLE_REPAIR,
    ROLE_RETRY,
    ROLE_ESCALATION,
    ROLE_FALLBACK,
)

TRANSPORT_BATCH = "batch"
TRANSPORT_REALTIME = "realtime"
TRANSPORTS: tuple[str, ...] = (TRANSPORT_BATCH, TRANSPORT_REALTIME)

#: Spend this run caused.
SCOPE_RUN = "run"
#: Spend billed before this collection started: the primary batch of a resumed
#: run, or a repair batch an earlier collection submitted and this one read.
SCOPE_EARLIER = "earlier"
SCOPES: tuple[str, ...] = (SCOPE_RUN, SCOPE_EARLIER)

#: The non-usage outcome tags a record may carry (descriptive only; pricing
#: reads the counters). Batch result types the API does not bill.
UNBILLED_BATCH_RESULT_TYPES = frozenset({"errored", "canceled", "expired"})

# Subtotal categories, in display order. A repair and an escalation are split
# out from their operation because they are the attempts a report reader asks
# about separately ("what did the repair cost?", "what did escalation cost?").
CATEGORY_LABELS: dict[str, str] = {
    "review": "review",
    "review_repair": "review repair",
    "verification": "verification",
    "verification_escalation": "verification escalation",
    OPERATION_TRIAGE: "verification triage",
    OPERATION_CROSS_CHECK: "cross-check",
    OPERATION_COMPLIANCE: "compliance",
    OPERATION_DRAWING_IMPACT: "drawing impact",
    OPERATION_RESEARCH: "location research",
    OPERATION_DRAWING_DIGEST: "drawing digest",
    OPERATION_OTHER: "other",
}

# Diagnostics phase names -> operation, for records that predate attempt
# metadata (a legacy event names only its phase). Anything else is "other".
_PHASE_OPERATIONS: dict[str, str] = {
    "batch_collect": OPERATION_REVIEW,
    "review": OPERATION_REVIEW,
    "verification": OPERATION_VERIFICATION,
    "cross_check_verification": OPERATION_VERIFICATION,
    "cross_check": OPERATION_CROSS_CHECK,
    "compliance": OPERATION_COMPLIANCE,
    "drawing_impact": OPERATION_DRAWING_IMPACT,
    "research": OPERATION_RESEARCH,
    "location_research": OPERATION_RESEARCH,
    "drawing_digest": OPERATION_DRAWING_DIGEST,
    "triage": OPERATION_TRIAGE,
}


def operation_for_phase(phase: str | None) -> str:
    """The operation a diagnostics phase records, for a legacy record."""
    return _PHASE_OPERATIONS.get(str(phase or ""), OPERATION_OTHER)


def spend_category(operation: str, role: str) -> str:
    """The subtotal a record belongs to (see :data:`CATEGORY_LABELS`)."""
    if operation == OPERATION_REVIEW and role == ROLE_REPAIR:
        return "review_repair"
    if operation == OPERATION_VERIFICATION and role == ROLE_ESCALATION:
        return "verification_escalation"
    return operation if operation in CATEGORY_LABELS else OPERATION_OTHER


def normalize_transport(value: Any, default: str = TRANSPORT_REALTIME) -> str:
    """``batch`` or ``realtime``; anything else is ``default``.

    The conservative default is ``realtime``: an unlabelled request is priced
    at the standard rate, never granted a batch discount it may not have had.
    """
    text = str(value or "").strip().lower()
    return text if text in TRANSPORTS else default


def _count(value: Any) -> int:
    """A non-negative int counter; a malformed value counts as zero."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        out = int(value)
    except (TypeError, ValueError):
        return 0
    return out if out > 0 else 0


def _field(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttemptUsage:
    """What one paid attempt used, and who it is. See the module docstring."""

    operation: str
    role: str = ROLE_PRIMARY
    transport: str = TRANSPORT_REALTIME
    model: str = ""
    usage_known: bool = True
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_creation_unknown_input_tokens: int = 0
    cache_creation_breakdown_status: str = CACHE_BREAKDOWN_NONE
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    batch_id: str = ""
    custom_id: str = ""
    message_id: str = ""
    scope: str = SCOPE_RUN
    #: Descriptive result tag ("ok", "incomplete", "errored", "pending", …).
    outcome: str = ""

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown attempt role {self.role!r}")
        if self.transport not in TRANSPORTS:
            raise ValueError(f"unknown attempt transport {self.transport!r}")
        if self.scope not in SCOPES:
            raise ValueError(f"unknown attempt scope {self.scope!r}")
        if not self.usage_known and any(self._counters()):
            # Unknown usage is unknown: a known part belongs in a record of
            # its own, or it would be priced as if the whole were known.
            raise ValueError("an attempt with unknown usage carries no counters")

    def _counters(self) -> tuple[int, ...]:
        return (
            self.input_tokens,
            self.output_tokens,
            self.cache_creation_input_tokens,
            self.cache_read_input_tokens,
            self.web_search_requests,
            self.web_fetch_requests,
        )

    @property
    def attempt_id(self) -> str:
        """Stable identity, or ``""`` when the attempt has none.

        A batch item is identified by its batch id, custom id, and role — the
        role keeps a repair distinct from the primary it repaired even when a
        repair batch reuses the primary's custom id. A synchronous request is
        identified by the message id of the response it returned. A request
        that raised before any response has no identity, so it is never
        deduplicated: it is recorded exactly once by the loop that made it.
        """
        if self.batch_id and self.custom_id:
            return f"batch:{self.batch_id}:{self.custom_id}:{self.role}"
        if self.message_id:
            return f"message:{self.message_id}"
        return ""

    @property
    def category(self) -> str:
        return spend_category(self.operation, self.role)

    @property
    def billable(self) -> bool:
        """Known usage that costs something (tokens, cache, or searches)."""
        return self.usage_known and any(
            (
                self.input_tokens,
                self.output_tokens,
                self.cache_creation_input_tokens,
                self.cache_read_input_tokens,
                self.web_search_requests,
            )
        )

    def cache_usage(self) -> dict:
        return {
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_5m_input_tokens": self.cache_creation_5m_input_tokens,
            "cache_creation_1h_input_tokens": self.cache_creation_1h_input_tokens,
            "cache_creation_unknown_input_tokens": self.cache_creation_unknown_input_tokens,
            CACHE_BREAKDOWN_STATUS_KEY: self.cache_creation_breakdown_status,
        }

    def with_scope(self, scope: str) -> "AttemptUsage":
        return replace(self, scope=scope)

    def to_dict(self) -> dict:
        """The JSON-friendly record, as carriers and diagnostics store it.

        Carries ``escalated`` (``role == "escalation"``) because a
        ``call_usage`` entry written before attempt records existed was read
        through that key.
        """
        return {
            "attempt_id": self.attempt_id,
            "operation": self.operation,
            "role": self.role,
            "transport": self.transport,
            "model": self.model,
            "usage_known": self.usage_known,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            **self.cache_usage(),
            "web_search_requests": self.web_search_requests,
            "web_fetch_requests": self.web_fetch_requests,
            "batch_id": self.batch_id,
            "custom_id": self.custom_id,
            "message_id": self.message_id,
            "scope": self.scope,
            "outcome": self.outcome,
            "escalated": self.role == ROLE_ESCALATION,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        operation: str = OPERATION_OTHER,
        transport: str = TRANSPORT_REALTIME,
        model: str = "",
    ) -> "AttemptUsage":
        """Read a record back, tolerating the shapes that predate it.

        A legacy ``call_usage`` entry (``model`` / ``escalated`` / counters)
        reads as a known attempt whose role follows ``escalated``; missing
        operation, transport, and model fall back to the keyword defaults (the
        diagnostics event's own). A record marked ``usage_known: False`` stays
        unknown, whatever counters it carries.
        """
        role = str(data.get("role") or "").strip().lower()
        if role not in ROLES:
            role = ROLE_ESCALATION if data.get("escalated") else ROLE_PRIMARY
        scope = str(data.get("scope") or "").strip().lower()
        if scope not in SCOPES:
            scope = SCOPE_RUN
        known = data.get("usage_known") is not False
        common = dict(
            operation=str(data.get("operation") or operation or OPERATION_OTHER),
            role=role,
            transport=normalize_transport(data.get("transport"), transport),
            model=str(data.get("model") or model or ""),
            batch_id=str(data.get("batch_id") or ""),
            custom_id=str(data.get("custom_id") or ""),
            message_id=str(data.get("message_id") or ""),
            scope=scope,
            outcome=str(data.get("outcome") or ""),
        )
        if not known:
            return cls(usage_known=False, **common)
        return cls(
            usage_known=True,
            input_tokens=_count(data.get("input_tokens")),
            output_tokens=_count(data.get("output_tokens")),
            web_search_requests=_count(data.get("web_search_requests")),
            web_fetch_requests=_count(data.get("web_fetch_requests")),
            **cache_usage_from(data),
            **common,
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def known_attempt(
    source: Any,
    *,
    operation: str,
    role: str = ROLE_PRIMARY,
    transport: str,
    model: str = "",
    batch_id: str = "",
    custom_id: str = "",
    message_id: str = "",
    scope: str = SCOPE_RUN,
    outcome: str = "",
) -> AttemptUsage:
    """An attempt whose usage was read, from any carrier or counters dict.

    ``source`` may be a ``ReviewResult`` / ``VerificationResult``, a counters
    dict, or a provider ``usage`` block: tokens and searches are read by name
    (a raw block's searches sit under ``server_tool_use``), and the cache
    counters go through :func:`~.api_config.cache_usage_from`, which keeps the
    per-TTL split and recognizes a raw block.
    """
    server = _field(source, "server_tool_use")
    searches = _field(source, "web_search_requests")
    if searches is None:
        searches = _field(server, "web_search_requests")
    fetches = _field(source, "web_fetch_requests")
    if fetches is None:
        fetches = _field(server, "web_fetch_requests")
    return AttemptUsage(
        operation=operation,
        role=role,
        transport=normalize_transport(transport),
        model=str(model or ""),
        usage_known=True,
        input_tokens=_count(_field(source, "input_tokens")),
        output_tokens=_count(_field(source, "output_tokens")),
        web_search_requests=_count(searches),
        web_fetch_requests=_count(fetches),
        batch_id=str(batch_id or ""),
        custom_id=str(custom_id or ""),
        message_id=str(message_id or ""),
        scope=scope,
        outcome=str(outcome or ""),
        **cache_usage_from(source),
    )


def unknown_attempt(
    *,
    operation: str,
    role: str = ROLE_PRIMARY,
    transport: str,
    model: str = "",
    batch_id: str = "",
    custom_id: str = "",
    scope: str = SCOPE_RUN,
    outcome: str = "",
) -> AttemptUsage:
    """An attempt that may have been billed but whose usage was never read."""
    return AttemptUsage(
        operation=operation,
        role=role,
        transport=normalize_transport(transport),
        model=str(model or ""),
        usage_known=False,
        batch_id=str(batch_id or ""),
        custom_id=str(custom_id or ""),
        scope=scope,
        outcome=str(outcome or ""),
    )


#: A callback that receives one attempt record per request — how a caller
#: prices calls whose results ride no carrier (verification triage).
UsageSink = Callable[[AttemptUsage], None]


def attempt_dicts(attempts: Iterable[AttemptUsage]) -> list[dict]:
    return [attempt.to_dict() for attempt in attempts]


def attempts_from(
    entries: Any,
    *,
    operation: str = OPERATION_OTHER,
    transport: str = TRANSPORT_REALTIME,
    model: str = "",
) -> list[AttemptUsage]:
    """Records from a carrier's ``call_usage`` list (dicts or records)."""
    out: list[AttemptUsage] = []
    for entry in entries or ():
        if isinstance(entry, AttemptUsage):
            out.append(entry)
        elif isinstance(entry, Mapping):
            out.append(
                AttemptUsage.from_dict(
                    entry, operation=operation, transport=transport, model=model
                )
            )
    return out


def known_totals(attempts: Iterable[AttemptUsage]) -> dict:
    """Summed known usage — the flat fields of a carrier that holds attempts.

    Unknown attempts contribute nothing (they have no counters). The cache
    counters merge through :func:`~.api_config.merge_cache_usage`, so the
    per-TTL invariant and a sticky ``inconsistent`` status survive the sum.
    """
    known = [attempt for attempt in attempts if attempt.usage_known]
    totals: dict = {
        "input_tokens": sum(a.input_tokens for a in known),
        "output_tokens": sum(a.output_tokens for a in known),
        "web_search_requests": sum(a.web_search_requests for a in known),
        "web_fetch_requests": sum(a.web_fetch_requests for a in known),
    }
    totals.update(
        merge_cache_usage(*(a.cache_usage() for a in known))
        if known
        else empty_cache_usage()
    )
    return totals


__all__ = [
    "AttemptUsage",
    "CATEGORY_LABELS",
    "OPERATION_COMPLIANCE",
    "OPERATION_CROSS_CHECK",
    "OPERATION_DRAWING_DIGEST",
    "OPERATION_DRAWING_IMPACT",
    "OPERATION_OTHER",
    "OPERATION_RESEARCH",
    "OPERATION_REVIEW",
    "OPERATION_TRIAGE",
    "OPERATION_VERIFICATION",
    "ROLE_ESCALATION",
    "ROLE_FALLBACK",
    "ROLE_PRIMARY",
    "ROLE_REPAIR",
    "ROLE_RETRY",
    "ROLES",
    "SCOPE_EARLIER",
    "SCOPE_RUN",
    "SCOPES",
    "TRANSPORT_BATCH",
    "TRANSPORT_REALTIME",
    "TRANSPORTS",
    "UNBILLED_BATCH_RESULT_TYPES",
    "UsageSink",
    "attempt_dicts",
    "attempts_from",
    "known_attempt",
    "known_totals",
    "normalize_transport",
    "operation_for_phase",
    "spend_category",
    "unknown_attempt",
]
