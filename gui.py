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

from src.extractor import extract_text_from_docx
from src.preprocessor import preprocess_spec
from src.tokenizer import analyze_token_usage, RECOMMENDED_MAX
from src.prompts import get_system_prompt
from src.reviewer import review_specs, MODEL_SONNET, MODEL_OPUS, MODEL_HAIKU
from src.report import generate_report


class SpecReviewApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Mechanical & Plumbing Spec Review - California K-12 DSA Projects")
        self.root.geometry("700x600")
        self.root.minsize(600, 500)
        
        # Variables
        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path.home() / "Desktop" / "spec-review-output"))
        self.model_choice = tk.StringVar(value="Opus")
        self.use_thinking = tk.BooleanVar(value=False)
        self.api_key = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        
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
            docx_files = list(Path(folder).glob("*.docx"))
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
        
        # Create timestamped output folder
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        run_dir = output_base / f"review_{timestamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        stripped_dir = run_dir / "stripped"
        stripped_dir.mkdir(exist_ok=True)
        
        self.last_output_path = run_dir
        
        self.root.after(0, lambda: self.set_status("Loading specification files..."))
        self.root.after(0, lambda: self.log(f"Output folder: {run_dir}"))
        
        # Load files
        docx_files = sorted(input_path.glob("*.docx"))
        specs = []
        
        for docx_file in docx_files:
            self.root.after(0, lambda f=docx_file: self.log(f"Loading: {f.name}"))
            spec = extract_text_from_docx(docx_file)
            specs.append(spec)
        
        self.root.after(0, lambda: self.log(f"\nLoaded {len(specs)} file(s)"))
        
        # Preprocess
        self.root.after(0, lambda: self.set_status("Preprocessing specifications..."))
        
        preprocess_results = []
        all_leed_alerts = []
        all_placeholder_alerts = []
        
        for spec in specs:
            result = preprocess_spec(spec.content, spec.filename)
            preprocess_results.append(result)
            all_leed_alerts.extend(result.leed_alerts)
            all_placeholder_alerts.extend(result.placeholder_alerts)
            
            # Save stripped content
            stripped_path = stripped_dir / f"{Path(spec.filename).stem}_stripped.txt"
            with open(stripped_path, "w", encoding="utf-8") as f:
                f.write(result.cleaned_content)
        
        # Log alerts
        if all_leed_alerts:
            self.root.after(0, lambda: self.log(f"\n⚠ Found {len(all_leed_alerts)} LEED reference(s)"))
        if all_placeholder_alerts:
            self.root.after(0, lambda: self.log(f"⚠ Found {len(all_placeholder_alerts)} placeholder(s)"))
        
        # Token analysis
        self.root.after(0, lambda: self.set_status("Analyzing token usage..."))
        
        system_prompt = get_system_prompt()
        spec_contents = [(spec.filename, result.cleaned_content) for spec, result in zip(specs, preprocess_results)]
        token_summary = analyze_token_usage(spec_contents, system_prompt)
        
        self.root.after(0, lambda: self.log(f"\nToken usage: {token_summary.total_tokens:,} / {RECOMMENDED_MAX:,}"))
        
        if not token_summary.within_limit:
            raise Exception(f"Token limit exceeded: {token_summary.total_tokens:,} > {RECOMMENDED_MAX:,}")
        
        # Determine model
        model_choice = self.model_choice.get()
        use_thinking = self.use_thinking.get()
        
        if model_choice == "opus":
            model = MODEL_OPUS
            model_name = "Opus 4.5"
        else:
            model = MODEL_SONNET
            model_name = "Sonnet 4.5"
        
        self.root.after(0, lambda: self.set_status(f"Sending to Claude API ({model_name})..."))
        self.root.after(0, lambda: self.log(f"\nCalling {model_name}..."))
        self.root.after(0, lambda: self.log("This may take a few minutes. Please wait..."))
        
        # Build combined content
        combined_content = "\n\n".join([
            f"===== FILE: {spec.filename} =====\n{result.cleaned_content}"
            for spec, result in zip(specs, preprocess_results)
        ])
        
        # Call API
        review_result = review_specs(
            combined_content,
            model=model,
            use_thinking=use_thinking,
            verbose=False
        )
        
        if review_result.error:
            raise Exception(review_result.error)
        
        self.root.after(0, lambda: self.log(f"\nReview complete! ({review_result.elapsed_seconds:.1f}s)"))
        self.root.after(0, lambda: self.log(f"Tokens: {review_result.input_tokens:,} in → {review_result.output_tokens:,} out"))
        
        # Log findings summary
        self.root.after(0, lambda: self.log(f"\nFindings:"))
        self.root.after(0, lambda: self.log(f"  CRITICAL: {review_result.critical_count}"))
        self.root.after(0, lambda: self.log(f"  HIGH: {review_result.high_count}"))
        self.root.after(0, lambda: self.log(f"  MEDIUM: {review_result.medium_count}"))
        self.root.after(0, lambda: self.log(f"  LOW: {review_result.low_count}"))
        self.root.after(0, lambda: self.log(f"  GRIPES: {review_result.gripes_count}"))
        self.root.after(0, lambda: self.log(f"  TOTAL: {review_result.total_count}"))
        
        # Generate report
        self.root.after(0, lambda: self.set_status("Generating report..."))
        
        leed_alerts_dicts = [{"filename": a['filename'], "line": a['line'], "text": a['text']} for a in all_leed_alerts]
        placeholder_alerts_dicts = [{"filename": a['filename'], "line": a['line'], "text": a['text']} for a in all_placeholder_alerts]
        
        report_path = generate_report(
            review_result=review_result,
            files_reviewed=[spec.filename for spec in specs],
            leed_alerts=leed_alerts_dicts,
            placeholder_alerts=placeholder_alerts_dicts,
            output_path=run_dir
        )
        
        # Save JSON too
        import json
        json_path = run_dir / "findings.json"
        findings_data = {
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "model": review_result.model,
                "input_tokens": review_result.input_tokens,
                "output_tokens": review_result.output_tokens,
                "thinking_tokens": review_result.thinking_tokens,
                "elapsed_seconds": review_result.elapsed_seconds,
                "files_reviewed": [spec.filename for spec in specs],
            },
            "summary": {
                "critical": review_result.critical_count,
                "high": review_result.high_count,
                "medium": review_result.medium_count,
                "low": review_result.low_count,
                "gripes": review_result.gripes_count,
                "total": review_result.total_count,
            },
            "findings": [
                {
                    "severity": f.severity,
                    "fileName": f.fileName,
                    "section": f.section,
                    "issue": f.issue,
                    "actionType": f.actionType,
                    "existingText": f.existingText,
                    "replacementText": f.replacementText,
                    "codeReference": f.codeReference,
                }
                for f in review_result.findings
            ]
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(findings_data, f, indent=2, ensure_ascii=False)
        
        self.root.after(0, lambda: self.log(f"\n✓ Report saved: {report_path.name}"))
        self.root.after(0, lambda: self.log(f"✓ JSON saved: {json_path.name}"))
        self.root.after(0, lambda: self.set_status("Done! Click 'Open Output Folder' to view results."))
        
        # Open report automatically
        self.root.after(0, lambda: os.startfile(report_path))
        
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
