"""Standalone CustomTkinter window for the drawing-context extractor.

Drop (or browse for) construction drawing PDFs, press Analyze, and the
subsystem renders + digests every sheet and shows the combined text digest,
which can be saved to a ``.md`` file for use as Project Context in the spec
reviewer. Runs the analysis on a worker thread so the UI stays responsive, and
reuses the main app's color palette for a consistent look.

Launch with ``python -m drawing_analyzer``.
"""
from __future__ import annotations

import os
import shlex
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:  # drag-and-drop is optional (mirrors the main app shell)
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # pragma: no cover - depends on optional dependency
    DND_FILES = None
    TkinterDnD = None

if TkinterDnD is not None:
    class _CTkDnDRoot(ctk.CTk, TkinterDnD.DnDWrapper):
        pass
else:  # pragma: no cover - exercised only without tkinterdnd2
    _CTkDnDRoot = ctk.CTk

from .core.api_config import REVIEW_MODEL_DEFAULT
from .core.api_key_store import load_api_key_from_file
from .colors import COLORS
from .cost import estimate_drawing_set_cost, format_drawing_cost_prompt
from .pipeline import DrawingContext, extract_drawing_context
from .render import list_sheets

_PDF_FILETYPES = [("PDF drawings", "*.pdf"), ("All files", "*.*")]


class DrawingAnalyzerApp(_CTkDnDRoot):
    """Minimal standalone window driving the drawing-digest pipeline."""

    def __init__(self) -> None:
        super().__init__()
        if TkinterDnD is not None:
            try:  # initialize the tkdnd runtime, as the main app does
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception:
                pass

        self.title("Spec Critic — Drawing Context Analyzer")
        self.geometry("820x680")
        self.minsize(640, 520)
        self.configure(fg_color=COLORS["bg_dark"])

        self._pdfs: list[Path] = []
        self._ctx: DrawingContext | None = None
        self._busy = False
        self._has_key = self._load_api_key()

        self._build_ui()
        self._register_dnd()
        self._refresh_summary()

    # ------------------------------------------------------------------ setup

    def _load_api_key(self) -> bool:
        key = os.environ.get("ANTHROPIC_API_KEY") or load_api_key_from_file()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            return True
        return False

    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=8)
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            outer,
            text="Drawing Context Analyzer",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            outer,
            text=(
                "Drop mechanical / plumbing / fire-protection drawing PDFs "
                "(one or many; multi-sheet PDFs are split page-by-page). "
                "Each sheet is read by Claude Opus 4.8 and summarized to text."
            ),
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_secondary"],
            wraplength=740,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 10))

        # Drop zone
        self.drop_zone = ctk.CTkFrame(
            outer, fg_color=COLORS["bg_input"], corner_radius=8,
            border_width=2, border_color=COLORS["border"], height=90,
        )
        self.drop_zone.pack(fill="x", padx=16, pady=(0, 8))
        self.drop_zone.pack_propagate(False)
        self.drop_label = ctk.CTkLabel(
            self.drop_zone,
            text="Drop drawing PDFs here  —  or  —",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=COLORS["text_muted"],
        )
        self.drop_label.pack(side="left", expand=True, padx=(16, 4), pady=16)
        ctk.CTkButton(
            self.drop_zone, text="Browse…", width=110, height=34,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_secondary"], command=self._on_browse,
        ).pack(side="right", padx=(4, 16), pady=16)

        # Summary + actions row
        row = ctk.CTkFrame(outer, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        self.summary_label = ctk.CTkLabel(
            row, text="", font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_secondary"], justify="left",
        )
        self.summary_label.pack(side="left")
        self.clear_btn = ctk.CTkButton(
            row, text="Clear", width=80, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_secondary"], command=self._on_clear,
        )
        self.clear_btn.pack(side="right")
        self.analyze_btn = ctk.CTkButton(
            row, text="Analyze Drawings", width=170, height=34,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=self._on_process,
        )
        self.analyze_btn.pack(side="right", padx=(0, 8))

        # Progress
        self.progress_label = ctk.CTkLabel(
            outer, text="", font=ctk.CTkFont(family="Consolas", size=12),
            text_color=COLORS["text_muted"], anchor="w",
        )
        self.progress_label.pack(fill="x", padx=16, pady=(0, 4))

        # Output
        self.output = ctk.CTkTextbox(
            outer, fg_color=COLORS["bg_input"], border_color=COLORS["border"],
            border_width=2, text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family="Consolas", size=12), wrap="word",
        )
        self.output.pack(fill="both", expand=True, padx=16, pady=(4, 8))

        self.save_btn = ctk.CTkButton(
            outer, text="Save Digest…", width=140, height=34,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=self._on_save, state="disabled",
        )
        self.save_btn.pack(anchor="e", padx=16, pady=(0, 16))

        if not self._has_key:
            self._set_progress_text(
                "No ANTHROPIC_API_KEY found — set it (or save a key file) before "
                "analyzing.",
                color=COLORS["warning"],
            )

    def _register_dnd(self) -> None:
        if DND_FILES is None:
            return
        try:
            self.drop_zone.drop_target_register(DND_FILES)
            self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:  # pragma: no cover - platform dependent
            pass

    # ------------------------------------------------------------- selection

    def _parse_paths(self, payload: str) -> list[Path]:
        if not payload:
            return []
        try:
            items = list(self.tk.splitlist(payload))
        except Exception:
            try:
                items = shlex.split(payload)
            except ValueError:
                items = [payload]
        out: list[Path] = []
        for item in items:
            name = item.strip().strip("{}").strip('"')
            if name:
                out.append(Path(name))
        return out

    def _on_drop(self, event) -> None:
        self._add_pdfs(self._parse_paths(getattr(event, "data", "")))

    def _on_browse(self) -> None:
        files = filedialog.askopenfilenames(
            title="Select drawing PDFs", filetypes=_PDF_FILETYPES
        )
        if files:
            self._add_pdfs([Path(f) for f in files])

    def _add_pdfs(self, paths: list[Path]) -> None:
        if self._busy:
            return
        added = False
        existing = {str(p) for p in self._pdfs}
        for p in paths:
            if p.suffix.lower() != ".pdf":
                continue
            if str(p) not in existing:
                self._pdfs.append(p)
                existing.add(str(p))
                added = True
        if added:
            self._refresh_summary()

    def _on_clear(self) -> None:
        if self._busy:
            return
        self._pdfs = []
        self._ctx = None
        self.output.delete("1.0", "end")
        self.save_btn.configure(state="disabled")
        self._set_progress_text("")
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        if not self._pdfs:
            self.summary_label.configure(text="No drawings selected.")
            return
        refs = list_sheets(self._pdfs)
        sheets = len(refs)
        files = len({r.pdf_path for r in refs})
        est = estimate_drawing_set_cost(
            sheets, file_count=files, model=REVIEW_MODEL_DEFAULT, batch=True
        )
        cost = (
            f"~${est.total_cost:,.2f} (est.)"
            if est.total_cost is not None
            else "cost n/a"
        )
        self.summary_label.configure(
            text=(
                f"{files} file(s), {sheets} sheet(s)  ·  "
                f"~{est.image_tokens:,} image tokens  ·  {cost}"
            )
        )

    # --------------------------------------------------------------- process

    def _on_process(self) -> None:
        if self._busy:
            return
        if not self._pdfs:
            messagebox.showinfo("No drawings", "Add one or more drawing PDFs first.")
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            messagebox.showerror(
                "No API key",
                "No ANTHROPIC_API_KEY is set. Set the environment variable or "
                "save a key file, then reopen this window.",
            )
            return

        # Cost-confirm gate — show the estimated (batch-rate) spend before the
        # batch is submitted. Nothing is sent until this is confirmed.
        refs = list_sheets(self._pdfs)
        estimate = estimate_drawing_set_cost(
            len(refs), file_count=len(self._pdfs), model=REVIEW_MODEL_DEFAULT, batch=True
        )
        if not messagebox.askyesno(
            "Confirm drawing analysis", format_drawing_cost_prompt(estimate)
        ):
            return

        self._busy = True
        self._ctx = None
        self.analyze_btn.configure(state="disabled", text="Analyzing…")
        self.clear_btn.configure(state="disabled")
        self.save_btn.configure(state="disabled")
        self.output.delete("1.0", "end")
        self._set_progress_text("Starting…", color=COLORS["text_secondary"])

        pdfs = list(self._pdfs)
        threading.Thread(target=self._worker, args=(pdfs,), daemon=True).start()

    def _worker(self, pdfs: list[Path]) -> None:
        try:
            ctx = extract_drawing_context(
                pdfs,
                model=REVIEW_MODEL_DEFAULT,
                progress=self._progress_from_thread,
                use_cache=True,
                synthesize=True,
                use_batch=True,
            )
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure
            self.after(0, lambda e=exc: self._on_error(str(e)))
            return
        self.after(0, lambda: self._on_done(ctx))

    def _progress_from_thread(self, done: int, total: int, label: str) -> None:
        self.after(0, lambda: self._set_progress(done, total, label))

    def _set_progress(self, done: int, total: int, label: str) -> None:
        pct = f"[{done}/{total}] " if total else ""
        self._set_progress_text(f"{pct}{label}", color=COLORS["text_secondary"])

    def _set_progress_text(self, text: str, *, color: str | None = None) -> None:
        self.progress_label.configure(
            text=text, text_color=color or COLORS["text_muted"]
        )

    def _on_done(self, ctx: DrawingContext) -> None:
        self._busy = False
        self._ctx = ctx
        self.analyze_btn.configure(state="normal", text="Analyze Drawings")
        self.clear_btn.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", ctx.combined_text or "(no digest produced)")
        if ctx.combined_text.strip():
            self.save_btn.configure(state="normal")

        ok = ctx.ok_sheet_count
        cached = ctx.cached_sheet_count
        cached_note = f" ({cached} from cache)" if cached else ""
        msg = (
            f"Done — {ok}/{ctx.sheet_count} sheet(s) analyzed{cached_note} · "
            f"input {ctx.total_input_tokens:,} tok, output "
            f"{ctx.total_output_tokens:,} tok"
        )
        color = COLORS["success"] if not ctx.errors else COLORS["warning"]
        if ctx.errors:
            msg += f" · {len(ctx.errors)} error(s)"
        self._set_progress_text(msg, color=color)
        if ctx.errors:
            messagebox.showwarning(
                "Some sheets could not be analyzed", "\n".join(ctx.errors[:12])
            )

    def _on_error(self, message: str) -> None:
        self._busy = False
        self.analyze_btn.configure(state="normal", text="Analyze Drawings")
        self.clear_btn.configure(state="normal")
        self._set_progress_text(f"Failed: {message}", color=COLORS["error"])
        messagebox.showerror("Analysis failed", message)

    def _on_save(self) -> None:
        if not self._ctx or not self._ctx.combined_text.strip():
            return
        path = filedialog.asksaveasfilename(
            title="Save drawing digest",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*.*")],
            initialfile="drawing_context.md",
        )
        if not path:
            return
        try:
            Path(path).write_text(self._ctx.combined_text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc))
            return
        self._set_progress_text(f"Saved to {path}", color=COLORS["success"])


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = DrawingAnalyzerApp()
    app.mainloop()
