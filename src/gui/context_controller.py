"""Project context text + attachment handling.

Project Context is a free-text user-supplied paragraph that ships with
every API call. This controller owns:

- the placeholder/focus toggle behavior on the inline textbox
- token-count refresh + warning thresholds on the textbox label
- ``.docx``/``.pdf`` attachment extraction (rejecting unsupported
  extensions, surfacing per-file errors via messagebox)
- the "Attach Drawings…" vision-digest flow (drawing PDFs -> one-time
  digest call -> editable text merged into the context textbox)
- the modal "Project Context" expand window

The widgets remain owned by ``SpecReviewApp``; this controller mutates
them through references on the app object.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..input.extractor import CONTEXT_ATTACHMENT_EXTENSIONS, extract_context_text
from ..input.drawing_digest import (
    DrawingDigestError,
    DRAWING_DIGEST_MODEL_DEFAULT,
    build_digest_chunks,
    format_digest_confirm_message,
    preflight_digest_cost,
    run_drawing_digest,
    validate_drawing_files,
    wrapped_digest_block,
)
from ..core.tokenizer import count_tokens, PROJECT_CONTEXT_MAX_TOKENS
from ..modules import get_module
from .context_attachment import (
    context_has_drawing_digest,
    context_within_token_cap,
    digested_drawing_filenames,
    drawing_filenames_with_failed_chunks,
    merge_into_context,
    wrap_attachment,
)
from .widgets import COLORS

_CONTEXT_PLACEHOLDER = "Describe your project (optional)"

_CONTEXT_FILETYPES = [
    ("Documents", "*.docx *.pdf *.md *.txt"),
    ("Word Documents", "*.docx"),
    ("PDF Documents", "*.pdf"),
    ("Markdown / Text", "*.md *.txt"),
    ("All Files", "*.*"),
]

_DRAWING_FILETYPES = [
    ("PDF Drawings", "*.pdf"),
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
    """Clear the FILES-panel drawing readout if the digest is no longer present.

    The operator can delete the merged digest straight out of the Project
    Context textbox; when that happens the read-only readout must not linger.
    A no-op when no drawings are tracked or the digest block is still present.
    """
    if not getattr(app, "_attached_drawings", None):
        return
    if context_has_drawing_digest(ctx):
        return
    app._attached_drawings = []
    panel = getattr(app, "file_list_panel", None)
    if panel is not None:
        try:
            panel.set_drawings([])
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
                f"{path.name}: no extractable text (scanned PDF?). If this "
                "is a drawing set, use 'Attach Drawings…' to analyze it "
                "with vision."
            )
            continue
        sections.append(wrap_attachment(path.name, text))
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
    merged = merge_into_context(existing, combined)

    merged_tokens, fits = context_within_token_cap(merged)
    if not fits:
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


def attach_drawing_files(app) -> None:
    """Pick drawing PDFs, run the one-time vision digest, merge the text.

    The digest is an API spend, so the flow is deliberately staged: fast
    local validation + chunk packing under the watch cursor, then a
    background ``count_tokens`` preflight feeding a cost-confirm dialog,
    and only then the digest call itself on a second background thread.
    All tkinter mutation is marshaled back to the main thread via
    ``app.after(0, ...)``; a running flag + button disable prevents
    concurrent digests. A review started mid-digest is safe — the review
    snapshots Project Context at submit time.
    """
    if getattr(app, "_drawing_digest_running", False):
        return
    if getattr(app, "is_processing", False):
        messagebox.showwarning(
            "Review in progress",
            "Wait for the current review to finish before analyzing drawings.",
        )
        return
    api_key = app.api_key_entry.get().strip()
    if not api_key:
        messagebox.showerror(
            "API key required",
            "Analyzing drawings calls the Anthropic API — enter your API key first.",
        )
        return
    os.environ["ANTHROPIC_API_KEY"] = api_key

    files = filedialog.askopenfilenames(
        title="Attach construction drawings (PDF)",
        filetypes=_DRAWING_FILETYPES,
    )
    if not files:
        return
    paths = [Path(f) for f in files]

    try:
        app.configure(cursor="watch")
        app.update_idletasks()
        drawing_files, errors = validate_drawing_files(paths)
        chunks = (
            build_digest_chunks(drawing_files, model=DRAWING_DIGEST_MODEL_DEFAULT)
            if drawing_files
            else []
        )
    finally:
        app.configure(cursor="")

    if errors:
        messagebox.showwarning(
            "Some drawings could not be used",
            "\n".join(errors),
        )
    if not chunks:
        return

    module = get_module(getattr(app, "_selected_module_id", None))
    module_display_name = module.display_name

    app._drawing_digest_running = True
    _set_drawings_button_state(app, "disabled")

    def _log(msg: str, level: str = "info", **_kwargs) -> None:
        if hasattr(app, "log"):
            app.after(0, lambda m=msg, l=level: app.log.log(m, level=l))

    def _reset() -> None:
        app._drawing_digest_running = False
        _set_drawings_button_state(app, "normal")

    def _preflight_worker() -> None:
        try:
            preflight = preflight_digest_cost(
                chunks,
                model=DRAWING_DIGEST_MODEL_DEFAULT,
                module_display_name=module_display_name,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the operator
            # Bind via default arg: Python clears ``exc`` when the except
            # block exits, so a plain closure would NameError when the Tk
            # callback fires later — and the reset/error path would never run.
            app.after(0, lambda e=exc: _on_preflight_failed(e))
            return
        app.after(0, lambda: _on_preflight_done(preflight))

    def _on_preflight_failed(exc: Exception) -> None:
        _reset()
        messagebox.showerror(
            "Drawing analysis failed",
            f"Could not estimate the drawing set's size: {exc}",
        )

    def _on_preflight_done(preflight) -> None:
        if preflight.over_window_chunk_indices:
            _reset()
            bad = ", ".join(
                str(i + 1) for i in preflight.over_window_chunk_indices
            )
            messagebox.showerror(
                "Drawing set too dense",
                f"Request(s) {bad} exceed the model's context window even "
                "as a single chunk. Split the densest PDFs into smaller "
                "files and re-attach.",
            )
            return
        proceed = messagebox.askyesno(
            "Analyze drawings?",
            format_digest_confirm_message(
                preflight, chunks=chunks, model=DRAWING_DIGEST_MODEL_DEFAULT
            ),
        )
        if not proceed:
            _reset()
            return
        _log(
            f"Analyzing {sum(len(c.parts) for c in chunks)} drawing "
            f"document(s) across {len(chunks)} request(s)...",
            level="step",
        )
        threading.Thread(target=_digest_worker, daemon=True).start()

    def _digest_worker() -> None:
        try:
            result = run_drawing_digest(
                chunks,
                model=DRAWING_DIGEST_MODEL_DEFAULT,
                module_display_name=module_display_name,
                log=_log,
            )
        except DrawingDigestError as exc:
            # Default-arg binding, same reason as the preflight worker.
            app.after(0, lambda msg=str(exc): _on_digest_failed(msg))
            return
        except Exception as exc:  # noqa: BLE001 — surfaced to the operator
            app.after(
                0,
                lambda msg=f"{type(exc).__name__}: {exc}": _on_digest_failed(msg),
            )
            return
        app.after(0, lambda: _on_digest_done(result))

    def _on_digest_failed(error: str) -> None:
        _reset()
        messagebox.showerror("Drawing analysis failed", error)

    def _on_digest_done(result) -> None:
        _reset()
        wrapped = wrapped_digest_block(result)
        merged = merge_into_context(get_project_context(app), wrapped)
        merged_tokens, fits = context_within_token_cap(merged)
        if not fits:
            digest_tokens = count_tokens(wrapped)
            messagebox.showerror(
                "Digest too large for Project Context",
                f"The drawing digest is {digest_tokens:,} tokens and would "
                f"push Project Context to {merged_tokens:,} tokens, over "
                f"the {PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
                "Attach fewer sheets, split the set into smaller runs, or "
                "trim the existing context, then try again.",
            )
            return
        set_context_text(app, merged)
        _surface_attached_drawings(app, drawing_files, result)
        if result.failed_chunks:
            failed = "\n".join(
                f"- request {s.chunk_index + 1}: {', '.join(s.file_labels)} — {s.error}"
                for s in result.chunk_statuses
                if s.status == "failed"
            )
            messagebox.showwarning(
                "Digest completed partially",
                "Some drawing requests failed; their sheets are NOT in the "
                f"digest:\n{failed}",
            )
        cost = result.actual_cost_usd()
        cost_text = f" (~${cost:,.2f})" if cost is not None else ""
        _log(
            f"Drawing digest complete: {result.completed_chunks}/"
            f"{len(result.chunk_statuses)} request(s), "
            f"{result.total_input_tokens:,} in / "
            f"{result.total_output_tokens:,} out tokens{cost_text}. "
            "Digest added to Project Context — review and edit it before "
            "running a review.",
            level="success",
        )

    threading.Thread(target=_preflight_worker, daemon=True).start()


def _surface_attached_drawings(app, drawing_files, result) -> None:
    """Show the just-digested drawings in the FILES panel (read-only).

    Gives the operator visible confirmation that the drawings were uploaded,
    beyond the activity-log line. Accumulates across repeated "Attach
    Drawings…" actions (de-duped by filename), mirroring how each digest is
    appended to Project Context. Only the sheets that landed in a non-failed
    chunk are listed (``digested_drawing_filenames``); failed sheets are named
    in the separate partial-failure warning instead. The page count is dropped
    for a file that was split across chunks and only partially digested (a
    failed range), since its *full* page count would overstate what actually
    reached Project Context.
    """
    panel = getattr(app, "file_list_panel", None)
    if panel is None:
        return
    pages_by_name = {f.name: f.page_count for f in drawing_files}
    partial = drawing_filenames_with_failed_chunks(result.chunk_statuses)
    attached = list(getattr(app, "_attached_drawings", []))
    seen = {d["name"] for d in attached}
    for name in digested_drawing_filenames(result.chunk_statuses):
        if name in seen:
            continue
        seen.add(name)
        page_count = pages_by_name.get(name)
        show_pages = isinstance(page_count, int) and name not in partial
        pages = f"{page_count} pp." if show_pages else ""
        attached.append({"name": name, "pages": pages})
    app._attached_drawings = attached
    try:
        panel.set_drawings(attached)
    except Exception:  # noqa: BLE001 — a panel render must never break attach
        pass


def _set_drawings_button_state(app, state: str) -> None:
    button = getattr(app, "attach_drawings_button", None)
    if button is not None:
        try:
            button.configure(state=state)
        except Exception:  # noqa: BLE001 — widget teardown must never raise
            pass


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
