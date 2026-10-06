"""Centralized retry, continuation, and batch-failure policy.

Before this module, retry behavior lived in five separate manual loops:

* the reviewer's streaming-review loop (max_retries=3) with
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

Retry timing (plan WP-11)
-------------------------

Every app-owned retry loop decides and waits through one
:class:`RetrySchedule`, so every loop honors the same contract:

* **The server's floor comes first.** A failed response's ``retry-after-ms``
  (milliseconds) or ``retry-after`` (seconds, or an HTTP date) header is read
  by :func:`parse_server_delay`. A valid value is a floor: the retry is sent
  no earlier, plus a small random spread so workers told the same number do
  not all retry at the same instant. A missing, malformed, negative,
  non-finite, zero, or already-expired value is no floor, and the local
  policy applies. Nothing is invented for a failure that carries no headers
  (a batch item's error, a mid-stream error event).
* **Local waits are exponential, capped, and jittered.** Without a floor the
  wait is ``base x multiplier ** attempt``, capped at
  ``RetryPolicy.max_backoff_seconds``, then drawn uniformly from
  ``[(1 - jitter_fraction) x wait, wait]``, so concurrent loops that fail
  together retry apart.
* **Two bounds.** The attempt count (each loop keeps its own setting's
  meaning, zero included) and an elapsed retry budget,
  ``RetryPolicy.max_retry_wait_seconds``: the total time one loop may spend
  waiting to retry. A server floor longer than what is left of the budget
  is never shortened; the loop stops with a reason that says so.
* **Explicit classes.** Only ``RATE_LIMIT``, ``SERVER_ERROR``, and
  ``CONNECTION`` are retried (408 and 409, which the SDK retries, among
  them). An authentication, permission, not-found, or invalid-request error
  is ``INVALID_REQUEST``; the monthly spend cap, a 429 that no wait can
  clear, is ``SPEND_LIMIT``; neither is retried, and a response marked
  ``x-should-retry: false`` is not retried unchanged either.
* **Injected time.** The clock, the sleep, and the random source are one
  :class:`RetryTiming` (module default :data:`DEFAULT_RETRY_TIMING`, which
  tests replace), and a wait is interrupted as soon as the loop's cancel
  event is set.

Concurrency permits are taken by the loops, per outbound call, and never
held across a :meth:`RetrySchedule.wait`.
"""

from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Callable, Mapping

from ..core import resource_pressure as _pressure

# The typed SDK exceptions. Imported eagerly so the classifier can do
# real ``isinstance`` checks rather than string-matching class names.
# This module is import-light by design (its one src import is the
# stdlib-only ``core.resource_pressure`` leaf) so it can be loaded from the
# tests' fake-anthropic harness without pulling in the full pipeline graph.
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

    # The request itself is malformed (HTTP 400 / 422), or the API refused
    # it for good (401 authentication, 403 permission, 404 not found; any
    # 4xx but 408 / 409, which are transient, and 429, a rate limit).
    # NEVER retry — the request or the account would have to change to get
    # a different answer.
    INVALID_REQUEST = "invalid_request"

    # The organization reached its monthly spend cap: a 429 whose
    # ``error.details.error_code`` is ``enforced_spend_limit_reached``. It
    # carries no ``retry-after`` and every retry fails until the cap resets
    # or is raised, so it is never retried as a rate limit.
    SPEND_LIMIT = "spend_limit"

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


# Classes the API itself settled: the request, the key, or the account would
# have to change. A loop that tolerates unclassified errors (batch polling)
# still stops on these at once.
_REFUSED = frozenset({FailureClass.INVALID_REQUEST, FailureClass.SPEND_LIMIT})


def is_refused_request_class(failure_class: FailureClass) -> bool:
    """True when the API refused the request for good (never retried).

    ``INVALID_REQUEST`` (bad request, authentication, permission, not found)
    and ``SPEND_LIMIT``. Call sites that word a failure as an "API error"
    rather than an unexpected one use this, so the two stay together.
    """
    return failure_class in _REFUSED


def is_invalid_resume_error(exc: BaseException) -> bool:
    """Whether a rejected continuation may need a fresh conversation.

    Only request validation qualifies (e.g. an expired container or invalid
    preserved history), never authentication, permissions, or spend limits.
    Callers must also require a resumed request and charge the restart to
    their existing retry budget. The identical invalid request is not resent.
    """
    if classify_exception(exc) is not FailureClass.INVALID_REQUEST:
        return False
    status = getattr(exc, "status_code", None)
    return status in (400, 422) or (
        isinstance(exc, APIError)
        and status in (None, 200)
        and _error_object(exc).get("type") == "invalid_request_error"
    )


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


#: ``error.details.error_code`` of the 429 the API returns once the
#: organization's monthly spend cap is reached (see :attr:`FailureClass.SPEND_LIMIT`).
SPEND_LIMIT_ERROR_CODE = "enforced_spend_limit_reached"


def _error_object(exc: BaseException) -> Mapping[str, Any]:
    """The ``error`` object of an API error's JSON body, or ``{}``."""
    body = getattr(exc, "body", None)
    if not isinstance(body, Mapping):
        return {}
    inner = body.get("error")
    if isinstance(inner, Mapping):
        return inner
    return body


def _is_spend_limit(exc: BaseException) -> bool:
    details = _error_object(exc).get("details")
    return isinstance(details, Mapping) and details.get("error_code") == SPEND_LIMIT_ERROR_CODE


# Structured ``error.type`` values, for an error the status code cannot
# classify: an ``error`` event in the middle of a stream arrives on the
# stream's 200 response, so only its body says what went wrong.
_BODY_ERROR_TYPES = {
    "rate_limit_error": FailureClass.RATE_LIMIT,
    "overloaded_error": FailureClass.SERVER_ERROR,
    "api_error": FailureClass.SERVER_ERROR,
    "timeout_error": FailureClass.CONNECTION,
    "invalid_request_error": FailureClass.INVALID_REQUEST,
    "authentication_error": FailureClass.INVALID_REQUEST,
    "permission_error": FailureClass.INVALID_REQUEST,
    "not_found_error": FailureClass.INVALID_REQUEST,
    "request_too_large": FailureClass.INVALID_REQUEST,
}


def classify_exception(exc: BaseException) -> FailureClass:
    """Classify an exception into a :class:`FailureClass`.

    Typed SDK exceptions are checked first so the legacy string-matching
    heuristic is only consulted for generic ``Exception`` instances that
    escaped the SDK's translation layer.
    """
    if isinstance(exc, RateLimitError):
        if _is_spend_limit(exc):
            return FailureClass.SPEND_LIMIT
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
        # 408 (request timeout) and 409 (lock timeout) are client-range
        # statuses the SDK retries. App-owned loops run with SDK retries off,
        # so they must retry them too, or switching a path to an app-owned
        # loop would stop it on a transient failure. (429 is RateLimitError,
        # caught above.)
        if status == 408:
            return FailureClass.CONNECTION
        if status == 409:
            return FailureClass.SERVER_ERROR
        if isinstance(status, int) and 400 <= status < 500:
            return FailureClass.INVALID_REQUEST
        # Any other status is the stream's own 200: an ``error`` event sent
        # mid-stream. Its body names the error (``overloaded_error`` is the
        # common one, and a fresh request is the remedy).
        error_type = _error_object(exc).get("type")
        if error_type == "rate_limit_error" and _is_spend_limit(exc):
            return FailureClass.SPEND_LIMIT
        if isinstance(error_type, str) and error_type in _BODY_ERROR_TYPES:
            return _BODY_ERROR_TYPES[error_type]
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
        # Rate limiting and overload are distinct classes: a 429 waits on
        # the longer rate-limit backoff multiplier, a 529 on the
        # server-error one. The message-scan branch below mirrors this
        # split exactly so an ``errored`` item without a structured type
        # classifies the same way as one with it.
        if "rate_limit" in et:
            return FailureClass.RATE_LIMIT
        if "overloaded" in et:
            return FailureClass.SERVER_ERROR
        if "server_error" in et or "internal_server" in et or "api_error" in et:
            return FailureClass.SERVER_ERROR
        if "timeout" in et:
            return FailureClass.CONNECTION

    if rt == "expired":
        return FailureClass.BATCH_EXPIRED
    if rt == "canceled":
        return FailureClass.BATCH_CANCELED
    if rt == "errored":
        # Try to find a structured signal in the message body. The
        # rate-limit / overloaded split mirrors the structured branch
        # above — a rate-limit message used to collapse into SERVER_ERROR
        # here while the same failure with a typed ``error.type``
        # classified RATE_LIMIT, so the two shapes backed off differently.
        if "invalid_request" in em or "invalid request" in em:
            return FailureClass.INVALID_REQUEST
        if "rate limit" in em or "rate_limit" in em or "rate-limit" in em:
            return FailureClass.RATE_LIMIT
        if "overloaded" in em:
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

    The timing fields (plan WP-11) bound and spread the waits:

    * ``max_backoff_seconds`` caps one locally computed wait.
    * ``jitter_fraction``: a local wait ``d`` is drawn uniformly from
      ``[(1 - jitter_fraction) x d, d]``, never longer than the schedule
      says and never zero for a positive base.
    * ``server_jitter_fraction`` / ``server_jitter_min_seconds``: a server
      floor ``f`` is extended by a random spread of up to
      ``max(server_jitter_min_seconds, server_jitter_fraction x f)``. The
      retry never goes before ``f``.
    * ``max_retry_wait_seconds`` is the elapsed retry budget: the total time
      one loop may spend waiting to retry. A call's own duration is bounded
      by the client timeout, not by this budget, so a long stream that fails
      late still gets its retry.
    """

    max_attempts: int = 3
    base_backoff_seconds: float = 5.0
    rate_limit_multiplier: float = 2.0
    server_error_multiplier: float = 2.0
    connection_multiplier: float = 1.0
    max_backoff_seconds: float = 60.0
    jitter_fraction: float = 0.5
    server_jitter_fraction: float = 0.1
    server_jitter_min_seconds: float = 1.0
    max_retry_wait_seconds: float = 300.0


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
    """The nominal local wait after ``attempt`` (0-indexed) failed.

    Exponential within the retry policy: ``base * multiplier ** attempt``,
    capped at ``policy.max_backoff_seconds``. The multiplier is per-class so
    a rate-limit waits longer than a transport blip. Unknown classes return
    ``base`` (one short sleep so the loop does not hammer the API). This is
    the wait before jitter; :meth:`RetrySchedule.decide` draws the actual
    wait from it, or uses the server's floor instead.
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
    # Bound the exponent before ``**`` so a huge attempt index cannot
    # overflow; past the cap the value no longer matters.
    exponent = min(max(0, int(attempt)), 64)
    nominal = base * (max(0.0, float(multiplier)) ** exponent)
    cap = float(policy.max_backoff_seconds)
    if math.isfinite(cap) and cap >= 0:
        nominal = min(nominal, cap)
    return nominal


def jittered_backoff(nominal: float, *, jitter_fraction: float, rng: Callable[[], float]) -> float:
    """A local wait drawn uniformly from ``[(1 - jitter_fraction) x nominal, nominal]``."""
    nominal = max(0.0, float(nominal))
    fraction = min(1.0, max(0.0, float(jitter_fraction)))
    return nominal * (1.0 - fraction * _unit(rng))


def jittered_server_floor(
    floor: float,
    *,
    spread_fraction: float,
    spread_min_seconds: float,
    rng: Callable[[], float],
) -> float:
    """A server floor plus a random spread; never less than ``floor``."""
    floor = max(0.0, float(floor))
    spread = max(max(0.0, float(spread_min_seconds)), max(0.0, float(spread_fraction)) * floor)
    return floor + spread * _unit(rng)


def _unit(rng: Callable[[], float]) -> float:
    """``rng()`` clamped into ``[0, 1]`` (an injected source is not trusted)."""
    try:
        value = float(rng())
    except Exception:  # noqa: BLE001 — a broken source means no jitter, not a crash
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


# ---------------------------------------------------------------------------
# Server-requested delay (Retry-After)
# ---------------------------------------------------------------------------

#: Non-standard, millisecond precision; read before ``retry-after`` when
#: valid (the Anthropic SDK reads it the same way).
RETRY_AFTER_MS_HEADER = "retry-after-ms"
#: RFC 9110: delay-seconds or an HTTP date. The API sends it with a 429.
RETRY_AFTER_HEADER = "retry-after"
#: ``"false"`` on a failed response means the API says not to retry it.
SHOULD_RETRY_HEADER = "x-should-retry"

# A delay value: digits with an optional fraction. Signs, exponents, and
# words ("inf", "nan") are refused here rather than by ``float``.
_DELAY_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class ServerDelay:
    """A validated server-requested wait: the earliest a retry may be sent."""

    seconds: float
    header: str
    #: ``"milliseconds"``, ``"seconds"``, or ``"http-date"``.
    form: str


def _header_value(headers: Any, name: str) -> str | None:
    """One header's value from an ``httpx.Headers``-like or plain mapping."""
    if headers is None:
        return None
    value: Any = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        try:
            value = getter(name)
        except Exception:  # noqa: BLE001 — unreadable headers are no headers
            value = None
    if value is None:
        items = getattr(headers, "items", None)
        if callable(items):
            try:
                for key, candidate in items():
                    if isinstance(key, (str, bytes)) and (
                        key.decode("latin-1") if isinstance(key, bytes) else key
                    ).lower() == name:
                        value = candidate
                        break
            except Exception:  # noqa: BLE001
                return None
    if isinstance(value, bytes):
        value = value.decode("latin-1", errors="replace")
    return value if isinstance(value, str) else None


def _delay_number(value: str) -> float | None:
    match = _DELAY_NUMBER_RE.match(value)
    if match is None:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) else None


def _http_date_seconds(value: str, *, now: float) -> float | None:
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # RFC 9110 dates are GMT; ``-0000`` parses as a naive datetime.
        when = when.replace(tzinfo=timezone.utc)
    try:
        seconds = when.timestamp() - float(now)
    except (OverflowError, OSError, ValueError):
        return None
    return seconds if math.isfinite(seconds) else None


def parse_server_delay(headers: Any, *, now: float) -> ServerDelay | None:
    """The server's retry floor from response headers, or ``None``.

    ``retry-after-ms`` is read first; when it is absent or invalid,
    ``retry-after`` (whole or decimal seconds, or an HTTP date measured
    against ``now``, wall-clock seconds since the epoch). A value that is
    malformed, negative, non-finite, zero, or (for a date) not in the future
    is no floor, and ``None`` sends the caller to its local policy. A valid
    ``retry-after-ms`` decides on its own, as in the SDK: its ``0`` means
    "no wait", not "read the other header".
    """
    ms_value = _header_value(headers, RETRY_AFTER_MS_HEADER)
    if ms_value is not None:
        ms = _delay_number(ms_value)
        if ms is not None:
            seconds = ms / 1000.0
            return ServerDelay(seconds, RETRY_AFTER_MS_HEADER, "milliseconds") if seconds > 0 else None
    value = _header_value(headers, RETRY_AFTER_HEADER)
    if value is None:
        return None
    seconds = _delay_number(value)
    if seconds is not None:
        return ServerDelay(seconds, RETRY_AFTER_HEADER, "seconds") if seconds > 0 else None
    seconds = _http_date_seconds(value, now=now)
    if seconds is None or seconds <= 0:
        return None
    return ServerDelay(seconds, RETRY_AFTER_HEADER, "http-date")


def _response_headers(exc: BaseException | None) -> Any:
    response = getattr(exc, "response", None) if exc is not None else None
    return getattr(response, "headers", None)


def server_delay_for(exc: BaseException | None, *, now: float) -> ServerDelay | None:
    """The retry floor an exception's HTTP response asked for, or ``None``.

    Only an error with a response has headers: a connection failure, a
    parse failure, or a batch item's error has none, and none is invented.
    """
    return parse_server_delay(_response_headers(exc), now=now)


def server_declined_retry(exc: BaseException | None) -> bool:
    """True when the failed response said ``x-should-retry: false``."""
    value = _header_value(_response_headers(exc), SHOULD_RETRY_HEADER)
    return value is not None and value.strip().lower() == "false"


# ---------------------------------------------------------------------------
# Injected time
# ---------------------------------------------------------------------------


def _system_wait(seconds: float, cancel_event: Any = None) -> bool:
    """Wait ``seconds``; return False if ``cancel_event`` interrupted it.

    ``time.sleep`` is looked up at call time, so tests that patch it on the
    ``time`` module keep working.
    """
    seconds = max(0.0, float(seconds))
    if cancel_event is None:
        time.sleep(seconds)
        return True
    return not cancel_event.wait(seconds)


@dataclass(frozen=True)
class RetryTiming:
    """The clock, the wait, and the random source a retry loop uses.

    ``wait(seconds, cancel_event)`` returns True when it waited the whole
    time and False when the cancel event cut it short; it must return
    promptly once the event is set. ``now()`` is wall-clock seconds since
    the epoch (an HTTP-date ``retry-after`` is measured against it).
    ``random()`` returns a float in ``[0, 1)``.
    """

    wait: Callable[[float, Any], bool]
    now: Callable[[], float]
    random: Callable[[], float]


SYSTEM_RETRY_TIMING = RetryTiming(
    wait=_system_wait,
    now=lambda: time.time(),
    random=lambda: random.random(),
)

#: The timing every loop uses unless it is handed one. Tests replace this
#: module attribute (``monkeypatch.setattr(retry_policy,
#: "DEFAULT_RETRY_TIMING", fake)``); it is read when a schedule is created.
DEFAULT_RETRY_TIMING = SYSTEM_RETRY_TIMING


def current_retry_timing() -> RetryTiming:
    """The module's current default :class:`RetryTiming`."""
    return DEFAULT_RETRY_TIMING


# ---------------------------------------------------------------------------
# The schedule: one loop's retry decisions
# ---------------------------------------------------------------------------

#: Why a :class:`RetryDecision` stops the loop.
STOP_NOT_RETRYABLE = "not_retryable"
STOP_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
STOP_SERVER_DECLINED = "server_declined"
STOP_SERVER_DELAY_OVER_BUDGET = "server_delay_over_budget"
STOP_RETRY_BUDGET_SPENT = "retry_budget_spent"
STOP_CANCELLED = "cancelled"

#: Stops that mean "this call could have been retried and was not": what the
#: resource-pressure ledger counts as a call given up on after capacity
#: failures. Not-retryable and cancelled stops are not starvation.
_ABANDONED_FOR_CAPACITY_STOPS = frozenset(
    {
        STOP_ATTEMPTS_EXHAUSTED,
        STOP_RETRY_BUDGET_SPENT,
        STOP_SERVER_DELAY_OVER_BUDGET,
        STOP_SERVER_DECLINED,
    }
)


@dataclass(frozen=True)
class RetryDecision:
    """What a loop does after one failed attempt.

    ``retry`` with ``delay_seconds`` to wait first (``server_delay`` set when
    the wait honors the server's floor), or a stop with ``stop`` (one of the
    ``STOP_*`` constants) and a plain-language ``reason``.
    """

    failure_class: FailureClass
    retry: bool
    delay_seconds: float = 0.0
    server_delay: ServerDelay | None = None
    stop: str = ""
    reason: str = ""

    @property
    def note(self) -> str:
        """``" — <reason>"`` for a stop the loop's own messages do not
        already describe (anything but not-retryable and out-of-attempts);
        ``""`` otherwise. Loops append it to their terminal error text."""
        if self.retry or self.stop in ("", STOP_NOT_RETRYABLE, STOP_ATTEMPTS_EXHAUSTED):
            return ""
        return f" — {self.reason}"


class RetrySchedule:
    """One retry loop's budget: attempts, waiting time, and its clock.

    Create one per loop invocation. After a failed attempt the loop calls
    :meth:`decide`; when it says retry, the loop releases any concurrency
    permit it holds and calls :meth:`wait`. ``max_attempts`` is the total
    number of attempts, however the loop's own setting counts them (the
    loop converts; ``None`` takes ``policy.max_attempts``). Not thread-safe:
    each loop owns its schedule.
    """

    def __init__(
        self,
        policy: RetryPolicy,
        *,
        max_attempts: int | None = None,
        timing: RetryTiming | None = None,
        cancel_event: Any = None,
        label: str = "",
    ) -> None:
        self.policy = policy
        attempts = policy.max_attempts if max_attempts is None else max_attempts
        self.max_attempts = max(1, int(attempts))
        self.timing = timing if timing is not None else current_retry_timing()
        self.cancel_event = cancel_event
        #: Which loop this is (``"verification"``, ``"review"``, ...), for
        #: the resource-pressure ledger. Display only; never changes a wait.
        self.label = str(label or "")
        #: Total seconds waited so far (the elapsed retry budget spent).
        self.waited_seconds = 0.0
        #: Every wait taken, in order.
        self.waits: list[float] = []

    @property
    def remaining_wait_seconds(self) -> float:
        budget = float(self.policy.max_retry_wait_seconds)
        if not math.isfinite(budget):
            return math.inf
        return max(0.0, budget - self.waited_seconds)

    def cancelled(self) -> bool:
        event = self.cancel_event
        return bool(event is not None and event.is_set())

    def decide(
        self,
        exc: BaseException | None,
        *,
        attempt: int,
        failure_class: FailureClass | None = None,
        retryable: bool | None = None,
        same_request: bool = True,
    ) -> RetryDecision:
        """Decide what follows the failure of ``attempt`` (0-indexed).

        ``failure_class`` defaults to :func:`classify_exception`;
        ``retryable`` overrides the class's retryability (cross-check grants
        one re-request for an unparseable payload this way). In order: a
        cancelled loop stops; a non-retryable class stops; the last attempt
        stops; ``x-should-retry: false`` stops an unchanged request; then the
        wait is the server's floor (plus spread) when the response carries
        a valid one, else the jittered local backoff, and a wait that does
        not fit the remaining retry budget stops without being shortened.

        ``same_request=False`` allows a changed request, such as a fresh
        conversation after an invalid resume, past the server's identical
        retry veto. It still requires a retryable class or explicit override
        and obeys cancellation, attempt, delay, and wait-budget checks.

        A stop for a capacity-class failure (rate limit, server error,
        connection) that *could* have been retried — out of attempts, out of
        wait budget, a server floor past the budget, or the API declining —
        is reported to the resource-pressure ledger as a call given up on.
        The decision itself is unchanged.
        """
        decision = self._decide(
            exc,
            attempt=attempt,
            failure_class=failure_class,
            retryable=retryable,
            same_request=same_request,
        )
        if (
            not decision.retry
            and decision.stop in _ABANDONED_FOR_CAPACITY_STOPS
            and is_retryable_failure_class(decision.failure_class)
        ):
            _pressure.record_retry_abandoned(
                label=self.label,
                failure_class=decision.failure_class.value,
                stop=decision.stop,
                attempts=self.max_attempts,
            )
        return decision

    def _decide(
        self,
        exc: BaseException | None,
        *,
        attempt: int,
        failure_class: FailureClass | None = None,
        retryable: bool | None = None,
        same_request: bool = True,
    ) -> RetryDecision:
        if failure_class is not None:
            fc = failure_class
        elif exc is not None:
            fc = classify_exception(exc)
        else:
            fc = FailureClass.UNKNOWN
        if self.cancelled():
            return RetryDecision(fc, False, stop=STOP_CANCELLED, reason="the run was cancelled")
        can_retry = is_retryable_failure_class(fc) if retryable is None else bool(retryable)
        if not can_retry:
            return RetryDecision(
                fc, False, stop=STOP_NOT_RETRYABLE, reason=f"{fc.value} is not retried"
            )
        if int(attempt) + 1 >= self.max_attempts:
            return RetryDecision(
                fc,
                False,
                stop=STOP_ATTEMPTS_EXHAUSTED,
                reason=f"all {self.max_attempts} attempt(s) used",
            )
        if same_request and server_declined_retry(exc):
            return RetryDecision(
                fc,
                False,
                stop=STOP_SERVER_DECLINED,
                reason="the API marked the failure not retryable (x-should-retry: false)",
            )
        remaining = self.remaining_wait_seconds
        budget = float(self.policy.max_retry_wait_seconds)
        try:
            now = float(self.timing.now())
        except Exception:  # noqa: BLE001 — no clock: an HTTP-date floor cannot be read
            now = math.nan
        server = server_delay_for(exc, now=now)
        rng = self.timing.random
        if server is not None:
            if server.seconds > remaining:
                return RetryDecision(
                    fc,
                    False,
                    server_delay=server,
                    stop=STOP_SERVER_DELAY_OVER_BUDGET,
                    reason=(
                        f"the API asked to wait {server.seconds:.0f}s before retrying "
                        f"({server.header}), more than the {remaining:.0f}s left of "
                        f"this call's {budget:.0f}s retry budget; not retried"
                    ),
                )
            delay = jittered_server_floor(
                server.seconds,
                spread_fraction=self.policy.server_jitter_fraction,
                spread_min_seconds=self.policy.server_jitter_min_seconds,
                rng=rng,
            )
            return RetryDecision(fc, True, delay_seconds=min(delay, remaining), server_delay=server)
        nominal = compute_backoff_seconds(self.policy, attempt=attempt, failure_class=fc)
        delay = jittered_backoff(nominal, jitter_fraction=self.policy.jitter_fraction, rng=rng)
        if delay > remaining:
            return RetryDecision(
                fc,
                False,
                stop=STOP_RETRY_BUDGET_SPENT,
                reason=(
                    f"the {budget:.0f}s retry budget is spent "
                    f"({self.waited_seconds:.0f}s waited); not retried"
                ),
            )
        return RetryDecision(fc, True, delay_seconds=delay)

    def wait(self, decision: RetryDecision) -> bool:
        """Wait out a retry decision. False when the wait was cancelled.

        Call it with no concurrency permit held: the whole point of the wait
        is that another request may use the slot meanwhile.
        """
        if not decision.retry:
            return False
        if self.cancelled():
            return False
        delay = max(0.0, float(decision.delay_seconds))
        completed = bool(self.timing.wait(delay, self.cancel_event))
        self.waited_seconds += delay
        self.waits.append(delay)
        # Observation only, after the wait: the ledger learns how long this
        # loop stood still and why. It cannot change the wait.
        _pressure.record_retry_wait(
            label=self.label,
            seconds=delay,
            failure_class=decision.failure_class.value,
            server_floor=decision.server_delay is not None,
            completed=completed,
            attempt=len(self.waits),
        )
        return completed and not self.cancelled()


# ---------------------------------------------------------------------------
# Continuation policy (verification pause-turn loop)
# ---------------------------------------------------------------------------


# Default cap for the real-time pause-turn continuation loop. The plan
# explicitly calls this out: drop from 5 to 2. The deep-mode override
# (4) is reserved for DEEP_REASONING routing — a CRITICAL JURISDICTIONAL
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
    "DEFAULT_RETRY_TIMING",
    "DEFAULT_VERIFICATION_RETRY_POLICY",
    "FailureClass",
    "RETRY_AFTER_HEADER",
    "RETRY_AFTER_MS_HEADER",
    "RetryDecision",
    "RetryPolicy",
    "RetrySchedule",
    "RetryTiming",
    "SHOULD_RETRY_HEADER",
    "SPEND_LIMIT_ERROR_CODE",
    "STOP_ATTEMPTS_EXHAUSTED",
    "STOP_CANCELLED",
    "STOP_NOT_RETRYABLE",
    "STOP_RETRY_BUDGET_SPENT",
    "STOP_SERVER_DECLINED",
    "STOP_SERVER_DELAY_OVER_BUDGET",
    "SYSTEM_RETRY_TIMING",
    "ServerDelay",
    "classify_batch_failure",
    "classify_exception",
    "compute_backoff_seconds",
    "current_retry_timing",
    "is_refused_request_class",
    "is_invalid_resume_error",
    "is_retryable_failure_class",
    "jittered_backoff",
    "jittered_server_floor",
    "max_continuations_for_mode",
    "parse_server_delay",
    "retry_diagnostics_payload",
    "server_declined_retry",
    "server_delay_for",
    "should_retry_batch_failure",
]
