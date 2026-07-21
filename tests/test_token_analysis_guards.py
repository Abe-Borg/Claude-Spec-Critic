"""Token-analysis input guards (WS7: C6, C7).

* C6 — the token-analysis chain is gated on ``is_processing``: mid-run UI
  pokes (checkbox toggles, context changes, focus-out, Browse/drag-drop)
  must neither fire live ``count_tokens`` API calls nor re-enable the
  deliberately-disabled run button.
* C7 — ``_token_refresh_epoch`` staleness guard: an exact count captured
  under an older selection state is dropped instead of overwriting a
  fresher gauge value with ``is_exact=True``.

No tkinter needed — the controller imports none; the fake app is
duck-typed with an immediate ``after``.
"""
from __future__ import annotations

import threading

from src.gui import token_analysis_controller as tac
from src.gui.token_analysis_controller import (
    analyze_tokens,
    on_file_selection_change,
    refresh_exact_token_count,
)


class _Recorder:
    def __init__(self):
        self.gauge_calls: list[tuple] = []
        self.run_button_states: list[str] = []
        self.warnings: list[str] = []


class _FakeApp:
    """Duck-typed app: immediate ``after``, recording widgets."""

    def __init__(self, *, is_processing: bool = False):
        self.is_processing = is_processing
        self.rec = _Recorder()
        self._loaded_file_data = [
            {"path": "a.docx", "filename": "a.docx", "tokens": 100}
        ]
        self._analysis_epoch = 0
        self._extracted_specs = []
        self._system_prompt_tokens = 10
        self._project_context_tokens = 0
        outer = self

        class _Gauge:
            def update_gauge(self, *a, **k):
                outer.rec.gauge_calls.append((a, k))

            def reset(self):
                pass

        class _Panel:
            def get_selected_files(self):
                return ["a.docx"]

            def set_over_limit(self, _v):
                pass

            def reset(self):
                pass

        class _Button:
            def configure(self, **kwargs):
                if "state" in kwargs:
                    outer.rec.run_button_states.append(kwargs["state"])

        class _Log:
            def log(self, *a, **k):
                pass

            def log_warning(self, msg, *a, **k):
                outer.rec.warnings.append(str(msg))

            def log_step(self, *a, **k):
                pass

        self.token_gauge = _Gauge()
        self.file_list_panel = _Panel()
        self.run_button = _Button()
        self.log = _Log()

    def after(self, _ms, fn=None):
        if fn is not None:
            fn()
        return "timer-1"

    def after_cancel(self, _timer_id):
        pass

    def _get_project_context(self):
        return ""


class TestIsProcessingGate:
    def test_selection_change_is_noop_while_processing(self):
        app = _FakeApp(is_processing=True)
        on_file_selection_change(app)
        # No gauge update and — the latent bug — no run-button re-enable.
        assert app.rec.gauge_calls == []
        assert app.rec.run_button_states == []

    def test_selection_change_no_run_button_reenable_regression(self):
        # Sanity inverse: when NOT processing, the handler does drive the
        # run-button state (proving the gate is what suppressed it above).
        app = _FakeApp(is_processing=False)
        on_file_selection_change(app)
        assert app.rec.run_button_states == ["normal"]

    def test_analyze_tokens_refused_while_processing(self, monkeypatch):
        app = _FakeApp(is_processing=True)
        started: list = []

        class _NoThread:
            def __init__(self, *a, **k):
                started.append((a, k))

            def start(self):
                raise AssertionError("analysis thread must not start mid-run")

        monkeypatch.setattr(threading, "Thread", _NoThread)
        analyze_tokens(app, ["a.docx"])
        assert started == []
        assert any("Review in progress" in w for w in app.rec.warnings)


class TestRefreshEpochGuard:
    def _drive_refresh(self, app, monkeypatch, *, exact_value: int, bump_before_dispatch: bool):
        """Run refresh_exact_token_count synchronously with a scripted count."""
        # Immediate debounce + synchronous thread execution.
        monkeypatch.setattr(
            tac.threading,
            "Thread",
            lambda target=None, daemon=None: type(
                "_T", (), {"start": lambda self: target()}
            )(),
        )
        spec = type(
            "_Spec",
            (),
            {
                "content": "body",
                "filename": "a.docx",
                "paragraph_map": [],
                "source_path": "a.docx",
            },
        )()
        monkeypatch.setattr(tac, "select_biggest_spec", lambda fd, es: spec)
        import src.core.tokenizer as tokenizer
        import src.review.prompts as prompts
        import src.review.structured_schemas as schemas

        monkeypatch.setattr(prompts, "get_system_prompt", lambda cycle: "sys")
        monkeypatch.setattr(
            prompts,
            "get_single_spec_user_message",
            lambda *a, **k: "user",
        )
        monkeypatch.setattr(schemas, "review_findings_tool", lambda **k: {})

        def _count(**kwargs):
            if bump_before_dispatch:
                app._token_refresh_epoch = (
                    getattr(app, "_token_refresh_epoch", 0) + 1
                )
            return exact_value

        monkeypatch.setattr(tokenizer, "count_tokens_via_api", _count)
        refresh_exact_token_count(
            app,
            app._loaded_file_data,
            [spec],
            "",
            None,
            10,
            0,
            lambda fn: app.after(0, fn),
        )

    def test_fresh_exact_count_lands(self, monkeypatch):
        app = _FakeApp()
        self._drive_refresh(app, monkeypatch, exact_value=1234, bump_before_dispatch=False)
        assert app.rec.gauge_calls, "expected a gauge update"
        args, kwargs = app.rec.gauge_calls[-1]
        assert args[0] == 1234
        assert kwargs.get("is_exact") is True

    def test_stale_exact_count_dropped_after_epoch_bump(self, monkeypatch):
        # The epoch bumps between the API call starting and its dispatch
        # executing (a newer selection state took over) — the stale exact
        # count must NOT overwrite the fresher gauge value.
        app = _FakeApp()
        self._drive_refresh(app, monkeypatch, exact_value=1234, bump_before_dispatch=True)
        assert app.rec.gauge_calls == []

    def test_selection_change_bumps_epoch(self, monkeypatch):
        app = _FakeApp()
        app._extracted_specs = [object()]
        captured: dict = {}

        def _fake_refresh(*args, **kwargs):
            captured["epoch"] = getattr(app, "_token_refresh_epoch", 0)

        monkeypatch.setattr(tac, "refresh_exact_token_count", _fake_refresh)
        before = getattr(app, "_token_refresh_epoch", 0)
        on_file_selection_change(app)
        assert captured["epoch"] == before + 1

    def test_clear_selection_bumps_epoch(self):
        from src.gui.file_selection_controller import clear_selection

        class _ClearApp(_FakeApp):
            def __init__(self):
                super().__init__()
                self._selected_files = []
                self._exact_token_refresh_timer_id = None

                class _Entry:
                    def delete(self, *a, **k):
                        pass

                self.input_path_entry = _Entry()

            def _clear_file_state(self):
                pass

            def _update_context_token_label(self):
                pass

        app = _ClearApp()
        app._token_refresh_epoch = 5
        try:
            clear_selection(app)
        except AttributeError:
            # The full clear touches more widgets than this fake models;
            # the epoch bump happens first, which is what we pin.
            pass
        assert app._token_refresh_epoch == 6
