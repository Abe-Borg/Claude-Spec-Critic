"""Resource-pressure telemetry: was the run starved of capacity?

A run can finish late, or finish with less than it set out to do, because the
work itself was slow — or because the agents doing the work were made to wait.
The diagnostics report already counts *how many* retries, truncations, and
exhausted search budgets a run saw. It did not record *how long* the run
stood still, nor why. This module records the waiting, from four sources:

* **The API's capacity.** Every wait a retry loop takes after a rate limit
  (HTTP 429), an overloaded or failing server (529 / 5xx), or a dropped
  connection, with the failure class and whether the API set the wait itself
  (``retry-after``); every call a loop gave up on after such failures; the
  count of throttled responses; and the API's own rate-limit headroom
  (``anthropic-ratelimit-*-remaining`` against ``-limit``) on every response,
  so a run that was *about* to be throttled is visible too.
* **The app's own concurrency limits.** Every acquisition of a call permit
  (the live-review, verification, research, collection, and drawing-digest
  pools), and how long it blocked. A long permit wait means the run was
  waiting on its own settings, not the API.
* **Shared verification.** A follower that waited on another finding's
  leader, and whether the wait timed out.
* **Batch processing.** How long each batch was polled, items the API
  expired instead of processing, and polls that detached for lack of
  progress.

**Observation only.** Nothing here changes a request, a wait, a permit, or a
verdict: the recorder is told what happened after it happened. With no
recorder installed every ``record_*`` call is a no-op, so unit tests and
tools that never start a run see exactly the old behavior. A recorder never
raises into the code that reports to it.

**Lifecycle.** ``DiagnosticsReport`` owns one :class:`PressureRecorder`
and installs it for the run (``start_pressure_recording``); ``finish``
uninstalls it. Worker threads do not inherit context variables, and the
loops that wait run on pool threads, so the installed recorder is
process-wide — one run per process, as the trace recorder assumes.
:func:`recording` binds a recorder to the current context instead, for
tests and for code that wants scoping; a bound recorder wins over the
installed one.

Stdlib only: this is a ``src/core`` leaf, imported by the retry policy, the
batch runtime, the pipeline, and the client factory.
"""

from __future__ import annotations

import contextvars
import random
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Optional

#: A permit acquisition that blocked at least this long counts as a wait;
#: anything shorter is scheduler noise on an uncontended semaphore.
PERMIT_WAIT_FLOOR_SECONDS = 0.01
#: One permit wait at least this long makes local concurrency a signal.
PERMIT_SIGNAL_SECONDS = 1.0
#: Rate-limit headroom below this share of the limit is "low".
LOW_HEADROOM_FRACTION = 0.10
#: Reservoir for permit-wait percentiles (count, total, and max stay exact).
_RESERVOIR_SIZE = 2048
#: Timeline events for exhausted headroom are capped per run.
_MAX_HEADROOM_EVENTS = 3

#: Signal names in ``summary()["signals"]``.
SIGNAL_PROVIDER_THROTTLING = "provider_throttling"
SIGNAL_CONNECTION_ERRORS = "connection_errors"
SIGNAL_LOCAL_CONCURRENCY = "local_concurrency"
SIGNAL_COORDINATION_WAIT = "coordination_wait"
SIGNAL_BATCH_QUEUE = "batch_queue"

#: Retry failure classes (``retry_policy.FailureClass`` values, kept as
#: literals so this leaf does not import the verification layer).
THROTTLING_CLASSES = frozenset({"rate_limit", "server_error"})
CONNECTION_CLASSES = frozenset({"connection"})
CAPACITY_CLASSES = THROTTLING_CLASSES | CONNECTION_CLASSES

#: Batch-poll detach reasons that mean the queue, not the operator, stopped
#: the wait. ``detach_reason`` may carry detail after the prefix.
_BATCH_QUEUE_DETACHES = ("max_elapsed", "no_progress", "retry_after_exceeds_poll_bound")

_RATELIMIT_HEADER = re.compile(r"^anthropic-ratelimit-([a-z0-9-]+)-(limit|remaining|reset)$")
_RETRY_AFTER_HEADERS = ("retry-after", "retry-after-ms")

EventSink = Callable[[str, str, str, dict], None]


def _label_key(label: str | None) -> str:
    return str(label or "").strip() or "unlabeled"


def _bucket(table: dict[str, dict], key: str) -> dict:
    bucket = table.get(key)
    if bucket is None:
        bucket = {"count": 0, "total_seconds": 0.0}
        table[key] = bucket
    return bucket


def _count(table: dict[str, int], key: str) -> None:
    table[key] = table.get(key, 0) + 1


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def _as_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 — a header value that cannot be read is no value
        return None
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


class PressureRecorder:
    """Thread-safe ledger of the waiting a run did and why.

    Every ``record_*`` method is cheap, holds the recorder's own lock only
    while updating counters, and never raises. ``on_event`` (when given) is
    called outside the lock with ``(phase, level, message, data)`` for the
    few occurrences worth a timeline line: a retry wait, a call given up on,
    a shared-verification wait that timed out, a batch that expired items or
    detached, and the first few responses with no rate-limit headroom left.
    Permit acquisitions and ordinary responses are counted silently.
    """

    def __init__(self, *, on_event: EventSink | None = None) -> None:
        self._lock = threading.Lock()
        self._on_event = on_event
        self._rng = random.Random(0x5EC)
        # Retry waits.
        self._retry_count = 0
        self._retry_total = 0.0
        self._retry_max = 0.0
        self._retry_server_floor = 0
        self._retry_cancelled = 0
        self._retry_by_class: dict[str, dict] = {}
        self._retry_by_label: dict[str, dict] = {}
        # Calls given up on after capacity failures.
        self._abandon_count = 0
        self._abandon_by_class: dict[str, int] = {}
        self._abandon_by_stop: dict[str, int] = {}
        self._abandon_by_label: dict[str, int] = {}
        # Permit acquisitions.
        self._permit_acquisitions = 0
        self._permit_waited = 0
        self._permit_total = 0.0
        self._permit_max = 0.0
        self._permit_samples: list[float] = []
        self._permit_sampled = 0
        self._permit_by_pool: dict[str, dict] = {}
        # Shared-verification follower waits.
        self._sf_count = 0
        self._sf_total = 0.0
        self._sf_max = 0.0
        self._sf_timed_out = 0
        self._sf_orphaned = 0
        # HTTP responses and rate-limit headroom.
        self._http_count = 0
        self._http_429 = 0
        self._http_529 = 0
        self._http_5xx = 0
        self._http_retry_after = 0
        self._headroom_responses = 0
        self._headroom_exhausted = 0
        self._headroom_low = 0
        self._headroom_lowest: dict | None = None
        self._headroom_by_dimension: dict[str, dict] = {}
        self._headroom_events = 0
        # Batch polls.
        self._batch_count = 0
        self._batch_total = 0.0
        self._batch_max = 0.0
        self._batch_expired = 0
        self._batch_detached: dict[str, int] = {}
        self._batch_terminal: dict[str, int] = {}

    # -- recording -----------------------------------------------------------

    def _emit(self, phase: str, level: str, message: str, data: dict) -> None:
        sink = self._on_event
        if sink is None:
            return
        try:
            sink(phase, level, message, data)
        except Exception:  # noqa: BLE001 — telemetry never fails the run
            pass

    def record_retry_wait(
        self,
        *,
        label: str,
        seconds: float,
        failure_class: str,
        server_floor: bool = False,
        completed: bool = True,
        attempt: int | None = None,
    ) -> None:
        """One wait a retry loop took before trying again.

        ``seconds`` is the wait the loop asked for (a cancelled wait is still
        that long in the schedule's own budget); ``server_floor`` says the
        API set it (``retry-after``); ``attempt`` is the 1-based number of
        the attempt that failed, when the loop knows it.
        """
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            seconds = 0.0
        label_key = _label_key(label)
        class_key = str(failure_class or "unknown")
        with self._lock:
            self._retry_count += 1
            self._retry_total += seconds
            self._retry_max = max(self._retry_max, seconds)
            if server_floor:
                self._retry_server_floor += 1
            if not completed:
                self._retry_cancelled += 1
            for table, key in ((self._retry_by_class, class_key), (self._retry_by_label, label_key)):
                bucket = _bucket(table, key)
                bucket["count"] += 1
                bucket["total_seconds"] += seconds
        origin = "set by the API's retry-after" if server_floor else "local backoff"
        attempt_part = f" after attempt {int(attempt)}" if attempt else ""
        self._emit(
            label_key,
            "warning",
            f"Waited {seconds:.1f}s before retrying ({class_key}; {origin}){attempt_part}.",
            {
                "pressure": "retry_wait",
                "failure_class": class_key,
                "wait_seconds": round(seconds, 3),
                "server_floor": bool(server_floor),
                "completed": bool(completed),
            },
        )

    def record_retry_abandoned(
        self,
        *,
        label: str,
        failure_class: str,
        stop: str,
        attempts: int | None = None,
    ) -> None:
        """A call a loop stopped retrying after capacity failures.

        ``stop`` is the schedule's ``STOP_*`` reason: attempts exhausted, the
        retry-wait budget spent, a server floor longer than the budget, or
        the API declining a retry.
        """
        label_key = _label_key(label)
        class_key = str(failure_class or "unknown")
        stop_key = str(stop or "unknown")
        with self._lock:
            self._abandon_count += 1
            _count(self._abandon_by_class, class_key)
            _count(self._abandon_by_stop, stop_key)
            _count(self._abandon_by_label, label_key)
        attempts_part = f" after {int(attempts)} attempt(s)" if attempts else ""
        self._emit(
            label_key,
            "warning",
            f"Gave up retrying a {class_key} failure{attempts_part}: {stop_key}.",
            {
                "pressure": "retry_abandoned",
                "failure_class": class_key,
                "stop": stop_key,
                "attempts": int(attempts or 0),
            },
        )

    def record_permit_wait(self, *, pool: str, seconds: float, acquired: bool = True) -> None:
        """One acquisition of a concurrency permit and how long it blocked."""
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            seconds = 0.0
        pool_key = _label_key(pool)
        waited = seconds >= PERMIT_WAIT_FLOOR_SECONDS
        with self._lock:
            self._permit_acquisitions += 1
            bucket = self._permit_by_pool.get(pool_key)
            if bucket is None:
                bucket = {"acquisitions": 0, "waited": 0, "total_seconds": 0.0, "max_seconds": 0.0}
                self._permit_by_pool[pool_key] = bucket
            bucket["acquisitions"] += 1
            if not acquired:
                return
            if waited:
                self._permit_waited += 1
                bucket["waited"] += 1
            self._permit_total += seconds
            self._permit_max = max(self._permit_max, seconds)
            bucket["total_seconds"] += seconds
            bucket["max_seconds"] = max(bucket["max_seconds"], seconds)
            # Reservoir sampling keeps the percentiles unbiased past the cap.
            self._permit_sampled += 1
            if len(self._permit_samples) < _RESERVOIR_SIZE:
                self._permit_samples.append(seconds)
            else:
                slot = self._rng.randrange(self._permit_sampled)
                if slot < _RESERVOIR_SIZE:
                    self._permit_samples[slot] = seconds

    def record_singleflight_wait(
        self, *, seconds: float, timed_out: bool, findings: int = 1
    ) -> None:
        """A shared-verification follower's wait on its leader."""
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            seconds = 0.0
        with self._lock:
            self._sf_count += 1
            self._sf_total += seconds
            self._sf_max = max(self._sf_max, seconds)
            if timed_out:
                self._sf_timed_out += 1
                self._sf_orphaned += max(0, int(findings or 0))
        if timed_out:
            self._emit(
                "verification",
                "warning",
                f"Shared verification: waited {seconds:.0f}s for an equivalent "
                f"finding's leader, which did not finish; {int(findings or 0)} "
                "finding(s) verified independently.",
                {
                    "pressure": "singleflight_timeout",
                    "wait_seconds": round(seconds, 3),
                    "findings": int(findings or 0),
                },
            )

    def record_http_response(self, *, status_code: int, headers: Mapping[str, Any] | None) -> None:
        """One HTTP response from the API: its status and rate-limit headers.

        Reads ``anthropic-ratelimit-<dimension>-limit`` / ``-remaining`` /
        ``-reset`` for every dimension present (requests, tokens, input and
        output tokens, ...). Header names are matched case-insensitively; a
        value that is not a number is ignored.
        """
        try:
            status = int(status_code or 0)
        except (TypeError, ValueError):
            status = 0
        dims: dict[str, dict[str, Any]] = {}
        retry_after = False
        if headers is not None:
            try:
                items = list(headers.items())
            except Exception:  # noqa: BLE001 — not a mapping: no headers to read
                items = []
            for raw_key, value in items:
                key = str(raw_key).strip().lower()
                if key in _RETRY_AFTER_HEADERS:
                    retry_after = True
                    continue
                match = _RATELIMIT_HEADER.match(key)
                if not match:
                    continue
                dims.setdefault(match.group(1), {})[match.group(2)] = value
        exhausted_now: list[dict] = []
        with self._lock:
            self._http_count += 1
            if status == 429:
                self._http_429 += 1
            elif status == 529:
                self._http_529 += 1
            elif 500 <= status <= 599:
                self._http_5xx += 1
            if retry_after:
                self._http_retry_after += 1
            saw_headroom = False
            response_exhausted = False
            response_low = False
            for dimension, values in sorted(dims.items()):
                limit = _as_int(values.get("limit"))
                remaining = _as_int(values.get("remaining"))
                if limit is None or remaining is None or limit <= 0:
                    continue
                saw_headroom = True
                remaining = max(0, remaining)
                fraction = min(1.0, remaining / limit)
                reset = str(values.get("reset") or "") or None
                entry = self._headroom_by_dimension.get(dimension)
                if entry is None:
                    entry = {
                        "limit": limit,
                        "lowest_remaining": remaining,
                        "lowest_fraction": round(fraction, 4),
                        "reset_at_lowest": reset,
                        "exhausted_responses": 0,
                        "low_responses": 0,
                    }
                    self._headroom_by_dimension[dimension] = entry
                else:
                    entry["limit"] = limit
                    if fraction < entry["lowest_fraction"]:
                        entry["lowest_remaining"] = remaining
                        entry["lowest_fraction"] = round(fraction, 4)
                        entry["reset_at_lowest"] = reset
                if remaining <= 0:
                    entry["exhausted_responses"] += 1
                    response_exhausted = True
                    exhausted_now.append(
                        {"dimension": dimension, "limit": limit, "reset": reset}
                    )
                if fraction < LOW_HEADROOM_FRACTION:
                    entry["low_responses"] += 1
                    response_low = True
                lowest = self._headroom_lowest
                if lowest is None or fraction < lowest["fraction"]:
                    self._headroom_lowest = {
                        "dimension": dimension,
                        "fraction": round(fraction, 4),
                        "remaining": remaining,
                        "limit": limit,
                        "reset": reset,
                        "status_code": status,
                    }
            if saw_headroom:
                self._headroom_responses += 1
                if response_exhausted:
                    self._headroom_exhausted += 1
                if response_low:
                    self._headroom_low += 1
            emit_exhausted = bool(exhausted_now) and self._headroom_events < _MAX_HEADROOM_EVENTS
            if emit_exhausted:
                self._headroom_events += 1
        if emit_exhausted:
            names = ", ".join(
                f"{item['dimension']} (limit {item['limit']:,}"
                + (f", resets {item['reset']}" if item.get("reset") else "")
                + ")"
                for item in exhausted_now
            )
            self._emit(
                "api",
                "warning",
                f"Rate-limit headroom exhausted on an HTTP {status} response: {names}.",
                {
                    "pressure": "headroom_exhausted",
                    "status_code": status,
                    "dimensions": [item["dimension"] for item in exhausted_now],
                },
            )

    def record_batch_poll(
        self,
        *,
        seconds: float,
        terminal_status: str | None,
        expired: int = 0,
        detach_reason: str | None = None,
        poll_failed: bool = False,
        batch_id: str | None = None,
    ) -> None:
        """One bounded poll of a batch, from its first status read to its end."""
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            seconds = 0.0
        expired = max(0, int(expired or 0))
        detach_key = None
        if detach_reason:
            detach_key = str(detach_reason).split(":", 1)[0].strip() or "detached"
        with self._lock:
            self._batch_count += 1
            self._batch_total += seconds
            self._batch_max = max(self._batch_max, seconds)
            self._batch_expired += expired
            if detach_key:
                _count(self._batch_detached, detach_key)
            if terminal_status:
                _count(self._batch_terminal, str(terminal_status))
            elif poll_failed:
                _count(self._batch_terminal, "poll_failed")
        if expired or detach_key:
            what = []
            if expired:
                what.append(f"{expired} request(s) expired unprocessed")
            if detach_key:
                what.append(f"polling detached ({detach_key})")
            label = f" {batch_id}" if batch_id else ""
            self._emit(
                "batch",
                "warning",
                f"Batch{label}: " + "; ".join(what) + f" after {seconds / 60:.0f} min.",
                {
                    "pressure": "batch_queue",
                    "poll_seconds": round(seconds, 1),
                    "expired": expired,
                    "detach_reason": detach_key,
                    "terminal_status": terminal_status,
                },
            )

    # -- reading -------------------------------------------------------------

    def summary(self) -> dict:
        """A JSON-safe rollup. ``observed`` says whether any signal fired."""
        with self._lock:
            retry_waits = {
                "count": self._retry_count,
                "total_seconds": round(self._retry_total, 3),
                "max_seconds": round(self._retry_max, 3),
                "server_floor_count": self._retry_server_floor,
                "cancelled": self._retry_cancelled,
                "by_failure_class": {
                    key: {"count": b["count"], "total_seconds": round(b["total_seconds"], 3)}
                    for key, b in sorted(self._retry_by_class.items())
                },
                "by_label": {
                    key: {"count": b["count"], "total_seconds": round(b["total_seconds"], 3)}
                    for key, b in sorted(self._retry_by_label.items())
                },
            }
            retries_abandoned = {
                "count": self._abandon_count,
                "by_failure_class": dict(sorted(self._abandon_by_class.items())),
                "by_stop": dict(sorted(self._abandon_by_stop.items())),
                "by_label": dict(sorted(self._abandon_by_label.items())),
            }
            permit_waits = {
                "acquisitions": self._permit_acquisitions,
                "waited": self._permit_waited,
                "total_seconds": round(self._permit_total, 3),
                "max_seconds": round(self._permit_max, 3),
                "p50_seconds": round(_percentile(self._permit_samples, 50), 3),
                "p95_seconds": round(_percentile(self._permit_samples, 95), 3),
                "wait_floor_seconds": PERMIT_WAIT_FLOOR_SECONDS,
                "by_pool": {
                    key: {
                        "acquisitions": b["acquisitions"],
                        "waited": b["waited"],
                        "total_seconds": round(b["total_seconds"], 3),
                        "max_seconds": round(b["max_seconds"], 3),
                    }
                    for key, b in sorted(self._permit_by_pool.items())
                },
            }
            singleflight = {
                "count": self._sf_count,
                "total_seconds": round(self._sf_total, 3),
                "max_seconds": round(self._sf_max, 3),
                "timed_out": self._sf_timed_out,
                "findings_orphaned": self._sf_orphaned,
            }
            http = {
                "count": self._http_count,
                "status_429": self._http_429,
                "status_529": self._http_529,
                "status_5xx_other": self._http_5xx,
                "retry_after_count": self._http_retry_after,
            }
            headroom = {
                "responses_with_headers": self._headroom_responses,
                "exhausted_responses": self._headroom_exhausted,
                "low_responses": self._headroom_low,
                "low_threshold": LOW_HEADROOM_FRACTION,
                "lowest": dict(self._headroom_lowest) if self._headroom_lowest else None,
                "by_dimension": {
                    key: dict(value) for key, value in sorted(self._headroom_by_dimension.items())
                },
            }
            batch = {
                "count": self._batch_count,
                "total_seconds": round(self._batch_total, 1),
                "max_seconds": round(self._batch_max, 1),
                "expired_requests": self._batch_expired,
                "detached": dict(sorted(self._batch_detached.items())),
                "terminal": dict(sorted(self._batch_terminal.items())),
            }
        signals: list[str] = []
        throttling = (
            any(key in THROTTLING_CLASSES for key in retry_waits["by_failure_class"])
            or any(key in THROTTLING_CLASSES for key in retries_abandoned["by_failure_class"])
            or http["status_429"]
            or http["status_529"]
            or http["status_5xx_other"]
            or headroom["exhausted_responses"]
        )
        if throttling:
            signals.append(SIGNAL_PROVIDER_THROTTLING)
        if any(key in CONNECTION_CLASSES for key in retry_waits["by_failure_class"]) or any(
            key in CONNECTION_CLASSES for key in retries_abandoned["by_failure_class"]
        ):
            signals.append(SIGNAL_CONNECTION_ERRORS)
        if permit_waits["max_seconds"] >= PERMIT_SIGNAL_SECONDS:
            signals.append(SIGNAL_LOCAL_CONCURRENCY)
        if singleflight["timed_out"]:
            signals.append(SIGNAL_COORDINATION_WAIT)
        if batch["expired_requests"] or any(
            key.startswith(prefix) for key in batch["detached"] for prefix in _BATCH_QUEUE_DETACHES
        ):
            signals.append(SIGNAL_BATCH_QUEUE)
        return {
            "observed": bool(signals),
            "signals": signals,
            "retry_waits": retry_waits,
            "retries_abandoned": retries_abandoned,
            "permit_waits": permit_waits,
            "singleflight_waits": singleflight,
            "http_responses": http,
            "rate_limit_headroom": headroom,
            "batch_polls": batch,
        }


# ---------------------------------------------------------------------------
# The active recorder
# ---------------------------------------------------------------------------

_BOUND: contextvars.ContextVar[Optional[PressureRecorder]] = contextvars.ContextVar(
    "spec_critic_pressure_recorder", default=None
)
_INSTALLED: PressureRecorder | None = None
_INSTALL_LOCK = threading.Lock()


def current() -> PressureRecorder | None:
    """The recorder that receives this thread's records, or ``None``.

    A recorder bound to the current context (:func:`recording`) wins over
    the process-wide installed one (:func:`install`).
    """
    bound = _BOUND.get()
    if bound is not None:
        return bound
    return _INSTALLED


def install(recorder: PressureRecorder | None) -> None:
    """Make ``recorder`` the process-wide recorder (``None`` clears it)."""
    global _INSTALLED
    if recorder is not None and not isinstance(recorder, PressureRecorder):
        raise TypeError("install takes a PressureRecorder or None")
    with _INSTALL_LOCK:
        _INSTALLED = recorder


def uninstall(recorder: PressureRecorder | None) -> bool:
    """Clear the process-wide recorder if it is ``recorder``.

    Returns whether it was cleared. A newer run's recorder is never removed
    by an older run finishing late.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if recorder is not None and _INSTALLED is recorder:
            _INSTALLED = None
            return True
    return False


@contextmanager
def recording(recorder: PressureRecorder) -> Iterator[PressureRecorder]:
    """Bind ``recorder`` to the current context for the block."""
    if not isinstance(recorder, PressureRecorder):
        raise TypeError("recording takes a PressureRecorder")
    token = _BOUND.set(recorder)
    try:
        yield recorder
    finally:
        _BOUND.reset(token)


# ---------------------------------------------------------------------------
# Record helpers (no-ops without a recorder; never raise)
# ---------------------------------------------------------------------------


def record_retry_wait(
    *,
    label: str,
    seconds: float,
    failure_class: str,
    server_floor: bool = False,
    completed: bool = True,
    attempt: int | None = None,
) -> None:
    recorder = current()
    if recorder is None:
        return
    try:
        recorder.record_retry_wait(
            label=label,
            seconds=seconds,
            failure_class=failure_class,
            server_floor=server_floor,
            completed=completed,
            attempt=attempt,
        )
    except Exception:  # noqa: BLE001 — telemetry never fails the run
        pass


def record_retry_abandoned(
    *, label: str, failure_class: str, stop: str, attempts: int | None = None
) -> None:
    recorder = current()
    if recorder is None:
        return
    try:
        recorder.record_retry_abandoned(
            label=label, failure_class=failure_class, stop=stop, attempts=attempts
        )
    except Exception:  # noqa: BLE001
        pass


def record_permit_wait(*, pool: str, seconds: float, acquired: bool = True) -> None:
    recorder = current()
    if recorder is None:
        return
    try:
        recorder.record_permit_wait(pool=pool, seconds=seconds, acquired=acquired)
    except Exception:  # noqa: BLE001
        pass


def record_singleflight_wait(*, seconds: float, timed_out: bool, findings: int = 1) -> None:
    recorder = current()
    if recorder is None:
        return
    try:
        recorder.record_singleflight_wait(seconds=seconds, timed_out=timed_out, findings=findings)
    except Exception:  # noqa: BLE001
        pass


def record_batch_poll(
    *,
    seconds: float,
    terminal_status: str | None,
    expired: int = 0,
    detach_reason: str | None = None,
    poll_failed: bool = False,
    batch_id: str | None = None,
) -> None:
    recorder = current()
    if recorder is None:
        return
    try:
        recorder.record_batch_poll(
            seconds=seconds,
            terminal_status=terminal_status,
            expired=expired,
            detach_reason=detach_reason,
            poll_failed=poll_failed,
            batch_id=batch_id,
        )
    except Exception:  # noqa: BLE001
        pass


def observe_http_response(response: Any) -> None:
    """An ``httpx`` response hook: count the status and read its headroom.

    Installed on the SDK's HTTP client by :func:`observe_sdk_client`, so it
    sees every response the SDK receives — streamed or not, retried inside
    the SDK or by an app loop — before the body is read. It reads headers
    only and never raises.
    """
    recorder = current()
    if recorder is None:
        return
    try:
        recorder.record_http_response(
            status_code=int(getattr(response, "status_code", 0) or 0),
            headers=getattr(response, "headers", None),
        )
    except Exception:  # noqa: BLE001
        pass


def observe_sdk_client(client: Any) -> bool:
    """Attach :func:`observe_http_response` to an SDK client's HTTP client.

    Reaches the ``httpx`` client the Anthropic SDK built (its ``_client``)
    and appends the hook to its public ``event_hooks["response"]`` list,
    once. ``with_options`` views share that HTTP client, so one attachment
    covers them. Returns whether the hook is attached; a client without a
    reachable HTTP client (a test double, a future SDK shape) is left alone.
    """
    try:
        http = getattr(client, "_client", None)
        hooks = getattr(http, "event_hooks", None)
        if not isinstance(hooks, dict):
            return False
        response_hooks = list(hooks.get("response") or ())
        if observe_http_response in response_hooks:
            return True
        response_hooks.append(observe_http_response)
        http.event_hooks = {**hooks, "response": response_hooks}
        return observe_http_response in list(
            (getattr(http, "event_hooks", None) or {}).get("response") or ()
        )
    except Exception:  # noqa: BLE001 — telemetry never breaks client construction
        return False


# ---------------------------------------------------------------------------
# A permit pool that measures its own contention
# ---------------------------------------------------------------------------


class MeteredSemaphore:
    """A bounded semaphore whose acquisitions report how long they blocked.

    Drop-in for the ``threading.BoundedSemaphore`` call permits the loops
    take with ``with gate:``: same context-manager protocol, same
    ``acquire`` / ``release`` surface, same bound. The only addition is one
    :func:`record_permit_wait` per acquisition, with the pool's name, after
    the permit is held — never on the critical path of releasing it.
    """

    def __init__(self, value: int = 1, *, pool: str) -> None:
        self._semaphore = threading.BoundedSemaphore(max(1, int(value)))
        self.value = max(1, int(value))
        self.pool = str(pool)

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        started = time.monotonic()
        acquired = self._semaphore.acquire(blocking, timeout)
        record_permit_wait(
            pool=self.pool, seconds=time.monotonic() - started, acquired=bool(acquired)
        )
        return acquired

    def release(self) -> None:
        self._semaphore.release()

    def __enter__(self) -> "MeteredSemaphore":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:
        return f"MeteredSemaphore(value={self.value}, pool={self.pool!r})"


__all__ = [
    "CAPACITY_CLASSES",
    "CONNECTION_CLASSES",
    "LOW_HEADROOM_FRACTION",
    "MeteredSemaphore",
    "PERMIT_SIGNAL_SECONDS",
    "PERMIT_WAIT_FLOOR_SECONDS",
    "PressureRecorder",
    "SIGNAL_BATCH_QUEUE",
    "SIGNAL_CONNECTION_ERRORS",
    "SIGNAL_COORDINATION_WAIT",
    "SIGNAL_LOCAL_CONCURRENCY",
    "SIGNAL_PROVIDER_THROTTLING",
    "THROTTLING_CLASSES",
    "current",
    "install",
    "observe_http_response",
    "observe_sdk_client",
    "record_batch_poll",
    "record_permit_wait",
    "record_retry_abandoned",
    "record_retry_wait",
    "record_singleflight_wait",
    "recording",
    "uninstall",
]
