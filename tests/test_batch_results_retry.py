"""Connection-drop retry for the batch-results streaming download.

Regression for the run that crashed at ~92% with::

    peer closed connection without sending complete message body
    (incomplete chunked read)

The verification batch had completed server-side (288/288 done), but the
single long-lived chunked download of the results dropped mid-stream and
took the whole run down because the stream iteration had no retry. The
failure taxonomy already classified that message as a retryable CONNECTION
failure (``retry_policy._CONNECTION_PATTERNS``); these tests pin that the
download path now actually uses it — re-fetching the whole stream from
scratch (the results endpoint is not resumable) on connection-class errors,
while still propagating non-retryable errors immediately.
"""
from __future__ import annotations

import pytest

from src.batch import batch as batch_mod
from src.verification.retry_policy import DEFAULT_REALTIME_RETRY_POLICY


class _Result:
    def __init__(self, custom_id: str) -> None:
        self.custom_id = custom_id


class _FlakyBatches:
    """Stand-in for ``client.messages.batches`` whose ``.results()`` raises
    ``exc`` for the first ``fail_times`` calls, then yields ``payload``."""

    def __init__(self, *, fail_times: int, exc: BaseException, payload: list[_Result]) -> None:
        self._fail_times = fail_times
        self._exc = exc
        self._payload = payload
        self.calls = 0

    def results(self, batch_id: str):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return iter(self._payload)


class _FakeClient:
    def __init__(self, batches: _FlakyBatches) -> None:
        self.messages = type("_Messages", (), {"batches": batches})()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # The retry loop backs off with time.sleep; don't actually wait in tests.
    monkeypatch.setattr(batch_mod.time, "sleep", lambda *a, **k: None)


def _install(monkeypatch, batches: _FlakyBatches) -> _FlakyBatches:
    monkeypatch.setattr(batch_mod, "_get_client", lambda: _FakeClient(batches))
    return batches


def test_retries_incomplete_chunked_read_then_succeeds(monkeypatch):
    # The exact transport error from the crash, surfaced unwrapped as a
    # generic Exception (httpx.RemoteProtocolError escapes the SDK's typed
    # translation on the results stream).
    exc = Exception(
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )
    batches = _install(
        monkeypatch,
        _FlakyBatches(fail_times=1, exc=exc, payload=[_Result("a"), _Result("b")]),
    )

    out = batch_mod._collect_batch_results_with_retry("msgbatch_x")

    assert set(out) == {"a", "b"}
    # One dropped stream + one clean re-fetch. The partial first attempt is
    # discarded, not merged.
    assert batches.calls == 2


def test_non_retryable_error_propagates_without_retry(monkeypatch):
    # A generic error classifies as UNKNOWN → not retryable → propagate on
    # the first attempt. No silent swallow, no spinning on a real bug.
    batches = _install(
        monkeypatch,
        _FlakyBatches(fail_times=99, exc=ValueError("totally unexpected"), payload=[]),
    )

    with pytest.raises(ValueError):
        batch_mod._collect_batch_results_with_retry("msgbatch_x")

    assert batches.calls == 1


def test_gives_up_after_max_attempts(monkeypatch):
    # Every attempt drops → after the shared policy's max_attempts the last
    # exception propagates, so the run surfaces a real failure rather than
    # looping forever against a persistent middlebox cut.
    exc = Exception("incomplete chunked read")
    batches = _install(
        monkeypatch,
        _FlakyBatches(fail_times=99, exc=exc, payload=[]),
    )

    with pytest.raises(Exception):
        batch_mod._collect_batch_results_with_retry("msgbatch_x")

    assert batches.calls == DEFAULT_REALTIME_RETRY_POLICY.max_attempts
