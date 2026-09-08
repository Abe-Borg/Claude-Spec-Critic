"""Window-close handler (A-10) and file logging / print routing (B-20).

``SpecReviewApp._on_close`` is exercised against a fake app object (no Tk
root, no display): the confirmation policy is a pure function, the
recorder-stop/destroy ordering is recorded, and the ``WM_DELETE_WINDOW``
registration plus the ``print`` ban are AST/source pins over ``gui.py``.

``src.gui.gui`` imports ``customtkinter`` at module scope, so this file skips
cleanly without it (repo convention).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("customtkinter")

from src.gui import gui as gui_module  # noqa: E402
from src.gui.gui import SpecReviewApp, close_confirmation_message  # noqa: E402

_GUI_PATH = Path(gui_module.__file__)


class _FakeApp:
    """Only what ``_on_close`` / ``_confirm_close`` touch, plus a call log."""

    _on_close = SpecReviewApp._on_close
    _confirm_close = SpecReviewApp._confirm_close

    def __init__(
        self, *, recorder=None, is_processing=False, transport="batch", digest=False, export=False
    ):
        self.calls: list[tuple] = []
        self._trace_recorder = recorder
        self.is_processing = is_processing
        self._review_transport_for_review = transport
        self._drawing_digest_running = digest
        self._report_export_running = export

    def destroy(self):
        self.calls.append(("destroy",))


@pytest.fixture
def stop_spy(monkeypatch):
    """Record ``stop_run_recorder`` calls in the gui module's namespace."""
    seen: list = []

    def _stop(recorder):
        seen.append(recorder)

    monkeypatch.setattr(gui_module, "stop_run_recorder", _stop)
    monkeypatch.setattr(gui_module, "get_recorder", lambda: None)
    return seen


@pytest.fixture
def ask_spy(monkeypatch):
    """Scripted ``messagebox.askyesno`` that records its kwargs."""
    state = {"answer": True, "calls": []}

    def _ask(title, message, **options):
        state["calls"].append((title, message, options))
        return state["answer"]

    monkeypatch.setattr(gui_module.messagebox, "askyesno", _ask)
    return state


class TestCloseConfirmationPolicy:
    def test_idle_needs_no_confirmation(self):
        assert close_confirmation_message(
            is_processing=False, review_transport="batch", drawing_digest_running=False
        ) is None

    def test_batch_run_needs_no_confirmation(self):
        # A batch keeps running remotely and is offered for resume next launch.
        assert close_confirmation_message(
            is_processing=True, review_transport="batch", drawing_digest_running=False
        ) is None

    def test_realtime_run_warns_it_cannot_be_resumed_unlike_batch(self):
        message = close_confirmation_message(
            is_processing=True, review_transport="realtime", drawing_digest_running=False
        )
        assert message is not None
        assert "real-time" in message.lower()
        assert "cannot be resumed" in message
        assert "batch" in message.lower()

    def test_drawing_digest_warns(self):
        message = close_confirmation_message(
            is_processing=False, review_transport="batch", drawing_digest_running=True
        )
        assert message is not None
        assert "drawing analysis" in message.lower()

    def test_report_export_warns_even_during_a_batch_run(self):
        # After collection the pending state is already cleared and the
        # completed result lives only in memory while the worker writes the
        # report — the batch transport is no reason to close silently.
        message = close_confirmation_message(
            is_processing=True,
            review_transport="batch",
            drawing_digest_running=False,
            report_export_running=True,
        )
        assert message is not None
        assert "report is being written" in message.lower()

    def test_on_demand_export_warns_while_idle(self):
        message = close_confirmation_message(
            is_processing=False,
            review_transport="batch",
            drawing_digest_running=False,
            report_export_running=True,
        )
        assert message is not None
        assert "report" in message.lower()

    def test_export_flag_defaults_to_false(self):
        assert close_confirmation_message(
            is_processing=True, review_transport="batch", drawing_digest_running=False
        ) is None


class TestOnClose:
    def test_idle_close_stops_recorder_then_destroys_without_prompt(self, stop_spy, ask_spy):
        rec = object()
        app = _FakeApp(recorder=rec)
        app._on_close()
        assert ask_spy["calls"] == []  # nothing to confirm
        assert stop_spy == [rec]
        assert app.calls == [("destroy",)]
        assert app._trace_recorder is None

    def test_stop_precedes_destroy(self, monkeypatch, ask_spy):
        order: list[str] = []
        app = _FakeApp(recorder=object())
        monkeypatch.setattr(gui_module, "stop_run_recorder", lambda r: order.append("stop"))
        monkeypatch.setattr(gui_module, "get_recorder", lambda: None)
        app.destroy = lambda: order.append("destroy")  # type: ignore[method-assign]
        app._on_close()
        assert order == ["stop", "destroy"]

    def test_realtime_run_prompts_and_no_keeps_window_open(self, stop_spy, ask_spy):
        ask_spy["answer"] = False
        app = _FakeApp(recorder=object(), is_processing=True, transport="realtime")
        app._on_close()
        assert len(ask_spy["calls"]) == 1
        title, message, options = ask_spy["calls"][0]
        assert options.get("parent") is app  # owned by the app window
        assert "cannot be resumed" in message
        assert stop_spy == []  # nothing torn down: the run continues
        assert app.calls == []

    def test_realtime_run_prompts_and_yes_stops_then_destroys(self, stop_spy, ask_spy):
        rec = object()
        app = _FakeApp(recorder=rec, is_processing=True, transport="realtime")
        app._on_close()
        assert len(ask_spy["calls"]) == 1
        assert stop_spy == [rec]
        assert app.calls == [("destroy",)]

    def test_batch_run_closes_without_prompt_but_still_drains_trace(self, stop_spy, ask_spy):
        rec = object()
        app = _FakeApp(recorder=rec, is_processing=True, transport="batch")
        app._on_close()
        assert ask_spy["calls"] == []
        assert stop_spy == [rec]
        assert app.calls == [("destroy",)]

    def test_drawing_digest_prompts(self, stop_spy, ask_spy):
        ask_spy["answer"] = False
        app = _FakeApp(recorder=None, digest=True)
        app._on_close()
        assert len(ask_spy["calls"]) == 1
        assert "drawing analysis" in ask_spy["calls"][0][1].lower()
        assert app.calls == []

    def test_report_export_in_flight_prompts_on_a_batch_run(self, stop_spy, ask_spy):
        ask_spy["answer"] = False
        app = _FakeApp(recorder=None, is_processing=True, transport="batch", export=True)
        app._on_close()
        assert len(ask_spy["calls"]) == 1
        assert "report is being written" in ask_spy["calls"][0][1].lower()
        assert app.calls == []  # No -> window stays open, nothing torn down

    def test_report_export_prompt_yes_still_drains_trace_then_destroys(self, stop_spy, ask_spy):
        rec = object()
        app = _FakeApp(recorder=rec, export=True)
        app._on_close()
        assert len(ask_spy["calls"]) == 1
        assert stop_spy == [rec]
        assert app.calls == [("destroy",)]

    def test_falls_back_to_the_global_recorder(self, monkeypatch, ask_spy):
        # A recorder installed globally but not on the app attribute (e.g. a
        # resumed run) is still drained.
        sentinel = object()
        seen: list = []
        monkeypatch.setattr(gui_module, "get_recorder", lambda: sentinel)
        monkeypatch.setattr(gui_module, "stop_run_recorder", lambda r: seen.append(r))
        app = _FakeApp(recorder=None)
        app._on_close()
        assert seen == [sentinel]
        assert app.calls == [("destroy",)]

    def test_recorder_failure_never_blocks_closing(self, monkeypatch, ask_spy):
        def _boom(_recorder):
            raise RuntimeError("writer thread wedged")

        monkeypatch.setattr(gui_module, "stop_run_recorder", _boom)
        monkeypatch.setattr(gui_module, "get_recorder", lambda: None)
        app = _FakeApp(recorder=object())
        app._on_close()
        assert app.calls == [("destroy",)]
        assert app._trace_recorder is None


def _method_body(class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(_GUI_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"{class_name}.{method_name} not found")


class TestSourcePins:
    def test_wm_delete_window_protocol_registered_in_init(self):
        init = _method_body("SpecReviewApp", "__init__")
        registered = False
        for node in ast.walk(init):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "protocol"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "WM_DELETE_WINDOW"
            ):
                target = node.args[1]
                assert isinstance(target, ast.Attribute) and target.attr == "_on_close"
                registered = True
        assert registered, "SpecReviewApp.__init__ must bind WM_DELETE_WINDOW to _on_close"

    def test_on_close_method_exists_and_stops_recorder_before_destroy(self):
        body = _method_body("SpecReviewApp", "_on_close")
        source = ast.unparse(body)
        assert source.index("stop_run_recorder(") < source.index("self.destroy()")

    def test_no_print_calls_remain_in_gui(self):
        tree = ast.parse(_GUI_PATH.read_text(encoding="utf-8"))
        prints = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        assert prints == [], f"print() calls remain in gui.py at lines {prints}"
        source = _GUI_PATH.read_text(encoding="utf-8")
        assert "print(" not in source

    def test_drag_and_drop_diagnostics_go_through_logging(self):
        source = _GUI_PATH.read_text(encoding="utf-8")
        assert "_log = logging.getLogger(__name__)" in source
        assert source.count("Drag-and-drop unavailable") == 3
        assert "_log.warning(\"Drag-and-drop unavailable: %s\", e)" in source
        assert "_log.info(" in source

    def test_main_configures_file_logging_before_building_the_app(self):
        tree = ast.parse(_GUI_PATH.read_text(encoding="utf-8"))
        main = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        statements = [ast.unparse(node) for node in main.body if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Constant)]
        assert statements[0] == "configure_file_logging()"
        app_index = next(i for i, text in enumerate(statements) if "SpecReviewApp()" in text)
        assert app_index > 0
