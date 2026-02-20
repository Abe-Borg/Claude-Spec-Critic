"""
Word document report exporter for Spec Critic.

Generates a formatted .docx report from a PipelineResult, replicating
everything the in-app ReportPanel / ReportWindow shows:
    - Title block with generation metadata
    - Summary table (severity counts)
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
    - Color-coded severity via table cell shading (matching app colors)
    - Verification verdicts shown inline beneath each finding
    - The exporter is stateless — one function call, one file written
    - No GUI dependencies — can be called from any context

v1.8.0 — Initial implementation.

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
from docx.shared import Inches, Pt, RGBColor, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn


# ---------------------------------------------------------------------------
# Color constants (match the app's COLORS dict as closely as possible)
# ---------------------------------------------------------------------------

_SEVERITY_RGB = {
    "CRITICAL": RGBColor(0xDC, 0x26, 0x26),  # #DC2626
    "HIGH":     RGBColor(0xF9, 0x73, 0x16),  # #F97316
    "MEDIUM":   RGBColor(0xEA, 0xB3, 0x08),  # #EAB308
    "GRIPES":   RGBColor(0xA8, 0x55, 0xF7),  # #A855F7
}

_SEVERITY_SHADING = {
    "CRITICAL": "DC2626",
    "HIGH":     "F97316",
    "MEDIUM":   "EAB308",
    "GRIPES":   "A855F7",
}

_VERDICT_RGB = {
    "CONFIRMED":  RGBColor(0x22, 0xC5, 0x5E),  # green
    "CORRECTED":  RGBColor(0xF5, 0x9E, 0x0B),  # amber
    "UNVERIFIED": RGBColor(0x6B, 0x72, 0x80),  # gray
    "DISPUTED":   RGBColor(0xEF, 0x44, 0x44),  # red
}

_CONFIDENCE_RGB = {
    "high":     RGBColor(0x22, 0xC5, 0x5E),
    "moderate": RGBColor(0xF5, 0x9E, 0x0B),
    "low":      RGBColor(0xEF, 0x44, 0x44),
}

_COORDINATION_RGB = RGBColor(0x06, 0xB6, 0xD4)  # cyan


def _confidence_tier(confidence: float) -> str:
    """Return 'high', 'moderate', or 'low' for a confidence score."""
    if confidence >= 0.85:
        return "high"
    elif confidence >= 0.60:
        return "moderate"
    return "low"


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------

def _set_cell_shading(cell, hex_color: str) -> None:
    """Apply background shading to a table cell.

    Args:
        cell: A python-docx table cell object
        hex_color: 6-character hex color string (no #)
    """
    shading = cell._element.get_or_add_tcPr()
    shd = shading.makeelement(
        qn("w:shd"),
        {
            qn("w:val"): "clear",
            qn("w:color"): "auto",
            qn("w:fill"): hex_color,
        },
    )
    shading.append(shd)


def _add_run(paragraph, text: str, *, bold: bool = False, italic: bool = False,
             size: int = 10, color: RGBColor | None = None, font: str = "Calibri") -> None:
    """Add a formatted text run to a paragraph.

    Args:
        paragraph: A python-docx Paragraph object
        text: Text content
        bold: Bold flag
        italic: Italic flag
        size: Font size in points
        color: Optional RGBColor
        font: Font name
    """
    run = paragraph.add_run(text)
    run.font.size = Pt(size)
    run.font.name = font
    run.font.bold = bold
    run.font.italic = italic
    if color:
        run.font.color.rgb = color


def _add_paragraph(doc_or_container, text: str = "", *, style: str | None = None,
                   bold: bool = False, size: int = 10, color: RGBColor | None = None,
                   space_before: int = 0, space_after: int = 60,
                   alignment=None, font: str = "Calibri"):
    """Add a paragraph with a single formatted run.

    Returns the paragraph for further modification.
    """
    p = doc_or_container.add_paragraph(style=style)
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    if alignment:
        p.alignment = alignment
    if text:
        _add_run(p, text, bold=bold, size=size, color=color, font=font)
    return p


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _write_title_block(doc: Document, review, files_reviewed: list[str],
                       project_context: str, cross_check_result) -> None:
    """Write the report title and metadata."""
    _add_paragraph(doc, "Spec Critic Report", bold=True, size=20,
                   space_before=0, space_after=4)
    _add_paragraph(doc, "M&P Specification Review  •  California K-12 DSA",
                   size=10, color=RGBColor(0x70, 0x70, 0x70), space_after=12)

    meta_lines = [
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Model: {review.model}",
        f"Files: {len(files_reviewed)}",
        f"Tokens: {review.input_tokens:,} in → {review.output_tokens:,} out",
        f"Time: {review.elapsed_seconds:.1f}s",
    ]
    if project_context:
        meta_lines.append(f"Project: {project_context}")

    _add_paragraph(doc, "  •  ".join(meta_lines),
                   size=9, color=RGBColor(0x70, 0x70, 0x70), space_after=12)


def _write_summary_table(doc: Document, review, cross_check_result) -> None:
    """Write the severity counts summary table."""
    _add_paragraph(doc, "SUMMARY", bold=True, size=12, space_before=6, space_after=6)

    cc_count = len(cross_check_result.findings) if cross_check_result and cross_check_result.findings else 0

    columns = [
        ("Critical", review.critical_count, "DC2626"),
        ("High", review.high_count, "F97316"),
        ("Medium", review.medium_count, "EAB308"),
        ("Gripes", review.gripe_count, "A855F7"),
        ("Total", review.total_count, "333333"),
    ]
    if cc_count > 0:
        columns.append(("Cross-Check", cc_count, "06B6D4"))

    table = doc.add_table(rows=2, cols=len(columns))
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = True

    # Header row
    for col_idx, (label, _count, hex_color) in enumerate(columns):
        cell = table.rows[0].cells[col_idx]
        _set_cell_shading(cell, hex_color)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        # Use white text for darker backgrounds, black for medium yellow
        text_color = RGBColor(0x00, 0x00, 0x00) if label == "Medium" else RGBColor(0xFF, 0xFF, 0xFF)
        _add_run(p, label.upper(), bold=True, size=9, color=text_color)

    # Count row
    for col_idx, (_label, count, _hex_color) in enumerate(columns):
        cell = table.rows[1].cells[col_idx]
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_run(p, str(count), bold=True, size=14)

    # Verification summary line
    all_findings_for_verdicts = list(review.findings)
    if cross_check_result and cross_check_result.findings:
        all_findings_for_verdicts.extend(cross_check_result.findings)

    verdicts: dict[str, int] = {}
    for f in all_findings_for_verdicts:
        if f.verification and f.verification.verdict != "UNVERIFIED":
            v = f.verification.verdict
            verdicts[v] = verdicts.get(v, 0) + 1

    if verdicts:
        verdict_parts = [
            f"{verdicts[v]} {v.lower()}"
            for v in ["CONFIRMED", "CORRECTED", "DISPUTED"]
            if v in verdicts
        ]
        _add_paragraph(doc, f"Verification: {', '.join(verdict_parts)}",
                       size=9, color=RGBColor(0x70, 0x70, 0x70),
                       space_before=4, space_after=6)

    doc.add_paragraph()  # spacer


def _write_alerts(doc: Document, leed_alerts: list[dict],
                  placeholder_alerts: list[dict]) -> None:
    """Write LEED and placeholder alert sections."""
    if not leed_alerts and not placeholder_alerts:
        return

    _add_paragraph(doc, "ALERTS", bold=True, size=12, space_before=6, space_after=6)

    for label, alerts in [("LEED References Detected", leed_alerts),
                          ("Unresolved Placeholders", placeholder_alerts)]:
        if not alerts:
            continue

        _add_paragraph(doc, label, bold=True, size=11,
                       color=RGBColor(0xF5, 0x9E, 0x0B), space_after=4)

        by_file: dict[str, list[dict]] = {}
        for a in alerts:
            by_file.setdefault(a["filename"], []).append(a)

        for fname, file_alerts in by_file.items():
            p = _add_paragraph(doc, "", space_after=2)
            _add_run(p, f"{fname}: ", bold=True, size=10)
            _add_run(p, f"{len(file_alerts)} found", size=9,
                     color=RGBColor(0x70, 0x70, 0x70))

    doc.add_paragraph()  # spacer


def _write_finding(doc: Document, finding, index: int) -> None:
    """Write a single finding entry.

    Each finding is a small table: one header row (severity badge + location)
    and detail rows for the issue, existing/replacement text, code reference,
    and verification verdict.
    """
    sev = finding.severity
    sev_hex = _SEVERITY_SHADING.get(sev, "333333")
    conf_tier = _confidence_tier(finding.confidence)
    conf_color = _CONFIDENCE_RGB[conf_tier]

    # --- Header line ---
    p = _add_paragraph(doc, "", space_before=6, space_after=2)
    _add_run(p, f"  {sev}  ", bold=True, size=9,
             color=RGBColor(0x00, 0x00, 0x00) if sev == "MEDIUM" else RGBColor(0xFF, 0xFF, 0xFF))
    # Severity background via a highlight (approximation — python-docx doesn't
    # have direct inline background, so we use the approach of writing the badge
    # as text and relying on the section header color for grouping)
    _add_run(p, f"  {finding.confidence:.0%}", bold=True, size=9, color=conf_color)
    _add_run(p, f"  •  ", size=9, color=RGBColor(0x70, 0x70, 0x70))
    _add_run(p, finding.fileName or "Unknown", bold=True, size=10)
    if finding.section:
        _add_run(p, f"  •  {finding.section}", size=9,
                 color=RGBColor(0x70, 0x70, 0x70))

    # --- Issue description ---
    _add_paragraph(doc, finding.issue or "", size=10, space_after=2)

    # --- Existing text ---
    if finding.existingText:
        p = _add_paragraph(doc, "", space_after=2)
        _add_run(p, "Existing: ", bold=True, size=9, color=RGBColor(0x70, 0x70, 0x70))
        _add_run(p, finding.existingText, size=9, color=RGBColor(0xDC, 0x26, 0x26),
                 font="Consolas")

    # --- Replacement text ---
    if finding.replacementText:
        p = _add_paragraph(doc, "", space_after=2)
        _add_run(p, "Replace with: ", bold=True, size=9,
                 color=RGBColor(0x70, 0x70, 0x70))
        _add_run(p, finding.replacementText, size=9, color=RGBColor(0x22, 0xC5, 0x5E),
                 font="Consolas")

    # --- Code reference ---
    if finding.codeReference:
        _add_paragraph(doc, f"Reference: {finding.codeReference}",
                       size=9, color=RGBColor(0x3B, 0x82, 0xF6), space_after=2)

    # --- Verification verdict ---
    if finding.verification and finding.verification.verdict != "UNVERIFIED":
        vr = finding.verification
        verdict_color = _VERDICT_RGB.get(vr.verdict, _VERDICT_RGB["UNVERIFIED"])
        verdict_icon = {
            "CONFIRMED": "✓", "CORRECTED": "✎", "DISPUTED": "✗"
        }.get(vr.verdict, "—")

        p = _add_paragraph(doc, "", space_after=2)
        _add_run(p, f"{verdict_icon} {vr.verdict}", bold=True, size=9,
                 color=verdict_color)
        if vr.explanation:
            _add_run(p, f"  — {vr.explanation}", size=9,
                     color=RGBColor(0x70, 0x70, 0x70))

        if vr.correction:
            p2 = _add_paragraph(doc, "", space_after=2)
            _add_run(p2, "Correction: ", bold=True, size=9,
                     color=RGBColor(0xF5, 0x9E, 0x0B))
            _add_run(p2, vr.correction, size=9, color=RGBColor(0xF5, 0x9E, 0x0B),
                     font="Consolas")

    # Thin separator line after each finding
    p_sep = doc.add_paragraph()
    p_sep.paragraph_format.space_before = Pt(2)
    p_sep.paragraph_format.space_after = Pt(2)
    # Add a bottom border to simulate a thin rule
    pPr = p_sep._element.get_or_add_pPr()
    pBdr = pPr.makeelement(qn("w:pBdr"), {})
    bottom = pBdr.makeelement(
        qn("w:bottom"),
        {
            qn("w:val"): "single",
            qn("w:sz"): "4",
            qn("w:space"): "1",
            qn("w:color"): "DDDDDD",
        },
    )
    pBdr.append(bottom)
    pPr.append(pBdr)


def _write_findings_section(doc: Document, review) -> None:
    """Write per-spec findings grouped by severity, sorted by confidence."""
    _add_paragraph(doc, "FINDINGS", bold=True, size=12, space_before=6, space_after=6)

    if review.total_count == 0:
        _add_paragraph(doc, "✓ No issues found", size=12,
                       color=RGBColor(0x22, 0xC5, 0x5E))
        return

    severity_order = ["CRITICAL", "HIGH", "MEDIUM", "GRIPES"]
    finding_index = 0

    for sev in severity_order:
        sev_findings = sorted(
            [f for f in review.findings if f.severity == sev],
            key=lambda f: f.confidence,
            reverse=True,
        )
        if not sev_findings:
            continue

        sev_color = _SEVERITY_RGB.get(sev, RGBColor(0x70, 0x70, 0x70))
        _add_paragraph(doc, f"{sev} ({len(sev_findings)})", bold=True, size=11,
                       color=sev_color, space_before=8, space_after=4)

        for f in sev_findings:
            finding_index += 1
            _write_finding(doc, f, finding_index)


def _write_cross_check_section(doc: Document, cross_check_result) -> None:
    """Write cross-spec coordination findings."""
    if not cross_check_result or not cross_check_result.findings:
        return

    _add_paragraph(doc, "CROSS-SPEC COORDINATION", bold=True, size=12,
                   color=_COORDINATION_RGB, space_before=12, space_after=4)

    count = len(cross_check_result.findings)
    _add_paragraph(
        doc,
        f"{count} coordination issue{'s' if count != 1 else ''} found (Sonnet 4.6)",
        size=9, color=RGBColor(0x70, 0x70, 0x70), space_after=6,
    )

    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}
    sorted_findings = sorted(
        cross_check_result.findings,
        key=lambda f: (severity_rank.get(f.severity, 99), -f.confidence),
    )

    for idx, f in enumerate(sorted_findings, start=1):
        _write_finding(doc, f, idx)

    # Cross-check narrative summary
    if cross_check_result.thinking:
        _add_paragraph(doc, "Coordination Summary", bold=True, size=10,
                       space_before=8, space_after=4)
        _add_paragraph(doc, cross_check_result.thinking, size=10,
                       color=RGBColor(0x50, 0x50, 0x50))


def _write_notes(doc: Document, thinking: str) -> None:
    """Write the reviewer's analysis summary / notes."""
    if not thinking:
        return

    _add_paragraph(doc, "REVIEWER'S NOTES", bold=True, size=12,
                   space_before=12, space_after=6)
    _add_paragraph(doc, thinking, size=10, color=RGBColor(0x50, 0x50, 0x50))


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
    report shows: summary grid, alerts, per-spec findings, cross-check
    findings, and reviewer's notes.

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

    # Set default font for the document
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10)
    style.paragraph_format.space_after = Pt(4)

    # Set narrow margins for more content per page
    for section in doc.sections:
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin = Inches(0.8)
        section.right_margin = Inches(0.8)

    # Build the report
    _write_title_block(doc, review, pipeline_result.files_reviewed,
                       project_context, cross_check)
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
