"""Project context text + attachment handling.

Project Context is a free-text user-supplied paragraph that ships with
every API call. This controller owns:

- the placeholder/focus toggle behavior on the inline textbox
- token-count refresh + warning thresholds on the textbox label
- ``.docx``/``.pdf`` attachment extraction (rejecting unsupported
  extensions, surfacing per-file errors via messagebox)
- the "Attach Drawing Analysis…" flow: the text output of the operator's
  separate drawing-analyzer program, read and token-counted locally, then
  merged into the context textbox as the drawing-digest block (no API
  call, no drawing file upload — Spec Critic does not read drawings)
- the FILES-panel readout of the drawing analyses currently in Project
  Context, with their live token counts
- the modal "Project Context" expand window

The widgets remain owned by ``SpecReviewApp``; this controller mutates
them through references on the app object.

Threading: attachment extraction (``.docx``/``.pdf`` text) and the drawing
analysis read + token count both run on daemon worker threads so the window
never freezes under a watch cursor; every tkinter mutation — cursor restore,
warnings, the token-cap refusal, the textbox merge — is marshaled back with
``app.after(0, ...)``. One running flag covers both flows (they write the
same textbox) and refuses a second concurrent start.
"""
from __future__ import annotations

import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..input.extractor import CONTEXT_ATTACHMENT_EXTENSIONS, extract_context_text
from ..input.drawing_analysis import (
    DRAWING_ANALYSIS_EXTENSIONS,
    load_drawing_analyses,
    wrapped_drawing_analysis_block,
)
from ..core.tokenizer import count_tokens, PROJECT_CONTEXT_MAX_TOKENS
from .context_attachment import (
    context_within_token_cap,
    drawing_analysis_readout,
    merge_into_context,
    wrap_attachment,
)
from .dialog_owner import owner_window
from .widgets import COLORS

_CONTEXT_PLACEHOLDER = "Describe your project (optional)"

_CONTEXT_FILETYPES = [
    ("Documents", "*.docx *.pdf *.md *.txt"),
    ("Word Documents", "*.docx"),
    ("PDF Documents", "*.pdf"),
    ("Markdown / Text", "*.md *.txt"),
    ("All Files", "*.*"),
]

_DRAWING_ANALYSIS_FILETYPES = [
    (
        "Drawing analysis output",
        " ".join(f"*{ext}" for ext in sorted(DRAWING_ANALYSIS_EXTENSIONS)),
    ),
    ("All Files", "*.*"),
]


def context_focus_in(app, event=None) -> None:
    if app._context_has_placeholder:
        app.context_textbox.delete("1.0", "end")
        app.context_textbox.configure(text_color=COLORS["text_primary"])
        app._context_has_placeholder = False


def context_focus_out(app, event=None) -> None:
    text = app.context_textbox.get("1.0", "end").strip()
    if not text:
        app._context_has_placeholder = True
        app.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
        app.context_textbox.configure(text_color=COLORS["text_muted"])
        on_context_change(app)


def get_project_context(app) -> str:
    if app._context_has_placeholder:
        return ""
    return app.context_textbox.get("1.0", "end").strip()


def on_context_change(app, event=None) -> None:
    if app._context_debounce_id is not None:
        app.after_cancel(app._context_debounce_id)
    app._context_debounce_id = app.after(300, lambda: do_context_change(app))


def do_context_change(app) -> None:
    app._context_debounce_id = None
    ctx = get_project_context(app)
    if ctx:
        app._project_context_tokens = count_tokens(ctx)
    else:
        app._project_context_tokens = 0
    _sync_drawings_readout(app, ctx)
    update_context_token_label(app)
    if app._loaded_file_data:
        app._on_file_selection_change()


def _sync_drawings_readout(app, ctx: str) -> None:
    """Keep the FILES-panel drawing-analysis readout equal to the textbox.

    The readout is derived from the Project Context text itself: one row per
    ``Construction Drawing Digest`` block with its current token count. The
    operator can edit or delete a merged analysis straight out of the textbox;
    after the edit settles, a deleted block's row disappears and a trimmed
    block's count shrinks. Unchanged blocks are not re-counted (a per-app
    memo keyed by block text), and the panel is only re-rendered when the
    rows actually changed, so typing elsewhere in the textbox costs nothing.
    """
    memo = getattr(app, "_drawing_analysis_token_memo", None)
    if memo is None:
        memo = {}
        app._drawing_analysis_token_memo = memo
    rows = drawing_analysis_readout(ctx, token_memo=memo)
    if rows == getattr(app, "_drawing_analyses", []):
        return
    app._drawing_analyses = rows
    panel = getattr(app, "file_list_panel", None)
    if panel is not None:
        try:
            panel.set_drawings(rows)
        except Exception:  # noqa: BLE001 — a panel render must never break input
            pass


def update_context_token_label(app) -> None:
    tokens = app._project_context_tokens
    over = tokens > PROJECT_CONTEXT_MAX_TOKENS
    text = f"{tokens:,} / {PROJECT_CONTEXT_MAX_TOKENS:,} tokens"
    if over:
        text += " — exceeds limit"
        color = COLORS["error"]
    elif tokens > int(PROJECT_CONTEXT_MAX_TOKENS * 0.9):
        color = COLORS["warning"]
    else:
        color = COLORS["text_muted"]
    if hasattr(app, "context_token_label"):
        app.context_token_label.configure(text=text, text_color=color)


def set_context_text(app, new_text: str) -> None:
    """Replace the context textbox contents, restoring placeholder when empty."""
    app.context_textbox.delete("1.0", "end")
    if new_text:
        app._context_has_placeholder = False
        app.context_textbox.configure(text_color=COLORS["text_primary"])
        app.context_textbox.insert("1.0", new_text)
    else:
        app._context_has_placeholder = True
        app.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
        app.context_textbox.configure(text_color=COLORS["text_muted"])
    on_context_change(app)


def extract_context_attachments(paths: list[Path]) -> tuple[str, list[str]]:
    """Extract text from .docx/.pdf attachments. Returns (combined_text, errors)."""
    sections: list[str] = []
    errors: list[str] = []
    for path in paths:
        try:
            text = extract_context_text(path).strip()
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if not text:
            errors.append(
                f"{path.name}: no extractable text (scanned PDF?). Spec "
                "Critic does not read drawings or images; attach the text "
                "output of your drawing analyzer with 'Attach Drawing "
                "Analysis…' instead."
            )
            continue
        sections.append(wrap_attachment(path.name, text))
    return ("\n\n".join(sections), errors)


def _set_cursor(app, cursor: str) -> None:
    try:
        app.configure(cursor=cursor)
    except Exception:  # noqa: BLE001 — a cursor is cosmetic; never break the flow
        pass


def attach_context_files(app, target_textbox=None) -> None:
    """Open a file picker, extract .docx/.pdf text, and append to the context.

    ``target_textbox`` lets the modal dialog reuse this flow against its
    own textbox; when None, the inline context textbox is updated. The
    extraction runs on a worker thread (a large PDF can take seconds);
    the merge, the token-cap refusal, and every dialog run on the Tk
    thread afterwards, owned by the window the flow started from.
    """
    owner = owner_window(app, target_textbox)
    if getattr(app, "_context_extraction_running", False):
        messagebox.showinfo(
            "Attachment in progress",
            "The previous attachment is still being read \u2014 wait for it "
            "to finish before attaching more files.",
            parent=owner,
        )
        return
    files = filedialog.askopenfilenames(
        title="Attach project context documents",
        filetypes=_CONTEXT_FILETYPES,
        parent=owner,
    )
    if not files:
        return
    paths = [Path(f) for f in files]
    unsupported = [p for p in paths if p.suffix.lower() not in CONTEXT_ATTACHMENT_EXTENSIONS]
    if unsupported:
        messagebox.showwarning(
            "Unsupported files",
            "Only .docx and .pdf files can be attached. Skipping:\n"
            + "\n".join(p.name for p in unsupported),
            parent=owner,
        )
        paths = [p for p in paths if p not in unsupported]
    if not paths:
        return

    app._context_extraction_running = True
    _set_cursor(app, "watch")

    def _finish() -> None:
        app._context_extraction_running = False
        _set_cursor(app, "")

    def _worker() -> None:
        try:
            combined, errors = extract_context_attachments(paths)
        except Exception as exc:  # noqa: BLE001 — surfaced on the Tk thread
            # Default-arg binding: ``exc`` is cleared when the except block
            # exits, so a plain closure would NameError when the callback fires.
            app.after(0, lambda e=exc: _on_failed(e))
            return
        app.after(0, lambda c=combined, e=errors: _on_extracted(c, e))

    def _on_failed(exc: BaseException) -> None:
        _finish()
        messagebox.showerror(
            "Attachment failed",
            f"Could not read the attachment(s): {exc}",
            parent=owner,
        )

    def _on_extracted(combined: str, errors: list[str]) -> None:
        _finish()
        if errors:
            messagebox.showwarning(
                "Some attachments could not be read",
                "\n".join(errors),
                parent=owner,
            )
        if not combined:
            return

        try:
            if target_textbox is None:
                existing = get_project_context(app)
            else:
                existing = target_textbox.get("1.0", "end").strip()
        except Exception:  # noqa: BLE001 — the modal was closed mid-extraction
            if hasattr(app, "log"):
                app.log.log_warning(
                    "The Project Context window was closed before the "
                    "attachment finished; nothing was added."
                )
            return
        merged = merge_into_context(existing, combined)

        merged_tokens, fits = context_within_token_cap(merged)
        if not fits:
            messagebox.showerror(
                "Project Context too large",
                f"Attaching these file(s) would push Project Context to "
                f"{merged_tokens:,} tokens, exceeding the {PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
                f"Trim the existing context or attach smaller documents.",
                parent=owner,
            )
            return

        if target_textbox is None:
            set_context_text(app, merged)
        else:
            target_textbox.delete("1.0", "end")
            target_textbox.insert("1.0", merged)

    threading.Thread(
        target=_worker, name="spec-critic-context-attach", daemon=True
    ).start()


def attach_drawing_analysis(app, target_textbox=None) -> None:
    """Attach the text output of the operator's drawing-analyzer program.

    Spec Critic does not read drawings, and this flow makes no API call and
    uploads no drawing file: the analyzer's output file is read verbatim and
    token-counted on a worker thread (the local tokenizer — the same count
    the Project Context label shows), wrapped as the ``Construction Drawing
    Digest`` block with its file name, and merged into Project Context on the
    Tk thread. The count is shown in the activity log and, once the text is
    in the main textbox, in the FILES-panel readout. A merge that would exceed
    the Project Context cap is refused with the counts, never truncated.

    ``target_textbox`` lets the modal editor reuse the flow against its own
    textbox (the readout then updates on Save & Close). Shares the
    attach-files running flag, since both flows write the same textbox.
    """
    owner = owner_window(app, target_textbox)
    if getattr(app, "_context_extraction_running", False):
        messagebox.showinfo(
            "Attachment in progress",
            "The previous attachment is still being read \u2014 wait for it "
            "to finish before attaching more files.",
            parent=owner,
        )
        return
    files = filedialog.askopenfilenames(
        title="Attach drawing analysis output (text)",
        filetypes=_DRAWING_ANALYSIS_FILETYPES,
        parent=owner,
    )
    if not files:
        return
    paths = [Path(f) for f in files]

    app._context_extraction_running = True
    _set_cursor(app, "watch")

    def _log(msg: str, level: str = "info") -> None:
        if hasattr(app, "log"):
            app.log.log(msg, level=level)

    def _finish() -> None:
        app._context_extraction_running = False
        _set_cursor(app, "")

    def _worker() -> None:
        try:
            analyses, errors = load_drawing_analyses(paths)
        except Exception as exc:  # noqa: BLE001 — surfaced on the Tk thread
            # Default-arg binding: ``exc`` is cleared when the except block
            # exits, so a plain closure would NameError when the callback fires.
            app.after(0, lambda e=exc: _on_failed(e))
            return
        app.after(0, lambda a=analyses, e=errors: _on_loaded(a, e))

    def _on_failed(exc: BaseException) -> None:
        _finish()
        messagebox.showerror(
            "Drawing analysis failed",
            f"Could not read the drawing analysis file(s): {exc}",
            parent=owner,
        )

    def _on_loaded(analyses: list, errors: list[str]) -> None:
        _finish()
        if errors:
            messagebox.showwarning(
                "Some drawing analysis files could not be used",
                "\n".join(errors),
                parent=owner,
            )
        if not analyses:
            return

        try:
            if target_textbox is None:
                existing = get_project_context(app)
            else:
                existing = target_textbox.get("1.0", "end").strip()
        except Exception:  # noqa: BLE001 — the modal was closed mid-read
            _log(
                "The Project Context window was closed before the drawing "
                "analysis finished loading; nothing was added.",
                level="warning",
            )
            return
        addition = "\n\n".join(wrapped_drawing_analysis_block(a) for a in analyses)
        merged = merge_into_context(existing, addition)

        merged_tokens, fits = context_within_token_cap(merged)
        if not fits:
            named = "; ".join(f"{a.name} is {a.tokens:,} tokens" for a in analyses)
            messagebox.showerror(
                "Drawing analysis too large for Project Context",
                f"{named}. Attaching would push Project Context to "
                f"{merged_tokens:,} tokens, over the "
                f"{PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
                "Trim the analysis output, attach fewer files, or trim the "
                "existing context, then try again.",
                parent=owner,
            )
            return

        if target_textbox is None:
            set_context_text(app, merged)
            # The debounced change handler syncs the readout too; do it now
            # so the panel shows the new row (and its count) immediately.
            _sync_drawings_readout(app, merged)
        else:
            target_textbox.delete("1.0", "end")
            target_textbox.insert("1.0", merged)

        for analysis in analyses:
            _log(
                f"Drawing analysis attached: {analysis.name} \u2014 "
                f"{analysis.tokens:,} tokens (local estimate).",
                level="success",
            )
        where = (
            "Project Context"
            if target_textbox is None
            else "the Project Context editor (Save & Close to apply)"
        )
        _log(
            f"{where} is now {merged_tokens:,} / {PROJECT_CONTEXT_MAX_TOKENS:,} "
            "tokens. The analysis is sent as text with every review, "
            "cross-check, and compliance call \u2014 review and edit it before "
            "running a review."
        )

    threading.Thread(
        target=_worker, name="spec-critic-drawing-analysis", daemon=True
    ).start()


def open_context_modal(app) -> None:
    dialog = ctk.CTkToplevel(app)
    dialog.title("Project Context")
    dialog.geometry("700x500")
    dialog.configure(fg_color=COLORS["bg_dark"])
    dialog.resizable(True, True)
    dialog.minsize(400, 300)
    dialog.transient(app)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_force()

    outer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
    outer.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        outer, text="Project Context",
        font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=16, pady=(16, 8))

    modal_textbox = ctk.CTkTextbox(
        outer, fg_color=COLORS["bg_input"], border_color=COLORS["border"],
        border_width=2, text_color=COLORS["text_primary"],
        font=ctk.CTkFont(family="Consolas", size=13), wrap="word",
    )
    modal_textbox.pack(fill="both", expand=True, padx=16, pady=(0, 8))

    current = get_project_context(app)
    if current:
        modal_textbox.insert("1.0", current)

    def _save_and_close():
        new_text = modal_textbox.get("1.0", "end").strip()
        if new_text:
            tokens = count_tokens(new_text)
            if tokens > PROJECT_CONTEXT_MAX_TOKENS:
                messagebox.showerror(
                    "Project Context too large",
                    f"Project Context is {tokens:,} tokens, exceeding the "
                    f"{PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
                    f"Trim the text before saving.",
                    parent=dialog,
                )
                return
        set_context_text(app, new_text)
        dialog.destroy()

    button_row = ctk.CTkFrame(outer, fg_color="transparent")
    button_row.pack(fill="x", padx=16, pady=(0, 16))
    ctk.CTkButton(
        button_row, text="Attach Files…", width=120, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
        border_width=1, border_color=COLORS["border"],
        text_color=COLORS["text_secondary"],
        command=lambda: attach_context_files(app, target_textbox=modal_textbox),
    ).pack(side="left")
    ctk.CTkButton(
        button_row, text="Attach Drawing Analysis…", width=180, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
        border_width=1, border_color=COLORS["border"],
        text_color=COLORS["text_secondary"],
        command=lambda: attach_drawing_analysis(app, target_textbox=modal_textbox),
    ).pack(side="left", padx=(8, 0))
    ctk.CTkButton(
        button_row, text="Save & Close", width=120, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=_save_and_close,
    ).pack(side="right")
