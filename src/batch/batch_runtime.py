"""Shared bounded polling runtime for batch phases."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .batch import BatchStatus, poll_batch
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

    The initial status check deliberately bypasses the poll loop's
    consecutive-error retry: a typo'd batch id or auth failure raises
    immediately instead of backing off for minutes. An already-ended batch
    returns from that single check with no waiting.

    Raises :class:`BatchNotFinishedError` when the poll bound is hit (or the
    poll loop gives up / is canceled) while the batch is still processing.
    """
    status = poll_batch(batch_id)
    if is_terminal_batch_status(status.status):
        return status

    log(
        f"Batch {batch_id} is still processing "
        f"({status.completed} of {status.total} requests done). "
        "Waiting for it to finish before collecting results...",
        level="warning",
    )
    last_seen = {"status": status}

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


def poll_batch_bounded(
    batch_id: str,
    *,
    policy: PollPolicy,
    log: Callable[..., None],
    progress_cb: Callable[[BatchStatus], None],
    cancel_event=None,
) -> PollOutcome:
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
            status = poll_batch(batch_id)
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            if consecutive_errors >= policy.max_consecutive_errors:
                log(
                    f"Polling failed {consecutive_errors} times consecutively. "
                    "Remote batch may still be running.",
                    level="error",
                )
                return PollOutcome(poll_failed=True, poll_error=f"poll_error_threshold: {exc}")
            backoff = min(policy.poll_interval_seconds * (2 ** consecutive_errors), 300)
            time.sleep(backoff)
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
        # long-running batches.
        time.sleep(
            _progressive_poll_interval(
                elapsed_seconds=time.monotonic() - started,
                policy=policy,
            )
        )
