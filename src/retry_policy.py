"""Chunk 6 — centralized retry, continuation, and batch-failure policy.

Before this module, retry behavior lived in five separate manual loops:

* ``reviewer._stream_review`` — streaming review (max_retries=3) with
  ad-hoc per-exception backoff, plus a string-matching fallback for
  generic connection errors.
* ``cross_checker.run_cross_check`` — same shape as the reviewer loop,
  duplicated.
* ``verifier._run_verification_call`` — verification streaming (max_retries=2)
  with yet another per-exception backoff schedule, plus a continuation
  loop that resumes on ``pause_turn`` (cap=5).
* ``verifier.collect_verification_batch_results`` — batch wave loop that
  re-submits findings up to ``MAX_VERIFICATION_WAVES`` times, with no
  per-finding failure-class tracking.
* ``batch_runtime.poll_batch_bounded`` — consecutive-error polling
  backoff (this one already lives behind a policy object).

The plan calls out three problems with that arrangement:

1. **Retry behavior is unpredictable.** Backoff schedules differ per
   call site, and the same exception class produces different sleep
   times depending on which loop catches it.
2. **String-matching exception text is fragile.** The reviewer's
   "retryable connection error" heuristic scans the message body for
   substrings; the typed SDK exceptions (``APIConnectionError``,
   ``RateLimitError``, ``InternalServerError``, ``APIStatusError``)
   already carry the right semantics.
3. **Batch waves can burn budget on permanently-broken findings.**
   A finding that returns ``invalid_request_error`` on wave 1 gets the
   same retry treatment as one that hit a transient ``server_error``,
   even though the latter is retryable and the former is not.

Design
------

This module exposes three closed concepts:

* :class:`RetryPolicy` — frozen bundle for an app-level retry loop. The
  fields are ``max_attempts`` (how many total tries) and ``backoff_*``
  (base / multiplier / cap seconds). Helpers like
  :func:`compute_backoff_seconds` turn these into a wait-time given the
  attempt index.
* :func:`classify_exception` — typed-SDK-first classifier. Returns one
  of the :class:`FailureClass` values so the call site can branch on
  semantics rather than ``isinstance`` cascades. The string-matching
  fallback for generic ``Exception`` is preserved for transport-level
  errors that surface unwrapped (audit Issue 9), but it is now the
  *last* check, not the first.
* :func:`classify_batch_failure` — variant for parsing the wave-failure
  error message a batch result carries. Returns the same
  :class:`FailureClass` taxonomy so per-finding tracking in the wave
  loop can decide "do not retry this class twice" without re-scanning
  free text at every call site.

The policies themselves are intentionally short — the loops they back
are pre-existing and the module's job is to make those loops legible
rather than to take ownership of the transport.

Continuation cap
----------------

The plan asks for the real-time pause-turn continuation cap to drop
from 5 to 2, with a higher cap allowed only for routing decisions that
explicitly say "deep". :func:`max_continuations_for_mode` is the lookup
the routing module reaches for; the real-time loop in
:func:`verifier._run_verification_call` already reads
``decision.max_continuations``, so the change lands purely inside the
routing selector. The default (2) and the deep-mode override (4) are
expressed here so a future tuning pass touches one constant.

Batch wave failure tracking
---------------------------

:class:`BatchWaveFailureTracker` records ``(custom_id → failure class →
count)`` across waves. A finding that hits the same :class:`FailureClass`
twice in a row becomes terminal-unverified before the global
``MAX_VERIFICATION_WAVES`` cap. :class:`FailureClass.INVALID_REQUEST`
is special-cased: it never retries because the request shape would have
to change to get a different answer (the plan explicitly calls this
out: "Do not retry invalid request errors without changing the
request shape").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# The typed SDK exceptions. Imported eagerly so the classifier can do
# real ``isinstance`` checks rather than string-matching class names.
# This module is import-light by design (no other src deps) so it can
# be loaded from the tests' fake-anthropic harness without pulling in
# the full pipeline graph.
from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    InternalServerError,
    RateLimitError,
)


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------


class FailureClass(str, Enum):
    """Closed taxonomy of API failure classes.

    Each class has a documented retry policy (see
    :func:`is_retryable_failure_class`) and a documented backoff
    multiplier so the same class produces the same wait time
    regardless of which loop is running.

    Inheriting from ``str`` keeps serialization cheap: a diagnostics
    dump can write the enum value directly without an enum-name
    lookup, and a future telemetry aggregator can bucket by string
    value without round-tripping through the enum.
    """

    # The model overloaded our token bucket. Retry with a long backoff.
    RATE_LIMIT = "rate_limit"

    # The Anthropic server is overloaded (HTTP 529) or returned a 5xx.
    # Retry with a moderate backoff.
    SERVER_ERROR = "server_error"

    # Transport-level connection failure (httpx / urllib3 / aiohttp).
    # Retry with a short backoff.
    CONNECTION = "connection"

    # The request itself is malformed (HTTP 400 / 422). NEVER retry —
    # the request shape would have to change to get a different answer.
    INVALID_REQUEST = "invalid_request"

    # The batch run reported the request errored / expired / canceled.
    # Retry once at most; repeated occurrences indicate a permanent
    # failure (e.g. the request shape was rejected by validation).
    BATCH_ERRORED = "batch_errored"
    BATCH_EXPIRED = "batch_expired"
    BATCH_CANCELED = "batch_canceled"

    # The model returned text that could not be parsed (no tool_use
    # block, no JSON array, stop_reason=max_tokens). Retry once at
    # most; a finding that keeps producing parse errors should go
    # terminal unverified rather than burn another wave.
    PARSE_ERROR = "parse_error"

    # The model paused with ``stop_reason=pause_turn`` and needs the
    # server-tool turn resumed. NOT a failure per se — distinct
    # class so the continuation loop can count it separately.
    PAUSE_TURN = "pause_turn"

    # Any other error. Conservative default: do not retry.
    UNKNOWN = "unknown"


# Failure classes that the app-level retry loop should retry. The batch
# wave loop applies its own per-class policy via
# :func:`should_retry_batch_failure` because the trade-offs are
# different there (an invalid_request_error from the batch API means
# the request shape is bad, not that the call is transiently broken).
_RETRYABLE_REALTIME = frozenset(
    {
        FailureClass.RATE_LIMIT,
        FailureClass.SERVER_ERROR,
        FailureClass.CONNECTION,
    }
)


def is_retryable_failure_class(failure_class: FailureClass) -> bool:
    """Return True iff a real-time app-level loop should retry this class."""
    return failure_class in _RETRYABLE_REALTIME


# ---------------------------------------------------------------------------
# Exception classification (typed-SDK-first)
# ---------------------------------------------------------------------------


# Connection-error message substrings (audit Issue 9). Used ONLY as a
# last resort, when the exception came in as a generic ``Exception``
# rather than one of the typed SDK classes. The typed SDK
# ``APIConnectionError`` covers the modern path; this list catches
# stale wrappers (e.g. an httpx ``RemoteProtocolError`` that escapes
# the SDK's translation layer).
_CONNECTION_PATTERNS = (
    "peer closed connection",
    "incomplete chunked read",
    "connection reset",
    "connection closed",
    "timed out",
    "timeout",
    "broken pipe",
    "remotedisconnected",
    "connectionreset",
    "server disconnected",
    "eof occurred",
    "incomplete read",
)


def classify_exception(exc: BaseException) -> FailureClass:
    """Classify an exception into a :class:`FailureClass`.

    Typed SDK exceptions are checked first so the legacy string-matching
    heuristic is only consulted for generic ``Exception`` instances that
    escaped the SDK's translation layer.
    """
    if isinstance(exc, RateLimitError):
        return FailureClass.RATE_LIMIT
    if isinstance(exc, InternalServerError):
        return FailureClass.SERVER_ERROR
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        # 529 (overloaded) and the explicit ``OverloadedError`` subclass
        # are server-side overload, not client-side bugs.
        if status == 529 or exc.__class__.__name__ == "OverloadedError":
            return FailureClass.SERVER_ERROR
        if isinstance(status, int) and 500 <= status < 600:
            return FailureClass.SERVER_ERROR
        if isinstance(status, int) and 400 <= status < 500:
            # 408 / 429 are technically client errors but semantically
            # retryable. The SDK already exposes 429 as RateLimitError
            # (caught above); 408 is rare enough that we leave it in
            # INVALID_REQUEST and let the operator surface it.
            return FailureClass.INVALID_REQUEST
        return FailureClass.UNKNOWN
    if isinstance(exc, APIConnectionError):
        return FailureClass.CONNECTION
    if isinstance(exc, APIError):
        # Generic API error from the SDK — neither a status error nor
        # a connection error. Treat as INVALID_REQUEST so we do not
        # blindly retry; the operator should see the error text.
        return FailureClass.INVALID_REQUEST

    # Last resort: a generic exception that did not pass through the
    # SDK's translation. Use the message-substring heuristic only for
    # this branch (audit Issue 9).
    msg = str(exc).lower()
    if any(pat in msg for pat in _CONNECTION_PATTERNS):
        return FailureClass.CONNECTION
    return FailureClass.UNKNOWN


# ---------------------------------------------------------------------------
# Batch failure classification
# ---------------------------------------------------------------------------


def classify_batch_failure(
    *,
    result_type: str | None,
    error_message: str | None = None,
    error_type: str | None = None,
) -> FailureClass:
    """Classify a batch-result failure into a :class:`FailureClass`.

    The batch API surfaces failures via ``result.type`` (one of
    ``"errored"`` / ``"expired"`` / ``"canceled"`` / ``"succeeded"``)
    plus an optional ``result.error`` block. The error block carries
    a ``type`` (e.g. ``"invalid_request_error"`` /
    ``"overloaded_error"``) and a ``message``.

    Parameters
    ----------
    result_type:
        The batch result type string. Typically passed from
        ``result.result.type``.
    error_message:
        The free-text error message attached to the batch result, if
        any. Lower-cased for matching.
    error_type:
        The structured error type string (e.g.
        ``"invalid_request_error"``). When present, takes priority
        over the message scan.
    """
    rt = (result_type or "").lower()
    et = (error_type or "").lower()
    em = (error_message or "").lower()

    # Structured error type wins when present — it is the SDK's typed
    # answer to "what went wrong?".
    if et:
        if "invalid_request" in et:
            return FailureClass.INVALID_REQUEST
        if "overloaded" in et or "rate_limit" in et:
            return FailureClass.RATE_LIMIT if "rate" in et else FailureClass.SERVER_ERROR
        if "server_error" in et or "internal_server" in et or "api_error" in et:
            return FailureClass.SERVER_ERROR
        if "timeout" in et:
            return FailureClass.CONNECTION

    if rt == "expired":
        return FailureClass.BATCH_EXPIRED
    if rt == "canceled":
        return FailureClass.BATCH_CANCELED
    if rt == "errored":
        # Try to find a structured signal in the message body.
        if "invalid_request" in em or "invalid request" in em:
            return FailureClass.INVALID_REQUEST
        if "overloaded" in em or "rate limit" in em or "rate_limit" in em:
            return FailureClass.SERVER_ERROR
        if "server error" in em or "internal" in em:
            return FailureClass.SERVER_ERROR
        return FailureClass.BATCH_ERRORED
    return FailureClass.UNKNOWN


# Batch wave failure classes that should NOT be retried even on the
# first occurrence. ``INVALID_REQUEST`` is the canonical case: the
# request shape would have to change, and the wave loop does not
# rebuild request bodies.
_BATCH_NEVER_RETRY = frozenset(
    {
        FailureClass.INVALID_REQUEST,
        FailureClass.BATCH_CANCELED,
    }
)


def should_retry_batch_failure(failure_class: FailureClass) -> bool:
    """Return True iff the batch wave loop should resubmit this failure class.

    ``INVALID_REQUEST`` returns ``False`` unconditionally (the request
    shape is bad). ``PARSE_ERROR`` returns ``True`` once — the per-finding
    tracker in :class:`BatchWaveFailureTracker` enforces the "same class
    twice in a row → terminal" rule on top of this.
    """
    return failure_class not in _BATCH_NEVER_RETRY


# ---------------------------------------------------------------------------
# Per-finding wave failure tracking
# ---------------------------------------------------------------------------


@dataclass
class BatchWaveFailureTracker:
    """Track per-finding failure classes across batch verification waves.

    The plan calls for two behaviors:

    1. *Repeated same-class failures become terminal earlier than the
       global wave cap.* A finding that fails with ``PARSE_ERROR`` on
       wave 1 and ``PARSE_ERROR`` on wave 2 is terminal-unverified;
       it does not get a third try.
    2. *``INVALID_REQUEST`` is never retried.* The request shape would
       have to change to get a different answer, and the wave loop does
       not rebuild request bodies.

    The tracker is keyed by ``custom_id`` (the per-request identifier
    the batch API uses). A new wave's tracker is fresh because each
    wave re-stamps its custom_ids with a new prefix
    (``verify_retry_<wave>__<original>``); the parent
    ``original_custom_id`` is used as the stable key so the tracker
    follows a finding across waves.
    """

    # original_custom_id -> [FailureClass, FailureClass, ...] across waves
    history: dict[str, list[FailureClass]] = field(default_factory=dict)

    def record(self, original_custom_id: str, failure_class: FailureClass) -> None:
        """Record one failure for ``original_custom_id``."""
        self.history.setdefault(original_custom_id, []).append(failure_class)

    def total_failures(self, original_custom_id: str) -> int:
        """Total recorded failures for this finding (any class)."""
        return len(self.history.get(original_custom_id, []))

    def is_terminal(self, original_custom_id: str, *, current: FailureClass) -> bool:
        """Return True iff ``current`` failure should become terminal-unverified.

        Terminal conditions:

        * ``INVALID_REQUEST`` is terminal on the very first occurrence.
        * Any other class is terminal when it would be the *second*
          consecutive occurrence of the same class for this finding.
        """
        if current in _BATCH_NEVER_RETRY:
            return True
        history = self.history.get(original_custom_id, [])
        if not history:
            return False
        return history[-1] == current

    def terminal_reason(
        self,
        original_custom_id: str,
        *,
        current: FailureClass,
    ) -> str:
        """Return a short human-readable reason for terminal classification."""
        if current in _BATCH_NEVER_RETRY:
            return f"non-retryable failure class: {current.value}"
        prev_count = len(self.history.get(original_custom_id, []))
        return (
            f"repeated {current.value} failure "
            f"(occurrence #{prev_count + 1} on this finding)"
        )


# ---------------------------------------------------------------------------
# Real-time retry policy (review / cross-check / verification streaming)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Closed bundle for an app-level retry loop.

    The default values match what the legacy hand-rolled loops did
    before this module landed:

    * Review / cross-check streaming: ``max_attempts=3``, base 5s.
    * Verification streaming: ``max_attempts=3`` (2 retries + initial),
      base 5s.

    Per-failure-class multipliers shape the wait time so a
    ``SERVER_ERROR`` waits longer than a generic ``CONNECTION`` blip.
    """

    max_attempts: int = 3
    base_backoff_seconds: float = 5.0
    rate_limit_multiplier: float = 2.0
    server_error_multiplier: float = 2.0
    connection_multiplier: float = 1.0


# Conservative defaults — wire each call site to a single shared
# policy so a future tuning pass touches one constant. The plan
# explicitly does not want per-call-site bespoke schedules.
DEFAULT_REALTIME_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    base_backoff_seconds=5.0,
)

# Verification has historically used 2 retries (3 attempts) with the
# same base backoff. The legacy loop multiplied SERVER_ERROR by 3x
# (``15 * (attempt+1)``) — we match that here.
DEFAULT_VERIFICATION_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    base_backoff_seconds=5.0,
    rate_limit_multiplier=2.0,
    server_error_multiplier=3.0,
    connection_multiplier=1.0,
)


def compute_backoff_seconds(
    policy: RetryPolicy,
    *,
    attempt: int,
    failure_class: FailureClass,
) -> float:
    """Compute the seconds to sleep before ``attempt`` (0-indexed).

    Exponential within the retry policy: ``base * multiplier ** attempt``.
    The multiplier is per-class so a rate-limit waits longer than a
    transport blip. Unknown classes return ``base`` (one short sleep
    so the loop does not hammer the API).
    """
    base = max(0.0, float(policy.base_backoff_seconds))
    if failure_class is FailureClass.RATE_LIMIT:
        multiplier = policy.rate_limit_multiplier
    elif failure_class is FailureClass.SERVER_ERROR:
        multiplier = policy.server_error_multiplier
    elif failure_class is FailureClass.CONNECTION:
        multiplier = policy.connection_multiplier
    else:
        multiplier = 1.0
    return base * (multiplier ** max(0, int(attempt)))


# ---------------------------------------------------------------------------
# Continuation policy (verification pause-turn loop)
# ---------------------------------------------------------------------------


# Default cap for the real-time pause-turn continuation loop. The plan
# explicitly calls this out: drop from 5 to 2. The deep-mode override
# (4) is reserved for DEEP_REASONING routing — a CRITICAL CALIFORNIA_AHJ
# finding may legitimately need more web_search rounds.
DEFAULT_MAX_CONTINUATIONS = 2
DEEP_MAX_CONTINUATIONS = 4


def max_continuations_for_mode(mode_value: str) -> int:
    """Return the continuation cap for a :class:`VerificationMode` value.

    Imports :mod:`verification_modes` lazily so this module remains
    leaf-level (no other src deps at module load). The lookup is
    string-based to avoid the enum cycle.
    """
    if not mode_value:
        return DEFAULT_MAX_CONTINUATIONS
    if mode_value == "deep_reasoning":
        return DEEP_MAX_CONTINUATIONS
    return DEFAULT_MAX_CONTINUATIONS


# ---------------------------------------------------------------------------
# Diagnostics payload shape
# ---------------------------------------------------------------------------


def retry_diagnostics_payload(
    *,
    attempts: int,
    failure_class: FailureClass | None,
    terminal_reason: str | None,
    continuation_count: int = 0,
) -> dict[str, Any]:
    """Build a small JSON-safe dict describing a retry outcome.

    Used by the verifier / reviewer / batch wave loops to stamp a
    consistent diagnostics payload onto the per-finding event so a
    downstream aggregator can bucket by retry reason without
    re-deriving from free text.
    """
    return {
        "attempts": int(attempts),
        "failure_class": failure_class.value if failure_class else None,
        "terminal_reason": terminal_reason,
        "continuation_count": int(continuation_count),
    }


__all__ = [
    "BatchWaveFailureTracker",
    "DEEP_MAX_CONTINUATIONS",
    "DEFAULT_MAX_CONTINUATIONS",
    "DEFAULT_REALTIME_RETRY_POLICY",
    "DEFAULT_VERIFICATION_RETRY_POLICY",
    "FailureClass",
    "RetryPolicy",
    "classify_batch_failure",
    "classify_exception",
    "compute_backoff_seconds",
    "is_retryable_failure_class",
    "max_continuations_for_mode",
    "retry_diagnostics_payload",
    "should_retry_batch_failure",
]
