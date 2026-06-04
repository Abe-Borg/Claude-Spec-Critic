"""Unique-basename guard for multi-folder accumulation.

The review/report pipeline keys several stages by ``spec.filename`` (bare
basename): deterministic pre-screen alerts, the review-repair lookup,
failed-spec filtering, and report / edit-sidecar grouping. That is unique
within a single folder but not across the folders accumulation can now span,
so two same-basename specs (e.g. ``230500.docx`` from two project folders)
would mis-attribute or collapse. ``filter_name_collisions`` rejects such a
candidate at the input boundary, preserving the invariant.

Hermetic: ``file_selection_controller`` imports ``filedialog`` lazily and the
package ``__init__`` re-exports ``main`` lazily, so this loads without
``tkinter`` / ``customtkinter``. A minimal ``FakeApp`` stands in for the real
app for the ``apply_selected_specs`` integration checks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.gui.file_selection_controller import (
    apply_selected_specs,
    filter_name_collisions,
)


def _docx(folder: str, name: str) -> Path:
    return Path(f"/{folder}/{name}.docx")


# --------------------------------------------------------------------------
# filter_name_collisions (pure)
# --------------------------------------------------------------------------
def test_accepts_unique_basenames():
    new = [_docx("folderA", "s1"), _docx("folderB", "s2")]
    accepted, rejected = filter_name_collisions([], new)
    assert accepted == new
    assert rejected == []


def test_rejects_basename_collision_with_existing():
    existing = [_docx("folderA", "230500")]
    new = [_docx("folderB", "230500")]  # different file, same basename
    accepted, rejected = filter_name_collisions(existing, new)
    assert accepted == []
    assert rejected == [_docx("folderB", "230500")]


def test_rejects_collision_within_one_batch_keeping_first():
    new = [_docx("folderA", "230500"), _docx("folderB", "230500")]
    accepted, rejected = filter_name_collisions([], new)
    assert accepted == [_docx("folderA", "230500")]
    assert rejected == [_docx("folderB", "230500")]


def test_exact_path_readd_is_silent_not_a_collision():
    existing = [_docx("folderA", "x")]
    accepted, rejected = filter_name_collisions(existing, [_docx("folderA", "x")])
    # Same resolved path: dropped silently, NOT reported as a name collision.
    assert accepted == []
    assert rejected == []


def test_mixed_exact_dup_collision_and_new():
    existing = [_docx("folderA", "x")]
    new = [
        _docx("folderB", "x"),   # collision (different folder, same basename)
        _docx("folderB", "y"),   # genuinely new
        _docx("folderA", "x"),   # exact dup of existing -> silent
    ]
    accepted, rejected = filter_name_collisions(existing, new)
    assert accepted == [_docx("folderB", "y")]
    assert rejected == [_docx("folderB", "x")]


def test_does_not_mutate_inputs():
    existing = [_docx("folderA", "x")]
    new = [_docx("folderB", "x")]
    filter_name_collisions(existing, new)
    assert existing == [_docx("folderA", "x")]
    assert new == [_docx("folderB", "x")]


# --------------------------------------------------------------------------
# apply_selected_specs integration
# --------------------------------------------------------------------------
class _FakeLog:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.steps: list[str] = []

    def log_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def log_step(self, msg: str) -> None:
        self.steps.append(msg)


class _FakeEntry:
    def __init__(self) -> None:
        self.text = ""

    def delete(self, start, end) -> None:
        self.text = ""

    def insert(self, index, value) -> None:
        self.text = value


class _FakeApp:
    def __init__(self) -> None:
        self.log = _FakeLog()
        self.input_dir_entry = _FakeEntry()
        self.input_dir = None
        self._selected_files: list[Path] = []
        self.analyzed_with: list[list[Path]] = []

    def _analyze_tokens(self, paths) -> None:
        self.analyzed_with.append(list(paths))


def _warned_collision(app) -> bool:
    return any("already loaded" in w for w in app.log.warnings)


def test_collision_from_second_folder_warns_and_skips():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "230500")])
    analyses_before = len(app.analyzed_with)

    apply_selected_specs(app, [_docx("folderB", "230500")])

    # The colliding file is not loaded, and no re-analysis runs for it.
    assert app._selected_files == [_docx("folderA", "230500")]
    assert len(app.analyzed_with) == analyses_before
    assert _warned_collision(app)
    assert "230500.docx" in app.log.warnings[-1]


def test_collision_within_single_drop_keeps_first():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "230500"), _docx("folderB", "230500")])
    assert app._selected_files == [_docx("folderA", "230500")]
    assert _warned_collision(app)


def test_unique_addition_still_accumulates():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "230500")])
    apply_selected_specs(app, [_docx("folderB", "260500")])
    assert app._selected_files == [_docx("folderA", "230500"), _docx("folderB", "260500")]
    assert app.analyzed_with[-1] == [_docx("folderA", "230500"), _docx("folderB", "260500")]
    assert not _warned_collision(app)


def test_partial_collision_adds_only_noncolliding():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "230500"), _docx("folderA", "260500")])
    apply_selected_specs(
        app,
        [_docx("folderB", "230500"), _docx("folderB", "270500")],  # 230500 collides
    )
    assert app._selected_files == [
        _docx("folderA", "230500"),
        _docx("folderA", "260500"),
        _docx("folderB", "270500"),
    ]
    assert app.analyzed_with[-1] == app._selected_files
    assert _warned_collision(app)
    # The warning names the collided file, not the accepted one.
    assert "230500.docx" in app.log.warnings[-1]
    assert "270500.docx" not in app.log.warnings[-1]


def test_only_collisions_does_not_emit_no_new_files_message():
    app = _FakeApp()
    apply_selected_specs(app, [_docx("folderA", "230500")])
    apply_selected_specs(app, [_docx("folderB", "230500")])
    # The collision warning explains it; the generic "No new files" message
    # would be redundant/misleading, so it must not appear.
    assert not any("No new files added" in w for w in app.log.warnings)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
