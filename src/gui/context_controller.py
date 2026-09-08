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

Threading: attachment extraction (``.docx``/``.pdf`` text) and the drawing
set's validate + chunk-pack step (pypdf reads every page tree, rewrites
oversized files by page range) both run on daemon worker threads so the
window never freezes under a watch cursor; every tkinter mutation —
cursor restore, warnings, the token-cap refusal, the textbox merge — is
marshaled back with ``app.after(0, ...)``. A running flag per flow refuses
a second concurrent start.
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
from .context_attachment import (
    context_has_drawing_digest,
    context_within_token_cap,
    digested_drawing_filenames,
    drawing_filenames_with_failed_chunks,
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


def attach_drawing_files(app) -> None:
    """Pick drawing PDFs, run the one-time vision digest, merge the text.

    The digest is an API spend, so the flow is deliberately staged: local
    validation + chunk packing on a worker thread (pypdf reads every page
    tree and rewrites oversized files, which froze the window under a
    watch cursor), then a background ``count_tokens`` preflight feeding a
    cost-confirm dialog, and only then the digest call itself on a third
    background thread. All tkinter mutation is marshaled back to the main
    thread via ``app.after(0, ...)``; a running flag + button disable
    prevents concurrent digests and covers the prepare step too. A review
    started mid-digest is safe — the review snapshots Project Context at
    submit time.
    """
    if getattr(app, "_drawing_digest_running", False):
        return
    if getattr(app, "is_processing", False):
        messagebox.showwarning(
            "Review in progress",
            "Wait for the current review to finish before analyzing drawings.",
            parent=app,
        )
        return
    api_key = app.api_key_entry.get().strip()
    if not api_key:
        messagebox.showerror(
            "API key required",
            "Analyzing drawings calls the Anthropic API \u2014 enter your API key first.",
            parent=app,
        )
        return
    os.environ["ANTHROPIC_API_KEY"] = api_key

    files = filedialog.askopenfilenames(
        title="Attach construction drawings (PDF)",
        filetypes=_DRAWING_FILETYPES,
        parent=app,
    )
    if not files:
        return
    paths = [Path(f) for f in files]

    from ..programs import get_program

    program = get_program(
        getattr(app, "_selected_program_id", None)
        or getattr(app, "_selected_module_id", None)
    )
    module_display_name = program.display_name

    # Filled in by ``_on_prepared`` (Tk thread) once the worker has validated
    # and packed the set; read by the preflight / digest closures below.
    drawing_files: list = []
    chunks: list = []

    app._drawing_digest_running = True
    _set_drawings_button_state(app, "disabled")
    _set_cursor(app, "watch")

    def _log(msg: str, level: str = "info", **_kwargs) -> None:
        if hasattr(app, "log"):
            app.after(0, lambda m=msg, l=level: app.log.log(m, level=l))

    def _progress(pct: float, _msg: str, **_kwargs) -> None:
        # ``run_drawing_digest`` reports 0-100 (research precedent); the
        # bar wants 0-1. Never touch the bar while a review owns it.
        app.after(0, lambda p=pct: _show_digest_progress(app, p))

    def _reset() -> None:
        app._drawing_digest_running = False
        _set_drawings_button_state(app, "normal")
        _set_cursor(app, "")
        _hide_digest_progress(app)

    def _prepare_worker() -> None:
        try:
            files_ok, errors = validate_drawing_files(paths)
            packed = (
                build_digest_chunks(files_ok, model=DRAWING_DIGEST_MODEL_DEFAULT)
                if files_ok
                else []
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the operator
            app.after(0, lambda e=exc: _on_prepare_failed(e))
            return
        app.after(0, lambda f=files_ok, e=errors, c=packed: _on_prepared(f, e, c))

    def _on_prepare_failed(exc: BaseException) -> None:
        _reset()
        messagebox.showerror(
            "Drawing analysis failed",
            f"Could not read the drawing set: {exc}",
            parent=app,
        )

    def _on_prepared(files_ok: list, errors: list[str], packed: list) -> None:
        _set_cursor(app, "")
        if errors:
            messagebox.showwarning(
                "Some drawings could not be used",
                "\n".join(errors),
                parent=app,
            )
        if not packed:
            _reset()
            return
        drawing_files.extend(files_ok)
        chunks.extend(packed)
        threading.Thread(target=_preflight_worker, daemon=True).start()

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
            parent=app,
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
                parent=app,
            )
            return
        proceed = messagebox.askyesno(
            "Analyze drawings?",
            format_digest_confirm_message(
                preflight, chunks=chunks, model=DRAWING_DIGEST_MODEL_DEFAULT
            ),
            parent=app,
        )
        if not proceed:
            _reset()
            return
        _log(
            f"Analyzing {sum(len(c.parts) for c in chunks)} drawing "
            f"document(s) across {len(chunks)} request(s)...",
            level="step",
        )
        _show_digest_progress(app, 0.0)
        threading.Thread(target=_digest_worker, daemon=True).start()

    def _digest_worker() -> None:
        try:
            result = run_drawing_digest(
                chunks,
                model=DRAWING_DIGEST_MODEL_DEFAULT,
                module_display_name=module_display_name,
                log=_log,
                progress=_progress,
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
        messagebox.showerror("Drawing analysis failed", error, parent=app)

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
                parent=app,
            )
            return
        set_context_text(app, merged)
        _surface_attached_drawings(app, drawing_files, result)
        if result.failed_chunks:
            failed = "\n".join(
                f"- request {s.chunk_index + 1}: {', '.join(s.file_labels)} \u2014 {s.error}"
                for s in result.chunk_statuses
                if s.status == "failed"
            )
            messagebox.showwarning(
                "Digest completed partially",
                "Some drawing requests failed; their sheets are NOT in the "
                f"digest:\n{failed}",
                parent=app,
            )
        cost = result.actual_cost_usd()
        cost_text = f" (~${cost:,.2f})" if cost is not None else ""
        _log(
            f"Drawing digest complete: {result.completed_chunks}/"
            f"{len(result.chunk_statuses)} request(s), "
            f"{result.total_input_tokens:,} in / "
            f"{result.total_output_tokens:,} out tokens{cost_text}. "
            "Digest added to Project Context \u2014 review and edit it before "
            "running a review.",
            level="success",
        )

    threading.Thread(
        target=_prepare_worker, name="spec-critic-drawing-prepare", daemon=True
    ).start()


def _show_digest_progress(app, pct: float) -> None:
    """Show/advance the main progress bar for a digest (Tk thread only).

    A review run owns the bar while ``is_processing``; the digest then
    leaves it alone. ``pct`` is the runner's 0-100 scale.
    """
    if getattr(app, "is_processing", False):
        return
    bar = getattr(app, "progress_bar", None)
    if bar is None:
        return
    try:
        if not bar.winfo_ismapped():
            bar.pack(fill="x", pady=(8, 0), after=app.run_button)
            bar.configure(mode="determinate")
        bar.set(max(0.0, min(float(pct) / 100.0, 1.0)))
    except Exception:  # noqa: BLE001 — the bar is cosmetic; never break the digest
        pass


def _hide_digest_progress(app) -> None:
    if getattr(app, "is_processing", False):
        return
    bar = getattr(app, "progress_bar", None)
    if bar is None:
        return
    try:
        bar.pack_forget()
    except Exception:  # noqa: BLE001
        pass


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
        button_row, text="Save & Close", width=120, height=32,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
        command=_save_and_close,
    ).pack(side="right")
