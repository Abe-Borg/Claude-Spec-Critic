"""Spec Critic — main app shell.

This file is a thin GUI shell. It builds the root window, the layout, and
delegates all workflow concerns to focused controller modules:

- ``app_paths`` / ``api_key_store`` — persistence
- ``about_usage_dialogs`` — static informational dialogs
- ``file_selection_controller`` / ``context_controller`` /
  ``token_analysis_controller`` — input handling
- ``review_run_controller`` — run orchestration + shared
  run-lifecycle helpers
- ``batch_controller`` — batch submission, polling, collection
- ``report_controller`` — report export and the report window
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
from src.batch.batch import BatchStatus
from src.orchestration.diagnostics import DiagnosticsReport
from src.input.extractor import ExtractedSpec
from src.orchestration.pipeline import BatchSubmission

# Constants used by widgets
from src.core.code_cycles import DEFAULT_CYCLE
from src.core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS, RECOMMENDED_MAX

from src.gui.widgets import (
    AnimatedButton,
    COLORS,
    DiagnosticsWindow,
    EnhancedLog,
    FileListPanel,
    TokenGauge,
)

# Persistence helpers (also re-exported for backward compatibility with
# tests/external code that imports them from ``src.gui``)
from src.core.api_key_store import load_api_key_from_file

# Controllers
from src.gui.about_usage_dialogs import show_about_dialog, show_usage_dialog
from src.gui.batch_controller import (
    collect_batch_results,
    offer_batch_resume,
    on_batch_submitted,
    poll_and_collect_thread,
    poll_batch,
    recover_batch_dialog,
    submit_batch_thread,
    update_poll_progress,
)
from src.gui.context_controller import (
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
from src.gui.diagnostics_controller import (
    finalize_diagnostics,
    make_diag_log,
    make_diag_progress,
    open_diagnostics_window,
)
from src.gui.file_selection_controller import (
    apply_selected_specs,
    browse_for_specs,
    clear_file_state,
    parse_dropped_paths,
    set_file_data,
)
from src.gui.report_controller import export_report_to_file
from src.gui.review_run_controller import (
    dispatch_if_current,
    next_run_epoch,
    on_review_complete,
    on_review_error,
    reset_ui,
    start_review as _start_review,
    validate_inputs,
)
from src.gui.token_analysis_controller import (
    analyze_tokens,
    on_file_selection_change,
    refresh_exact_token_count,
)

_CONTEXT_PLACEHOLDER = "Describe your project (optional)"

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
        self._last_result = None
        self._diagnostics_report: Optional[DiagnosticsReport] = None
        self._diagnostics_window: Optional[DiagnosticsWindow] = None
        self._context_debounce_id: str | None = None
        # Debounce timer id for the exact-token-count refresh.
        # Tracked here so rapid file-list churn cancels the prior timer
        # instead of stacking up multiple outbound API calls.
        self._exact_token_refresh_timer_id: str | None = None
        self._selected_cycle_label: str = DEFAULT_CYCLE.label
        self._font_scale_label: str = "Default (100%)"
        self._create_ui()

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
        self.run_button = AnimatedButton(c, text="Submit Batch", command=self.start_review)
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
        self.recover_button = ctk.CTkButton(
            c, text="Recover batch…", height=32,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLORS["bg_input"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_secondary"],
            command=self._recover_batch_dialog,
        )
        self.recover_button.pack(fill="x", pady=(8, 0))

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

        # --- Row 3: Options ---
        ctk.CTkLabel(self.inputs_content, text="Options", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=3, column=0, sticky="w", pady=8)
        options_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        options_frame.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=8)
        self._cross_check_var = ctk.BooleanVar(value=False)
        self._cross_check_cb = ctk.CTkCheckBox(
            options_frame, text="Cross-spec coordination check", variable=self._cross_check_var,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"], border_color=COLORS["border"],
            checkmark_color=COLORS["text_primary"], text_color=COLORS["text_secondary"],
            checkbox_width=20, checkbox_height=20,
        )
        self._cross_check_cb.pack(side="left")
        self._cross_check_hint = ctk.CTkLabel(options_frame,
            text="Sonnet 4.6 \u2022 full content \u2022 finds inter-spec conflicts",
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_muted"])
        self._cross_check_hint.pack(side="left", padx=(12, 0))

        # --- Row 4: Agent tracing ---
        ctk.CTkLabel(self.inputs_content, text="Tracing", font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), text_color=COLORS["text_secondary"], width=100, anchor="w").grid(row=4, column=0, sticky="w", pady=8)
        tracing_frame = ctk.CTkFrame(self.inputs_content, fg_color="transparent")
        tracing_frame.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=8)
        self._trace_var = ctk.BooleanVar(value=True)
        self._trace_cb = ctk.CTkCheckBox(
            tracing_frame, text="Record agent trace", variable=self._trace_var,
            command=self._on_trace_toggle,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"], border_color=COLORS["border"],
            checkmark_color=COLORS["text_primary"], text_color=COLORS["text_secondary"],
            checkbox_width=20, checkbox_height=20,
        )
        self._trace_cb.pack(side="left")
        self._trace_deep_var = ctk.BooleanVar(value=False)
        self._trace_deep_cb = ctk.CTkCheckBox(
            tracing_frame, text="Deep mode", variable=self._trace_deep_var,
            command=self._on_trace_toggle,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE), fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"], border_color=COLORS["border"],
            checkmark_color=COLORS["text_primary"], text_color=COLORS["text_secondary"],
            checkbox_width=20, checkbox_height=20,
        )
        self._trace_deep_cb.pack(side="left", padx=(12, 0))
        self._trace_show_btn = ctk.CTkButton(
            tracing_frame, text="Show folder", width=110,
            command=self._on_show_trace_folder,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            fg_color=COLORS["border"], hover_color=COLORS["accent_hover"],
            text_color=COLORS["text_primary"],
        )
        self._trace_show_btn.pack(side="left", padx=(12, 0))
        self._trace_viewer_btn = ctk.CTkButton(
            tracing_frame, text="Open viewer", width=110,
            command=self._on_open_trace_viewer,
            font=ctk.CTkFont(family="Segoe UI", size=_UI_FONT_SIZE),
            fg_color=COLORS["border"], hover_color=COLORS["accent_hover"],
            text_color=COLORS["text_primary"],
        )
        self._trace_viewer_btn.pack(side="left", padx=(8, 0))
        # Apply initial state to env vars so the recorder picks them up
        # on the first run without needing to toggle first.
        self._on_trace_toggle()

        # (Accessibility row is now in the header area, not inside the inputs card)

        self.inputs_content.columnconfigure(1, weight=1)

    def _on_font_scale_change(self, value: str):
        scale = _FONT_SCALE_OPTIONS.get(value, 1.0)
        ctk.set_widget_scaling(scale)
        self._font_scale_label = value

    def _on_trace_toggle(self) -> None:
        """Translate the checkboxes into env vars the recorder reads.

        ``SPEC_CRITIC_TRACE`` is the main switch; ``SPEC_CRITIC_TRACE_DEEP``
        opts into deep mode. Both are read at recorder construction time
        (next run start), so toggling between runs takes effect without a
        process restart.
        """
        import os
        os.environ["SPEC_CRITIC_TRACE"] = "1" if self._trace_var.get() else "0"
        os.environ["SPEC_CRITIC_TRACE_DEEP"] = "1" if self._trace_deep_var.get() else "0"

    def _on_show_trace_folder(self) -> None:
        """Open ~/.spec_critic/traces in the OS file explorer."""
        import os
        import platform
        import subprocess
        from ..tracing import default_trace_root
        path = default_trace_root()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Windows":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as exc:
            self.log.log_warning(f"Could not open trace folder ({exc}). Path: {path}")

    def _on_open_trace_viewer(self) -> None:
        """Open the bundled single-file HTML trace viewer in the browser.

        The viewer is a static artifact; the user picks a trace folder from
        within it. We point ``file://`` at the bundled HTML so it works
        offline without a server.
        """
        import webbrowser
        from pathlib import Path
        viewer = Path(__file__).resolve().parent.parent / "tracing" / "viewer" / "trace_viewer.html"
        if not viewer.exists():
            self.log.log_warning(f"Trace viewer not found at {viewer}")
            return
        try:
            webbrowser.open(viewer.as_uri())
        except Exception as exc:
            self.log.log_warning(f"Could not open trace viewer ({exc}). Path: {viewer}")

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

    def start_review(self):
        _start_review(self)

    def _make_diag_log(self, phase: str, run_epoch: int):
        return make_diag_log(self, phase, run_epoch)

    def _make_diag_progress(self, phase: str, run_epoch: int):
        return make_diag_progress(self, phase, run_epoch)

    def _finalize_diagnostics(self, phase: str, level: str, message: str) -> None:
        finalize_diagnostics(self, phase, level, message)

    def _on_review_complete(self, result):
        on_review_complete(self, result)

    def _export_report_to_file(self, result) -> str:
        return export_report_to_file(self, result)

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

    def _collect_batch_results(self):
        collect_batch_results(self)

    def _reset_ui(self):
        reset_ui(self)

    def _open_diagnostics_window(self):
        open_diagnostics_window(self)

    def _show_about_dialog(self):
        show_about_dialog(self)

    def _show_usage_dialog(self):
        show_usage_dialog(self)

    def _maybe_offer_batch_resume(self):
        offer_batch_resume(self)

    def _recover_batch_dialog(self):
        recover_batch_dialog(self)


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = SpecReviewApp()
    # After the window is up, offer to resume an unfinished batch from a prior
    # session (closed app / detached poller). Scheduled on the event loop so the
    # prompt appears once the UI is interactive.
    app.after(600, app._maybe_offer_batch_resume)
    app.mainloop()


if __name__ == "__main__":
    main()
