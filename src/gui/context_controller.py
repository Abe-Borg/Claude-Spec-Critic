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

import os
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..input.extractor import CONTEXT_ATTACHMENT_EXTENSIONS, extract_context_text
from ..core.tokenizer import count_tokens, PROJECT_CONTEXT_MAX_TOKENS
from .context_attachment import (
    context_within_token_cap,
    merge_into_context,
    plan_drawing_attachment,
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

# Drawings are PDFs only — each page is one sheet (see ``src/drawings``).
_DRAWING_FILETYPES = [
    ("PDF drawings", "*.pdf"),
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


# ---------------------------------------------------------------------------
# Attach Drawings… — digest construction-drawing PDFs into Project Context
# ---------------------------------------------------------------------------
#
# The drawing engine reads each sheet with Opus 4.8 vision and emits a TEXT
# digest (``src/drawings``). Splicing that text into ``project_context`` makes
# every downstream phase — review, cross-check, verification — drawing-aware at
# plain-text cost, side-stepping the per-phase image caps. The digest pass is a
# vision call per sheet (minutes for a large set), so it runs on a worker
# thread; the engine import is lazy so this controller stays importable without
# PyMuPDF, and so tests can replace extraction with a fake at the seam below.


def _run_drawing_extraction(pdfs, *, progress):
    """Lazy, single-seam bridge to the drawing engine.

    Isolated here (rather than imported at module top) so ``context_controller``
    imports without PyMuPDF, and so a test can monkeypatch this one function to
    return a synthetic ``DrawingContext`` — no rendering, no network, no PyMuPDF.
    """
    from ..drawings import extract_drawing_context

    return extract_drawing_context(
        pdfs, progress=progress, use_cache=True, synthesize=True
    )


def _spawn(target, args) -> None:
    """Run ``target(*args)`` on a daemon worker thread.

    A one-line indirection that tests monkeypatch to run synchronously, so the
    threaded attach flow can be exercised deterministically.
    """
    threading.Thread(target=target, args=args, daemon=True).start()


def _estimate_drawing_cost(pdfs):
    """Lazy seam: count sheets and estimate the run's cost, or None on failure.

    Counting sheets opens the PDFs (PyMuPDF) but does not render — cheap enough
    for the main thread. Isolated + lazy so the controller imports without
    PyMuPDF and tests can monkeypatch it. Returns a ``DrawingCostEstimate`` or
    None (a failed estimate must not block the run — the worker surfaces the
    real error).
    """
    try:
        from ..core.api_config import REVIEW_MODEL_DEFAULT
        from ..drawings.cost import estimate_drawing_set_cost
        from ..drawings.render import list_sheets

        sheets = list_sheets([Path(p) for p in pdfs])
        if not sheets:
            return None  # nothing to confirm; the worker will report the empty set
        return estimate_drawing_set_cost(
            len(sheets), file_count=len(pdfs), model=REVIEW_MODEL_DEFAULT
        )
    except Exception:  # noqa: BLE001 - estimate is advisory; never block the run
        return None


def _confirm_drawing_cost(app, estimate) -> bool:
    """Ask the operator to confirm the estimated spend. Monkeypatched in tests."""
    from ..drawings.cost import format_drawing_cost_prompt

    return messagebox.askyesno(
        "Confirm drawing analysis", format_drawing_cost_prompt(estimate)
    )


def attach_drawings(app) -> None:
    """Pick drawing PDFs, digest them to text off-thread, and merge into context.

    Restricts the picker to PDFs (each page is one sheet), runs the vision
    digest on a worker thread with progress marshaled back to the UI, then
    splices the digest into Project Context as a labeled attachment — refused
    (never truncated) if it would exceed ``PROJECT_CONTEXT_MAX_TOKENS``. Reuses
    the app-wide ``is_processing`` busy flag so a review / resume / recover can't
    start mid-digest, and vice versa.
    """
    if getattr(app, "is_processing", False) or getattr(app, "_drawings_busy", False):
        return

    files = filedialog.askopenfilenames(
        title="Attach drawing PDFs", filetypes=_DRAWING_FILETYPES
    )
    if not files:
        return
    paths = [Path(f) for f in files]
    pdfs = [p for p in paths if p.suffix.lower() == ".pdf"]
    skipped = [p for p in paths if p.suffix.lower() != ".pdf"]
    if skipped:
        messagebox.showwarning(
            "Unsupported files",
            "Only .pdf drawings can be analyzed. Skipping:\n"
            + "\n".join(p.name for p in skipped),
        )
    if not pdfs:
        return

    key = app.api_key_entry.get().strip()
    if not key:
        messagebox.showerror(
            "API key required",
            "Enter your Anthropic API key, then try again — reading drawings is a "
            "live vision call.",
        )
        return
    os.environ["ANTHROPIC_API_KEY"] = key

    # Cost-confirm gate: estimate the spend and let the operator back out before
    # any (potentially expensive) vision calls. A None estimate (count failed)
    # skips the prompt and lets the worker surface the real error.
    estimate = _estimate_drawing_cost(pdfs)
    if estimate is not None and not _confirm_drawing_cost(app, estimate):
        app.log.log("Drawing analysis canceled.", level="muted")
        return

    app._drawings_busy = True
    app.is_processing = True
    app.log.log_step(
        f"Analyzing {len(pdfs)} drawing file(s) — one vision call per sheet; "
        "this can take a few minutes…"
    )
    app.progress_bar.pack(fill="x", pady=(8, 0), after=app.run_button)
    app.progress_bar.set(0)
    app.progress_bar.configure(mode="determinate")

    _spawn(_drawings_worker, (app, pdfs))


def _drawings_worker(app, pdfs) -> None:
    """Worker-thread body: run extraction, marshal the outcome to the UI."""

    def progress(done, total, label):
        app.after(0, lambda d=done, t=total, l=label: _on_drawings_progress(app, d, t, l))

    try:
        ctx = _run_drawing_extraction(pdfs, progress=progress)
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure
        app.after(0, lambda e=str(exc): _on_drawings_error(app, e))
        return
    app.after(0, lambda: _apply_drawing_result(app, ctx))


def _on_drawings_progress(app, done, total, label) -> None:
    if total:
        app.progress_bar.set(done / total)
    app.log.log(f"[{done}/{total}] {label}", level="muted")


def _reset_drawings_ui(app) -> None:
    app._drawings_busy = False
    app.is_processing = False
    try:
        app.progress_bar.pack_forget()
    except Exception:  # pragma: no cover - defensive; widget may be torn down
        pass


def _on_drawings_error(app, message) -> None:
    _reset_drawings_ui(app)
    app.log.log_error(f"Drawing analysis failed: {message}")
    messagebox.showerror("Drawing analysis failed", message)


def _apply_drawing_result(app, ctx) -> None:
    """Main-thread completion handler: surface errors, merge under the cap."""
    _reset_drawings_ui(app)
    plan = plan_drawing_attachment(get_project_context(app), ctx)

    if plan.error_lines:
        app.log.log_warning(
            f"{len(plan.error_lines)} drawing sheet(s) could not be analyzed."
        )
        if plan.has_digest:
            title = "Some sheets could not be analyzed"
            intro = "The digest of the readable sheets will still be attached."
        else:
            # Every sheet failed — nothing attaches, so don't promise it will.
            # All-failed is almost always a transient API/network blip, so point
            # the operator at a retry rather than at their PDF.
            title = "No sheets could be analyzed"
            intro = (
                "None of the sheets could be analyzed, so nothing was attached.\n"
                "This usually means a temporary API or network issue — try again "
                "in a few minutes."
            )
        messagebox.showwarning(title, intro + "\n\n" + "\n".join(plan.error_lines[:12]))

    if not plan.has_digest:
        app.log.log_warning("No drawing digest was produced; nothing attached.")
        return

    if not plan.within_cap:
        messagebox.showerror(
            "Project Context too large",
            f"Attaching this drawing digest would push Project Context to "
            f"{plan.tokens:,} tokens, exceeding the {PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
            f"Trim the existing context or attach fewer sheets.",
        )
        app.log.log_warning(
            "Drawing digest not attached — would exceed the Project Context token cap."
        )
        return

    set_context_text(app, plan.merged_context)
    app.log.log_success(
        f"Attached drawing digest: {ctx.ok_sheet_count}/{ctx.sheet_count} "
        f"sheet(s) from {ctx.file_count} file(s) ({plan.tokens:,} context tokens)."
    )


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
