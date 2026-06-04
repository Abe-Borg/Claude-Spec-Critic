"""Multi-folder file accumulation in the file-selection controller.

Locks in the behavior that lets a user load specs from more than one
folder: each Browse / drag-and-drop action *accumulates* onto the existing
selection (de-duped by resolved path) instead of replacing it, and the
Clear button is the explicit reset.

Hermetic: ``file_selection_controller`` imports ``filedialog`` lazily and
the package ``__init__`` re-exports ``main`` lazily, so this module loads
without ``tkinter`` / ``customtkinter``. A small ``FakeApp`` stands in for
the real ``SpecReviewApp`` so no Tk root is needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.gui.file_selection_controller import (
    apply_selected_specs,
    clear_selection,
    merge_selected_specs,
)
from src.gui.token_analysis_controller import resolve_initial_selection
from src.gui.token_analysis_controller import (
    CallMetrics,
    compute_call_metrics,
)


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------
class _FakeLog:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.steps: list[str] = []

    def log_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def log_step(self, msg: str) -> None:
        self.steps.append(msg)

    def log(self, msg: str, level: str = "info") -> None:  # pragma: no cover
        pass


class _FakeEntry:
    def __init__(self) -> None:
        self.text = ""

    def delete(self, start, end) -> None:
        self.text = ""

    def insert(self, index, value) -> None:
        self.text = value


class _FakePanel:
    def __init__(self) -> None:
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1


class _FakeGauge:
    def __init__(self) -> None:
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1


class _FakeButton:
    def __init__(self) -> None:
        self.state = "normal"

    def configure(self, state=None, **_) -> None:
        if state is not None:
            self.state = state


class _FakeApp:
    """Minimal stand-in exercising only what the controller touches."""

    def __init__(self) -> None:
        self.log = _FakeLog()
        self.input_dir_entry = _FakeEntry()
        self.file_list_panel = _FakePanel()
        self.token_gauge = _FakeGauge()
        self.run_button = _FakeButton()
        self.input_dir = None
        self._selected_files: list[Path] = []
        self._loaded_file_data: list = ["stale"]
        self._extracted_specs: list = ["stale"]
        self._analysis_epoch = 0
        self._exact_token_refresh_timer_id = None
        self.analyzed_with: list[list[Path]] = []
        self.after_cancelled: list = []

    # The controller calls these app methods.
    def _analyze_tokens(self, paths) -> None:
        self.analyzed_with.append(list(paths))

    def after_cancel(self, timer_id) -> None:
        self.after_cancelled.append(timer_id)


def _docx(folder: str, name: str) -> Path:
    return Path(f"/{folder}/{name}.docx")


# --------------------------------------------------------------------------
# merge_selected_specs (pure)
# --------------------------------------------------------------------------
def test_merge_unions_across_folders_preserving_order():
    a = [_docx("folderA", "s1"), _docx("folderA", "s2")]
    b = [_docx("folderB", "s3"), _docx("folderB", "s4")]
    assert merge_selected_specs(a, b) == a + b


def test_merge_dedups_exact_duplicates_keeping_first_position():
    a = [_docx("folderA", "s1"), _docx("folderA", "s2")]
    b = [_docx("folderA", "s1"), _docx("folderB", "s3")]
    merged = merge_selected_specs(a, b)
    assert merged == [
        _docx("folderA", "s1"),
        _docx("folderA", "s2"),
        _docx("folderB", "s3"),
    ]


def test_merge_into_empty_is_just_new():
    new = [_docx("folderA", "s1")]
    assert merge_selected_specs([], new) == new


def test_merge_does_not_mutate_inputs():
    a = [_docx("folderA", "s1")]
    b = [_docx("folderB", "s2")]
    merge_selected_specs(a, b)
    assert a == [_docx("folderA", "s1")]
    assert b == [_docx("folderB", "s2")]


def test_merge_dedups_same_file_via_two_spellings(tmp_path):
    """``..`` / symlink spellings of one file collapse via resolve()."""
    real = tmp_path / "spec.docx"
    real.write_text("x")
    alt = tmp_path / "sub" / ".." / "spec.docx"
    merged = merge_selected_specs([real], [alt])
    assert len(merged) == 1


# --------------------------------------------------------------------------
# apply_selected_specs (accumulation)
# --------------------------------------------------------------------------
def test_second_folder_accumulates_not_replaces():
    app = _FakeApp()
    folder_a = [_docx("folderA", "s1"), _docx("folderA", "s2")]
    folder_b = [_docx("folderB", "s3")]

    apply_selected_specs(app, folder_a)
    assert app._selected_files == folder_a

    apply_selected_specs(app, folder_b)
    # The regression guard: folder A's files survive the second selection.
    assert app._selected_files == folder_a + folder_b
    # Re-analysis runs on the full merged set.
    assert app.analyzed_with[-1] == folder_a + folder_b
    assert app.input_dir_entry.text == "3 files selected"


def test_reselecting_existing_files_is_a_noop():
    app = _FakeApp()
    folder_a = [_docx("folderA", "s1"), _docx("folderA", "s2")]
    apply_selected_specs(app, folder_a)
    analyses_before = len(app.analyzed_with)

    # Drop the exact same files again.
    apply_selected_specs(app, list(folder_a))

    assert app._selected_files == folder_a
    assert len(app.analyzed_with) == analyses_before  # no re-analysis
    assert any("No new files added" in w for w in app.log.warnings)


def test_partial_overlap_adds_only_new_files():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "s1")])
    apply_selected_specs(app, [_docx("folderA", "s1"), _docx("folderB", "s2")])
    assert app._selected_files == [_docx("folderA", "s1"), _docx("folderB", "s2")]
    assert app.analyzed_with[-1] == [_docx("folderA", "s1"), _docx("folderB", "s2")]


def test_single_file_entry_shows_full_path():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "only")])
    assert app.input_dir_entry.text == str(_docx("folderA", "only"))
    assert app.input_dir == Path("/folderA")


def test_unsupported_only_selection_warns_and_keeps_state():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "s1")])
    state_before = list(app._selected_files)
    analyses_before = len(app.analyzed_with)

    apply_selected_specs(app, [Path("/folderB/notes.txt"), Path("/folderB/img.png")])

    assert app._selected_files == state_before
    assert len(app.analyzed_with) == analyses_before
    assert any("No supported" in w for w in app.log.warnings)


def test_mixed_selection_keeps_only_supported():
    app = _FakeApp()
    apply_selected_specs(
        app, [_docx("folderA", "s1"), Path("/folderA/readme.txt")]
    )
    assert app._selected_files == [_docx("folderA", "s1")]


# --------------------------------------------------------------------------
# clear_selection (Clear button)
# --------------------------------------------------------------------------
def test_clear_resets_everything():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "s1"), _docx("folderB", "s2")])
    epoch_before = app._analysis_epoch

    clear_selection(app)

    assert app._selected_files == []
    assert app.input_dir is None
    assert app.input_dir_entry.text == ""
    assert app._loaded_file_data == []
    assert app._extracted_specs == []
    assert app.file_list_panel.reset_calls >= 1
    assert app.token_gauge.reset_calls >= 1
    assert app.run_button.state == "disabled"
    # Epoch bumped so an in-flight analysis can't repopulate the panel.
    assert app._analysis_epoch > epoch_before
    assert any("Cleared file selection" in s for s in app.log.steps)


def test_clear_cancels_pending_exact_token_timer():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "s1")])
    app._exact_token_refresh_timer_id = "timer-123"

    clear_selection(app)

    assert "timer-123" in app.after_cancelled
    assert app._exact_token_refresh_timer_id is None


def test_clear_when_empty_is_idempotent_and_quiet():
    app = _FakeApp()
    clear_selection(app)
    assert app._selected_files == []
    # No "Cleared" message when nothing was loaded.
    assert not any("Cleared file selection" in s for s in app.log.steps)


def test_can_load_again_after_clear():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "s1")])
    clear_selection(app)
    apply_selected_specs(app, [_docx("folderB", "s2")])
    assert app._selected_files == [_docx("folderB", "s2")]
    assert app.analyzed_with[-1] == [_docx("folderB", "s2")]


# --------------------------------------------------------------------------
# resolve_initial_selection (checkbox-state preservation across reload)
# --------------------------------------------------------------------------
# Regression guard: because loading another folder re-analyzes the *full*
# merged list and FileListPanel.load_files rebuilds every row, a naive reload
# would reset all checkboxes to checked — silently re-selecting a file the
# user had unchecked and pulling it into the review. resolve_initial_selection
# is the seam that preserves the prior state while defaulting new files on.
def test_unchecked_file_survives_accumulation_reload():
    a, b, c = _docx("folderA", "s1"), _docx("folderA", "s2"), _docx("folderB", "s3")
    prior = {a: True, b: False}  # user unchecked B...
    merged = [a, b, c]           # ...then loaded folder B (adds C)
    assert resolve_initial_selection(merged, prior) == {a: True, b: False, c: True}


def test_first_load_defaults_all_selected():
    a, b = _docx("folderA", "s1"), _docx("folderA", "s2")
    assert resolve_initial_selection([a, b], {}) == {a: True, b: True}


def test_none_prior_treated_as_empty():
    a = _docx("folderA", "s1")
    assert resolve_initial_selection([a], None) == {a: True}


def test_mixed_prior_states_preserved_and_new_default_on():
    a, b, c, d = (
        _docx("folderA", "s1"),
        _docx("folderA", "s2"),
        _docx("folderB", "s3"),
        _docx("folderB", "s4"),
    )
    prior = {a: False, b: True}  # a unchecked, b checked
    merged = [a, b, c, d]
    assert resolve_initial_selection(merged, prior) == {
        a: False,
        b: True,
        c: True,
        d: True,
    }


def test_prior_entries_absent_from_new_list_are_dropped():
    a, b = _docx("folderA", "s1"), _docx("folderA", "s2")
    prior = {a: False, b: True}
    # Only ``a`` is in the new list; ``b`` simply doesn't appear.
    assert resolve_initial_selection([a], prior) == {a: False}


# --------------------------------------------------------------------------
# compute_call_metrics (gauge / run-button / over-limit from CHECKED files)
# --------------------------------------------------------------------------
# Regression guard: after a reload preserves an unchecked oversized file, the
# gauge / Review-button / "too large" warning must reflect only the checked
# files — otherwise Review stays disabled (and keeps warning) about a file
# that won't be reviewed until the user toggles a box. exceeds_per_call_limit
# fires when overhead + tokens > 500_000.
_OVERHEAD = 1_000


def _fd(name: str, tokens: int) -> dict:
    return {"path": _docx("folderA", name), "filename": f"{name}.docx", "tokens": tokens}


def test_metrics_empty_selection_zeros_and_disables():
    assert compute_call_metrics([], _OVERHEAD) == CallMetrics(0, 0, False, [])


def test_metrics_all_under_limit():
    data = [_fd("a", 10_000), _fd("b", 50_000)]
    assert compute_call_metrics(data, _OVERHEAD) == CallMetrics(_OVERHEAD + 50_000, 2, False, [])


def test_metrics_flags_oversized_selected_file():
    data = [_fd("a", 10_000), _fd("big", 600_000)]
    m = compute_call_metrics(data, _OVERHEAD)
    assert m.per_file_limit_exceeded is True
    assert m.over_files == ["big.docx"]
    assert m.largest_call == _OVERHEAD + 600_000
    assert m.file_count == 2


def test_metrics_ignores_unchecked_oversized_file():
    # The feedback scenario: the oversized file is unchecked, so it isn't in
    # selected_data — Review must not stay disabled and nothing should warn.
    selected = [_fd("a", 10_000), _fd("b", 50_000)]  # big.docx (unchecked) omitted
    m = compute_call_metrics(selected, _OVERHEAD)
    assert m.per_file_limit_exceeded is False
    assert m.over_files == []
    assert m.file_count == 2


def test_metrics_lists_every_oversized_selected_file():
    data = [_fd("big1", 700_000), _fd("ok", 10_000), _fd("big2", 800_000)]
    m = compute_call_metrics(data, _OVERHEAD)
    assert m.per_file_limit_exceeded is True
    assert sorted(m.over_files) == ["big1.docx", "big2.docx"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
