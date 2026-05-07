"""File selection, drag/drop parsing, and file-state preparation.

Owns the workflow that turns user file selections (browse dialog or
drop event) into the app's ``_selected_files`` list and triggers token
analysis. The actual ``FileListPanel`` widget remains owned by
``SpecReviewApp``; this controller just normalizes paths and notifies the
app of the new selection.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from tkinter import filedialog

from .extractor import SUPPORTED_EXTENSIONS

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


def browse_for_specs(parent) -> list[Path]:
    """Open a file picker. Returns selected paths (possibly empty)."""
    files = filedialog.askopenfilenames(
        title="Select specification files",
        filetypes=_SPEC_FILETYPES,
    )
    return [Path(f) for f in files] if files else []


def apply_selected_specs(app, candidate_paths: list[Path]) -> None:
    """Apply a list of candidate paths to the app, kicking off analysis."""
    paths = filter_supported_specs(candidate_paths)
    if not paths:
        app.log.log_warning("No supported .docx files selected")
        return
    app._selected_files = paths
    app.input_dir = paths[0].parent
    app.input_dir_entry.delete(0, "end")
    app.input_dir_entry.insert(
        0,
        str(paths[0]) if len(paths) == 1 else f"{len(paths)} files selected",
    )
    app._analyze_tokens(paths)


def clear_file_state(app) -> None:
    app._loaded_file_data = []
    app._extracted_specs = []
    app.file_list_panel.reset()
    app.token_gauge.reset()
    app.run_button.configure(state="disabled")


def set_file_data(app, file_data, extracted_specs, sys_tokens, ctx_tokens) -> None:
    app._loaded_file_data = file_data
    app._extracted_specs = extracted_specs
    app._system_prompt_tokens = sys_tokens
    app._project_context_tokens = ctx_tokens
