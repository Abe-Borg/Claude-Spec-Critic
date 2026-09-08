"""Token-analysis controller: basis cache (B-22.4), timer marshaling (B-26d),
and the post-run Run-button gate (B-24).

Hermetic: ``count_tokens`` is stubbed (the cl100k rank file is not available
offline), no Tk root is created, and the fake app records which thread
called ``after`` / ``after_cancel`` so the Tk-thread-only contract on the
debounce timer is provable.
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.tokenizer import RECOMMENDED_MAX
from src.gui import token_analysis_controller as tac
from src.modules import DEFAULT_MODULE
from src.programs import AVAILABLE_PROGRAMS


@pytest.fixture(autouse=True)
def _stub_count_tokens(monkeypatch):
    monkeypatch.setattr(tac, "count_tokens", lambda text: len(text))
    tac.clear_program_basis_cache()
    yield
    tac.clear_program_basis_cache()


# ---------------------------------------------------------------------------
# B-22.4 — program basis computed once per program per process
# ---------------------------------------------------------------------------


class TestProgramBasisCache:
    def test_basis_is_tokenized_once_per_program(self, monkeypatch):
        prompts: list[str] = []
        real = tac.get_system_prompt

        def _spy(cycle):
            text = real(cycle)
            prompts.append(text)
            return text

        monkeypatch.setattr(tac, "get_system_prompt", _spy)
        multi = next(
            p for p in AVAILABLE_PROGRAMS.values() if len(p.implemented_module_ids) > 1
        )
        app = SimpleNamespace(_selected_program_id=multi.program_id)
        first = tac._token_cycle_for_app(app)
        n_modules = len(multi.implemented_module_ids)
        assert len(prompts) == n_modules
        for _ in range(5):  # five checkbox clicks
            assert tac._token_cycle_for_app(app) is first
        assert len(prompts) == n_modules  # nothing re-tokenized

    def test_cache_is_keyed_by_module_ids(self, monkeypatch):
        calls: list[tuple[str, ...]] = []
        real = tac._program_basis_cycle

        def _spy(module_ids):
            calls.append(module_ids)
            return real(module_ids)

        monkeypatch.setattr(tac, "_program_basis_cycle", _spy)
        for program in AVAILABLE_PROGRAMS.values():
            tac._token_cycle_for_app(SimpleNamespace(_selected_program_id=program.program_id))
        assert set(calls) == {
            tuple(p.implemented_module_ids) for p in AVAILABLE_PROGRAMS.values()
        }
        assert set(tac._PROGRAM_BASIS_CACHE) == set(calls)

    def test_basis_is_the_largest_prompt_module(self, monkeypatch):
        multi = next(
            p for p in AVAILABLE_PROGRAMS.values() if len(p.implemented_module_ids) > 1
        )
        from src.modules import require_module

        expected = max(
            (require_module(m) for m in multi.implemented_module_ids),
            key=lambda module: len(tac.get_system_prompt(module.cycle)),
        ).cycle
        assert tac._token_cycle_for_app(SimpleNamespace(_selected_program_id=multi.program_id)) is expected

    def test_legacy_scalar_module_path_bypasses_cache(self):
        app = SimpleNamespace(_selected_module_id=DEFAULT_MODULE.module_id)
        assert tac._token_cycle_for_app(app) is DEFAULT_MODULE.cycle
        assert tac._PROGRAM_BASIS_CACHE == {}

    def test_cache_is_thread_safe_and_single_valued(self):
        multi = next(
            p for p in AVAILABLE_PROGRAMS.values() if len(p.implemented_module_ids) > 1
        )
        results: list = []
        barrier = threading.Barrier(4)

        def _worker():
            barrier.wait()
            results.append(tac._program_basis_cycle(tuple(multi.implemented_module_ids)))

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({id(r) for r in results}) == 1
        assert len(tac._PROGRAM_BASIS_CACHE) == 1


# ---------------------------------------------------------------------------
# B-26d — only the Tk thread touches the debounce timer
# ---------------------------------------------------------------------------


class _ThreadRecordingApp:
    """Records the calling thread of every ``after`` / ``after_cancel``."""

    def __init__(self):
        self._exact_token_refresh_timer_id = None
        self.after_calls: list[tuple[int, int]] = []  # (delay_ms, thread_ident)
        self.after_cancel_calls: list[tuple[object, int]] = []
        self.timer_id_writes: list[int] = []
        self._queued: list = []
        self._cv = threading.Condition()
        self._next = 0

    def after(self, delay_ms, fn):
        with self._cv:
            self.after_calls.append((delay_ms, threading.get_ident()))
            self._next += 1
            ident = f"after#{self._next}"
            if delay_ms == 0:
                self._queued.append(fn)
                self._cv.notify_all()
            return ident

    def after_cancel(self, timer_id):
        self.after_cancel_calls.append((timer_id, threading.get_ident()))

    def wait_for_queued(self, timeout=5.0):
        with self._cv:
            assert self._cv.wait_for(lambda: bool(self._queued), timeout=timeout)

    def drain(self):
        """Run the marshaled callbacks — this is the 'Tk thread'."""
        while self._queued:
            self._queued.pop(0)()


def _spec(name="a.docx"):
    return SimpleNamespace(
        filename=name, content="body", paragraph_map=[], source_path=f"/specs/{name}"
    )


class TestDebounceTimerMarshaling:
    def test_worker_thread_never_touches_the_timer(self):
        app = _ThreadRecordingApp()
        app._exact_token_refresh_timer_id = "stale-timer"
        main_ident = threading.get_ident()
        file_data = [{"path": Path("/specs/a.docx"), "filename": "a.docx", "tokens": 10}]
        specs = [_spec()]

        def _from_worker():
            tac.refresh_exact_token_count(
                app, file_data, specs, "", DEFAULT_MODULE.cycle, 0, 0,
                dispatch=lambda fn: app.after(0, fn),
            )

        worker = threading.Thread(target=_from_worker)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        # Before the Tk thread drains: the stale id is untouched and nothing
        # but the 0-ms marshal hop was scheduled from the worker.
        assert app._exact_token_refresh_timer_id == "stale-timer"
        assert app.after_cancel_calls == []
        worker_after = [(d, t) for d, t in app.after_calls if t != main_ident]
        assert worker_after and all(d == 0 for d, _ in worker_after)

        app.wait_for_queued()
        app.drain()  # the Tk thread runs the marshaled scheduling step
        assert app.after_cancel_calls == [("stale-timer", main_ident)]
        timer_after = [(d, t) for d, t in app.after_calls if d == tac.EXACT_TOKEN_REFRESH_DEBOUNCE_MS]
        assert timer_after == [(tac.EXACT_TOKEN_REFRESH_DEBOUNCE_MS, main_ident)]
        assert app._exact_token_refresh_timer_id == f"after#{len(app.after_calls)}"

    def test_tk_thread_call_still_debounces_through_dispatch(self):
        app = _ThreadRecordingApp()
        file_data = [{"path": Path("/specs/a.docx"), "filename": "a.docx", "tokens": 10}]
        specs = [_spec()]
        for _ in range(3):
            tac.refresh_exact_token_count(
                app, file_data, specs, "", DEFAULT_MODULE.cycle, 0, 0,
                dispatch=lambda fn: app.after(0, fn),
            )
            app.drain()
        # Three calls -> three timers, the two earlier ones cancelled.
        timers = [d for d, _ in app.after_calls if d == tac.EXACT_TOKEN_REFRESH_DEBOUNCE_MS]
        assert len(timers) == 3
        assert len(app.after_cancel_calls) == 2

    def test_stale_analysis_dispatch_is_dropped_before_scheduling(self):
        # ``analyze_tokens`` passes an epoch-guarded dispatch; a superseded
        # analysis must not schedule an exact-count call at all.
        app = _ThreadRecordingApp()
        file_data = [{"path": Path("/specs/a.docx"), "filename": "a.docx", "tokens": 10}]
        tac.refresh_exact_token_count(
            app, file_data, [_spec()], "", DEFAULT_MODULE.cycle, 0, 0,
            dispatch=lambda fn: None,  # stale: the guard swallows it
        )
        assert app.after_calls == []
        assert app._exact_token_refresh_timer_id is None


# ---------------------------------------------------------------------------
# B-24 — the Run-button gate survives set_ready()
# ---------------------------------------------------------------------------


class _Button:
    def __init__(self):
        self.state = "normal"
        self.text = ""
        self.ready_calls = 0

    def configure(self, state=None, text=None, **_):
        if state is not None:
            self.state = state
        if text is not None:
            self.text = text

    def set_ready(self):
        self.ready_calls += 1
        self.state = "normal"  # the unconditional re-enable under test


class _Panel:
    def __init__(self, selected):
        self._selected = selected
        self.over_limit: list[bool] = []

    def get_selected_files(self):
        return list(self._selected)

    def set_over_limit(self, flag):
        self.over_limit.append(flag)


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _gate_app(*, over_limit: bool):
    big = RECOMMENDED_MAX + 1 if over_limit else 10
    path = Path("/specs/big.docx")
    app = SimpleNamespace(
        run_button=_Button(),
        progress_bar=SimpleNamespace(pack_forget=lambda: None, set=lambda v: None),
        log=_Log(),
        file_list_panel=_Panel([path]),
        _loaded_file_data=[{"path": path, "filename": "big.docx", "tokens": big}],
        _system_prompt_tokens=0,
        _project_context_tokens=0,
        is_processing=True,
        _batch_submission=object(),
        _trace_recorder=None,
    )
    app._finalize_diagnostics = lambda *a, **k: None
    return app


class TestRunButtonGate:
    def test_apply_gate_disables_over_limit_selection(self):
        app = _gate_app(over_limit=True)
        tac.apply_run_button_gate(app)
        assert app.run_button.state == "disabled"
        assert app.file_list_panel.over_limit == [True]

    def test_apply_gate_keeps_valid_selection_enabled(self):
        app = _gate_app(over_limit=False)
        tac.apply_run_button_gate(app)
        assert app.run_button.state == "normal"
        assert app.file_list_panel.over_limit == [False]

    def test_apply_gate_is_a_noop_before_files_load(self):
        app = SimpleNamespace(run_button=_Button(), _loaded_file_data=[])
        tac.apply_run_button_gate(app)
        assert app.run_button.state == "normal"

    def test_apply_gate_does_not_trigger_an_exact_count_refresh(self, monkeypatch):
        app = _gate_app(over_limit=False)
        monkeypatch.setattr(
            tac, "refresh_exact_token_count",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API refresh")),
        )
        tac.apply_run_button_gate(app)

    def test_reset_ui_reapplies_the_gate_after_set_ready(self):
        pytest.importorskip("tkinter")
        from src.gui.review_run_controller import reset_ui

        app = _gate_app(over_limit=True)
        reset_ui(app)
        assert app.run_button.ready_calls == 1
        assert app.run_button.state == "disabled"  # set_ready's re-enable undone
        assert app.is_processing is False
        assert app._batch_submission is None

    def test_on_review_error_reapplies_the_gate_after_set_ready(self):
        pytest.importorskip("tkinter")
        from src.gui.review_run_controller import on_review_error

        app = _gate_app(over_limit=True)
        on_review_error(app, "boom")
        assert app.run_button.ready_calls == 1
        assert app.run_button.state == "disabled"
        assert app.is_processing is False

    def test_reset_ui_leaves_a_valid_selection_runnable(self):
        pytest.importorskip("tkinter")
        from src.gui.review_run_controller import reset_ui

        app = _gate_app(over_limit=False)
        reset_ui(app)
        assert app.run_button.state == "normal"
