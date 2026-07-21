"""Terminal-state honesty for the review-complete handler.

When a run finishes but one or more specs failed review, the UI must not
present the same green "✓ Complete" terminal state (and "success"
diagnostics) as a fully-clean run. ``on_review_complete`` routes genuine
failures to the amber ``set_complete_with_errors`` button state and
finalizes diagnostics at ``warning`` level.

A program run whose only deviation is confirmed unsupported-skips is NOT an
error: it gets the distinct ``set_complete_with_coverage_gaps`` terminal
state, finalizes at ``info`` level, and never claims a spec "failed review".

Diagnostics are finalized BEFORE the modal export dialog opens so the
recorded run duration cannot absorb user idle time in the save dialog; the
export outcome lands as a separate post-finish diagnostics event.

This file imports the GUI controller, which imports ``tkinter`` at module
scope, so it is registered in ``conftest._GUI_DEPENDENT_TESTS`` and
skipped at collection time on hosts without the ``python3-tk`` package.
"""
from __future__ import annotations

from src.gui.review_run_controller import (
    TerminalState,
    classify_terminal_state,
    on_review_complete,
)
from src.review.reviewer import ReviewResult


class _Recorder:
    """Captures the calls ``on_review_complete`` makes on the app."""

    def __init__(self):
        self.button_calls: list[str] = []
        self.finalize_calls: list[tuple[str, str, str]] = []
        self.export_status = "success"
        self._last_result = None
        # Ordered journal of the load-bearing calls, for ordering asserts.
        self.journal: list[str] = []

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
            self._outer.journal.append("button:complete")

        def set_complete_with_errors(self):
            self._outer.button_calls.append("complete_with_errors")
            self._outer.journal.append("button:complete_with_errors")

        def set_complete_with_coverage_gaps(self):
            self._outer.button_calls.append("complete_with_coverage_gaps")
            self._outer.journal.append("button:complete_with_coverage_gaps")

    def __init_app__(self):
        self.progress_bar = self._Bar()
        self.log = self._Log()
        self.run_button = self._Button(self)

    # methods the handler calls on ``app``
    def _export_report_to_file(self, _result) -> str:
        self.journal.append("export")
        return self.export_status

    def _finalize_diagnostics(self, phase: str, level: str, message: str) -> None:
        self.finalize_calls.append((phase, level, message))
        self.journal.append(f"finalize:{level}")

    def _reset_ui(self):
        pass

    def update_idletasks(self):
        self.journal.append("update_idletasks")

    def after(self, _ms, _fn):
        # Do not actually schedule the reset; just record nothing.
        pass


def _make_app(export_status: str = "success") -> _Recorder:
    app = _Recorder()
    app.__init_app__()
    app.export_status = export_status
    return app


def _result(*, error: str | None) -> object:
    rv = ReviewResult(findings=[], error=error, model="claude-opus-4-8")

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

    def test_program_coverage_gap_gets_partial_coverage_state(self):
        # Confirmed unsupported-skips only: NOT an error. The terminal state
        # is the dedicated partial-coverage one, diagnostics finalize at
        # "info", and no message claims a spec failed review.
        app = _make_app()
        result = _result(error=None)
        result.status = "partial"  # must be ignored by the classifier
        result.skipped_files = ["27 10 00 Structured Cabling.docx"]

        on_review_complete(app, result)

        assert app.button_calls == ["complete_with_coverage_gaps"]
        finalize = [
            (lvl, msg) for _phase, lvl, msg in app.finalize_calls
        ]
        assert len(finalize) == 1
        level, message = finalize[0]
        assert level == "info"
        assert "partial coverage" in message
        assert "failed review" not in message
        assert "error" not in message.lower()

    def test_missing_module_ids_is_errors(self):
        app = _make_app()
        result = _result(error=None)
        result.skipped_files = ["27 10 00 Structured Cabling.docx"]
        result.missing_module_ids = ["datacenter_electrical"]

        on_review_complete(app, result)

        assert app.button_calls == ["complete_with_errors"]
        levels = [lvl for _phase, lvl, _msg in app.finalize_calls]
        assert "warning" in levels
        assert "success" not in levels

    def test_module_errors_is_errors(self):
        app = _make_app()
        result = _result(error=None)
        result.module_errors = {
            "datacenter_fire": "collection failed: HTTP 529"
        }

        on_review_complete(app, result)

        assert app.button_calls == ["complete_with_errors"]
        levels = [lvl for _phase, lvl, _msg in app.finalize_calls]
        assert "warning" in levels
        assert "success" not in levels

    def test_failed_review_specs_is_errors(self):
        app = _make_app()
        result = _result(error=None)
        result.failed_review_specs = ["datacenter_architecture: GLAZING.docx"]

        on_review_complete(app, result)

        assert app.button_calls == ["complete_with_errors"]
        levels = [lvl for _phase, lvl, _msg in app.finalize_calls]
        assert "warning" in levels
        assert "success" not in levels

    def test_diagnostics_finalized_before_export(self):
        # D1: the modal save dialog must not be able to inflate the
        # diagnostics duration — finalize happens strictly before export.
        for status in ("success", "canceled", "error"):
            app = _make_app(export_status=status)
            on_review_complete(app, _result(error=None))
            assert "export" in app.journal
            assert app.journal.index("finalize:success") < app.journal.index("export")
            # The terminal button + a UI paint land before the modal too.
            assert app.journal.index("button:complete") < app.journal.index("export")
            assert app.journal.index("update_idletasks") < app.journal.index("export")


class TestClassifyTerminalState:
    def test_clean(self):
        state = classify_terminal_state(_result(error=None))
        assert state == TerminalState(kind="success")

    def test_review_error(self):
        state = classify_terminal_state(_result(error="1 spec(s) had errors"))
        assert state.kind == "errors"
        assert state.review_error == "1 spec(s) had errors"

    def test_skips_only_is_coverage_gap(self):
        result = _result(error=None)
        result.skipped_files = ["a.docx", "b.docx"]
        state = classify_terminal_state(result)
        assert state.kind == "coverage_gap"
        assert state.skipped_files == ("a.docx", "b.docx")

    def test_status_string_is_ignored(self):
        result = _result(error=None)
        result.status = "partial"
        assert classify_terminal_state(result).kind == "success"

    def test_missing_modules_trump_skips(self):
        result = _result(error=None)
        result.skipped_files = ["a.docx"]
        result.missing_module_ids = ["datacenter_fire"]
        state = classify_terminal_state(result)
        assert state.kind == "errors"
        assert state.missing_module_ids == ("datacenter_fire",)

    def test_module_errors(self):
        result = _result(error=None)
        result.module_errors = {"m2": "boom", "m1": "bang"}
        state = classify_terminal_state(result)
        assert state.kind == "errors"
        # Sorted for deterministic rendering.
        assert state.module_errors == (("m1", "bang"), ("m2", "boom"))

    def test_failed_review_specs(self):
        result = _result(error=None)
        result.failed_review_specs = ["mod: spec.docx"]
        assert classify_terminal_state(result).kind == "errors"

    def test_no_review_result_defaults_success(self):
        class _Bare:
            review_result = None

        assert classify_terminal_state(_Bare()).kind == "success"


class TestResetUiKeepsBar:
    def test_reset_ui_does_not_hide_progress_bar(self):
        from src.gui.review_run_controller import reset_ui

        calls: list[str] = []

        class _Bar:
            def pack_forget(self):
                calls.append("pack_forget")

        class _Btn:
            def set_ready(self):
                pass

            def configure(self, **_k):
                pass

        class _App:
            progress_bar = _Bar()
            run_button = _Btn()
            is_processing = True
            _batch_submission = object()
            _trace_recorder = None

        app = _App()
        reset_ui(app)
        # C5: the completed bar persists at 100% until the next run
        # re-packs and zeroes it.
        assert calls == []
        assert app.is_processing is False
        assert app._batch_submission is None
