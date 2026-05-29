"""Terminal-state honesty for the review-complete handler (P0-1).

When a run finishes but one or more specs failed review, the UI must not
present the same green "✓ Complete" terminal state (and "success"
diagnostics) as a fully-clean run. ``on_review_complete`` routes a
partial failure to the amber ``set_complete_with_errors`` button state
and finalizes diagnostics at ``warning`` level instead of ``success``.

This file imports the GUI controller, which imports ``tkinter`` at module
scope, so it is registered in ``conftest._GUI_DEPENDENT_TESTS`` and
skipped at collection time on hosts without the ``python3-tk`` package.
"""
from __future__ import annotations

from src.gui.review_run_controller import on_review_complete
from src.review.reviewer import ReviewResult


class _Recorder:
    """Captures the calls ``on_review_complete`` makes on the app."""

    def __init__(self):
        self.button_calls: list[str] = []
        self.finalize_calls: list[tuple[str, str, str]] = []
        self.export_status = "success"
        self._last_result = None

    # --- attributes on_review_complete touches ---
    class _Bar:
        def set(self, _v):
            pass

    class _Log:
        def log(self, *a, **k):
            pass

        def log_warning(self, *a, **k):
            pass

        def log_success(self, *a, **k):
            pass

    class _Button:
        def __init__(self, outer):
            self._outer = outer

        def set_complete(self):
            self._outer.button_calls.append("complete")

        def set_complete_with_errors(self):
            self._outer.button_calls.append("complete_with_errors")

    def __init_app__(self):
        self.progress_bar = self._Bar()
        self.log = self._Log()
        self.run_button = self._Button(self)

    # methods the handler calls on ``app``
    def _export_report_to_file(self, _result) -> str:
        return self.export_status

    def _finalize_diagnostics(self, phase: str, level: str, message: str) -> None:
        self.finalize_calls.append((phase, level, message))

    def _reset_ui(self):
        pass

    def after(self, _ms, _fn):
        # Do not actually schedule the reset; just record nothing.
        pass


def _make_app(export_status: str = "success") -> _Recorder:
    app = _Recorder()
    app.__init_app__()
    app.export_status = export_status
    return app


def _result(*, error: str | None) -> object:
    rv = ReviewResult(findings=[], error=error, model="claude-opus-4-7")

    class _Result:
        review_result = rv
        cross_check_result = None
        total_elapsed_seconds = 1.0

    return _Result()


class TestTerminalState:
    def test_clean_run_is_green_and_success(self):
        app = _make_app()
        on_review_complete(app, _result(error=None))
        assert app.button_calls == ["complete"]
        # The success-path finalize is logged at "success".
        levels = [lvl for _phase, lvl, _msg in app.finalize_calls]
        assert "success" in levels
        assert "warning" not in levels

    def test_partial_failure_is_amber_and_warning(self):
        app = _make_app()
        on_review_complete(
            app, _result(error="2 spec(s) had errors: B.docx: truncated; C.docx: errored")
        )
        assert app.button_calls == ["complete_with_errors"]
        levels = [lvl for _phase, lvl, _msg in app.finalize_calls]
        # Never finalizes as bare "success" when specs failed review.
        assert "success" not in levels
        assert "warning" in levels

    def test_partial_failure_with_canceled_export_still_warns(self):
        # Even if the export is canceled, a review failure must not be
        # downgraded to a neutral "info" terminal state.
        app = _make_app(export_status="canceled")
        on_review_complete(app, _result(error="1 spec(s) had errors: B.docx: truncated"))
        assert app.button_calls == ["complete_with_errors"]
        levels = [lvl for _phase, lvl, _msg in app.finalize_calls]
        assert "warning" in levels
        assert "success" not in levels
