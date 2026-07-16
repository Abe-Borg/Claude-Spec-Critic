"""Recovering a still-running batch: poll to completion first, then reconstruct.

The bare-id recovery paths (the GUI "Recover batch…" dialog and
``scripts/recover_batch.py --batch-id``) rebuild the request map from the
batch's *results* stream (``thin_submission_from_batch_results``), which does
not exist until the batch ends — pointing them at an in-progress batch failed
with the SDK's raw error ("No ``results_url`` for the given batch; Has it
finished processing? in_progress"; observed live recovering a batch ~4h into
slow remote processing). ``batch_runtime.ensure_batch_ended`` closes the gap:

- one immediate status check first, so a typo'd batch id / auth failure raises
  at once (never absorbed into the poll loop's consecutive-error backoff) and
  an already-ended batch passes through with no waiting;
- the standard bounded poll loop when the batch is still processing;
- a typed ``BatchNotFinishedError`` when the poll bound is hit, so both
  recovery surfaces render "still processing — try again later" instead of a
  raw SDK error.
"""
from __future__ import annotations

import pytest

import src.batch.batch_runtime as rt
from src.batch.batch import BatchStatus
from src.batch.batch_runtime import (
    BatchNotFinishedError,
    DEFAULT_REVIEW_POLL_POLICY,
    PollPolicy,
    ensure_batch_ended,
    is_terminal_batch_status,
)


def _status(
    *, processing: int = 2, succeeded: int = 0, status: str = "in_progress"
) -> BatchStatus:
    return BatchStatus(
        status=status,
        processing=processing,
        succeeded=succeeded,
        errored=0,
        canceled=0,
        expired=0,
        total=processing + succeeded,
    )


def _no_sleep(_seconds):  # pragma: no cover - failure path only
    raise AssertionError("must not sleep for an already-ended batch")


def _noop_log(*_a, **_k):
    return None


class TestTerminalStatusHelper:
    def test_terminal_and_non_terminal_statuses(self):
        assert is_terminal_batch_status("ended")
        assert is_terminal_batch_status("canceled")
        assert is_terminal_batch_status("expired")
        assert is_terminal_batch_status("failed")
        assert not is_terminal_batch_status("in_progress")
        assert not is_terminal_batch_status("canceling")
        assert not is_terminal_batch_status("")

    def test_hyphenated_variants_normalize(self):
        # Defensive parity with poll_batch_bounded's legacy normalization.
        assert not is_terminal_batch_status("in-progress")


class TestEnsureBatchEnded:
    def test_ended_batch_returns_after_single_status_check(self, monkeypatch):
        calls = {"n": 0}

        def fake_poll(_bid):
            calls["n"] += 1
            return _status(processing=0, succeeded=2, status="ended")

        monkeypatch.setattr(rt, "poll_batch", fake_poll)
        monkeypatch.setattr(rt.time, "sleep", _no_sleep)

        out = ensure_batch_ended(
            "msgbatch_done", policy=PollPolicy(), log=_noop_log
        )
        assert out.status == "ended"
        assert out.succeeded == 2
        assert calls["n"] == 1

    def test_in_progress_batch_is_polled_to_completion(self, monkeypatch):
        # First status is consumed by the immediate pre-check; the poll loop
        # then sees progress and finally the terminal state.
        seq = [
            _status(),
            _status(),
            _status(processing=1, succeeded=1),
            _status(processing=0, succeeded=2, status="ended"),
        ]

        def fake_poll(_bid):
            return seq.pop(0) if seq else _status(
                processing=0, succeeded=2, status="ended"
            )

        monkeypatch.setattr(rt, "poll_batch", fake_poll)
        clock = {"t": 0.0}
        monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            rt.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s)
        )

        observed: list[BatchStatus] = []
        out = ensure_batch_ended(
            "msgbatch_slow",
            policy=PollPolicy(),
            log=_noop_log,
            progress_cb=observed.append,
        )
        assert out.status == "ended"
        assert out.succeeded == 2
        # The caller's progress callback saw every poll-loop status, ending
        # with the terminal one.
        assert observed
        assert observed[-1].status == "ended"

    def test_never_finishing_batch_raises_typed_error(self, monkeypatch):
        monkeypatch.setattr(rt, "poll_batch", lambda _bid: _status())
        clock = {"t": 0.0}
        monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            rt.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + 600)
        )

        with pytest.raises(BatchNotFinishedError) as exc_info:
            ensure_batch_ended(
                "msgbatch_stuck",
                policy=DEFAULT_REVIEW_POLL_POLICY,
                log=_noop_log,
            )
        err = exc_info.value
        assert err.batch_id == "msgbatch_stuck"
        assert err.reason == "max_elapsed"
        # str(exc) must be presentable on its own (the CLI prints it verbatim).
        assert "msgbatch_stuck" in str(err)
        assert "has not finished processing" in str(err)
        assert "0 of 2 requests done" in str(err)

    def test_bad_batch_id_fails_fast_without_retry_backoff(self, monkeypatch):
        """The immediate pre-check must let a 404 propagate unchanged — never
        absorbed into the poll loop's 10-strike consecutive-error backoff
        (minutes of sleeping for a typo'd id)."""

        class FakeNotFound(Exception):
            status_code = 404

        calls = {"n": 0}

        def fake_poll(_bid):
            calls["n"] += 1
            raise FakeNotFound("not_found")

        monkeypatch.setattr(rt, "poll_batch", fake_poll)
        monkeypatch.setattr(rt.time, "sleep", _no_sleep)

        with pytest.raises(FakeNotFound):
            ensure_batch_ended(
                "msgbatch_typo", policy=PollPolicy(), log=_noop_log
            )
        assert calls["n"] == 1

    def test_user_cancel_raises_typed_error(self, monkeypatch):
        import threading

        monkeypatch.setattr(rt, "poll_batch", lambda _bid: _status())
        cancel = threading.Event()
        cancel.set()

        with pytest.raises(BatchNotFinishedError) as exc_info:
            ensure_batch_ended(
                "msgbatch_cxl",
                policy=PollPolicy(),
                log=_noop_log,
                cancel_event=cancel,
            )
        assert exc_info.value.reason == "user_canceled"
