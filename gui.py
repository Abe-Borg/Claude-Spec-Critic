"""GUI application for Mechanical & Plumbing Specifications Review."""
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from datetime import datetime

# Add src to path for imports
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, base_path)

from src.pipeline import run_review
from src.reviewer import MODEL_OPUS_45



class SpecReviewApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Mechanical & Plumbing Spec Review - California K-12 DSA Projects")
        self.root.geometry("700x600")
        self.root.minsize(600, 500)
        
        # Variables
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path.home() / "Desktop" / "spec-review-output"))
        self.api_key = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.verbose = tk.BooleanVar(value=False)
        self.dry_run = tk.BooleanVar(value=False)

        
        self.create_widgets()
        
    def create_widgets(self):
        # Main container with padding
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        
        row = 0
        
        # Title
        title_label = ttk.Label(main_frame, text="Mechanical & Plumbing Specifications Review", font=("Arial", 16, "bold"))
        title_label.grid(row=row, column=0, columnspan=3, pady=(0, 10))
        row += 1
        
        subtitle = ttk.Label(main_frame, text="California K-12 DSA Projects", font=("Arial", 10))
        subtitle.grid(row=row, column=0, columnspan=3, pady=(0, 15))
        row += 1
        
        # API Key
        ttk.Label(main_frame, text="API Key:").grid(row=row, column=0, sticky="w", pady=5)
        api_entry = ttk.Entry(main_frame, textvariable=self.api_key, width=50, show="*")
        api_entry.grid(row=row, column=1, sticky="ew", pady=5, padx=5)
        row += 1
        
        # Input directory
        ttk.Label(main_frame, text="Specs Folder:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(main_frame, textvariable=self.input_dir, width=50).grid(row=row, column=1, sticky="ew", pady=5, padx=5)
        ttk.Button(main_frame, text="Browse...", width=10, command=self.browse_input).grid(row=row, column=2, pady=5)
        row += 1
        
        # Output directory
        ttk.Label(main_frame, text="Output Folder:").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(main_frame, textvariable=self.output_dir, width=50).grid(row=row, column=1, sticky="ew", pady=5, padx=5)
        ttk.Button(main_frame, text="Browse...", width=10, command=self.browse_output).grid(row=row, column=2, pady=5)
        row += 1
        
        row += 1
        
        # Run button
        self.run_button = ttk.Button(main_frame, text="Run Review", command=self.start_review, style="Accent.TButton")
        self.run_button.grid(row=row, column=0, columnspan=3, pady=15, ipadx=20, ipady=5)
        row += 1
        
        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode="indeterminate")
        self.progress.grid(row=row, column=0, columnspan=3, sticky="ew", pady=5)
        row += 1
        
        # Status label
        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(main_frame, textvariable=self.status_var, font=("Arial", 9))
        self.status_label.grid(row=row, column=0, columnspan=3, pady=5)
        row += 1
        
        # Log area
        ttk.Label(main_frame, text="Log:").grid(row=row, column=0, sticky="w", pady=(10, 5))
        row += 1
        
        self.log_area = scrolledtext.ScrolledText(main_frame, height=12, width=70, font=("Consolas", 9))
        self.log_area.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=5)
        main_frame.rowconfigure(row, weight=1)
        row += 1
        
        # Open output folder button
        self.open_folder_button = ttk.Button(main_frame, text="Open Output Folder", command=self.open_output_folder, state="disabled")
        self.open_folder_button.grid(row=row, column=0, columnspan=3, pady=10)
        
        self.last_output_path = None
        
    def browse_input(self):
        folder = filedialog.askdirectory(title="Select folder containing .docx specification files")
        if folder:
            self.input_dir.set(folder)
            self.log(f"Input folder: {folder}")
            
            # Count docx files
            docx_files = [p for p in Path(folder).glob("*.docx") if not p.name.startswith("~$")]
            self.log(f"Found {len(docx_files)} .docx file(s)")
    
    def browse_output(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_dir.set(folder)
            self.log(f"Output folder: {folder}")
    
    def log(self, message):
        self.log_area.insert(tk.END, f"{message}\n")
        self.log_area.see(tk.END)
        self.root.update_idletasks()
    
    def set_status(self, message):
        self.status_var.set(message)
        self.root.update_idletasks()
    
    def open_output_folder(self):
        if self.last_output_path and self.last_output_path.exists():
            os.startfile(self.last_output_path)
    
    def validate_inputs(self):
        if not self.api_key.get().strip():
            messagebox.showerror("Error", "Please enter your Anthropic API key.")
            return False
        
        if not self.input_dir.get().strip():
            messagebox.showerror("Error", "Please select a folder containing specification files.")
            return False
        
        input_path = Path(self.input_dir.get())
        if not input_path.exists():
            messagebox.showerror("Error", f"Input folder does not exist:\n{input_path}")
            return False
        
        docx_files = list(input_path.glob("*.docx"))
        if not docx_files:
            messagebox.showerror("Error", "No .docx files found in the selected folder.")
            return False
        
        return True
    
    def start_review(self):
        if not self.validate_inputs():
            return
        
        # Set API key in environment
        os.environ["ANTHROPIC_API_KEY"] = self.api_key.get().strip()
        
        # Disable UI during processing
        self.run_button.config(state="disabled")
        self.open_folder_button.config(state="disabled")
        self.progress.start()
        self.log_area.delete(1.0, tk.END)
        
        # Run in background thread
        thread = threading.Thread(target=self.run_review_thread)
        thread.daemon = True
        thread.start()
    
    def run_review_thread(self):
        try:
            self.do_review()
        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n\n{traceback.format_exc()}"
            self.root.after(0, lambda: self.on_error(error_msg))
        finally:
            self.root.after(0, self.on_complete)
    
    def do_review(self):
        input_path = Path(self.input_dir.get())
        output_base = Path(self.output_dir.get())

        self.root.after(0, lambda: self.set_status(f"Running (model: {MODEL_OPUS_45})..."))
        self.root.after(0, lambda: self.log(f"Model: {MODEL_OPUS_45}"))
        self.root.after(0, lambda: self.log(f"Input folder: {input_path}"))
        self.root.after(0, lambda: self.log(f"Output base: {output_base}\n"))

        def log(msg: str) -> None:
            self.root.after(0, lambda m=msg: self.log(m))

        def prog(pct: float, msg: str) -> None:
            # Your progress bar is indeterminate; we’ll just update status + log milestones.
            self.root.after(0, lambda m=msg: self.set_status(m))
            # If you ever switch to determinate, you can bind pct to a DoubleVar and set it here.

        out = run_review(
            input_dir=input_path,
            output_dir=output_base,
            dry_run=self.dry_run.get(),
            verbose=self.verbose.get(),
            log=log,
            progress=prog,
        )

        self.last_output_path = out.run_dir

        self.root.after(0, lambda: self.log("\n✓ Complete"))
        self.root.after(0, lambda: self.log(f"Run folder: {out.run_dir}"))
        self.root.after(0, lambda: self.log(f"Report: {out.report_docx.name}"))
        self.root.after(0, lambda: self.log(f"Findings JSON: {out.findings_json.name}"))
        self.root.after(0, lambda: self.log(f"Raw response: {out.raw_response_txt.name}"))

        self.root.after(0, lambda: self.set_status("Done! Click 'Open Output Folder' to view results."))

        # Auto-open report (skip on dry-run where report still exists but may be empty)
        try:
            self.root.after(0, lambda p=out.report_docx: os.startfile(p))
        except Exception:
            pass


    def on_error(self, message):
        self.log(f"\n❌ ERROR: {message}")
        self.set_status("Error occurred")
        messagebox.showerror("Error", message)
    
    def on_complete(self):
        self.progress.stop()
        self.run_button.config(state="normal")
        if self.last_output_path:
            self.open_folder_button.config(state="normal")


def main():
    root = tk.Tk()
    
    # Set style
    style = ttk.Style()
    style.theme_use("clam")
    
    app = SpecReviewApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
