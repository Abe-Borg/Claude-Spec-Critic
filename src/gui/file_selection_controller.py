"""File selection, drag/drop parsing, and file-state preparation.

Owns the workflow that turns user file selections (browse dialog or
drop event) into the app's ``_selected_files`` list and triggers token
analysis. The actual ``FileListPanel`` widget remains owned by
``SpecReviewApp``; this controller just normalizes paths and notifies the
app of the new selection.

Selections **accumulate**: each Browse / drag-and-drop action unions its
files onto the existing selection (de-duped by resolved path) rather than
replacing it, so a user can load specs from more than one folder. The
native file dialog only multi-selects within a single folder, so
accumulation is the only way to span folders. The Clear button
(``clear_selection``) is the explicit reset.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from ..input.extractor import SUPPORTED_EXTENSIONS

_SPEC_FILETYPES = [
    ("Word Specifications", "*.docx"),
    ("All Files", "*.*"),
]


def is_supported_spec(filepath: Path) -> bool:
    return filepath.suffix.lower() in SUPPORTED_EXTENSIONS


def parse_dropped_paths(tk_root, payload: str) -> list[Path]:
    """Parse a drop-event payload into a list of Path objects.

    ``tk_root`` is needed to access ``Tk.splitlist`` for brace-quoted paths
    (the standard Tk format for paths with spaces). Falls back to shlex
    splitting and finally to a single path.
    """
    if not payload:
        return []
    raw_items: list[str] = []
    try:
        raw_items = list(tk_root.tk.splitlist(payload))
    except Exception:
        pass
    if not raw_items:
        try:
            raw_items = shlex.split(payload)
        except ValueError:
            raw_items = [payload]
    cleaned: list[Path] = []
    for item in raw_items:
        normalized = item.strip().strip("{}").strip("\"")
        if not normalized:
            continue
        cleaned.append(Path(normalized))
    return cleaned


def filter_supported_specs(candidate_paths: list[Path]) -> list[Path]:
    return [p for p in candidate_paths if is_supported_spec(p)]


def _dedup_key(path: Path) -> str:
    """Stable identity for de-duplication across folders.

    Resolves symlinks / ``..`` so the same file reached by two different
    path spellings collapses to one entry. Falls back to the raw string
    when resolution fails (e.g. a path that no longer exists on disk).
    """
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def merge_selected_specs(existing: list[Path], new_paths: list[Path]) -> list[Path]:
    """Union ``new_paths`` onto ``existing``, de-duped by resolved path.

    Order-preserving: existing files keep their position, genuinely-new
    files append in arrival order. This is what lets a user accumulate
    specs across several Browse / drag-and-drop actions from different
    folders instead of each action replacing the last.
    """
    merged = list(existing)
    seen = {_dedup_key(p) for p in existing}
    for p in new_paths:
        key = _dedup_key(p)
        if key in seen:
            continue
        seen.add(key)
        merged.append(p)
    return merged


def browse_for_specs(parent) -> list[Path]:
    """Open a file picker. Returns selected paths (possibly empty)."""
    # Imported lazily so the module (and its pure path-merge helpers) stays
    # importable in headless / hermetic test environments without tkinter.
    from tkinter import filedialog

    files = filedialog.askopenfilenames(
        title="Select specification files",
        filetypes=_SPEC_FILETYPES,
    )
    return [Path(f) for f in files] if files else []


def apply_selected_specs(app, candidate_paths: list[Path]) -> None:
    """Apply a list of candidate paths to the app, kicking off analysis.

    New selections **accumulate** onto any already-loaded files (de-duped
    by resolved path via ``merge_selected_specs``) so a user can load specs
    from more than one folder across multiple Browse / drag-and-drop
    actions. Re-selecting files already in the set is a no-op (no redundant
    re-analysis). Use the Clear button (``clear_selection``) to reset.
    """
    paths = filter_supported_specs(candidate_paths)
    if not paths:
        app.log.log_warning("No supported .docx files selected")
        return
    existing = list(getattr(app, "_selected_files", None) or [])
    merged = merge_selected_specs(existing, paths)
    added = len(merged) - len(existing)
    if added == 0:
        # Everything dropped/browsed is already loaded — skip the wipe +
        # re-extract + re-count churn that re-analysis would trigger.
        app.log.log_warning("No new files added — already in the selection")
        return
    if existing:
        app.log.log_step(f"Added {added} file(s) — {len(merged)} total")
    app._selected_files = merged
    app.input_dir = merged[0].parent
    app.input_dir_entry.delete(0, "end")
    app.input_dir_entry.insert(
        0,
        str(merged[0]) if len(merged) == 1 else f"{len(merged)} files selected",
    )
    app._analyze_tokens(merged)


def clear_file_state(app) -> None:
    app._loaded_file_data = []
    app._extracted_specs = []
    app.file_list_panel.reset()
    app.token_gauge.reset()
    app.run_button.configure(state="disabled")


def clear_selection(app) -> None:
    """Full reset for the Clear button.

    Drops every loaded file, the input-path text, and the analyzed file
    state. Distinct from ``clear_file_state`` (the *transient* clear run at
    the start of each re-analysis), which intentionally leaves
    ``_selected_files`` intact so accumulation survives re-analysis.

    Bumps ``_analysis_epoch`` and cancels the pending exact-token debounce
    so an in-flight background analysis can neither repopulate the
    just-cleared panel (epoch-guarded dispatches drop) nor fire a now-
    pointless ``count_tokens`` API call.
    """
    app._analysis_epoch = getattr(app, "_analysis_epoch", 0) + 1
    timer_id = getattr(app, "_exact_token_refresh_timer_id", None)
    if timer_id is not None:
        try:
            app.after_cancel(timer_id)
        except Exception:
            pass
        app._exact_token_refresh_timer_id = None
    had_files = bool(getattr(app, "_selected_files", None))
    app._selected_files = []
    app.input_dir = None
    app.input_dir_entry.delete(0, "end")
    clear_file_state(app)
    if had_files:
        app.log.log_step("Cleared file selection")


def set_file_data(app, file_data, extracted_specs, sys_tokens, ctx_tokens) -> None:
    app._loaded_file_data = file_data
    app._extracted_specs = extracted_specs
    app._system_prompt_tokens = sys_tokens
    app._project_context_tokens = ctx_tokens
