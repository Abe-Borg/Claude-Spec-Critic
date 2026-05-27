"""
Custom widgets for Spec Critic GUI.

Contains: TokenGauge, FileListPanel, EnhancedLog,
AnimatedButton, DiagnosticsWindow.
"""
import math
from datetime import datetime
from collections import deque

import customtkinter as ctk

# ============================================================================
# SHARED CONFIG
# ============================================================================

COLORS = {
    "bg_dark": "#0D0D0D",
    "bg_card": "#1A1A1A",
    "bg_input": "#252525",
    "border": "#333333",
    "text_primary": "#FFFFFF",
    "text_secondary": "#B0B0B0",
    "text_muted": "#707070",
    "accent": "#3B82F6",
    "accent_hover": "#2563EB",
    "accent_glow": "#60A5FA",
    "success": "#22C55E",
    "success_glow": "#4ADE80",
    "warning": "#F59E0B",
    "error": "#EF4444",
    "critical": "#DC2626",
    "high": "#F97316",
    "medium": "#EAB308",
    "gripe": "#A855F7",
    "coordination": "#06B6D4",
}

SEVERITY_COLORS = {
    "CRITICAL": COLORS["critical"],
    "HIGH": COLORS["high"],
    "MEDIUM": COLORS["medium"],
    "GRIPES": COLORS["gripe"],
}

VERDICT_COLORS = {
    "CONFIRMED": "#22C55E",
    "CORRECTED": "#F59E0B",
    "UNVERIFIED": "#6B7280",
    "DISPUTED": "#EF4444",
}

LOG_COLORS = {
    "info": COLORS["text_secondary"],
    "success": COLORS["success"],
    "warning": COLORS["warning"],
    "error": COLORS["error"],
    "step": COLORS["accent"],
    "file": COLORS["text_primary"],
    "muted": COLORS["text_muted"],
}

ANIM = {
    "log_file_delay": 200, "log_status_delay": 400, "gauge_step": 33,
    "gauge_duration": 700, "fade_duration": 200, "fade_steps": 8,
    "pulse_interval": 1500, "pulse_step_ms": 67, "glow_step_ms": 67,
    "expand_duration": 200, "expand_steps": 10,
}


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def lerp(start, end, t): return start + (end - start) * t
def ease_out_cubic(t): return 1 - pow(1 - t, 3)
def hex_to_rgb(h):
    h = h.lstrip('#'); return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
def rgb_to_hex(r, g, b): return f"#{r:02x}{g:02x}{b:02x}"
def blend_colors(c1, c2, t):
    r1, g1, b1 = hex_to_rgb(c1); r2, g2, b2 = hex_to_rgb(c2)
    return rgb_to_hex(int(lerp(r1, r2, t)), int(lerp(g1, g2, t)), int(lerp(b1, b2, t)))


# ============================================================================
# TOKEN GAUGE
# ============================================================================

class TokenGauge(ctk.CTkFrame):
    """Displays the largest single spec's estimated API call size against the
    per-call token limit.

    Since specs are reviewed one at a time (per-spec siloed review), the
    bottleneck is the largest individual spec, not the total across all specs.
    The gauge shows: (system prompt + project context + largest spec tokens)
    vs. RECOMMENDED_MAX, which is the per-call input budget.
    """

    def __init__(self, master, max_tokens, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_card"], corner_radius=8, **kwargs)
        self.token_count = 0; self.max_tokens = max_tokens
        self._target_pct = 0.0; self._current_pct = 0.0; self._animating = False
        self._target_color = COLORS["accent"]; self.is_over_limit = False; self._expanded = True

        self.header_frame = ctk.CTkFrame(self, fg_color="transparent", cursor="hand2")
        self.header_frame.pack(fill="x", padx=16, pady=(12, 8))
        self.header_frame.bind("<Button-1>", self._toggle)
        self.expand_label = ctk.CTkLabel(self.header_frame, text="\u25bc", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_muted"], width=20)
        self.expand_label.pack(side="left"); self.expand_label.bind("<Button-1>", self._toggle)
        self.title_label = ctk.CTkLabel(self.header_frame, text="LARGEST SPEC CAPACITY (approx)", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"])
        self.title_label.pack(side="left", padx=(4, 0)); self.title_label.bind("<Button-1>", self._toggle)
        self.count_label = ctk.CTkLabel(self.header_frame, text=f"\u2014 / {max_tokens:,}", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_secondary"])
        self.count_label.pack(side="right"); self.count_label.bind("<Button-1>", self._toggle)

        self.content_container = ctk.CTkFrame(self, fg_color="transparent"); self.content_container.pack(fill="x")
        bar_frame = ctk.CTkFrame(self.content_container, fg_color=COLORS["bg_input"], corner_radius=4, height=8)
        bar_frame.pack(fill="x", padx=16, pady=(0, 8)); bar_frame.pack_propagate(False)
        self.progress_bar = ctk.CTkFrame(bar_frame, fg_color=COLORS["accent"], corner_radius=4, height=8, width=0)
        self.progress_bar.place(x=0, y=0, relheight=1); self.bar_frame = bar_frame
        self.status_label = ctk.CTkLabel(self.content_container, text="Select specs to analyze token usage", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLORS["text_muted"])
        self.status_label.pack(padx=16, pady=(0, 12))

    def _toggle(self, event=None): self.collapse() if self._expanded else self.expand()
    def expand(self): self._expanded = True; self.expand_label.configure(text="\u25bc"); self.content_container.pack(fill="x")
    def collapse(self): self._expanded = False; self.expand_label.configure(text="\u25b6"); self.content_container.pack_forget()

    def update_gauge(self, largest_call_tokens, file_count=0, *, is_exact: bool = False):
        """Update the gauge to show the largest spec's call estimate.

        Args:
            largest_call_tokens: Estimated input tokens for the largest single
                spec API call (overhead + spec content tokens).
            file_count: Number of selected files (shown in status text).
            is_exact: True if the count came from Anthropic's count_tokens
                endpoint; False for the local cl100k_base estimate. Phase 2.3
                of the implementation plan asked the GUI to distinguish
                approximate from exact counts.
        """
        self.token_count = largest_call_tokens; raw_pct = largest_call_tokens / self.max_tokens
        self._target_pct = min(raw_pct, 1.0); self.is_over_limit = raw_pct > 1.0
        title_suffix = "" if is_exact else " (approx)"
        self.title_label.configure(text=f"LARGEST SPEC CAPACITY{title_suffix}")
        self.count_label.configure(text=f"{largest_call_tokens:,} / {self.max_tokens:,}")
        if raw_pct > 1.0: self._target_color, status, sc = COLORS["error"], "\u26a0 Largest spec exceeds per-call limit!", COLORS["error"]
        elif raw_pct > 0.9: self._target_color, status, sc = COLORS["warning"], f"\u26a0 {raw_pct*100:.0f}% \u2014 largest spec approaching limit \u2022 {file_count} files", COLORS["warning"]
        elif raw_pct > 0.7: self._target_color, status, sc = COLORS["warning"], f"\u2713 {raw_pct*100:.0f}% \u2014 {file_count} files ready", COLORS["text_secondary"]
        else: self._target_color, status, sc = COLORS["success"], f"\u2713 {raw_pct*100:.0f}% \u2014 {file_count} files ready", COLORS["text_secondary"]
        self.status_label.configure(text=status, text_color=sc)
        if not self._animating: self._animating = True; self._animate_gauge(0)

    def _animate_gauge(self, step):
        total = ANIM["gauge_duration"] // ANIM["gauge_step"]
        if step >= total: self._current_pct = self._target_pct; self._animating = False; self._update_bar(); return
        self._current_pct = lerp(0, self._target_pct, ease_out_cubic(step / total)); self._update_bar()
        self.after(ANIM["gauge_step"], lambda: self._animate_gauge(step + 1))

    def _update_bar(self):
        w = self.bar_frame.winfo_width()
        if w > 1: self.progress_bar.configure(width=int(w * self._current_pct))
        c = blend_colors(COLORS["accent"], self._target_color, self._current_pct / max(self._target_pct, 0.01))
        self.progress_bar.configure(fg_color=c)

    def reset(self):
        self.token_count = 0; self._target_pct = self._current_pct = 0.0
        self.count_label.configure(text=f"\u2014 / {self.max_tokens:,}")
        self.progress_bar.configure(width=0, fg_color=COLORS["accent"])
        self.status_label.configure(text="Select specs to analyze token usage", text_color=COLORS["text_muted"])


# ============================================================================
# FILE LIST PANEL
# ============================================================================

class FileListPanel(ctk.CTkFrame):
    def __init__(self, master, on_selection_change=None, pack_after=None, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_card"], corner_radius=8, **kwargs)
        self._expanded = False; self._animating = False; self._file_data = []
        self._on_selection_change = on_selection_change; self._pack_after = pack_after
        self._is_over_limit = False; self._glow_animation_id = None

        self.header = ctk.CTkFrame(self, fg_color="transparent", cursor="hand2")
        self.header.pack(fill="x", padx=16, pady=12); self.header.bind("<Button-1>", self._toggle)
        self.expand_label = ctk.CTkLabel(self.header, text="\u25b6", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_muted"], width=20)
        self.expand_label.pack(side="left"); self.expand_label.bind("<Button-1>", self._toggle)
        self.title_label = ctk.CTkLabel(self.header, text="FILES", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"])
        self.title_label.pack(side="left", padx=(4, 0)); self.title_label.bind("<Button-1>", self._toggle)
        self.count_label = ctk.CTkLabel(self.header, text="", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLORS["text_secondary"])
        self.count_label.pack(side="right"); self.count_label.bind("<Button-1>", self._toggle)
        btn_frame = ctk.CTkFrame(self.header, fg_color="transparent"); btn_frame.pack(side="right", padx=(0, 16))
        ctk.CTkButton(btn_frame, text="All", width=40, height=22, font=ctk.CTkFont(size=10), fg_color="transparent", hover_color=COLORS["bg_input"], text_color=COLORS["text_muted"], command=self._select_all).pack(side="left", padx=(0, 4))
        ctk.CTkButton(btn_frame, text="None", width=40, height=22, font=ctk.CTkFont(size=10), fg_color="transparent", hover_color=COLORS["bg_input"], text_color=COLORS["text_muted"], command=self._select_none).pack(side="left")
        self.content_container = ctk.CTkFrame(self, fg_color="transparent")
        self.file_list = ctk.CTkScrollableFrame(self.content_container, fg_color=COLORS["bg_input"], corner_radius=4, height=150)
        self.file_list.pack(fill="both", expand=True, padx=16, pady=(0, 12)); self.pack_forget()

    def load_files(self, file_data):
        for w in self.file_list.winfo_children(): w.destroy()
        self._file_data.clear()
        for data in file_data:
            var = ctk.BooleanVar(value=True); var.trace_add("write", lambda *a: self._on_checkbox_change())
            row = ctk.CTkFrame(self.file_list, fg_color="transparent"); row.pack(fill="x", pady=2)
            ctk.CTkCheckBox(row, text="", variable=var, width=24, height=24, checkbox_width=18, checkbox_height=18, corner_radius=4, border_width=2, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], border_color=COLORS["border"], checkmark_color=COLORS["text_primary"]).pack(side="left")
            nl = ctk.CTkLabel(row, text=data["filename"], font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLORS["text_secondary"], anchor="w")
            nl.pack(side="left", padx=(8, 0), fill="x", expand=True)
            ctk.CTkLabel(row, text=f"{data['tokens']:,}", font=ctk.CTkFont(family="Consolas", size=10), text_color=COLORS["text_muted"], width=60, anchor="e").pack(side="right", padx=(8, 4))
            self._file_data.append({"path": data["path"], "filename": data["filename"], "tokens": data["tokens"], "var": var, "name_label": nl})
        self._update_count()
        if self._pack_after: self.pack(fill="x", pady=(16, 0), after=self._pack_after)
        else: self.pack(fill="x", pady=(16, 0))
        self._expanded = False; self.expand_label.configure(text="\u25b6")

    def get_selected_files(self): return [d["path"] for d in self._file_data if d["var"].get()]
    def get_selected_count(self): return sum(1 for d in self._file_data if d["var"].get())
    def _on_checkbox_change(self):
        self._update_count()
        for d in self._file_data: d["name_label"].configure(text_color=COLORS["text_secondary"] if d["var"].get() else COLORS["text_muted"])
        if self._on_selection_change: self._on_selection_change()
    def _update_count(self): self.count_label.configure(text=f"{self.get_selected_count()}/{len(self._file_data)} selected")
    def _select_all(self):
        for d in self._file_data: d["var"].set(True)
    def _select_none(self):
        for d in self._file_data: d["var"].set(False)
    def _toggle(self, event=None):
        if self._animating: return
        self.collapse() if self._expanded else self.expand()
    def expand(self): self._expanded = True; self.expand_label.configure(text="\u25bc"); self.content_container.pack(fill="x")
    def collapse(self): self._expanded = False; self.expand_label.configure(text="\u25b6"); self.content_container.pack_forget()

    def set_over_limit(self, over):
        if over == self._is_over_limit: return
        self._is_over_limit = over
        if over: self._glow_step = 0; self._animate_glow()
        else:
            if self._glow_animation_id: self.after_cancel(self._glow_animation_id); self._glow_animation_id = None
            self.title_label.configure(text_color=COLORS["text_muted"])

    def _animate_glow(self):
        if not self._is_over_limit: return
        t = (math.sin(self._glow_step * 0.15) + 1) / 2
        self.title_label.configure(text_color=blend_colors(COLORS["error"], "#ff9999", t))
        self._glow_step += 1; self._glow_animation_id = self.after(ANIM["glow_step_ms"], self._animate_glow)

    def reset(self):
        if self._glow_animation_id: self.after_cancel(self._glow_animation_id); self._glow_animation_id = None
        self._is_over_limit = False; self.title_label.configure(text_color=COLORS["text_muted"])
        for w in self.file_list.winfo_children(): w.destroy()
        self._file_data.clear(); self.pack_forget()


# ============================================================================
# ENHANCED LOG
# ============================================================================

class EnhancedLog(ctk.CTkFrame):
    _COLLAPSED_HEIGHT = 48
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_card"], corner_radius=8, **kwargs)
        self._log_queue: deque = deque(); self._processing_queue = False; self._expanded = True
        self.header = ctk.CTkFrame(self, fg_color="transparent", height=36, cursor="hand2")
        self.header.pack(fill="x", padx=16, pady=(12, 0)); self.header.pack_propagate(False)
        self.header.bind("<Button-1>", self._toggle)
        self.expand_label = ctk.CTkLabel(self.header, text="\u25bc", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_muted"], width=20)
        self.expand_label.pack(side="left"); self.expand_label.bind("<Button-1>", self._toggle)
        ctk.CTkLabel(self.header, text="ACTIVITY LOG", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"]).pack(side="left", padx=(4, 0))
        ctk.CTkButton(self.header, text="Clear", width=50, height=24, font=ctk.CTkFont(size=11), fg_color="transparent", hover_color=COLORS["bg_input"], text_color=COLORS["text_muted"], command=self.clear).pack(side="right")
        self.content_container = ctk.CTkFrame(self, fg_color="transparent"); self.content_container.pack(fill="both", expand=True)
        self._textbox = ctk.CTkTextbox(self.content_container, fg_color=COLORS["bg_input"], corner_radius=4, font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_secondary"], wrap="word", state="disabled", activate_scrollbars=True)
        self._textbox.pack(fill="both", expand=True, padx=16, pady=12)
        inner_text = self._textbox._textbox
        for level, color in LOG_COLORS.items(): inner_text.tag_configure(level, foreground=color)

    def _toggle(self, event=None): self.collapse() if self._expanded else self.expand()
    def expand(self): self._expanded = True; self.expand_label.configure(text="\u25bc"); self.pack_propagate(True); self.content_container.pack(fill="both", expand=True)
    def collapse(self): self._expanded = False; self.expand_label.configure(text="\u25b6"); self.content_container.pack_forget(); self.configure(height=self._COLLAPSED_HEIGHT); self.pack_propagate(False)

    def _queue_log(self, msg, level, ts, delay):
        self._log_queue.append((msg, level, ts, delay))
        if not self._processing_queue: self._process_queue()
    def _process_queue(self):
        if not self._log_queue: self._processing_queue = False; return
        self._processing_queue = True; msg, level, ts, delay = self._log_queue.popleft(); self._append_line(msg, level, ts)
        self.after(delay, self._process_queue)
    def _append_line(self, msg: str, level: str, ts: bool):
        txt = f"[{datetime.now().strftime('%H:%M:%S')}]  {msg}" if ts else f"         {msg}"
        self._textbox.configure(state="normal"); inner = self._textbox._textbox
        if inner.index("end-1c") != "1.0": inner.insert("end", "\n", ())
        inner.insert("end", txt, (level,)); self._textbox.configure(state="disabled"); inner.see("end")

    def log(self, msg, level="info", timestamp=True, paced=True):
        if paced: self._queue_log(msg, level, timestamp, ANIM["log_status_delay"])
        else: self._append_line(msg, level, timestamp)
    def clear(self):
        self._log_queue.clear(); self._textbox.configure(state="normal"); self._textbox._textbox.delete("1.0", "end"); self._textbox.configure(state="disabled")
    def log_step(self, msg): self._queue_log(f"\u25b8 {msg}", "step", True, ANIM["log_status_delay"])
    def log_success(self, msg): self._queue_log(f"\u2713 {msg}", "success", True, ANIM["log_status_delay"])
    def log_warning(self, msg): self._queue_log(f"\u26a0 {msg}", "warning", True, ANIM["log_status_delay"])
    def log_error(self, msg): self._queue_log(f"\u2717 {msg}", "error", True, ANIM["log_status_delay"])
    def log_file(self, fn): self._queue_log(f"  \u2192 {fn}", "file", False, ANIM["log_file_delay"])
    def log_file_batch(self, filenames: list[str]):
        for fn in filenames: self._queue_log(f"  \u2192 {fn}", "file", False, ANIM["log_file_delay"])


# ============================================================================
# ANIMATED BUTTON
# ============================================================================

class AnimatedButton(ctk.CTkButton):
    def __init__(self, master, **kwargs):
        self.default_text = kwargs.pop("text", "Run")
        super().__init__(master, text=self.default_text, font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"), height=44, corner_radius=8, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], **kwargs)
        self._state = "ready"; self._pulse_active = False; self._pulse_step = 0; self._glow_active = False

    def set_processing(self):
        self._state = "processing"; self.configure(text="Processing...", text_color_disabled="#FFFFFF", state="disabled")
        self._pulse_active = True; self._pulse_step = 0; self._animate_pulse()
    def _animate_pulse(self):
        if not self._pulse_active or self._state != "processing": return
        spc = ANIM["pulse_interval"] // ANIM["pulse_step_ms"]
        t = (math.sin(self._pulse_step / spc * math.pi * 2) + 1) / 2
        self.configure(fg_color=blend_colors(COLORS["bg_input"], COLORS["accent"], t), hover_color=blend_colors(COLORS["bg_input"], COLORS["accent"], t))
        self._pulse_step = (self._pulse_step + 1) % spc; self.after(ANIM["pulse_step_ms"], self._animate_pulse)
    def set_ready(self):
        self._pulse_active = self._glow_active = False; self._state = "ready"
        self.configure(text=self.default_text, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], state="normal")
    def set_complete(self):
        self._pulse_active = False; self._state = "complete"
        self.configure(text="\u2713 Complete", fg_color=COLORS["success"], hover_color=COLORS["success"], state="disabled")
        self._glow_active = True; self._animate_glow(0)
    def _animate_glow(self, step):
        if not self._glow_active or self._state != "complete": return
        if step >= 15: self.configure(fg_color=COLORS["success"]); self._glow_active = False; return
        t = step / 15
        c = blend_colors(COLORS["success"], COLORS["success_glow"], t / 0.3) if t < 0.3 else blend_colors(COLORS["success_glow"], COLORS["success"], (t - 0.3) / 0.7)
        self.configure(fg_color=c, hover_color=c); self.after(20, lambda: self._animate_glow(step + 1))


# ============================================================================
# DIAGNOSTICS WINDOW (pop-out toplevel)
# ============================================================================

class DiagnosticsWindow(ctk.CTkToplevel):
    """Displays the in-memory diagnostics report for a pipeline run."""

    def __init__(self, master, report, **kwargs):
        """
        Parameters
        ----------
        report : diagnostics.DiagnosticsReport
        """
        super().__init__(master, **kwargs)
        self.title("Diagnostics Report")
        self.geometry("900x700")
        self.minsize(700, 500)
        self.configure(fg_color=COLORS["bg_dark"])
        self._report = report
        self._build_ui()
        self.lift()
        self.focus_force()

    def _build_ui(self):
        # Toolbar
        toolbar = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=0, height=48)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        tb_inner = ctk.CTkFrame(toolbar, fg_color="transparent")
        tb_inner.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(
            tb_inner, text="Diagnostics Report",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(side="left")

        btn_kw = {
            "height": 30, "font": ctk.CTkFont(size=12),
            "fg_color": COLORS["bg_input"], "hover_color": COLORS["border"],
            "border_width": 1, "border_color": COLORS["border"],
            "text_color": COLORS["text_secondary"],
        }
        ctk.CTkButton(tb_inner, text="Copy to Clipboard", width=130, command=self._copy_text, **btn_kw).pack(side="right")

        # Body
        body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True, padx=16, pady=16)

        self._render_config_section(body)
        self._render_summary_section(body)
        self._render_timeline_section(body)

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _render_config_section(self, parent):
        card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8)
        card.pack(fill="x", pady=(0, 12))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=12)

        ctk.CTkLabel(
            inner, text="RUN CONFIGURATION",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=COLORS["accent"],
        ).pack(anchor="w")

        r = self._report
        started = datetime.fromtimestamp(r.started_at).strftime("%Y-%m-%d %H:%M:%S")
        duration = f"{r.ended_at - r.started_at:.1f}s" if r.ended_at else "in progress"
        lines = [
            f"Run ID: {r.run_id}",
            f"Mode: {r.mode}  •  Model: {r.model}  •  Cycle: {r.cycle_label}",
            f"Files: {len(r.files_selected)}  •  Context Tokens: {r.project_context_tokens:,}",
            f"Cross-Check: {'Enabled' if r.cross_check_enabled else 'Disabled'}",
            f"Started: {started}  •  Duration: {duration}",
        ]
        if r.files_selected:
            lines.append("Files: " + ", ".join(r.files_selected))

        ctk.CTkLabel(
            inner, text="\n".join(lines),
            font=ctk.CTkFont(family="Consolas", size=12),
            text_color=COLORS["text_secondary"],
            justify="left", anchor="w",
        ).pack(anchor="w", pady=(6, 0))

    def _render_summary_section(self, parent):
        card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8)
        card.pack(fill="x", pady=(0, 12))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=12)

        ctk.CTkLabel(
            inner, text="SUMMARY",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=COLORS["accent"],
        ).pack(anchor="w")

        s = self._report.summary()

        # Stats grid
        stats_frame = ctk.CTkFrame(inner, fg_color="transparent")
        stats_frame.pack(fill="x", pady=(8, 0))

        stat_items = [
            ("Total Time", f"{s['total_time_seconds']:.1f}s"),
            ("Events", str(s["total_events"])),
            ("Errors", str(s["errors"])),
            ("Warnings", str(s["warnings"])),
            ("Input Tokens", f"{s['total_input_tokens']:,}"),
            ("Output Tokens", f"{s['total_output_tokens']:,}"),
        ]
        for i, (label, value) in enumerate(stat_items):
            cell = ctk.CTkFrame(stats_frame, fg_color=COLORS["bg_input"], corner_radius=6, width=130, height=50)
            cell.grid(row=0, column=i, padx=(0, 8), sticky="nsew")
            cell.grid_propagate(False)
            color = COLORS["error"] if label == "Errors" and s["errors"] > 0 else \
                    COLORS["warning"] if label == "Warnings" and s["warnings"] > 0 else \
                    COLORS["text_primary"]
            ctk.CTkLabel(cell, text=value, font=ctk.CTkFont(family="Consolas", size=14, weight="bold"), text_color=color).place(relx=0.5, rely=0.35, anchor="center")
            ctk.CTkLabel(cell, text=label, font=ctk.CTkFont(size=10), text_color=COLORS["text_muted"]).place(relx=0.5, rely=0.72, anchor="center")
        stats_frame.grid_columnconfigure(list(range(len(stat_items))), weight=1)

        # Severity counts
        if s["severity_counts"]:
            sev_frame = ctk.CTkFrame(inner, fg_color="transparent")
            sev_frame.pack(fill="x", pady=(10, 0))
            ctk.CTkLabel(sev_frame, text="Findings:", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_secondary"]).pack(side="left")
            for sev, cnt in s["severity_counts"].items():
                color = SEVERITY_COLORS.get(sev, COLORS["text_secondary"])
                ctk.CTkLabel(sev_frame, text=f"  {sev}: {cnt}", font=ctk.CTkFont(family="Consolas", size=12, weight="bold"), text_color=color).pack(side="left")

        # Verdict breakdown
        if s["verification_verdicts"]:
            verd_frame = ctk.CTkFrame(inner, fg_color="transparent")
            verd_frame.pack(fill="x", pady=(4, 0))
            ctk.CTkLabel(verd_frame, text="Verdicts:", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_secondary"]).pack(side="left")
            for verdict, cnt in s["verification_verdicts"].items():
                color = VERDICT_COLORS.get(verdict, COLORS["text_secondary"])
                ctk.CTkLabel(verd_frame, text=f"  {verdict}: {cnt}", font=ctk.CTkFont(family="Consolas", size=12, weight="bold"), text_color=color).pack(side="left")

        # Phase durations
        if s["phase_durations"]:
            pd_frame = ctk.CTkFrame(inner, fg_color="transparent")
            pd_frame.pack(fill="x", pady=(8, 0))
            ctk.CTkLabel(pd_frame, text="Phase Durations:", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_secondary"]).pack(anchor="w")
            for phase, dur in s["phase_durations"].items():
                ctk.CTkLabel(pd_frame, text=f"  {phase:22s} {dur:.1f}s", font=ctk.CTkFont(family="Consolas", size=12), text_color=COLORS["text_muted"]).pack(anchor="w")

        # Phase 7.3: actionable diagnostics — render the fields previously
        # only available in the Save-as-Text/JSON exports.
        self._render_actionable_section(inner, s)

    def _render_actionable_section(self, parent, summary: dict):
        """Render Phase 7.3 / 9.4 actionable diagnostics in the summary card."""
        # Cache token usage.
        cache_creation = summary.get("total_cache_creation_input_tokens", 0)
        cache_read = summary.get("total_cache_read_input_tokens", 0)
        if cache_creation or cache_read:
            cache_frame = ctk.CTkFrame(parent, fg_color="transparent")
            cache_frame.pack(fill="x", pady=(8, 0))
            ctk.CTkLabel(
                cache_frame, text="Prompt Cache:",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["text_secondary"],
            ).pack(side="left")
            ctk.CTkLabel(
                cache_frame,
                text=f"  created={cache_creation:,}  read={cache_read:,}",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["text_muted"],
            ).pack(side="left")

        # Verification evidence (grounded / cache hits / escalations).
        evidence = summary.get("verification_evidence") or {}
        if any(evidence.values()):
            evi_frame = ctk.CTkFrame(parent, fg_color="transparent")
            evi_frame.pack(fill="x", pady=(4, 0))
            ctk.CTkLabel(
                evi_frame, text="Verification Evidence:",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w")
            evi_pairs = [
                ("grounded", "grounded"),
                ("ungrounded", "ungrounded"),
                ("escalated", "escalated"),
                ("cache_hits", "cache hits"),
                ("local_skips", "local skips"),
                ("search_errors", "search errors"),
                ("search_requests", "search reqs"),
            ]
            for key, label in evi_pairs:
                val = int(evidence.get(key, 0) or 0)
                if val == 0:
                    continue
                ctk.CTkLabel(
                    evi_frame, text=f"  {label:18s} {val:>6}",
                    font=ctk.CTkFont(family="Consolas", size=12),
                    text_color=COLORS["text_muted"],
                ).pack(anchor="w")

        # Failed / skipped specs.
        failed = summary.get("failed_specs") or []
        skipped = summary.get("skipped_specs") or []
        if failed or skipped:
            fs_frame = ctk.CTkFrame(parent, fg_color="transparent")
            fs_frame.pack(fill="x", pady=(8, 0))
            ctk.CTkLabel(
                fs_frame, text="Specs:",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w")
            if failed:
                ctk.CTkLabel(
                    fs_frame,
                    text=f"  failed   ({len(failed)}): {', '.join(failed[:6])}{' ...' if len(failed) > 6 else ''}",
                    font=ctk.CTkFont(family="Consolas", size=12),
                    text_color=COLORS["error"],
                ).pack(anchor="w")
            if skipped:
                ctk.CTkLabel(
                    fs_frame,
                    text=f"  skipped  ({len(skipped)}): {', '.join(skipped[:6])}{' ...' if len(skipped) > 6 else ''}",
                    font=ctk.CTkFont(family="Consolas", size=12),
                    text_color=COLORS["warning"],
                ).pack(anchor="w")

        # Edit pipeline outcomes.
        applied = summary.get("edits_applied_total", 0)
        skipped_e = summary.get("edits_skipped_total", 0)
        failed_e = summary.get("edits_failed_total", 0)
        ambig = summary.get("ambiguous_locator_count", 0)
        edit_reasons = summary.get("edit_skip_reasons") or {}
        if applied or skipped_e or failed_e or ambig or edit_reasons:
            ed_frame = ctk.CTkFrame(parent, fg_color="transparent")
            ed_frame.pack(fill="x", pady=(8, 0))
            ctk.CTkLabel(
                ed_frame,
                text=f"Edits: applied={applied}  skipped={skipped_e}  failed={failed_e}  ambiguous_locators={ambig}",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w")
            if edit_reasons:
                for reason, cnt in edit_reasons.items():
                    ctk.CTkLabel(
                        ed_frame, text=f"  skip[{reason}] = {cnt}",
                        font=ctk.CTkFont(family="Consolas", size=12),
                        text_color=COLORS["text_muted"],
                    ).pack(anchor="w")

        # Output / search budget telemetry.
        ot = summary.get("output_telemetry") or {}
        if ot.get("samples"):
            ot_frame = ctk.CTkFrame(parent, fg_color="transparent")
            ot_frame.pack(fill="x", pady=(8, 0))
            ctk.CTkLabel(
                ot_frame,
                text=(
                    f"Output tokens: max={ot.get('max_observed', 0):,}  "
                    f"p50={ot.get('p50', 0):,}  p95={ot.get('p95', 0):,}  "
                    f"truncated={ot.get('truncated_calls', 0)}"
                ),
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w")

        sb = summary.get("search_budget") or {}
        if sb.get("samples"):
            sb_frame = ctk.CTkFrame(parent, fg_color="transparent")
            sb_frame.pack(fill="x", pady=(4, 0))
            ctk.CTkLabel(
                sb_frame,
                text=(
                    f"Search budget: ceiling={sb.get('ceiling', 0)}  "
                    f"max={sb.get('max_observed', 0)}  p50={sb.get('p50', 0)}  "
                    f"p95={sb.get('p95', 0)}  saturated={sb.get('saturated_calls', 0)}"
                ),
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w")

        dropped = summary.get("events_dropped", 0)
        if dropped:
            ctk.CTkLabel(
                parent,
                text=f"⚠ {dropped:,} events dropped (event cap)",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["warning"],
            ).pack(anchor="w", pady=(4, 0))
        truncated = summary.get("events_truncated_by_size", 0)
        if truncated:
            ctk.CTkLabel(
                parent,
                text=f"⚠ {truncated:,} events truncated (per-event byte cap)",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["warning"],
            ).pack(anchor="w", pady=(2, 0))
        secrets_red = summary.get("secrets_redacted", 0)
        if secrets_red:
            ctk.CTkLabel(
                parent,
                text=f"🔒 {secrets_red:,} secret-shaped values redacted",
                font=ctk.CTkFont(family="Consolas", size=12),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w", pady=(2, 0))

    def _render_timeline_section(self, parent):
        card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8)
        card.pack(fill="x", pady=(0, 12))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=12)

        ctk.CTkLabel(
            inner, text=f"EVENT TIMELINE  ({len(self._report.events)} events)",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=COLORS["accent"],
        ).pack(anchor="w")

        level_colors = {
            "info": COLORS["text_secondary"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["error"],
            "step": COLORS["accent"],
        }
        level_icons = {
            "info": "  ",
            "success": "+ ",
            "warning": "! ",
            "error": "X ",
            "step": "> ",
        }

        # Use a textbox for efficient rendering of many events
        textbox = ctk.CTkTextbox(
            inner, fg_color=COLORS["bg_input"], corner_radius=4,
            font=ctk.CTkFont(family="Consolas", size=12),
            text_color=COLORS["text_secondary"],
            wrap="word", state="disabled", activate_scrollbars=True,
            height=400,
        )
        textbox.pack(fill="x", pady=(8, 0))

        inner_text = textbox._textbox
        for level, color in level_colors.items():
            inner_text.tag_configure(level, foreground=color)
        inner_text.tag_configure("data_tag", foreground=COLORS["text_muted"])
        inner_text.tag_configure("phase_tag", foreground=COLORS["coordination"])

        textbox.configure(state="normal")
        for i, e in enumerate(self._report.events):
            if i > 0:
                inner_text.insert("end", "\n", ())
            ts = datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S")
            elapsed = f"{e.elapsed:7.1f}s"
            icon = level_icons.get(e.level, "  ")
            phase_str = f"[{e.phase}]" if e.phase else ""

            inner_text.insert("end", f"{ts} {elapsed} ", ("info",))
            inner_text.insert("end", icon, (e.level,))
            if phase_str:
                inner_text.insert("end", f"{phase_str:20s} ", ("phase_tag",))
            inner_text.insert("end", e.message, (e.level,))
            if e.data:
                for k, v in e.data.items():
                    inner_text.insert("end", f"\n{'':38s}{k}: {v}", ("data_tag",))
        textbox.configure(state="disabled")

    # ------------------------------------------------------------------
    # Export actions
    # ------------------------------------------------------------------

    def _copy_text(self):
        text = self._report.to_text()
        self.clipboard_clear()
        self.clipboard_append(text)
