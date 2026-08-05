"""Tests for the additive post-run "Save HTML Report…" hook.

Covers the narrow integration contract from
``docs/html_report_baseline_evidence.md``:

* ``export_html_report_to_file`` mirrors the DOCX controller's
  canceled/success/error contract — cancel is a no-op, failure is visible
  and non-destructive, browser auto-open is nonfatal, and the retained
  result is never mutated.
* The GUI change is additive-only: a footer button + a ``_last_result``
  property that enables it, with zero edits to the review lifecycle
  (``review_run_controller`` keeps its untouched ``app._last_result =
  result`` seam and knows nothing about HTML).

Skips when tkinter is unavailable (the controller imports
``tkinter.filedialog``), like the other GUI tests.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

from src.gui import report_controller
from tests.test_html_report_exporter import build_full_pipeline_result


class _FakeLog:
    def __init__(self):
        self.steps: list[str] = []
        self.successes: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def log_step(self, msg):
        self.steps.append(msg)

    def log_success(self, msg):
        self.successes.append(msg)

    def log_warning(self, msg):
        self.warnings.append(msg)

    def log_error(self, msg):
        self.errors.append(msg)


class _FakeApp:
    def __init__(self):
        self.log = _FakeLog()


class TestExportHtmlController:
    def test_cancel_is_a_noop(self, monkeypatch, tmp_path):
        app = _FakeApp()
        monkeypatch.setattr(
            report_controller.filedialog, "asksaveasfilename", lambda **kw: ""
        )
        status = report_controller.export_html_report_to_file(
            app, build_full_pipeline_result()
        )
        assert status == "canceled"
        assert app.log.warnings == ["HTML export canceled"]
        assert list(tmp_path.iterdir()) == []

    def test_success_writes_file_and_opens_browser(self, monkeypatch, tmp_path):
        app = _FakeApp()
        out = tmp_path / "report.html"
        opened: list[str] = []
        monkeypatch.setattr(
            report_controller.filedialog, "asksaveasfilename", lambda **kw: str(out)
        )
        monkeypatch.setattr(
            report_controller.webbrowser, "open", lambda uri: opened.append(uri)
        )
        result = build_full_pipeline_result()
        snapshot = copy.deepcopy(result)
        status = report_controller.export_html_report_to_file(app, result)
        assert status == "success"
        text = out.read_bytes().decode("utf-8")
        assert text.startswith("<!DOCTYPE html>")
        assert "SENTINEL-ISSUE-MULTIFILE" in text
        assert opened and opened[0].startswith("file://")
        assert result == snapshot  # read-only input

    def test_browser_open_failure_is_nonfatal(self, monkeypatch, tmp_path):
        app = _FakeApp()
        out = tmp_path / "report.html"
        monkeypatch.setattr(
            report_controller.filedialog, "asksaveasfilename", lambda **kw: str(out)
        )

        def _boom(uri):
            raise RuntimeError("no browser in this environment")

        monkeypatch.setattr(report_controller.webbrowser, "open", _boom)
        status = report_controller.export_html_report_to_file(
            app, build_full_pipeline_result()
        )
        assert status == "success"
        assert out.exists()
        assert app.log.errors == []

    def test_write_failure_is_visible_and_nondestructive(self, monkeypatch, tmp_path):
        app = _FakeApp()
        out = tmp_path / "report.html"
        monkeypatch.setattr(
            report_controller.filedialog, "asksaveasfilename", lambda **kw: str(out)
        )

        def _fail(result, path):
            raise OSError("disk full")

        monkeypatch.setattr(report_controller, "write_html_report", _fail)
        result = build_full_pipeline_result()
        snapshot = copy.deepcopy(result)
        status = report_controller.export_html_report_to_file(app, result)
        assert status == "error"
        assert any("disk full" in e for e in app.log.errors)
        assert result == snapshot

    def test_docx_export_contract_unchanged(self):
        # The automatic DOCX exporter still exists with its original name and
        # signature — the HTML action is a sibling, not a replacement.
        assert callable(report_controller.export_report_to_file)


class TestAdditiveGuiWiring:
    """Source-level pins: the hook is additive and the lifecycle is untouched."""

    GUI = Path(__file__).resolve().parent.parent / "src" / "gui" / "gui.py"
    RUN_CONTROLLER = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "gui"
        / "review_run_controller.py"
    )

    def test_button_created_disabled_in_footer(self):
        source = self.GUI.read_text(encoding="utf-8")
        assert "save_html_btn" in source
        assert "Save HTML Report…" in source
        block = source.split("self.save_html_btn = ")[1].split(".pack(")[0]
        assert 'state="disabled"' in block
        assert "_on_save_html_report_clicked" in block

    def test_last_result_property_enables_button(self):
        source = self.GUI.read_text(encoding="utf-8")
        assert "@property" in source
        assert "def _last_result(self):" in source
        assert "@_last_result.setter" in source
        setter = source.split("@_last_result.setter")[1].split("def _on_save")[0]
        assert "save_html_btn" in setter and '"normal"' in setter and '"disabled"' in setter

    def test_click_handler_guards(self):
        source = self.GUI.read_text(encoding="utf-8")
        handler = source.split("def _on_save_html_report_clicked(self):")[1].split(
            "def "
        )[0]
        assert "is_processing" in handler
        assert "No completed review in this session yet." in handler
        assert "export_html_report_to_file" in handler

    def test_review_lifecycle_untouched(self):
        source = self.RUN_CONTROLLER.read_text(encoding="utf-8")
        # The dormant seam this feature keys off is still exactly the plain
        # assignment — the lifecycle file gained no HTML awareness.
        assert "app._last_result = result" in source
        assert "html" not in source.lower()
        assert "save_html_btn" not in source
