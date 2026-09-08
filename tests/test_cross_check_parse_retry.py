"""Cross-check parse failures get exactly one re-request (B-7).

``_extract_json_array`` raises a bare ``ValueError`` when a response carries
no tool call and no findings JSON. Before this, that surfaced through
``classify_exception`` as ``FailureClass.UNKNOWN`` — non-retryable — so a
single unparseable response produced a terminal ``parse_error`` on attempt
one, even though the review path treats ``parse_error`` as repairable. The
cross-check loop now classifies it as ``FailureClass.PARSE_ERROR`` explicitly
and grants **one** re-request; a second parse failure is terminal with no
third attempt. The global retryable set is unchanged.
"""
from __future__ import annotations

import pytest

import src.cross_check.cross_checker as cc
from src.core.code_cycles import DEFAULT_CYCLE
from src.cross_check.cross_checker import run_cross_check
from src.input.extractor import ExtractedSpec
from src.review.structured_schemas import CROSS_CHECK_TOOL_NAME
from src.verification.retry_policy import FailureClass, is_retryable_failure_class
from tests.fixtures.fake_anthropic import FakeMessage, FakeTextBlock, FakeToolUseBlock


# ---------------------------------------------------------------------------
# Scripted streaming client
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @property
    def text_stream(self):
        for block in self._message.content:
            text = getattr(block, "text", None)
            if text:
                yield text

    def get_final_message(self):
        return self._message


class _FakeMessages:
    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        assert self._script, "script exhausted: more calls than planned"
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeStream(item)


class _FakeClient:
    def __init__(self, script: list):
        self.messages = _FakeMessages(script)

    @property
    def calls(self):
        return self.messages.calls


def _specs() -> list[ExtractedSpec]:
    return [
        ExtractedSpec(filename="22 11 13 Water.docx", content="Provide domestic water piping.", word_count=4),
        ExtractedSpec(filename="23 05 00 HVAC.docx", content="Provide HVAC equipment per schedule.", word_count=5),
    ]


def _ok_message() -> FakeMessage:
    return FakeMessage(
        content=[
            FakeToolUseBlock(
                name=CROSS_CHECK_TOOL_NAME,
                input={"findings": [], "coordination_summary": "Coordination appears adequate."},
            )
        ],
        stop_reason="tool_use",
    )


def _unparseable_message() -> FakeMessage:
    # No tool call, no findings JSON, nothing the tagged-JSON fallback accepts.
    return FakeMessage(
        content=[FakeTextBlock(text="I could not produce a findings list this time.")],
        stop_reason="end_turn",
    )


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Hermetic loop: word-count tokens, recorded (not slept) backoffs."""
    monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))
    recorded: list[float] = []
    monkeypatch.setattr(cc.time, "sleep", lambda seconds: recorded.append(seconds))
    return recorded


def _install(monkeypatch, script: list) -> _FakeClient:
    client = _FakeClient(script)
    monkeypatch.setattr(cc, "_get_client", lambda *_a, **_k: client)
    return client


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def test_parse_failure_gets_exactly_one_re_request(monkeypatch, sleeps):
    client = _install(monkeypatch, [_unparseable_message(), _ok_message()])

    result = run_cross_check(_specs(), [], cycle=DEFAULT_CYCLE)

    assert result.cross_check_status == "completed"
    assert result.parse_status == "ok"
    assert len(client.calls) == 2
    # The re-request waits one backoff so the loop never hammers the API.
    assert len(sleeps) == 1


def test_second_parse_failure_is_terminal_with_no_third_attempt(monkeypatch, sleeps):
    client = _install(
        monkeypatch, [_unparseable_message(), _unparseable_message(), _ok_message()]
    )

    result = run_cross_check(_specs(), [], cycle=DEFAULT_CYCLE)

    assert result.cross_check_status == "failed"
    assert result.parse_status == "parse_error"
    assert result.error and result.error.startswith("Error:")
    # Two attempts made, the third (which would have succeeded) never issued.
    assert len(client.calls) == 2
    assert len(sleeps) == 1


def test_parse_retry_never_exceeds_the_attempt_budget(monkeypatch, sleeps):
    client = _install(monkeypatch, [_unparseable_message(), _ok_message()])

    result = run_cross_check(_specs(), [], cycle=DEFAULT_CYCLE, max_retries=1)

    assert result.cross_check_status == "failed"
    assert result.parse_status == "parse_error"
    assert len(client.calls) == 1
    assert sleeps == []


def test_transient_failure_and_parse_retry_budgets_are_independent(monkeypatch, sleeps):
    # attempt 1: transient (retryable class) → attempt 2: unparseable (the one
    # parse retry) → attempt 3: clean payload.
    client = _install(
        monkeypatch,
        [RuntimeError("connection reset by peer"), _unparseable_message(), _ok_message()],
    )

    result = run_cross_check(_specs(), [], cycle=DEFAULT_CYCLE)

    assert result.cross_check_status == "completed"
    assert len(client.calls) == 3
    assert len(sleeps) == 2


def test_parse_error_stays_outside_the_global_retryable_set():
    """The single retry is granted inside the cross-check loop only; the
    shared taxonomy still treats PARSE_ERROR as non-retryable."""
    assert is_retryable_failure_class(FailureClass.PARSE_ERROR) is False


def test_non_retryable_api_error_is_still_terminal_on_attempt_one(monkeypatch, sleeps):
    client = _install(monkeypatch, [RuntimeError("something unrelated broke"), _ok_message()])

    result = run_cross_check(_specs(), [], cycle=DEFAULT_CYCLE)

    assert result.cross_check_status == "failed"
    assert len(client.calls) == 1
    assert sleeps == []
