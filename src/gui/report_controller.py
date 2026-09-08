"""Report export.

Coordinates the ``export_report`` call (with filedialog and progress
logging) and writes the machine-readable edit-instructions sidecar beside
the report on success. Returns status strings ("canceled" / "success" /
"error") so the caller can decide what to log.

Two things a paid run must never lose to a locked file:

* **A failed write is a dialog, not a log line.** The usual cause on
  Windows is the target ``.docx`` (or an earlier report with the same name)
  being open in Word, which holds the file locked. The failure surfaces as
  an ``askretrycancel`` with that hint; Retry re-attempts the same path,
  Cancel keeps the completed result in memory so the footer's
  "Save Word Report…" can try again later.
* **The write runs off the Tk thread.** DOCX rendering plus the sidecars
  can take seconds on a large run; ``export_report_to_file(...,
  on_complete=...)`` shows the save dialog on the Tk thread, writes on a
  worker, and marshals every log line / dialog / completion back with
  ``app.after(0, ...)``. Without ``on_complete`` the call is synchronous
  (legacy contract, used by headless-style callers and test doubles).

``export_html_report_to_file`` is the additive post-run HTML action: it
consumes the already-completed result retained on ``app._last_result``
read-only and writes one self-contained HTML file. It runs only when the
user clicks "Save HTML Report…" — the automatic DOCX-at-completion flow
above is untouched, no sidecars are written, and cancellation or failure
leaves the retained result and every existing export intact.
"""
from __future__ import annotations

import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

from ..output.html_report_exporter import write_html_report
from ..output.report_exporter import export_report
from ..output.edit_sidecar import (
    write_edit_instructions_sidecar,
    write_requirements_profile_sidecar,
)

# Returned by the asynchronous form of ``export_report_to_file`` (a worker
# is writing; the terminal status arrives through ``on_complete``).
EXPORT_STATUS_PENDING = "pending"

_EXPORT_FAILURE_HINT = (
    "Spec Critic could not write the report to:\n\n{path}\n\n{error}\n\n"
    "On Windows the usual cause is that this file (or an earlier report with "
    "the same name) is open in Word, which keeps it locked. Close it — or "
    "pick a different name — and choose Retry.\n\n"
    "Choose Cancel to keep the results in memory; you can save them later "
    "with the footer's “Save Word Report…” button."
)


def _ask_report_path(app) -> str:
    default_name = f"spec-critic-report-{datetime.now().strftime('%Y-%m-%d')}.docx"
    return filedialog.asksaveasfilename(
        title="Save Review Report",
        defaultextension=".docx",
        filetypes=[("Word Documents", "*.docx"), ("All Files", "*.*")],
        initialfile=default_name,
        parent=app,
    )


def _write_report_and_sidecars(result, output_path: Path) -> list[tuple[str, str]]:
    """Write the ``.docx`` plus its sidecars; return ``(level, message)`` log lines.

    Pure with respect to the GUI (no ``app`` access) so it can run on a
    worker thread — the caller emits the returned lines on the Tk thread.
    Raises only when the REPORT itself cannot be written; a sidecar failure
    is a warning line, never a failed export (the report is the deliverable).
    """
    export_report(result, output_path)
    entries: list[tuple[str, str]] = [("success", f"Report saved: {output_path}")]
    try:
        sidecar_path = write_edit_instructions_sidecar(result, output_path)
        entries.append(("success", f"Edit instructions saved: {sidecar_path.name}"))
    except Exception as e:  # noqa: BLE001 — sidecar failure must not fail the export
        entries.append(("warning", f"Edit-instructions sidecar not written: {e}"))
    # WS-4 (D-14 [FT]): the standalone requirements-profile export — the
    # longest-half-life artifact. Returns None (writes nothing) on every
    # profile-less run.
    try:
        profile_path = write_requirements_profile_sidecar(result, output_path)
        if profile_path is not None:
            entries.append(("success", f"Requirements profile saved: {profile_path.name}"))
    except Exception as e:  # noqa: BLE001 — same policy as the edit sidecar
        entries.append(("warning", f"Requirements-profile sidecar not written: {e}"))
    return entries


def _emit_log_entries(app, entries: list[tuple[str, str]]) -> None:
    for level, message in entries:
        emit = getattr(app.log, f"log_{level}", None) or app.log.log_warning
        emit(message)


def _ask_retry(app, output_path: Path, error: BaseException) -> bool:
    return messagebox.askretrycancel(
        "Report export failed",
        _EXPORT_FAILURE_HINT.format(path=output_path, error=error),
        parent=app,
    )


def _export_with_retry(app, result, output_path: Path) -> str:
    """Synchronous write with the retry prompt. Runs on the Tk thread."""
    while True:
        app.log.log_step(f"Exporting report to {output_path.name}...")
        try:
            entries = _write_report_and_sidecars(result, output_path)
        except Exception as e:  # noqa: BLE001 — surfaced to the operator
            app.log.log_error(f"Export failed: {e}")
            if _ask_retry(app, output_path, e):
                continue
            return "error"
        _emit_log_entries(app, entries)
        return "success"


def _export_in_background(app, result, output_path: Path, on_complete) -> None:
    """Write on a worker; log lines, the retry prompt, and ``on_complete`` run
    on the Tk thread via ``app.after(0, ...)``. Retry re-launches the worker."""

    def _attempt() -> None:
        app.log.log_step(f"Exporting report to {output_path.name}...")
        threading.Thread(
            target=_worker, name="spec-critic-report-export", daemon=True
        ).start()

    def _worker() -> None:
        try:
            entries = _write_report_and_sidecars(result, output_path)
        except Exception as exc:  # noqa: BLE001 — surfaced on the Tk thread
            # Default-arg binding: Python clears ``exc`` when the except block
            # exits, so a plain closure would NameError when the Tk callback
            # fires later (context_controller precedent).
            app.after(0, lambda e=exc: _failed(e))
            return
        app.after(0, lambda lines=entries: _succeeded(lines))

    def _succeeded(entries: list[tuple[str, str]]) -> None:
        _emit_log_entries(app, entries)
        on_complete("success")

    def _failed(exc: BaseException) -> None:
        app.log.log_error(f"Export failed: {exc}")
        if _ask_retry(app, output_path, exc):
            _attempt()
        else:
            on_complete("error")

    _attempt()


def export_report_to_file(app, result, *, on_complete=None) -> str:
    """Save ``result`` as a Word report (+ sidecars) after a save-as dialog.

    Without ``on_complete``: synchronous; returns ``"canceled"`` /
    ``"success"`` / ``"error"`` once the retry loop has settled.

    With ``on_complete``: the dialog runs now (Tk thread), the write runs on
    a worker, and ``on_complete(status)`` is called exactly once on the Tk
    thread with the same three statuses — a canceled dialog reports
    ``"canceled"`` immediately. Returns ``EXPORT_STATUS_PENDING`` while the
    worker runs (or ``"canceled"``).
    """
    path = _ask_report_path(app)
    if not path:
        app.log.log_warning("Export canceled")
        if on_complete is not None:
            on_complete("canceled")
        return "canceled"
    output_path = Path(path)
    if on_complete is None:
        return _export_with_retry(app, result, output_path)
    _export_in_background(app, result, output_path, on_complete)
    return EXPORT_STATUS_PENDING


def export_word_report_to_file(app, result, *, on_complete=None) -> str:
    """Footer "Save Word Report…": the at-completion export, on demand.

    Same dialog, same ``.docx`` writer, same sidecars, same retry prompt as
    the automatic export that runs when a review completes — so a report
    lost to a locked file (or a canceled dialog) can be produced later from
    the retained result. Mirrors ``export_html_report_to_file``'s
    canceled/success/error contract; the retained result is read-only.
    """
    return export_report_to_file(app, result, on_complete=on_complete)


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
        parent=app,
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
