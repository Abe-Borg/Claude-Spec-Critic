"""
Spec Critic - Modern GUI with CustomTkinter
M&P Specification Review • California K-12 DSA • Claude Opus 4.6
v1.4.0 - Per-spec siloed review with determinate progress bar
"""
import os, sys, threading
from pathlib import Path
from typing import Optional
import customtkinter as ctk

base_path = os.path.dirname(os.path.abspath(__file__))
exe_dir = Path(base_path).parent
sys.path.insert(0, str(exe_dir))

from src.pipeline import run_review
from src.reviewer import MODEL_OPUS_46
from src.extractor import extract_text_from_docx
from src.tokenizer import RECOMMENDED_MAX
from src.prompts import get_system_prompt
from src.widgets import (COLORS, TokenGauge, FileListPanel, EnhancedLog, AnimatedButton, ReportPanel, ReportWindow)

API_KEY_FILENAME = "spec_critic_api_key.txt"

# Placeholder hint shown in the project context textbox when empty
_CONTEXT_PLACEHOLDER = "Describe your project (optional)"


def load_api_key_from_file():
    kf = exe_dir / API_KEY_FILENAME
    if kf.exists():
        try: return kf.read_text(encoding="utf-8").strip()
        except Exception: pass
    return ""


class SpecReviewApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Spec Critic")
        self.geometry("900x950")
        self.minsize(750, 700)
        self.configure(fg_color=COLORS["bg_dark"])
        self.input_dir = None
        self.is_processing = False
        self._report_mode = False
        self._report_window: Optional[ReportWindow] = None
        self._project_context_tokens = 0
        fk = load_api_key_from_file()
        ek = os.environ.get("ANTHROPIC_API_KEY", "")
        self.api_key = fk if fk else ek
        self._create_ui()

    def _create_ui(self):
        c = ctk.CTkFrame(self, fg_color="transparent")
        c.pack(fill="both", expand=True, padx=24, pady=24)
        self.container = c

        # Report-mode toolbar (hidden by default)
        self.report_toolbar = ctk.CTkFrame(c, fg_color="transparent")
        tb_kw = {"height": 34, "font": ctk.CTkFont(family="Segoe UI", size=12), "fg_color": COLORS["bg_card"], "hover_color": COLORS["border"], "border_width": 1, "border_color": COLORS["border"], "text_color": COLORS["text_secondary"]}
        ctk.CTkButton(self.report_toolbar, text="\u2190  Back to Review", width=150, command=self._exit_report_mode, **tb_kw).pack(side="left")
        ctk.CTkButton(self.report_toolbar, text="\u21bb  New Review", width=130, command=self._reset_for_new_review, **tb_kw).pack(side="left", padx=(8, 0))

        # Header
        self.hdr = ctk.CTkFrame(c, fg_color="transparent")
        self.hdr.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(self.hdr, text="Spec Critic", font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"), text_color=COLORS["text_primary"]).pack(anchor="w")
        ctk.CTkLabel(self.hdr, text="M&P Specification Review  \u2022  California K-12 DSA  \u2022  Claude Opus 4.6", font=ctk.CTkFont(family="Segoe UI", size=13), text_color=COLORS["text_secondary"]).pack(anchor="w", pady=(4, 0))

        self._create_inputs_card(c)
        self.file_list_panel = FileListPanel(c, on_selection_change=self._on_file_selection_change, pack_after=self.inputs_card)
        self.token_gauge = TokenGauge(c, max_tokens=RECOMMENDED_MAX)
        self.token_gauge.pack(fill="x", pady=(16, 0))
        self.run_button = AnimatedButton(c, text="Run Review", command=self.start_review)
        self.run_button.pack(fill="x", pady=(16, 0))
        self.progress_bar = ctk.CTkProgressBar(c, height=4, corner_radius=2, fg_color=COLORS["bg_input"], progress_color=COLORS["accent"], indeterminate_speed=0.5)
        self.progress_bar.set(0)
        self.report_panel = ReportPanel(c, on_fullscreen=self._enter_report_mode)
        self.log = EnhancedLog(c)
        self.log.pack(fill="both", expand=True, pady=(16, 0))

    def _create_inputs_card(self, parent):
        self.inputs_card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8)
        self.inputs_card.pack(fill="x")
        self._inputs_expanded = True
        header = ctk.CTkFrame(self.inputs_card, fg_color="transparent", cursor="hand2")
        header.pack(fill="x", padx=16, pady=12)
        header.bind("<Button-1>", self._toggle_inputs_card)
        self.inputs_expand_label = ctk.CTkLabel(header, text="\u25bc", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_muted"], width=20)
        self.inputs_expand_label.pack(side="left")
        self.inputs_expand_label.bind("<Button-1>", self._toggle_inputs_card)
        lbl = ctk.CTkLabel(header, text="INPUTS", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"])
        lbl.pack(side="left", padx=(4, 0))
        lbl.bind("<Button-1>", self._toggle_inputs_card)
        self.inputs_content = ctk.CTkFrame(self.inputs_card, fg_color="transparent")
        self.inputs_content.pack(fill="x", padx=16, pady=(0, 16))

        # --- Row 0: API Key ---
        ctk.CTkLabel(self.inputs_content, text="API Key", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=0, column=0, sticky="w", pady=8)
        self.api_key_entry = ctk.CTkEntry(self.inputs_content, placeholder_text="sk-ant-...", font=ctk.CTkFont(family="Consolas", size=12), fg_color=COLORS["bg_input"], border_color=COLORS["border"], text_color=COLORS["text_primary"], height=36, show="\u2022")
        self.api_key_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=8)
        if self.api_key: self.api_key_entry.insert(0, self.api_key)

        # --- Row 1: Specs ---
        ctk.CTkLabel(self.inputs_content, text="Specs", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=1, column=0, sticky="w", pady=8)
        ef = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        ef.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=8)
        ef.columnconfigure(0, weight=1)
        self.input_dir_entry = ctk.CTkEntry(ef, placeholder_text="Select .docx specification files", font=ctk.CTkFont(family="Consolas", size=12), fg_color=COLORS["bg_input"], border_color=COLORS["border"], text_color=COLORS["text_primary"], height=36)
        self.input_dir_entry.grid(row=0, column=0, sticky="ew")
        bkw = {"height": 36, "font": ctk.CTkFont(size=12), "fg_color": COLORS["bg_input"], "hover_color": COLORS["border"], "border_width": 1, "border_color": COLORS["border"], "text_color": COLORS["text_secondary"]}
        ctk.CTkButton(ef, text="Browse", width=70, command=self._browse_files, **bkw).grid(row=0, column=1, padx=(8, 0))

        # --- Row 2: Project Context ---
        ctk.CTkLabel(self.inputs_content, text="Project Context", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="nw").grid(row=2, column=0, sticky="nw", pady=8)
        self.context_textbox = ctk.CTkTextbox(
            self.inputs_content,
            fg_color=COLORS["bg_input"],
            border_color=COLORS["border"],
            border_width=2,
            text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family="Consolas", size=12),
            height=80,
            wrap="word",
        )
        self.context_textbox.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=8)

        # Placeholder behavior: show muted hint when empty
        self._context_has_placeholder = True
        self.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
        self.context_textbox.configure(text_color=COLORS["text_muted"])
        self.context_textbox.bind("<FocusIn>", self._context_focus_in)
        self.context_textbox.bind("<FocusOut>", self._context_focus_out)
        # Recount tokens when project context changes
        self.context_textbox.bind("<KeyRelease>", self._on_context_change)

        self.inputs_content.columnconfigure(1, weight=1)

    # --- Project context placeholder helpers ---

    def _context_focus_in(self, event=None):
        """Clear placeholder text when the user clicks into the textbox."""
        if self._context_has_placeholder:
            self.context_textbox.delete("1.0", "end")
            self.context_textbox.configure(text_color=COLORS["text_primary"])
            self._context_has_placeholder = False

    def _context_focus_out(self, event=None):
        """Restore placeholder text if the user leaves the textbox empty."""
        text = self.context_textbox.get("1.0", "end").strip()
        if not text:
            self._context_has_placeholder = True
            self.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
            self.context_textbox.configure(text_color=COLORS["text_muted"])

    def _get_project_context(self) -> str:
        """Return the project context text, or empty string if placeholder is showing."""
        if self._context_has_placeholder:
            return ""
        return self.context_textbox.get("1.0", "end").strip()

    def _on_context_change(self, event=None):
        """Recount tokens when the project context text changes."""
        if not hasattr(self, "_loaded_file_data") or not self._loaded_file_data:
            return
        # Recompute project context tokens
        ctx = self._get_project_context()
        if ctx:
            from tiktoken import get_encoding
            enc = get_encoding("cl100k_base")
            self._project_context_tokens = len(enc.encode(ctx))
        else:
            self._project_context_tokens = 0
        # Trigger a full recount
        self._on_file_selection_change()

    def _toggle_inputs_card(self, event=None):
        if self._inputs_expanded:
            self.inputs_content.pack_forget(); self.inputs_expand_label.configure(text="\u25b6"); self._inputs_expanded = False
        else:
            self.inputs_content.pack(fill="x", padx=16, pady=(0, 16)); self.inputs_expand_label.configure(text="\u25bc"); self._inputs_expanded = True

    def _browse_files(self):
        files = ctk.filedialog.askopenfilenames(title="Select .docx specification files", filetypes=[("Word Documents", "*.docx"), ("All Files", "*.*")])
        if files:
            paths = [Path(f) for f in files if f.lower().endswith(".docx")]
            if not paths: self.log.log_warning("No .docx files selected"); return
            self._selected_files = paths
            self.input_dir = paths[0].parent
            self.input_dir_entry.delete(0, "end")
            self.input_dir_entry.insert(0, str(paths[0]) if len(paths) == 1 else f"{len(paths)} files selected")
            self._analyze_tokens(paths)

    def _analyze_tokens(self, file_paths):
        """Run token analysis in a background thread.

        v1.2.0: filenames are accumulated and logged in a single batched
        callback instead of one after(0) per file, reducing main-thread
        scheduling pressure during rapid file processing.

        v1.3.0: includes project context tokens in the total.
        """
        if not file_paths:
            self.log.log_warning("No .docx files found"); self.token_gauge.reset(); self.file_list_panel.reset(); return
        self.log.log_step(f"Analyzing {len(file_paths)} files...")
        def analyze():
            try:
                file_data = []
                processed_names: list[str] = []
                from tiktoken import get_encoding
                enc = get_encoding("cl100k_base")
                self._system_prompt_tokens = len(enc.encode(get_system_prompt()))

                # Count project context tokens
                ctx = self._get_project_context()
                self._project_context_tokens = len(enc.encode(ctx)) if ctx else 0

                for f in file_paths:
                    try:
                        spec = extract_text_from_docx(f)
                        tokens = len(enc.encode(spec.content))
                        file_data.append({"path": f, "filename": spec.filename, "tokens": tokens, "content": spec.content})
                        processed_names.append(f.name)
                    except Exception as e:
                        self.after(0, lambda err=str(e), n=f.name: self.log.log_warning(f"Could not read {n}: {err}"))

                # Batch-log all successfully processed filenames in one callback
                if processed_names:
                    self.after(0, lambda names=processed_names: self.log.log_file_batch(names))

                if file_data:
                    self._loaded_file_data = file_data
                    total = self._system_prompt_tokens + self._project_context_tokens + sum(d["tokens"] for d in file_data)
                    self.after(0, lambda: self.file_list_panel.load_files(file_data))
                    self.after(0, lambda: self.token_gauge.update_gauge(total, len(file_data)))
                    self.after(0, lambda: self.log.log_success(f"Token analysis complete: {total:,} tokens"))
                    wl = total <= RECOMMENDED_MAX
                    self.after(0, lambda: self.run_button.configure(state="normal" if wl else "disabled"))
                    self.after(0, lambda w=wl: self.file_list_panel.set_over_limit(not w))
            except Exception as e:
                self.after(0, lambda: self.log.log_error(f"Analysis failed: {e}"))
        threading.Thread(target=analyze, daemon=True).start()

    def _on_file_selection_change(self):
        if not hasattr(self, "_loaded_file_data") or not self._loaded_file_data: return
        sel = set(self.file_list_panel.get_selected_files())
        total = (
            getattr(self, "_system_prompt_tokens", 0)
            + getattr(self, "_project_context_tokens", 0)
            + sum(d["tokens"] for d in self._loaded_file_data if d["path"] in sel)
        )
        fc = len(sel)
        self.token_gauge.update_gauge(total, fc)
        wl = total <= RECOMMENDED_MAX
        self.run_button.configure(state="normal" if (wl and fc > 0) else "disabled")
        self.file_list_panel.set_over_limit(not wl)

    def _validate_inputs(self):
        if not self.api_key_entry.get().strip(): self.log.log_error("API key is required"); return False
        if not hasattr(self, "_selected_files") or not self._selected_files: self.log.log_error("Select .docx specification files"); return False
        missing = [f for f in self._selected_files if not f.exists()]
        if missing: self.log.log_error(f"File not found: {missing[0].name}"); return False
        if self.file_list_panel.get_selected_count() == 0: self.log.log_error("No files selected"); return False
        if self.token_gauge.token_count > RECOMMENDED_MAX: self.log.log_error("Token limit exceeded"); return False
        return True

    def start_review(self):
        if self.is_processing: return
        if not self._validate_inputs(): return
        self._selected_files_for_review = self.file_list_panel.get_selected_files()
        self._project_context_for_review = self._get_project_context()
        self.is_processing = True
        self.report_panel.clear()
        self._close_report_window()
        self.log.log("\u2500" * 40, level="muted", timestamp=False, paced=False)
        self.run_button.set_processing()
        self.progress_bar.pack(fill="x", pady=(8, 0), after=self.run_button)
        self.progress_bar.set(0); self.progress_bar.configure(mode="determinate")
        os.environ["ANTHROPIC_API_KEY"] = self.api_key_entry.get().strip()
        self.log.log_step(f"Reviewing {len(self._selected_files_for_review)} files...")
        threading.Thread(target=self._run_review_thread, daemon=True).start()

    def _run_review_thread(self):
        try:
            n = len(self._selected_files_for_review)
            self.after(0, lambda: self.log.log_step("Starting per-spec review..."))
            self.after(0, lambda: self.log.log(f"Model: {MODEL_OPUS_46}  \u2022  {n} specs \u2022  1 API call per spec", level="muted"))

            def _on_progress(pct, msg):
                self.after(0, lambda m=msg: self.log.log_step(m))
                # Drive the determinate progress bar from pipeline progress %
                self.after(0, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

            result = run_review(
                input_dir=self.input_dir,
                files=self._selected_files_for_review,
                project_context=self._project_context_for_review,
                dry_run=False, verbose=False,
                log=lambda msg: self.after(0, lambda m=msg: self.log.log(m, level="info")),
                progress=_on_progress,
            )
            self.after(0, lambda: self._on_review_complete(result))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            self.after(0, lambda: self._on_review_error(err))

    def _on_review_complete(self, result):
        self.progress_bar.set(1.0)
        self.log.log_success("Review complete!")
        if result.review_result:
            rv = result.review_result
            self.log.log(f"Findings: {rv.critical_count} critical, {rv.high_count} high, {rv.medium_count} medium, {rv.gripe_count} gripes", level="info")
            self.log.log(f"Time: {rv.elapsed_seconds:.1f}s", level="muted")
            # Open pop-out report window (report no longer renders in the main UI)
            self._open_report_window(rv, result.files_reviewed, result.leed_alerts, result.placeholder_alerts)
        self.run_button.set_complete()
        self.after(2500, self._reset_ui)

    def _on_review_error(self, err):
        self.progress_bar.pack_forget()
        self.log.log_error(f"Review failed: {err}")
        self.run_button.set_ready(); self.is_processing = False

    def _reset_ui(self):
        self.run_button.set_ready(); self.progress_bar.pack_forget(); self.is_processing = False

    # ----- Pop-out report window -----

    def _open_report_window(self, review, files_reviewed, leed_alerts, placeholder_alerts):
        """Open a detached report window with the full results."""
        self._close_report_window()
        self._report_window = ReportWindow(
            self, review=review, files_reviewed=files_reviewed,
            leed_alerts=leed_alerts, placeholder_alerts=placeholder_alerts,
            project_context=getattr(self, "_project_context_for_review", ""),
        )

    def _close_report_window(self):
        """Close the existing report window if one is open."""
        if self._report_window is not None:
            try:
                self._report_window.destroy()
            except Exception:
                pass
            self._report_window = None

    # ----- Report expand / collapse mode -----

    def _enter_report_mode(self):
        """Hide all input panels so the report fills the entire window."""
        self._report_mode = True
        self.hdr.pack_forget()
        self.inputs_card.pack_forget()
        self.file_list_panel.pack_forget()
        self.token_gauge.pack_forget()
        self.run_button.pack_forget()
        self.progress_bar.pack_forget()
        self.log.pack_forget()
        self.report_toolbar.pack(fill="x", pady=(0, 8), before=self.report_panel)

    def _exit_report_mode(self):
        """Restore all input panels, keep report visible below."""
        self._report_mode = False
        self.report_toolbar.pack_forget()
        # Re-pack all widgets in original order
        self.hdr.pack(fill="x", pady=(0, 20))
        self.inputs_card.pack(fill="x")
        if self.file_list_panel._file_data:
            self.file_list_panel.pack(fill="x", pady=(16, 0), after=self.inputs_card)
        self.token_gauge.pack(fill="x", pady=(16, 0))
        self.run_button.pack(fill="x", pady=(16, 0))
        # Log stays collapsed so report keeps space
        self.log.pack(fill="x", pady=(16, 0))

    def _reset_for_new_review(self):
        """Clear all state and return to a fresh starting layout."""
        # Exit report mode if active
        if self._report_mode:
            self._report_mode = False
            self.report_toolbar.pack_forget()

        # Close pop-out window
        self._close_report_window()

        # Clear results
        self.report_panel.clear()
        self.progress_bar.pack_forget()

        # Reset input state
        self.input_dir = None
        self._selected_files = []
        self._loaded_file_data = []
        self._project_context_tokens = 0
        self.input_dir_entry.delete(0, "end")

        # Clear project context and restore placeholder
        self.context_textbox.delete("1.0", "end")
        self._context_has_placeholder = True
        self.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
        self.context_textbox.configure(text_color=COLORS["text_muted"])

        # Reset widgets
        self.token_gauge.reset()
        self.file_list_panel.reset()
        self.log.clear()
        self.run_button.set_ready()
        self.is_processing = False

        # Re-pack everything in original order
        self.hdr.pack(fill="x", pady=(0, 20))
        self.inputs_card.pack(fill="x")
        self.token_gauge.pack(fill="x", pady=(16, 0))
        self.run_button.pack(fill="x", pady=(16, 0))
        self.log.pack(fill="both", expand=True, pady=(16, 0))
        if not self._inputs_expanded:
            self._toggle_inputs_card()


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    SpecReviewApp().mainloop()

if __name__ == "__main__":
    main()