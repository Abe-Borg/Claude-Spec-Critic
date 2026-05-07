"""Spec Critic — main app shell.

This file is a thin GUI shell. It builds the root window, the layout, and
delegates all workflow concerns to focused controller modules:

- ``app_paths`` / ``api_key_store`` / ``batch_state_store`` — persistence
- ``about_usage_dialogs`` — static informational dialogs
- ``file_selection_controller`` / ``context_controller`` /
  ``token_analysis_controller`` — input handling
- ``review_run_controller`` — real-time review orchestration + shared
  run-lifecycle helpers
- ``batch_controller`` — batch submission, polling, collection, resume
- ``report_controller`` — report export and the report window
- ``edit_workflow_controller`` — edit candidate selection + application
- ``diagnostics_controller`` — diagnostics callbacks and window
"""
import os
import sys
from pathlib import Path
from typing import Optional

import customtkinter as ctk

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

# Type annotations on method signatures
from src.batch import BatchStatus
from src.diagnostics import DiagnosticsReport
from src.extractor import ExtractedSpec
from src.pipeline import BatchSubmission
from src.review_modes import (
    DEFAULT_REVIEW_MODE,
    REVIEW_MODE_PROFILES,
    ReviewMode,
)
from src.reviewer import Finding
from src.spec_editor import EditReport

# Constants used by widgets
from src.code_cycles import DEFAULT_CYCLE
from src.tokenizer import PROJECT_CONTEXT_MAX_TOKENS, RECOMMENDED_MAX

from src.widgets import (
    AnimatedButton,
    COLORS,
    DiagnosticsWindow,
    EnhancedLog,
    FileListPanel,
    TokenGauge,
)

# Persistence helpers (also re-exported for backward compatibility with
# tests/external code that imports them from ``src.gui``)
from src.api_key_store import load_api_key_from_file
from src.batch_state_store import (
    delete_batch_state,
    load_batch_state,
    save_batch_state,
)

# Controllers
from src.about_usage_dialogs import show_about_dialog, show_usage_dialog
from src.batch_controller import (
    check_pending_batch,
    collect_batch_results,
    format_batch_age,
    is_valid_verification_resume_state,
    on_batch_submitted,
    on_poll_result,
    poll_and_collect_thread,
    poll_batch,
    resume_batch,
    resume_cross_check_verification_poll,
    resume_verification_poll,
    submit_batch_thread,
    update_poll_progress,
)
from src.context_controller import (
    attach_context_files,
    context_focus_in,
    context_focus_out,
    do_context_change,
    extract_context_attachments,
    get_project_context,
    on_context_change,
    open_context_modal,
    set_context_text,
    update_context_token_label,
)
from src.diagnostics_controller import (
    finalize_diagnostics,
    make_diag_log,
    make_diag_progress,
    open_diagnostics_window,
)
from src.edit_workflow_controller import (
    apply_selected_edits,
    on_edits_applied,
    show_edit_selection_dialog,
)
from src.file_selection_controller import (
    apply_selected_specs,
    browse_for_specs,
    clear_file_state,
    parse_dropped_paths,
    set_file_data,
)
from src.report_controller import export_report_to_file
from src.review_run_controller import (
    confirm_realtime_cost,
    dispatch_if_current,
    next_run_epoch,
    on_review_complete,
    on_review_error,
    reset_ui,
    run_review_thread,
    start_review as _start_review,
    validate_inputs,
)
from src.token_analysis_controller import (
    analyze_tokens,
    on_file_selection_change,
    refresh_exact_token_count,
)

_CONTEXT_PLACEHOLDER = "Describe your project (optional)"

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
        ctk.CTkLabel(self.hdr, text="M&P Specification Review  \u2022  California K-12 DSA  \u2022  Opus 4.7", font=ctk.CTkFont(family="Segoe UI", size=13), text_color=COLORS["text_secondary"]).pack(anchor="w", pady=(4, 0))

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

        # --- Row 4: Options ---
        ctk.CTkLabel(self.inputs_content, text="Options", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=4, column=0, sticky="w", pady=8)
        options_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        options_frame.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=8)
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
            text="Opus 4.7 \u2022 full content \u2022 finds inter-spec conflicts",
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_muted"])
        self._cross_check_hint.pack(side="left", padx=(12, 0))

        # --- Row 5: Review Mode (Phase 8 / plan section 12.1) ---
        ctk.CTkLabel(self.inputs_content, text="Review Mode", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=5, column=0, sticky="w", pady=8)
        mode_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        mode_frame.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=8)
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

    # --- Project context placeholder helpers ---

    def _context_focus_in(self, event=None):
        context_focus_in(self, event)

    def _context_focus_out(self, event=None):
        context_focus_out(self, event)

    def _get_project_context(self) -> str:
        return get_project_context(self)

    def _on_context_change(self, event=None):
        on_context_change(self, event)

    def _do_context_change(self):
        do_context_change(self)

    def _update_context_token_label(self) -> None:
        update_context_token_label(self)

    def _set_context_text(self, new_text: str) -> None:
        set_context_text(self, new_text)

    def _extract_context_attachments(self, paths: list[Path]) -> tuple[str, list[str]]:
        return extract_context_attachments(paths)

    def _attach_context_files(self, target_textbox=None) -> None:
        attach_context_files(self, target_textbox)

    def _open_context_modal(self):
        open_context_modal(self)

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
        paths = browse_for_specs(self)
        if paths:
            apply_selected_specs(self, paths)

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
        return parse_dropped_paths(self, payload)

    def _apply_selected_specs(self, candidate_paths: list[Path]):
        apply_selected_specs(self, candidate_paths)

    def _on_specs_drop(self, event):
        dropped_paths = parse_dropped_paths(self, getattr(event, "data", ""))
        apply_selected_specs(self, dropped_paths)

    def _clear_file_state(self):
        clear_file_state(self)

    def _set_file_data(self, file_data, extracted_specs, sys_tokens, ctx_tokens):
        set_file_data(self, file_data, extracted_specs, sys_tokens, ctx_tokens)

    def _analyze_tokens(self, file_paths):
        analyze_tokens(self, file_paths)

    def _refresh_exact_token_count(self, file_data, extracted_specs, project_context, cycle, sys_tokens, ctx_tokens, dispatch):
        refresh_exact_token_count(
            self, file_data, extracted_specs, project_context, cycle,
            sys_tokens, ctx_tokens, dispatch,
        )

    def _on_file_selection_change(self):
        on_file_selection_change(self)

    def _validate_inputs(self):
        return validate_inputs(self)

    def _next_run_epoch(self) -> int:
        return next_run_epoch(self)

    def _dispatch_if_current(self, epoch: int, fn):
        dispatch_if_current(self, epoch, fn)

    def _confirm_realtime_cost(self, num_specs: int) -> bool:
        return confirm_realtime_cost(self, num_specs)

    def start_review(self):
        _start_review(self)

    def _make_diag_log(self, phase: str, run_epoch: int):
        return make_diag_log(self, phase, run_epoch)

    def _make_diag_progress(self, phase: str, run_epoch: int):
        return make_diag_progress(self, phase, run_epoch)

    def _finalize_diagnostics(self, phase: str, level: str, message: str) -> None:
        finalize_diagnostics(self, phase, level, message)

    def _run_review_thread(self, run_epoch: int):
        run_review_thread(self, run_epoch)

    def _on_review_complete(self, result):
        on_review_complete(self, result)

    def _export_report_to_file(self, result) -> str:
        return export_report_to_file(self, result)

    def _show_edit_selection_dialog(self, result) -> None:
        show_edit_selection_dialog(self, result)

    def _apply_selected_edits(
        self,
        selected_indices: list[int],
        all_findings: list[Finding],
        cross_check_findings: list[Finding],
        extracted_specs: list[ExtractedSpec],
        source_paths: list[Path],
    ) -> None:
        apply_selected_edits(
            self,
            selected_indices,
            all_findings,
            cross_check_findings,
            extracted_specs,
            source_paths,
        )

    def _on_edits_applied(self, reports: list[EditReport]) -> None:
        on_edits_applied(self, reports)

    def _on_review_error(self, err):
        on_review_error(self, err)

    # ----- Batch mode -----

    def _submit_batch_thread(self, run_epoch: int):
        submit_batch_thread(self, run_epoch)

    def _on_batch_submitted(self, submission: BatchSubmission):
        on_batch_submitted(self, submission)

    def _poll_batch(self):
        poll_batch(self)

    def _update_poll_progress(self, status: BatchStatus):
        update_poll_progress(self, status)

    def _poll_and_collect_thread(self, run_epoch: int):
        poll_and_collect_thread(self, run_epoch)

    # Backward-compatible helper retained for tests and legacy call paths.
    def _on_poll_result(self, status: BatchStatus):
        on_poll_result(self, status)

    def _collect_batch_results(self):
        collect_batch_results(self)

    def _reset_ui(self):
        reset_ui(self)

    # ----- Persistent batch state -----

    def _check_pending_batch(self):
        check_pending_batch(self)

    def _format_batch_age(self, created_at: float) -> str:
        return format_batch_age(created_at)

    def _resume_batch(self, loaded_state: dict):
        resume_batch(self, loaded_state)

    def _is_valid_verification_resume_state(self, loaded_state: dict) -> bool:
        return is_valid_verification_resume_state(loaded_state)

    def _resume_verification_poll(self, loaded_state: dict):
        resume_verification_poll(self, loaded_state)

    def _resume_cross_check_verification_poll(self, loaded_state: dict):
        resume_cross_check_verification_poll(self, loaded_state)

    def _open_diagnostics_window(self):
        open_diagnostics_window(self)

    def _show_about_dialog(self):
        show_about_dialog(self)

    def _show_usage_dialog(self):
        show_usage_dialog(self)


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    SpecReviewApp().mainloop()


if __name__ == "__main__":
    main()
