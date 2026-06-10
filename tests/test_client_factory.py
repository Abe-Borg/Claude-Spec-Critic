"""Thread-safety tests for the shared Anthropic client factory.

``reviewer._get_client`` is a module-global cache shared by the tokenizer,
batch, cross-check, triage, and verifier modules, which the GUI drives from
different worker threads. The lock added around the check-and-set guarantees
two invariants these tests pin down:

1. Concurrent first calls construct exactly ONE client (no duplicate
   construction under a race).
2. ``_cached_client`` always pairs with the ``_cached_key`` it was built
   for, even across a runtime API-key change — the unlocked version could
   interleave the two assignments and durably serve a client built with a
   stale key.
"""
from __future__ import annotations

import threading
import time

import pytest

from src.review import reviewer


class _FakeAnthropic:
    """Stand-in for the SDK client that records constructions."""

    constructed: list[str] = []
    construct_delay: float = 0.0

    def __init__(self, *, api_key: str):
        self.api_key = api_key
        if _FakeAnthropic.construct_delay:
            # Widen the race window so an unlocked factory would reliably
            # double-construct; with the lock, exactly one wins regardless.
            time.sleep(_FakeAnthropic.construct_delay)
        _FakeAnthropic.constructed.append(api_key)


@pytest.fixture
def fake_client_factory(monkeypatch):
    """Swap in the fake SDK client and reset the module-global cache."""
    monkeypatch.setattr(reviewer, "Anthropic", _FakeAnthropic)
    monkeypatch.setattr(reviewer, "_cached_client", None)
    monkeypatch.setattr(reviewer, "_cached_key", None)
    _FakeAnthropic.constructed = []
    _FakeAnthropic.construct_delay = 0.0
    yield _FakeAnthropic
    _FakeAnthropic.construct_delay = 0.0


def test_same_key_returns_cached_instance(fake_client_factory, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-a")
    first = reviewer._get_client()
    second = reviewer._get_client()
    assert first is second
    assert fake_client_factory.constructed == ["key-a"]


def test_key_change_rebuilds_and_pairs_client_with_key(fake_client_factory, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-a")
    client_a = reviewer._get_client()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-b")
    client_b = reviewer._get_client()
    assert client_b is not client_a
    assert client_b.api_key == "key-b"
    assert reviewer._cached_key == "key-b"
    # Switching back must not serve the key-b client.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-a")
    assert reviewer._get_client().api_key == "key-a"


def test_concurrent_first_calls_construct_exactly_one_client(fake_client_factory, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-a")
    fake_client_factory.construct_delay = 0.05
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(reviewer._get_client())
        except BaseException as exc:  # noqa: BLE001 — surfaced via assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(results) == n_threads
    # The lock guarantees a single construction; every caller observes it.
    assert fake_client_factory.constructed == ["key-a"]
    assert all(r is results[0] for r in results)
    assert reviewer._cached_key == "key-a"
    assert reviewer._cached_client is results[0]
