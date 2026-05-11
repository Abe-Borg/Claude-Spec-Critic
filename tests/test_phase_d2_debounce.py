"""Phase D2.2 — debounce GUI-triggered exact-token-count refresh.

The exact ``count_tokens`` API call is gated through ``app.after`` so
rapid file-list churn (multiple drops, fast browse, future selection
toggles) collapses into a single outbound API call after the burst
settles. Each invocation cancels the prior timer and reschedules.

Stale-result protection (``_analysis_epoch`` inside ``dispatch``) is
left in place; this chunk only prevents unnecessary API calls from
*starting*.
"""
from __future__ import annotations

import pytest

from src.code_cycles import DEFAULT_CYCLE
from src.extractor import ExtractedSpec
from src.token_analysis_controller import (
    EXACT_TOKEN_REFRESH_DEBOUNCE_MS,
    refresh_exact_token_count,
)


class _FakeApp:
    """Minimal Tk-like stand-in.

    Implements ``after`` / ``after_cancel`` so the debounce logic
    schedules a deferred callback (rather than launching a thread
    immediately). ``fire_pending()`` is the test hook that simulates
    the timer firing.
    """

    def __init__(self) -> None:
        self._pending: dict[str, tuple[int, object]] = {}
        self._next_id = 1
        self._cancelled: list[str] = []
        self._exact_token_refresh_timer_id: str | None = None
        # The token gauge / log surfaces are touched by the closure
        # ``_exact()`` body if an API call returns a value. They are not
        # exercised in the debounce-shape tests because we stub the API
        # to return ``None`` (silent fallback path).
        self.token_gauge = _NoopWidget()
        self.log = _NoopLog()

    def after(self, ms: int, callback=None) -> str:
        if callback is None:
            # Tk's ``after(ms)`` form (sleep without callback) is not
            # used by this controller.
            raise NotImplementedError
        tid = f"after#{self._next_id}"
        self._next_id += 1
        self._pending[tid] = (ms, callback)
        return tid

    def after_cancel(self, after_id: str) -> None:
        self._cancelled.append(after_id)
        self._pending.pop(after_id, None)

    def fire_pending(self) -> int:
        """Fire every currently-scheduled callback.

        Returns the number of callbacks fired. Each callback runs once.
        """
        items = list(self._pending.items())
        self._pending.clear()
        for _tid, (_ms, cb) in items:
            cb()
        return len(items)

    def pending_count(self) -> int:
        return len(self._pending)


class _NoopWidget:
    def update_gauge(self, *args, **kwargs) -> None:
        return None

    def reset(self) -> None:
        return None


class _NoopLog:
    def log(self, *args, **kwargs) -> None:
        return None

    def log_warning(self, *args, **kwargs) -> None:
        return None

    def log_success(self, *args, **kwargs) -> None:
        return None

    def log_error(self, *args, **kwargs) -> None:
        return None

    def log_step(self, *args, **kwargs) -> None:
        return None


@pytest.fixture
def fake_app() -> _FakeApp:
    return _FakeApp()


@pytest.fixture
def file_data() -> list[dict]:
    return [
        {"path": "/x/a.docx", "filename": "a.docx", "tokens": 1000, "content": "alpha"},
        {"path": "/x/b.docx", "filename": "b.docx", "tokens": 5000, "content": "beta body"},
    ]


@pytest.fixture
def extracted_specs() -> list[ExtractedSpec]:
    return [
        ExtractedSpec(filename="a.docx", content="alpha", word_count=1),
        ExtractedSpec(filename="b.docx", content="beta body", word_count=2),
    ]


@pytest.fixture
def thread_tracker(monkeypatch):
    """Replace ``threading.Thread`` so target callables are recorded but
    never actually executed in a background thread.

    Returns the list of (target, started) tuples so a test can assert
    how many threads were launched.
    """
    launched: list[tuple[object, bool]] = []

    class _FakeThread:
        def __init__(self, *, target=None, daemon=False, **_kwargs):
            self._target = target
            self._daemon = daemon
            launched.append((target, False))

        def start(self) -> None:
            # Mark the last appended record as started; do not actually
            # invoke ``self._target``. The debounce tests only care that
            # ``start()`` was called once.
            launched[-1] = (self._target, True)

    monkeypatch.setattr(
        "src.token_analysis_controller.threading.Thread", _FakeThread
    )
    return launched


class TestExactTokenRefreshDebounce:
    def test_constant_within_recommended_window(self):
        # The delta plan recommends 300–500 ms. Lock the chosen value
        # inside that range so a future tweak doesn't drift outside it
        # without an explicit decision.
        assert 300 <= EXACT_TOKEN_REFRESH_DEBOUNCE_MS <= 500

    def test_single_call_schedules_one_timer(
        self, fake_app, file_data, extracted_specs, thread_tracker
    ):
        refresh_exact_token_count(
            fake_app, file_data, extracted_specs,
            project_context="", cycle=DEFAULT_CYCLE,
            sys_tokens=100, ctx_tokens=0,
            dispatch=lambda fn: fn(),
        )

        # Exactly one pending timer; no threads launched yet.
        assert fake_app.pending_count() == 1
        assert thread_tracker == []
        assert fake_app._exact_token_refresh_timer_id is not None

    def test_rapid_calls_collapse_to_one_outbound(
        self, fake_app, file_data, extracted_specs, thread_tracker
    ):
        # Simulate 5 rapid file-list changes. Each should cancel the
        # prior timer and reschedule.
        for _ in range(5):
            refresh_exact_token_count(
                fake_app, file_data, extracted_specs,
                project_context="", cycle=DEFAULT_CYCLE,
                sys_tokens=100, ctx_tokens=0,
                dispatch=lambda fn: fn(),
            )

        # Only one timer pending after the burst.
        assert fake_app.pending_count() == 1
        # No thread has launched yet — the debounce window has not
        # fired.
        assert thread_tracker == []
        # The previous 4 timer ids were cancelled.
        assert len(fake_app._cancelled) == 4

        # Fire the pending timer.
        fired = fake_app.fire_pending()
        assert fired == 1

        # Exactly one thread was launched after the debounce fired.
        assert len(thread_tracker) == 1
        target, started = thread_tracker[0]
        assert started is True
        assert callable(target)
        # The post-fire timer id should be cleared so the next refresh
        # call starts fresh.
        assert fake_app._exact_token_refresh_timer_id is None

    def test_timer_uses_debounce_window(
        self, fake_app, file_data, extracted_specs, thread_tracker
    ):
        refresh_exact_token_count(
            fake_app, file_data, extracted_specs,
            project_context="", cycle=DEFAULT_CYCLE,
            sys_tokens=100, ctx_tokens=0,
            dispatch=lambda fn: fn(),
        )

        ((ms, _cb),) = list(fake_app._pending.values())
        assert ms == EXACT_TOKEN_REFRESH_DEBOUNCE_MS

    def test_cancel_failure_is_swallowed(
        self, fake_app, file_data, extracted_specs, thread_tracker, monkeypatch
    ):
        # Simulate an "invalid command name" from Tk (e.g. timer already
        # fired). The debounce path should silently overwrite the id
        # and reschedule, not propagate the exception.
        def _boom(_tid):
            raise RuntimeError("invalid command name")

        # First call seeds a timer id.
        refresh_exact_token_count(
            fake_app, file_data, extracted_specs,
            project_context="", cycle=DEFAULT_CYCLE,
            sys_tokens=100, ctx_tokens=0,
            dispatch=lambda fn: fn(),
        )
        # Make the next ``after_cancel`` raise; the refresh path should
        # absorb the exception and proceed.
        monkeypatch.setattr(fake_app, "after_cancel", _boom)
        refresh_exact_token_count(
            fake_app, file_data, extracted_specs,
            project_context="", cycle=DEFAULT_CYCLE,
            sys_tokens=100, ctx_tokens=0,
            dispatch=lambda fn: fn(),
        )
        # A new pending timer exists; no thread launched yet.
        assert fake_app.pending_count() >= 1
        assert thread_tracker == []

    def test_returns_early_when_biggest_spec_missing(
        self, fake_app, file_data, thread_tracker
    ):
        # ``file_data`` references filenames not present in
        # ``extracted_specs``; the existing early return must still fire
        # and the debounce must NOT schedule a timer.
        refresh_exact_token_count(
            fake_app, file_data, extracted_specs=[],
            project_context="", cycle=DEFAULT_CYCLE,
            sys_tokens=100, ctx_tokens=0,
            dispatch=lambda fn: fn(),
        )
        assert fake_app.pending_count() == 0
        assert thread_tracker == []
        assert fake_app._exact_token_refresh_timer_id is None

    def test_debounce_prevents_api_calls(
        self, fake_app, file_data, extracted_specs, monkeypatch
    ):
        """End-to-end variant: count_tokens_via_api should be invoked
        at most once after a burst of refresh calls.

        Unlike the other tests, this one lets the launched thread's
        target actually run (synchronously) so we can observe the API
        call count.
        """
        api_calls: list[dict] = []

        def _fake_count(**kwargs):
            api_calls.append(kwargs)
            return 1234

        monkeypatch.setattr(
            "src.tokenizer.count_tokens_via_api", _fake_count
        )

        class _InlineThread:
            def __init__(self, *, target=None, daemon=False, **_kwargs):
                self._target = target

            def start(self) -> None:
                if self._target is not None:
                    self._target()

        monkeypatch.setattr(
            "src.token_analysis_controller.threading.Thread", _InlineThread
        )

        # Burst of 10 rapid refresh calls.
        for _ in range(10):
            refresh_exact_token_count(
                fake_app, file_data, extracted_specs,
                project_context="", cycle=DEFAULT_CYCLE,
                sys_tokens=100, ctx_tokens=0,
                dispatch=lambda fn: fn(),
            )

        # Before the timer fires: zero API calls (debounce holds).
        assert api_calls == []

        # Fire the single pending timer.
        fake_app.fire_pending()

        # Exactly one API call after the burst.
        assert len(api_calls) == 1
