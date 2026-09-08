"""Report export after a paid run: retry prompt (B-25), worker-thread export
with Tk-thread marshaling (B-22.3), and the completion handler's terminal
state (amber on a failed export, diagnostics-before-button order).

Hermetic: the save dialog and the retry prompt are monkeypatched, the DOCX
writer runs for real into ``tmp_path`` (the same fixture result the HTML
exporter tests use), and the fake app records which thread called ``after``.

Imports ``src.gui.report_controller`` (tkinter at module scope), so this
file skips without tkinter like the other GUI tests.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

from src.gui import report_controller as rc  # noqa: E402
from src.gui import review_run_controller as rrc  # noqa: E402
from src.review.reviewer import ReviewResult  # noqa: E402
from tests.test_html_report_exporter import build_full_pipeline_result  # noqa: E402


class _Log:
    def __init__(self):
        self.entries: list[tuple[str, str, int]] = []  # (level, msg, thread)

    def _rec(self, level):
        return lambda msg, **kw: self.entries.append(
            (kw.get("level", level), msg, threading.get_ident())
        )

    def __getattr__(self, name):
        if name.startswith("log"):
            return self._rec(name.removeprefix("log_") or "info")
        raise AttributeError(name)

    def levels(self):
        return [lvl for lvl, _, _ in self.entries]


class _App:
    def __init__(self):
        self.log = _Log()
        self._queued: list = []
        self.after_threads: list[int] = []
        self._cv = threading.Condition()

    def after(self, _ms, fn):
        with self._cv:
            self.after_threads.append(threading.get_ident())
            self._queued.append(fn)
            self._cv.notify_all()

    def pump(self, timeout=10.0):
        """Run a SNAPSHOT of the marshaled callbacks on this thread.

        A retry callback starts a second worker, which enqueues its own
        completion; a live drain could swallow that too and leave the next
        ``pump`` waiting on an empty queue.
        """
        with self._cv:
            assert self._cv.wait_for(lambda: bool(self._queued), timeout=timeout)
            batch = list(self._queued)
            self._queued.clear()
        for fn in batch:
            fn()


@pytest.fixture
def save_to(monkeypatch, tmp_path):
    out = tmp_path / "report.docx"
    calls: list[dict] = []

    def _ask(**kw):
        calls.append(kw)
        return str(out)

    monkeypatch.setattr(rc.filedialog, "asksaveasfilename", _ask)
    return out, calls


@pytest.fixture
def retry_prompt(monkeypatch):
    state = {"answers": [], "calls": []}

    def _ask(title, message, **kw):
        state["calls"].append((title, message, kw))
        return state["answers"].pop(0) if state["answers"] else False

    monkeypatch.setattr(rc.messagebox, "askretrycancel", _ask)
    return state


def _failing_then_ok(monkeypatch, failures: int):
    attempts = {"n": 0, "threads": []}
    real = rc.export_report

    def _export(result, path):
        attempts["n"] += 1
        attempts["threads"].append(threading.get_ident())
        if attempts["n"] <= failures:
            raise PermissionError(f"[Errno 13] {path} is open in Word")
        real(result, path)

    monkeypatch.setattr(rc, "export_report", _export)
    return attempts


# ---------------------------------------------------------------------------
# Synchronous form (legacy contract) + the retry loop
# ---------------------------------------------------------------------------


class TestSynchronousExport:
    def test_cancel(self, monkeypatch):
        app = _App()
        monkeypatch.setattr(rc.filedialog, "asksaveasfilename", lambda **kw: "")
        assert rc.export_report_to_file(app, build_full_pipeline_result()) == "canceled"
        assert app.log.levels() == ["warning"]

    def test_save_dialog_is_owned_by_the_app(self, save_to, retry_prompt):
        app = _App()
        rc.export_report_to_file(app, build_full_pipeline_result())
        assert save_to[1][0]["parent"] is app

    def test_success_writes_report_and_sidecar(self, save_to, retry_prompt):
        app = _App()
        out, _ = save_to
        status = rc.export_report_to_file(app, build_full_pipeline_result())
        assert status == "success"
        assert out.exists()
        assert out.with_suffix(".edits.json").exists()
        assert "success" in app.log.levels()
        assert retry_prompt["calls"] == []

    def test_failure_prompts_with_the_word_hint_and_retry_succeeds(
        self, monkeypatch, save_to, retry_prompt
    ):
        app = _App()
        out, _ = save_to
        attempts = _failing_then_ok(monkeypatch, failures=1)
        retry_prompt["answers"] = [True]
        status = rc.export_report_to_file(app, build_full_pipeline_result())
        assert status == "success"
        assert attempts["n"] == 2
        assert out.exists()
        title, message, kw = retry_prompt["calls"][0]
        assert kw["parent"] is app
        assert "open in Word" in message
        assert str(out) in message
        assert "Retry" in message and "Save Word Report" in message
        assert "error" in app.log.levels()  # the failed attempt was logged

    def test_failure_then_cancel_returns_error_and_keeps_nothing_partial(
        self, monkeypatch, save_to, retry_prompt
    ):
        app = _App()
        out, _ = save_to
        attempts = _failing_then_ok(monkeypatch, failures=5)
        retry_prompt["answers"] = [True, False]
        status = rc.export_report_to_file(app, build_full_pipeline_result())
        assert status == "error"
        assert attempts["n"] == 2  # retried once, then canceled
        assert not out.exists()
        assert not out.with_suffix(".edits.json").exists()

    def test_sidecar_failure_is_a_warning_not_a_failed_export(
        self, monkeypatch, save_to, retry_prompt
    ):
        app = _App()

        def _boom(result, path):
            raise OSError("sidecar disk full")

        monkeypatch.setattr(rc, "write_edit_instructions_sidecar", _boom)
        status = rc.export_report_to_file(app, build_full_pipeline_result())
        assert status == "success"
        assert any("sidecar disk full" in msg for lvl, msg, _ in app.log.entries if lvl == "warning")
        assert retry_prompt["calls"] == []


# ---------------------------------------------------------------------------
# Asynchronous form: write on a worker, everything else on the Tk thread
# ---------------------------------------------------------------------------


class TestBackgroundExport:
    def test_writes_off_thread_and_reports_on_the_tk_thread(
        self, monkeypatch, save_to, retry_prompt
    ):
        app = _App()
        out, _ = save_to
        attempts = _failing_then_ok(monkeypatch, failures=0)
        main_ident = threading.get_ident()
        done: list[tuple[str, int]] = []

        status = rc.export_report_to_file(
            app, build_full_pipeline_result(),
            on_complete=lambda s: done.append((s, threading.get_ident())),
        )
        assert status == rc.EXPORT_STATUS_PENDING
        assert done == []  # not finished synchronously
        assert app.log.levels() == ["step"]  # "Exporting…" logged before the worker
        app.pump()
        assert attempts["threads"] == [pytest.approx(attempts["threads"][0])]
        assert attempts["threads"][0] != main_ident
        assert app.after_threads[0] != main_ident  # marshaled from the worker
        assert done == [("success", main_ident)]
        assert out.exists()
        # Every log line landed on the Tk thread, none from the worker.
        assert all(t == main_ident for _, _, t in app.log.entries)
        assert "success" in app.log.levels()

    def test_cancel_reports_immediately(self, monkeypatch):
        app = _App()
        monkeypatch.setattr(rc.filedialog, "asksaveasfilename", lambda **kw: "")
        done: list[str] = []
        assert rc.export_report_to_file(app, object(), on_complete=done.append) == "canceled"
        assert done == ["canceled"]

    def test_failure_prompts_on_the_tk_thread_and_retry_relaunches_the_worker(
        self, monkeypatch, save_to, retry_prompt
    ):
        app = _App()
        out, _ = save_to
        attempts = _failing_then_ok(monkeypatch, failures=1)
        retry_prompt["answers"] = [True]
        main_ident = threading.get_ident()
        done: list[str] = []
        rc.export_report_to_file(app, build_full_pipeline_result(), on_complete=done.append)
        app.pump()  # failure -> prompt (Retry) -> second worker
        assert len(retry_prompt["calls"]) == 1
        assert retry_prompt["calls"][0][2]["parent"] is app
        assert done == []
        app.pump()  # second attempt succeeds
        assert attempts["n"] == 2
        assert all(t != main_ident for t in attempts["threads"])
        assert done == ["success"]
        assert out.exists()

    def test_failure_then_cancel_reports_error(self, monkeypatch, save_to, retry_prompt):
        app = _App()
        _failing_then_ok(monkeypatch, failures=9)
        retry_prompt["answers"] = [False]
        done: list[str] = []
        rc.export_report_to_file(app, build_full_pipeline_result(), on_complete=done.append)
        app.pump()
        assert done == ["error"]

    def test_word_button_entry_point_mirrors_the_contract(self, monkeypatch, save_to, retry_prompt):
        app = _App()
        out, _ = save_to
        done: list[str] = []
        assert rc.export_word_report_to_file(
            app, build_full_pipeline_result(), on_complete=done.append
        ) == rc.EXPORT_STATUS_PENDING
        app.pump()
        assert done == ["success"]
        assert out.exists() and out.with_suffix(".edits.json").exists()
        monkeypatch.setattr(rc.filedialog, "asksaveasfilename", lambda **kw: "")
        assert rc.export_word_report_to_file(app, build_full_pipeline_result()) == "canceled"


# ---------------------------------------------------------------------------
# on_review_complete: async seam, ordering, amber on export failure
# ---------------------------------------------------------------------------


class _Calls:
    def __init__(self):
        self.order: list[tuple] = []


class _CompleteApp:
    def __init__(self, *, async_seam=True, sync_status="success"):
        self.calls = _Calls()
        self.pending_export: list = []
        self.sync_status = sync_status
        self.log = _Log()
        self._last_result_value = None
        self.progress_bar = type("Bar", (), {"set": lambda s, v: None})()
        outer = self

        class _Button:
            def set_complete(self):
                outer.calls.order.append(("button", "complete"))

            def set_complete_with_errors(self):
                outer.calls.order.append(("button", "complete_with_errors"))

        self.run_button = _Button()
        if async_seam:
            self._export_report_async = self._async

    @property
    def _last_result(self):
        return self._last_result_value

    @_last_result.setter
    def _last_result(self, value):
        self._last_result_value = value
        self.calls.order.append(("last_result", value is not None))

    def _async(self, result, on_complete):
        self.calls.order.append(("export_started",))
        self.pending_export.append(on_complete)

    def _export_report_to_file(self, result):
        self.calls.order.append(("export_sync",))
        return self.sync_status

    def _finalize_diagnostics(self, phase, level, message):
        self.calls.order.append(("finalize", level))

    def _reset_ui(self):
        pass

    def after(self, ms, fn):
        self.calls.order.append(("after", ms, getattr(fn, "__name__", "?")))


def _result(error=None):
    rv = ReviewResult(findings=[], error=error, model="m")

    class _R:
        review_result = rv
        cross_check_result = None
        total_elapsed_seconds = 1.0

    return _R()


class TestOnReviewCompleteAsync:
    def test_result_retained_before_export_and_terminal_state_waits_for_it(self):
        app = _CompleteApp()
        rrc.on_review_complete(app, _result())
        assert app.calls.order == [("last_result", True), ("export_started",)]
        assert app.pending_export  # the worker is "running"; nothing else yet
        app.pending_export[0]("success")
        assert app.calls.order[2:] == [
            ("finalize", "success"),
            ("button", "complete"),
            ("after", 2500, "_reset_ui"),
        ]

    def test_failed_export_ends_amber_even_on_a_clean_review(self):
        app = _CompleteApp()
        rrc.on_review_complete(app, _result())
        app.pending_export[0]("error")
        assert ("finalize", "warning") in app.calls.order
        assert ("button", "complete_with_errors") in app.calls.order
        assert ("button", "complete") not in app.calls.order
        assert any("Save Word" in msg for _, msg, _ in app.log.entries)

    def test_canceled_export_keeps_green_on_a_clean_review(self):
        app = _CompleteApp()
        rrc.on_review_complete(app, _result())
        app.pending_export[0]("canceled")
        assert ("finalize", "info") in app.calls.order
        assert ("button", "complete") in app.calls.order

    def test_review_errors_stay_amber_after_a_successful_export(self):
        app = _CompleteApp()
        rrc.on_review_complete(app, _result(error="1 spec(s) had errors"))
        app.pending_export[0]("success")
        assert ("finalize", "warning") in app.calls.order
        assert ("button", "complete_with_errors") in app.calls.order

    def test_sync_fallback_without_the_seam_still_ends_amber_on_export_error(self):
        app = _CompleteApp(async_seam=False, sync_status="error")
        rrc.on_review_complete(app, _result())
        assert ("export_sync",) in app.calls.order
        assert ("button", "complete_with_errors") in app.calls.order
        assert app.calls.order.index(("finalize", "warning")) < app.calls.order.index(
            ("button", "complete_with_errors")
        )

    def test_gui_shell_exposes_the_async_seam(self):
        pytest.importorskip("customtkinter")
        from src.gui.gui import SpecReviewApp

        assert callable(getattr(SpecReviewApp, "_export_report_async", None))
        src = Path(SpecReviewApp.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8")
        assert "export_report_to_file(self, result, on_complete=_done)" in src


# ---------------------------------------------------------------------------
# Default filename stem (C-2): unique per run so the unconditionally written
# sidecars of a second same-day run never silently overwrite the first's.
# ---------------------------------------------------------------------------


class TestDefaultReportStem:
    _CLOCK = __import__("datetime").datetime(2026, 9, 8, 14, 7, 59)

    def test_single_module_result_stem_is_date_plus_minute(self):
        result = build_full_pipeline_result()
        assert rc.default_report_stem(result, now=self._CLOCK) == (
            "spec-critic-report-2026-09-08-14-07"
        )

    def test_program_result_appends_the_program_id(self):
        from tests.test_html_report_exporter import build_program_result

        result = build_program_result()
        assert rc.default_report_stem(result, now=self._CLOCK) == (
            "spec-critic-report-2026-09-08-14-07-hyperscale_datacenter"
        )

    def test_program_id_is_sanitized_for_filenames(self):
        from types import SimpleNamespace

        stem = rc.default_report_stem(
            SimpleNamespace(program_id="odd/id with:chars"), now=self._CLOCK
        )
        assert stem == "spec-critic-report-2026-09-08-14-07-odd-id-with-chars"

    def test_no_result_falls_back_to_the_stamp_only(self):
        assert rc.default_report_stem(None, now=self._CLOCK) == (
            "spec-critic-report-2026-09-08-14-07"
        )

    def test_two_runs_a_minute_apart_do_not_collide(self):
        import datetime as _dt

        a = rc.default_report_stem(None, now=self._CLOCK)
        b = rc.default_report_stem(None, now=self._CLOCK + _dt.timedelta(minutes=1))
        assert a != b

    def test_save_dialogs_seed_initialfile_from_the_stem(self, monkeypatch):
        seen: list[str] = []

        def _ask(**kw):
            seen.append(kw["initialfile"])
            return ""

        monkeypatch.setattr(rc.filedialog, "asksaveasfilename", _ask)
        monkeypatch.setattr(
            rc, "default_report_stem", lambda result=None, *, now=None: "STEM"
        )
        app = _App()
        result = build_full_pipeline_result()
        assert rc.export_report_to_file(app, result) == "canceled"
        assert rc.export_html_report_to_file(app, result) == "canceled"
        assert seen == ["STEM.docx", "STEM.html"]
