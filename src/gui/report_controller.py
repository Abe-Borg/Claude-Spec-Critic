"""Report export.

Coordinates the ``export_report`` call (with filedialog and progress
logging). Returns status strings ("canceled" / "success" / "error") so
the caller can decide what to log or whether to open the edit-selection
dialog.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tkinter import filedialog

from ..output.report_exporter import export_report


def export_report_to_file(app, result) -> str:
    default_name = f"spec-critic-report-{datetime.now().strftime('%Y-%m-%d')}.docx"
    path = filedialog.asksaveasfilename(
        title="Save Review Report",
        defaultextension=".docx",
        filetypes=[("Word Documents", "*.docx"), ("All Files", "*.*")],
        initialfile=default_name,
    )
    if not path:
        app.log.log_warning("Export canceled")
        return "canceled"
    try:
        output_path = Path(path)
        app.log.log_step(f"Exporting report to {output_path.name}...")
        export_report(result, output_path)
        app.log.log_success(f"Report saved: {output_path}")
        return "success"
    except Exception as e:
        app.log.log_error(f"Export failed: {e}")
        return "error"
