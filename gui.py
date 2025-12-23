"""
MEP Spec Review - Modern GUI with CustomTkinter
California K-12 DSA Projects
"""
import os
import sys
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

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
    "text_secondary": "#888888",
    "text_muted": "#555555",
    "accent": "#3B82F6",        # Blue
    "accent_hover": "#2563EB",
    "success": "#22C55E",
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


# ============================================================================
# CUSTOM WIDGETS
# ============================================================================

class TokenGauge(ctk.CTkFrame):
    """Visual gauge showing token usage against limit."""
    
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_card"], corner_radius=8, **kwargs)
        
        self.token_count = 0
        self.max_tokens = RECOMMENDED_MAX
        
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
        """Update the gauge with new token count."""
        self.token_count = tokens
        pct = min(tokens / self.max_tokens, 1.0)
        
        # Update count label
        self.count_label.configure(text=f"{tokens:,} / {self.max_tokens:,}")
        
        # Update progress bar width
        bar_width = self.bar_frame.winfo_width()
        if bar_width > 1:
            self.progress_bar.configure(width=int(bar_width * pct))
        
        # Update color based on usage
        if pct > 1.0:
            color = COLORS["error"]
            status = f"⚠ EXCEEDS LIMIT — Cannot process. Remove some specs."
        elif pct > 0.9:
            color = COLORS["warning"]
            status = f"⚠ {pct*100:.0f}% capacity — Approaching limit"
        elif pct > 0.7:
            color = COLORS["warning"]
            status = f"✓ {pct*100:.0f}% capacity — {file_count} files ready"
        else:
            color = COLORS["success"]
            status = f"✓ {pct*100:.0f}% capacity — {file_count} files ready"
        
        self.progress_bar.configure(fg_color=color)
        self.status_label.configure(text=status, text_color=color if pct > 0.9 else COLORS["text_secondary"])
        
    def reset(self):
        """Reset gauge to initial state."""
        self.token_count = 0
        self.count_label.configure(text="— / 150,000")
        self.progress_bar.configure(width=0, fg_color=COLORS["accent"])
        self.status_label.configure(
            text="Select a specs folder to analyze token usage",
            text_color=COLORS["text_muted"]
        )


class EnhancedLog(ctk.CTkFrame):
    """Enhanced log display with colored entries and timestamps."""
    
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_card"], corner_radius=8, **kwargs)
        
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
        
    def log(self, message: str, level: str = "info", timestamp: bool = True):
        """Add a log entry with optional timestamp and color."""
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
        
        # Auto-scroll to bottom
        self.log_frame._parent_canvas.yview_moveto(1.0)
        
    def clear(self):
        """Clear all log entries."""
        for entry in self.entries:
            entry.destroy()
        self.entries.clear()
        
    def log_step(self, message: str):
        """Log a major step."""
        self.log(f"▸ {message}", level="step")
        
    def log_success(self, message: str):
        """Log a success message."""
        self.log(f"✓ {message}", level="success")
        
    def log_warning(self, message: str):
        """Log a warning."""
        self.log(f"⚠ {message}", level="warning")
        
    def log_error(self, message: str):
        """Log an error."""
        self.log(f"✗ {message}", level="error")
        
    def log_file(self, filename: str):
        """Log a file being processed."""
        self.log(f"  → {filename}", level="file", timestamp=False)


class AnimatedButton(ctk.CTkButton):
    """Button with state-based styling for run/processing/complete states."""
    
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
        
    def set_processing(self):
        """Set button to processing state."""
        self._state = "processing"
        self.configure(
            text="Processing...",
            fg_color=COLORS["bg_input"],
            hover_color=COLORS["bg_input"],
            state="disabled"
        )
        
    def set_ready(self):
        """Reset button to ready state."""
        self._state = "ready"
        self.configure(
            text=self.default_text,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            state="normal"
        )
        
    def set_complete(self):
        """Set button to complete state (brief success indicator)."""
        self._state = "complete"
        self.configure(
            text="✓ Complete",
            fg_color=COLORS["success"],
            hover_color=COLORS["success"],
            state="disabled"
        )


# ============================================================================
# MAIN APPLICATION
# ============================================================================

class SpecReviewApp(ctk.CTk):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        
        # Window setup
        self.title("MEP Spec Review")
        self.geometry("800x700")
        self.minsize(700, 600)
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
        
        # Log area
        self.log = EnhancedLog(container)
        self.log.pack(fill="both", expand=True, pady=(16, 0))
        
        # Footer with output button
        self._create_footer(container)
        
    def _create_header(self, parent):
        """Create the header section."""
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))
        
        title = ctk.CTkLabel(
            header,
            text="MEP Spec Review",
            font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"),
            text_color=COLORS["text_primary"]
        )
        title.pack(anchor="w")
        
        subtitle = ctk.CTkLabel(
            header,
            text=f"California K-12 DSA Projects  •  {MODEL_OPUS_45}",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=COLORS["text_muted"]
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
            default_value=str(self.output_dir),
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
        
    def _create_footer(self, parent):
        """Create footer with output folder button."""
        footer = ctk.CTkFrame(parent, fg_color="transparent", height=44)
        footer.pack(fill="x", pady=(16, 0))
        
        self.open_folder_btn = ctk.CTkButton(
            footer,
            text="Open Output Folder",
            font=ctk.CTkFont(size=12),
            fg_color="transparent",
            hover_color=COLORS["bg_card"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text_secondary"],
            height=36,
            state="disabled",
            command=self._open_output_folder
        )
        self.open_folder_btn.pack(side="right")
        
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
        
    def _open_output_folder(self):
        """Open the output folder in file explorer."""
        if self.last_output_path and self.last_output_path.exists():
            os.startfile(self.last_output_path)
            
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
        self.log.clear()
        
        # Update UI
        self.run_button.set_processing()
        self.progress_bar.pack(fill="x", pady=(8, 0), before=self.log)
        self.progress_bar.set(0)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.open_folder_btn.configure(state="disabled")
        
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
            
        self.run_button.set_complete()
        self.open_folder_btn.configure(state="normal")
        
        # Reset button after delay
        self.after(2000, self._reset_ui)
        
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