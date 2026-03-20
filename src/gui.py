"""
Spec Critic - Modern GUI with CustomTkinter
M&P Specification Review • California K-12 DSA • Claude Opus 4.6
v2.3.0 - Opus-only pipeline, real-time cost confirmation, updated mode labels
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

from src.pipeline import (
    run_review,
    start_batch_review,
    collect_review_batch_results,
    run_cross_check_for_batch,
    prepare_verification_work,
    start_batch_verification,
    collect_batch_verification_results,
    finalize_batch_result,
    BatchSubmission,
    CollectedBatchState,
)
from src.batch import poll_batch, BatchStatus, BatchJob
from src.reviewer import MODEL_OPUS_46, REVIEW_MODELS
from src.extractor import extract_text, ExtractedSpec, SUPPORTED_EXTENSIONS
from src.tokenizer import RECOMMENDED_MAX, exceeds_per_call_limit
from src.prompts import get_system_prompt
from src.code_cycles import AVAILABLE_CYCLES, DEFAULT_CYCLE
from src.report_exporter import export_report
from src.resume_state import (
    PHASE_REVIEW_POLL,
    PHASE_REVIEW_COLLECT,
    PHASE_VERIFICATION_POLL,
    PHASE_FINALIZE,
    SUPPORTED_PHASES,
    build_resume_state,
    deserialize_resume_state,
)

from src.widgets import (COLORS, TokenGauge, FileListPanel, EnhancedLog, AnimatedButton, ReportWindow)

from platformdirs import user_config_dir, user_state_dir

API_KEY_FILENAME = "spec_critic_api_key.txt"
BATCH_STATE_FILENAME = "batch_state.json"

BATCH_STATE_MAX_AGE_HOURS = 24

_CONTEXT_PLACEHOLDER = "Describe your project (optional)"

_SPEC_FILETYPES = [
    ("Word Specifications", "*.docx"),
    ("All Files", "*.*"),
]

# Mode selector labels
_MODE_REALTIME = "Real-time (FAST: Expensive!)"
_MODE_BATCH = "Batch (SLOW: Cheap!)"
_BATCH_TIMING_COPY = "Usually 15 to 30 min, 24 hrs maximum (Extremely Rare)"

_FONT_SCALE_OPTIONS = {
    "Default (100%)": 1.0,
    "Large (+10%)": 1.1,
    "Larger (+20%)": 1.2,
}


def _app_config_dir() -> Path:
    d = Path(user_config_dir("SpecCritic", appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _app_state_dir() -> Path:
    d = Path(user_state_dir("SpecCritic", appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_api_key_from_file():
    kf = _app_config_dir() / API_KEY_FILENAME
    if kf.exists():
        try:
            return kf.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    kf = exe_dir / API_KEY_FILENAME
    if kf.exists():
        try:
            return kf.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# Persistent batch state
# ---------------------------------------------------------------------------

def _batch_state_path() -> Path:
    return _app_state_dir() / BATCH_STATE_FILENAME


def save_batch_state(state: dict) -> None:
    try:
        _batch_state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[SpecCritic] Warning: Could not save batch state: {e}")


def load_batch_state() -> Optional[dict]:
    path = _batch_state_path()
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        delete_batch_state()
        return None
    try:
        saved_at = datetime.fromisoformat(state["saved_at"])
        age_hours = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600
        if age_hours > BATCH_STATE_MAX_AGE_HOURS:
            delete_batch_state()
            return None
    except Exception:
        delete_batch_state()
        return None
    try:
        restored = deserialize_resume_state(state)
        submission = restored["submission"]
        if not isinstance(submission.job.batch_id, str) or not submission.job.batch_id.startswith("msgbatch_"):
            delete_batch_state()
            return None
        return restored
    except (KeyError, TypeError, ValueError):
        # Intentionally retained for upgrade continuity with older installed versions
        # that persisted pre-resume-state (v1) payloads.
        try:
            batch_id = state.get("batch_id", "")
            if not isinstance(batch_id, str) or not batch_id.startswith("msgbatch_"):
                delete_batch_state()
                return None
            legacy_submission = BatchSubmission(
                job=BatchJob(
                    batch_id=batch_id,
                    job_type=state.get("job_type", "review"),
                    request_map=state["request_map"],
                    created_at=state["created_at"],
                ),
                files_reviewed=state.get("files_reviewed", []),
                review_request_ids=state.get("review_request_ids", []),
                leed_alerts=state.get("leed_alerts", []),
                placeholder_alerts=state.get("placeholder_alerts", []),
                model=MODEL_OPUS_46,
                project_context=state.get("project_context", ""),
                cycle_label=state.get("code_cycle", DEFAULT_CYCLE.label),
                cross_check_enabled=state.get("cross_check_enabled", False),
                export_mode=state.get("export_mode", False),
                prepared_specs=[ExtractedSpec(**s) for s in (state.get("prepared_specs") or [])] if state.get("prepared_specs") else None,
            )
            phase = state.get("phase", "review")
            phase_map = {"review": PHASE_REVIEW_POLL}
            migrated_phase = phase_map.get(phase, phase)
            return {"phase": migrated_phase, "submission": legacy_submission, "resume_flags": {}}
        except Exception:
            delete_batch_state()
            return None


def delete_batch_state() -> None:
    try:
        path = _batch_state_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _is_supported_spec(filepath: Path) -> bool:
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
        self._report_window: Optional[ReportWindow] = None
        self._project_context_tokens = 0
        self._batch_submission: Optional[BatchSubmission] = None
        self._batch_poll_id: Optional[str] = None
        self._run_epoch = 0
        self._extracted_specs: list[ExtractedSpec] = []
        fk = load_api_key_from_file()
        ek = os.environ.get("ANTHROPIC_API_KEY", "")
        self.api_key = fk if fk else ek
        self._selected_files: list[Path] = []
        self._loaded_file_data: list[dict] = []
        self._system_prompt_tokens: int = 0
        self._selected_files_for_review: list[Path] = []
        self._project_context_for_review: str = ""
        self._cross_check_for_review: bool = False
        self._export_mode_for_review: bool = False
        self._last_result = None
        self._poll_consecutive_errors: int = 0
        self._realtime_confirmed: bool = False
        self._context_debounce_id: str | None = None
        self._selected_cycle_label: str = DEFAULT_CYCLE.label
        self._font_scale_label: str = "Default (100%)"
        self._create_ui()
        self.after(500, self._check_pending_batch)

    def _create_ui(self):
        c = ctk.CTkFrame(self, fg_color="transparent")
        c.pack(fill="both", expand=True, padx=24, pady=24)
        self.container = c

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
        ctk.CTkButton(
            hdr_title_row, text="How to Use", width=100, height=30,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_secondary"], command=self._show_usage_dialog,
        ).pack(side="right", padx=(0, 8), pady=(4, 0))
        ctk.CTkLabel(self.hdr, text="M&P Specification Review  \u2022  California K-12 DSA  \u2022  Opus 4.6", font=ctk.CTkFont(family="Segoe UI", size=13), text_color=COLORS["text_secondary"]).pack(anchor="w", pady=(4, 0))

        self._create_inputs_card(c)
        self.file_list_panel = FileListPanel(c, on_selection_change=self._on_file_selection_change, pack_after=self.inputs_card)
        self.token_gauge = TokenGauge(c, max_tokens=RECOMMENDED_MAX)
        self.token_gauge.pack(fill="x", pady=(16, 0))
        self.run_button = AnimatedButton(c, text="Run Review", command=self.start_review)
        self.run_button.pack(fill="x", pady=(16, 0))
        self.progress_bar = ctk.CTkProgressBar(c, height=4, corner_radius=2, fg_color=COLORS["bg_input"], progress_color=COLORS["accent"], indeterminate_speed=0.5)
        self.progress_bar.set(0)
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
        ctx_label_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        ctx_label_frame.grid(row=2, column=0, sticky="nw", pady=8)
        ctk.CTkLabel(ctx_label_frame, text="Project Context", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="nw").pack(anchor="nw")
        ctk.CTkButton(ctx_label_frame, text="Expand", width=60, height=24, font=ctk.CTkFont(size=11), fg_color=COLORS["bg_input"], hover_color=COLORS["border"], border_width=1, border_color=COLORS["border"], text_color=COLORS["text_secondary"], command=self._open_context_modal).pack(anchor="nw", pady=(4, 0))
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

        # --- Row 3: Review Mode ---
        ctk.CTkLabel(self.inputs_content, text="Mode", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=3, column=0, sticky="w", pady=8)
        mode_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        mode_frame.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=8)
        self.mode_selector = ctk.CTkSegmentedButton(
            mode_frame, values=[_MODE_REALTIME, _MODE_BATCH],
            command=self._on_mode_change, font=ctk.CTkFont(family="Segoe UI", size=11),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_secondary"],
            text_color_disabled=COLORS["text_muted"], height=32,
        )
        self.mode_selector.set(_MODE_REALTIME)
        self.mode_selector.pack(side="left")
        self._mode_hint = ctk.CTkLabel(mode_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=10), text_color=COLORS["text_muted"])
        self._mode_hint.pack(side="left", padx=(12, 0))

        # --- Row 4: Code Cycle ---
        ctk.CTkLabel(self.inputs_content, text="Code Cycle", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=6, column=0, sticky="w", pady=8)
        cycle_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        cycle_frame.grid(row=6, column=1, sticky="w", padx=(8, 0), pady=8)
        self.cycle_selector = ctk.CTkSegmentedButton(cycle_frame, values=["2022", "2025"], font=ctk.CTkFont(family="Segoe UI", size=11), selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"], unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"], fg_color=COLORS["bg_input"], text_color=COLORS["text_secondary"], height=32)
        self.cycle_selector.set(DEFAULT_CYCLE.label)
        self.cycle_selector.pack(side="left")

        # --- Row 5: Output ---
        ctk.CTkLabel(self.inputs_content, text="Output", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=4, column=0, sticky="w", pady=8)
        output_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        output_frame.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=8)
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

        # --- Row 5: Options (cross-check) ---
        ctk.CTkLabel(self.inputs_content, text="Options", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=5, column=0, sticky="w", pady=8)
        options_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        options_frame.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=8)
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
            text="Opus 4.6 \u2022 full content \u2022 finds inter-spec conflicts",
            font=ctk.CTkFont(family="Segoe UI", size=10), text_color=COLORS["text_muted"])
        self._cross_check_hint.pack(side="left", padx=(12, 0))

        # --- Row 7: Accessibility / Font scaling ---
        ctk.CTkLabel(self.inputs_content, text="Accessibility", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=7, column=0, sticky="w", pady=8)
        accessibility_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        accessibility_frame.grid(row=7, column=1, sticky="w", padx=(8, 0), pady=8)
        self._font_scale_var = ctk.StringVar(value=self._font_scale_label)
        self.font_size_selector = ctk.CTkSegmentedButton(
            accessibility_frame,
            values=list(_FONT_SCALE_OPTIONS.keys()),
            variable=self._font_scale_var,
            command=self._on_font_scale_change,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_secondary"],
            text_color_disabled=COLORS["text_muted"], height=32,
        )
        self.font_size_selector.set(self._font_scale_label)
        self.font_size_selector.pack(side="left")

        self.inputs_content.columnconfigure(1, weight=1)

    # --- Output mode helpers ---

    def _on_output_mode_change(self, value: str):
        if value == "Export Report":
            self._output_hint.configure(text="Saves .docx report \u2022 no in-app rendering")
        else:
            self._output_hint.configure(text="")

    def _on_font_scale_change(self, value: str):
        scale = _FONT_SCALE_OPTIONS.get(value, 1.0)
        ctk.set_widget_scaling(scale)
        self._font_scale_label = value

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
            self._on_context_change()

    def _get_project_context(self) -> str:
        if self._context_has_placeholder:
            return ""
        return self.context_textbox.get("1.0", "end").strip()

    def _on_context_change(self, event=None):
        if self._context_debounce_id is not None:
            self.after_cancel(self._context_debounce_id)
        self._context_debounce_id = self.after(300, self._do_context_change)

    def _do_context_change(self):
        self._context_debounce_id = None
        if not self._loaded_file_data:
            return
        ctx = self._get_project_context()
        if ctx:
            from tiktoken import get_encoding
            enc = get_encoding("cl100k_base")
            self._project_context_tokens = len(enc.encode(ctx))
        else:
            self._project_context_tokens = 0
        self._on_file_selection_change()

    def _open_context_modal(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Project Context")
        dialog.geometry("700x500")
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.resizable(True, True)
        dialog.minsize(400, 300)
        dialog.transient(self)
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

        current = self._get_project_context()
        if current:
            modal_textbox.insert("1.0", current)

        def _save_and_close():
            new_text = modal_textbox.get("1.0", "end").strip()
            self.context_textbox.delete("1.0", "end")
            if new_text:
                self._context_has_placeholder = False
                self.context_textbox.configure(text_color=COLORS["text_primary"])
                self.context_textbox.insert("1.0", new_text)
            else:
                self._context_has_placeholder = True
                self.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
                self.context_textbox.configure(text_color=COLORS["text_muted"])
            self._on_context_change()
            dialog.destroy()

        ctk.CTkButton(
            outer, text="Save & Close", width=120, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=_save_and_close,
        ).pack(anchor="e", padx=16, pady=(0, 16))

    def _on_mode_change(self, value: str):
        if value == _MODE_BATCH:
            self._mode_hint.configure(text=f"Queued processing \u2022 {_BATCH_TIMING_COPY}")
            self.run_button.configure(text="Submit Batch")
        else:
            self._mode_hint.configure(text="")
            self.run_button.configure(text="Run Review")

    @property
    def _is_batch_mode(self) -> bool:
        return self.mode_selector.get() == _MODE_BATCH

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
            if not paths: self.log.log_warning("No supported .docx files selected"); return
            self._selected_files = paths
            self.input_dir = paths[0].parent
            self.input_dir_entry.delete(0, "end")
            self.input_dir_entry.insert(0, str(paths[0]) if len(paths) == 1 else f"{len(paths)} files selected")
            self._analyze_tokens(paths)

    def _clear_file_state(self):
        self._loaded_file_data = []
        self._extracted_specs = []
        self.file_list_panel.reset()
        self.token_gauge.reset()
        self.run_button.configure(state="disabled")

    def _set_file_data(self, file_data, extracted_specs, sys_tokens, ctx_tokens):
        self._loaded_file_data = file_data
        self._extracted_specs = extracted_specs
        self._system_prompt_tokens = sys_tokens
        self._project_context_tokens = ctx_tokens

    def _analyze_tokens(self, file_paths):
        if not file_paths:
            self.log.log_warning("No supported files found"); self.token_gauge.reset(); self.file_list_panel.reset(); return
        self.log.log_step(f"Analyzing {len(file_paths)} files...")
        project_context = self._get_project_context()

        def analyze():
            try:
                self.after(0, lambda: self._clear_file_state())
                file_data = []
                processed_names: list[str] = []
                from tiktoken import get_encoding
                enc = get_encoding("cl100k_base")
                sys_tokens = len(enc.encode(get_system_prompt(AVAILABLE_CYCLES.get(self.cycle_selector.get(), DEFAULT_CYCLE))))
                ctx_tokens = len(enc.encode(project_context)) if project_context else 0
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
                    self.after(0, lambda fd=file_data, es=extracted_specs, st=sys_tokens, ct=ctx_tokens:
                        self._set_file_data(fd, es, st, ct))
                    overhead = sys_tokens + ctx_tokens
                    max_per_file = max(d["tokens"] for d in file_data)
                    largest_call = overhead + max_per_file
                    per_file_limit_exceeded = exceeds_per_call_limit(max_per_file, overhead)
                    self.after(0, lambda fd=file_data: self.file_list_panel.load_files(fd))
                    self.after(0, lambda lc=largest_call, fc=len(file_data): self.token_gauge.update_gauge(lc, fc))
                    self.after(0, lambda lc=largest_call: self.log.log_success(f"Token analysis complete: largest spec call ~{lc:,} tokens"))
                    if per_file_limit_exceeded:
                        over_files = [d["filename"] for d in file_data if exceeds_per_call_limit(d["tokens"], overhead)]
                        self.after(0, lambda of=over_files: self.log.log_warning(
                            f"File too large for single API call: {', '.join(of)}"
                        ))
                    self.after(0, lambda b=per_file_limit_exceeded: self.run_button.configure(
                        state="disabled" if b else "normal"
                    ))
                    self.after(0, lambda b=per_file_limit_exceeded: self.file_list_panel.set_over_limit(b))
            except Exception as e:
                self.after(0, lambda err=e: self.log.log_error(f"Analysis failed: {err}"))

        threading.Thread(target=analyze, daemon=True).start()

    def _on_file_selection_change(self):
        if not self._loaded_file_data: return
        sel = set(self.file_list_panel.get_selected_files())
        selected_data = [d for d in self._loaded_file_data if d["path"] in sel]
        overhead = (
            getattr(self, "_system_prompt_tokens", 0)
            + getattr(self, "_project_context_tokens", 0)
        )
        fc = len(selected_data)
        if fc > 0:
            max_per_file = max(d["tokens"] for d in selected_data)
            largest_call = overhead + max_per_file
            per_file_exceeded = exceeds_per_call_limit(max_per_file, overhead)
        else:
            largest_call = 0
            per_file_exceeded = False
        self.token_gauge.update_gauge(largest_call, fc)
        self.run_button.configure(state="normal" if (fc > 0 and not per_file_exceeded) else "disabled")
        self.file_list_panel.set_over_limit(per_file_exceeded)

    def _validate_inputs(self):
        if not self.api_key_entry.get().strip(): self.log.log_error("API key is required"); return False
        if not self._selected_files: self.log.log_error("Select .docx specification files"); return False
        missing = [f for f in self._selected_files if not f.exists()]
        if missing: self.log.log_error(f"File not found: {missing[0].name}"); return False
        if self.file_list_panel.get_selected_count() == 0: self.log.log_error("No files selected"); return False
        return True

    def _next_run_epoch(self) -> int:
        self._run_epoch += 1
        return self._run_epoch

    def _dispatch_if_current(self, epoch: int, fn):
        self.after(0, lambda: fn() if self._run_epoch == epoch else None)

    # ----- Real-time cost confirmation dialog -----

    def _confirm_realtime_cost(self, num_specs: int) -> bool:
        """Show a confirmation dialog warning about real-time mode costs.

        Returns True if the user confirms, False if they cancel.
        Uses wait_window to block until the dialog is closed.
        """
        self._realtime_confirmed = False

        dialog = ctk.CTkToplevel(self)
        dialog.title("Real-Time Mode — Cost Warning")
        dialog.geometry("520x340")
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

        inner = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
        inner.pack(fill="both", expand=True, padx=16, pady=16)

        # Warning icon + title
        ctk.CTkLabel(inner, text="\u26a0  Real-Time Mode Cost Warning",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=COLORS["warning"]).pack(anchor="w", padx=16, pady=(16, 8))

        # Warning text
        warning_text = (
            f"You are about to run a real-time review of {num_specs} spec{'s' if num_specs != 1 else ''} "
            f"using Claude Opus 4.6.\n\n"
            f"Real-time mode uses full-price API calls for every stage: "
            f"per-spec review, verification (one call per finding), and "
            f"cross-spec coordination (if enabled). Depending on the number "
            f"of specs and findings, this can cost anywhere from dozens to "
            f"hundreds to thousands of dollars.\n\n"
        )

        if num_specs > 5:
            warning_text += (
                f"\u26a0  You have {num_specs} specs selected. For more than 5 specs, "
                f"batch mode is strongly recommended — it provides 50% cost savings "
                f"and identical results."
            )
        else:
            warning_text += (
                f"Batch mode provides identical results at 50% cost savings "
                f"({_BATCH_TIMING_COPY} instead of real-time streaming)."
            )

        ctk.CTkLabel(inner, text=warning_text,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_secondary"],
            wraplength=460, justify="left").pack(anchor="w", padx=16, pady=(0, 16))

        # Buttons
        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))
        btn_kw = {"height": 36, "font": ctk.CTkFont(family="Segoe UI", size=12, weight="bold"), "corner_radius": 6}

        def _confirm():
            self._realtime_confirmed = True
            dialog.destroy()

        def _cancel():
            self._realtime_confirmed = False
            dialog.destroy()

        ctk.CTkButton(btn_frame, text="Switch to Batch Mode", width=180,
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=_cancel, **btn_kw).pack(side="left", padx=(0, 8))

        ctk.CTkButton(btn_frame, text="Proceed (Real-Time)", width=160,
            fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["warning"],
            text_color=COLORS["warning"], command=_confirm, **btn_kw).pack(side="left")

        # Block until dialog is closed
        dialog.wait_window()
        return self._realtime_confirmed

    def start_review(self):
        if self.is_processing: return
        if not self._validate_inputs(): return

        selected_files = self.file_list_panel.get_selected_files()
        num_specs = len(selected_files)

        # Show cost confirmation for real-time mode
        if not self._is_batch_mode:
            confirmed = self._confirm_realtime_cost(num_specs)
            if not confirmed:
                # User chose to switch to batch mode
                self.mode_selector.set(_MODE_BATCH)
                self._on_mode_change(_MODE_BATCH)
                self.log.log("Switched to batch mode.", level="info")
                return

        self._selected_files_for_review = selected_files
        self._project_context_for_review = self._get_project_context()
        self._cross_check_for_review = self._cross_check_var.get()
        self._export_mode_for_review = self._is_export_mode
        self._selected_cycle_label = self.cycle_selector.get()
        self.is_processing = True
        self._close_report_window()
        self.log.log("\u2500" * 40, level="muted", timestamp=False, paced=False)
        self.run_button.set_processing()
        self.progress_bar.pack(fill="x", pady=(8, 0), after=self.run_button)
        self.progress_bar.set(0); self.progress_bar.configure(mode="determinate")
        os.environ["ANTHROPIC_API_KEY"] = self.api_key_entry.get().strip()

        n = num_specs
        output_label = " \u2192 Export Report" if self._export_mode_for_review else ""
        if self._is_batch_mode:
            self.log.log_step(f"Submitting {n} files for batch review (Opus 4.6){output_label}...")
            run_epoch = self._next_run_epoch()
            threading.Thread(target=self._submit_batch_thread, args=(run_epoch,), daemon=True).start()
        else:
            self.log.log_step(f"Reviewing {n} files (Opus 4.6){output_label}...")
            run_epoch = self._next_run_epoch()
            threading.Thread(target=self._run_review_thread, args=(run_epoch,), daemon=True).start()

    def _run_review_thread(self, run_epoch: int):
        try:
            n = len(self._selected_files_for_review)
            self._dispatch_if_current(run_epoch, lambda: self.log.log_step("Starting per-spec review..."))
            cross_check_note = " + cross-check" if self._cross_check_for_review else ""
            mode_info = f"Model: Opus 4.6  \u2022  {n} specs \u2022  1 API call per spec  \u2022  verification enabled{cross_check_note}"
            self._dispatch_if_current(run_epoch, lambda: self.log.log(mode_info, level="muted"))

            def _on_progress(pct, msg):
                self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log_step(m))
                self._dispatch_if_current(run_epoch, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

            result = run_review(
                input_dir=self.input_dir,
                files=self._selected_files_for_review,
                project_context=self._project_context_for_review,
                model=MODEL_OPUS_46,
                verify=True,
                cross_check=self._cross_check_for_review,
                dry_run=False, verbose=False,
                cycle=AVAILABLE_CYCLES.get(self._selected_cycle_label, DEFAULT_CYCLE),
                log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                progress=_on_progress,
            )
            self._dispatch_if_current(run_epoch, lambda: self._on_review_complete(result))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))

    def _on_review_complete(self, result):
        self.progress_bar.set(1.0)
        self.log.log_success("Review complete!")
        self._last_result = result
        if result.review_result:
            rv = result.review_result
            self.log.log(f"Findings: {rv.critical_count} critical, {rv.high_count} high, {rv.medium_count} medium, {rv.gripe_count} gripes", level="info")
            if result.cross_check_result and result.cross_check_result.findings:
                cc = result.cross_check_result
                self.log.log(f"Cross-check: {len(cc.findings)} coordination issues found", level="info")
            self.log.log(f"Time: {rv.elapsed_seconds:.1f}s", level="muted")
            if getattr(self, "_export_mode_for_review", False):
                exported = self._export_report_to_file(result)
                if not exported:
                    self.log.log_step("Opening results in pop-out window instead...")
                    self._open_report_window(rv, result.files_reviewed, result.leed_alerts, result.placeholder_alerts, result.cross_check_result)
            else:
                self._open_report_window(rv, result.files_reviewed, result.leed_alerts, result.placeholder_alerts, result.cross_check_result)
        delete_batch_state()
        self.run_button.set_complete()
        self.after(2500, self._reset_ui)

    def _export_report_to_file(self, result) -> bool:
        default_name = f"spec-critic-report-{datetime.now().strftime('%Y-%m-%d')}.docx"
        path = filedialog.asksaveasfilename(
            title="Save Review Report",
            defaultextension=".docx",
            filetypes=[("Word Documents", "*.docx"), ("All Files", "*.*")],
            initialfile=default_name,
        )
        if not path:
            self.log.log_warning("Export canceled")
            return False
        try:
            output_path = Path(path)
            self.log.log_step(f"Exporting report to {output_path.name}...")
            export_report(
                result,
                output_path,
                project_context=getattr(self, "_project_context_for_review", ""),
                cycle_label=getattr(self, "_selected_cycle_label", DEFAULT_CYCLE.label),
            )
            self.log.log_success(f"Report saved: {output_path}")
            return True
        except Exception as e:
            self.log.log_error(f"Export failed: {e}")
            return False

    def _on_review_error(self, err):
        self.progress_bar.pack_forget()
        self.log.log_error(f"Review failed: {err}")
        self.run_button.set_ready(); self.is_processing = False

    # ----- Batch mode -----

    def _submit_batch_thread(self, run_epoch: int):
        try:
            def _on_progress(pct, msg):
                self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log_step(m))
                self._dispatch_if_current(run_epoch, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

            submission = start_batch_review(
                input_dir=self.input_dir,
                files=self._selected_files_for_review,
                project_context=self._project_context_for_review,
                model=MODEL_OPUS_46,
                cycle=AVAILABLE_CYCLES.get(self._selected_cycle_label, DEFAULT_CYCLE),
                cross_check_enabled=self._cross_check_for_review,
                export_mode=self._export_mode_for_review,
                log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                progress=_on_progress,
            )
            save_batch_state(build_resume_state(phase=PHASE_REVIEW_POLL, submission=submission))
            self._dispatch_if_current(run_epoch, lambda: self._on_batch_submitted(submission))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))

    def _on_batch_submitted(self, submission: BatchSubmission):
        self._batch_submission = submission
        self._poll_consecutive_errors = 0
        self.progress_bar.set(0.4)
        self.log.log_success(f"Batch submitted: {submission.job.batch_id}")
        self.log.log(f"  {len(submission.files_reviewed)} specs queued \u2022 50% cost savings", level="muted")
        self.log.log_step(f"Polling for results ({_BATCH_TIMING_COPY})...")
        self.run_button.configure(text="Polling...")
        self._poll_batch()

    def _poll_batch(self):
        if self._batch_submission is None:
            return
        run_epoch = self._run_epoch
        def _do_poll():
            try:
                status = poll_batch(self._batch_submission.job.batch_id)
                self._poll_consecutive_errors = 0
                self._dispatch_if_current(run_epoch, lambda: self._on_poll_result(status))
            except Exception as e:
                self._poll_consecutive_errors = getattr(self, "_poll_consecutive_errors", 0) + 1
                if self._poll_consecutive_errors >= 5:
                    self._dispatch_if_current(run_epoch, lambda: self.log.log_warning(f"5 consecutive poll errors \u2014 will keep trying"))
                self._dispatch_if_current(run_epoch, lambda: self.log.log_warning(f"Poll error (retrying): {e}"))
                self._dispatch_if_current(run_epoch, lambda: self._schedule_next_poll(30_000))
        threading.Thread(target=_do_poll, daemon=True).start()

    def _on_poll_result(self, status: BatchStatus):
        batch_pct = 0.40 + (status.progress_pct / 100.0) * 0.55
        self.progress_bar.set(min(batch_pct, 0.95))
        self.log.log(
            f"  Batch: {status.succeeded} done, {status.processing} processing, "
            f"{status.errored} errors \u2022 {status.progress_pct:.0f}%",
            level="info", paced=False,
        )
        normalized_status = status.status.replace("-", "_")

        if normalized_status == "ended":
            self.log.log_success("Batch complete \u2014 collecting results...")
            if self._batch_submission is not None:
                save_batch_state(build_resume_state(phase=PHASE_REVIEW_COLLECT, submission=self._batch_submission))
            self._collect_batch_results()
        elif normalized_status == "in_progress":
            self._schedule_next_poll(15_000)
        elif normalized_status == "canceling":
            self.log.log_warning("Batch is being canceled...")
            self._schedule_next_poll(5_000)
        elif normalized_status in ("failed", "expired", "canceled"):
            self.log.log_error(f"Batch terminated with status: {status.status}")
            self.log.log_warning("No results to collect. Clearing batch state.")
            delete_batch_state()
            self._batch_submission = None
            self._reset_ui()
        else:
            self.log.log_warning(f"Unexpected batch status: {status.status} \u2014 continuing to poll...")
            self._schedule_next_poll(15_000)

    def _schedule_next_poll(self, delay_ms: int):
        self._batch_poll_id = self.after(delay_ms, self._poll_batch)

    def _collect_batch_results(self):
        run_epoch = self._next_run_epoch()
        def _do_collect():
            try:
                def _on_progress(pct, msg):
                    self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log_step(m))
                    self._dispatch_if_current(run_epoch, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))
                if self._batch_submission is None:
                    raise RuntimeError("No active batch submission to collect.")
                cycle = AVAILABLE_CYCLES.get(getattr(self._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)
                review_state = collect_review_batch_results(
                    self._batch_submission,
                    log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                )
                review_state = run_cross_check_for_batch(
                    review_state,
                    specs=getattr(self._batch_submission, "prepared_specs", None),
                    project_context=getattr(self, "_project_context_for_review", ""),
                    cycle=cycle,
                    log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                )
                if review_state.cross_check_skipped_due_to_missing_specs:
                    self._dispatch_if_current(run_epoch, lambda: self.log.log_warning(
                        "Cross-check skipped due to missing resumable extracted specs."
                    ))

                verifiable_findings = prepare_verification_work(review_state)
                verification_completed = False
                if verifiable_findings:
                    verification_job = start_batch_verification(
                        verifiable_findings,
                        cycle=cycle,
                        log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                        progress=_on_progress,
                    )
                    save_batch_state(build_resume_state(
                        phase=PHASE_VERIFICATION_POLL,
                        submission=self._batch_submission,
                        review_state=review_state,
                        verification_batch=verification_job,
                        verification_started=True,
                    ))
                    collect_batch_verification_results(
                        verification_job,
                        verifiable_findings,
                        cycle=cycle,
                        log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                        progress=_on_progress,
                    )
                    verification_completed = True

                final_result = finalize_batch_result(review_state)
                save_batch_state(build_resume_state(
                    phase=PHASE_FINALIZE,
                    submission=self._batch_submission,
                    review_state=review_state,
                    cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                    verification_started=bool(verifiable_findings),
                    verification_completed=verification_completed,
                ))
                self._dispatch_if_current(run_epoch, lambda r=final_result: self._on_review_complete(r))
            except Exception as e:
                import traceback
                err = f"{e}\n{traceback.format_exc()}"
                self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))
        threading.Thread(target=_do_collect, daemon=True).start()

    def _reset_ui(self):
        self.run_button.set_ready()
        if self._is_batch_mode:
            self.run_button.configure(text="Submit Batch")
        self.progress_bar.pack_forget()
        self.is_processing = False
        self._batch_submission = None

    # ----- Persistent batch state -----

    def _check_pending_batch(self):
        loaded = load_batch_state()
        if loaded is None:
            return
        submission = loaded["submission"]
        phase = loaded.get("phase", PHASE_REVIEW_POLL)
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

        info_text = (
            f"Batch ID: {submission.job.batch_id[:30]}...\n"
            f"Files: {len(submission.files_reviewed)} specs  \u2022  Model: Opus 4.6\n"
            f"Submitted: {age_str}  \u2022  Phase: {phase}"
        )
        ctk.CTkLabel(inner, text=info_text, font=ctk.CTkFont(family="Consolas", size=11),
            text_color=COLORS["text_secondary"], justify="left").pack(anchor="w", padx=16, pady=(0, 12))

        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))
        btn_kw = {"height": 34, "font": ctk.CTkFont(family="Segoe UI", size=12), "corner_radius": 6}

        def _resume():
            dialog.destroy()
            self._resume_batch(loaded)

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

    def _resume_batch(self, loaded_state: dict):
        submission: BatchSubmission = loaded_state["submission"]
        phase = loaded_state.get("phase", PHASE_REVIEW_POLL)
        api_key = self.api_key_entry.get().strip()
        if not api_key:
            api_key = load_api_key_from_file() or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            self.log.log_error("API key is required to resume batch. Enter your key and try again.")
            return
        if phase not in SUPPORTED_PHASES:
            self.log.log_error("Saved batch state has unsupported phase. Discarding it.")
            delete_batch_state()
            return

        os.environ["ANTHROPIC_API_KEY"] = api_key
        self._batch_submission = submission
        self._cross_check_for_review = getattr(submission, "cross_check_enabled", False)
        cross_check_skipped = False
        if self._cross_check_for_review and not getattr(submission, "prepared_specs", None):
            self.log.log_warning("Cross-check was enabled but spec content could not be restored from saved state. Cross-check will be skipped for this resumed batch.")
            self._cross_check_for_review = False
            cross_check_skipped = True
        self._project_context_for_review = getattr(submission, "project_context", "")
        self._selected_cycle_label = getattr(submission, "cycle_label", DEFAULT_CYCLE.label)
        self._export_mode_for_review = bool(getattr(submission, "export_mode", False))
        self.output_selector.set("Export Report" if self._export_mode_for_review else "View in App")
        self._on_output_mode_change(self.output_selector.get())
        if submission.cycle_label in AVAILABLE_CYCLES:
            self.cycle_selector.set(submission.cycle_label)
        self._cross_check_var.set(bool(getattr(submission, "cross_check_enabled", False)))
        self.is_processing = True

        self.log.log("\u2500" * 40, level="muted", timestamp=False, paced=False)
        self.log.log_step(f"Resuming batch: {submission.job.batch_id}")
        self.log.log(f"  {len(submission.files_reviewed)} specs \u2022 Phase: {phase}", level="muted")

        self.run_button.set_processing()
        self.run_button.configure(text="Polling...")
        self.progress_bar.pack(fill="x", pady=(8, 0), after=self.run_button)
        self.progress_bar.set(0.4)
        self.progress_bar.configure(mode="determinate")
        if phase == PHASE_REVIEW_POLL:
            self._poll_batch()
            return
        if phase == PHASE_REVIEW_COLLECT:
            self._collect_batch_results()
            return
        if phase == PHASE_VERIFICATION_POLL:
            if not self._is_valid_verification_resume_state(loaded_state):
                self.log.log_error("Saved verification resume state is incomplete. Discarding it.")
                delete_batch_state()
                self._reset_ui()
                return
            self._resume_verification_poll(loaded_state)
            return
        if phase == PHASE_FINALIZE:
            review_state: CollectedBatchState | None = loaded_state.get("review_state")
            if review_state is None:
                self.log.log_error("Saved finalize resume state is incomplete. Discarding it.")
                delete_batch_state()
                self._reset_ui()
                return
            if cross_check_skipped:
                review_state.cross_check_skipped_due_to_missing_specs = True
            result = finalize_batch_result(review_state)
            self._on_review_complete(result)
            return

    def _is_valid_verification_resume_state(self, loaded_state: dict) -> bool:
        review_state = loaded_state.get("review_state")
        verification_batch = loaded_state.get("verification_batch")
        if review_state is None or not isinstance(verification_batch, BatchJob):
            return False
        batch_id = getattr(verification_batch, "batch_id", None)
        if not isinstance(batch_id, str) or not batch_id.startswith("msgbatch_") or len(batch_id) <= len("msgbatch_"):
            return False
        request_map = getattr(verification_batch, "request_map", None)
        if not isinstance(request_map, dict) or not request_map:
            return False
        return True

    def _resume_verification_poll(self, loaded_state: dict):
        run_epoch = self._next_run_epoch()
        review_state: CollectedBatchState = loaded_state["review_state"]
        verification_job = loaded_state["verification_batch"]
        cycle = AVAILABLE_CYCLES.get(getattr(self._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)
        verifiable_findings = prepare_verification_work(review_state)

        def _do_resume_verification():
            try:
                def _on_progress(pct, msg):
                    self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log_step(m))
                    self._dispatch_if_current(run_epoch, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

                collect_batch_verification_results(
                    verification_job,
                    verifiable_findings,
                    cycle=cycle,
                    log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                    progress=_on_progress,
                )
                result = finalize_batch_result(review_state)
                save_batch_state(build_resume_state(
                    phase=PHASE_FINALIZE,
                    submission=self._batch_submission,
                    review_state=review_state,
                    cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                    verification_started=True,
                    verification_completed=True,
                ))
                self._dispatch_if_current(run_epoch, lambda r=result: self._on_review_complete(r))
            except Exception as e:
                import traceback
                err = f"{e}\n{traceback.format_exc()}"
                self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))
        threading.Thread(target=_do_resume_verification, daemon=True).start()

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

    # ----- About / How It Works dialog -----

    def _show_about_dialog(self):
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

        scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        sections = [
            ("1.  Text Extraction", (
                "Your .docx files are read locally. Paragraphs and tables are "
                "extracted \u2014 nothing is sent to Claude yet."
            )),
            ("2.  Local Pre-Screening", (
                "Before any API calls, the tool scans for LEED references and unresolved "
                "placeholders (like [EDIT] or [VERIFY]). These are flagged as alerts and "
                "don\u2019t cost any tokens."
            )),
            ("3.  Per-Spec Review", (
                "Each specification is sent individually to Claude Opus 4.6. "
                "Claude checks for code compliance issues (CBC, CMC, CPC, "
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
                "If enabled, a separate Opus 4.6 call analyzes the full text of all your "
                "specs together using the 1M token context window. It catches contradictions "
                "between specs, missing cross-references, scope gaps and overlaps, "
                "inconsistent equipment data, and division-of-work conflicts. Because it "
                "has the complete content of every spec, it can find coordination issues "
                "that are invisible to per-spec review."
            )),
            ("6.  Verification", (
                "Every finding is checked in a secondary AI verification pass using Claude Opus 4.6 with web search. "
                "The verifier searches for the cited codes and standards and returns a verdict: Confirmed, Corrected, "
                "Disputed, or Unverified. This is an AI-assisted check, not a substitute for engineer review."
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

        ctk.CTkButton(
            outer, text="Close", width=100, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=dialog.destroy,
        ).pack(pady=(0, 16))

    def _show_usage_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("How to Use Spec Critic")
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
            outer, text="How to Use Spec Critic",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=20, pady=(20, 4))

        ctk.CTkLabel(
            outer, text="Step-by-step guide to running a specification review",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=20, pady=(0, 12))

        scroll = ctk.CTkScrollableFrame(outer, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        sections = [
            ("1.  Enter Your API Key", (
                "Paste your Anthropic API key (starts with sk-ant-...) into the "
                "API Key field. The key is used for all Claude API calls during "
                "the review. You can also save it to a file named "
                "'spec_critic_api_key.txt' next to the application — it will "
                "be loaded automatically on startup."
            )),
            ("2.  Select Specification Files", (
                "Click Browse and select one or more .docx specification files. "
                "The tool will extract text and analyze token usage. The token "
                "gauge shows the largest single spec's estimated API call size "
                "against the per-call limit — if a spec is too large, it will "
                "be flagged."
            )),
            ("3.  Add Project Context (Optional)", (
                "Describe your project in the Project Context field — things "
                "like building type, square footage, number of stories, or "
                "any special conditions. This context is included with every "
                "API call and helps Claude produce more relevant findings. "
                "Click Expand for a larger editing area."
            )),
            ("4.  Choose Your Mode", (
                "Real-time mode streams results as they come in — fast but "
                "expensive. Batch mode queues all specs for processing at 50% "
                f"cost savings, with results {_BATCH_TIMING_COPY}. "
                "For more than a few specs, batch mode is strongly recommended."
            )),
            ("5.  Select Code Cycle", (
                "Choose the California code cycle for your project (2022 or "
                "2025). This determines which edition of CBC, CMC, CPC, Energy "
                "Code, CALGreen, and ASCE 7 the reviewer checks against."
            )),
            ("6.  Enable Cross-Spec Coordination (Optional)", (
                "Check this option to run a separate coordination analysis that "
                "sends all spec content to Claude in a single call. This catches "
                "contradictions between specs, missing cross-references, and "
                "scope gaps that per-spec review cannot detect. Requires the "
                "combined content to fit within the cross-check token limit."
            )),
            ("7.  Choose Output Mode", (
                "'View in App' renders results in a pop-out report window with "
                "collapsible finding cards. 'Export Report' saves a formatted "
                ".docx file — useful for large reviews that would slow down "
                "in-app rendering, or for sharing with your team."
            )),
            ("8.  Run the Review", (
                "Click Run Review (real-time) or Submit Batch (batch mode). "
                "The activity log shows progress. In batch mode, you can close "
                "the app and reopen it later — the pending batch state is saved "
                "and you will be prompted to resume."
            )),
            ("9.  Review the Results", (
                "Findings are grouped by severity (Critical, High, Medium, "
                "Gripe) and sorted by confidence within each tier. Each finding "
                "includes a verification verdict from a secondary AI pass with "
                "web search. Use Export JSON from the report window to save "
                "structured results for further processing."
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

        ctk.CTkLabel(
            scroll, text="Tips",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=8, pady=(14, 2))
        ctk.CTkLabel(
            scroll,
            text=(
                "Use batch mode for routine reviews — identical results at "
                "half the cost. Save your API key to a file so you don’t "
                "have to paste it every time. Write specific project context — "
                "the more detail you provide, the more targeted the findings. "
                "Always spot-check code citations against the actual code text "
                "before acting on findings."
            ),
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_secondary"],
            wraplength=520, justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 10))

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
