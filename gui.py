"""
MEP Spec Review - Modern GUI with CustomTkinter
California K-12 DSA Projects

v0.3.0 - Animation polish, log pacing, smooth transitions
"""
import math
import os
import sys
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from collections import deque

import customtkinter as ctk

# Path setup for imports
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    exe_dir = Path(sys.executable).parent
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
    exe_dir = Path(base_path).parent
    # Add parent directory to path so 'src' package is importable
    sys.path.insert(0, str(Path(base_path).parent))

# Try both import styles for flexibility
try:
    from src.pipeline import run_review
    from src.reviewer import MODEL_OPUS_45
    from src.extractor import extract_text_from_docx
    from src.tokenizer import analyze_token_usage, RECOMMENDED_MAX
    from src.prompts import get_system_prompt
except ImportError:
    from pipeline import run_review
    from reviewer import MODEL_OPUS_45
    from extractor import extract_text_from_docx
    from tokenizer import analyze_token_usage, RECOMMENDED_MAX
    from prompts import get_system_prompt


# ============================================================================
# CONFIGURATION
# ============================================================================

API_KEY_FILENAME = "spec_critic_api_key.txt"

# Color palette - sophisticated dark theme
COLORS = {
    "bg_dark": "#0D0D0D",
    "bg_card": "#1A1A1A", 
    "bg_input": "#252525",
    "border": "#333333",
    "text_primary": "#FFFFFF",
    "text_secondary": "#B0B0B0",  # Brighter for better legibility
    "text_muted": "#707070",      # Slightly brighter
    "accent": "#3B82F6",        # Blue
    "accent_hover": "#2563EB",
    "accent_glow": "#60A5FA",   # Lighter blue for glow effects
    "success": "#22C55E",
    "success_glow": "#4ADE80",  # Lighter green for glow
    "warning": "#F59E0B",
    "error": "#EF4444",
    "critical": "#DC2626",
    "high": "#F97316",
    "medium": "#EAB308",
    "gripe": "#A855F7",
}

# Log entry types with colors
LOG_COLORS = {
    "info": COLORS["text_secondary"],
    "success": COLORS["success"],
    "warning": COLORS["warning"],
    "error": COLORS["error"],
    "step": COLORS["accent"],
    "file": COLORS["text_primary"],
    "muted": COLORS["text_muted"],
}

# Animation timing (ms)
ANIM = {
    "log_file_delay": 200,      # Delay between file log entries
    "log_status_delay": 400,    # Delay for status log entries
    "gauge_step": 16,           # ~60fps for gauge animation
    "gauge_duration": 700,      # Total gauge fill animation time (slower)
    "fade_duration": 200,       # Fade-in duration for log entries
    "fade_steps": 8,            # Number of fade steps
    "pulse_interval": 1500,     # Button pulse cycle time
    "expand_duration": 200,     # Panel expand/collapse time
    "expand_steps": 10,         # Steps for expand animation
}


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def load_api_key_from_file() -> str:
    """Load API key from file in executable directory."""
    key_file = exe_dir / API_KEY_FILENAME
    if key_file.exists():
        try:
            return key_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


def get_docx_files(folder: Path) -> list[Path]:
    """Get .docx files from folder, excluding temp files."""
    return sorted([p for p in folder.glob("*.docx") if not p.name.startswith("~$")])


def lerp(start: float, end: float, t: float) -> float:
    """Linear interpolation between start and end."""
    return start + (end - start) * t


def ease_out_cubic(t: float) -> float:
    """Cubic ease-out function for smooth deceleration."""
    return 1 - pow(1 - t, 3)


def ease_in_out_cubic(t: float) -> float:
    """Cubic ease-in-out for smooth acceleration and deceleration."""
    if t < 0.5:
        return 4 * t * t * t
    else:
        return 1 - pow(-2 * t + 2, 3) / 2


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color to RGB tuple."""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert RGB values to hex color."""
    return f"#{r:02x}{g:02x}{b:02x}"


def blend_colors(color1: str, color2: str, t: float) -> str:
    """Blend between two hex colors."""
    r1, g1, b1 = hex_to_rgb(color1)
    r2, g2, b2 = hex_to_rgb(color2)
    r = int(lerp(r1, r2, t))
    g = int(lerp(g1, g2, t))
    b = int(lerp(b1, b2, t))
    return rgb_to_hex(r, g, b)


# ============================================================================
# CUSTOM WIDGETS
# ============================================================================

class TokenGauge(ctk.CTkFrame):
    """Visual gauge showing token usage against limit with animated fill."""
    
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_card"], corner_radius=8, **kwargs)
        
        self.token_count = 0
        self.max_tokens = RECOMMENDED_MAX
        self._target_pct = 0.0
        self._current_pct = 0.0
        self._animating = False
        self._target_color = COLORS["accent"]
        
        # Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=16, pady=(12, 8))
        
        self.title_label = ctk.CTkLabel(
            header_frame,
            text="TOKEN CAPACITY",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=COLORS["text_muted"]
        )
        self.title_label.pack(side="left")
        
        self.count_label = ctk.CTkLabel(
            header_frame,
            text="— / 150,000",
            font=ctk.CTkFont(family="Consolas", size=12),
            text_color=COLORS["text_secondary"]
        )
        self.count_label.pack(side="right")
        
        # Progress bar container
        bar_frame = ctk.CTkFrame(self, fg_color=COLORS["bg_input"], corner_radius=4, height=8)
        bar_frame.pack(fill="x", padx=16, pady=(0, 8))
        bar_frame.pack_propagate(False)
        
        self.progress_bar = ctk.CTkFrame(bar_frame, fg_color=COLORS["accent"], corner_radius=4, height=8, width=0)
        self.progress_bar.place(x=0, y=0, relheight=1)
        
        self.bar_frame = bar_frame
        
        # Status message
        self.status_label = ctk.CTkLabel(
            self,
            text="Select a specs folder to analyze token usage",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COLORS["text_muted"]
        )
        self.status_label.pack(padx=16, pady=(0, 12))
        
    def update_gauge(self, tokens: int, file_count: int = 0):
        """Update the gauge with new token count (animated)."""
        self.token_count = tokens
        self._target_pct = min(tokens / self.max_tokens, 1.0)
        
        # Update count label immediately
        self.count_label.configure(text=f"{tokens:,} / {self.max_tokens:,}")
        
        # Determine target color and status
        pct = self._target_pct
        if pct > 1.0:
            self._target_color = COLORS["error"]
            status = f"⚠ EXCEEDS LIMIT — Cannot process. Remove some specs."
            status_color = COLORS["error"]
        elif pct > 0.9:
            self._target_color = COLORS["warning"]
            status = f"⚠ {pct*100:.0f}% capacity — Approaching limit"
            status_color = COLORS["warning"]
        elif pct > 0.7:
            self._target_color = COLORS["warning"]
            status = f"✓ {pct*100:.0f}% capacity — {file_count} files ready"
            status_color = COLORS["text_secondary"]
        else:
            self._target_color = COLORS["success"]
            status = f"✓ {pct*100:.0f}% capacity — {file_count} files ready"
            status_color = COLORS["text_secondary"]
        
        self.status_label.configure(text=status, text_color=status_color)
        
        # Start animation if not already running
        if not self._animating:
            self._animating = True
            self._animate_gauge(0)
    
    def _animate_gauge(self, step: int):
        """Animate the gauge fill."""
        total_steps = ANIM["gauge_duration"] // ANIM["gauge_step"]
        
        if step >= total_steps:
            self._current_pct = self._target_pct
            self._animating = False
            self._update_bar_visual()
            return
        
        # Ease-out animation
        t = ease_out_cubic(step / total_steps)
        self._current_pct = lerp(0, self._target_pct, t)
        self._update_bar_visual()
        
        self.after(ANIM["gauge_step"], lambda: self._animate_gauge(step + 1))
    
    def _update_bar_visual(self):
        """Update the progress bar width and color."""
        bar_width = self.bar_frame.winfo_width()
        if bar_width > 1:
            self.progress_bar.configure(width=int(bar_width * self._current_pct))
        
        # Animate color transition
        current_color = blend_colors(COLORS["accent"], self._target_color, self._current_pct / max(self._target_pct, 0.01))
        self.progress_bar.configure(fg_color=current_color)
        
    def reset(self):
        """Reset gauge to initial state."""
        self.token_count = 0
        self._target_pct = 0.0
        self._current_pct = 0.0
        self.count_label.configure(text="— / 150,000")
        self.progress_bar.configure(width=0, fg_color=COLORS["accent"])
        self.status_label.configure(
            text="Select a specs folder to analyze token usage",
            text_color=COLORS["text_muted"]
        )


class EnhancedLog(ctk.CTkFrame):
    """Enhanced log display with colored entries, timestamps, and paced output."""
    
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_card"], corner_radius=8, **kwargs)
        
        # Log queue for paced output
        self._log_queue: deque = deque()
        self._processing_queue = False
        
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent", height=36)
        header.pack(fill="x", padx=16, pady=(12, 0))
        header.pack_propagate(False)
        
        ctk.CTkLabel(
            header,
            text="ACTIVITY LOG",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=COLORS["text_muted"]
        ).pack(side="left", anchor="w")
        
        self.clear_btn = ctk.CTkButton(
            header,
            text="Clear",
            width=50,
            height=24,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            hover_color=COLORS["bg_input"],
            text_color=COLORS["text_muted"],
            command=self.clear
        )
        self.clear_btn.pack(side="right")
        
        # Scrollable log area
        self.log_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=COLORS["bg_input"],
            corner_radius=4,
        )
        self.log_frame.pack(fill="both", expand=True, padx=16, pady=12)
        
        self.entries: list[ctk.CTkLabel] = []
        
    def _queue_log(self, message: str, level: str, timestamp: bool, delay: int):
        """Add a log entry to the queue."""
        self._log_queue.append((message, level, timestamp, delay))
        if not self._processing_queue:
            self._process_queue()
    
    def _process_queue(self):
        """Process queued log entries with pacing."""
        if not self._log_queue:
            self._processing_queue = False
            return
        
        self._processing_queue = True
        message, level, timestamp, delay = self._log_queue.popleft()
        self._create_log_entry(message, level, timestamp)
        
        # Schedule next entry
        self.after(delay, self._process_queue)
    
    def _create_log_entry(self, message: str, level: str, timestamp: bool):
        """Create and animate a log entry."""
        color = LOG_COLORS.get(level, COLORS["text_secondary"])
        
        # Build display text
        if timestamp:
            ts = datetime.now().strftime("%H:%M:%S")
            display_text = f"[{ts}]  {message}"
        else:
            display_text = f"         {message}"
        
        entry = ctk.CTkLabel(
            self.log_frame,
            text=display_text,
            font=ctk.CTkFont(family="Consolas", size=12),
            text_color=color,
            anchor="w",
            justify="left"
        )
        entry.pack(fill="x", anchor="w", pady=1)
        self.entries.append(entry)
        
        # Fade-in animation
        self._fade_in_entry(entry, color, 0)
        
        # Auto-scroll to bottom (update first so geometry is current)
        self.log_frame.update_idletasks()
        self.log_frame._parent_canvas.yview_moveto(1.0)
    
    def _fade_in_entry(self, entry: ctk.CTkLabel, target_color: str, step: int):
        """Animate entry fade-in."""
        if step >= ANIM["fade_steps"]:
            entry.configure(text_color=target_color)
            return
        
        t = step / ANIM["fade_steps"]
        # Fade from muted to target color
        current_color = blend_colors(COLORS["bg_input"], target_color, ease_out_cubic(t))
        entry.configure(text_color=current_color)
        
        delay = ANIM["fade_duration"] // ANIM["fade_steps"]
        self.after(delay, lambda: self._fade_in_entry(entry, target_color, step + 1))
        
    def log(self, message: str, level: str = "info", timestamp: bool = True, paced: bool = True):
        """Add a log entry with optional timestamp and color."""
        delay = ANIM["log_status_delay"] if paced else 0
        if paced:
            self._queue_log(message, level, timestamp, delay)
        else:
            self._create_log_entry(message, level, timestamp)
        
    def clear(self):
        """Clear all log entries."""
        self._log_queue.clear()
        for entry in self.entries:
            entry.destroy()
        self.entries.clear()
        
    def log_step(self, message: str):
        """Log a major step (paced)."""
        self._queue_log(f"▸ {message}", "step", True, ANIM["log_status_delay"])
        
    def log_success(self, message: str):
        """Log a success message (paced)."""
        self._queue_log(f"✓ {message}", "success", True, ANIM["log_status_delay"])
        
    def log_warning(self, message: str):
        """Log a warning (paced)."""
        self._queue_log(f"⚠ {message}", "warning", True, ANIM["log_status_delay"])
        
    def log_error(self, message: str):
        """Log an error (paced)."""
        self._queue_log(f"✗ {message}", "error", True, ANIM["log_status_delay"])
        
    def log_file(self, filename: str):
        """Log a file being processed (faster pacing)."""
        self._queue_log(f"  → {filename}", "file", False, ANIM["log_file_delay"])


class ThinkingPanel(ctk.CTkFrame):
    """Collapsible panel to display Claude's reasoning process with smooth animation."""
    
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_card"], corner_radius=8, **kwargs)
        
        self._expanded = False
        self._thinking_text = ""
        self._animating = False
        self._target_height = 0
        
        # Header (always visible, clickable to expand/collapse)
        self.header = ctk.CTkFrame(self, fg_color="transparent", cursor="hand2")
        self.header.pack(fill="x", padx=16, pady=12)
        self.header.bind("<Button-1>", self._toggle)
        
        # Expand/collapse indicator
        self.expand_label = ctk.CTkLabel(
            self.header,
            text="▶",
            font=ctk.CTkFont(family="Consolas", size=12),
            text_color=COLORS["text_muted"],
            width=20
        )
        self.expand_label.pack(side="left")
        self.expand_label.bind("<Button-1>", self._toggle)
        
        # Title
        self.title_label = ctk.CTkLabel(
            self.header,
            text="CLAUDE'S ANALYSIS",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=COLORS["text_muted"]
        )
        self.title_label.pack(side="left", padx=(4, 0))
        self.title_label.bind("<Button-1>", self._toggle)
        
        # Preview (shown when collapsed)
        self.preview_label = ctk.CTkLabel(
            self.header,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COLORS["text_muted"],
            anchor="e"
        )
        self.preview_label.pack(side="right", fill="x", expand=True, padx=(16, 0))
        self.preview_label.bind("<Button-1>", self._toggle)
        
        # Content container (for animation)
        self.content_container = ctk.CTkFrame(self, fg_color="transparent", height=0)
        self.content_container.pack_propagate(False)
        
        # Content area
        self.content_frame = ctk.CTkFrame(self.content_container, fg_color=COLORS["bg_input"], corner_radius=4)
        self.content_frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        
        self.content_text = ctk.CTkTextbox(
            self.content_frame,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_secondary"],
            fg_color="transparent",
            wrap="word",
            height=150,
            activate_scrollbars=True
        )
        self.content_text.pack(fill="both", expand=True, padx=12, pady=12)
        
        # Initially hidden
        self.pack_forget()
        
    def _toggle(self, event=None):
        """Toggle expanded/collapsed state with animation."""
        if self._animating:
            return
        if self._expanded:
            self._animate_collapse()
        else:
            self._animate_expand()
            
    def _animate_expand(self):
        """Animate expanding the content."""
        self._animating = True
        self._expanded = True
        self.expand_label.configure(text="▼")
        self.preview_label.configure(text="")
        
        # Show container and animate height
        self.content_container.pack(fill="x")
        self._target_height = 180  # Target expanded height
        self._animate_height(0, 0, self._target_height, True)
    
    def _animate_collapse(self):
        """Animate collapsing the content."""
        self._animating = True
        self._expanded = False
        self.expand_label.configure(text="▶")
        
        current_height = self.content_container.winfo_height()
        self._animate_height(0, current_height, 0, False)
    
    def _animate_height(self, step: int, start_height: int, end_height: int, expanding: bool):
        """Animate height change."""
        if step >= ANIM["expand_steps"]:
            if expanding:
                self.content_container.configure(height=end_height)
            else:
                self.content_container.pack_forget()
                self._update_preview()
            self._animating = False
            return
        
        t = ease_in_out_cubic(step / ANIM["expand_steps"])
        current_height = int(lerp(start_height, end_height, t))
        self.content_container.configure(height=max(1, current_height))
        
        delay = ANIM["expand_duration"] // ANIM["expand_steps"]
        self.after(delay, lambda: self._animate_height(step + 1, start_height, end_height, expanding))
    
    def expand(self):
        """Expand to show full thinking text."""
        if not self._expanded and not self._animating:
            self._animate_expand()
        
    def collapse(self):
        """Collapse to show only preview."""
        if self._expanded and not self._animating:
            self._animate_collapse()
        
    def _update_preview(self):
        """Update the preview text shown when collapsed."""
        if self._thinking_text:
            # Show first ~60 chars as preview
            preview = self._thinking_text[:80].replace("\n", " ").strip()
            if len(self._thinking_text) > 80:
                preview += "..."
            self.preview_label.configure(text=preview, text_color=COLORS["text_muted"])
        else:
            self.preview_label.configure(text="")
            
    def set_thinking(self, text: str, before_widget=None):
        """Set the thinking text and show the panel."""
        self._thinking_text = text.strip()
        
        if self._thinking_text:
            # Update content
            self.content_text.configure(state="normal")
            self.content_text.delete("1.0", "end")
            self.content_text.insert("1.0", self._thinking_text)
            self.content_text.configure(state="disabled")
            
            # Show panel collapsed by default
            self._expanded = False
            self.expand_label.configure(text="▶")
            self.content_container.pack_forget()
            self._update_preview()
            
            if before_widget:
                self.pack(fill="x", pady=(16, 0), before=before_widget)
            else:
                self.pack(fill="x", pady=(16, 0))
        else:
            self.hide()
            
    def hide(self):
        """Hide the panel entirely."""
        self.pack_forget()
        self._thinking_text = ""
        self._expanded = False
        self._animating = False
        self.expand_label.configure(text="▶")
        self.content_container.pack_forget()
        
    def clear(self):
        """Clear content and hide."""
        self.hide()


class AnimatedButton(ctk.CTkButton):
    """Button with animated states for run/processing/complete."""
    
    def __init__(self, master, **kwargs):
        self.default_text = kwargs.pop("text", "Run")
        super().__init__(
            master,
            text=self.default_text,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            height=44,
            corner_radius=8,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            **kwargs
        )
        self._state = "ready"
        self._pulse_active = False
        self._pulse_step = 0
        self._glow_active = False
        
    def set_processing(self):
        """Set button to processing state with pulse animation."""
        self._state = "processing"
        self.configure(
            text="Processing...",
            text_color=COLORS["text_primary"],
            state="disabled"
        )
        self._start_pulse()
        
    def _start_pulse(self):
        """Start the pulse animation."""
        self._pulse_active = True
        self._pulse_step = 0
        self._animate_pulse()
    
    def _animate_pulse(self):
        """Animate button pulse during processing."""
        if not self._pulse_active or self._state != "processing":
            return
        
        # Pulse between dark and accent color for visibility
        steps_per_cycle = ANIM["pulse_interval"] // 16
        t = self._pulse_step / steps_per_cycle
        
        # Sin wave for smooth pulsing (0 to 1 to 0)
        pulse_t = (math.sin(t * math.pi * 2) + 1) / 2
        
        # Pulse from bg_input to a muted accent for noticeable effect
        color = blend_colors(COLORS["bg_input"], COLORS["accent"], pulse_t)
        self.configure(fg_color=color, hover_color=color)
        
        self._pulse_step = (self._pulse_step + 1) % steps_per_cycle
        self.after(16, self._animate_pulse)
        
    def set_ready(self):
        """Reset button to ready state."""
        self._pulse_active = False
        self._glow_active = False
        self._state = "ready"
        self.configure(
            text=self.default_text,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            state="normal"
        )
        
    def set_complete(self):
        """Set button to complete state with glow effect."""
        self._pulse_active = False
        self._state = "complete"
        self.configure(
            text="✓ Complete",
            fg_color=COLORS["success"],
            hover_color=COLORS["success"],
            state="disabled"
        )
        # Start glow animation
        self._start_glow()
    
    def _start_glow(self):
        """Start the completion glow animation."""
        self._glow_active = True
        self._animate_glow(0)
    
    def _animate_glow(self, step: int):
        """Animate a brief glow on completion."""
        if not self._glow_active or self._state != "complete":
            return
        
        # Quick glow then settle
        total_steps = 15
        if step >= total_steps:
            self.configure(fg_color=COLORS["success"])
            self._glow_active = False
            return
        
        t = step / total_steps
        # Glow up then down
        if t < 0.3:
            glow_t = t / 0.3
            color = blend_colors(COLORS["success"], COLORS["success_glow"], glow_t)
        else:
            glow_t = (t - 0.3) / 0.7
            color = blend_colors(COLORS["success_glow"], COLORS["success"], glow_t)
        
        self.configure(fg_color=color, hover_color=color)
        self.after(20, lambda: self._animate_glow(step + 1))




# ============================================================================
# MAIN APPLICATION
# ============================================================================

class SpecReviewApp(ctk.CTk):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        
        # Window setup
        self.title("MEP Spec Review")
        self.geometry("800x800")
        self.minsize(700, 650)
        self.configure(fg_color=COLORS["bg_dark"])
        
        # State
        self.input_dir: Optional[Path] = None
        self.output_dir = Path.home() / "Desktop" / "spec-review-output"
        self.last_output_path: Optional[Path] = None
        self.is_processing = False
        
        # Load API key
        file_key = load_api_key_from_file()
        env_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.api_key = file_key if file_key else env_key
        
        self._create_ui()
        
    def _create_ui(self):
        """Build the user interface."""
        # Main container
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=24, pady=24)
        
        # Header
        self._create_header(container)
        
        # Input fields card
        self._create_inputs_card(container)
        
        # Token gauge
        self.token_gauge = TokenGauge(container)
        self.token_gauge.pack(fill="x", pady=(16, 0))
        
        # Run button
        self.run_button = AnimatedButton(
            container,
            text="Run Review",
            command=self.start_review
        )
        self.run_button.pack(fill="x", pady=(16, 0))
        
        # Progress bar (hidden until processing)
        self.progress_bar = ctk.CTkProgressBar(
            container,
            height=4,
            corner_radius=2,
            fg_color=COLORS["bg_input"],
            progress_color=COLORS["accent"]
        )
        self.progress_bar.set(0)
        # Will be packed when processing starts
        
        # Thinking panel (hidden until review complete)
        self.thinking_panel = ThinkingPanel(container)
        # Note: pack is called dynamically when thinking is available
        
        # Log area
        self.log = EnhancedLog(container)
        self.log.pack(fill="both", expand=True, pady=(16, 0))
        
    def _create_header(self, parent):
        """Create the header section."""
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))
        
        title = ctk.CTkLabel(
            header,
            text="Mechanical & Plumbing Spec Review",
            font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"),
            text_color=COLORS["text_primary"]
        )
        title.pack(anchor="w")
        
        subtitle = ctk.CTkLabel(
            header,
            text="California K-12 DSA Projects  •  Claude OPUS 4.5",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=COLORS["text_secondary"]
        )
        subtitle.pack(anchor="w", pady=(4, 0))
        
    def _create_inputs_card(self, parent):
        """Create the inputs card with API key and folder selections."""
        card = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8)
        card.pack(fill="x")
        
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=16)
        
        # API Key
        self._create_input_row(
            inner,
            label="API Key",
            placeholder="sk-ant-...",
            show="•",
            variable_name="api_key_entry",
            default_value=self.api_key,
            row=0
        )
        
        # Specs folder
        self._create_folder_row(
            inner,
            label="Specs Folder",
            placeholder="Select folder containing .docx files",
            variable_name="input_dir_entry",
            browse_command=self._browse_input,
            row=1
        )
        
        # Output folder
        self._create_folder_row(
            inner,
            label="Output Folder",
            placeholder="Select output folder",
            variable_name="output_dir_entry",
            browse_command=self._browse_output,
            # default_value=str(self.output_dir),
            row=2
        )
        
    def _create_input_row(self, parent, label, placeholder, variable_name, row, 
                          show=None, default_value=""):
        """Create a labeled input row."""
        label_widget = ctk.CTkLabel(
            parent,
            text=label,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_secondary"],
            width=100,
            anchor="w"
        )
        label_widget.grid(row=row, column=0, sticky="w", pady=8)
        
        entry = ctk.CTkEntry(
            parent,
            placeholder_text=placeholder,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=COLORS["bg_input"],
            border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            height=36,
            show=show
        )
        entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=8)
        
        if default_value:
            entry.insert(0, default_value)
            
        parent.columnconfigure(1, weight=1)
        setattr(self, variable_name, entry)
        
    def _create_folder_row(self, parent, label, placeholder, variable_name, 
                           browse_command, row, default_value=""):
        """Create a folder selection row with browse button."""
        label_widget = ctk.CTkLabel(
            parent,
            text=label,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLORS["text_secondary"],
            width=100,
            anchor="w"
        )
        label_widget.grid(row=row, column=0, sticky="w", pady=8)
        
        entry_frame = ctk.CTkFrame(parent, fg_color="transparent")
        entry_frame.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=8)
        entry_frame.columnconfigure(0, weight=1)
        
        entry = ctk.CTkEntry(
            entry_frame,
            placeholder_text=placeholder,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=COLORS["bg_input"],
            border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            height=36
        )
        entry.grid(row=0, column=0, sticky="ew")
        
        if default_value:
            entry.insert(0, default_value)
        
        browse_btn = ctk.CTkButton(
            entry_frame,
            text="Browse",
            width=70,
            height=36,
            font=ctk.CTkFont(size=12),
            fg_color=COLORS["bg_input"],
            hover_color=COLORS["border"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text_secondary"],
            command=browse_command
        )
        browse_btn.grid(row=0, column=1, padx=(8, 0))
        
        parent.columnconfigure(1, weight=1)
        setattr(self, variable_name, entry)
        
    # ========================================================================
    # ACTIONS
    # ========================================================================
    
    def _browse_input(self):
        """Open folder picker for input directory."""
        folder = ctk.filedialog.askdirectory(
            title="Select folder containing .docx specification files"
        )
        if folder:
            self.input_dir = Path(folder)
            self.input_dir_entry.delete(0, "end")
            self.input_dir_entry.insert(0, folder)
            
            # Analyze tokens immediately
            self._analyze_folder_tokens()
            
    def _browse_output(self):
        """Open folder picker for output directory."""
        folder = ctk.filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_dir = Path(folder)
            self.output_dir_entry.delete(0, "end")
            self.output_dir_entry.insert(0, folder)
            
    def _analyze_folder_tokens(self):
        """Analyze token usage for selected folder."""
        if not self.input_dir or not self.input_dir.exists():
            return
            
        docx_files = get_docx_files(self.input_dir)
        if not docx_files:
            self.log.log_warning("No .docx files found in folder")
            self.token_gauge.reset()
            return
            
        self.log.log_step(f"Analyzing {len(docx_files)} files...")
        
        # Run analysis in background to keep UI responsive
        def analyze():
            try:
                spec_contents = []
                for f in docx_files:
                    try:
                        spec = extract_text_from_docx(f)
                        spec_contents.append((spec.filename, spec.content))
                        self.after(0, lambda name=f.name: self.log.log_file(name))
                    except Exception as e:
                        self.after(0, lambda err=str(e), name=f.name: 
                                   self.log.log_warning(f"Could not read {name}: {err}"))
                
                if spec_contents:
                    system_prompt = get_system_prompt()
                    summary = analyze_token_usage(spec_contents, system_prompt)
                    
                    self.after(0, lambda: self.token_gauge.update_gauge(
                        summary.total_tokens, 
                        len(spec_contents)
                    ))
                    self.after(0, lambda: self.log.log_success(
                        f"Token analysis complete: {summary.total_tokens:,} tokens"
                    ))
                    
            except Exception as e:
                self.after(0, lambda: self.log.log_error(f"Analysis failed: {e}"))
                
        thread = threading.Thread(target=analyze, daemon=True)
        thread.start()
        
    def _validate_inputs(self) -> bool:
        """Validate all inputs before running."""
        api_key = self.api_key_entry.get().strip()
        if not api_key:
            self.log.log_error("API key is required")
            return False
            
        input_path = self.input_dir_entry.get().strip()
        if not input_path:
            self.log.log_error("Specs folder is required")
            return False
            
        input_dir = Path(input_path)
        if not input_dir.exists():
            self.log.log_error(f"Folder not found: {input_path}")
            return False
            
        docx_files = get_docx_files(input_dir)
        if not docx_files:
            self.log.log_error("No .docx files found in folder")
            return False
            
        # Check token limit
        if self.token_gauge.token_count > RECOMMENDED_MAX:
            self.log.log_error("Token limit exceeded. Remove some specs and try again.")
            return False
            
        return True
        
    def start_review(self):
        """Start the review process."""
        if self.is_processing:
            return
            
        if not self._validate_inputs():
            return
            
        self.is_processing = True
        self.thinking_panel.clear()
        
        # Add separator in log for new run
        self.log.log("─" * 40, level="muted", timestamp=False, paced=False)
        
        # Update UI
        self.run_button.set_processing()
        self.progress_bar.pack(fill="x", pady=(8, 0), after=self.run_button)
        self.progress_bar.set(0)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        
        # Set API key
        os.environ["ANTHROPIC_API_KEY"] = self.api_key_entry.get().strip()
        
        # Run in background
        thread = threading.Thread(target=self._run_review_thread, daemon=True)
        thread.start()
        
    def _run_review_thread(self):
        """Background thread for review process."""
        try:
            input_path = Path(self.input_dir_entry.get())
            output_path = Path(self.output_dir_entry.get())
            
            self.after(0, lambda: self.log.log_step("Starting review..."))
            self.after(0, lambda: self.log.log(f"Model: {MODEL_OPUS_45}", level="muted"))
            
            def log_callback(msg: str):
                self.after(0, lambda m=msg: self.log.log(m, level="info"))
                
            def progress_callback(pct: float, msg: str):
                self.after(0, lambda m=msg: self.log.log_step(m))
                
            result = run_review(
                input_dir=input_path,
                output_dir=output_path,
                dry_run=False,
                verbose=False,
                log=log_callback,
                progress=progress_callback
            )
            
            self.last_output_path = result.run_dir
            
            # Success
            self.after(0, lambda: self._on_review_complete(result))
            
        except Exception as e:
            import traceback
            error_msg = f"{e}\n{traceback.format_exc()}"
            self.after(0, lambda: self._on_review_error(error_msg))
            
    def _on_review_complete(self, result):
        """Handle successful review completion."""
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0)
        
        self.log.log_success("Review complete!")
        self.log.log(f"Output: {result.run_dir}", level="muted", timestamp=False)
        self.log.log(f"Report: {result.report_docx.name}", level="muted", timestamp=False)
        
        if result.review_result:
            findings = result.review_result
            self.log.log(
                f"Findings: {findings.critical_count} critical, {findings.high_count} high, "
                f"{findings.medium_count} medium, {findings.gripe_count} gripes",
                level="info"
            )
            
            # Log timing info
            elapsed = findings.elapsed_seconds
            file_count = len(list(Path(self.input_dir_entry.get()).glob("*.docx")))
            avg_time = elapsed / file_count if file_count > 0 else 0
            self.log.log(
                f"Time: {elapsed:.1f}s total, {avg_time:.1f}s avg per spec",
                level="muted"
            )
            
            # Display Claude's thinking if present
            if findings.thinking:
                self.thinking_panel.set_thinking(findings.thinking, before_widget=self.log)
            
        self.run_button.set_complete()
        
        # Reset button after delay
        self.after(2500, self._reset_ui)
        
        # Auto-open report
        try:
            os.startfile(result.report_docx)
        except Exception:
            pass
            
    def _on_review_error(self, error_msg: str):
        """Handle review error."""
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        
        self.log.log_error(f"Review failed: {error_msg}")
        self.run_button.set_ready()
        self.is_processing = False
        
    def _reset_ui(self):
        """Reset UI to ready state."""
        self.run_button.set_ready()
        self.progress_bar.pack_forget()
        self.is_processing = False


# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    
    app = SpecReviewApp()
    app.mainloop()


if __name__ == "__main__":
    main()