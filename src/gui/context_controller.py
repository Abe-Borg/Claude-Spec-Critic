"""Project context text + attachment handling.

Project Context is a free-text user-supplied paragraph that ships with
every API call. This controller owns:

- the placeholder/focus toggle behavior on the inline textbox
- token-count refresh + warning thresholds on the textbox label
- ``.docx``/``.pdf`` attachment extraction (rejecting unsupported
  extensions, surfacing per-file errors via messagebox)
- the modal "Project Context" expand window

The widgets remain owned by ``SpecReviewApp``; this controller mutates
them through references on the app object.
"""
from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..input.extractor import CONTEXT_ATTACHMENT_EXTENSIONS, extract_context_text
from ..core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS
from .widgets import COLORS

_CONTEXT_PLACEHOLDER = "Describe your project (optional)"

_CONTEXT_FILETYPES = [
    ("Documents", "*.docx *.pdf"),
    ("Word Documents", "*.docx"),
    ("PDF Documents", "*.pdf"),
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
        from tiktoken import get_encoding
        enc = get_encoding("cl100k_base")
        app._project_context_tokens = len(enc.encode(ctx))
    else:
        app._project_context_tokens = 0
    update_context_token_label(app)
    if app._loaded_file_data:
        app._on_file_selection_change()


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
            errors.append(f"{path.name}: no extractable text (scanned PDF?)")
            continue
        sections.append(
            f"--- BEGIN ATTACHMENT: {path.name} ---\n{text}\n--- END ATTACHMENT: {path.name} ---"
        )
    return ("\n\n".join(sections), errors)


def attach_context_files(app, target_textbox=None) -> None:
    """Open a file picker, extract .docx/.pdf text, and append to the context.

    ``target_textbox`` lets the modal dialog reuse this flow against its
    own textbox; when None, the inline context textbox is updated.
    """
    files = filedialog.askopenfilenames(
        title="Attach project context documents",
        filetypes=_CONTEXT_FILETYPES,
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
        )
        paths = [p for p in paths if p not in unsupported]
    if not paths:
        return

    try:
        app.configure(cursor="watch")
        app.update_idletasks()
        combined, errors = extract_context_attachments(paths)
    finally:
        app.configure(cursor="")

    if errors:
        messagebox.showwarning(
            "Some attachments could not be read",
            "\n".join(errors),
        )
    if not combined:
        return

    if target_textbox is None:
        existing = get_project_context(app)
    else:
        existing = target_textbox.get("1.0", "end").strip()
    merged = f"{existing}\n\n{combined}" if existing else combined

    from tiktoken import get_encoding
    enc = get_encoding("cl100k_base")
    merged_tokens = len(enc.encode(merged))
    if merged_tokens > PROJECT_CONTEXT_MAX_TOKENS:
        messagebox.showerror(
            "Project Context too large",
            f"Attaching these file(s) would push Project Context to "
            f"{merged_tokens:,} tokens, exceeding the {PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
            f"Trim the existing context or attach smaller documents.",
        )
        return

    if target_textbox is None:
        set_context_text(app, merged)
    else:
        target_textbox.delete("1.0", "end")
        target_textbox.insert("1.0", merged)


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
            from tiktoken import get_encoding
            enc = get_encoding("cl100k_base")
            tokens = len(enc.encode(new_text))
            if tokens > PROJECT_CONTEXT_MAX_TOKENS:
                messagebox.showerror(
                    "Project Context too large",
                    f"Project Context is {tokens:,} tokens, exceeding the "
                    f"{PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
                    f"Trim the text before saving.",
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
        button_row, text="Save & Close", width=120, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=_save_and_close,
    ).pack(side="right")
