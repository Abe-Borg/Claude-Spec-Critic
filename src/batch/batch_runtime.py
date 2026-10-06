"""Shared bounded polling runtime for batch phases.

Retry ownership (plan WP-11): the poll loop owns its retries. Each status read
goes out with SDK retries off (``poll_batch(..., sdk_retries=False)``), and a
failed read is retried here, within ``PollPolicy``'s bounds: a refused request
(authentication, permission, not found, spend cap) stops polling at once; any
other failure waits the response's ``retry-after`` floor when it sends one,
else a jittered exponential backoff, and counts toward
``max_consecutive_errors``. Every wait — between polls and after a failure —
returns as soon as the cancel event is set. Waits and the jitter source come
from :data:`src.verification.retry_policy.DEFAULT_RETRY_TIMING`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .batch import BatchStatus, poll_batch
from ..core import resource_pressure as _pressure
from ..tracing import capture_hooks as _trace

DEFAULT_POLL_INTERVAL_SECONDS = 15
DEFAULT_MAX_ELAPSED_SECONDS = 4 * 3600
DEFAULT_MAX_NO_PROGRESS_SECONDS = 30 * 60
DEFAULT_MAX_CONSECUTIVE_ERRORS = 10

# Phase 5.1 (audit Section 9.1): progressive polling backoff. The initial
# interval keeps short batches snappy; the cap throttles long-running
# batches so we don't pile up needless API calls. ``backoff_after_seconds``
# defines how soon stretching kicks in.
DEFAULT_POLL_BACKOFF_AFTER_SECONDS = 5 * 60
DEFAULT_POLL_MAX_INTERVAL_SECONDS = 120


@dataclass
class PollPolicy:
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    max_elapsed_seconds: int = DEFAULT_MAX_ELAPSED_SECONDS
    max_no_progress_seconds: int = DEFAULT_MAX_NO_PROGRESS_SECONDS
    max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS
    backoff_after_seconds: int = DEFAULT_POLL_BACKOFF_AFTER_SECONDS
    max_poll_interval_seconds: int = DEFAULT_POLL_MAX_INTERVAL_SECONDS


def _progressive_poll_interval(
    *,
    elapsed_seconds: float,
    policy: PollPolicy,
) -> int:
    """Return the wait between polls given how long polling has been running.

    Schedule (audit Section 9.1):
    - First ``backoff_after_seconds``: ``poll_interval_seconds`` (snappy).
    - After that: linearly stretch toward ``max_poll_interval_seconds`` over
      the next equal window, then hold at the max.
    """
    base = max(1, int(policy.poll_interval_seconds))
    cap = max(base, int(policy.max_poll_interval_seconds))
    threshold = max(0, int(policy.backoff_after_seconds))
    if elapsed_seconds <= threshold or cap == base:
        return base
    # Linear ramp: at threshold -> base, at 2*threshold -> cap.
    span = max(1, threshold)
    progress = min(1.0, (elapsed_seconds - threshold) / span)
    interval = int(base + (cap - base) * progress)
    return max(base, min(cap, interval))


# Review batches carry the system's largest inputs (full spec docs) and its
# largest outputs (up to 128k / 300k tokens of deep-reasoning review), so they
# are the slowest batches to land their first completion. The Batches API can
# take up to 24h and frequently returns *every* item in one late burst, so
# "0 completed so far" is a normal interim state, not a stall. The bare 30-min
# no-progress default tripped on legitimate large runs — a 32-spec batch
# detached at ~31 min with 0/32 done while the remote batch was still
# processing — abandoning the run; 30 min is also shorter than the GUI's own
# "45 min to 2 hrs" expectation. Mirror the verification policy: bound the
# review poll by max_elapsed (4h), not by an early no-progress trip. The
# trade-off is slower detection of a genuinely wedged batch (4h vs 30 min),
# which is acceptable: detach is non-destructive (the batch keeps running
# remotely) and the user can cancel at any time.
DEFAULT_REVIEW_POLL_POLICY = PollPolicy(
    max_no_progress_seconds=4 * 3600,
)
DEFAULT_VERIFICATION_POLL_POLICY = PollPolicy(
    max_no_progress_seconds=4 * 3600,
)


@dataclass
class PollOutcome:
    terminal: bool = False
    terminal_status: str | None = None
    final_status: BatchStatus | None = None
    detached: bool = False
    detach_reason: str | None = None
    poll_failed: bool = False
    poll_error: str | None = None
    user_canceled: bool = False


# Batch ``processing_status`` values after which no more items will complete.
# Shared by the poll loop's terminal check and the recovery paths'
# pre-reconstruction check (``ensure_batch_ended``) so "terminal" cannot
# drift between them.
TERMINAL_BATCH_STATUSES = frozenset({"ended", "failed", "expired", "canceled"})


def is_terminal_batch_status(status: str) -> bool:
    """True when ``status`` (an API ``processing_status``) is terminal."""
    return (status or "").replace("-", "_") in TERMINAL_BATCH_STATUSES


class BatchNotFinishedError(RuntimeError):
    """Raised by :func:`ensure_batch_ended` when the batch is still processing.

    Carries the machine-readable ``reason`` (``max_elapsed`` / ``no_progress``
    / ``poll_error_threshold: ...`` / ``user_canceled``) and the last observed
    :class:`BatchStatus` so callers can build surface-appropriate messages;
    ``str(exc)`` is already presentable.
    """

    def __init__(
        self,
        batch_id: str,
        *,
        reason: str,
        status: BatchStatus | None = None,
    ) -> None:
        self.batch_id = batch_id
        self.reason = reason
        self.status = status
        detail = ""
        if status is not None and status.total:
            detail = f" ({status.completed} of {status.total} requests done)"
        super().__init__(
            f"Batch {batch_id} has not finished processing{detail}; "
            f"polling stopped: {reason}. The batch keeps running remotely."
        )


def ensure_batch_ended(
    batch_id: str,
    *,
    policy: PollPolicy,
    log: Callable[..., None],
    progress_cb: Callable[[BatchStatus], None] | None = None,
    cancel_event=None,
) -> BatchStatus:
    """Return the batch's terminal status, polling until it ends when needed.

    Guard for the bare-id recovery paths: reconstructing a submission from a
    batch's results (``thin_submission_from_batch_results``) reads the results
    stream, which does not exist until the batch ends — pointing it at a
    still-running batch fails with the SDK's raw "No ``results_url`` for the
    given batch" error. Call this first so an in-progress batch is polled to
    completion (bounded by ``policy``) before any results are read.

    The initial status check fails fast only on NON-retryable errors (a
    typo'd batch id, auth failure — via the shared
    :func:`retry_policy.classify_exception` taxonomy) so a bad id raises
    immediately instead of backing off for minutes. A *transient* failure
    (connection drop, 5xx/529, rate limit) does not abort the recovery: it
    falls through to the bounded poll loop below, which re-polls under its
    own consecutive-error backoff — matching the retry behavior the
    results-download path already had before this guard existed. An
    already-ended batch returns from the single successful check with no
    waiting.

    Raises :class:`BatchNotFinishedError` when the poll bound is hit (or the
    poll loop gives up / is canceled) while the batch is still processing.
    """
    from ..verification.retry_policy import (  # local import — mirrors
        # batch._collect_batch_results_with_retry, keeping this module's
        # import surface (and any circular-import risk) small.
        classify_exception,
        is_retryable_failure_class,
    )

    status: BatchStatus | None = None
    try:
        # SDK retries off: a transient failure is retried by the poll loop
        # below, the one retry owner for status reads.
        status = poll_batch(batch_id, sdk_retries=False)
    except Exception as exc:  # noqa: BLE001 — classified, re-raised if terminal
        if not is_retryable_failure_class(classify_exception(exc)):
            raise
        log(
            f"Batch status check failed transiently ({exc}); "
            "retrying via the poll loop...",
            level="warning",
        )
    if status is not None:
        if is_terminal_batch_status(status.status):
            return status
        log(
            f"Batch {batch_id} is still processing "
            f"({status.completed} of {status.total} requests done). "
            "Waiting for it to finish before collecting results...",
            level="warning",
        )
    last_seen: dict[str, BatchStatus | None] = {"status": status}

    def _observe(s: BatchStatus) -> None:
        last_seen["status"] = s
        if progress_cb is not None:
            progress_cb(s)

    outcome = poll_batch_bounded(
        batch_id,
        policy=policy,
        log=log,
        progress_cb=_observe,
        cancel_event=cancel_event,
    )
    if outcome.terminal and outcome.final_status is not None:
        return outcome.final_status
    reason = (
        "user_canceled"
        if outcome.user_canceled
        else outcome.detach_reason or outcome.poll_error or "unknown"
    )
    raise BatchNotFinishedError(batch_id, reason=reason, status=last_seen["status"])


# The ceiling on one wait after a failed status read (the local backoff).
POLL_ERROR_MAX_BACKOFF_SECONDS = 300
# Local waits after a failed read are drawn from [(1 - j) x d, d].
POLL_ERROR_JITTER_FRACTION = 0.5


def _poll_error_wait(
    exc: BaseException,
    *,
    consecutive_errors: int,
    policy: PollPolicy,
    timing,
):
    """``(seconds, server_delay)`` to wait after a failed status read.

    The response's ``retry-after`` floor (plus spread) when it sends a valid
    one, else ``poll_interval x 2 ** consecutive_errors`` capped at
    :data:`POLL_ERROR_MAX_BACKOFF_SECONDS`, jittered.
    """
    from ..verification.retry_policy import (
        DEFAULT_REALTIME_RETRY_POLICY,
        jittered_backoff,
        jittered_server_floor,
        server_delay_for,
    )

    try:
        now = float(timing.now())
    except Exception:  # noqa: BLE001 — no clock: an HTTP-date floor cannot be read
        now = float("nan")
    server = server_delay_for(exc, now=now)
    if server is not None:
        wait = jittered_server_floor(
            server.seconds,
            spread_fraction=DEFAULT_REALTIME_RETRY_POLICY.server_jitter_fraction,
            spread_min_seconds=DEFAULT_REALTIME_RETRY_POLICY.server_jitter_min_seconds,
            rng=timing.random,
        )
        return wait, server
    nominal = min(
        policy.poll_interval_seconds * (2 ** min(consecutive_errors, 32)),
        POLL_ERROR_MAX_BACKOFF_SECONDS,
    )
    return jittered_backoff(nominal, jitter_fraction=POLL_ERROR_JITTER_FRACTION, rng=timing.random), None


def poll_batch_bounded(
    batch_id: str,
    *,
    policy: PollPolicy,
    log: Callable[..., None],
    progress_cb: Callable[[BatchStatus], None],
    cancel_event=None,
) -> PollOutcome:
    """Poll ``batch_id`` within ``policy``'s bounds (see the module docstring).

    The poll itself is :func:`_poll_batch_loop`; this wrapper reports the
    outcome to the resource-pressure ledger — how long the batch was polled,
    items the API expired, a detach for lack of progress — after the fact.
    A poll the operator cancelled is not pressure and is not recorded.
    """
    started = time.monotonic()
    outcome = _poll_batch_loop(
        batch_id,
        policy=policy,
        log=log,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
    )
    if not outcome.user_canceled:
        final = outcome.final_status
        _pressure.record_batch_poll(
            seconds=time.monotonic() - started,
            terminal_status=outcome.terminal_status,
            expired=int(getattr(final, "expired", 0) or 0),
            detach_reason=outcome.detach_reason,
            poll_failed=outcome.poll_failed,
            batch_id=batch_id,
        )
    return outcome


def _poll_batch_loop(
    batch_id: str,
    *,
    policy: PollPolicy,
    log: Callable[..., None],
    progress_cb: Callable[[BatchStatus], None],
    cancel_event=None,
) -> PollOutcome:
    from ..verification.retry_policy import (
        classify_exception,
        current_retry_timing,
        is_refused_request_class,
    )

    timing = current_retry_timing()
    started = time.monotonic()
    last_completed_count = 0
    last_progress_time = started
    consecutive_errors = 0

    while True:
        if cancel_event and cancel_event.is_set():
            return PollOutcome(user_canceled=True)

        now = time.monotonic()
        if now - started > policy.max_elapsed_seconds:
            log(
                f"Polling timed out after {policy.max_elapsed_seconds / 3600:.1f}h. "
                "Remote batch may still be running.",
                level="warning",
            )
            _trace.capture_note(
                None, "batch poll detached",
                batch_id=batch_id, reason="max_elapsed",
                elapsed_hours=policy.max_elapsed_seconds / 3600,
            )
            return PollOutcome(detached=True, detach_reason="max_elapsed")

        if now - last_progress_time > policy.max_no_progress_seconds:
            log(
                f"No progress for {policy.max_no_progress_seconds / 60:.0f} minutes. "
                "Remote batch may still be running.",
                level="warning",
            )
            _trace.capture_note(
                None, "batch poll detached",
                batch_id=batch_id, reason="no_progress",
                no_progress_minutes=policy.max_no_progress_seconds / 60,
            )
            return PollOutcome(detached=True, detach_reason="no_progress")

        try:
            # SDK retries off: this loop is the one retry owner for status
            # reads (plan WP-11), so its waits are the only waits.
            status = poll_batch(batch_id, sdk_retries=False)
            consecutive_errors = 0
        except Exception as exc:
            failure_class = classify_exception(exc)
            if is_refused_request_class(failure_class):
                # Authentication, permission, not found, spend cap: no wait
                # makes the next read succeed, so stop now rather than after
                # ten backoffs. The batch itself is untouched.
                log(
                    f"Polling stopped: the API refused the status request "
                    f"({failure_class.value}): {exc}. Remote batch may still be running.",
                    level="error",
                )
                return PollOutcome(
                    poll_failed=True,
                    poll_error=f"poll_refused ({failure_class.value}): {exc}",
                )
            consecutive_errors += 1
            if consecutive_errors >= policy.max_consecutive_errors:
                log(
                    f"Polling failed {consecutive_errors} times consecutively. "
                    "Remote batch may still be running.",
                    level="error",
                )
                return PollOutcome(poll_failed=True, poll_error=f"poll_error_threshold: {exc}")
            backoff, server = _poll_error_wait(
                exc, consecutive_errors=consecutive_errors, policy=policy, timing=timing
            )
            remaining = max(0.0, policy.max_elapsed_seconds - (time.monotonic() - started))
            if server is not None and server.seconds > remaining:
                # Never shorten the server's wait: past the polling bound,
                # detach (the batch keeps running and can be resumed).
                log(
                    f"The API asked to wait {server.seconds:.0f}s before polling "
                    f"again, past the {policy.max_elapsed_seconds / 3600:.1f}h "
                    "polling bound. Remote batch may still be running.",
                    level="warning",
                )
                return PollOutcome(
                    detached=True,
                    detach_reason=(
                        f"retry_after_exceeds_poll_bound: the API asked to wait "
                        f"{server.seconds:.0f}s"
                    ),
                )
            # The spread added to a floor (and a long local backoff) never
            # carries the wait past the polling bound; a floor is never cut
            # below itself, since it fits (checked above).
            error_wait = min(backoff, remaining)
            completed = bool(timing.wait(error_wait, cancel_event))
            # Observation only, after the wait (the ledger cannot change it).
            _pressure.record_retry_wait(
                label="batch_poll",
                seconds=error_wait,
                failure_class=failure_class.value,
                server_floor=server is not None,
                completed=completed,
                attempt=consecutive_errors,
            )
            if not completed:
                return PollOutcome(user_canceled=True)
            continue

        current_completed = status.succeeded + status.errored + status.canceled + status.expired
        if current_completed > last_completed_count:
            last_completed_count = current_completed
            last_progress_time = time.monotonic()

        progress_cb(status)

        if is_terminal_batch_status(status.status):
            _trace.capture_note(
                None, "batch poll terminal",
                batch_id=batch_id, terminal_status=status.status,
                succeeded=status.succeeded, errored=status.errored,
                canceled=status.canceled, expired=status.expired,
            )
            return PollOutcome(terminal=True, terminal_status=status.status, final_status=status)

        # Progressive backoff (audit Section 9.1): start at the configured
        # interval, then stretch toward max_poll_interval_seconds for
        # long-running batches. The wait returns as soon as the cancel
        # event is set.
        if not timing.wait(
            _progressive_poll_interval(
                elapsed_seconds=time.monotonic() - started,
                policy=policy,
            ),
            cancel_event,
        ):
            return PollOutcome(user_canceled=True)
