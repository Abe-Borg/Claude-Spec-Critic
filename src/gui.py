"""
Spec Critic - Modern GUI with CustomTkinter
M&P Specification Review • California K-12 DSA • Claude Opus / Sonnet
v1.9.0 - PDF support for native (text-selectable) PDF files
"""
import os, sys, json, time, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import customtkinter as ctk
from tkinter import filedialog

base_path = os.path.dirname(os.path.abspath(__file__))
exe_dir = Path(base_path).parent
sys.path.insert(0, str(exe_dir))

from src.pipeline import run_review, start_batch_review, collect_batch_results, BatchSubmission
from src.batch import poll_batch, cancel_batch, BatchStatus, BatchJob
from src.reviewer import MODEL_OPUS_46, MODEL_SONNET_46, REVIEW_MODELS
from src.extractor import extract_text, ExtractedSpec, SUPPORTED_EXTENSIONS
from src.tokenizer import RECOMMENDED_MAX
from src.prompts import get_system_prompt
from src.report_exporter import export_report

from src.widgets import (COLORS, TokenGauge, FileListPanel, EnhancedLog, AnimatedButton, ReportPanel, ReportWindow)

from platformdirs import user_config_dir, user_state_dir

API_KEY_FILENAME = "spec_critic_api_key.txt"
BATCH_STATE_FILENAME = "batch_state.json"

# Maximum age (hours) for a batch state file before it's considered stale
BATCH_STATE_MAX_AGE_HOURS = 24

# Placeholder hint shown in the project context textbox when empty
_CONTEXT_PLACEHOLDER = "Describe your project (optional)"

# File dialog filter for supported spec formats
_SPEC_FILETYPES = [
    ("Specifications", "*.docx *.pdf"),
    ("Word Documents", "*.docx"),
    ("PDF Documents", "*.pdf"),
    ("All Files", "*.*"),
]


def _app_config_dir() -> Path:
    """Return a user-writable config directory for Spec Critic.

    Uses platformdirs so this works correctly in frozen PyInstaller
    builds where the exe directory may be read-only.
    """
    d = Path(user_config_dir("SpecCritic", appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _app_state_dir() -> Path:
    """Return a user-writable state directory for Spec Critic."""
    d = Path(user_state_dir("SpecCritic", appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_api_key_from_file():
    """Load API key, checking platformdirs config dir first, then legacy exe_dir."""
    # Check new location first
    kf = _app_config_dir() / API_KEY_FILENAME
    if kf.exists():
        try:
            return kf.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    # Fall back to legacy location (project root / exe_dir)
    kf = exe_dir / API_KEY_FILENAME
    if kf.exists():
        try:
            return kf.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# Persistent batch state (Chunk 5)
# ---------------------------------------------------------------------------

def _batch_state_path() -> Path:
    """Return the path to the batch state file in user-writable state dir."""
    return _app_state_dir() / BATCH_STATE_FILENAME


def save_batch_state(submission: BatchSubmission, phase: str = "review") -> None:
    """Serialize a BatchSubmission to disk for recovery after app restart.

    Args:
        submission: The batch submission to save
        phase: Current phase — "review" or "verify"
    """
    state = {
        "version": "1.9.0",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "batch_id": submission.job.batch_id,
        "job_type": submission.job.job_type,
        "request_map": submission.job.request_map,
        "created_at": submission.job.created_at,
        "files_reviewed": submission.files_reviewed,
        "leed_alerts": submission.leed_alerts,
        "placeholder_alerts": submission.placeholder_alerts,
        "model": getattr(submission, "model", MODEL_OPUS_46),
    }
    try:
        _batch_state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        # Log will be available once GUI is running; print as fallback
        print(f"[SpecCritic] Warning: Could not save batch state: {e}")


def load_batch_state() -> Optional[tuple[BatchSubmission, str]]:
    """Load a saved batch state from disk.

    Returns:
        Tuple of (BatchSubmission, phase) if a valid state file exists,
        or None if no state file or it's invalid/stale.
    """
    path = _batch_state_path()
    if not path.exists():
        return None

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        delete_batch_state()
        return None

    # Check staleness
    try:
        saved_at = datetime.fromisoformat(state["saved_at"])
        age_hours = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600
        if age_hours > BATCH_STATE_MAX_AGE_HOURS:
            delete_batch_state()
            return None
    except Exception:
        delete_batch_state()
        return None

    # Reconstruct BatchSubmission
    try:
        job = BatchJob(
            batch_id=state["batch_id"],
            job_type=state.get("job_type", "review"),
            request_map=state["request_map"],
            created_at=state["created_at"],
        )
        submission = BatchSubmission(
            job=job,
            files_reviewed=state.get("files_reviewed", []),
            leed_alerts=state.get("leed_alerts", []),
            placeholder_alerts=state.get("placeholder_alerts", []),
            model=state.get("model", MODEL_OPUS_46),
        )
        phase = state.get("phase", "review")
        return submission, phase
    except (KeyError, TypeError):
        delete_batch_state()
        return None


def delete_batch_state() -> None:
    """Remove the batch state file."""
    try:
        path = _batch_state_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _is_supported_spec(filepath: Path) -> bool:
    """Check if a file has a supported spec extension."""
    return filepath.suffix.lower() in SUPPORTED_EXTENSIONS


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
        self._batch_submission: Optional[BatchSubmission] = None
        self._batch_poll_id: Optional[str] = None
        self._extracted_specs: list[ExtractedSpec] = []
        fk = load_api_key_from_file()
        ek = os.environ.get("ANTHROPIC_API_KEY", "")
        self.api_key = fk if fk else ek
        self._create_ui()
        # Check for pending batch state after UI is ready
        self.after(500, self._check_pending_batch)

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
        hdr_title_row = ctk.CTkFrame(self.hdr, fg_color="transparent")
        hdr_title_row.pack(fill="x")
        ctk.CTkLabel(hdr_title_row, text="Spec Critic", font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"), text_color=COLORS["text_primary"]).pack(side="left")
        ctk.CTkButton(
            hdr_title_row, text="How It Works", width=110, height=30,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_secondary"], command=self._show_about_dialog,
        ).pack(side="right", pady=(4, 0))
        ctk.CTkLabel(self.hdr, text="M&P Specification Review  \u2022  California K-12 DSA", font=ctk.CTkFont(family="Segoe UI", size=13), text_color=COLORS["text_secondary"]).pack(anchor="w", pady=(4, 0))

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
        self.input_dir_entry = ctk.CTkEntry(ef, placeholder_text="Select .docx or .pdf specification files", font=ctk.CTkFont(family="Consolas", size=12), fg_color=COLORS["bg_input"], border_color=COLORS["border"], text_color=COLORS["text_primary"], height=36)
        self.input_dir_entry.grid(row=0, column=0, sticky="ew")
        bkw = {"height": 36, "font": ctk.CTkFont(size=12), "fg_color": COLORS["bg_input"], "hover_color": COLORS["border"], "border_width": 1, "border_color": COLORS["border"], "text_color": COLORS["text_secondary"]}
        ctk.CTkButton(ef, text="Browse", width=70, command=self._browse_files, **bkw).grid(row=0, column=1, padx=(8, 0))

        # --- Row 2: Project Context ---
        ctk.CTkLabel(self.inputs_content, text="Project Context", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="nw").grid(row=2, column=0, sticky="nw", pady=8)
        self.context_textbox = ctk.CTkTextbox(
            self.inputs_content, fg_color=COLORS["bg_input"], border_color=COLORS["border"],
            border_width=2, text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family="Consolas", size=12), height=80, wrap="word",
        )
        self.context_textbox.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=8)
        self._context_has_placeholder = True
        self.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
        self.context_textbox.configure(text_color=COLORS["text_muted"])
        self.context_textbox.bind("<FocusIn>", self._context_focus_in)
        self.context_textbox.bind("<FocusOut>", self._context_focus_out)
        self.context_textbox.bind("<KeyRelease>", self._on_context_change)

        # --- Row 3: Review Model ---
        ctk.CTkLabel(self.inputs_content, text="Review Model", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=3, column=0, sticky="w", pady=8)
        model_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        model_frame.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=8)
        self._review_model_var = ctk.StringVar(value="Opus 4.6")
        self.model_selector = ctk.CTkSegmentedButton(
            model_frame, values=list(REVIEW_MODELS.keys()), variable=self._review_model_var,
            command=self._on_model_change, font=ctk.CTkFont(family="Segoe UI", size=11),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_secondary"],
            text_color_disabled=COLORS["text_muted"], height=32,
        )
        self.model_selector.set("Opus 4.6")
        self.model_selector.pack(side="left")
        self._model_hint = ctk.CTkLabel(model_frame, text="Most thorough \u2022 recommended",
            font=ctk.CTkFont(family="Segoe UI", size=10), text_color=COLORS["text_muted"])
        self._model_hint.pack(side="left", padx=(12, 0))

        # --- Row 4: Review Mode ---
        ctk.CTkLabel(self.inputs_content, text="Mode", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=4, column=0, sticky="w", pady=8)
        mode_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        mode_frame.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=8)
        self._review_mode = ctk.StringVar(value="realtime")
        self.mode_selector = ctk.CTkSegmentedButton(
            mode_frame, values=["Real-time", "Batch (50% off)"], variable=self._review_mode,
            command=self._on_mode_change, font=ctk.CTkFont(family="Segoe UI", size=11),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_secondary"],
            text_color_disabled=COLORS["text_muted"], height=32,
        )
        self.mode_selector.set("Real-time")
        self.mode_selector.pack(side="left")
        self._mode_hint = ctk.CTkLabel(mode_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=10), text_color=COLORS["text_muted"])
        self._mode_hint.pack(side="left", padx=(12, 0))

        # --- Row 5: Output ---
        ctk.CTkLabel(self.inputs_content, text="Output", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=5, column=0, sticky="w", pady=8)
        output_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        output_frame.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=8)
        self._output_mode_var = ctk.StringVar(value="View in App")
        self.output_selector = ctk.CTkSegmentedButton(
            output_frame, values=["View in App", "Export Report"],
            variable=self._output_mode_var,
            command=self._on_output_mode_change,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_secondary"],
            text_color_disabled=COLORS["text_muted"], height=32,
        )
        self.output_selector.set("View in App")
        self.output_selector.pack(side="left")
        self._output_hint = ctk.CTkLabel(output_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=10), text_color=COLORS["text_muted"])
        self._output_hint.pack(side="left", padx=(12, 0))

        # --- Row 6: Options (cross-check) ---
        ctk.CTkLabel(self.inputs_content, text="Options", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=6, column=0, sticky="w", pady=8)
        options_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        options_frame.grid(row=6, column=1, sticky="w", padx=(8, 0), pady=8)
        self._cross_check_var = ctk.BooleanVar(value=False)
        self._cross_check_cb = ctk.CTkCheckBox(
            options_frame, text="Cross-spec coordination check", variable=self._cross_check_var,
            font=ctk.CTkFont(family="Segoe UI", size=12), fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"], border_color=COLORS["border"],
            checkmark_color=COLORS["text_primary"], text_color=COLORS["text_secondary"],
            checkbox_width=20, checkbox_height=20,
        )
        self._cross_check_cb.pack(side="left")
        self._cross_check_hint = ctk.CTkLabel(options_frame,
            text="Sonnet 4.6 \u2022 finds inter-spec conflicts",
            font=ctk.CTkFont(family="Segoe UI", size=10), text_color=COLORS["text_muted"])
        self._cross_check_hint.pack(side="left", padx=(12, 0))

        self.inputs_content.columnconfigure(1, weight=1)

    # --- Model selector helpers ---

    def _on_model_change(self, value: str):
        if value == "Opus 4.6":
            self._model_hint.configure(text="Most thorough \u2022 recommended")
        else:
            self._model_hint.configure(text="Faster \u2022 cheaper \u2022 good for quick reviews")

    @property
    def _selected_review_model(self) -> str:
        label = self._review_model_var.get()
        return REVIEW_MODELS.get(label, MODEL_OPUS_46)

    # --- Output mode helpers ---

    def _on_output_mode_change(self, value: str):
        if value == "Export Report":
            self._output_hint.configure(text="Saves .docx report \u2022 no in-app rendering")
        else:
            self._output_hint.configure(text="")

    @property
    def _is_export_mode(self) -> bool:
        return self.output_selector.get() == "Export Report"

    # --- Project context placeholder helpers ---

    def _context_focus_in(self, event=None):
        if self._context_has_placeholder:
            self.context_textbox.delete("1.0", "end")
            self.context_textbox.configure(text_color=COLORS["text_primary"])
            self._context_has_placeholder = False

    def _context_focus_out(self, event=None):
        text = self.context_textbox.get("1.0", "end").strip()
        if not text:
            self._context_has_placeholder = True
            self.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
            self.context_textbox.configure(text_color=COLORS["text_muted"])

    def _get_project_context(self) -> str:
        if self._context_has_placeholder:
            return ""
        return self.context_textbox.get("1.0", "end").strip()

    def _on_context_change(self, event=None):
        if not hasattr(self, "_loaded_file_data") or not self._loaded_file_data:
            return
        ctx = self._get_project_context()
        if ctx:
            from tiktoken import get_encoding
            enc = get_encoding("cl100k_base")
            self._project_context_tokens = len(enc.encode(ctx))
        else:
            self._project_context_tokens = 0
        self._on_file_selection_change()

    def _on_mode_change(self, value: str):
        if value == "Batch (50% off)":
            self._mode_hint.configure(text="Queued processing \u2022 results in ~15-60 min")
            self.run_button.configure(text="Submit Batch")
        else:
            self._mode_hint.configure(text="")
            self.run_button.configure(text="Run Review")

    @property
    def _is_batch_mode(self) -> bool:
        return self.mode_selector.get() == "Batch (50% off)"

    def _toggle_inputs_card(self, event=None):
        if self._inputs_expanded:
            self.inputs_content.pack_forget(); self.inputs_expand_label.configure(text="\u25b6"); self._inputs_expanded = False
        else:
            self.inputs_content.pack(fill="x", padx=16, pady=(0, 16)); self.inputs_expand_label.configure(text="\u25bc"); self._inputs_expanded = True

    def _browse_files(self):
        files = filedialog.askopenfilenames(
            title="Select specification files",
            filetypes=_SPEC_FILETYPES,
        )
        if files:
            paths = [Path(f) for f in files if _is_supported_spec(Path(f))]
            if not paths: self.log.log_warning("No supported files selected (.docx or .pdf)"); return
            self._selected_files = paths
            self.input_dir = paths[0].parent
            self.input_dir_entry.delete(0, "end")
            self.input_dir_entry.insert(0, str(paths[0]) if len(paths) == 1 else f"{len(paths)} files selected")
            self._analyze_tokens(paths)

    def _analyze_tokens(self, file_paths):
        if not file_paths:
            self.log.log_warning("No supported files found"); self.token_gauge.reset(); self.file_list_panel.reset(); return
        self.log.log_step(f"Analyzing {len(file_paths)} files...")

        def analyze():
            try:
                file_data = []
                processed_names: list[str] = []
                from tiktoken import get_encoding
                enc = get_encoding("cl100k_base")
                self._system_prompt_tokens = len(enc.encode(get_system_prompt()))
                ctx = self._get_project_context()
                self._project_context_tokens = len(enc.encode(ctx)) if ctx else 0

                extracted_specs: list[ExtractedSpec] = []
                for f in file_paths:
                    try:
                        spec = extract_text(f)
                        tokens = len(enc.encode(spec.content))
                        file_data.append({"path": f, "filename": spec.filename, "tokens": tokens, "content": spec.content})
                        processed_names.append(f.name)
                        extracted_specs.append(spec)
                    except Exception as e:
                        self.after(0, lambda err=str(e), n=f.name: self.log.log_warning(f"Could not read {n}: {err}"))

                if processed_names:
                    self.after(0, lambda names=processed_names: self.log.log_file_batch(names))

                if file_data:
                    self._loaded_file_data = file_data
                    self._extracted_specs = extracted_specs
                    overhead = self._system_prompt_tokens + self._project_context_tokens
                    total = overhead + sum(d["tokens"] for d in file_data)
                    max_per_file = max(d["tokens"] for d in file_data)
                    per_file_limit_exceeded = (overhead + max_per_file) > RECOMMENDED_MAX
                    self.after(0, lambda: self.file_list_panel.load_files(file_data))
                    self.after(0, lambda: self.token_gauge.update_gauge(total, len(file_data)))
                    self.after(0, lambda: self.log.log_success(f"Token analysis complete: {total:,} tokens"))
                    if per_file_limit_exceeded:
                        # Find the offending file(s) for a clear message
                        over_files = [d["filename"] for d in file_data if (overhead + d["tokens"]) > RECOMMENDED_MAX]
                        self.after(0, lambda of=over_files: self.log.log_warning(
                            f"File too large for single API call: {', '.join(of)}"
                        ))
                    self.after(0, lambda b=per_file_limit_exceeded: self.run_button.configure(
                        state="disabled" if b else "normal"
                    ))
                    self.after(0, lambda b=per_file_limit_exceeded: self.file_list_panel.set_over_limit(b))
            except Exception as e:
                self.after(0, lambda: self.log.log_error(f"Analysis failed: {e}"))

        threading.Thread(target=analyze, daemon=True).start()

    def _on_file_selection_change(self):
        if not hasattr(self, "_loaded_file_data") or not self._loaded_file_data: return
        sel = set(self.file_list_panel.get_selected_files())
        selected_data = [d for d in self._loaded_file_data if d["path"] in sel]
        overhead = (
            getattr(self, "_system_prompt_tokens", 0)
            + getattr(self, "_project_context_tokens", 0)
        )
        total = overhead + sum(d["tokens"] for d in selected_data)
        fc = len(selected_data)
        self.token_gauge.update_gauge(total, fc)
        # Block only if any single file exceeds the per-call limit
        if fc > 0:
            max_per_file = max(d["tokens"] for d in selected_data)
            per_file_exceeded = (overhead + max_per_file) > RECOMMENDED_MAX
        else:
            per_file_exceeded = False
        self.run_button.configure(state="normal" if (fc > 0 and not per_file_exceeded) else "disabled")
        self.file_list_panel.set_over_limit(per_file_exceeded)

    def _validate_inputs(self):
        if not self.api_key_entry.get().strip(): self.log.log_error("API key is required"); return False
        if not hasattr(self, "_selected_files") or not self._selected_files: self.log.log_error("Select specification files (.docx or .pdf)"); return False
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
        self._cross_check_for_review = self._cross_check_var.get()
        self._model_for_review = self._selected_review_model
        self._export_mode_for_review = self._is_export_mode
        self.is_processing = True
        self.report_panel.clear()
        self._close_report_window()
        self.log.log("\u2500" * 40, level="muted", timestamp=False, paced=False)
        self.run_button.set_processing()
        self.progress_bar.pack(fill="x", pady=(8, 0), after=self.run_button)
        self.progress_bar.set(0); self.progress_bar.configure(mode="determinate")
        os.environ["ANTHROPIC_API_KEY"] = self.api_key_entry.get().strip()

        n = len(self._selected_files_for_review)
        model_label = self._review_model_var.get()
        output_label = " → Export Report" if self._export_mode_for_review else ""
        if self._is_batch_mode:
            self.log.log_step(f"Submitting {n} files for batch review ({model_label}){output_label}...")
            threading.Thread(target=self._submit_batch_thread, daemon=True).start()
        else:
            self.log.log_step(f"Reviewing {n} files ({model_label}){output_label}...")
            threading.Thread(target=self._run_review_thread, daemon=True).start()

    def _run_review_thread(self):
        try:
            n = len(self._selected_files_for_review)
            self.after(0, lambda: self.log.log_step("Starting per-spec review..."))
            model_label = [k for k, v in REVIEW_MODELS.items() if v == self._model_for_review][0]
            cross_check_note = " + cross-check" if self._cross_check_for_review else ""
            mode_info = f"Model: {model_label}  \u2022  {n} specs \u2022  1 API call per spec  \u2022  verification enabled{cross_check_note}"
            self.after(0, lambda: self.log.log(mode_info, level="muted"))

            def _on_progress(pct, msg):
                self.after(0, lambda m=msg: self.log.log_step(m))
                self.after(0, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

            result = run_review(
                input_dir=self.input_dir,
                files=self._selected_files_for_review,
                project_context=self._project_context_for_review,
                model=self._model_for_review,
                verify=True,
                cross_check=self._cross_check_for_review,
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
            if result.cross_check_result and result.cross_check_result.findings:
                cc = result.cross_check_result
                self.log.log(f"Cross-check: {len(cc.findings)} coordination issues found", level="info")
            self.log.log(f"Time: {rv.elapsed_seconds:.1f}s", level="muted")

            # Route to export or in-app rendering based on output mode
            if getattr(self, "_export_mode_for_review", False):
                self._export_report_to_file(result)
            else:
                self._open_report_window(rv, result.files_reviewed, result.leed_alerts, result.placeholder_alerts, result.cross_check_result)

        delete_batch_state()
        self.run_button.set_complete()
        self.after(2500, self._reset_ui)

    def _export_report_to_file(self, result):
        """Show save dialog and export the report to a .docx file.

        Called instead of _open_report_window() when Export Report mode
        is active. The in-app ReportPanel and pop-out ReportWindow are
        NOT rendered — this is the key performance fix for large reviews.

        Args:
            result: PipelineResult from the review pipeline
        """
        default_name = f"spec-critic-report-{datetime.now().strftime('%Y-%m-%d')}.docx"
        path = filedialog.asksaveasfilename(
            title="Save Review Report",
            defaultextension=".docx",
            filetypes=[("Word Documents", "*.docx"), ("All Files", "*.*")],
            initialfile=default_name,
        )
        if not path:
            self.log.log_warning("Export canceled — no file saved")
            return

        try:
            output_path = Path(path)
            self.log.log_step(f"Exporting report to {output_path.name}...")
            export_report(
                result,
                output_path,
                project_context=getattr(self, "_project_context_for_review", ""),
            )
            self.log.log_success(f"Report saved: {output_path}")
        except Exception as e:
            self.log.log_error(f"Export failed: {e}")

    def _on_review_error(self, err):
        self.progress_bar.pack_forget()
        self.log.log_error(f"Review failed: {err}")
        self.run_button.set_ready(); self.is_processing = False

    # ----- Batch mode -----

    def _submit_batch_thread(self):
        try:
            def _on_progress(pct, msg):
                self.after(0, lambda m=msg: self.log.log_step(m))
                self.after(0, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

            submission = start_batch_review(
                input_dir=self.input_dir,
                files=self._selected_files_for_review,
                project_context=self._project_context_for_review,
                model=self._model_for_review,
                log=lambda msg: self.after(0, lambda m=msg: self.log.log(m, level="info")),
                progress=_on_progress,
            )
            save_batch_state(submission, phase="review")
            self.after(0, lambda: self._on_batch_submitted(submission))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            self.after(0, lambda: self._on_review_error(err))

    def _on_batch_submitted(self, submission: BatchSubmission):
        self._batch_submission = submission
        self.progress_bar.set(0.4)
        self.log.log_success(f"Batch submitted: {submission.job.batch_id}")
        self.log.log(f"  {len(submission.files_reviewed)} specs queued \u2022 50% cost savings", level="muted")
        self.log.log_step("Polling for results (typically 15-60 min)...")
        self.run_button.configure(text="Polling...")
        self._poll_batch()

    def _poll_batch(self):
        if self._batch_submission is None:
            return
        def _do_poll():
            try:
                status = poll_batch(self._batch_submission.job.batch_id)
                self.after(0, lambda: self._on_poll_result(status))
            except Exception as e:
                self.after(0, lambda: self.log.log_warning(f"Poll error (retrying): {e}"))
                self.after(0, lambda: self._schedule_next_poll(30_000))
        threading.Thread(target=_do_poll, daemon=True).start()

    def _on_poll_result(self, status: BatchStatus):
        batch_pct = 0.40 + (status.progress_pct / 100.0) * 0.55
        self.progress_bar.set(min(batch_pct, 0.95))
        self.log.log(
            f"  Batch: {status.succeeded} done, {status.processing} processing, "
            f"{status.errored} errors \u2022 {status.progress_pct:.0f}%",
            level="info", paced=False,
        )
        if status.status == "ended":
            self.log.log_success("Batch complete — collecting results...")
            self._collect_batch_results()
        elif status.status in ("canceling",):
            self.log.log_warning("Batch is being canceled...")
            self._schedule_next_poll(5_000)
        elif status.status in ("failed", "expired", "canceled"):
            # Terminal failure — stop polling, inform user, clean up
            self.log.log_error(f"Batch terminated with status: {status.status}")
            self.log.log_warning("No results to collect. Clearing batch state.")
            delete_batch_state()
            self._batch_submission = None
            self._reset_ui()
        else:
            # Unknown status — keep polling but warn so we notice new statuses
            self.log.log_warning(f"Unexpected batch status: {status.status} — continuing to poll...")
            self._schedule_next_poll(15_000)

    def _schedule_next_poll(self, delay_ms: int):
        self._batch_poll_id = self.after(delay_ms, self._poll_batch)

    def _collect_batch_results(self):
        def _do_collect():
            try:
                def _on_progress(pct, msg):
                    self.after(0, lambda m=msg: self.log.log_step(m))
                    self.after(0, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

                cross_check = getattr(self, "_cross_check_for_review", False)
                project_context = getattr(self, "_project_context_for_review", "")

                # Build specs list for cross-check, filtered to reviewed files
                specs_for_cross_check = None
                if cross_check:
                    available_specs = getattr(self, "_extracted_specs", [])
                    if available_specs:
                        reviewed_set = set(self._batch_submission.files_reviewed)
                        specs_for_cross_check = [
                            s for s in available_specs
                            if s.filename in reviewed_set
                        ]
                    if not specs_for_cross_check:
                        self.after(0, lambda: self.log.log_warning(
                            "Cross-check skipped: spec content not available (resumed batch or files changed)."
                        ))

                result = collect_batch_results(
                    self._batch_submission,
                    verify=True,
                    cross_check=cross_check,
                    specs=specs_for_cross_check,
                    project_context=project_context,
                    log=lambda msg: self.after(0, lambda m=msg: self.log.log(m, level="info")),
                    progress=_on_progress,
                )

                self.after(0, lambda: self._on_review_complete(result))
            except Exception as e:
                import traceback
                err = f"{e}\n{traceback.format_exc()}"
                self.after(0, lambda: self._on_review_error(err))
        threading.Thread(target=_do_collect, daemon=True).start()

    def _reset_ui(self):
        self.run_button.set_ready()
        if self._is_batch_mode:
            self.run_button.configure(text="Submit Batch")
        self.progress_bar.pack_forget()
        self.is_processing = False
        self._batch_submission = None

    # ----- Persistent batch state (Chunk 5) -----

    def _check_pending_batch(self):
        """Check for a saved batch state file on app launch."""
        loaded = load_batch_state()
        if loaded is None:
            return

        submission, phase = loaded
        age_str = self._format_batch_age(submission.job.created_at)

        dialog = ctk.CTkToplevel(self)
        dialog.title("Pending Batch Found")
        dialog.geometry("480x220")
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

        inner = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
        inner.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(inner, text="A batch submission is pending",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=COLORS["text_primary"]).pack(anchor="w", padx=16, pady=(16, 4))

        model_label = [k for k, v in REVIEW_MODELS.items() if v == getattr(submission, "model", MODEL_OPUS_46)]
        model_str = model_label[0] if model_label else "Unknown"
        info_text = (
            f"Batch ID: {submission.job.batch_id[:30]}...\n"
            f"Files: {len(submission.files_reviewed)} specs  \u2022  Model: {model_str}\n"
            f"Submitted: {age_str}  \u2022  Phase: {phase}"
        )
        ctk.CTkLabel(inner, text=info_text, font=ctk.CTkFont(family="Consolas", size=11),
            text_color=COLORS["text_secondary"], justify="left").pack(anchor="w", padx=16, pady=(0, 12))

        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))
        btn_kw = {"height": 34, "font": ctk.CTkFont(family="Segoe UI", size=12), "corner_radius": 6}

        def _resume():
            dialog.destroy()
            self._resume_batch(submission, phase)

        def _discard():
            dialog.destroy()
            delete_batch_state()
            self.log.log("Discarded pending batch state.", level="muted")

        ctk.CTkButton(btn_frame, text="Resume Batch", width=140,
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=_resume, **btn_kw).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_frame, text="Discard", width=100,
            fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_secondary"], command=_discard, **btn_kw).pack(side="left")

    def _format_batch_age(self, created_at: float) -> str:
        try:
            age_seconds = time.time() - created_at
            if age_seconds < 3600:
                return f"{int(age_seconds / 60)} minutes ago"
            elif age_seconds < 86400:
                return f"{age_seconds / 3600:.1f} hours ago"
            else:
                return f"{age_seconds / 86400:.1f} days ago"
        except Exception:
            return "unknown time"

    def _resume_batch(self, submission: BatchSubmission, phase: str):
        """Resume polling a previously submitted batch."""
        # Ensure API key is set
        api_key = self.api_key_entry.get().strip()
        if not api_key:
            api_key = load_api_key_from_file() or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            self.log.log_error("API key is required to resume batch. Enter your key and try again.")
            return

        os.environ["ANTHROPIC_API_KEY"] = api_key

        self._batch_submission = submission
        # Cross-check is not available for resumed batches (no ExtractedSpec objects)
        self._cross_check_for_review = False
        self._project_context_for_review = ""
        # Resumed batches default to export mode (safest for potentially large results)
        self._export_mode_for_review = self._is_export_mode
        self.is_processing = True

        self.log.log("\u2500" * 40, level="muted", timestamp=False, paced=False)
        self.log.log_step(f"Resuming batch: {submission.job.batch_id}")
        self.log.log(f"  {len(submission.files_reviewed)} specs \u2022 Phase: {phase}", level="muted")

        self.run_button.set_processing()
        self.run_button.configure(text="Polling...")
        self.progress_bar.pack(fill="x", pady=(8, 0), after=self.run_button)
        self.progress_bar.set(0.4)
        self.progress_bar.configure(mode="determinate")

        # Start polling
        self._poll_batch()

    # ----- Pop-out report window -----

    def _open_report_window(self, review, files_reviewed, leed_alerts, placeholder_alerts, cross_check_result=None):
        self._close_report_window()
        self._report_window = ReportWindow(
            self, review=review, files_reviewed=files_reviewed,
            leed_alerts=leed_alerts, placeholder_alerts=placeholder_alerts,
            project_context=getattr(self, "_project_context_for_review", ""),
            cross_check_result=cross_check_result,
        )

    def _close_report_window(self):
        if self._report_window is not None:
            try: self._report_window.destroy()
            except Exception: pass
            self._report_window = None

    # ----- Report expand / collapse mode -----

    def _enter_report_mode(self):
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
        self._report_mode = False
        self.report_toolbar.pack_forget()
        self.hdr.pack(fill="x", pady=(0, 20))
        self.inputs_card.pack(fill="x")
        if self.file_list_panel._file_data:
            self.file_list_panel.pack(fill="x", pady=(16, 0), after=self.inputs_card)
        self.token_gauge.pack(fill="x", pady=(16, 0))
        self.run_button.pack(fill="x", pady=(16, 0))
        self.log.pack(fill="both", expand=True, pady=(16, 0))

    def _reset_for_new_review(self):
        if self._report_mode:
            self._report_mode = False
            self.report_toolbar.pack_forget()

        if self._batch_poll_id is not None:
            self.after_cancel(self._batch_poll_id)
            self._batch_poll_id = None
        self._batch_submission = None

        self._close_report_window()
        self.report_panel.clear()
        self.progress_bar.pack_forget()

        self.input_dir = None
        self._selected_files = []
        self._loaded_file_data = []
        self._extracted_specs = []
        self._project_context_tokens = 0
        self.input_dir_entry.delete(0, "end")

        self.context_textbox.delete("1.0", "end")
        self._context_has_placeholder = True
        self.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
        self.context_textbox.configure(text_color=COLORS["text_muted"])

        self.token_gauge.reset()
        self.file_list_panel.reset()
        self.log.clear()
        self.run_button.set_ready()
        self.run_button.configure(text="Run Review")
        self.model_selector.set("Opus 4.6")
        self._model_hint.configure(text="Most thorough \u2022 recommended")
        self.mode_selector.set("Real-time")
        self._mode_hint.configure(text="")
        self.output_selector.set("View in App")
        self._output_hint.configure(text="")
        self._cross_check_var.set(False)
        self.is_processing = False

        self.hdr.pack(fill="x", pady=(0, 20))
        self.inputs_card.pack(fill="x")
        self.token_gauge.pack(fill="x", pady=(16, 0))
        self.run_button.pack(fill="x", pady=(16, 0))
        self.log.pack(fill="both", expand=True, pady=(16, 0))
        if not self._inputs_expanded:
            self._toggle_inputs_card()

    # ----- About / How It Works dialog -----

    def _show_about_dialog(self):
        """Show a modal dialog explaining how Spec Critic works."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("How Spec Critic Works")
        dialog.geometry("620x640")
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.resizable(True, True)
        dialog.minsize(500, 500)
        dialog.transient(self)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

        outer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            outer, text="How Spec Critic Works",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=20, pady=(20, 4))

        ctk.CTkLabel(
            outer, text="AI-assisted M&P specification review for California K-12 DSA projects",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=20, pady=(0, 12))

        # Scrollable content
        scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        sections = [
            ("1.  Text Extraction", (
                "Your .docx and .pdf files are read locally. Paragraphs and tables are "
                "extracted \u2014 nothing is sent to Claude yet."
            )),
            ("2.  Local Pre-Screening", (
                "Before any API calls, the tool scans for LEED references and unresolved "
                "placeholders (like [EDIT] or [VERIFY]). These are flagged as alerts and "
                "don\u2019t cost any tokens."
            )),
            ("3.  Per-Spec Review", (
                "Each specification is sent individually to Claude (Opus 4.6 or Sonnet 4.6, "
                "your choice). Claude checks for code compliance issues (CBC, CMC, CPC, "
                "Energy Code, CALGreen), DSA-specific requirements, outdated standards, "
                "coordination problems, and constructability concerns. Each finding is "
                "assigned a severity (Critical, High, Medium, or Gripe) and a confidence score."
            )),
            ("4.  Deduplication", (
                "When the same issue appears across multiple specs \u2014 like an outdated "
                "seismic code reference \u2014 duplicates are consolidated into a single "
                "finding that lists all affected files."
            )),
            ("5.  Cross-Spec Coordination  (optional)", (
                "If enabled, a separate pass analyzes how your specs relate to each other. "
                "It catches contradictions between specs, missing cross-references, scope "
                "gaps and overlaps, and inconsistent equipment data. This uses a cheaper "
                "model (Sonnet 4.6) and only looks at section headers and existing "
                "findings \u2014 not the full spec text."
            )),
            ("6.  Verification", (
                "Every Critical, High, and Medium finding is independently verified by a "
                "second Claude call with web search access. The verifier checks whether "
                "the cited code or standard actually says what the finding claims. Each "
                "finding gets a verdict: Confirmed, Corrected, Disputed, or Unverified."
            )),
        ]

        for title, body in sections:
            ctk.CTkLabel(
                scroll, text=title,
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                text_color=COLORS["text_primary"],
            ).pack(anchor="w", padx=8, pady=(10, 2))
            ctk.CTkLabel(
                scroll, text=body,
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=COLORS["text_secondary"],
                wraplength=520, justify="left",
            ).pack(anchor="w", padx=8, pady=(0, 4))

        # Disclaimer
        ctk.CTkLabel(
            scroll, text="What it doesn\u2019t do",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=8, pady=(14, 2))
        ctk.CTkLabel(
            scroll,
            text=(
                "Spec Critic is a review assistant \u2014 it doesn\u2019t modify your "
                "documents. It\u2019s advisory only and not a substitute for AHJ review. "
                "Code citations should still be spot-checked by the engineer of record."
            ),
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_secondary"],
            wraplength=520, justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 10))

        # Close button
        ctk.CTkButton(
            outer, text="Close", width=100, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=dialog.destroy,
        ).pack(pady=(0, 16))


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    SpecReviewApp().mainloop()

if __name__ == "__main__":
    main()