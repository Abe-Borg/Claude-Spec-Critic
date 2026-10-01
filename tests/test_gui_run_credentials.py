"""GUI runs: the typed key stays in memory, and tracing is optional
(plan WP-13, chunk S16).

Each flow runs the real controller against a fake app. Worker threads are
captured (or run synchronously) and then executed, so the tests see what
the worker itself sees:

* every run worker — submit, poll, collect, reconnect, the drawing digest,
  and the token gauge's count — runs under the credential captured when
  the run (or digest, or count) started, and ``os.environ`` never holds it;
  typing a different key mid-run does not change the running run's key;
* a trace recorder that cannot start costs one warning, not the run; a
  failure before the review starts still restores the widgets;
* a run's teardown stops its own recorder and never a newer run's.

Imports the tkinter-bound controllers, so it is listed in
``conftest._GUI_DEPENDENT_TESTS`` and skips without ``python3-tk``.
"""
from __future__ import annotations

import contextvars
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("tkinter")

from src.batch.batch_runtime import PollOutcome  # noqa: E402
from src.core.credentials import ApiCredential, active_credential  # noqa: E402
from src.gui import batch_controller as bc  # noqa: E402
from src.gui import review_run_controller as rrc  # noqa: E402
from src.gui import token_analysis_controller as tac  # noqa: E402
from src.orchestration.diagnostics import DiagnosticsReport  # noqa: E402
from src.tracing import session  # noqa: E402
from src.tracing.recorder import get_recorder, set_recorder  # noqa: E402
from tests.fixtures import spec_docx as fx  # noqa: E402

FAKE_KEY = "sk-ant-api03-FAKE-gui-run-key-2222"
OTHER_KEY = "sk-ant-api03-FAKE-typed-mid-run-3333"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    saved = dict(os.environ)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SPEC_CRITIC_TRACE_DEEP", raising=False)
    monkeypatch.setenv("SPEC_CRITIC_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("SPEC_CRITIC_UI_STATE_PATH", str(tmp_path / "ui_state.json"))
    monkeypatch.setenv("SPEC_CRITIC_PENDING_BATCH_PATH", str(tmp_path / "pending.json"))
    set_recorder(None)
    yield
    rec = get_recorder()
    if rec is not None and hasattr(rec, "stop"):
        try:
            rec.stop()
        except Exception:
            pass
    set_recorder(None)
    os.environ.clear()
    os.environ.update(saved)


def _key_in_environment(*keys: str) -> bool:
    return any(key in value for key in keys for value in os.environ.values())


class _Captured:
    """Stands in for ``threading.Thread``: records the worker, runs it on
    demand."""

    started: list["_Captured"] = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        _Captured.started.append(self)

    def run_now(self):
        """Run the worker as a new thread would: in an empty context."""
        return contextvars.Context().run(self.target, *self.args, **self.kwargs)


class _Sync(_Captured):
    """Runs the worker at ``start()``, in a fresh context — a new thread
    inherits no context, so a worker nested inside another worker's
    ``start()`` must not see that worker's credential either."""

    def start(self):
        _Captured.started.append(self)
        self.run_now()


def _threads(monkeypatch, module, cls=_Captured) -> list[_Captured]:
    _Captured.started = []
    monkeypatch.setattr(
        module, "threading", SimpleNamespace(Thread=cls, Lock=threading.Lock)
    )
    return _Captured.started


# ---------------------------------------------------------------------------
# The key reaches every worker, and only through memory
# ---------------------------------------------------------------------------


def _review_app(spec_path: Path) -> MagicMock:
    from src.input.extractor import extract_text_from_docx

    app = MagicMock()
    app.is_processing = False
    app.api_key_entry.get.return_value = f"  {FAKE_KEY}  "
    app._selected_files = [spec_path]
    app.file_list_panel.get_selected_files.return_value = [spec_path]
    app._get_project_context.return_value = ""
    app._gather_project_profile.return_value = None
    app._cross_check_var.get.return_value = False
    app._realtime_var = None
    app._realtime_workers_var = None
    app._selected_program_id = None
    app._project_context_tokens = 0
    app._extracted_specs = [extract_text_from_docx(spec_path)]
    return app


class TestTheWorkerSeesTheRunsKey:
    def test_starting_a_review(self, monkeypatch, tmp_path):
        started = _threads(monkeypatch, rrc)
        app = _review_app(fx.save_docx(fx.build_clean_three_part(), tmp_path, "230500.docx"))
        seen: list = []
        app._submit_batch_thread = lambda epoch: seen.append(
            (active_credential(), os.environ.get("ANTHROPIC_API_KEY"))
        )
        rrc.start_review(app)
        assert len(started) == 1
        # The key typed after the run began belongs to the next run.
        app.api_key_entry.get.return_value = OTHER_KEY
        started[0].run_now()

        credential, env_key = seen[0]
        assert credential.reveal() == FAKE_KEY and credential.source == "gui"
        assert env_key is None
        assert app._run_credential is credential
        assert not _key_in_environment(FAKE_KEY, OTHER_KEY)

    def test_polling_uses_the_runs_credential(self, monkeypatch):
        started = _threads(monkeypatch, bc)
        credential = ApiCredential(FAKE_KEY)
        seen: list = []
        app = MagicMock()
        app._run_credential = credential
        app._poll_and_collect_thread = lambda epoch: seen.append(active_credential())
        bc.poll_batch(app)
        started[0].run_now()
        assert seen == [credential]

    def test_collection_uses_the_runs_credential(self, monkeypatch):
        started = _threads(monkeypatch, bc)
        credential = ApiCredential(FAKE_KEY)
        seen: list = []

        def collect(*_a, **_k):
            seen.append(active_credential())
            raise RuntimeError("stop after the first API-reaching step")

        monkeypatch.setattr(bc, "collect_review_batch_results", collect)
        app = MagicMock()
        app._run_credential = credential
        app._trace_recorder = None
        app._batch_submission = SimpleNamespace(module_id="california_k12_mep", review_transport="batch")
        app._diagnostics_report = None
        bc.collect_batch_results(app)
        started[0].run_now()
        assert seen == [credential]
        assert app._dispatch_if_current.called  # the error still reached the Tk thread

    def test_reconnecting(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_TRACE", "0")
        started = _threads(monkeypatch, bc)
        seen: list = []
        app = MagicMock()
        app.is_processing = False
        app.api_key_entry.get.return_value = FAKE_KEY

        def reconstruct(log, progress):
            seen.append((active_credential(), os.environ.get("ANTHROPIC_API_KEY")))
            return None

        bc._begin_reconnect_run(
            app, reconstruct_fn=reconstruct, model="claude-opus-5",
            cycle_label="x", project_context="", cross_check_enabled=False,
            files_for_review=[], module_id="california_k12_mep", batch_label="msgbatch_x",
        )
        app.api_key_entry.get.return_value = OTHER_KEY
        started[0].run_now()
        credential, env_key = seen[0]
        assert credential.reveal() == FAKE_KEY
        assert env_key is None
        assert not _key_in_environment(FAKE_KEY, OTHER_KEY)

    def test_reconnecting_without_a_key_is_refused(self, monkeypatch):
        started = _threads(monkeypatch, bc)
        errors: list = []
        monkeypatch.setattr(bc.messagebox, "showerror", lambda *a, **k: errors.append(a))
        app = MagicMock()
        app.is_processing = False
        app.api_key_entry.get.return_value = "   "
        bc._begin_reconnect_run(
            app, reconstruct_fn=lambda log, progress: None, model="m",
            cycle_label="x", project_context="", cross_check_enabled=False,
            files_for_review=[], batch_label="b",
        )
        assert errors and started == []

    def test_the_drawing_digest(self, monkeypatch, tmp_path):
        from src.gui import context_controller as cc
        from src.input.drawing_digest import DrawingDigestError

        _threads(monkeypatch, cc, cls=_Sync)
        pdf = tmp_path / "M-101.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        seen: dict = {}
        monkeypatch.setattr(cc.filedialog, "askopenfilenames", lambda **kw: (str(pdf),))
        monkeypatch.setattr(cc, "validate_drawing_files", lambda paths: (list(paths), []))
        monkeypatch.setattr(
            cc, "build_digest_chunks",
            lambda files, **kw: [SimpleNamespace(parts=[object()])],
        )

        def preflight(chunks, **kw):
            seen["preflight"] = (active_credential(), os.environ.get("ANTHROPIC_API_KEY"))
            return SimpleNamespace(over_window_chunk_indices=[])

        def digest(chunks, **kw):
            seen["digest"] = (active_credential(), os.environ.get("ANTHROPIC_API_KEY"))
            raise DrawingDigestError("stop here")

        monkeypatch.setattr(cc, "preflight_digest_cost", preflight)
        monkeypatch.setattr(cc, "format_digest_confirm_message", lambda *a, **k: "ok")
        monkeypatch.setattr(cc, "run_drawing_digest", digest)
        monkeypatch.setattr(cc.messagebox, "askyesno", lambda *a, **k: True)
        errors: list = []
        monkeypatch.setattr(cc.messagebox, "showerror", lambda *a, **k: errors.append(a))
        monkeypatch.setattr(cc.messagebox, "showwarning", lambda *a, **k: None)
        app = MagicMock()
        app._drawing_digest_running = False
        app.is_processing = False
        app.api_key_entry.get.return_value = FAKE_KEY
        app.after.side_effect = lambda _ms, fn: fn()

        cc.attach_drawing_files(app)

        for step in ("preflight", "digest"):
            credential, env_key = seen[step]
            assert credential is not None and credential.reveal() == FAKE_KEY, step
            assert env_key is None, step
        assert errors and "stop here" in errors[0][1]
        assert app._drawing_digest_running is False
        assert not _key_in_environment(FAKE_KEY)

    def test_the_token_gauge_count(self, monkeypatch):
        import src.core.tokenizer as tokenizer

        _threads(monkeypatch, tac, cls=_Sync)
        seen: list = []

        def count(**_kwargs):
            seen.append((active_credential(), os.environ.get("ANTHROPIC_API_KEY")))
            return None  # "no estimate": the gauge keeps the local count

        monkeypatch.setattr(tokenizer, "count_tokens_via_api", count)
        spec = SimpleNamespace(
            filename="a.docx", source_path="/x/a.docx", content="PART 1 GENERAL",
            paragraph_map=[],
        )
        app = MagicMock()
        app.api_key_entry.get.return_value = FAKE_KEY
        app._exact_token_refresh_timer_id = None
        app.after.side_effect = lambda _ms, fn: fn()
        from src.core.code_cycles import DEFAULT_CYCLE

        tac.refresh_exact_token_count(
            app, [{"path": "/x/a.docx", "filename": "a.docx", "tokens": 10}], [spec],
            "", DEFAULT_CYCLE, 0, 0, dispatch=lambda fn: fn(),
        )
        credential, env_key = seen[0]
        assert credential.reveal() == FAKE_KEY and env_key is None

    def test_the_gauge_reuses_its_credential_while_the_key_is_unchanged(self):
        app = SimpleNamespace(api_key_entry=SimpleNamespace(get=lambda: FAKE_KEY))
        first = tac._gauge_credential(app)
        assert tac._gauge_credential(app) is first
        app.api_key_entry = SimpleNamespace(get=lambda: OTHER_KEY)
        second = tac._gauge_credential(app)
        assert second is not first and second.reveal() == OTHER_KEY
        app.api_key_entry = SimpleNamespace(get=lambda: "  ")
        assert tac._gauge_credential(app) is None

    def test_the_gauge_with_an_empty_field_uses_the_environment(self, monkeypatch):
        import src.core.tokenizer as tokenizer

        _threads(monkeypatch, tac, cls=_Sync)
        monkeypatch.setenv("ANTHROPIC_API_KEY", OTHER_KEY)
        seen: list = []
        monkeypatch.setattr(
            tokenizer, "count_tokens_via_api",
            lambda **kw: seen.append(active_credential()),
        )
        spec = SimpleNamespace(
            filename="a.docx", source_path="/x/a.docx", content="x", paragraph_map=[],
        )
        app = MagicMock()
        app.api_key_entry.get.return_value = ""
        app.after.side_effect = lambda _ms, fn: fn()
        from src.core.code_cycles import DEFAULT_CYCLE

        tac.refresh_exact_token_count(
            app, [{"path": "/x/a.docx", "filename": "a.docx", "tokens": 1}], [spec],
            "", DEFAULT_CYCLE, 0, 0, dispatch=lambda fn: fn(),
        )
        assert seen == [None]

    def test_a_finished_run_drops_its_credential(self):
        app = MagicMock()
        app._run_credential = ApiCredential(FAKE_KEY)
        app._trace_recorder = None
        rrc.on_review_error(app, "boom")
        assert app._run_credential is None
        app._run_credential = ApiCredential(FAKE_KEY)
        rrc.reset_ui(app)
        assert app._run_credential is None


# ---------------------------------------------------------------------------
# Tracing is optional in the GUI workers
# ---------------------------------------------------------------------------


class _App:
    """The surface ``submit_batch_thread`` / ``_reconnect_worker`` touch."""

    def __init__(self, diag: DiagnosticsReport | None = None):
        self._diagnostics_report = diag if diag is not None else DiagnosticsReport()
        self._review_transport_for_review = "realtime"
        self._selected_program_id_for_review = None
        self._selected_program_id = None
        self._routed_module_ids_for_review = ("california_k12_mep",)
        self._routing_assignments_for_review = ()
        self._project_profile_for_review = None
        self._selected_files_for_review = []
        self._project_context_for_review = ""
        self._cross_check_for_review = False
        self._realtime_review_workers_for_review = None
        self.input_dir = ""
        self._trace_recorder = None
        self.dispatched: list = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.run_button = MagicMock()
        self.log = SimpleNamespace(log_warning=self.warnings.append)

    def _dispatch_if_current(self, _epoch, fn):
        self.dispatched.append(fn)
        fn()

    def _make_diag_log(self, *_a):
        return lambda *a, **k: None

    def _make_diag_progress(self, *_a):
        return lambda *a, **k: None

    def _on_review_error(self, err):
        self.errors.append(str(err))


def _block_the_trace_directory(monkeypatch, tmp_path):
    blocker = tmp_path / "blocked-trace-root"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("SPEC_CRITIC_TRACE_DIR", str(blocker))


class TestTracingIsOptionalInTheWorkers:
    def test_a_trace_failure_warns_once_and_the_review_runs(self, monkeypatch, tmp_path):
        _block_the_trace_directory(monkeypatch, tmp_path)
        reached: list = []

        def start_review(app, program, run_epoch, diag, **kwargs):
            reached.append(kwargs["review_transport"])
            return SimpleNamespace(files_reviewed=["a.docx"], review_request_ids=["r0"])

        monkeypatch.setattr(bc, "_start_selected_review", start_review)
        handed_off: list = []
        monkeypatch.setattr(bc, "on_realtime_reviewed", lambda app, s: handed_off.append(s))
        app = _App()

        bc.submit_batch_thread(app, run_epoch=1)

        assert reached == ["realtime"]
        assert len(handed_off) == 1
        assert app.errors == []
        assert len(app.warnings) == 1 and app.warnings[0].startswith("Tracing is off")
        trace_warnings = [
            e for e in app._diagnostics_report.events
            if e.level == "warning" and "Tracing is off" in e.message
        ]
        assert len(trace_warnings) == 1
        assert app._trace_recorder is None and get_recorder() is None

    def test_a_failure_before_the_review_restores_the_widgets(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("program registry unavailable")

        monkeypatch.setattr(bc, "get_program", boom)
        app = _App()
        bc.submit_batch_thread(app, run_epoch=1)
        assert len(app.errors) == 1 and "program registry unavailable" in app.errors[0]
        assert get_recorder() is None and app._trace_recorder is None

    def test_a_failed_submission_stops_the_runs_recorder(self, monkeypatch):
        def fail(*_a, **_k):
            raise RuntimeError("submit failed")

        monkeypatch.setattr(bc, "_start_selected_review", fail)
        app = _App()
        bc.submit_batch_thread(app, run_epoch=1)
        assert app.errors and "submit failed" in app.errors[0]
        assert get_recorder() is None and app._trace_recorder is None

    def test_a_resume_with_a_trace_failure_still_reconnects(self, monkeypatch, tmp_path):
        _block_the_trace_directory(monkeypatch, tmp_path)
        handed_off: list = []
        monkeypatch.setattr(bc, "on_batch_submitted", lambda app, s: handed_off.append(s))
        app = _App()
        submission = object()
        bc._reconnect_worker(
            app, lambda log, progress: submission, "claude-opus-5", "CA 2025",
            "california_k12_mep", "run_old", 1, "msgbatch_x",
        )
        assert handed_off == [submission]
        assert app.errors == []
        assert len(app.warnings) == 1

    def test_a_failed_resume_stops_the_reopened_recorder(self, monkeypatch):
        app = _App(DiagnosticsReport(run_id="run_resumed"))

        def reconstruct(log, progress):
            assert get_recorder() is not None, "precondition: the trace reopened"
            raise RuntimeError("results stream unavailable")

        bc._reconnect_worker(
            app, reconstruct, "claude-opus-5", "CA 2025", "california_k12_mep",
            "run_resumed", 1, "msgbatch_x",
        )
        assert app.errors and "Recovery failed" in app.errors[0]
        assert get_recorder() is None and app._trace_recorder is None

    def test_an_unexpected_polling_error_restores_the_widgets(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("poll loop exploded")

        monkeypatch.setattr(bc, "poll_batch_bounded", boom)
        app = _App()
        rec = session.start_run_recorder(
            run_id="run_poll", mode="batch", model="m", cycle_label="c", files=[]
        )
        app._trace_recorder = rec
        app._batch_submission = SimpleNamespace(job=SimpleNamespace(batch_id="msgbatch_p"))
        bc.poll_and_collect_thread(app, run_epoch=1)
        assert app.errors and "poll loop exploded" in app.errors[0]
        assert get_recorder() is None and app._trace_recorder is None
        assert rec.writer_alive is False

    def test_control_a_successful_poll_leaves_the_recorder_to_collection(self, monkeypatch):
        monkeypatch.setattr(bc, "poll_batch_bounded", lambda *a, **k: PollOutcome(terminal=True))
        app = _App()
        app._collect_batch_results = lambda: None
        app.log.log_success = lambda *_a: None
        rec = session.start_run_recorder(
            run_id="run_ok", mode="batch", model="m", cycle_label="c", files=[]
        )
        try:
            app._trace_recorder = rec
            app._batch_submission = SimpleNamespace(job=SimpleNamespace(batch_id="msgbatch_ok"))
            bc.poll_and_collect_thread(app, run_epoch=1)
            assert get_recorder() is rec and app._trace_recorder is rec
        finally:
            session.stop_run_recorder(rec)


class TestAnOldRunNeverStopsANewOne:
    def test_a_late_collect_teardown(self, monkeypatch):
        _threads(monkeypatch, bc, cls=_Sync)
        old = session.start_run_recorder(
            run_id="run_old", mode="batch", model="m", cycle_label="c", files=[]
        )
        holder: dict = {}

        def collect(*_a, **_k):
            # While the old run collects, it fails; its error reaches the Tk
            # thread and the user starts a new run before the old worker's
            # ``finally`` runs.
            holder["new"] = session.start_run_recorder(
                run_id="run_new", mode="batch", model="m", cycle_label="c", files=[]
            )
            app._trace_recorder = holder["new"]
            raise RuntimeError("old run failed")

        monkeypatch.setattr(bc, "collect_review_batch_results", collect)
        app = MagicMock()
        app._trace_recorder = old
        app._run_credential = None
        app._batch_submission = SimpleNamespace(module_id="california_k12_mep", review_transport="batch")
        app._diagnostics_report = None

        bc.collect_batch_results(app)

        new = holder["new"]
        try:
            assert old.writer_alive is False
            assert new.writer_alive is True
            assert get_recorder() is new
            assert app._trace_recorder is new
        finally:
            session.stop_run_recorder(new)

    def test_a_failed_start_leaves_the_app_idle(self, monkeypatch):
        errors: list = []
        app = MagicMock()
        app.is_processing = False
        app.api_key_entry.get.return_value = FAKE_KEY
        app.log.log_error = lambda msg: errors.append(msg)
        bc._begin_reconnect_run(
            app, reconstruct_fn=lambda log, progress: None, model="m",
            cycle_label="x", project_context="", cross_check_enabled=False,
            files_for_review=[], module_id="no_such_module", batch_label="msgbatch_bad",
        )
        assert errors and "msgbatch_bad could not start" in errors[0]
        assert app.is_processing is False
        assert app._run_credential is None
