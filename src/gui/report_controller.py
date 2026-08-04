"""Report export.

Coordinates the ``export_report`` call (with filedialog and progress
logging) and writes the machine-readable edit-instructions sidecar beside
the report on success. Returns status strings ("canceled" / "success" /
"error") so the caller can decide what to log.

``export_html_report_to_file`` is the additive post-run HTML action: it
consumes the already-completed result retained on ``app._last_result``
read-only and writes one self-contained HTML file. It runs only when the
user clicks "Save HTML Report…" — the automatic DOCX-at-completion flow
above is untouched, no sidecars are written, and cancellation or failure
leaves the retained result and every existing export intact.
"""
from __future__ import annotations

import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

from ..output.html_report_exporter import write_html_report
from ..output.report_exporter import export_report
from ..output.edit_sidecar import (
    write_edit_instructions_sidecar,
    write_requirements_profile_sidecar,
)


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
        try:
            sidecar_path = write_edit_instructions_sidecar(result, output_path)
            app.log.log_success(f"Edit instructions saved: {sidecar_path.name}")
        except Exception as e:
            app.log.log_warning(f"Edit-instructions sidecar not written: {e}")
        # WS-4 (D-14 [FT]): the standalone requirements-profile export — the
        # longest-half-life artifact. Returns None (writes nothing) on every
        # profile-less run.
        try:
            profile_path = write_requirements_profile_sidecar(result, output_path)
            if profile_path is not None:
                app.log.log_success(f"Requirements profile saved: {profile_path.name}")
        except Exception as e:
            app.log.log_warning(f"Requirements-profile sidecar not written: {e}")
        return "success"
    except Exception as e:
        app.log.log_error(f"Export failed: {e}")
        return "error"


def export_html_report_to_file(app, result) -> str:
    """Save the completed result as a self-contained HTML report.

    Mirrors :func:`export_report_to_file`'s canceled/success/error contract.
    A canceled save changes nothing; a write failure is logged and leaves the
    result usable; on success the report is opened in the default browser as
    a nonfatal convenience.
    """
    default_name = f"spec-critic-report-{datetime.now().strftime('%Y-%m-%d')}.html"
    path = filedialog.asksaveasfilename(
        title="Save HTML Report",
        defaultextension=".html",
        filetypes=[("HTML Files", "*.html"), ("All Files", "*.*")],
        initialfile=default_name,
    )
    if not path:
        app.log.log_warning("HTML export canceled")
        return "canceled"
    try:
        output_path = Path(path)
        app.log.log_step(f"Exporting HTML report to {output_path.name}...")
        write_html_report(result, output_path)
        app.log.log_success(f"HTML report saved: {output_path}")
        try:
            webbrowser.open(output_path.resolve().as_uri())
        except Exception:
            pass  # opening the browser is a convenience, never a failure
        return "success"
    except Exception as e:
        app.log.log_error(f"HTML export failed: {e}")
        return "error"
