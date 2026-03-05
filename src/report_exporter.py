"""
Word document report exporter for Spec Critic.

Generates a formatted .docx report from a PipelineResult, replicating
everything the in-app ReportWindow shows:
    - Title block with generation metadata
    - Files reviewed list
    - Summary table (severity counts) with colored cell shading
    - LEED and placeholder alerts
    - Per-spec findings grouped by severity, sorted by confidence
    - Verification verdicts and corrections inline with findings
    - Cross-spec coordination findings (if cross-check was enabled)
    - Reviewer's notes / analysis summary

This module exists to solve the GUI freezing problem when rendering
large reviews in-app. When "Export Report" output mode is selected,
the pipeline runs normally but results are written to a .docx file
instead of being rendered in CustomTkinter widgets.

Design decisions:
    - Uses python-docx (already a project dependency) for .docx generation
    - Accepts the same PipelineResult that the GUI receives — no pipeline changes
    - Uses real Word heading styles (doc.add_heading) for proper structure
    - Uses 'Table Grid' style and colored cell shading for the summary table
    - Uses 'List Bullet' style for file lists and alert details
    - Findings use the old structured layout with labeled rows on separate lines
    - Verification verdicts shown inline beneath each finding
    - The exporter is stateless — one function call, one file written
    - No GUI dependencies — can be called from any context

v1.8.0 — Initial implementation.
v1.8.1 — Restyled to use Word-native formatting (real headings, Table Grid,
    Arial 11pt, structured finding entries with labeled rows).

Usage:
    from report_exporter import export_report

    export_report(
        pipeline_result=result,
        output_path=Path("review-report.docx"),
        project_context="New 2-story elementary school",
    )
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


# ---------------------------------------------------------------------------
# Color constants
# ---------------------------------------------------------------------------

SEVERITY_COLORS = {
    "CRITICAL": RGBColor(192, 0, 0),      # Dark red
    "HIGH": RGBColor(255, 102, 0),         # Orange
    "MEDIUM": RGBColor(192, 152, 0),       # Dark yellow/gold
    "GRIPES": RGBColor(128, 0, 128),       # Purple
}

# Hex versions for cell shading (no # prefix)
SEVERITY_SHADING = {
    "CRITICAL": "C00000",
    "HIGH": "FF6600",
    "MEDIUM": "C09800",
    "GRIPES": "800080",
}

VERDICT_COLORS = {
    "CONFIRMED": RGBColor(0, 128, 0),     # Green
    "CORRECTED": RGBColor(204, 132, 0),    # Amber
    "UNVERIFIED": RGBColor(128, 128, 128), # Gray
    "DISPUTED": RGBColor(192, 0, 0),       # Red
}

VERDICT_ICONS = {
    "CONFIRMED": "✓",
    "CORRECTED": "✎",
    "DISPUTED": "✗",
    "UNVERIFIED": "—",
}

CONFIDENCE_COLORS = {
    "high": RGBColor(0, 128, 0),           # Green
    "moderate": RGBColor(204, 132, 0),     # Amber
    "low": RGBColor(192, 0, 0),            # Red
}

COORDINATION_COLOR = RGBColor(6, 182, 212)  # Cyan

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "GRIPES"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _confidence_tier(confidence: float) -> str:
    """Return 'high', 'moderate', or 'low' for a confidence score."""
    if confidence >= 0.85:
        return "high"
    elif confidence >= 0.60:
        return "moderate"
    return "low"


def _set_cell_shading(cell, hex_color: str) -> None:
    """Set background shading for a table cell."""
    shading = OxmlElement('w:shd')
    shading.set(qn('w:fill'), hex_color)
    cell._tc.get_or_add_tcPr().append(shading)


def _add_styled_paragraph(doc: Document, text: str, style: str | None = None,
                          bold: bool = False, color: RGBColor | None = None,
                          size: int | None = None, space_after: int | None = None,
                          italic: bool = False):
    """Add a paragraph with optional styling. Returns the paragraph."""
    para = doc.add_paragraph()
    if style:
        para.style = style

    run = para.add_run(text)
    if bold:
        run.bold = True
    if italic:
        run.font.italic = True
    if color:
        run.font.color.rgb = color
    if size:
        run.font.size = Pt(size)
    if space_after is not None:
        para.paragraph_format.space_after = Pt(space_after)

    return para


# ---------------------------------------------------------------------------
# Title block
# ---------------------------------------------------------------------------

def _write_title_block(doc: Document, review, files_reviewed: list[str],
                       project_context: str) -> None:
    """Write the report title and metadata."""
    title = doc.add_heading("Spec Critic — M&P Specification Review Report", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Metadata block (centered)
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.add_run(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    para.add_run(f"Model: {review.model}\n")
    para.add_run(f"Files Reviewed: {len(files_reviewed)}")
    if project_context:
        para.add_run(f"\nProject: {project_context}")


# ---------------------------------------------------------------------------
# Files reviewed
# ---------------------------------------------------------------------------

def _write_files_reviewed(doc: Document, files_reviewed: list[str]) -> None:
    """Write the files reviewed section with a bullet list."""
    doc.add_heading("Files Reviewed", level=1)
    for filename in files_reviewed:
        doc.add_paragraph(filename, style='List Bullet')



# ---------------------------------------------------------------------------
# Methodology note (v1.9.0)
# ---------------------------------------------------------------------------

def _write_methodology_note(doc, cross_check_enabled: bool = False) -> None:
    """Write a brief methodology note explaining how the review was produced.

    Placed after 'Files Reviewed' and before 'Summary' in the report.
    Two short paragraphs: what the tool does, and how verification works.
    If cross-check was enabled, a sentence about that is included.
    """
    doc.add_heading("About This Review", level=1)

    doc.add_paragraph(
        "This report was generated by Spec Critic, an AI-assisted specification "
        "review tool. Each specification was independently analyzed by Claude for "
        "code compliance issues, coordination problems, and technical errors "
        "relevant to California K-12 DSA projects. Findings are classified by "
        "severity (Critical, High, Medium, Gripe) and assigned a confidence score "
        "reflecting the model\u2019s certainty."
    )

    para2_text = (
        "All findings were independently verified "
        "through a second AI pass with web search access, which checks cited "
        "codes and standards against current published requirements. Verification "
        "verdicts (Confirmed, Corrected, Disputed, or Unverified) are shown with "
        "each finding."
    )

    if cross_check_enabled:
        para2_text += (
            " Additionally, a cross-spec coordination check was performed to "
            "identify contradictions between specifications, missing cross-references, "
            "scope gaps and overlaps, and inconsistent equipment data across the "
            "submitted set."
        )

    para2_text += (
        " This report is advisory \u2014 findings should be reviewed by the "
        "engineer of record before acting on them."
    )

    doc.add_paragraph(para2_text)


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _write_summary_table(doc: Document, review, cross_check_result) -> None:
    """Write the summary section with a styled severity counts table."""
    doc.add_heading("Summary", level=1)

    cc_count = (len(cross_check_result.findings)
                if cross_check_result and cross_check_result.findings else 0)

    # Build column definitions
    columns = [
        ("CRITICAL", review.critical_count, "C00000"),
        ("HIGH", review.high_count, "FF6600"),
        ("MEDIUM", review.medium_count, "C09800"),
        ("GRIPES", review.gripe_count, "800080"),
        ("TOTAL", review.total_count, "333333"),
    ]
    if cc_count > 0:
        columns.append(("CROSS-CHECK", cc_count, "06B6D4"))

    # Create table: header row + count row
    table = doc.add_table(rows=2, cols=len(columns))
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # Header row (colored backgrounds with white text)
    for col_idx, (label, _count, hex_color) in enumerate(columns):
        cell = table.rows[0].cells[col_idx]
        _set_cell_shading(cell, hex_color)
        # Clear default paragraph and write header text
        cell.text = ""
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(label)
        run.bold = True
        run.font.size = Pt(9)
        # White text on all dark backgrounds, black on medium yellow
        if label == "MEDIUM":
            run.font.color.rgb = RGBColor(0, 0, 0)
        else:
            run.font.color.rgb = RGBColor(255, 255, 255)

    # Count row
    for col_idx, (_label, count, _hex) in enumerate(columns):
        cell = table.rows[1].cells[col_idx]
        cell.text = ""
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(str(count))
        run.bold = True
        run.font.size = Pt(14)

    doc.add_paragraph()  # Spacer

    # Review-stage token usage only (excludes cross-check + verification)
    para = doc.add_paragraph()
    para.add_run("Review Stage Tokens: ").bold = True
    para.add_run(
        f"{review.input_tokens:,} input → "
        f"{review.output_tokens:,} output"
    )

    para = doc.add_paragraph()
    para.add_run("Note: ").bold = True
    para.add_run(
        "Cross-check and verification token usage are not included in the totals above."
    )

    # Processing time
    para = doc.add_paragraph()
    para.add_run("Processing Time: ").bold = True
    para.add_run(f"{review.elapsed_seconds:.1f} seconds")

    # Verification summary
    all_findings = list(review.findings)
    if cross_check_result and cross_check_result.findings:
        all_findings.extend(cross_check_result.findings)

    verdicts: dict[str, int] = {}
    for f in all_findings:
        if f.verification:
            v = f.verification.verdict
            verdicts[v] = verdicts.get(v, 0) + 1

    if verdicts:
        para = doc.add_paragraph()
        para.add_run("Verification: ").bold = True
        verdict_parts = [
            f"{verdicts[v]} {v.lower()}"
            for v in ["CONFIRMED", "CORRECTED", "DISPUTED", "UNVERIFIED"]
            if v in verdicts
        ]
        para.add_run(", ".join(verdict_parts))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _write_alerts(doc: Document, leed_alerts: list[dict],
                  placeholder_alerts: list[dict]) -> None:
    """Write LEED and placeholder alert sections with bullet lists."""
    if not leed_alerts and not placeholder_alerts:
        return

    doc.add_heading("Alerts", level=1)

    if leed_alerts:
        doc.add_heading("LEED References Detected", level=2)
        _add_styled_paragraph(
            doc,
            "The following LEED references were found. "
            "Since this is not a LEED project, these should be removed:",
            size=10,
            space_after=6,
        )

        by_file: dict[str, list[dict]] = {}
        for alert in leed_alerts:
            by_file.setdefault(alert["filename"], []).append(alert)

        for filename, alerts in by_file.items():
            para = doc.add_paragraph()
            para.add_run(f"{filename}").bold = True
            for alert in alerts[:5]:
                context = alert.get("context", alert.get("match", ""))
                doc.add_paragraph(context, style='List Bullet')
            if len(alerts) > 5:
                doc.add_paragraph(
                    f"... and {len(alerts) - 5} more",
                    style='List Bullet',
                )

    if placeholder_alerts:
        doc.add_heading("Unresolved Placeholders", level=2)
        _add_styled_paragraph(
            doc,
            "The following placeholders need to be resolved:",
            size=10,
            space_after=6,
        )

        by_file: dict[str, list[dict]] = {}
        for alert in placeholder_alerts:
            by_file.setdefault(alert["filename"], []).append(alert)

        for filename, alerts in by_file.items():
            para = doc.add_paragraph()
            para.add_run(f"{filename}").bold = True
            for alert in alerts[:5]:
                context = alert.get("context", alert.get("match", ""))
                doc.add_paragraph(context, style='List Bullet')
            if len(alerts) > 5:
                doc.add_paragraph(
                    f"... and {len(alerts) - 5} more",
                    style='List Bullet',
                )


# ---------------------------------------------------------------------------
# Single finding entry
# ---------------------------------------------------------------------------

def _write_finding_entry(doc: Document, finding, index: int) -> None:
    """Write a single finding with structured labeled rows.

    Layout mirrors the old report style:
        1. [SEVERITY] 92% — filename.docx
        Section: ...
        Issue: ...
        Action: ...
        Existing Text: ... (red)
        Replace With: ... (green)
        Reference: ... (blue)
        Verification: ... (if applicable)
    """
    severity_color = SEVERITY_COLORS.get(finding.severity, RGBColor(0, 0, 0))
    conf_tier = _confidence_tier(finding.confidence)
    conf_color = CONFIDENCE_COLORS[conf_tier]

    # --- Finding header ---
    para = doc.add_paragraph()
    # Index + severity badge
    run = para.add_run(f"{index}. [{finding.severity}] ")
    run.bold = True
    run.font.color.rgb = severity_color
    # Confidence
    run = para.add_run(f"{finding.confidence:.0%} ")
    run.bold = True
    run.font.size = Pt(10)
    run.font.color.rgb = conf_color
    # Separator
    run = para.add_run("— ")
    run.font.color.rgb = RGBColor(128, 128, 128)
    # Filename
    run = para.add_run(finding.fileName or "Unknown")
    run.bold = True

    # --- Section ---
    if finding.section:
        para = doc.add_paragraph()
        para.add_run("Section: ").bold = True
        para.add_run(finding.section)
        para.paragraph_format.space_after = Pt(3)

    # --- Issue ---
    para = doc.add_paragraph()
    para.add_run("Issue: ").bold = True
    para.add_run(finding.issue or "")
    para.paragraph_format.space_after = Pt(3)

    # --- Action type ---
    para = doc.add_paragraph()
    para.add_run("Action: ").bold = True
    para.add_run(finding.actionType or "")
    para.paragraph_format.space_after = Pt(3)

    # --- Existing text (red) ---
    if finding.existingText:
        para = doc.add_paragraph()
        para.add_run("Existing Text: ").bold = True
        run = para.add_run(finding.existingText)
        run.font.color.rgb = RGBColor(192, 0, 0)
        para.paragraph_format.space_after = Pt(3)

    # --- Replacement text (green) ---
    if finding.replacementText:
        para = doc.add_paragraph()
        para.add_run("Replace With: ").bold = True
        run = para.add_run(finding.replacementText)
        run.font.color.rgb = RGBColor(0, 128, 0)
        para.paragraph_format.space_after = Pt(3)

    # --- Code reference (blue) ---
    if finding.codeReference:
        para = doc.add_paragraph()
        para.add_run("Reference: ").bold = True
        run = para.add_run(finding.codeReference)
        run.font.color.rgb = RGBColor(59, 130, 246)
        para.paragraph_format.space_after = Pt(3)

    # --- Verification verdict ---
    if finding.verification:
        vr = finding.verification
        verdict_color = VERDICT_COLORS.get(vr.verdict, VERDICT_COLORS["UNVERIFIED"])
        verdict_icon = VERDICT_ICONS.get(vr.verdict, "—")

        para = doc.add_paragraph()
        run = para.add_run(f"Verification: {verdict_icon} {vr.verdict}")
        run.bold = True
        run.font.color.rgb = verdict_color
        para.paragraph_format.space_after = Pt(3)

        if vr.explanation:
            para = doc.add_paragraph()
            run = para.add_run(vr.explanation)
            run.font.size = Pt(10)
            run.font.color.rgb = RGBColor(100, 100, 100)
            para.paragraph_format.space_after = Pt(3)

        if vr.correction:
            para = doc.add_paragraph()
            para.add_run("Correction: ").bold = True
            run = para.add_run(vr.correction)
            run.font.color.rgb = RGBColor(204, 132, 0)  # Amber
            para.paragraph_format.space_after = Pt(3)

    # Spacer between findings
    doc.add_paragraph()


# ---------------------------------------------------------------------------
# Findings section
# ---------------------------------------------------------------------------

def _write_findings_section(doc: Document, review) -> None:
    """Write per-spec findings grouped by severity, sorted by confidence."""
    doc.add_heading("Findings", level=0)

    if review.total_count == 0:
        _add_styled_paragraph(
            doc,
            "No issues found.",
            size=12,
            color=RGBColor(0, 128, 0),
        )
        return

    finding_number = 0  # Running counter across all severities

    for severity in SEVERITY_ORDER:
        severity_findings = sorted(
            [f for f in review.findings if f.severity == severity],
            key=lambda f: f.confidence,
            reverse=True,
        )
        if not severity_findings:
            continue

        # Severity sub-heading with colored text
        heading = doc.add_heading(
            f"{severity} ({len(severity_findings)})", level=1,
        )
        for run in heading.runs:
            run.font.color.rgb = SEVERITY_COLORS.get(severity, RGBColor(0, 0, 0))

        for finding in severity_findings:
            finding_number += 1
            _write_finding_entry(doc, finding, finding_number)


# ---------------------------------------------------------------------------
# Cross-spec coordination section
# ---------------------------------------------------------------------------

def _write_cross_check_section(doc: Document, cross_check_result) -> None:
    """Write cross-spec coordination findings as a distinct section."""
    if not cross_check_result or not cross_check_result.findings:
        return

    doc.add_page_break()

    heading = doc.add_heading("Cross-Spec Coordination", level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT

    count = len(cross_check_result.findings)
    subtitle = doc.add_paragraph()
    run = subtitle.add_run(
        f"Sonnet 4.6 coordination analysis — "
        f"{count} issue{'s' if count != 1 else ''} found."
    )
    run.font.size = Pt(11)
    run.font.italic = True
    run.font.color.rgb = RGBColor(128, 128, 128)
    subtitle.paragraph_format.space_after = Pt(12)

    # Sort by severity then confidence
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}
    sorted_findings = sorted(
        cross_check_result.findings,
        key=lambda f: (severity_rank.get(f.severity, 99), -f.confidence),
    )

    for idx, finding in enumerate(sorted_findings, 1):
        _write_finding_entry(doc, finding, idx)

    # Coordination summary narrative
    if cross_check_result.thinking:
        doc.add_heading("Coordination Summary", level=2)
        _write_narrative_text(doc, cross_check_result.thinking)


# ---------------------------------------------------------------------------
# Reviewer's notes
# ---------------------------------------------------------------------------

def _write_notes(doc: Document, thinking: str) -> None:
    """Write the reviewer's analysis summary / notes."""
    if not thinking:
        return

    doc.add_page_break()

    heading = doc.add_heading("Reviewer's Notes", level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT

    subtitle = doc.add_paragraph()
    run = subtitle.add_run(
        "Claude's analysis summary — the reviewer's take on these specifications."
    )
    run.font.size = Pt(11)
    run.font.italic = True
    run.font.color.rgb = RGBColor(128, 128, 128)
    subtitle.paragraph_format.space_after = Pt(12)

    _write_narrative_text(doc, thinking)


def _write_narrative_text(doc: Document, text: str) -> None:
    """Write multi-paragraph narrative text, splitting on double newlines."""
    if '\n\n' in text:
        paragraphs = text.split('\n\n')
    else:
        paragraphs = text.split('\n')

    for para_text in paragraphs:
        para_text = para_text.strip()
        if para_text:
            para = doc.add_paragraph()
            run = para.add_run(para_text)
            run.font.size = Pt(11)
            para.paragraph_format.space_after = Pt(8)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_report(
    pipeline_result,
    output_path: Path,
    *,
    project_context: str = "",
) -> Path:
    """Export a complete review report to a Word document.

    Generates a formatted .docx file containing everything the in-app
    report shows: files reviewed, summary grid, alerts, per-spec findings,
    cross-check findings, and reviewer's notes.

    Args:
        pipeline_result: PipelineResult from the review pipeline
        output_path: Path where the .docx file should be saved
        project_context: Optional project description for the title block

    Returns:
        The output_path (for convenience / confirmation)

    Raises:
        ValueError: If pipeline_result has no review_result
        OSError: If the file cannot be written
    """
    if pipeline_result.review_result is None:
        raise ValueError("Cannot export report: no review results available")

    review = pipeline_result.review_result
    cross_check = pipeline_result.cross_check_result

    doc = Document()

    # Set default font (Arial 11pt — clean and professional)
    style = doc.styles['Normal']
    style.font.name = 'Arial'
    style.font.size = Pt(11)

    # Set margins (1 inch sides, 0.75 top/bottom)
    for section in doc.sections:
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)

    # Build the report
    _write_title_block(doc, review, pipeline_result.files_reviewed,
                       project_context)
    
    _write_files_reviewed(doc, pipeline_result.files_reviewed)
    _write_methodology_note(doc, cross_check_enabled=(cross_check is not None))
    _write_summary_table(doc, review, cross_check)
    
    _write_alerts(doc, pipeline_result.leed_alerts,
                  pipeline_result.placeholder_alerts)
    _write_findings_section(doc, review)
    _write_cross_check_section(doc, cross_check)
    _write_notes(doc, review.thinking)

    # Save
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))

    return output_path
