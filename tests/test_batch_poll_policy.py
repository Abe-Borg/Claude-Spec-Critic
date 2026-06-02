"""Regression: the review batch poll policy must not give up while a large
batch is still legitimately processing.

A 32-spec run detached at ~31 minutes with 0/32 completed because the review
poll policy used the bare 30-minute ``max_no_progress_seconds`` default while
the remote batch was still processing. The no-progress clock only advances when
an item *completes*, so a large batch whose first completion lands after 30 min
(normal for the Batches API, which can take up to 24h and often returns every
item in one late burst) tripped the guard and abandoned the run.

These tests lock in that the review policy bounds polling by ``max_elapsed``
(like verification) rather than by an early no-progress trip, and prove the
old 30-min window would have detached the reported run while the new window
does not.
"""

from src.batch.batch_runtime import (
    DEFAULT_MAX_NO_PROGRESS_SECONDS,
    DEFAULT_REVIEW_POLL_POLICY,
    DEFAULT_VERIFICATION_POLL_POLICY,
    PollPolicy,
    poll_batch_bounded,
)
from src.batch.batch import BatchStatus


def test_review_no_progress_window_matches_verification():
    """Review and verification must share the same no-progress tolerance.

    Review batches are the slowest (largest inputs + largest outputs), so they
    are the *most* likely to have a long zero-completion prefix — leaving them
    on the trigger-happy default while verification got 4h was the bug.
    """
    assert (
        DEFAULT_REVIEW_POLL_POLICY.max_no_progress_seconds
        == DEFAULT_VERIFICATION_POLL_POLICY.max_no_progress_seconds
    )


def test_review_no_progress_window_exceeds_bare_default():
    """The review policy must override the 30-min default that caused the stall."""
    assert (
        DEFAULT_REVIEW_POLL_POLICY.max_no_progress_seconds
        > DEFAULT_MAX_NO_PROGRESS_SECONDS
    )
    # And it must comfortably clear the GUI's advertised "45 min to 2 hrs"
    # typical completion window, or the poller would again abandon runs before
    # its own stated expectation.
    assert DEFAULT_REVIEW_POLL_POLICY.max_no_progress_seconds >= 2 * 3600


def _processing_status(total: int = 32) -> BatchStatus:
    """A batch with every item still processing (0 completed) — the stalled state."""
    return BatchStatus(
        status="in_progress",
        processing=total,
        succeeded=0,
        errored=0,
        canceled=0,
        expired=0,
        total=total,
    )


def test_old_30min_window_would_detach_the_reported_run(monkeypatch):
    """Proof the regression existed: under the old 30-min window a never-
    completing poll detaches with reason ``no_progress``."""
    import src.batch.batch_runtime as rt

    monkeypatch.setattr(rt, "poll_batch", lambda _bid: _processing_status())
    # Make time march forward past 30 min on each sleep without real waiting.
    clock = {"t": 0.0}
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rt.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + 600))

    old_policy = PollPolicy(max_no_progress_seconds=DEFAULT_MAX_NO_PROGRESS_SECONDS)
    outcome = poll_batch_bounded(
        "msgbatch_test",
        policy=old_policy,
        log=lambda *a, **k: None,
        progress_cb=lambda _s: None,
    )
    assert outcome.detached is True
    assert outcome.detach_reason == "no_progress"


def test_current_review_policy_survives_past_30min(monkeypatch):
    """With the fix, the same never-completing poll keeps polling past 30 min
    and only stops at ``max_elapsed`` (reason ``max_elapsed``), never an early
    ``no_progress`` trip."""
    import src.batch.batch_runtime as rt

    monkeypatch.setattr(rt, "poll_batch", lambda _bid: _processing_status())
    clock = {"t": 0.0}
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rt.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + 600))

    outcome = poll_batch_bounded(
        "msgbatch_test",
        policy=DEFAULT_REVIEW_POLL_POLICY,
        log=lambda *a, **k: None,
        progress_cb=lambda _s: None,
    )
    # It must NOT bail out early on no_progress; the only legitimate stop for a
    # genuinely never-completing batch is the elapsed ceiling.
    assert outcome.detached is True
    assert outcome.detach_reason == "max_elapsed"
