"""
Spec Critic - Modern GUI with CustomTkinter
M&P Specification Review • California K-12 DSA • Claude Opus 4.6
v2.8.0 - Batch-only enforcement, bounded polling, and reporting updates
"""
import os, sys, json, time, threading, shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import customtkinter as ctk
from tkinter import filedialog, messagebox
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

if TkinterDnD is not None:
    class _CTkDnDRoot(ctk.CTk, TkinterDnD.DnDWrapper):
        pass
else:
    _CTkDnDRoot = ctk.CTk

base_path = os.path.dirname(os.path.abspath(__file__))
exe_dir = Path(base_path).parent
sys.path.insert(0, str(exe_dir))

from src.pipeline import (
    run_review,
    start_batch_review,
    collect_review_batch_results,
    run_cross_check_for_batch,
    start_batch_verification,
    collect_batch_verification_results,
    finalize_batch_result,
    BatchSubmission,
    CollectedBatchState,
)
from src.batch import BatchStatus, BatchJob
from src.batch_runtime import DEFAULT_REVIEW_POLL_POLICY, poll_batch_bounded
from src.reviewer import MODEL_OPUS_46, REVIEW_MODELS, Finding
from src.extractor import (
    extract_text,
    extract_context_text,
    ExtractedSpec,
    SUPPORTED_EXTENSIONS,
    CONTEXT_ATTACHMENT_EXTENSIONS,
)
from src.tokenizer import (
    PROJECT_CONTEXT_MAX_TOKENS,
    RECOMMENDED_MAX,
    exceeds_per_call_limit,
)
from src.prompts import get_system_prompt
from src.code_cycles import AVAILABLE_CYCLES, DEFAULT_CYCLE
from src.review_modes import (
    DEFAULT_REVIEW_MODE,
    REVIEW_MODE_PROFILES,
    ReviewMode,
    coerce_review_mode,
)
from src.report_exporter import export_report
from src.edit_candidates import classify_edit_candidates
from src.apply_edits import execute_edit_plan
from src.spec_editor import EditReport
from src.resume_state import (
    PHASE_REVIEW_POLL,
    PHASE_REVIEW_COLLECT,
    PHASE_VERIFICATION_POLL,
    PHASE_VERIFICATION_WAVE_POLL,
    PHASE_CROSS_CHECK,
    PHASE_CROSS_CHECK_VERIFICATION_POLL,
    PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
    PHASE_FINALIZE,
    SUPPORTED_PHASES,
    build_resume_state,
    deserialize_resume_state,
)

from src.widgets import (
    COLORS,
    TokenGauge,
    FileListPanel,
    EnhancedLog,
    AnimatedButton,
    ReportWindow,
    DiagnosticsWindow,
    EditSelectionDialog,
    EditSummaryDialog,
)
from src.diagnostics import DiagnosticsReport


from platformdirs import user_config_dir, user_state_dir

API_KEY_FILENAME = "spec_critic_api_key.txt"
BATCH_STATE_FILENAME = "batch_state.json"

BATCH_STATE_MAX_AGE_HOURS = 24 * 30

_CONTEXT_PLACEHOLDER = "Describe your project (optional)"

_SPEC_FILETYPES = [
    ("Word Specifications", "*.docx"),
    ("All Files", "*.*"),
]

_CONTEXT_FILETYPES = [
    ("Documents", "*.docx *.pdf"),
    ("Word Documents", "*.docx"),
    ("PDF Documents", "*.pdf"),
    ("All Files", "*.*"),
]

# Mode selector labels
_MODE_REALTIME = "Real-time (FAST: Expensive!)"
_MODE_BATCH = "Batch (SLOW: Cheap!)"
_BATCH_TIMING_COPY = "Usually 45 min to 2 hrs, 24 hrs maximum (Extremely Rare)"

_FONT_SCALE_OPTIONS = {
    "Default (100%)": 1.0,
    "Large (+10%)": 1.1,
    "Larger (+20%)": 1.2,
}

# Consistent font size for all input row labels and controls
_UI_FONT_SIZE = 12


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


class SpecReviewApp(_CTkDnDRoot):
    def __init__(self):
        super().__init__()
        if TkinterDnD is not None:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception as e:
                print(f"[SpecCritic] Drag-and-drop unavailable: {e}")
        self.title("Spec Critic")
        self.geometry("900x950")
        self.minsize(750, 700)
        self.configure(fg_color=COLORS["bg_dark"])
        self.input_dir = None
        self.is_processing = False
        self._report_window: Optional[ReportWindow] = None
        self._project_context_tokens = 0
        self._batch_submission: Optional[BatchSubmission] = None
        self._run_epoch = 0
        # Phase 7.2 (audit Section 11.2): every background token analysis
        # captures an epoch when launched. When a newer analysis starts, the
        # epoch increments — older threads silently drop their results so a
        # stale background pass cannot overwrite UI state that already
        # reflects the latest user action.
        self._analysis_epoch = 0
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
        self._verbose_for_review: bool = True
        self._export_mode_for_review: bool = False
        self._last_result = None
        self._diagnostics_report: Optional[DiagnosticsReport] = None
        self._diagnostics_window: Optional[DiagnosticsWindow] = None
        self._realtime_confirmed: bool = False
        self._context_debounce_id: str | None = None
        self._selected_cycle_label: str = DEFAULT_CYCLE.label
        # Phase 8 / plan section 12.1: GUI tracks the active review mode so
        # both the real-time and batch paths submit with the user's choice.
        self._review_mode: ReviewMode = DEFAULT_REVIEW_MODE
        self._review_mode_for_review: ReviewMode = DEFAULT_REVIEW_MODE
        self._font_scale_label: str = "Default (100%)"
        self._create_ui()
        self.after(500, self._check_pending_batch)

    def _create_ui(self):
        c = ctk.CTkFrame(self, fg_color="transparent")
        c.pack(fill="both", expand=True, padx=24, pady=24)
        self.container = c

        # Header
        self.hdr = ctk.CTkFrame(c, fg_color="transparent")
        self.hdr.pack(fill="x", pady=(0, 8))
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

        # --- Accessibility row: sits between header and inputs card ---
        accessibility_bar = ctk.CTkFrame(c, fg_color="transparent")
        accessibility_bar.pack(fill="x", pady=(8, 12))
        ctk.CTkLabel(accessibility_bar, text="Accessibility", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 12))
        self._font_scale_var = ctk.StringVar(value=self._font_scale_label)
        self.font_size_selector = ctk.CTkSegmentedButton(
            accessibility_bar,
            values=list(_FONT_SCALE_OPTIONS.keys()),
            variable=self._font_scale_var,
            command=self._on_font_scale_change,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_primary"],
            text_color_disabled=COLORS["text_muted"], height=32,
        )
        self.font_size_selector.set(self._font_scale_label)
        self.font_size_selector.pack(side="left")

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
        self.diagnostics_button = ctk.CTkButton(
            c, text="Diagnostics", height=32,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_secondary"],
            command=self._open_diagnostics_window, state="disabled",
        )
        self.diagnostics_button.pack(fill="x", pady=(8, 0))

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
        ctk.CTkLabel(self.inputs_content, text="API Key", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=0, column=0, sticky="w", pady=8)
        self.api_key_entry = ctk.CTkEntry(self.inputs_content, placeholder_text="sk-ant-...", font=ctk.CTkFont(family="Consolas", size=_UI_FONT_SIZE), fg_color=COLORS["bg_input"], border_color=COLORS["border"], text_color=COLORS["text_primary"], height=36, show="\u2022")
        self.api_key_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=8)
        if self.api_key: self.api_key_entry.insert(0, self.api_key)

        # --- Row 1: Specs ---
        ctk.CTkLabel(self.inputs_content, text="Specs", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=1, column=0, sticky="w", pady=8)
        ef = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        ef.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=8)
        ef.columnconfigure(0, weight=1)
        self.input_dir_entry = ctk.CTkEntry(ef, placeholder_text="Select or drop .docx specification files", font=ctk.CTkFont(family="Consolas", size=_UI_FONT_SIZE), fg_color=COLORS["bg_input"], border_color=COLORS["border"], text_color=COLORS["text_primary"], height=36)
        self.input_dir_entry.grid(row=0, column=0, sticky="ew")
        bkw = {"height": 36, "font": ctk.CTkFont(size=_UI_FONT_SIZE), "fg_color": COLORS["bg_input"], "hover_color": COLORS["border"], "border_width": 1, "border_color": COLORS["border"], "text_color": COLORS["text_secondary"]}
        ctk.CTkButton(ef, text="Browse", width=70, command=self._browse_files, **bkw).grid(row=0, column=1, padx=(8, 0))
        self._register_specs_drop_target()

        # --- Row 2: Project Context ---
        ctx_label_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        ctx_label_frame.grid(row=2, column=0, sticky="nw", pady=8)
        ctk.CTkLabel(ctx_label_frame, text="Project Context", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="nw").pack(anchor="nw")
        ctk.CTkButton(ctx_label_frame, text="Expand", width=80, height=24, font=ctk.CTkFont(size=11), fg_color=COLORS["bg_input"], hover_color=COLORS["border"], border_width=1, border_color=COLORS["border"], text_color=COLORS["text_secondary"], command=self._open_context_modal).pack(anchor="nw", pady=(4, 0))
        ctk.CTkButton(ctx_label_frame, text="Attach Files…", width=80, height=24, font=ctk.CTkFont(size=11), fg_color=COLORS["bg_input"], hover_color=COLORS["border"], border_width=1, border_color=COLORS["border"], text_color=COLORS["text_secondary"], command=self._attach_context_files).pack(anchor="nw", pady=(4, 0))
        ctx_field_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        ctx_field_frame.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=8)
        ctx_field_frame.columnconfigure(0, weight=1)
        self.context_textbox = ctk.CTkTextbox(
            ctx_field_frame, fg_color=COLORS["bg_input"], border_color=COLORS["border"],
            border_width=2, text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family="Consolas", size=_UI_FONT_SIZE), height=80, wrap="word",
        )
        self.context_textbox.grid(row=0, column=0, sticky="ew")
        self._context_has_placeholder = True
        self.context_textbox.insert("1.0", _CONTEXT_PLACEHOLDER)
        self.context_textbox.configure(text_color=COLORS["text_muted"])
        self.context_textbox.bind("<FocusIn>", self._context_focus_in)
        self.context_textbox.bind("<FocusOut>", self._context_focus_out)
        self.context_textbox.bind("<KeyRelease>", self._on_context_change)
        self.context_token_label = ctk.CTkLabel(
            ctx_field_frame,
            text=f"0 / {PROJECT_CONTEXT_MAX_TOKENS:,} tokens",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COLORS["text_muted"],
            anchor="e",
        )
        self.context_token_label.grid(row=1, column=0, sticky="e", pady=(4, 0))

        # --- Row 3: Review Mode ---
        ctk.CTkLabel(self.inputs_content, text="Mode", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=3, column=0, sticky="w", pady=8)
        mode_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        mode_frame.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=8)
        self.mode_selector = ctk.CTkSegmentedButton(
            mode_frame, values=[_MODE_REALTIME, _MODE_BATCH],
            command=self._on_mode_change, font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_primary"],
            text_color_disabled=COLORS["text_muted"], height=32,
        )
        self.mode_selector.set(_MODE_REALTIME)
        self.mode_selector.pack(side="left")
        self._mode_hint = ctk.CTkLabel(mode_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_muted"])
        self._mode_hint.pack(side="left", padx=(12, 0))

        # --- Row 4: Output ---
        ctk.CTkLabel(self.inputs_content, text="Output", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=4, column=0, sticky="w", pady=8)
        output_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        output_frame.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=8)
        self._output_mode_var = ctk.StringVar(value="View in App")
        self.output_selector = ctk.CTkSegmentedButton(
            output_frame, values=["View in App", "Export Report"],
            variable=self._output_mode_var,
            command=self._on_output_mode_change,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_primary"],
            text_color_disabled=COLORS["text_muted"], height=32,
        )
        self.output_selector.set("View in App")
        self.output_selector.pack(side="left")
        self._output_hint = ctk.CTkLabel(output_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_muted"])
        self._output_hint.pack(side="left", padx=(12, 0))

        # --- Row 5: Options ---
        ctk.CTkLabel(self.inputs_content, text="Options", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=5, column=0, sticky="w", pady=8)
        options_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        options_frame.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=8)
        self._cross_check_var = ctk.BooleanVar(value=False)
        self._cross_check_cb = ctk.CTkCheckBox(
            options_frame, text="Cross-spec coordination check", variable=self._cross_check_var,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"], border_color=COLORS["border"],
            checkmark_color=COLORS["text_primary"], text_color=COLORS["text_secondary"],
            checkbox_width=20, checkbox_height=20,
        )
        self._cross_check_cb.pack(side="left")
        self._verbose_var = ctk.BooleanVar(value=True)
        self._verbose_cb = ctk.CTkCheckBox(
            options_frame, text="Verbose report", variable=self._verbose_var,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"], border_color=COLORS["border"],
            checkmark_color=COLORS["text_primary"], text_color=COLORS["text_secondary"],
            checkbox_width=20, checkbox_height=20,
        )
        self._verbose_cb.pack(side="left", padx=(12, 0))
        self._cross_check_hint = ctk.CTkLabel(options_frame,
            text="Opus 4.6 \u2022 full content \u2022 finds inter-spec conflicts",
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_muted"])
        self._cross_check_hint.pack(side="left", padx=(12, 0))

        # --- Row 6: Code Cycle ---
        ctk.CTkLabel(self.inputs_content, text="Code Cycle", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=6, column=0, sticky="w", pady=8)
        cycle_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        cycle_frame.grid(row=6, column=1, sticky="w", padx=(8, 0), pady=8)
        self.cycle_selector = ctk.CTkSegmentedButton(cycle_frame, values=["2022", "2025"], font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"], unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"], fg_color=COLORS["bg_input"], text_color=COLORS["text_primary"], height=32)
        self.cycle_selector.set(DEFAULT_CYCLE.label)
        self.cycle_selector.pack(side="left")

        # --- Row 7: Review Mode (Phase 8 / plan section 12.1) ---
        ctk.CTkLabel(self.inputs_content, text="Review Mode", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=7, column=0, sticky="w", pady=8)
        mode_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        mode_frame.grid(row=7, column=1, sticky="w", padx=(8, 0), pady=8)
        self._mode_label_to_enum: dict[str, ReviewMode] = {
            REVIEW_MODE_PROFILES[m].label: m for m in (
                ReviewMode.STRICT, ReviewMode.COMPREHENSIVE, ReviewMode.SAFE_EDIT,
            )
        }
        mode_values = list(self._mode_label_to_enum.keys())
        self.review_mode_selector = ctk.CTkSegmentedButton(
            mode_frame, values=mode_values,
            command=self._on_review_mode_change,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            selected_color=COLORS["accent"], selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["border"],
            fg_color=COLORS["bg_input"], text_color=COLORS["text_primary"],
            height=32,
        )
        self.review_mode_selector.set(REVIEW_MODE_PROFILES[DEFAULT_REVIEW_MODE].label)
        self.review_mode_selector.pack(side="left")
        self._review_mode_hint = ctk.CTkLabel(
            mode_frame,
            text=REVIEW_MODE_PROFILES[DEFAULT_REVIEW_MODE].short_description,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            text_color=COLORS["text_muted"],
        )
        self._review_mode_hint.pack(side="left", padx=(12, 0))

        # (Accessibility row is now in the header area, not inside the inputs card)

        self.inputs_content.columnconfigure(1, weight=1)

    # --- Output mode helpers ---

    def _on_output_mode_change(self, value: str):
        if value == "Export Report":
            self._output_hint.configure(text="Saves .docx report \u2022 no in-app rendering")
        else:
            self._output_hint.configure(text="")

    def _on_review_mode_change(self, value: str):
        mode = self._mode_label_to_enum.get(value, DEFAULT_REVIEW_MODE)
        self._review_mode = mode
        profile = REVIEW_MODE_PROFILES[mode]
        self._review_mode_hint.configure(text=profile.short_description)

    def _get_selected_review_mode(self) -> ReviewMode:
        # Reading the segmented control directly avoids stale state if a
        # background thread captured ``self._review_mode`` before the user
        # changed it. ``coerce_review_mode`` falls back to default for
        # unknown labels.
        try:
            label = self.review_mode_selector.get()
        except Exception:
            return self._review_mode
        return self._mode_label_to_enum.get(label, self._review_mode)

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
        ctx = self._get_project_context()
        if ctx:
            from tiktoken import get_encoding
            enc = get_encoding("cl100k_base")
            self._project_context_tokens = len(enc.encode(ctx))
        else:
            self._project_context_tokens = 0
        self._update_context_token_label()
        if self._loaded_file_data:
            self._on_file_selection_change()

    def _update_context_token_label(self) -> None:
        tokens = self._project_context_tokens
        over = tokens > PROJECT_CONTEXT_MAX_TOKENS
        text = f"{tokens:,} / {PROJECT_CONTEXT_MAX_TOKENS:,} tokens"
        if over:
            text += " — exceeds limit"
            color = COLORS["error"]
        elif tokens > int(PROJECT_CONTEXT_MAX_TOKENS * 0.9):
            color = COLORS["warning"]
        else:
            color = COLORS["text_muted"]
        if hasattr(self, "context_token_label"):
            self.context_token_label.configure(text=text, text_color=color)

    def _set_context_text(self, new_text: str) -> None:
        """Replace the context textbox contents, restoring placeholder when empty."""
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

    def _extract_context_attachments(self, paths: list[Path]) -> tuple[str, list[str]]:
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

    def _attach_context_files(self, target_textbox=None) -> None:
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
            self.configure(cursor="watch")
            self.update_idletasks()
            combined, errors = self._extract_context_attachments(paths)
        finally:
            self.configure(cursor="")

        if errors:
            messagebox.showwarning(
                "Some attachments could not be read",
                "\n".join(errors),
            )
        if not combined:
            return

        if target_textbox is None:
            existing = self._get_project_context()
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
            self._set_context_text(merged)
        else:
            target_textbox.delete("1.0", "end")
            target_textbox.insert("1.0", merged)

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
            self._set_context_text(new_text)
            dialog.destroy()

        button_row = ctk.CTkFrame(outer, fg_color="transparent")
        button_row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkButton(
            button_row, text="Attach Files…", width=120, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_secondary"],
            command=lambda: self._attach_context_files(target_textbox=modal_textbox),
        ).pack(side="left")
        ctk.CTkButton(
            button_row, text="Save & Close", width=120, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=_save_and_close,
        ).pack(side="right")

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
            self._apply_selected_specs([Path(f) for f in files])

    def _register_specs_drop_target(self):
        if DND_FILES is None:
            print("[SpecCritic] Drag-and-drop unavailable: install tkinterdnd2 to enable dropping .docx files")
            return
        try:
            self.input_dir_entry.drop_target_register(DND_FILES)
            self.input_dir_entry.dnd_bind("<<Drop>>", self._on_specs_drop)
        except Exception as e:
            print(f"[SpecCritic] Drag-and-drop unavailable: {e}")

    def _parse_dropped_paths(self, payload: str) -> list[Path]:
        if not payload:
            return []
        raw_items: list[str] = []
        try:
            raw_items = list(self.tk.splitlist(payload))
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

    def _apply_selected_specs(self, candidate_paths: list[Path]):
        paths = [p for p in candidate_paths if _is_supported_spec(p)]
        if not paths:
            self.log.log_warning("No supported .docx files selected")
            return
        self._selected_files = paths
        self.input_dir = paths[0].parent
        self.input_dir_entry.delete(0, "end")
        self.input_dir_entry.insert(0, str(paths[0]) if len(paths) == 1 else f"{len(paths)} files selected")
        self._analyze_tokens(paths)

    def _on_specs_drop(self, event):
        dropped_paths = self._parse_dropped_paths(getattr(event, "data", ""))
        self._apply_selected_specs(dropped_paths)

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

        # Phase 7.2 (audit Section 11.2): capture every UI-thread value that
        # the background pass needs *before* spawning the worker. The worker
        # must not read Tkinter/CustomTkinter state directly — those reads
        # are not thread-safe and risk reading partially-updated values.
        project_context = self._get_project_context()
        cycle_label = self.cycle_selector.get()
        cycle = AVAILABLE_CYCLES.get(cycle_label, DEFAULT_CYCLE)

        # Increment the analysis epoch and capture the value. Newer
        # analysis starts will bump the epoch; older threads will see their
        # captured value differs from ``self._analysis_epoch`` and discard
        # their result instead of overwriting fresher UI state.
        self._analysis_epoch += 1
        captured_epoch = self._analysis_epoch

        def _is_current() -> bool:
            return self._analysis_epoch == captured_epoch

        def _dispatch_if_current(fn):
            self.after(0, lambda: fn() if _is_current() else None)

        def analyze():
            try:
                _dispatch_if_current(lambda: self._clear_file_state())
                file_data = []
                processed_names: list[str] = []
                from tiktoken import get_encoding
                enc = get_encoding("cl100k_base")
                sys_tokens = len(enc.encode(get_system_prompt(cycle)))
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
                        _dispatch_if_current(lambda err=str(e), n=f.name: self.log.log_warning(f"Could not read {n}: {err}"))
                if processed_names:
                    _dispatch_if_current(lambda names=processed_names: self.log.log_file_batch(names))
                if file_data:
                    _dispatch_if_current(lambda fd=file_data, es=extracted_specs, st=sys_tokens, ct=ctx_tokens:
                        self._set_file_data(fd, es, st, ct))
                    overhead = sys_tokens + ctx_tokens
                    max_per_file = max(d["tokens"] for d in file_data)
                    largest_call = overhead + max_per_file
                    per_file_limit_exceeded = exceeds_per_call_limit(max_per_file, overhead)
                    _dispatch_if_current(lambda fd=file_data: self.file_list_panel.load_files(fd))
                    _dispatch_if_current(lambda lc=largest_call, fc=len(file_data): self.token_gauge.update_gauge(lc, fc))
                    _dispatch_if_current(lambda lc=largest_call: self.log.log_success(f"Token analysis complete: largest spec call ~{lc:,} tokens"))
                    if per_file_limit_exceeded:
                        over_files = [d["filename"] for d in file_data if exceeds_per_call_limit(d["tokens"], overhead)]
                        _dispatch_if_current(lambda of=over_files: self.log.log_warning(
                            f"File too large for single API call: {', '.join(of)}"
                        ))
                    _dispatch_if_current(lambda b=per_file_limit_exceeded: self.run_button.configure(
                        state="disabled" if b else "normal"
                    ))
                    _dispatch_if_current(lambda b=per_file_limit_exceeded: self.file_list_panel.set_over_limit(b))
                    # Phase 2.3 (audit Section 6.3): after the cl100k_base
                    # estimate, kick off an exact Anthropic count_tokens call
                    # for the largest spec and re-render the gauge with the
                    # exact value. The local estimate stays visible while
                    # the API call is in flight.
                    self._refresh_exact_token_count(file_data, extracted_specs, project_context, cycle, sys_tokens, ctx_tokens, _dispatch_if_current)
            except Exception as e:
                _dispatch_if_current(lambda err=e: self.log.log_error(f"Analysis failed: {err}"))

        threading.Thread(target=analyze, daemon=True).start()

    def _refresh_exact_token_count(self, file_data, extracted_specs, project_context, cycle, sys_tokens, ctx_tokens, dispatch):
        """Run Anthropic count_tokens for the largest spec and update the gauge.

        Runs in its own background thread so the cl100k_base estimate stays
        on screen while we wait. Falls back silently to the local estimate
        when the API call fails or returns None.
        """
        from .tokenizer import count_tokens_via_api
        from .prompts import get_single_spec_user_message, get_system_prompt
        from .reviewer import MODEL_OPUS_46 as _model
        from .review_modes import DEFAULT_REVIEW_MODE

        biggest = max(file_data, key=lambda d: d["tokens"])
        biggest_spec = next((s for s in extracted_specs if s.filename == biggest["filename"]), None)
        if biggest_spec is None:
            return

        def _exact():
            try:
                system_prompt = get_system_prompt(cycle, mode=DEFAULT_REVIEW_MODE)
                user_message = get_single_spec_user_message(
                    biggest_spec.content,
                    biggest_spec.filename,
                    project_context=project_context,
                    cycle=cycle,
                    mode=DEFAULT_REVIEW_MODE,
                )
                exact = count_tokens_via_api(
                    model=_model,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                if exact is None:
                    return
                fc = len(file_data)
                dispatch(lambda lc=int(exact), n=fc: self.token_gauge.update_gauge(lc, n, is_exact=True))
                dispatch(lambda lc=int(exact): self.log.log(
                    f"Exact token count (API): {lc:,} tokens for largest spec",
                    level="muted",
                ))
            except Exception:
                # Silent fallback — the cl100k_base estimate is already on screen.
                return

        threading.Thread(target=_exact, daemon=True).start()

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
        ctx = self._get_project_context()
        if ctx:
            from tiktoken import get_encoding
            ctx_tokens = len(get_encoding("cl100k_base").encode(ctx))
            self._project_context_tokens = ctx_tokens
            self._update_context_token_label()
            if ctx_tokens > PROJECT_CONTEXT_MAX_TOKENS:
                self.log.log_error(
                    f"Project Context is {ctx_tokens:,} tokens — limit is "
                    f"{PROJECT_CONTEXT_MAX_TOKENS:,}. Trim it before running."
                )
                messagebox.showerror(
                    "Project Context too large",
                    f"Project Context is {ctx_tokens:,} tokens, exceeding the "
                    f"{PROJECT_CONTEXT_MAX_TOKENS:,}-token limit.\n\n"
                    f"Trim the context (or remove some attachments) before running.",
                )
                return False
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
                f"batch mode is strongly recommended — same review prompts and criteria "
                f"at 50% lower pricing."
            )
        else:
            warning_text += (
                f"Batch mode uses the same review prompts and criteria at 50% lower pricing "
                f"({_BATCH_TIMING_COPY} instead of immediate in-session processing)."
            )

        ctk.CTkLabel(inner, text=warning_text,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            text_color=COLORS["text_secondary"],
            wraplength=460, justify="left").pack(anchor="w", padx=16, pady=(0, 16))

        # Buttons
        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))
        btn_kw = {"height": 36, "font": ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE, weight="bold"), "corner_radius": 6}

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
        self._verbose_for_review = self._verbose_var.get()
        self._export_mode_for_review = self._is_export_mode
        self._selected_cycle_label = self.cycle_selector.get()
        # Capture the segmented control's current value on the UI thread
        # (Phase 7.2 staleness-guard discipline) before kicking off the
        # background submission.
        self._review_mode_for_review = self._get_selected_review_mode()
        self.is_processing = True
        self._close_report_window()
        self.log.log("\u2500" * 40, level="muted", timestamp=False, paced=False)
        self.run_button.set_processing()
        self.progress_bar.pack(fill="x", pady=(8, 0), after=self.run_button)
        self.progress_bar.set(0); self.progress_bar.configure(mode="determinate")
        os.environ["ANTHROPIC_API_KEY"] = self.api_key_entry.get().strip()

        # Initialize diagnostics report for this run
        mode = "batch" if self._is_batch_mode else "real-time"
        self._diagnostics_report = DiagnosticsReport(
            mode=mode,
            model=MODEL_OPUS_46,
            cycle_label=self._selected_cycle_label,
            files_selected=[p.name for p in selected_files],
            project_context_tokens=self._project_context_tokens,
            cross_check_enabled=self._cross_check_for_review,
            export_mode=self._export_mode_for_review,
        )
        self._diagnostics_report.log("init", "info", f"Run started: {mode} mode, {num_specs} files, cycle {self._selected_cycle_label}")
        self.diagnostics_button.configure(state="disabled")

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

    def _make_diag_log(self, phase: str, run_epoch: int):
        """Return a log callback that writes to both the EnhancedLog and the diagnostics report.

        Phase 7.1 (audit Section 11.1): pipeline code now passes explicit
        ``level`` and ``phase`` keywords. The constructed ``phase`` is used
        as the default; when a caller (e.g., the verifier path) supplies
        ``phase=``, it overrides on a per-call basis.
        """
        ui_level_map = {
            "info": "info",
            "success": "success",
            "warning": "warning",
            "error": "error",
            "step": "step",
            "muted": "muted",
            "debug": "muted",
        }

        default_phase = phase

        def _log(msg: str, *, level: str = "info", phase: str | None = None, **_extra):
            ui_level = ui_level_map.get(level, "info")
            self._dispatch_if_current(run_epoch, lambda m=msg, lv=ui_level: self.log.log(m, level=lv))
            if self._diagnostics_report:
                self._diagnostics_report.log(phase or default_phase, level, msg)
        return _log

    def _make_diag_progress(self, phase: str, run_epoch: int):
        """Return a progress callback that writes to both UI and diagnostics."""
        default_phase = phase

        def _on_progress(pct, msg, *, phase: str | None = None, **_extra):
            self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log_step(m))
            self._dispatch_if_current(run_epoch, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))
            if self._diagnostics_report:
                self._diagnostics_report.log(phase or default_phase, "step", msg, {"progress_pct": round(pct, 1)})
        return _on_progress

    def _finalize_diagnostics(self, phase: str, level: str, message: str) -> None:
        if self._diagnostics_report:
            self._diagnostics_report.log(phase, level, message)
            self._diagnostics_report.finish()
        self.diagnostics_button.configure(state="normal")

    def _run_review_thread(self, run_epoch: int):
        diag = self._diagnostics_report
        try:
            n = len(self._selected_files_for_review)
            self._dispatch_if_current(run_epoch, lambda: self.log.log_step("Starting per-spec review..."))
            cross_check_note = " + cross-check" if self._cross_check_for_review else ""
            mode_info = f"Model: Opus 4.6  \u2022  {n} specs \u2022  1 API call per spec  \u2022  verification enabled{cross_check_note}"
            self._dispatch_if_current(run_epoch, lambda: self.log.log(mode_info, level="muted"))
            if diag:
                diag.log("review", "step", f"Starting real-time review of {n} specs")

            review_log = self._make_diag_log("review", run_epoch)
            review_progress = self._make_diag_progress("review", run_epoch)
            result = run_review(
                input_dir=self.input_dir,
                files=self._selected_files_for_review,
                project_context=self._project_context_for_review,
                model=MODEL_OPUS_46,
                verify=True,
                cross_check=self._cross_check_for_review,
                dry_run=False, verbose=False,
                cycle=AVAILABLE_CYCLES.get(self._selected_cycle_label, DEFAULT_CYCLE),
                mode=self._review_mode_for_review,
                log=review_log,
                progress=review_progress,
            )
            # Capture structured diagnostics from the result
            if diag and result.review_result:
                rv = result.review_result
                # Phase 9 plan 13.4: include the configured output cap so the
                # diagnostics summary can compute utilization vs ceiling.
                from .api_config import review_max_tokens as _review_cap
                review_cap = _review_cap(batch=False, model=rv.model)
                diag.log("review", "success", "Review completed", {
                    "input_tokens": rv.input_tokens,
                    "output_tokens": rv.output_tokens,
                    "cache_creation_input_tokens": rv.cache_creation_input_tokens,
                    "cache_read_input_tokens": rv.cache_read_input_tokens,
                    "elapsed_seconds": round(rv.elapsed_seconds, 2),
                    "stop_reason": rv.stop_reason,
                    "parse_status": rv.parse_status,
                    "max_output_tokens": review_cap,
                    "severity_counts": {
                        "CRITICAL": rv.critical_count,
                        "HIGH": rv.high_count,
                        "MEDIUM": rv.medium_count,
                        "GRIPES": rv.gripe_count,
                    },
                    "total_findings": rv.total_count,
                })
                
                if rv.error:
                    diag.log("review", "error", f"Review error: {rv.error}")
                    # Surface per-spec errors prominently in diagnostics
                    diag.log("review", "warning", "One or more specs failed during review — check Reviewer's Notes for details.")
                
                if result.cross_check_result:
                    cc = result.cross_check_result
                    diag.log("cross_check", "info", f"Cross-check: {cc.cross_check_status}", {
                        "finding_count": len(cc.findings),
                        "input_tokens": cc.input_tokens,
                        "output_tokens": cc.output_tokens,
                        "cache_creation_input_tokens": cc.cache_creation_input_tokens,
                        "cache_read_input_tokens": cc.cache_read_input_tokens,
                    })
                # Verification verdict breakdown (includes explanation for failure diagnosis)
                for f in rv.findings:
                    if f.verification:
                        v = f.verification
                        event_data = {
                            "verdict": v.verdict,
                            "finding_severity": f.severity,
                            "confidence": f.confidence,
                            "explanation": v.explanation or "",
                            # Phase 3 evidence model — feeds DiagnosticsReport.summary()
                            "grounded": v.grounded,
                            "model_used": v.model_used,
                            "escalated": v.escalated,
                            "cache_status": v.cache_status,
                            "web_search_requests": v.web_search_requests,
                            "successful_source_count": v.successful_source_count,
                            "search_error_count": v.search_error_count,
                        }
                        if v.sources:
                            event_data["sources"] = v.sources[:3]
                        if v.correction:
                            event_data["correction"] = v.correction
                        diag.log("verification", "info", f"Verified: {f.fileName} — {v.verdict}", event_data)
                # Summarize verification failures for quick diagnosis
                unverified = [f for f in rv.findings if f.verification and f.verification.verdict == "UNVERIFIED"]
                if unverified:
                    failure_reasons = list(set(
                        (f.verification.explanation or "No explanation provided")
                        for f in unverified
                    ))
                    diag.log("verification", "warning",
                        f"{len(unverified)}/{len(rv.findings)} findings UNVERIFIED",
                        {"failure_reasons": failure_reasons})
                if result.leed_alerts:
                    diag.log("preprocessing", "warning", f"LEED alerts: {len(result.leed_alerts)}")
                if result.placeholder_alerts:
                    diag.log("preprocessing", "warning", f"Placeholder alerts: {len(result.placeholder_alerts)}")
            self._dispatch_if_current(run_epoch, lambda: self._on_review_complete(result))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            if diag:
                diag.log("review", "error", f"Review failed: {e}", {"traceback": traceback.format_exc()})
            self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))

    def _on_review_complete(self, result):
        
        self.progress_bar.set(1.0)
        self._last_result = result
        if result.review_result:
            rv = result.review_result
            # --- FIX 3: Distinguish zero-findings-with-errors from clean passes ---
            has_review_errors = bool(rv.error)
            if has_review_errors:
                self.log.log_warning("Review completed with errors — some specs failed. See report for details.")
                self.log.log_warning(rv.error)
            else:
                self.log.log_success("Review complete!")
            self.log.log(f"Findings: {rv.critical_count} critical, {rv.high_count} high, {rv.medium_count} medium, {rv.gripe_count} gripes", level="info")
            
            if result.cross_check_result and result.cross_check_result.findings:
                cc = result.cross_check_result
                self.log.log(f"Cross-check: {len(cc.findings)} coordination issues found", level="info")
            total_elapsed = result.total_elapsed_seconds if getattr(result, "total_elapsed_seconds", None) is not None else rv.elapsed_seconds
            self.log.log(f"Time: {total_elapsed:.1f}s", level="muted")
            if getattr(self, "_export_mode_for_review", False):
                export_status = self._export_report_to_file(result)
                if export_status == "canceled":
                    self.log.log_warning("Export canceled; results are still available in memory.")
                    self._finalize_diagnostics("finalization", "info", "Run completed after export canceled")
                elif export_status == "error":
                    self.log.log_warning("Export failed. Retry export or switch output mode to 'View in App' to open the report window.")
                    self._finalize_diagnostics("finalization", "warning", "Run completed with export failure")
                elif export_status == "success":
                    self._show_edit_selection_dialog(result)
            else:
                self._open_report_window(
                    rv,
                    result.files_reviewed,
                    result.leed_alerts,
                    result.placeholder_alerts,
                    result.cross_check_result,
                    verbose=getattr(self, "_verbose_for_review", True),
                )
        delete_batch_state()
        if not (getattr(self, "_export_mode_for_review", False) and result.review_result):
            self._finalize_diagnostics("finalization", "success", "Run completed successfully")
        self.run_button.set_complete()
        self.after(2500, self._reset_ui)

    def _export_report_to_file(self, result) -> str:
        default_name = f"spec-critic-report-{datetime.now().strftime('%Y-%m-%d')}.docx"
        path = filedialog.asksaveasfilename(
            title="Save Review Report",
            defaultextension=".docx",
            filetypes=[("Word Documents", "*.docx"), ("All Files", "*.*")],
            initialfile=default_name,
        )
        if not path:
            self.log.log_warning("Export canceled")
            return "canceled"
        try:
            output_path = Path(path)
            self.log.log_step(f"Exporting report to {output_path.name}...")
            export_report(
                result,
                output_path,
                project_context=getattr(self, "_project_context_for_review", ""),
                verbose=getattr(self, "_verbose_for_review", True),
            )
            self.log.log_success(f"Report saved: {output_path}")
            return "success"
        except Exception as e:
            self.log.log_error(f"Export failed: {e}")
            return "error"

    def _show_edit_selection_dialog(self, result) -> None:
        extracted_specs = list(self._extracted_specs)
        source_paths = list(self._selected_files_for_review)

        if not extracted_specs and source_paths:
            self.log.log_step("Re-extracting specs for edit application...")
            extracted_specs = [extract_text(path) for path in source_paths if path.exists()]

        has_maps = any(spec.paragraph_map is not None for spec in extracted_specs)
        has_source_files = all(path.exists() for path in source_paths)
        if not has_maps and not has_source_files:
            self.log.log_warning(
                "Cannot apply edits: original spec files are not accessible and paragraph maps are unavailable."
            )
            return

        review_findings = list(result.review_result.findings) if result.review_result else []
        cross_check_findings = (
            list(result.cross_check_result.findings)
            if result.cross_check_result and result.cross_check_result.findings
            else []
        )

        candidates = classify_edit_candidates(
            review_findings,
            cross_check_findings=cross_check_findings,
        )
        eligible_count = sum(1 for c in candidates if c.eligible)
        ineligible_count = len(candidates) - eligible_count
        self.log.log(
            f"Edit candidates: {eligible_count} eligible, {ineligible_count} ineligible "
            f"(of {len(candidates)} total findings)",
            level="info",
        )
        if self._diagnostics_report:
            self._diagnostics_report.log(
                "edit_selection",
                "info",
                f"Edit candidates classified: {eligible_count} eligible, {ineligible_count} ineligible",
                {"eligible": eligible_count, "ineligible": ineligible_count, "total": len(candidates)},
            )
            for candidate in candidates:
                if candidate.eligible or not candidate.ineligible_reason:
                    continue
                self._diagnostics_report.log(
                    "edit_selection",
                    "info",
                    f"Ineligible finding {candidate.finding_index}: {candidate.ineligible_reason}",
                    {"finding_index": candidate.finding_index, "reason": candidate.ineligible_reason},
                )

        if not any(candidate.eligible for candidate in candidates):
            reasons = {c.ineligible_reason for c in candidates if c.ineligible_reason}
            reason_str = "; ".join(sorted(reasons)) if reasons else "unknown"
            self.log.log(
                f"No findings eligible for auto-apply ({len(candidates)} total). Reasons: {reason_str}",
                level="muted",
            )
            self._finalize_diagnostics("finalization", "success", "Run completed without eligible auto-edits")
            return

        def on_apply(selected_indices: list[int]):
            self._apply_selected_edits(
                selected_indices,
                review_findings,
                cross_check_findings,
                extracted_specs,
                source_paths,
            )

        def on_dismiss():
            self._finalize_diagnostics(
                "finalization", "info",
                "Run completed after edit selection dismissed",
            )

        EditSelectionDialog(
            self, candidates=candidates,
            on_apply=on_apply, on_dismiss=on_dismiss,
        )

    def _apply_selected_edits(
        self,
        selected_indices: list[int],
        all_findings: list[Finding],
        cross_check_findings: list[Finding],
        extracted_specs: list[ExtractedSpec],
        source_paths: list[Path],
    ) -> None:
        output_dir = filedialog.askdirectory(
            title="Select output directory for edited specs",
            initialdir=str(source_paths[0].parent) if source_paths else None,
        )
        if not output_dir:
            self.log.log("Edit application canceled.", level="muted")
            self._finalize_diagnostics("finalization", "info", "Run completed after user declined edit application")
            return

        output_path = Path(output_dir)

        run_epoch = self._next_run_epoch()

        if self._diagnostics_report:
            self._diagnostics_report.log(
                "edit_application", "step", f"Applying {len(selected_indices)} edits to specs"
            )

        def _do_apply():
            try:
                reports = execute_edit_plan(
                    selected_finding_indices=selected_indices,
                    all_findings=all_findings,
                    cross_check_findings=cross_check_findings,
                    extracted_specs=extracted_specs,
                    source_paths=source_paths,
                    output_dir=output_path,
                    log=lambda msg: self._dispatch_if_current(
                        run_epoch, lambda m=msg: self.log.log(m, level="info")
                    ),
                )
                self._dispatch_if_current(
                    run_epoch, lambda r=reports: self._on_edits_applied(r)
                )
            except Exception as e:
                import traceback

                err = f"{e}\n{traceback.format_exc()}"
                self._dispatch_if_current(
                    run_epoch,
                    lambda: self.log.log_error(f"Edit application failed: {err}"),
                )
                self._dispatch_if_current(
                    run_epoch,
                    lambda: self._finalize_diagnostics("finalization", "warning", "Run completed with edit application failure"),
                )

        threading.Thread(target=_do_apply, daemon=True).start()

    def _on_edits_applied(self, reports: list[EditReport]) -> None:
        total_applied = sum(report.edits_applied for report in reports)
        total_skipped = sum(report.edits_skipped for report in reports)
        total_failed = sum(report.edits_failed for report in reports)
        self.log.log_success(
            f"Edits complete: {total_applied} applied, {total_skipped} skipped, {total_failed} failed"
        )
        EditSummaryDialog(self, edit_reports=reports)
        if self._diagnostics_report:
            # Phase 7.3 actionable diagnostics: aggregate per-report counts
            # and outcome reasons so the summary shows what required manual
            # follow-up rather than only writing freeform timeline entries.
            for report in reports:
                self._diagnostics_report.record_edit_report(
                    applied=report.edits_applied,
                    skipped=report.edits_skipped,
                    failed=report.edits_failed,
                )
                for outcome in getattr(report, "outcomes", []) or []:
                    if outcome.status in ("skipped", "failed"):
                        reason = (outcome.detail or outcome.status).strip().lower()
                        # Bucket common locator-skip reasons; keep specific
                        # detail under a separate key when present.
                        if "ambiguous" in reason:
                            bucket = "ambiguous"
                        elif "not found" in reason or "not_found" in reason:
                            bucket = "not_found"
                        elif "manual" in reason:
                            bucket = "manual_review"
                        elif outcome.status == "failed":
                            bucket = "failed"
                        else:
                            bucket = "skipped_other"
                        self._diagnostics_report.record_edit_skip(bucket)
                self._diagnostics_report.log(
                    "edit_application",
                    "info",
                    (
                        f"{report.source_path.name}: {report.edits_applied} applied, "
                        f"{report.edits_skipped} skipped, {report.edits_failed} failed"
                    ),
                    {
                        "file": report.source_path.name,
                        "applied": report.edits_applied,
                        "skipped": report.edits_skipped,
                        "failed": report.edits_failed,
                    },
                )
            self._diagnostics_report.log(
                "edit_application",
                "success" if all(report.edits_failed == 0 for report in reports) else "warning",
                "Edit application complete",
            )
        self._finalize_diagnostics("finalization", "success", "Run completed after edit application")

    def _on_review_error(self, err):
        self.progress_bar.pack_forget()
        self.log.log_error(f"Review failed: {err}")
        self._finalize_diagnostics("error", "error", f"Run failed: {err}")
        self.run_button.set_ready(); self.is_processing = False

    # ----- Batch mode -----

    def _submit_batch_thread(self, run_epoch: int):
        diag = self._diagnostics_report
        try:
            if diag:
                diag.log("batch_submit", "step", "Preparing batch submission")

            submission = start_batch_review(
                input_dir=self.input_dir,
                files=self._selected_files_for_review,
                project_context=self._project_context_for_review,
                model=MODEL_OPUS_46,
                cycle=AVAILABLE_CYCLES.get(self._selected_cycle_label, DEFAULT_CYCLE),
                cross_check_enabled=self._cross_check_for_review,
                export_mode=self._export_mode_for_review,
                mode=self._review_mode_for_review,
                log=self._make_diag_log("batch_submit", run_epoch),
                progress=self._make_diag_progress("batch_submit", run_epoch),
            )
            if diag:
                diag.log("batch_submit", "success", f"Batch submitted: {submission.job.batch_id}", {
                    "batch_id": submission.job.batch_id,
                    "files_queued": len(submission.files_reviewed),
                })
            save_batch_state(build_resume_state(phase=PHASE_REVIEW_POLL, submission=submission))
            self._dispatch_if_current(run_epoch, lambda: self._on_batch_submitted(submission))
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()}"
            if diag:
                diag.log("batch_submit", "error", f"Batch submission failed: {e}", {"traceback": traceback.format_exc()})
            self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))

    def _on_batch_submitted(self, submission: BatchSubmission):
        self._batch_submission = submission
        self.progress_bar.set(0.4)
        self.log.log_success(f"Batch submitted: {submission.job.batch_id}")
        self.log.log(f"  {len(submission.files_reviewed)} specs queued \u2022 50% cost savings", level="muted")
        self.log.log_step(f"Polling for results ({_BATCH_TIMING_COPY})...")
        self.run_button.configure(text="Polling...")
        self._poll_batch()

    def _poll_batch(self):
        if self._batch_submission is None:
            return
        run_epoch = self._next_run_epoch()
        threading.Thread(target=self._poll_and_collect_thread, args=(run_epoch,), daemon=True).start()

    def _update_poll_progress(self, status: BatchStatus):
        diag = self._diagnostics_report
        batch_pct = 0.40 + (status.progress_pct / 100.0) * 0.55
        self.progress_bar.set(min(batch_pct, 0.95))
        self.log.log(
            f"  Batch: {status.succeeded} done, {status.processing} processing, "
            f"{status.errored} errors \u2022 {status.progress_pct:.0f}%",
            level="info", paced=False,
        )
        if diag:
            diag.log("batch_poll", "info", f"Poll: {status.succeeded}/{status.total} done, {status.errored} errors", {
                "succeeded": status.succeeded,
                "processing": status.processing,
                "errored": status.errored,
                "canceled": status.canceled,
                "expired": status.expired,
                "total": status.total,
                "progress_pct": round(status.progress_pct, 1),
            })
    def _poll_and_collect_thread(self, run_epoch: int):
        if self._batch_submission is None:
            return
        outcome = poll_batch_bounded(
            self._batch_submission.job.batch_id,
            policy=DEFAULT_REVIEW_POLL_POLICY,
            log=self._make_diag_log("batch_poll", run_epoch),
            progress_cb=lambda status: self._dispatch_if_current(run_epoch, lambda s=status: self._update_poll_progress(s)),
        )
        if outcome.detached or outcome.poll_failed:
            save_batch_state(build_resume_state(phase=PHASE_REVIEW_POLL, submission=self._batch_submission))
            reason = outcome.detach_reason or outcome.poll_error or "unknown"
            msg = (
                f"Batch polling stopped: {reason}. Batch ID {self._batch_submission.job.batch_id} "
                "may still be running remotely. Resume later to continue."
            )
            self._dispatch_if_current(run_epoch, lambda m=msg: self._on_review_error(m))
            return
        if self._batch_submission is not None:
            save_batch_state(build_resume_state(phase=PHASE_REVIEW_COLLECT, submission=self._batch_submission))
        self._dispatch_if_current(run_epoch, lambda: self.log.log_success("Batch complete — collecting results..."))
        self._dispatch_if_current(run_epoch, self._collect_batch_results)

    # Backward-compatible helper retained for tests and legacy call paths.
    def _on_poll_result(self, status: BatchStatus):
        if hasattr(self, "_update_poll_progress"):
            self._update_poll_progress(status)
        normalized_status = status.status.replace("-", "_")
        if normalized_status in ("ended", "failed", "expired", "canceled"):
            if self._batch_submission is not None:
                save_batch_state(build_resume_state(phase=PHASE_REVIEW_COLLECT, submission=self._batch_submission))
            self._collect_batch_results()

    def _collect_batch_results(self):
        run_epoch = self._next_run_epoch()
        diag = self._diagnostics_report
        def _do_collect():
            try:
                if self._batch_submission is None:
                    raise RuntimeError("No active batch submission to collect.")
                cycle = AVAILABLE_CYCLES.get(getattr(self._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)

                if diag:
                    diag.log("batch_collect", "step", "Collecting review batch results")
                review_state = collect_review_batch_results(
                    self._batch_submission,
                    log=self._make_diag_log("batch_collect", run_epoch),
                )
                rv = review_state.review_result
                if diag:
                    diag.log("batch_collect", "success", "Review results collected", {
                        "input_tokens": rv.input_tokens,
                        "output_tokens": rv.output_tokens,
                        "cache_creation_input_tokens": rv.cache_creation_input_tokens,
                        "cache_read_input_tokens": rv.cache_read_input_tokens,
                        "elapsed_seconds": round(rv.elapsed_seconds, 2),
                        "parse_status": rv.parse_status,
                        "severity_counts": {
                            "CRITICAL": rv.critical_count, "HIGH": rv.high_count,
                            "MEDIUM": rv.medium_count, "GRIPES": rv.gripe_count,
                        },
                        "total_findings": rv.total_count,
                    })
                    if rv.error:
                        diag.log("batch_collect", "error", f"Review errors: {rv.error}")

                verifiable_findings = list(rv.findings)
                verification_completed = False
                if review_state.truncated_specs:
                    # Phase 7.3 actionable diagnostics: surface failed specs
                    # in the structured report so they appear in the summary
                    # without needing to scan the timeline.
                    if diag:
                        for spec_name in review_state.truncated_specs:
                            diag.record_failed_spec(spec_name)
                    for spec_name in review_state.truncated_specs:
                        self._dispatch_if_current(
                            run_epoch,
                            lambda n=spec_name: self.log.log_warning(f"⚠ Review failed for {n} — see report for details"),
                        )
                if verifiable_findings:
                    self._dispatch_if_current(run_epoch, lambda: self.run_button.configure(text="Verifying findings..."))
                    if diag:
                        diag.log("verification", "step", f"Starting verification batch for {len(verifiable_findings)} findings")
                    verification_job = start_batch_verification(
                        verifiable_findings,
                        cycle=cycle,
                        log=self._make_diag_log("verification", run_epoch),
                        progress=self._make_diag_progress("verification", run_epoch),
                    )
                    if verification_job is None:
                        # Phase 3: every finding resolved locally / from cache.
                        if diag:
                            diag.log("verification", "info", "Verification: all findings resolved locally; no batch submitted.")
                        verification_completed = True
                    else:
                        if diag:
                            diag.log("verification", "info", f"Verification batch submitted: {verification_job.batch_id}", {
                                "batch_id": verification_job.batch_id,
                            })
                        save_batch_state(build_resume_state(
                            phase=PHASE_VERIFICATION_WAVE_POLL,
                            submission=self._batch_submission,
                            review_state=review_state,
                            verification_batch=verification_job,
                            verification_started=True,
                        ))
                        collect_batch_verification_results(
                            verification_job,
                            verifiable_findings,
                            cycle=cycle,
                            log=self._make_diag_log("verification", run_epoch),
                            progress=self._make_diag_progress("verification", run_epoch),
                        )
                        verification_completed = True
                    if diag:
                        verdicts = {}
                        for f in verifiable_findings:
                            if f.verification:
                                v = f.verification.verdict
                                verdicts[v] = verdicts.get(v, 0) + 1
                                diag.log("verification", "info",
                                    f"Verified: {f.fileName} — {f.verification.verdict}", {
                                        "verdict": f.verification.verdict,
                                        "finding_severity": f.severity,
                                        "confidence": f.confidence,
                                        "explanation": f.verification.explanation or "",
                                    })
                        diag.log("verification", "success", "Verification complete", {"verdicts": verdicts})

                save_batch_state(build_resume_state(
                    phase=PHASE_CROSS_CHECK,
                    submission=self._batch_submission,
                    review_state=review_state,
                    verification_started=bool(verifiable_findings),
                    verification_completed=verification_completed,
                ))
                if diag:
                    diag.log("cross_check", "step", "Running cross-spec coordination check")
                self._dispatch_if_current(run_epoch, lambda: self.run_button.configure(text="Cross-check (live API)..."))
                self._dispatch_if_current(run_epoch, lambda: self.log.log_step("Running cross-spec coordination check (live API)..."))
                review_state = run_cross_check_for_batch(
                    review_state,
                    specs=getattr(self._batch_submission, "prepared_specs", None),
                    project_context=getattr(self, "_project_context_for_review", ""),
                    cycle=cycle,
                    log=self._make_diag_log("cross_check", run_epoch),
                )
                if review_state.cross_check_skipped_due_to_missing_specs:
                    self._dispatch_if_current(run_epoch, lambda: self.log.log_warning(
                        "Cross-check skipped due to missing resumable extracted specs."
                    ))
                    if diag:
                        diag.log("cross_check", "warning", "Cross-check skipped: missing resumable extracted specs")
                if diag and review_state.cross_check_result:
                    cc = review_state.cross_check_result
                    diag.log("cross_check", "info", f"Cross-check: {cc.cross_check_status}", {
                        "finding_count": len(cc.findings),
                        "input_tokens": cc.input_tokens,
                        "output_tokens": cc.output_tokens,
                        "cache_creation_input_tokens": cc.cache_creation_input_tokens,
                        "cache_read_input_tokens": cc.cache_read_input_tokens,
                    })

                cross_check_findings = list(review_state.cross_check_result.findings) if review_state.cross_check_result and review_state.cross_check_result.findings else []
                if cross_check_findings:
                    self._dispatch_if_current(run_epoch, lambda: self.run_button.configure(text="Verifying cross-check..."))
                    if diag:
                        diag.log("cross_check_verification", "step", f"Verifying {len(cross_check_findings)} cross-check findings")
                    cross_check_verification_job = start_batch_verification(
                        cross_check_findings,
                        cycle=cycle,
                        log=self._make_diag_log("cross_check_verification", run_epoch),
                        progress=self._make_diag_progress("cross_check_verification", run_epoch),
                    )
                    if cross_check_verification_job is None:
                        if diag:
                            diag.log("cross_check_verification", "info", "Cross-check verification: all findings resolved locally; no batch submitted.")
                    else:
                        save_batch_state(build_resume_state(
                            phase=PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
                            submission=self._batch_submission,
                            review_state=review_state,
                            verification_batch=cross_check_verification_job,
                            cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                            verification_started=bool(verifiable_findings),
                            verification_completed=verification_completed,
                        ))
                        collect_batch_verification_results(
                            cross_check_verification_job,
                            cross_check_findings,
                            cycle=cycle,
                            log=self._make_diag_log("cross_check_verification", run_epoch),
                            progress=self._make_diag_progress("cross_check_verification", run_epoch),
                        )
                        if diag:
                            diag.log("cross_check_verification", "success", "Cross-check verification complete")

                if diag:
                    diag.log("finalization", "step", "Finalizing batch results")
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
                if diag:
                    diag.log("batch_collect", "error", f"Batch collection failed: {e}", {"traceback": traceback.format_exc()})
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
        btn_kw = {"height": 34, "font": ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), "corner_radius": 6}

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
        verbose_var = getattr(self, "_verbose_var", None)
        self._verbose_for_review = verbose_var.get() if verbose_var is not None else bool(getattr(self, "_verbose_for_review", True))
        self.output_selector.set("Export Report" if self._export_mode_for_review else "View in App")
        self._on_output_mode_change(self.output_selector.get())
        if submission.cycle_label in AVAILABLE_CYCLES:
            self.cycle_selector.set(submission.cycle_label)
        self._cross_check_var.set(bool(getattr(submission, "cross_check_enabled", False)))
        # Phase 8: restore the review mode that produced the saved batch so
        # any retry/repair calls (e.g. truncated review repair) keep using
        # the same prompt path.
        restored_mode = coerce_review_mode(getattr(submission, "review_mode", DEFAULT_REVIEW_MODE.value))
        self._review_mode = restored_mode
        self._review_mode_for_review = restored_mode
        try:
            self.review_mode_selector.set(REVIEW_MODE_PROFILES[restored_mode].label)
            self._review_mode_hint.configure(text=REVIEW_MODE_PROFILES[restored_mode].short_description)
        except Exception:
            pass
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
        if phase in (PHASE_VERIFICATION_POLL, PHASE_VERIFICATION_WAVE_POLL):
            if not self._is_valid_verification_resume_state(loaded_state):
                self.log.log_error("Saved verification resume state is incomplete. Discarding it.")
                delete_batch_state()
                self._reset_ui()
                return
            self._resume_verification_poll(loaded_state)
            return
        if phase in (PHASE_CROSS_CHECK_VERIFICATION_POLL, PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL):
            if not self._is_valid_verification_resume_state(loaded_state):
                self.log.log_error("Saved cross-check verification resume state is incomplete. Discarding it.")
                delete_batch_state()
                self._reset_ui()
                return
            self._resume_cross_check_verification_poll(loaded_state)
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
        if phase == PHASE_CROSS_CHECK:
            review_state: CollectedBatchState | None = loaded_state.get("review_state")
            if review_state is None:
                self.log.log_error("Saved cross-check resume state is incomplete. Discarding it.")
                delete_batch_state()
                self._reset_ui()
                return
            run_epoch = self._next_run_epoch()

            def _do_resume_cross_check():
                try:
                    cycle = AVAILABLE_CYCLES.get(getattr(self._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)

                    def _on_progress(pct, msg):
                        self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log_step(m))
                        self._dispatch_if_current(run_epoch, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

                    review_state_local = run_cross_check_for_batch(
                        review_state,
                        specs=getattr(self._batch_submission, "prepared_specs", None),
                        project_context=getattr(self, "_project_context_for_review", ""),
                        cycle=cycle,
                        log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                    )
                    cross_check_findings = list(review_state_local.cross_check_result.findings) if review_state_local.cross_check_result and review_state_local.cross_check_result.findings else []
                    if cross_check_findings:
                        cross_check_verification_job = start_batch_verification(
                            cross_check_findings,
                            cycle=cycle,
                            log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                            progress=_on_progress,
                        )
                        if cross_check_verification_job is not None:
                            save_batch_state(build_resume_state(
                                    phase=PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
                                submission=self._batch_submission,
                                review_state=review_state_local,
                                verification_batch=cross_check_verification_job,
                                cross_check_skipped_due_to_missing_specs=review_state_local.cross_check_skipped_due_to_missing_specs,
                                verification_started=True,
                                verification_completed=True,
                            ))
                            collect_batch_verification_results(
                                cross_check_verification_job,
                                cross_check_findings,
                                cycle=cycle,
                                log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                                progress=_on_progress,
                            )
                    result = finalize_batch_result(review_state_local)
                    self._dispatch_if_current(run_epoch, lambda r=result: self._on_review_complete(r))
                except Exception as e:
                    import traceback
                    err = f"{e}\n{traceback.format_exc()}"
                    self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))

            threading.Thread(target=_do_resume_cross_check, daemon=True).start()
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
        verifiable_findings = list(review_state.review_result.findings)

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
                save_batch_state(build_resume_state(
                    phase=PHASE_CROSS_CHECK,
                    submission=self._batch_submission,
                    review_state=review_state,
                    cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                    verification_started=True,
                    verification_completed=True,
                ))
                review_state_local = run_cross_check_for_batch(
                    review_state,
                    specs=getattr(self._batch_submission, "prepared_specs", None),
                    project_context=getattr(self, "_project_context_for_review", ""),
                    cycle=cycle,
                    log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                )
                cross_check_findings = list(review_state_local.cross_check_result.findings) if review_state_local.cross_check_result and review_state_local.cross_check_result.findings else []
                if cross_check_findings:
                    cross_check_verification_job = start_batch_verification(
                        cross_check_findings,
                        cycle=cycle,
                        log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                        progress=_on_progress,
                    )
                    if cross_check_verification_job is not None:
                        save_batch_state(build_resume_state(
                                phase=PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL,
                            submission=self._batch_submission,
                            review_state=review_state_local,
                            verification_batch=cross_check_verification_job,
                            cross_check_skipped_due_to_missing_specs=review_state_local.cross_check_skipped_due_to_missing_specs,
                            verification_started=True,
                            verification_completed=True,
                        ))
                        collect_batch_verification_results(
                            cross_check_verification_job,
                            cross_check_findings,
                            cycle=cycle,
                            log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                            progress=_on_progress,
                        )
                result = finalize_batch_result(review_state_local)
                self._dispatch_if_current(run_epoch, lambda r=result: self._on_review_complete(r))
            except Exception as e:
                import traceback
                err = f"{e}\n{traceback.format_exc()}"
                self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))
        threading.Thread(target=_do_resume_verification, daemon=True).start()

    def _resume_cross_check_verification_poll(self, loaded_state: dict):
        run_epoch = self._next_run_epoch()
        review_state: CollectedBatchState = loaded_state["review_state"]
        verification_job = loaded_state["verification_batch"]
        cycle = AVAILABLE_CYCLES.get(getattr(self._batch_submission, "cycle_label", DEFAULT_CYCLE.label), DEFAULT_CYCLE)
        cross_check_findings = list(review_state.cross_check_result.findings) if review_state.cross_check_result and review_state.cross_check_result.findings else []

        def _do_resume_cross_check_verification():
            try:
                def _on_progress(pct, msg):
                    self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log_step(m))
                    self._dispatch_if_current(run_epoch, lambda p=pct: self.progress_bar.set(max(0.0, min(p / 100.0, 1.0))))

                if cross_check_findings:
                    collect_batch_verification_results(
                        verification_job,
                        cross_check_findings,
                        cycle=cycle,
                        log=lambda msg: self._dispatch_if_current(run_epoch, lambda m=msg: self.log.log(m, level="info")),
                        progress=_on_progress,
                    )
                save_batch_state(build_resume_state(
                    phase=PHASE_FINALIZE,
                    submission=self._batch_submission,
                    review_state=review_state,
                    cross_check_skipped_due_to_missing_specs=review_state.cross_check_skipped_due_to_missing_specs,
                    verification_started=True,
                    verification_completed=True,
                ))
                result = finalize_batch_result(review_state)
                self._dispatch_if_current(run_epoch, lambda r=result: self._on_review_complete(r))
            except Exception as e:
                import traceback
                err = f"{e}\n{traceback.format_exc()}"
                self._dispatch_if_current(run_epoch, lambda: self._on_review_error(err))

        threading.Thread(target=_do_resume_cross_check_verification, daemon=True).start()

    # ----- Pop-out report window -----

    def _open_report_window(self, review, files_reviewed, leed_alerts, placeholder_alerts, cross_check_result=None, verbose: bool = True):
        self._close_report_window()
        self._report_window = ReportWindow(
            self, review=review, files_reviewed=files_reviewed,
            leed_alerts=leed_alerts, placeholder_alerts=placeholder_alerts,
            project_context=getattr(self, "_project_context_for_review", ""),
            cross_check_result=cross_check_result,
            verbose=verbose,
        )

    def _close_report_window(self):
        if self._report_window is not None:
            try: self._report_window.destroy()
            except Exception: pass
            self._report_window = None

    def _open_diagnostics_window(self):
        if self._diagnostics_report is None:
            return
        if self._diagnostics_window is not None:
            try: self._diagnostics_window.destroy()
            except Exception: pass
        self._diagnostics_window = DiagnosticsWindow(self, report=self._diagnostics_report)



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
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
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
                "assigned a severity (Critical, High, Medium, or Gripe) and a confidence score. "
                "The Review Mode selector controls scope: Strict (evidence-backed contradictions "
                "and code-cycle issues only), Comprehensive (adds AEC constructability and "
                "coordination), or Safe edit (only findings that can be expressed as a precise "
                "auto-applicable edit)."
            )),
            ("4.  Deduplication", (
                "When the same issue appears across multiple specs \u2014 like an outdated "
                "seismic code reference \u2014 duplicates are consolidated into a single "
                "finding that lists all affected files. Per-file edit occurrences are "
                "preserved internally so multi-file edits can target every affected spec."
            )),
            ("5.  Cross-Spec Coordination  (optional)", (
                "If enabled, a separate Opus 4.6 call analyzes the full text of all your "
                "specs together using the 1M token context window. It catches contradictions "
                "between specs, missing cross-references, scope gaps and overlaps, "
                "inconsistent equipment data, and division-of-work conflicts. Large projects "
                "are chunked by CSI division (21 / 22 / 23 / Controls) and merged. Cross-check "
                "runs in parallel with verification to reduce wall-clock time."
            )),
            ("6.  Verification", (
                "Every finding that needs external grounding is checked in a secondary AI "
                "pass with web search. The default verifier is Claude Sonnet 4.6 (faster and "
                "cheaper); Opus 4.6 is used as an escalation model for low-confidence "
                "Critical/High findings. Verdicts are Confirmed, Corrected, Disputed, or "
                "Unverified, and a verdict cannot be marked Confirmed/Corrected unless real "
                "web evidence was actually returned. Internal-only issues (placeholders, "
                "duplicates, internal contradictions) are resolved locally without web search. "
                "This is an AI-assisted check, not a substitute for engineer review."
            )),
            ("7.  Edit Safety Classification", (
                "Each finding is classified into an edit-safety category: Auto-safe "
                "(unambiguous single-paragraph match), Auto-with-caution (exact match with "
                "minor risk), Manual-review (ambiguous or structurally complex), or "
                "Report-only (no safe edit possible). Ambiguous matches, missing ADD anchors, "
                "and table/header/footer/rich-format edits are never auto-applied."
            )),
            ("8.  Output", (
                "Results can be viewed in-app, exported as a Word report, or used to produce "
                "an edited copy of each spec. Auto-edit mode applies surgical changes in a "
                "safe order with revalidation immediately before each mutation. Annotation "
                "mode writes a copy with yellow-highlighted suggestion paragraphs without "
                "changing the original text. The source files are never overwritten."
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
                font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
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
                "Spec Critic is a review assistant \u2014 it never modifies your source "
                "documents. Auto-edit and annotation modes always write to a copy. "
                "It\u2019s advisory only and not a substitute for AHJ review. Code "
                "citations should still be spot-checked by the engineer of record."
            ),
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            text_color=COLORS["text_secondary"],
            wraplength=520, justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 10))

        ctk.CTkButton(
            outer, text="Close", width=100, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
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
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
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
                "Real-time mode processes the review immediately in the current "
                "session — faster turnaround but higher cost. Batch mode queues all specs for processing at 50% "
                f"cost savings, with results {_BATCH_TIMING_COPY}. "
                "For more than a few specs, batch mode is strongly recommended."
            )),
            ("5.  Select Code Cycle", (
                "Choose the California code cycle for your project (2022 or "
                "2025). This determines which edition of CBC, CMC, CPC, Energy "
                "Code, CALGreen, and ASCE 7 the reviewer checks against."
            )),
            ("6.  Choose Review Mode", (
                "Strict reports only evidence-backed contradictions, code-cycle "
                "issues, and invalid references — fewer findings, higher precision. "
                "Comprehensive (the default) adds AEC constructability, coordination, "
                "TAB/commissioning, schedules, controls, closeout, and material "
                "coordination issues. Safe edit only emits findings whose fix is a "
                "precise, unambiguous, low-risk edit — useful when you intend to use "
                "the auto-edit output."
            )),
            ("7.  Enable Cross-Spec Coordination (Optional)", (
                "Check this option to run a separate coordination analysis that "
                "sends all spec content to Claude in a single call. This catches "
                "contradictions between specs, missing cross-references, and "
                "scope gaps that per-spec review cannot detect. Large projects are "
                "automatically chunked by CSI division when the combined input "
                "exceeds the recommended token ceiling."
            )),
            ("8.  Choose Output Mode", (
                "'View in App' renders results in a pop-out report window with "
                "collapsible finding cards. 'Export Report' saves a formatted "
                ".docx report. 'Auto-Edit' writes an edited copy of each spec — "
                "only Auto-safe findings are applied; ambiguous, table, header/"
                "footer, and richly formatted edits are downgraded to manual review. "
                "'Annotate' writes a copy with yellow-highlighted suggestion "
                "paragraphs after each anchor without changing the original text. "
                "Source files are never overwritten."
            )),
            ("9.  Run the Review", (
                "Click Run Review (real-time) or Submit Batch (batch mode). "
                "The activity log shows progress. In batch mode, you can close "
                "the app and reopen it later — the pending batch state is saved "
                "and you will be prompted to resume."
            )),
            ("10.  Review the Results", (
                "Findings are grouped by severity (Critical, High, Medium, "
                "Gripe) and sorted by confidence within each severity tier. Each finding "
                "includes a verification verdict from a secondary AI pass with "
                "web search, and shows whether the verdict was externally grounded "
                "or escalated to Opus. Use Export JSON from the report window to "
                "save structured results. Open the Diagnostics window to see "
                "model usage, prompt-cache hits, token counts by phase, "
                "verification evidence stats, and edit-skip reasons."
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
                font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
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
                "Use batch mode for routine reviews — same review logic at "
                "lower cost, with slower turnaround. Save your API key to a file so you don't "
                "have to paste it every time. Write specific project context — "
                "the more detail you provide, the more targeted the findings. "
                "Always spot-check code citations against the actual code text "
                "before acting on findings."
            ),
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            text_color=COLORS["text_secondary"],
            wraplength=520, justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 10))

        ctk.CTkButton(
            outer, text="Close", width=100, height=32,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            command=dialog.destroy,
        ).pack(pady=(0, 16))


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    SpecReviewApp().mainloop()

if __name__ == "__main__":
    main()
