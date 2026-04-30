"""Shared bounded polling runtime for batch phases."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .batch import BatchStatus, poll_batch

DEFAULT_POLL_INTERVAL_SECONDS = 15
DEFAULT_MAX_ELAPSED_SECONDS = 4 * 3600
DEFAULT_MAX_NO_PROGRESS_SECONDS = 30 * 60
DEFAULT_MAX_CONSECUTIVE_ERRORS = 10
DEFAULT_MAX_POLL_INTERVAL_SECONDS = 120


@dataclass
class PollPolicy:
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    max_elapsed_seconds: int = DEFAULT_MAX_ELAPSED_SECONDS
    max_no_progress_seconds: int = DEFAULT_MAX_NO_PROGRESS_SECONDS
    max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS
    max_poll_interval_seconds: int = DEFAULT_MAX_POLL_INTERVAL_SECONDS


def _compute_poll_interval(elapsed_seconds: float, base_interval: int, max_interval: int) -> int:
    """Progressive backoff: keep base_interval for ~5 minutes, then double up to max.

    Short batches (<5 min) get prompt updates at the base interval. Longer batches
    get progressively quieter polling so we don't hammer the API for hours.
    """
    if elapsed_seconds < 5 * 60:
        return base_interval
    if elapsed_seconds < 15 * 60:
        return min(base_interval * 2, max_interval)
    if elapsed_seconds < 30 * 60:
        return min(base_interval * 4, max_interval)
    return max_interval


DEFAULT_REVIEW_POLL_POLICY = PollPolicy()
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


def poll_batch_bounded(
    batch_id: str,
    *,
    policy: PollPolicy,
    log: Callable[[str], None],
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
                "Remote batch may still be running."
            )
            return PollOutcome(detached=True, detach_reason="max_elapsed")

        if now - last_progress_time > policy.max_no_progress_seconds:
            log(
                f"No progress for {policy.max_no_progress_seconds / 60:.0f} minutes. "
                "Remote batch may still be running."
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
                    "Remote batch may still be running."
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

        normalized = status.status.replace("-", "_")
        if normalized in ("ended", "failed", "expired", "canceled"):
            return PollOutcome(terminal=True, terminal_status=status.status, final_status=status)

        elapsed = time.monotonic() - started
        time.sleep(
            _compute_poll_interval(elapsed, policy.poll_interval_seconds, policy.max_poll_interval_seconds)
        )
