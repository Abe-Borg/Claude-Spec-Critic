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

Collapsible findings (v2.5.0):
    Each finding header uses Word Heading 3 style. In Word 2016+ and
    365, hovering over any heading shows a collapse triangle. Clicking
    it hides everything between that heading and the next heading of
    the same or higher level. This means:
    - Collapse a severity heading (Heading 1) to hide all its findings
    - Collapse a single finding heading (Heading 3) to hide its details
    No macros or special XML required — this is native Word behavior.

Rejection triage (v2.6.0):
    Per-spec findings include a clickable w14 checkbox content control
    in their Heading 3 header. The reviewer opens the exported .docx
    in Word (2013+), checks boxes next to findings they want to reject,
    then uses Spec Critic's 'Process Rejections' feature to produce a
    triaged report with rejected findings moved to an appendix.

Usage:
    from report_exporter import export_report

    export_report(
        pipeline_result=result,
        output_path=Path("review-report.docx"),
        project_context="New 2-story elementary school",
    )
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from lxml import etree


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
# w14 checkbox constants and helpers (rejection triage)
# ---------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

REJECT_TAG_PREFIX = "reject_f"
INSTRUCTIONS_TAG = "sc_rejection_instructions"


def _create_rejection_checkbox(tag_value: str) -> etree._Element:
    """Create a w14 checkbox structured document tag (SDT) element.

    This produces a clickable checkbox in Word 2013+ that toggles between
    ☐ (unchecked) and ☒ (checked) on click. No macros or form protection
    required — w14 content controls are interactive in normal editing mode.

    The tag_value (e.g. 'reject_f001') is embedded in the SDT so the
    report processor can identify which finding each checkbox belongs to.
    """
    xml = (
        f'<w:sdt xmlns:w="{W_NS}" xmlns:w14="{W14_NS}">'
        f'  <w:sdtPr>'
        f'    <w:tag w:val="{tag_value}"/>'
        f'    <w:alias w:val="Reject Finding"/>'
        f'    <w14:checkbox>'
        f'      <w14:checked w14:val="0"/>'
        f'      <w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
        f'      <w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
        f'    </w14:checkbox>'
        f'  </w:sdtPr>'
        f'  <w:sdtContent>'
        f'    <w:r>'
        f'      <w:rPr>'
        f'        <w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" w:hAnsi="MS Gothic"/>'
        f'      </w:rPr>'
        f'      <w:t>\u2610</w:t>'
        f'    </w:r>'
        f'  </w:sdtContent>'
        f'</w:sdt>'
    )
    return etree.fromstring(xml)


def _ensure_w14_namespace(doc: Document) -> None:
    """Ensure the w14 namespace is declared on the document root element.

    Word needs this namespace declaration to recognize the w14:checkbox
    content control. Without it, the checkbox renders as plain text.
    Also adds w14 to mc:Ignorable so older Word versions degrade gracefully.
    """
    etree.register_namespace("w14", W14_NS)
    root = doc.element
    # Add w14 to mc:Ignorable attribute
    mc_ignorable = root.get(f"{{{MC_NS}}}Ignorable", "")
    if "w14" not in mc_ignorable.split():
        root.set(f"{{{MC_NS}}}Ignorable", f"{mc_ignorable} w14".strip())


def _create_instructions_sdt(text: str) -> etree._Element:
    """Create a tagged SDT wrapping instruction text so the processor can find and remove it."""
    xml = (
        f'<w:sdt xmlns:w="{W_NS}">'
        f'  <w:sdtPr>'
        f'    <w:tag w:val="{INSTRUCTIONS_TAG}"/>'
        f'  </w:sdtPr>'
        f'  <w:sdtContent>'
        f'    <w:r>'
        f'      <w:rPr>'
        f'        <w:i/>'
        f'        <w:color w:val="6B7280"/>'
        f'        <w:sz w:val="20"/>'
        f'      </w:rPr>'
        f'      <w:t xml:space="preserve">{text}</w:t>'
        f'    </w:r>'
        f'  </w:sdtContent>'
        f'</w:sdt>'
    )
    return etree.fromstring(xml)


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
                       project_context: str, cycle_label: str = "2025") -> None:
    """Write the report title and metadata.

    Uses separate paragraphs instead of \\n within runs to ensure
    reliable rendering across all Word versions and viewers.
    """
    title = doc.add_heading("Spec Critic — M&P Specification Review Report", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Metadata as separate centered paragraphs (not \n in a single para)
    meta_lines = [
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Model: {review.model}",
        f"Files Reviewed: {len(files_reviewed)}",
        f"Code Cycle: California {cycle_label}",
    ]
    if project_context:
        meta_lines.append(f"Project: {project_context}")

    for line in meta_lines:
        para = doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = para.add_run(line)
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(100, 100, 100)
        para.paragraph_format.space_after = Pt(2)


# ---------------------------------------------------------------------------
# Files reviewed
# ---------------------------------------------------------------------------

def _write_files_reviewed(doc: Document, files_reviewed: list[str]) -> None:
    """Write the files reviewed section with a bullet list."""
    doc.add_heading("Files Reviewed", level=1)
    for filename in files_reviewed:
        doc.add_paragraph(filename, style='List Bullet')



# ---------------------------------------------------------------------------
# Methodology note
# ---------------------------------------------------------------------------

def _summarize_verification_outcomes(findings: list) -> dict[str, object]:
    stats = {
        "total_findings": len(findings),
        "with_verification": 0,
        "verdict_counts": {"CONFIRMED": 0, "CORRECTED": 0, "DISPUTED": 0, "UNVERIFIED": 0},
    }
    for finding in findings:
        verification = getattr(finding, "verification", None)
        if not verification:
            continue
        stats["with_verification"] += 1
        verdict = str(getattr(verification, "verdict", "UNVERIFIED") or "UNVERIFIED").upper()
        if verdict not in stats["verdict_counts"]:
            verdict = "UNVERIFIED"
        stats["verdict_counts"][verdict] += 1
    verified_non_unverified = stats["verdict_counts"]["CONFIRMED"] + stats["verdict_counts"]["CORRECTED"] + stats["verdict_counts"]["DISPUTED"]
    stats["all_unverified"] = stats["with_verification"] > 0 and verified_non_unverified == 0 and stats["verdict_counts"]["UNVERIFIED"] == stats["with_verification"]
    stats["partial_unverified"] = stats["verdict_counts"]["UNVERIFIED"] > 0 and verified_non_unverified > 0
    return stats


def _write_methodology_note(doc, cross_check_enabled: bool = False, cycle_label: str = "2025", cross_check_status: str | None = None, cross_check_reason: str = "", verification_stats: dict[str, object] | None = None) -> None:
    """Write a brief methodology note explaining how the review was produced."""
    doc.add_heading("About This Review", level=1)

    doc.add_paragraph(
        "This report was generated by Spec Critic, an AI-assisted specification "
        "review tool. Each specification was analyzed by Claude for "
        "code compliance issues, coordination problems, and technical errors "
        "relevant to California K-12 DSA projects. Findings are classified by "
        "severity (Critical, High, Medium, Gripe) and assigned a confidence score "
        "reflecting the model\u2019s certainty."
    )

    verification_stats = verification_stats or {}
    all_unverified = bool(verification_stats.get("all_unverified", False))
    partial_unverified = bool(verification_stats.get("partial_unverified", False))
    with_verification = int(verification_stats.get("with_verification", 0) or 0)

    if all_unverified:
        para2_text = (
            "Verification was attempted but did not return usable results. "
            "Findings have not been independently verified."
        )
    elif partial_unverified:
        para2_text = (
            "Findings were checked in a secondary AI verification pass with web search access. "
            "Some findings could not be verified — see individual verdicts."
        )
    elif with_verification > 0:
        para2_text = (
            "All findings were checked in a secondary AI verification pass with web search access. "
            "Verification verdicts (Confirmed, Corrected, Disputed, or Unverified) reflect the verifier model's assessment and should be treated as advisory."
        )
    else:
        para2_text = (
            "No verification outcomes were recorded for this run. "
            "Findings should be treated as unverified unless noted otherwise."
        )

    para2_text += f" This review used California {cycle_label} code cycle references."

    if cross_check_enabled and cross_check_status == "completed":
        para2_text += " Cross-check completed."
    elif cross_check_enabled and cross_check_status == "skipped":
        para2_text += f" Cross-check was skipped: {cross_check_reason}"
    elif cross_check_enabled and cross_check_status == "failed":
        para2_text += f" Cross-check failed: {cross_check_reason}"

    para2_text += (
        " This report is advisory \u2014 findings should be reviewed by the "
        "engineer of record before acting on them."
    )

    doc.add_paragraph(para2_text)

    # Collapsibility tip
    doc.add_paragraph(
        "Tip: In Word, hover over any heading to reveal a collapse triangle. "
        "Click it to hide the content beneath that heading. Use this to "
        "collapse individual findings or entire severity groups."
    )


# ---------------------------------------------------------------------------
# Rejection triage instructions
# ---------------------------------------------------------------------------

def _write_rejection_instructions(doc: Document) -> None:
    """Write a triage instruction paragraph with a tagged SDT for later removal.

    The instruction text is wrapped in a structured document tag (SDT)
    with the INSTRUCTIONS_TAG so the report processor can find and remove
    it after triage is complete.
    """
    para = doc.add_paragraph()
    para.paragraph_format.space_before = Pt(6)
    para.paragraph_format.space_after = Pt(12)

    # Insert the tagged SDT into the paragraph
    instruction_text = (
        "Rejection Triage: Each per-spec finding below has a clickable "
        "checkbox (\u2610). Check the box next to any finding you want to "
        "reject, then use Spec Critic\u2019s \u2018Process Rejections\u2019 "
        "feature to produce a clean report with rejected findings moved to "
        "an appendix. (This instruction is automatically removed during processing.)"
    )
    sdt = _create_instructions_sdt(instruction_text)
    para._p.append(sdt)


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _write_summary_table(doc: Document, review, cross_check_result, *, total_elapsed_seconds: float | None = None) -> None:
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
    processing_seconds = total_elapsed_seconds if total_elapsed_seconds is not None else review.elapsed_seconds
    para.add_run(f"{processing_seconds:.1f} seconds")

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
# Single finding entry (collapsible via Heading 3)
# ---------------------------------------------------------------------------

def _write_finding_entry(doc: Document, finding, index: int, verbose: bool = True, *,
                         include_checkbox: bool = False) -> None:
    """Write a single finding as a collapsible block.

    The finding header is rendered as a Heading 3 paragraph, which enables
    Word's native heading-collapse feature. Users can click the collapse
    triangle that appears on hover to hide the finding's body content.

    When include_checkbox is True, a w14 checkbox content control is
    prepended to the heading paragraph. This checkbox is used for
    rejection triage — the reviewer checks it in Word, then processes
    the report through Spec Critic to move rejected findings to an appendix.

    Layout:
        Heading 3: [☐] [SEVERITY] 92% — filename.docx — Section ref
        Normal:    Issue: ...
        Normal:    Action: ...
        Normal:    Existing Text: ... (red)
        Normal:    Replace With: ... (green)
        Normal:    Reference: ... (blue)
        Normal:    Verification: ... (if applicable)
    """
    severity_color = SEVERITY_COLORS.get(finding.severity, RGBColor(0, 0, 0))
    conf_tier = _confidence_tier(finding.confidence)
    conf_color = CONFIDENCE_COLORS[conf_tier]

    # --- Finding header as Heading 3 (enables Word collapse) ---
    para = doc.add_paragraph()
    para.style = doc.styles['Heading 3']
    para.paragraph_format.space_before = Pt(12)
    para.paragraph_format.space_after = Pt(4)

    # --- Rejection checkbox (w14 content control) ---
    if include_checkbox:
        tag = f"{REJECT_TAG_PREFIX}{index:03d}"
        checkbox_sdt = _create_rejection_checkbox(tag)
        # Insert checkbox at the beginning of the paragraph XML
        para._p.insert(0, checkbox_sdt)
        # Add a space run after the checkbox for visual separation
        spacer_run = para.add_run(" ")
        spacer_run.font.size = Pt(11)

    # Index + severity badge
    run = para.add_run(f"{index}. [{finding.severity}] ")
    run.bold = True
    run.font.color.rgb = severity_color
    run.font.size = Pt(11)
    # Confidence
    run = para.add_run(f"{finding.confidence:.0%} ")
    run.bold = True
    run.font.size = Pt(11)
    run.font.color.rgb = conf_color
    # Separator
    run = para.add_run("— ")
    run.font.color.rgb = RGBColor(128, 128, 128)
    run.font.size = Pt(11)
    # Filename
    run = para.add_run(finding.fileName or "Unknown")
    run.bold = True
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0, 0, 0)
    # Section (inline in header for compact view)
    if finding.section:
        run = para.add_run(f" — {finding.section}")
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(100, 100, 100)

    # --- Body content (Normal paragraphs, hidden when heading is collapsed) ---

    # --- Issue ---
    if verbose:
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
    if verbose and finding.codeReference:
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

        if verbose and vr.explanation:
            para = doc.add_paragraph()
            run = para.add_run(vr.explanation)
            run.font.size = Pt(10)
            run.font.color.rgb = RGBColor(100, 100, 100)
            para.paragraph_format.space_after = Pt(3)

        if vr.verdict == "CORRECTED" and vr.correction:
            para = doc.add_paragraph()
            para.add_run("Correction: ").bold = True
            run = para.add_run(vr.correction)
            run.font.color.rgb = RGBColor(204, 132, 0)  # Amber
            para.paragraph_format.space_after = Pt(3)


# ---------------------------------------------------------------------------
# Findings section
# ---------------------------------------------------------------------------

def _write_findings_section(doc: Document, review, verbose: bool = True) -> None:
    """Write per-spec findings grouped by file name.

    Uses heading hierarchy for Word-native collapse support:
    - Title (level 0): "Findings"
    - Heading 1: Severity group (e.g., "CRITICAL (1)")
    - Heading 3: Individual finding header (collapsible)
    - Normal: Finding body content

    Per-spec findings include rejection checkboxes for triage.
    """
    doc.add_heading("Findings", level=0)

    if review.total_count == 0:
        _add_styled_paragraph(
            doc,
            "No issues found.",
            size=12,
            color=RGBColor(0, 128, 0),
        )
        return

    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}
    by_file: dict[str, list] = defaultdict(list)
    for f in review.findings:
        by_file[f.fileName or "Unknown"].append(f)

    finding_number = 0

    for filename in sorted(by_file.keys(), key=str.casefold):
        file_findings = sorted(
            by_file[filename],
            key=lambda f: (severity_rank.get(f.severity, 99), -f.confidence),
        )
        doc.add_heading(filename, level=1)

        for finding in file_findings:
            finding_number += 1
            _write_finding_entry(
                doc, finding, finding_number,
                verbose=verbose,
                include_checkbox=True,
            )


# ---------------------------------------------------------------------------
# Cross-spec coordination section
# ---------------------------------------------------------------------------

def _write_cross_check_section(doc: Document, cross_check_result, verbose: bool = True) -> None:
    """Write cross-spec coordination section and explicit status.

    Cross-check findings do NOT include rejection checkboxes —
    only per-spec findings are rejectable.
    """
    if not cross_check_result:
        return

    doc.add_page_break()

    heading = doc.add_heading("Cross-Spec Coordination", level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT

    status = getattr(cross_check_result, "cross_check_status", None)
    count = len(cross_check_result.findings)
    subtitle = doc.add_paragraph()
    if status == "skipped":
        run = subtitle.add_run(f"Cross-check was skipped: {cross_check_result.thinking}")
    elif status == "failed":
        run = subtitle.add_run(f"Cross-check failed: {cross_check_result.error}")
    elif status == "completed" and count == 0:
        run = subtitle.add_run("Cross-check completed — no coordination issues found.")
    else:
        run = subtitle.add_run(
            f"Opus 4.6 coordination analysis — "
            f"{count} issue{'s' if count != 1 else ''} found."
        )
    run.font.size = Pt(11)
    run.font.italic = True
    run.font.color.rgb = RGBColor(128, 128, 128)
    subtitle.paragraph_format.space_after = Pt(12)

    if status in ("skipped", "failed") or count == 0:
        return

    # Sort by severity then confidence
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}
    sorted_findings = sorted(
        cross_check_result.findings,
        key=lambda f: (severity_rank.get(f.severity, 99), -f.confidence),
    )

    for idx, finding in enumerate(sorted_findings, 1):
        _write_finding_entry(doc, finding, idx, verbose=verbose, include_checkbox=False)

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


def _sanitize_markdown_line(line: str) -> str:
    """Strip markdown header prefixes from a line."""
    stripped = line
    while stripped.startswith('#'):
        stripped = stripped[1:]
    return stripped.strip() if line.startswith('#') else line


def _write_narrative_text(doc: Document, text: str) -> None:
    """Write multi-paragraph narrative text, splitting on double newlines.

    Strips markdown header formatting (## ...) since the cross-check
    prompt sometimes produces markdown despite instructions not to.
    """
    if '\n\n' in text:
        paragraphs = text.split('\n\n')
    else:
        paragraphs = text.split('\n')

    for para_text in paragraphs:
        para_text = _sanitize_markdown_line(para_text.strip())
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
    verbose: bool = True,
) -> Path:
    """Export a complete review report to a Word document.

    Generates a formatted .docx file containing everything the in-app
    report shows: files reviewed, summary grid, alerts, per-spec findings,
    cross-check findings, and reviewer's notes.

    Each per-spec finding uses Heading 3 for its header line with a w14
    checkbox content control, enabling both Word's native heading-collapse
    feature and rejection triage via the 'Process Rejections' workflow.

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
    all_findings = list(review.findings)
    if cross_check and cross_check.findings:
        all_findings.extend(cross_check.findings)
    verification_stats = _summarize_verification_outcomes(all_findings)

    doc = Document()

    # Set default font (Arial 11pt — clean and professional)
    style = doc.styles['Normal']
    style.font.name = 'Arial'
    style.font.size = Pt(11)

    # Configure Heading 3 style for finding entries
    # Keep it compact so findings don't dominate vertical space
    h3_style = doc.styles['Heading 3']
    h3_style.font.name = 'Arial'
    h3_style.font.size = Pt(11)
    h3_style.paragraph_format.space_before = Pt(12)
    h3_style.paragraph_format.space_after = Pt(4)
    # Remove the default Heading 3 color so our per-run colors show through
    h3_style.font.color.rgb = RGBColor(0, 0, 0)

    # Set margins (1 inch sides, 0.75 top/bottom)
    for section in doc.sections:
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)

    # Build the report
    cycle_label = getattr(pipeline_result, "cycle_label", "2025") or "2025"
    _write_title_block(
        doc,
        review,
        pipeline_result.files_reviewed,
        project_context,
        cycle_label=cycle_label,
    )
    
    _write_files_reviewed(doc, pipeline_result.files_reviewed)
    cross_check_status = getattr(cross_check, "cross_check_status", None) if cross_check else None
    cross_check_reason = ""
    if cross_check and cross_check_status == "skipped":
        cross_check_reason = cross_check.thinking or ""
    elif cross_check and cross_check_status == "failed":
        cross_check_reason = cross_check.error or ""
    _write_methodology_note(
        doc,
        cross_check_enabled=(cross_check is not None),
        cycle_label=cycle_label,
        cross_check_status=cross_check_status,
        cross_check_reason=cross_check_reason,
        verification_stats=verification_stats,
    )
    _write_summary_table(
        doc,
        review,
        cross_check,
        total_elapsed_seconds=getattr(pipeline_result, "total_elapsed_seconds", None),
    )

    # Rejection triage instructions (before findings)
    if review.total_count > 0:
        _write_rejection_instructions(doc)

    _write_alerts(doc, pipeline_result.leed_alerts,
                  pipeline_result.placeholder_alerts)
    _write_findings_section(doc, review, verbose=verbose)
    _write_cross_check_section(doc, cross_check, verbose=verbose)
    _write_notes(doc, review.thinking)

    # Ensure w14 namespace is declared for checkbox content controls
    _ensure_w14_namespace(doc)

    # Save
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))

    return output_path