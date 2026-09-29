"""Injected time for the retry contract (plan WP-11).

Every app-owned retry loop waits through
``src.verification.retry_policy.RetrySchedule``, which reads its clock, wait,
and random source from ``retry_policy.DEFAULT_RETRY_TIMING`` when the
schedule is created. :func:`install_fake_retry_timing` swaps that default for
a :class:`FakeRetryTiming` that never sleeps, so a test can see every wait a
loop asked for, move the wall clock, script the jitter draws, and cancel a
wait from inside it — without waiting in real time.

The fake is thread-safe: concurrent loops (a review pool, a research fan-out)
share one instance, and ``wait_log`` records which thread waited and whether
it held a permit (``held_probe``) at that moment.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from src.verification import retry_policy

# A fixed wall clock (2023-11-14T22:13:20Z) so HTTP-date tests are exact.
FAKE_EPOCH = 1_700_000_000.0


@dataclass(frozen=True)
class WaitRecord:
    seconds: float
    thread: int
    held_permit: bool
    cancelled: bool


class FakeRetryTiming:
    """Records waits instead of sleeping; the wall clock advances by each wait."""

    def __init__(
        self,
        *,
        now: float = FAKE_EPOCH,
        randoms: Optional[Iterable[float]] = None,
        seed: int = 0,
        on_wait: Optional[Callable[[float, Any], None]] = None,
        held_probe: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.clock = float(now)
        self._randoms = list(randoms) if randoms is not None else None
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self.on_wait = on_wait
        self.held_probe = held_probe
        self.wait_log: list[WaitRecord] = []
        self.random_draws: list[float] = []

    # -- the three seams ----------------------------------------------------

    def now(self) -> float:
        with self._lock:
            return self.clock

    def random(self) -> float:
        with self._lock:
            if self._randoms is not None:
                value = self._randoms.pop(0) if self._randoms else 0.0
            else:
                value = self._rng.random()
            self.random_draws.append(value)
            return value

    def wait(self, seconds: float, cancel_event: Any = None) -> bool:
        held = bool(self.held_probe()) if self.held_probe is not None else False
        if self.on_wait is not None:
            self.on_wait(seconds, cancel_event)
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        with self._lock:
            self.clock += float(seconds)
            self.wait_log.append(
                WaitRecord(float(seconds), threading.get_ident(), held, cancelled)
            )
        return not cancelled

    # -- views --------------------------------------------------------------

    @property
    def waits(self) -> list[float]:
        return [record.seconds for record in self.wait_log]

    def timing(self) -> retry_policy.RetryTiming:
        return retry_policy.RetryTiming(wait=self.wait, now=self.now, random=self.random)


def install_fake_retry_timing(monkeypatch, **kwargs: Any) -> FakeRetryTiming:
    """Make every retry schedule created from now on use a fresh fake."""
    fake = FakeRetryTiming(**kwargs)
    monkeypatch.setattr(retry_policy, "DEFAULT_RETRY_TIMING", fake.timing())
    return fake
