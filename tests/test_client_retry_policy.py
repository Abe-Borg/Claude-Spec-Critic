"""B-5: app-level retry loops must not stack on the SDK's own retries.

``reviewer._get_client`` hands out two flavors of the one cached client:
the default keeps the SDK's built-in retries (bare, single-shot call sites —
batch submit / poll, token counting, triage), and ``sdk_retries=False``
returns a ``max_retries=0`` view for call sites that own a
``retry_policy`` loop, so one rate-limited call is (attempts) HTTP
requests, not (attempts × SDK retries). The real-time verification loop is
the adopter this codebase owns; the factory docstring lists the others.
"""
from __future__ import annotations

import anthropic
import pytest

from src.core.code_cycles import DEFAULT_CYCLE
from src.review import reviewer
from src.review.reviewer import Finding, _get_client
import src.verification.verifier as V
from tests.fixtures.fake_anthropic import FakeMessage, FakeUsage


@pytest.fixture(autouse=True)
def _fresh_client_cache(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-retry-policy")
    monkeypatch.setattr(reviewer, "_cached_client", None)
    monkeypatch.setattr(reviewer, "_cached_key", None)


def _sdk_default_max_retries() -> int:
    return anthropic.Anthropic(api_key="sk-reference").max_retries


# ---------------------------------------------------------------------------
# 1. The factory
# ---------------------------------------------------------------------------


class TestClientFactoryRetryPolicy:
    def test_default_keeps_sdk_retries(self) -> None:
        client = _get_client()
        assert isinstance(client, anthropic.Anthropic)
        assert client.max_retries == _sdk_default_max_retries()
        assert client.max_retries > 0

    def test_sdk_retries_off_returns_a_zero_retry_view(self) -> None:
        base = _get_client()
        view = _get_client(sdk_retries=False)
        assert view.max_retries == 0
        # Same credentials and the same underlying HTTP connection pool —
        # a per-call wrapper, not a second client.
        assert view.api_key == base.api_key
        assert view._client is base._client

    def test_zero_retry_view_never_mutates_the_cached_client(self) -> None:
        _get_client(sdk_retries=False)
        assert _get_client().max_retries == _sdk_default_max_retries()
        assert reviewer._cached_client.max_retries == _sdk_default_max_retries()

    def test_one_cached_client_backs_both_flavors(self) -> None:
        base = _get_client()
        _get_client(sdk_retries=False)
        assert _get_client() is base
        assert reviewer._cached_client is base

    def test_key_change_rebuilds_the_cached_client(self, monkeypatch) -> None:
        first = _get_client()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-rotated")
        second = _get_client()
        assert second is not first
        assert second.api_key == "sk-test-rotated"


# ---------------------------------------------------------------------------
# 2. The real-time verification loop is an adopter
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._message


class _FakeMessagesAPI:
    def __init__(self, message):
        self._message = message
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(self._message)


def _finding() -> Finding:
    return Finding(
        severity="MEDIUM",
        fileName="21 13 13 - Wet-Pipe Sprinkler.docx",
        section="2.4",
        issue="Sprinkler spacing cited exceeds the maximum for the listed hazard class.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="NFPA 13 §10.2.4",
        confidence=0.5,
    )


class TestRealtimeVerificationOwnsItsRetries:
    def test_realtime_verification_requests_the_no_sdk_retry_client(self, monkeypatch) -> None:
        seen: list[dict] = []
        # A terminal, content-less response: the loop returns a failed
        # UNVERIFIED and makes no second attempt (max_retries=0), so the
        # only thing under test is which client flavor the loop asked for.
        api = _FakeMessagesAPI(FakeMessage(content=[], stop_reason="max_tokens", usage=FakeUsage()))
        fake_client = type("_Client", (), {"messages": api})()

        def recording_factory(**kwargs):
            seen.append(kwargs)
            return fake_client

        monkeypatch.setattr(V, "_get_client", recording_factory)
        V.verify_finding(_finding(), max_retries=0, cycle=DEFAULT_CYCLE, cache=None)
        assert seen == [{"sdk_retries": False}]
        assert len(api.calls) == 1
