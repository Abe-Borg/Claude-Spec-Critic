"""
Custom widgets for Spec Critic GUI.

Contains: TokenGauge, FileListPanel, EnhancedLog,
AnimatedButton, ReportWindow.

v2.3.0 changes:
    - TokenGauge relabeled to "LARGEST SPEC CAPACITY" and updated to show
      the largest single spec's estimated API call size against the per-call
      limit. Since specs are reviewed one at a time, this is the actual
      bottleneck — total tokens across all specs is irrelevant.
    - Model selector references removed from ReportWindow and summary grid
      (Opus 4.6 is now the only review model)

v1.6.0 changes:
    - Cross-spec coordination section rendered in report (separate from
      per-spec findings)
    - ReportWindow accepts optional cross_check_result
    - _render_cross_check_section() added for coordination findings
    - Cross-check findings included in JSON export
    - "coordination" color added to COLORS dict

v1.5.0 changes:
    - Confidence badge displayed in finding card headers (color-coded)
    - Findings sorted by confidence (descending) within each severity tier
    - Confidence included in JSON export via _finding_to_dict()
    - Model references updated to Opus 4.6
"""
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Optional
from collections import deque

import customtkinter as ctk
from tkinter import filedialog

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

CONFIDENCE_COLORS = {
    "high": "#22C55E",
    "moderate": "#F59E0B",
    "low": "#EF4444",
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

def _confidence_color(confidence: float) -> str:
    if confidence >= 0.85: return CONFIDENCE_COLORS["high"]
    elif confidence >= 0.60: return CONFIDENCE_COLORS["moderate"]
    else: return CONFIDENCE_COLORS["low"]

def _confidence_label(confidence: float) -> str: return f"{confidence:.0%}"


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
        self.title_label = ctk.CTkLabel(self.header_frame, text="LARGEST SPEC CAPACITY", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"])
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

    def update_gauge(self, largest_call_tokens, file_count=0):
        """Update the gauge to show the largest spec's call estimate.

        Args:
            largest_call_tokens: Estimated input tokens for the largest single
                spec API call (overhead + spec content tokens).
            file_count: Number of selected files (shown in status text).
        """
        self.token_count = largest_call_tokens; raw_pct = largest_call_tokens / self.max_tokens
        self._target_pct = min(raw_pct, 1.0); self.is_over_limit = raw_pct > 1.0
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
# REPORT RENDERING HELPERS (used by ReportWindow)
# ============================================================================

def _render_summary_grid(parent, review, files_reviewed, cross_check_result=None):
    hc = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8); hc.pack(fill="x", pady=(0, 12))
    hi = ctk.CTkFrame(hc, fg_color="transparent"); hi.pack(fill="x", padx=16, pady=12)
    ctk.CTkLabel(hi, text="Spec Critic Report", font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"), text_color=COLORS["text_primary"]).pack(anchor="w")
    ctk.CTkLabel(hi, text=f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  \u2022  Model: {review.model}  \u2022  Files: {len(files_reviewed)}", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLORS["text_muted"]).pack(anchor="w", pady=(4, 0))

    sc = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8); sc.pack(fill="x", pady=(0, 12))
    si = ctk.CTkFrame(sc, fg_color="transparent"); si.pack(fill="x", padx=16, pady=12)
    ctk.CTkLabel(si, text="SUMMARY", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"]).pack(anchor="w", pady=(0, 8))
    grid = ctk.CTkFrame(si, fg_color="transparent"); grid.pack(fill="x", pady=(0, 8))

    cc_count = len(cross_check_result.findings) if cross_check_result and cross_check_result.findings else 0
    columns = [
        ("Critical", review.critical_count, COLORS["critical"]),
        ("High", review.high_count, COLORS["high"]),
        ("Medium", review.medium_count, COLORS["medium"]),
        ("Gripes", review.gripe_count, COLORS["gripe"]),
        ("Total", review.total_count, COLORS["text_primary"]),
    ]
    if cc_count > 0:
        columns.append(("Cross-Check", cc_count, COLORS["coordination"]))

    for i in range(len(columns)): grid.columnconfigure(i, weight=1)
    for col, (label, count, color) in enumerate(columns):
        cell = ctk.CTkFrame(grid, fg_color=COLORS["bg_input"], corner_radius=6)
        cell.grid(row=0, column=col, padx=4, sticky="nsew")
        ci = ctk.CTkFrame(cell, fg_color="transparent"); ci.pack(padx=12, pady=10)
        ctk.CTkLabel(ci, text=str(count), font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"), text_color=color).pack()
        ctk.CTkLabel(ci, text=label.upper(), font=ctk.CTkFont(family="Segoe UI", size=9, weight="bold"), text_color=COLORS["text_muted"]).pack()

    meta_text = (
        f"Review tokens: {review.input_tokens:,} in \u2192 {review.output_tokens:,} out"
        f"  \u2022  Time: {review.elapsed_seconds:.1f}s"
    )
    all_findings_for_verdicts = list(review.findings)
    if cross_check_result and cross_check_result.findings:
        all_findings_for_verdicts.extend(cross_check_result.findings)
    if all_findings_for_verdicts:
        verdicts = {}
        for f in all_findings_for_verdicts:
            if f.verification:
                v = f.verification.verdict; verdicts[v] = verdicts.get(v, 0) + 1
        if verdicts:
            verdict_parts = [f"{verdicts[v]} {v.lower()}" for v in ["CONFIRMED", "CORRECTED", "DISPUTED", "UNVERIFIED"] if v in verdicts]
            meta_text += f"  \u2022  Verified: {', '.join(verdict_parts)}"
    ctk.CTkLabel(si, text=meta_text, font=ctk.CTkFont(family="Consolas", size=11), text_color=COLORS["text_muted"]).pack(anchor="w", pady=(4, 0))


def _render_alerts(parent, leed_alerts, placeholder_alerts):
    if not leed_alerts and not placeholder_alerts: return
    card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8); card.pack(fill="x", pady=(0, 12))
    inner = ctk.CTkFrame(card, fg_color="transparent"); inner.pack(fill="x", padx=16, pady=12)
    ctk.CTkLabel(inner, text="ALERTS", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"]).pack(anchor="w", pady=(0, 8))
    for label, alerts in [("LEED References Detected", leed_alerts), ("Unresolved Placeholders", placeholder_alerts)]:
        if not alerts: continue
        ctk.CTkLabel(inner, text=label, font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"), text_color=COLORS["warning"]).pack(anchor="w", pady=(4, 4))
        by_file = {}
        for a in alerts: by_file.setdefault(a["filename"], []).append(a)
        for fname, fa in by_file.items():
            ai = ctk.CTkFrame(inner, fg_color=COLORS["bg_input"], corner_radius=6); ai.pack(fill="x", pady=2)
            aii = ctk.CTkFrame(ai, fg_color="transparent"); aii.pack(fill="x", padx=12, pady=8)
            ctk.CTkLabel(aii, text=fname, font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"), text_color=COLORS["text_primary"]).pack(anchor="w")
            ctk.CTkLabel(aii, text=f"{len(fa)} found", font=ctk.CTkFont(family="Consolas", size=10), text_color=COLORS["text_muted"]).pack(anchor="w")


def _render_collapsible_card(parent, finding, card_refs: list | None = None):
    sc = SEVERITY_COLORS.get(finding.severity, COLORS["border"])
    outer = ctk.CTkFrame(parent, fg_color=sc, corner_radius=8); outer.pack(fill="x", pady=4)
    card = ctk.CTkFrame(outer, fg_color=COLORS["bg_input"], corner_radius=6); card.pack(fill="x", padx=(4, 0))
    header = ctk.CTkFrame(card, fg_color="transparent", cursor="hand2"); header.pack(fill="x", padx=14, pady=(10, 0))
    arrow_label = ctk.CTkLabel(header, text="\u25bc", font=ctk.CTkFont(family="Consolas", size=11), text_color=COLORS["text_muted"], width=16)
    arrow_label.pack(side="left")
    ctk.CTkLabel(header, text=finding.severity, font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
        text_color="white" if finding.severity != "MEDIUM" else "black",
        fg_color=sc, corner_radius=4, width=70, height=22).pack(side="left", padx=(4, 0))
    conf_color = _confidence_color(finding.confidence)
    ctk.CTkLabel(header, text=_confidence_label(finding.confidence),
        font=ctk.CTkFont(family="Consolas", size=9, weight="bold"),
        text_color=conf_color, width=36, height=22).pack(side="left", padx=(6, 0))
    ctk.CTkLabel(header, text=finding.fileName or "Unknown", font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        text_color=COLORS["text_primary"]).pack(side="left", padx=(8, 0))
    if finding.section:
        ctk.CTkLabel(header, text=f"\u2022  {finding.section}", font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COLORS["text_muted"]).pack(side="left", padx=(10, 0))

    body = ctk.CTkFrame(card, fg_color="transparent"); body.pack(fill="x", padx=14, pady=(4, 12))
    ctk.CTkLabel(body, text=finding.issue or "", font=ctk.CTkFont(family="Segoe UI", size=12),
        text_color=COLORS["text_secondary"], anchor="w", justify="left", wraplength=700).pack(fill="x", pady=(0, 8))
    if finding.existingText:
        r = ctk.CTkFrame(body, fg_color="transparent"); r.pack(fill="x", pady=2)
        ctk.CTkLabel(r, text="Existing:", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"], width=90, anchor="w").pack(side="left")
        ctk.CTkLabel(r, text=finding.existingText, font=ctk.CTkFont(family="Consolas", size=11), text_color=COLORS["error"], anchor="w", justify="left", wraplength=600).pack(side="left", fill="x", expand=True)
    if finding.replacementText:
        r = ctk.CTkFrame(body, fg_color="transparent"); r.pack(fill="x", pady=2)
        ctk.CTkLabel(r, text="Replace with:", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"], width=90, anchor="w").pack(side="left")
        ctk.CTkLabel(r, text=finding.replacementText, font=ctk.CTkFont(family="Consolas", size=11), text_color=COLORS["success"], anchor="w", justify="left", wraplength=600).pack(side="left", fill="x", expand=True)
    if finding.codeReference:
        ctk.CTkLabel(body, text=f"Reference: {finding.codeReference}", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLORS["accent"], anchor="w").pack(fill="x", pady=(4, 0))

    # Verification verdict
    if finding.verification:
        vr = finding.verification; vc = VERDICT_COLORS.get(vr.verdict, VERDICT_COLORS["UNVERIFIED"])
        vf = ctk.CTkFrame(body, fg_color="transparent"); vf.pack(fill="x", pady=(8, 0))
        verdict_icon = {"CONFIRMED": "\u2713", "CORRECTED": "\u270e", "DISPUTED": "\u2717", "UNVERIFIED": "\u2014"}.get(vr.verdict, "\u2014")
        ctk.CTkLabel(vf, text=f"{verdict_icon} {vr.verdict}", font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color="white" if vr.verdict != "CORRECTED" else "black",
            fg_color=vc, corner_radius=4, width=100, height=22).pack(side="left")
        if vr.explanation:
            ctk.CTkLabel(vf, text=vr.explanation, font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=COLORS["text_secondary"], anchor="w", justify="left", wraplength=550).pack(side="left", padx=(8, 0), fill="x", expand=True)
        if vr.correction:
            cr = ctk.CTkFrame(body, fg_color="transparent"); cr.pack(fill="x", pady=2)
            ctk.CTkLabel(cr, text="Correction:", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["warning"], width=90, anchor="w").pack(side="left")
            ctk.CTkLabel(cr, text=vr.correction, font=ctk.CTkFont(family="Consolas", size=11), text_color=COLORS["warning"], anchor="w", justify="left", wraplength=600).pack(side="left", fill="x", expand=True)
        if vr.sources:
            sf = ctk.CTkFrame(body, fg_color="transparent"); sf.pack(fill="x", pady=(2, 0))
            ctk.CTkLabel(sf, text="Sources:", font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"), text_color=COLORS["text_muted"], width=90, anchor="w").pack(side="left", anchor="n")
            src_text = "\n".join(vr.sources[:3])
            ctk.CTkLabel(sf, text=src_text, font=ctk.CTkFont(family="Consolas", size=10), text_color=COLORS["text_muted"], anchor="w", justify="left", wraplength=600).pack(side="left", fill="x", expand=True)

    card_state = {"expanded": True}
    def _toggle_card(event=None):
        if card_state["expanded"]: body.pack_forget(); arrow_label.configure(text="\u25b6"); card_state["expanded"] = False
        else: body.pack(fill="x", padx=14, pady=(4, 12)); arrow_label.configure(text="\u25bc"); card_state["expanded"] = True
    header.bind("<Button-1>", _toggle_card)
    for child in header.winfo_children(): child.bind("<Button-1>", _toggle_card)
    if card_refs is not None: card_refs.append({"body": body, "arrow": arrow_label, "state": card_state, "padx": 14, "pady": (4, 12)})


def _render_findings_section(parent, review, card_refs: list | None = None):
    card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8); card.pack(fill="x", pady=(0, 12))
    inner = ctk.CTkFrame(card, fg_color="transparent"); inner.pack(fill="x", padx=16, pady=12)
    findings_header = ctk.CTkFrame(inner, fg_color="transparent"); findings_header.pack(fill="x", pady=(0, 8))
    ctk.CTkLabel(findings_header, text="FINDINGS", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"]).pack(side="left")

    if review.total_count > 0 and card_refs is not None:
        btn_kw = {"width": 85, "height": 24, "font": ctk.CTkFont(size=10), "fg_color": "transparent",
            "hover_color": COLORS["bg_input"], "text_color": COLORS["text_muted"],
            "border_width": 1, "border_color": COLORS["border"], "corner_radius": 4}
        def _collapse_all():
            for ref in card_refs:
                if ref["state"]["expanded"]: ref["body"].pack_forget(); ref["arrow"].configure(text="\u25b6"); ref["state"]["expanded"] = False
        def _expand_all():
            for ref in card_refs:
                if not ref["state"]["expanded"]: ref["body"].pack(fill="x", padx=ref["padx"], pady=ref["pady"]); ref["arrow"].configure(text="\u25bc"); ref["state"]["expanded"] = True
        ctk.CTkButton(findings_header, text="Expand All", command=_expand_all, **btn_kw).pack(side="right", padx=(4, 0))
        ctk.CTkButton(findings_header, text="Collapse All", command=_collapse_all, **btn_kw).pack(side="right")

    if review.total_count == 0:
        ctk.CTkLabel(inner, text="\u2713 No issues found", font=ctk.CTkFont(family="Segoe UI", size=14), text_color=COLORS["success"]).pack(pady=16); return

    for sev in ["CRITICAL", "HIGH", "MEDIUM", "GRIPES"]:
        sf = sorted([f for f in review.findings if f.severity == sev], key=lambda f: f.confidence, reverse=True)
        if not sf: continue
        ctk.CTkLabel(inner, text=f"{sev} ({len(sf)})", font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=SEVERITY_COLORS.get(sev, COLORS["text_primary"])).pack(anchor="w", pady=(12, 6))
        for f in sf: _render_collapsible_card(inner, f, card_refs=card_refs)


def _render_cross_check_section(parent, cross_check_result, card_refs: list | None = None):
    """Render the cross-spec coordination findings as a separate section.

    This is visually distinct from the per-spec findings section, using
    a cyan accent color and its own header. Coordination findings use
    the same collapsible card rendering as per-spec findings.

    Args:
        parent: Parent widget to render into
        cross_check_result: ReviewResult from the cross-check pass (may be None)
        card_refs: Shared card reference list for collapse/expand all
    """
    if not cross_check_result:
        return

    card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8)
    card.pack(fill="x", pady=(0, 12))
    inner = ctk.CTkFrame(card, fg_color="transparent")
    inner.pack(fill="x", padx=16, pady=12)

    # Section header with cyan accent
    header_frame = ctk.CTkFrame(inner, fg_color="transparent")
    header_frame.pack(fill="x", pady=(0, 8))

    ctk.CTkLabel(
        header_frame,
        text="CROSS-SPEC COORDINATION",
        font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
        text_color=COLORS["coordination"],
    ).pack(side="left")

    status = getattr(cross_check_result, "cross_check_status", None)
    count = len(cross_check_result.findings)
    if status == "skipped":
        ctk.CTkLabel(inner, text=f"Cross-check was skipped: {cross_check_result.thinking}", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["warning"], wraplength=750, justify="left").pack(anchor="w", pady=(0, 8))
        return
    if status == "failed":
        ctk.CTkLabel(inner, text=f"Cross-check failed: {cross_check_result.error}", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["warning"], wraplength=750, justify="left").pack(anchor="w", pady=(0, 8))
        return
    if status == "completed" and count == 0:
        ctk.CTkLabel(inner, text="Cross-check completed — no coordination issues found.", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["success"]).pack(anchor="w", pady=(0, 8))
        return
    ctk.CTkLabel(
        header_frame,
        text=f"({count} issue{'s' if count != 1 else ''})",
        font=ctk.CTkFont(family="Segoe UI", size=11),
        text_color=COLORS["text_muted"],
    ).pack(side="left", padx=(8, 0))

    ctk.CTkLabel(
        header_frame,
        text="Opus 4.6",
        font=ctk.CTkFont(family="Consolas", size=9),
        text_color=COLORS["text_muted"],
    ).pack(side="right")

    # Render findings sorted by severity then confidence
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}
    sorted_findings = sorted(
        cross_check_result.findings,
        key=lambda f: (severity_order.get(f.severity, 99), -f.confidence),
    )

    for f in sorted_findings:
        _render_collapsible_card(inner, f, card_refs=card_refs)

    # Cross-check notes (coordination summary from the model)
    if cross_check_result.thinking:
        nf = ctk.CTkFrame(inner, fg_color=COLORS["bg_input"], corner_radius=6)
        nf.pack(fill="x", pady=(8, 0))

        # Section label
        ctk.CTkLabel(
            nf,
            text="COORDINATION SUMMARY",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=14, pady=(14, 6))

        # Render each paragraph separately for readability
        raw_text = cross_check_result.thinking
        if '\n\n' in raw_text:
            paragraphs = raw_text.split('\n\n')
        else:
            paragraphs = raw_text.split('\n')

        for para_text in paragraphs:
            # Strip markdown headers
            stripped = para_text.strip()
            while stripped.startswith('#'):
                stripped = stripped[1:]
            stripped = stripped.strip()
            if not stripped:
                continue
            ctk.CTkLabel(
                nf,
                text=stripped,
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=COLORS["text_secondary"],
                anchor="w", justify="left", wraplength=720,
            ).pack(fill="x", padx=14, pady=(0, 10))

        # Bottom padding for the frame
        ctk.CTkFrame(nf, fg_color="transparent", height=4).pack()


def _render_notes(parent, text):
    card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8); card.pack(fill="x", pady=(0, 12))
    inner = ctk.CTkFrame(card, fg_color="transparent"); inner.pack(fill="x", padx=16, pady=12)
    ctk.CTkLabel(inner, text="REVIEWER'S NOTES", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=COLORS["text_muted"]).pack(anchor="w", pady=(0, 8))
    nf = ctk.CTkFrame(inner, fg_color=COLORS["bg_input"], corner_radius=6); nf.pack(fill="x")
    ctk.CTkLabel(nf, text=text, font=ctk.CTkFont(family="Segoe UI", size=12), text_color=COLORS["text_secondary"], anchor="w", justify="left", wraplength=750).pack(fill="x", padx=14, pady=14)


def _finding_to_dict(finding) -> dict:
    """Serialize a Finding to a JSON-safe dict, including confidence and verification."""
    d = {k: v for k, v in finding.__dict__.items() if k != "verification"}
    if finding.verification is not None:
        d["verification"] = {"verdict": finding.verification.verdict, "explanation": finding.verification.explanation,
            "sources": finding.verification.sources, "correction": finding.verification.correction}
    else:
        d["verification"] = None
    return d


# ============================================================================
# REPORT WINDOW (pop-out toplevel)
# ============================================================================

class ReportWindow(ctk.CTkToplevel):
    def __init__(self, master, review, files_reviewed, leed_alerts, placeholder_alerts, project_context="", cross_check_result=None, **kwargs):
        super().__init__(master, **kwargs)
        self.title("Spec Critic Report"); self.geometry("960x800"); self.minsize(700, 500)
        self.configure(fg_color=COLORS["bg_dark"])
        self._review = review; self._files_reviewed = files_reviewed
        self._leed_alerts = leed_alerts; self._placeholder_alerts = placeholder_alerts
        self._project_context = project_context; self._cross_check_result = cross_check_result
        self._card_refs: list[dict] = []
        self._build_ui(); self.lift(); self.focus_force()

    def _build_ui(self):
        toolbar = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=0, height=48)
        toolbar.pack(fill="x"); toolbar.pack_propagate(False)
        tb_inner = ctk.CTkFrame(toolbar, fg_color="transparent"); tb_inner.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(tb_inner, text="Spec Critic Report", font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"), text_color=COLORS["text_primary"]).pack(side="left")
        btn_kw = {"height": 30, "font": ctk.CTkFont(size=12), "fg_color": COLORS["bg_input"], "hover_color": COLORS["border"], "border_width": 1, "border_color": COLORS["border"], "text_color": COLORS["text_secondary"]}
        ctk.CTkButton(tb_inner, text="Copy Summary", width=110, command=lambda: self._copy_summary(self._review.thinking), **btn_kw).pack(side="right", padx=(8, 0))
        ctk.CTkButton(tb_inner, text="Export JSON", width=100, command=lambda: self._export_json(self._review, self._files_reviewed, self._leed_alerts, self._placeholder_alerts), **btn_kw).pack(side="right")

        body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        _render_summary_grid(body, self._review, self._files_reviewed, cross_check_result=self._cross_check_result)
        _render_alerts(body, self._leed_alerts, self._placeholder_alerts)
        _render_findings_section(body, self._review, card_refs=self._card_refs)
        _render_cross_check_section(body, self._cross_check_result, card_refs=self._card_refs)
        if self._review.thinking: _render_notes(body, self._review.thinking)

    def _export_json(self, review, files_reviewed, leed_alerts, placeholder_alerts):
        data = {
            "meta": {"model": review.model, "input_tokens": review.input_tokens, "output_tokens": review.output_tokens,
                "elapsed_seconds": review.elapsed_seconds, "generated_at": datetime.now().isoformat(), "project_context": self._project_context},
            "files_reviewed": files_reviewed,
            "findings": [_finding_to_dict(f) for f in review.findings],
            "cross_check_findings": [_finding_to_dict(f) for f in self._cross_check_result.findings] if self._cross_check_result and self._cross_check_result.findings else [],
            "alerts": {"leed_alerts": leed_alerts, "placeholder_alerts": placeholder_alerts},
            "analysis_summary": review.thinking,
            "cross_check_summary": self._cross_check_result.thinking if self._cross_check_result else None,
        }
        path = filedialog.asksaveasfilename(parent=self, title="Save findings JSON", defaultextension=".json",
            filetypes=[("JSON Files", "*.json")], initialfile=f"spec-critic-{datetime.now().strftime('%Y-%m-%d')}.json")
        if path:
            try:
                Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception as e:
                from tkinter import messagebox
                messagebox.showerror("Export Error", f"Could not save JSON file:\n{e}", parent=self)

    def _copy_summary(self, text):
        if text: self.clipboard_clear(); self.clipboard_append(text)
