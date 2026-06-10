"""
Word document report exporter for Spec Critic.

Generates a formatted .docx report from a PipelineResult:
    - Title block with generation metadata
    - Files reviewed list
    - Summary table (severity counts) with colored cell shading
    - LEED and placeholder alerts
    - Per-spec findings grouped by severity, then by spec file, then confidence
    - Verification verdicts and corrections inline with findings
    - Cross-spec coordination findings (if cross-check was enabled)

Collapsible findings (v2.5.0):
    Each finding header uses Word Heading 3 style. In Word 2016+ and
    365, hovering over any heading shows a collapse triangle. Clicking
    it hides everything between that heading and the next heading of
    the same or higher level. This means:
    - Collapse a severity heading (Heading 1) to hide all its findings
    - Collapse a single finding heading (Heading 3) to hide its details
    No macros or special XML required — this is native Word behavior.

Usage:
    from report_exporter import export_report

    export_report(
        pipeline_result=result,
        output_path=Path("review-report.docx"),
    )
"""

from __future__ import annotations

import time
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

from ..core.api_config import web_search_max_uses_for_severity
from ..core.code_cycles import AVAILABLE_CYCLES, DEFAULT_CYCLE, CodeCycle
from ..verification.verification_cache import default_cache_path
from .report_status import (
    EDIT_ACTION_DISPLAY_ORDER,
    EditActionLabel,
    ReportStatus,
    STATUS_DISPLAY_ORDER,
    classify_edit_action,
    classify_status,
    edit_action_label,
    is_budget_exhausted,
    status_glyph,
    status_label,
    summarize_budget_exhausted,
    summarize_edit_actions,
    summarize_statuses,
    verdict_supersedes_confidence,
)


# ---------------------------------------------------------------------------
# Evidence panel labels
# ---------------------------------------------------------------------------

# Human-readable labels for the verification-mode strings stamped on
# VerificationResult.verification_mode. Keys match VerificationMode.value
# (lowercase string form); unknown modes fall back to a Title-cased
# version of the raw string so a future mode does not render as blank.
_VERIFICATION_MODE_LABELS: dict[str, str] = {
    "local_skip": "Local skip",
    "strict_structured": "Strict structured",
    "standard_reasoning": "Standard reasoning",
    "deep_reasoning": "Deep reasoning",
}


def _verification_mode_label(mode: str) -> str:
    """Map a raw VerificationMode string to a display label."""
    mode = (mode or "").strip().lower()
    if not mode:
        return ""
    return _VERIFICATION_MODE_LABELS.get(mode, mode.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Color constants
# ---------------------------------------------------------------------------

SEVERITY_COLORS = {
    "CRITICAL": RGBColor(192, 0, 0),      # Dark red
    "HIGH": RGBColor(255, 102, 0),         # Orange
    "MEDIUM": RGBColor(192, 152, 0),       # Dark yellow/gold
    "GRIPES": RGBColor(128, 0, 128),       # Purple
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

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "GRIPES"]

# Closed-set status display colors. To avoid presenting all findings as
# equally certain, each status
# gets a distinct color so a quick scroll of the report makes the
# evidence picture visible. Status color hex strings double as the
# summary-table cell shading values.
STATUS_COLORS: dict[ReportStatus, RGBColor] = {
    ReportStatus.VERIFIED_SUPPORTED: RGBColor(0, 128, 0),          # Green
    ReportStatus.VERIFIED_CONTRADICTED: RGBColor(204, 132, 0),     # Amber
    ReportStatus.DISPUTED: RGBColor(192, 0, 0),                    # Red
    ReportStatus.INSUFFICIENT_EVIDENCE: RGBColor(128, 128, 128),   # Gray
    ReportStatus.LOCALLY_CLASSIFIED: RGBColor(59, 130, 246),       # Blue
    ReportStatus.NOT_CHECKED: RGBColor(100, 100, 100),             # Dark gray
    ReportStatus.MANUAL_REVIEW_REQUIRED: RGBColor(255, 102, 0),    # Orange
    # Dark red-orange, distinct from the red
    # used for DISPUTED (C00000) and the orange used for
    # MANUAL_REVIEW_REQUIRED (FF6600). Operational failures need a
    # visually distinct treatment so a quick scroll of the report shows
    # which findings need re-verification vs. those the verifier ran
    # cleanly on.
    ReportStatus.VERIFICATION_FAILED: RGBColor(178, 34, 34),       # Firebrick / dark red-orange
    # Purple, distinct from every other
    # status color above. Indicates "two verifiers, different verdicts"
    # — a quality signal that survives the verdict-based rendering so
    # the report can flag the disagreement without burying it inside a
    # nominally-supported (green) finding.
    ReportStatus.VERIFIED_CONTESTED: RGBColor(128, 0, 128),        # Purple
}

STATUS_SHADING: dict[ReportStatus, str] = {
    ReportStatus.VERIFIED_SUPPORTED: "008000",
    ReportStatus.VERIFIED_CONTRADICTED: "CC8400",
    ReportStatus.DISPUTED: "C00000",
    ReportStatus.INSUFFICIENT_EVIDENCE: "808080",
    ReportStatus.LOCALLY_CLASSIFIED: "3B82F6",
    ReportStatus.NOT_CHECKED: "646464",
    ReportStatus.MANUAL_REVIEW_REQUIRED: "FF6600",
    ReportStatus.VERIFICATION_FAILED: "B22222",
    ReportStatus.VERIFIED_CONTESTED: "800080",
}

EDIT_ACTION_COLORS: dict[EditActionLabel, RGBColor] = {
    EditActionLabel.EDIT_SUGGESTED: RGBColor(0, 128, 0),            # Green
    EditActionLabel.REPORT_ONLY: RGBColor(100, 100, 100),           # Gray
}


# Cache-age badge color tiers. Cache replays carry
# evidence that may have drifted since the original verdict was produced;
# the badge color signals "how stale is this verdict?" at a glance.
#   < 30 days  → amber  (recent — likely still accurate)
#   30-90 days → orange (worth a second look on high-stakes findings)
#   > 90 days  → red    (likely stale; consider re-verifying)
CACHE_AGE_COLORS: dict[str, RGBColor] = {
    "fresh": RGBColor(204, 132, 0),       # Amber
    "stale": RGBColor(255, 102, 0),       # Orange
    "very_stale": RGBColor(192, 0, 0),    # Red
}


def _cache_age_tier(age_days: int) -> str:
    """Bucket a cache entry's age in days into the badge-color tier."""
    if age_days < 30:
        return "fresh"
    if age_days <= 90:
        return "stale"
    return "very_stale"


def _cache_entry_age_days(verification) -> int | None:
    """Return the age in days of the cache entry behind a cache-hit result.

    Returns ``None`` for non-hit results, for results without a recorded
    ``cache_entry_created_ts`` (legacy resume payloads that predate the badge),
    or for timestamps in the future (clock-skew anomaly). The caller
    suppresses the badge in those cases.
    """
    if verification is None:
        return None
    if (getattr(verification, "cache_status", "") or "") != "hit":
        return None
    created_ts = float(getattr(verification, "cache_entry_created_ts", 0.0) or 0.0)
    if created_ts <= 0.0:
        return None
    age_seconds = time.time() - created_ts
    if age_seconds < 0:
        return None
    return int(age_seconds // 86400)


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


# Word 2012 extension namespace — NOT the base w: namespace.
# Per [MS-DOCX] §2.5.1.3, the `collapsed` element lives here.
_W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"


def _set_paragraph_collapsed(paragraph) -> None:
    """Mark a heading paragraph as collapsed-by-default when the doc is opened.

    Emits ``<w15:collapsed w15:val="1"/>`` per [MS-DOCX] §2.5.1.3. The element
    MUST be in the Word 2012 extension namespace
    (http://schemas.microsoft.com/office/word/2012/wordml). Using the base
    w: namespace causes Word to silently ignore the element, which is why
    earlier versions of this code rendered Sources headings expanded on open.

    Semantics (per the spec): when this element is set on a heading of level N,
    immediately subsequent paragraphs with a higher heading level number appear
    collapsed when the document is opened. In our use, it's set on the
    "Sources" Heading 4; the URL paragraph below carries outlineLvl=8, which
    satisfies "higher heading level number" and gets collapsed. The next
    finding's Heading 3 (outlineLvl=2) is lower and terminates the zone.

    python-docx's default nsmap does NOT register w15, so we construct the
    element via lxml directly instead of using OxmlElement / qn helpers.
    """
    pPr = paragraph._p.get_or_add_pPr()
    collapsed = etree.SubElement(
        pPr,
        f"{{{_W15_NS}}}collapsed",
        nsmap={"w15": _W15_NS},
    )
    collapsed.set(f"{{{_W15_NS}}}val", "1")


def _set_paragraph_outline_level(paragraph, level: int) -> None:
    """Set <w:outlineLvl> on a paragraph without changing its visual style.

    Word reads the outline level in both directions: a deep level (e.g. 8)
    pulls a body paragraph INTO a preceding heading's open-time collapse
    zone, while a shallow level (e.g. 0) makes a styled-but-unleveled
    paragraph (such as a Title-styled section header, which carries no
    native outline level) TERMINATE any open collapse zone and appear in
    Word's Navigation Pane."""
    pPr = paragraph._p.get_or_add_pPr()
    outline = OxmlElement('w:outlineLvl')
    outline.set(qn('w:val'), str(level))
    pPr.append(outline)


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
                       cycle_label: str = "2025",
                       failed_review_count: int = 0) -> None:
    """Write the report title and metadata.

    Uses separate paragraphs instead of \\n within runs to ensure
    reliable rendering across all Word versions and viewers.

    When ``failed_review_count`` > 0 the "Files Reviewed" line reports
    "{reviewed} of {submitted} ({failed} failed review)" so the metadata
    cannot read as a clean complete run when some specs never produced a
    review. A clean run keeps the original "Files Reviewed: {N}" form
    byte-for-byte.
    """
    title = doc.add_heading("Spec Critic — M&P Specification Review Report", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    submitted = len(files_reviewed)
    if failed_review_count > 0:
        reviewed = max(0, submitted - failed_review_count)
        files_reviewed_line = (
            f"Files Reviewed: {reviewed} of {submitted} "
            f"({failed_review_count} failed review)"
        )
    else:
        files_reviewed_line = f"Files Reviewed: {submitted}"

    # Metadata as separate centered paragraphs (not \n in a single para)
    meta_lines = [
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Model: {review.model}",
        files_reviewed_line,
        f"Code Cycle: California {cycle_label}",
    ]

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

def _write_files_reviewed(doc: Document, files_reviewed: list[str],
                          failed_review_specs: set[str] | None = None) -> None:
    """Write the files reviewed section with a bullet list.

    Any filename in ``failed_review_specs`` is annotated in red with a
    "— review failed (not reviewed)" suffix so the per-file list stays
    honest: a spec that failed review is visually distinct from one that
    was reviewed clean.
    """
    failed = set(failed_review_specs or ())
    doc.add_heading("Files Reviewed", level=1)
    for filename in files_reviewed:
        if filename in failed:
            para = doc.add_paragraph(style='List Bullet')
            run = para.add_run(f"{filename} — review failed (not reviewed)")
            run.bold = True
            run.font.color.rgb = RGBColor(192, 0, 0)
        else:
            doc.add_paragraph(filename, style='List Bullet')



# ---------------------------------------------------------------------------
# Run Diagnostics banner
# ---------------------------------------------------------------------------

def _summarize_run_diagnostics(
    *,
    findings: list,
    status_counts: dict,
    edit_action_counts: dict,
    cross_check_result,
    pipeline_result=None,
) -> dict:
    """Roll up operational counts for the Run Diagnostics banner.

    Surfaces at-a-glance operational health:
    edit-action histogram, cache-replay count + oldest age, verification
    failures, parse-time REPORT_ONLY demotions, extraction warnings,
    and cross-check status. Every value is
    derived from data already present on the findings / status counts /
    pipeline result; no new persistence is needed.

    Args:
        findings: All findings included in the report (review + cross-
            check). Used to count cache replays, find the oldest cache
            entry age, and count parse-time edit-shape demotions.
        status_counts: Pre-computed ``ReportStatus`` histogram from
            :func:`summarize_statuses` over the same finding list.
        edit_action_counts: Pre-computed ``EditActionLabel`` histogram.
        cross_check_result: The cross-check ``ReviewResult`` or ``None``
            when cross-check was disabled / never ran.
        pipeline_result: The pipeline result, queried opportunistically
            for ``extracted_specs`` so the extraction-warning slot can
            be populated. ``getattr`` with default
            so legacy callers and test doubles work.

    Returns a dict with the rolled-up values the renderer consumes.
    """
    edit_suggested = int(edit_action_counts.get(EditActionLabel.EDIT_SUGGESTED, 0) or 0)
    report_only = int(edit_action_counts.get(EditActionLabel.REPORT_ONLY, 0) or 0)
    verification_failed = int(status_counts.get(ReportStatus.VERIFICATION_FAILED, 0) or 0)

    # Cache replays: count findings whose verification carries a cache
    # hit. Track the oldest age so a reviewer can see "the staleness
    # picture" without expanding individual findings. Legacy resume
    # payloads without a recorded cache-entry timestamp produce ``None`` from
    # :func:`_cache_entry_age_days` — they count toward the total but
    # cannot contribute to the oldest-age display.
    cache_replay_count = 0
    oldest_age_days: int | None = None
    for finding in findings:
        vr = getattr(finding, "verification", None)
        if vr is None:
            continue
        if (getattr(vr, "cache_status", "") or "") != "hit":
            continue
        cache_replay_count += 1
        age = _cache_entry_age_days(vr)
        if age is not None and (oldest_age_days is None or age > oldest_age_days):
            oldest_age_days = age

    # Parse-time REPORT_ONLY demotions: findings stamped with a
    # ``demotion_reason`` are EDIT/ADD/DELETE proposals that were
    # rejected by :func:`validate_edit_shape` at parse time and routed
    # to REPORT_ONLY. Surfaced separately from the general REPORT_ONLY
    # count because they signal model-output shape issues (a
    # potentially-actionable finding that the model emitted with
    # missing fields), not a deliberate coordination/interpretation
    # finding.
    demotion_count = sum(
        1
        for finding in findings
        if (getattr(finding, "demotion_reason", None) or "").strip()
    )

    # Specs that failed review (not reviewed). Sourced from
    # ``PipelineResult.failed_review_specs`` (carried from
    # ``CollectedBatchState.truncated_specs``). This is the headline
    # honesty signal: a spec whose review truncated / parse-errored /
    # errored produces zero findings, exactly like a genuinely-clean
    # spec — so the banner must call it out explicitly or a partially-
    # failed run reads as fully clean. Defensive ``getattr`` keeps legacy
    # callers and test doubles (which may not set the field) at 0.
    failed_review_specs = [
        str(name)
        for name in (getattr(pipeline_result, "failed_review_specs", None) or [])
    ]
    failed_review_count = len(failed_review_specs)

    # Extraction warnings. Looks for an
    # ``extracted_specs`` attribute on the pipeline result with per-spec
    # ``extraction_warnings`` lists. Resolves to 0 when no specs carry
    # warnings — the row still renders so the banner shape stays stable.
    extraction_warning_count = 0
    extracted_specs = getattr(pipeline_result, "extracted_specs", None) or []
    for spec in extracted_specs:
        warnings = getattr(spec, "extraction_warnings", None) or []
        if warnings:
            extraction_warning_count += 1

    # Specs that carried pending Word "Track Changes" markup. Extraction
    # resolves these to the Accept-All view (insertions kept, deletions
    # removed); the advisory tells reviewers the spec was read as accept-all
    # so they can confirm that is the version they meant to review. This is
    # informational (the spec WAS reviewed), not a failure — surfaced in its
    # own calm-amber row/hint, distinct from the red content-loss warning row.
    # Defensive getattr keeps legacy callers / test doubles at 0.
    tracked_changes_spec_count = 0
    for spec in extracted_specs:
        if getattr(spec, "tracked_changes_detected", False):
            tracked_changes_spec_count += 1

    # Cross-check state: None means cross-check was not requested (or
    # disabled); otherwise the status string is "completed" / "skipped" /
    # "failed". The renderer treats "skipped" / "failed" as the actionable
    # signals the plan calls out.
    cross_check_state: dict | None = None
    if cross_check_result is not None:
        cc_status = (
            getattr(cross_check_result, "cross_check_status", None) or "completed"
        )
        cross_check_state = {
            "status": cc_status,
            "finding_count": len(getattr(cross_check_result, "findings", []) or []),
            # Chunked-pass telemetry: chunks that failed / skipped while the
            # overall status is still "completed" (TRUST_AUDIT P1-3 follow-up).
            # Defensive getattr keeps non-chunked results / test doubles at 0.
            "chunk_failures": int(getattr(cross_check_result, "chunk_failures", 0) or 0),
            "chunk_skips": int(getattr(cross_check_result, "chunk_skips", 0) or 0),
            "reason": (
                getattr(cross_check_result, "thinking", "")
                if cc_status == "skipped"
                else (
                    getattr(cross_check_result, "error", "")
                    if cc_status == "failed"
                    else ""
                )
            ),
        }

    # Findings whose verifier consumed the full
    # mode-scaled search budget without producing a grounded verdict.
    # Surfaced in the banner with a hint pointing operators at the
    # severity-tiered budget knob so they can choose to re-run with more
    # headroom. The flag round-trips through resume state so a resumed
    # run shows the same count.
    budget_exhausted_count = summarize_budget_exhausted(findings)

    return {
        "edit_suggested": edit_suggested,
        "report_only": report_only,
        "failed_review_count": failed_review_count,
        "failed_review_specs": failed_review_specs,
        "verification_failed": verification_failed,
        "cache_replay_count": cache_replay_count,
        "oldest_cache_age_days": oldest_age_days,
        "demotion_count": demotion_count,
        "extraction_warning_count": extraction_warning_count,
        "tracked_changes_spec_count": tracked_changes_spec_count,
        "cross_check": cross_check_state,
        "budget_exhausted_count": budget_exhausted_count,
    }


def _write_run_diagnostics_banner(doc: Document, summary: dict) -> None:
    """Render the Run Diagnostics banner.

    A styled table that surfaces operational health right after the
    title block. Reviewers can scan the banner to answer "did anything
    operationally bad happen on this run?" without scrolling through
    every finding. The table layout (label | value) makes label/value
    pairs greppable in the resulting .docx and keeps the visual style
    consistent with the existing severity / trust-model tables above.

    The banner highlights verification-failure and extraction-warning
    rows in red whenever the count is non-zero (the plan's only hard
    highlight requirement); skipped/failed cross-check status also
    renders in red so a reviewer can spot "the coordination pass did
    not run" at a glance. All other rows render neutral.

    A failure recovery hint paragraph appears below the table when
    verification-failure count > 0, pointing the reviewer at the
    workflow for re-running the failed findings. The actual re-run-
    failed-only mechanism is deferred; this only surfaces the visibility.
    """
    doc.add_heading("Run Diagnostics", level=1)

    intro = doc.add_paragraph()
    intro_run = intro.add_run(
        "Operational summary of this run. Use this section to spot at a "
        "glance whether any findings failed verification, were replayed "
        "from a stale cache, or had model-output shape issues that "
        "needed parse-time demotion."
    )
    intro_run.font.size = Pt(10)
    intro_run.font.italic = True
    intro_run.font.color.rgb = RGBColor(100, 100, 100)
    intro.paragraph_format.space_after = Pt(6)

    verification_failed = int(summary.get("verification_failed", 0) or 0)
    extraction_warnings = int(summary.get("extraction_warning_count", 0) or 0)
    tracked_changes_spec_count = int(summary.get("tracked_changes_spec_count", 0) or 0)
    cache_count = int(summary.get("cache_replay_count", 0) or 0)
    oldest_age = summary.get("oldest_cache_age_days")
    cross_check = summary.get("cross_check")
    budget_exhausted_count = int(summary.get("budget_exhausted_count", 0) or 0)
    failed_review_specs = list(summary.get("failed_review_specs", []) or [])
    failed_review_count = int(
        summary.get("failed_review_count", len(failed_review_specs)) or 0
    )

    # Build row tuples: (label, value, highlight). ``highlight=True``
    # paints the value cell with light-red shading + dark-red text so
    # the row pops from the surrounding neutral grid.
    rows: list[tuple[str, str, bool]] = [
        ("Edit suggested", str(summary.get("edit_suggested", 0)), False),
        ("Report-only", str(summary.get("report_only", 0)), False),
    ]

    # Specs that failed review (not reviewed). THE headline honesty row:
    # a spec whose review truncated / parse-errored / errored produces
    # zero findings, indistinguishable from a genuinely-clean spec, so it
    # is called out explicitly and highlighted red when > 0. Placed at the
    # top of the operational-health rows because "did we actually review
    # everything we said we did?" is the first question a reviewer of a
    # compliance tool must be able to answer.
    rows.append(
        (
            "Specs that failed review (not reviewed)",
            str(failed_review_count),
            failed_review_count > 0,
        )
    )

    # Cache replays: when there are any, show oldest age too so a
    # reviewer doesn't have to expand each Sources panel to find the
    # staleness picture.
    if cache_count > 0 and oldest_age is not None:
        cache_text = f"{cache_count} (oldest {oldest_age}d old)"
    else:
        cache_text = str(cache_count)
    rows.append(("Cache replays", cache_text, False))

    # Verification failures: only row the plan mandates highlight on
    # when > 0. Distinct from INSUFFICIENT_EVIDENCE — these are
    # operational errors that re-running can fix.
    rows.append(
        (
            "Verification failures (operational)",
            str(verification_failed),
            verification_failed > 0,
        )
    )

    rows.append(
        (
            "REPORT_ONLY demotions at parse time",
            str(summary.get("demotion_count", 0)),
            False,
        )
    )

    # Spec content extraction warnings.
    # Highlight in red when > 0 so a drawing-heavy spec that may have
    # lost text content stands out. Reads from
    # ExtractedSpec.extraction_warnings.
    rows.append(
        (
            "Spec content extraction warnings",
            str(extraction_warnings),
            extraction_warnings > 0,
        )
    )

    # Budget-exhausted findings. Highlight in
    # red when > 0 because it's an actionable signal — the operator can
    # raise the severity of the affected findings to grant more search
    # headroom. The hint paragraph below the table explains the action;
    # the row count gives the at-a-glance number.
    rows.append(
        (
            "Budget-exhausted findings",
            str(budget_exhausted_count),
            budget_exhausted_count > 0,
        )
    )

    # Specs with pending tracked changes (read as accept-all). Informational,
    # not a failure — the spec WAS reviewed, just resolved to the Accept-All
    # view — so the row renders neutral (not the red used for problems) and is
    # only added when > 0, keeping a clean run's banner byte-identical. The
    # amber hint below the table explains the resolution.
    if tracked_changes_spec_count > 0:
        rows.append(
            (
                "Specs with tracked changes (read as accept-all)",
                str(tracked_changes_spec_count),
                False,
            )
        )

    # Cross-spec coordination: surface when the pass ran (or didn't).
    # "skipped" / "failed" are the actionable signals the plan calls out;
    # "completed" is also rendered for transparency so a reader sees
    # "yes, the pass ran" without parsing the trust-model table.
    if cross_check is not None:
        cc_status = str(cross_check.get("status", "completed") or "completed")
        cc_count = int(cross_check.get("finding_count", 0) or 0)
        if cc_status == "skipped":
            cc_value = "skipped"
            cc_highlight = True
        elif cc_status == "failed":
            cc_value = "failed"
            cc_highlight = True
        else:
            cc_value = f"{cc_count} finding{'s' if cc_count != 1 else ''}"
            # A "completed" chunked pass can still have left a division
            # un-analyzed (a failed/skipped chunk). Flag it red so the green
            # finding count doesn't read as a clean, complete pass.
            incomplete = int(cross_check.get("chunk_failures", 0) or 0) + int(
                cross_check.get("chunk_skips", 0) or 0
            )
            if incomplete:
                cc_value += (
                    f" — {incomplete} chunk{'s' if incomplete != 1 else ''} not analyzed"
                )
                cc_highlight = True
            else:
                cc_highlight = False
        rows.append(("Cross-spec coordination", cc_value, cc_highlight))

    # Render as a 2-column table. Label cells get a light-gray
    # shading so the visual rhythm matches the existing summary table
    # above; value cells get red shading when ``highlight=True``.
    table = doc.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT

    for row_idx, (label, value, highlight) in enumerate(rows):
        label_cell = table.rows[row_idx].cells[0]
        _set_cell_shading(label_cell, "F0F0F0")
        label_cell.text = ""
        p = label_cell.paragraphs[0]
        label_run = p.add_run(label)
        label_run.bold = True
        label_run.font.size = Pt(10)

        value_cell = table.rows[row_idx].cells[1]
        if highlight:
            _set_cell_shading(value_cell, "FFE5E5")
        value_cell.text = ""
        p = value_cell.paragraphs[0]
        value_run = p.add_run(value)
        value_run.bold = True
        value_run.font.size = Pt(10)
        if highlight:
            value_run.font.color.rgb = RGBColor(192, 0, 0)

    # --- Failed-review recovery hint ---
    # The headline trust signal: name the specs that were NOT reviewed so
    # a reviewer cannot mistake a partially-failed run for a clean one.
    # Rendered first (above the verification/budget hints) and in
    # failure-red, matching the ⚠ glyph used on the per-spec GUI log line.
    # The cause/remedy differs from a verification failure: these specs
    # never produced findings at all, so the absence of findings carries
    # no information about whether the spec is compliant.
    if failed_review_count > 0:
        names = ", ".join(failed_review_specs) if failed_review_specs else "(names unavailable)"
        plural = failed_review_count != 1
        hint_para = doc.add_paragraph()
        hint_para.paragraph_format.space_before = Pt(6)
        hint_para.paragraph_format.space_after = Pt(8)
        hint_run = hint_para.add_run(
            f"⚠ {failed_review_count} spec{'s' if plural else ''} failed "
            f"review and {'were' if plural else 'was'} NOT reviewed: {names}. "
            f"{'Their' if plural else 'Its'} review truncated, failed to "
            f"parse, or errored, so {'they' if plural else 'it'} produced no "
            "findings — the absence of findings does NOT mean "
            f"{'they are' if plural else 'it is'} compliant. Re-run "
            f"{'these specs' if plural else 'this spec'} individually to "
            "obtain a review."
        )
        hint_run.font.size = Pt(10)
        hint_run.font.italic = True
        hint_run.font.color.rgb = RGBColor(192, 0, 0)

    # --- Failure recovery hint ---
    # The re-run-failed-only mechanism is deferred; for now we just
    # describe what re-running will do. The ⚠ glyph match is the same
    # one stamped on individual VERIFICATION_FAILED findings (see
    # STATUS_GLYPHS in report_status.py) so a reviewer can grep for it.
    if verification_failed > 0:
        hint_para = doc.add_paragraph()
        hint_para.paragraph_format.space_before = Pt(6)
        hint_para.paragraph_format.space_after = Pt(8)
        hint_run = hint_para.add_run(
            f"{verification_failed} finding"
            f"{'s' if verification_failed != 1 else ''} failed verification "
            "due to operational errors (network, rate limit, parse failures). "
            "These are visually marked with the ⚠ glyph in the findings "
            "below. Re-running the review will re-attempt verification for "
            "these findings; the cache does not persist operational-failure "
            "results so a re-run sees them fresh."
        )
        hint_run.font.size = Pt(10)
        hint_run.font.italic = True
        hint_run.font.color.rgb = RGBColor(178, 34, 34)

    # --- Budget-exhaustion recovery hint ---
    # Distinct from the failure hint above because the cause and the
    # remedy differ: failures are transient (re-run sees them fresh),
    # but budget exhaustion is a policy outcome (re-run with the same
    # severity will exhaust the same budget). The actionable hint is to
    # raise the finding's severity so the routing decision allocates
    # more searches. The per-severity budgets are rendered from
    # api_config._SEVERITY_MAX_USES (via web_search_max_uses_for_severity)
    # so this hint cannot drift from the actual policy. Rendered in a
    # calmer amber rather than the failure-red so a reader can tell the
    # two situations apart at a glance.
    if budget_exhausted_count > 0:
        budget_str = ", ".join(
            f"{sev} {web_search_max_uses_for_severity(sev)}"
            for sev in SEVERITY_ORDER
        )
        hint_para = doc.add_paragraph()
        hint_para.paragraph_format.space_before = Pt(6)
        hint_para.paragraph_format.space_after = Pt(8)
        hint_run = hint_para.add_run(
            f"{budget_exhausted_count} finding"
            f"{'s' if budget_exhausted_count != 1 else ''} exhausted the "
            "verifier's web_search budget without grounding a verdict "
            "(rendered as 'Insufficient evidence (search budget exhausted)' "
            "inline). Per-severity web_search budgets are "
            f"{budget_str}; re-running a lower-severity finding at a higher "
            "severity grants more headroom (CRITICAL findings already "
            "receive the maximum budget)."
        )
        hint_run.font.size = Pt(10)
        hint_run.font.italic = True
        hint_run.font.color.rgb = RGBColor(204, 132, 0)

    # --- Tracked-changes advisory ---
    # Not a failure: the spec WAS reviewed, but as the Accept-All-Changes view
    # (insertions kept, deletions removed). Rendered in the same calm amber as
    # the budget hint so it reads as informational, distinct from the failure-
    # red hints above. Prompts the reviewer to confirm the accept-all view is
    # the one they intend to review (vs. the pre-redline text).
    if tracked_changes_spec_count > 0:
        plural = tracked_changes_spec_count != 1
        hint_para = doc.add_paragraph()
        hint_para.paragraph_format.space_before = Pt(6)
        hint_para.paragraph_format.space_after = Pt(8)
        hint_run = hint_para.add_run(
            f"{tracked_changes_spec_count} spec{'s' if plural else ''} contained "
            f"pending tracked changes (Word 'Track Changes'). "
            f"{'They were' if plural else 'It was'} reviewed as if all changes "
            "were accepted — insertions kept, deletions removed — i.e. the text "
            "that will remain once the redline is accepted. Confirm that is the "
            "version you intend to review."
        )
        hint_run.font.size = Pt(10)
        hint_run.font.italic = True
        hint_run.font.color.rgb = RGBColor(204, 132, 0)

    doc.add_paragraph()  # Spacer between banner and the next section.


# ---------------------------------------------------------------------------
# Methodology note
# ---------------------------------------------------------------------------

def _summarize_verification_outcomes(findings: list) -> dict[str, object]:
    """Roll up the trust-model statuses + raw verdict counts for the methodology note.

    The status histogram (computed via
    :func:`report_status.summarize_statuses`) drives the methodology
    narrative and the new trust-model summary table. The verdict-count
    breakdown is preserved alongside it so existing summary lines
    (``CONFIRMED / CORRECTED / DISPUTED / UNVERIFIED``) keep working.
    """
    stats = {
        "total_findings": len(findings),
        "with_verification": 0,
        "verdict_counts": {"CONFIRMED": 0, "CORRECTED": 0, "DISPUTED": 0, "UNVERIFIED": 0},
        "status_counts": summarize_statuses(findings),
        "edit_action_counts": summarize_edit_actions(findings),
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
        "reflecting the review model\u2019s certainty at review time, before "
        "verification. The confidence score and the verification verdict are "
        "distinct signals: confidence is the review model\u2019s own pre-verification "
        "estimate, while the verdict is a separate verification pass\u2019s grounded "
        "assessment. For any finding the verifier went on to confirm, correct, "
        "contest, or dispute, the verification verdict \u2014 not the review "
        "confidence \u2014 is the authoritative trust signal, so the report "
        "de-emphasizes the confidence on those findings (shown small beside the "
        "status, marked \u201cpre-verification\u201d) and lets the verdict stand as "
        "the headline. The confidence score stays prominent only where verification "
        "did not reach a verdict (for example, not checked or insufficient evidence)."
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

    # Surface the pinned standards editions
    # that drove the verifier prompt for this cycle. Reviewers reading
    # the report can see which editions were treated as authoritative
    # without opening the source; if the spec cites a different edition
    # for one of these standards, the finding's relevance to the
    # current cycle should be re-checked.
    pinning_text = _render_pinned_editions_note(cycle_label)
    if pinning_text:
        doc.add_paragraph(pinning_text)

    # Collapsibility tip
    doc.add_paragraph(
        "Tip: In Word, hover over any heading to reveal a collapse triangle. "
        "Click it to hide the content beneath that heading. Use this to "
        "collapse individual findings or entire severity groups."
    )


def _render_pinned_editions_note(cycle_label: str) -> str:
    """Render the methodology paragraph that enumerates pinned editions.

    Looks up the :class:`CodeCycle` for the
    label and emits a one-paragraph note. Pinning details only render
    when the cycle has populated the new edition fields \u2014 a cycle with
    no pinning yet falls back to an empty string so the methodology
    note degrades gracefully.
    """
    cycle: CodeCycle = AVAILABLE_CYCLES.get(cycle_label, DEFAULT_CYCLE)
    entries = [std for std in cycle.standards if std.edition]
    if not entries:
        return ""
    # Join with semicolons because an individual description can itself contain a
    # comma (e.g. "NFPA 13 2025, as amended by California").
    rendered = "; ".join(std.description for std in entries)
    return (
        f"This review pinned the following standards editions per the {cycle_label} "
        f"California cycle: {rendered}. Findings referencing other editions should "
        "be reviewed for relevance to the current cycle."
    )


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
        # The UNVERIFIED *verdict* lumps locally-classified findings
        # (resolved without a web search — placeholders, stale cycles, etc.)
        # together with genuinely-unverifiable ones, so the raw count
        # overstates "unverified". Break the bucket down by trust status,
        # using the same labels as the Trust Model Summary below, so a reader
        # doesn't see "N unverified" here and "M insufficient evidence" there
        # and wonder which is real.
        if verdicts.get("UNVERIFIED", 0) > 0:
            unv_status_counts: dict = {}
            for f in all_findings:
                ver = getattr(f, "verification", None)
                if ver and str(getattr(ver, "verdict", "") or "").upper() == "UNVERIFIED":
                    st = classify_status(f)
                    unv_status_counts[st] = unv_status_counts.get(st, 0) + 1
            breakdown = [
                f"{unv_status_counts[s]} {status_label(s).lower()}"
                for s in STATUS_DISPLAY_ORDER
                if unv_status_counts.get(s, 0) > 0
            ]
            if breakdown:
                note = doc.add_paragraph()
                note_run = note.add_run(
                    "Of the unverified-verdict findings: " + ", ".join(breakdown) + "."
                )
                note_run.italic = True
                note_run.font.size = Pt(9)
                note_run.font.color.rgb = RGBColor(100, 100, 100)


# ---------------------------------------------------------------------------
# Trust-model summary
# ---------------------------------------------------------------------------

def _write_trust_model_summary(
    doc: Document,
    status_counts: dict,
    edit_action_counts: dict,
) -> None:
    """Render the per-status / per-edit-action histograms.

    Every finding receives one status and one
    edit-action label. The table here gives a top-of-report at-a-glance
    picture of how much of the run is supported vs. uncertain, before
    the reader gets to individual findings. The
    severity table above answers "how many issues are critical?"; this
    one answers "how many of them are actually trustworthy?"
    """
    total_status = sum(status_counts.values())
    if total_status == 0:
        return

    doc.add_heading("Trust Model Summary", level=1)
    intro = doc.add_paragraph()
    intro_run = intro.add_run(
        "Every finding is tagged with a trust status and an edit-action "
        "label. The two histograms below give the at-a-glance picture; "
        "individual findings carry the same labels inline."
    )
    intro_run.font.size = Pt(10)
    intro_run.font.italic = True
    intro_run.font.color.rgb = RGBColor(100, 100, 100)
    intro.paragraph_format.space_after = Pt(6)

    # --- Status histogram table ---
    visible_statuses = [
        s for s in STATUS_DISPLAY_ORDER if status_counts.get(s, 0) > 0
    ]
    if visible_statuses:
        table = doc.add_table(rows=2, cols=len(visible_statuses))
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER

        for col_idx, status in enumerate(visible_statuses):
            hex_color = STATUS_SHADING[status]
            header_cell = table.rows[0].cells[col_idx]
            _set_cell_shading(header_cell, hex_color)
            header_cell.text = ""
            p = header_cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(status_label(status))
            run.bold = True
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(255, 255, 255)

            count_cell = table.rows[1].cells[col_idx]
            count_cell.text = ""
            p = count_cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(str(status_counts[status]))
            run.bold = True
            run.font.size = Pt(14)
            run.font.color.rgb = STATUS_COLORS[status]

    # --- Edit-action histogram (compact inline form) ---
    visible_actions = [
        a for a in EDIT_ACTION_DISPLAY_ORDER if edit_action_counts.get(a, 0) > 0
    ]
    if visible_actions:
        para = doc.add_paragraph()
        para.paragraph_format.space_before = Pt(6)
        label_run = para.add_run("Edit eligibility: ")
        label_run.bold = True
        for i, action in enumerate(visible_actions):
            if i > 0:
                sep = para.add_run("  •  ")
                sep.font.color.rgb = RGBColor(160, 160, 160)
            count_run = para.add_run(f"{edit_action_counts[action]} ")
            count_run.bold = True
            count_run.font.color.rgb = EDIT_ACTION_COLORS[action]
            name_run = para.add_run(edit_action_label(action).lower())
            name_run.font.color.rgb = EDIT_ACTION_COLORS[action]
        para.paragraph_format.space_after = Pt(6)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _write_alert_section(
    doc: Document,
    title: str,
    description: str,
    alerts: list[dict],
) -> None:
    """Render one named alert section with per-file bullet groups.

    Every deterministic alert category goes through this helper
    so the section layout stays consistent. Each section also gets a
    "(deterministic check)" suffix so the reader can tell at a glance which
    alerts came from local rules vs. LLM findings — deterministic findings
    are clearly labeled.
    """
    if not alerts:
        return
    doc.add_heading(f"{title} (deterministic check)", level=2)
    _add_styled_paragraph(doc, description, size=10, space_after=6)
    by_file: dict[str, list[dict]] = {}
    for alert in alerts:
        by_file.setdefault(alert.get("filename", ""), []).append(alert)
    for filename, items in by_file.items():
        para = doc.add_paragraph()
        para.add_run(f"{filename}").bold = True
        for alert in items[:5]:
            context = alert.get("context", alert.get("match", ""))
            doc.add_paragraph(context, style='List Bullet')
        if len(items) > 5:
            doc.add_paragraph(
                f"... and {len(items) - 5} more",
                style='List Bullet',
            )


def _write_alerts(
    doc: Document,
    leed_alerts: list[dict],
    placeholder_alerts: list[dict],
    *,
    code_cycle_alerts: list[dict] | None = None,
    structural_alerts: list[dict] | None = None,
    naming_alerts: list[dict] | None = None,
    template_marker_alerts: list[dict] | None = None,
    invalid_code_cycle_alerts: list[dict] | None = None,
    duplicate_paragraph_alerts: list[dict] | None = None,
) -> None:
    """Write the Alerts section with every deterministic-check category.

    Previously this rendered only ``leed_alerts`` and
    ``placeholder_alerts``; ``code_cycle_alerts`` / ``structural_alerts`` /
    ``naming_alerts`` were collected during preflight but silently dropped
    before the report saw them, so users had to read the log to discover
    them. The new optional kwargs default to ``None`` (treated as empty) so
    legacy callers — including older snapshot tests — keep working.
    """
    code_cycle_alerts = code_cycle_alerts or []
    structural_alerts = structural_alerts or []
    naming_alerts = naming_alerts or []
    template_marker_alerts = template_marker_alerts or []
    invalid_code_cycle_alerts = invalid_code_cycle_alerts or []
    duplicate_paragraph_alerts = duplicate_paragraph_alerts or []
    if not any((
        leed_alerts,
        placeholder_alerts,
        code_cycle_alerts,
        structural_alerts,
        naming_alerts,
        template_marker_alerts,
        invalid_code_cycle_alerts,
        duplicate_paragraph_alerts,
    )):
        return

    doc.add_heading("Alerts", level=1)
    _add_styled_paragraph(
        doc,
        "These items were detected locally by deterministic rules; no LLM "
        "tokens were spent on them. They sit alongside the LLM findings "
        "below.",
        size=10,
        space_after=6,
    )
    _write_alert_section(
        doc,
        "LEED References Detected",
        "The following LEED references were found. Since this is not a "
        "LEED project, these should be removed:",
        leed_alerts,
    )
    _write_alert_section(
        doc,
        "Unresolved Placeholders",
        "The following editorial placeholders need to be resolved:",
        placeholder_alerts,
    )
    _write_alert_section(
        doc,
        "Unresolved Template Markers",
        "The following TODO / FIXME / XXX / ??? markers are still in the "
        "spec and should be resolved before issuing:",
        template_marker_alerts,
    )
    _write_alert_section(
        doc,
        "Stale California Code Cycle References",
        "The following references cite a historical California code cycle "
        "rather than the one selected for this review:",
        code_cycle_alerts,
    )
    _write_alert_section(
        doc,
        "Invalid California Code Cycle Years",
        "The following references cite a year that is not a real California "
        "code cycle (California publishes cycles every 3 years: 2010, 2013, "
        "2016, 2019, 2022, 2025). These are likely typos:",
        invalid_code_cycle_alerts,
    )
    _write_alert_section(
        doc,
        "Structural Issues",
        "Empty sections and duplicate section headings detected in the "
        "spec body:",
        structural_alerts,
    )
    _write_alert_section(
        doc,
        "Duplicate Paragraphs",
        "These paragraphs appear verbatim more than once in the same spec — "
        "likely copy-paste mistakes:",
        duplicate_paragraph_alerts,
    )
    _write_alert_section(
        doc,
        "Inconsistent Filenames",
        "These files use a CSI naming style that differs from the project's "
        "dominant style:",
        naming_alerts,
    )


# ---------------------------------------------------------------------------
# Per-finding evidence panel
# ---------------------------------------------------------------------------

def _write_evidence_panel(doc: Document, finding, vr) -> None:
    """Render the verifier-evidence panel under the collapsed Sources heading.

    Replaces the old URL-only Sources block.
    For every finding with a verification result, surface enough audit
    trail that a reviewer can answer "why did the verifier reach this
    verdict?" without leaving the report:

    - Verifier model (Sonnet 4.6 / Opus 4.8 / local).
    - Verification mode (LOCAL_SKIP / STRICT_STRUCTURED / STANDARD_REASONING
      / DEEP_REASONING) in human-readable form.
    - Search budget used ("N of M searches used") computed from
      ``web_search_requests`` and the severity-based ceiling.
    - Source quote: the verbatim snippet the verifier said
      it relied on. Rendered as an indented italic blockquote so it is
      visually distinct from prose paragraphs.
    - Verifier rationale: the model's explanation. Moved here from
      its old location above the Sources heading so it sits adjacent
      to the supporting quote.
    - Escalation history (when applicable): "Initial verdict … → Final
      verdict …" so a reviewer can see when the two models disagreed.
    - Accepted source URLs ("Web/code evidence").
    - Rejected source URLs.

    The Sources heading is collapsed-by-default — same behavior as
    before. Every paragraph inside the panel carries
    ``outlineLvl=8`` so Word's open-time collapse treats them all as
    part of the heading's zone.
    """
    # Locate the dataclass-level fields once with safe defaults so a
    # legacy resume payload (or a test-stub VerificationResult) can be
    # rendered without breaking.
    model_used = (getattr(vr, "model_used", "") or "").strip()
    mode_label = _verification_mode_label(getattr(vr, "verification_mode", "") or "")
    web_search_requests = int(getattr(vr, "web_search_requests", 0) or 0)
    severity_budget = web_search_max_uses_for_severity(
        getattr(finding, "severity", None)
    )
    source_quote = (getattr(vr, "source_quote", "") or "").strip()
    explanation = (getattr(vr, "explanation", "") or "").strip()
    escalation_attempted = bool(getattr(vr, "escalation_attempted", False))
    accepted = list(vr.sources or [])
    rejected = list(getattr(vr, "rejected_sources", []) or [])
    # web_fetch evidence. STRICT_STRUCTURED /
    # LOCAL_SKIP modes never attach the fetch tool, so this is 0/[] for
    # them; STANDARD/DEEP modes attach the tool but the model may not
    # have used it, so 0/[] is also the common case there.
    web_fetch_requests = int(getattr(vr, "web_fetch_requests", 0) or 0)
    fetched_sources = list(getattr(vr, "fetched_sources", []) or [])

    # The panel is rendered whenever ``finding.verification`` exists.
    # Even local-skip findings get the panel — they carry
    # ``verification_mode="local_skip"`` and an explanation, both of
    # which are auditable signal.
    sources_heading = doc.add_heading("Sources", level=4)
    _set_paragraph_collapsed(sources_heading)

    # --- Verifier model ---
    if model_used:
        para = doc.add_paragraph()
        _set_paragraph_outline_level(para, 8)
        label = para.add_run("Verifier model: ")
        label.bold = True
        label.font.size = Pt(9)
        label.font.color.rgb = RGBColor(100, 100, 100)
        body = para.add_run(model_used)
        body.font.size = Pt(9)
        body.font.color.rgb = RGBColor(100, 100, 100)
        para.paragraph_format.space_after = Pt(2)

    # --- Verification mode ---
    if mode_label:
        para = doc.add_paragraph()
        _set_paragraph_outline_level(para, 8)
        label = para.add_run("Verification mode: ")
        label.bold = True
        label.font.size = Pt(9)
        label.font.color.rgb = RGBColor(100, 100, 100)
        body = para.add_run(mode_label)
        body.font.size = Pt(9)
        body.font.color.rgb = RGBColor(100, 100, 100)
        para.paragraph_format.space_after = Pt(2)

    # --- Search budget used ---
    # Local-skip findings never invoke web_search (budget 0/M), but the
    # line is still useful — it makes "no external check ran" explicit.
    # We render the line whenever web_search was at least attempted
    # (web_search_requests > 0) OR when the verifier ran in a mode that
    # could have used the search tool. For local_skip we suppress
    # because "0 of N searches used" is misleading — the mode doesn't
    # use the search tool by design.
    mode_raw = (getattr(vr, "verification_mode", "") or "").strip().lower()
    if mode_raw != "local_skip":
        para = doc.add_paragraph()
        _set_paragraph_outline_level(para, 8)
        label = para.add_run("Search budget used: ")
        label.bold = True
        label.font.size = Pt(9)
        label.font.color.rgb = RGBColor(100, 100, 100)
        # When the verifier used web_fetch,
        # append the fetch count in the same line so a reviewer sees
        # "Searches: N, Full-page fetches: M" at a glance. The fetch
        # count is only rendered when > 0 because most verifications
        # never need a fetch — keeping the line short for the common
        # path matters more than uniformity.
        if web_fetch_requests > 0:
            usage_text = (
                f"Searches: {web_search_requests} of {severity_budget}, "
                f"Full-page fetches: {web_fetch_requests}"
            )
        else:
            usage_text = (
                f"{web_search_requests} of {severity_budget} searches used"
            )
        body = para.add_run(usage_text)
        body.font.size = Pt(9)
        body.font.color.rgb = RGBColor(100, 100, 100)
        para.paragraph_format.space_after = Pt(2)

    # --- Source quote ---
    if source_quote:
        label_para = doc.add_paragraph()
        _set_paragraph_outline_level(label_para, 8)
        label_run = label_para.add_run("Source quote (verbatim from search result):")
        label_run.bold = True
        label_run.font.size = Pt(9)
        label_run.font.color.rgb = RGBColor(100, 100, 100)
        label_para.paragraph_format.space_after = Pt(2)

        quote_para = doc.add_paragraph()
        _set_paragraph_outline_level(quote_para, 8)
        # Indent the blockquote so it stands apart from the
        # surrounding labels; italic + slightly smaller font signals
        # "this is verbatim source content, not commentary".
        quote_para.paragraph_format.left_indent = Inches(0.35)
        quote_run = quote_para.add_run(source_quote)
        quote_run.italic = True
        quote_run.font.size = Pt(9)
        quote_run.font.color.rgb = RGBColor(80, 80, 80)
        quote_para.paragraph_format.space_after = Pt(4)

    # --- Verifier rationale (moved from above) ---
    # Label kept as "Verification rationale:" so existing report
    # consumers and the label-rename invariants continue to
    # find the field.
    if explanation:
        para = doc.add_paragraph()
        _set_paragraph_outline_level(para, 8)
        label = para.add_run("Verification rationale: ")
        label.bold = True
        label.font.size = Pt(9)
        label.font.color.rgb = RGBColor(100, 100, 100)
        body = para.add_run(explanation)
        body.font.size = Pt(9)
        body.font.color.rgb = RGBColor(100, 100, 100)
        para.paragraph_format.space_after = Pt(3)

    # --- Escalation history ---
    if escalation_attempted:
        initial_verdict = (getattr(vr, "initial_verdict", "") or "").strip()
        initial_model = (getattr(vr, "initial_model", "") or "").strip()
        final_verdict = (getattr(vr, "verdict", "") or "").strip()
        final_model = model_used
        escalation_reason = (getattr(vr, "escalation_reason", "") or "").strip()
        changed = bool(getattr(vr, "escalation_changed_verdict", False))
        # The stricter "both grounded AND
        # verdicts differ" flag (vs. ``escalation_changed_verdict``
        # which also fires on initial-UNVERIFIED-then-CONFIRMED). When
        # set, the finding renders as VERIFIED_CONTESTED at the
        # top-level status badge and the panel below adds the
        # initial-pass citations side-by-side with the final-pass
        # citations so a reviewer can see "here are the sources Sonnet
        # cited, here are the sources Opus cited" without leaving the
        # finding entry.
        models_disagreed = bool(getattr(vr, "models_disagreed", False))
        initial_sources = list(getattr(vr, "initial_sources", []) or [])

        # Bold inline label.
        para = doc.add_paragraph()
        _set_paragraph_outline_level(para, 8)
        label = para.add_run("Escalation history: ")
        label.bold = True
        label.font.size = Pt(9)
        # Highlight in red-orange when the escalation actually changed
        # the verdict — that's the "two models disagreed" signal a
        # reviewer most wants to see. The contested case
        # (both grounded, verdicts differ) gets the purple
        # VERIFIED_CONTESTED color so the panel matches the top-level
        # status badge.
        if models_disagreed:
            label_color = RGBColor(128, 0, 128)  # Purple — VERIFIED_CONTESTED
        elif changed:
            label_color = RGBColor(178, 34, 34)  # Firebrick — verdict changed
        else:
            label_color = RGBColor(100, 100, 100)  # Gray — neutral
        label.font.color.rgb = label_color
        parts = []
        if initial_verdict:
            initial_summary = (
                f"{initial_verdict} from {initial_model}"
                if initial_model
                else initial_verdict
            )
            parts.append(f"Initial verdict: {initial_summary}")
        if final_verdict:
            final_summary = (
                f"{final_verdict} from {final_model}"
                if final_model
                else final_verdict
            )
            parts.append(f"Final verdict: {final_summary}")
        sentence = " → ".join(parts) if parts else "escalated"
        if escalation_reason:
            sentence += f". Reason: {escalation_reason}"
        # Surface the contested state explicitly so the
        # inline sentence stays self-explanatory even when the panel
        # is read in isolation (resume-state JSON dumps, exported
        # report scanned without the top-level status badge nearby).
        if models_disagreed:
            sentence += (
                " The two models disagreed on this finding while both "
                "produced grounded verdicts; manual review recommended."
            )
        elif changed:
            sentence += " (models disagreed)"
        sentence += "."
        body = para.add_run(sentence)
        body.font.size = Pt(9)
        body.font.color.rgb = label_color
        para.paragraph_format.space_after = Pt(3)

        # When the disagreement is real (both
        # grounded), render the initial verifier's accepted citations
        # as a follow-up line. The final verifier's citations already
        # render below under "Web/code evidence" so a reviewer reading
        # the panel top-to-bottom sees Sonnet's sources here, Opus's
        # sources below, and can compare side-by-side. Omitted when
        # there are no initial citations to show (legacy results, or
        # the stricter ``models_disagreed`` flag was never set).
        if models_disagreed and initial_sources:
            initial_label_para = doc.add_paragraph()
            _set_paragraph_outline_level(initial_label_para, 8)
            initial_label_run = initial_label_para.add_run(
                f"Initial verifier sources ({initial_model or 'initial pass'}):"
            )
            initial_label_run.font.size = Pt(9)
            initial_label_run.bold = True
            initial_label_run.font.color.rgb = RGBColor(128, 0, 128)
            initial_label_para.paragraph_format.space_after = Pt(2)

            initial_sources_para = doc.add_paragraph()
            _set_paragraph_outline_level(initial_sources_para, 8)
            initial_sources_para.paragraph_format.space_after = Pt(3)
            for i, url in enumerate(initial_sources):
                if i > 0:
                    initial_sources_para.add_run("  •  ")
                run = initial_sources_para.add_run(url)
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(59, 130, 246)

    # --- Accepted source URLs ("Web/code evidence") ---
    if accepted:
        label_para = doc.add_paragraph()
        _set_paragraph_outline_level(label_para, 8)
        label_run = label_para.add_run(
            "Web/code evidence (cited and found in search results):"
        )
        label_run.font.size = Pt(9)
        label_run.bold = True
        label_run.font.color.rgb = RGBColor(0, 100, 0)

        para = doc.add_paragraph()
        _set_paragraph_outline_level(para, 8)
        para.paragraph_format.space_after = Pt(3)
        for i, url in enumerate(accepted):
            if i > 0:
                para.add_run("  •  ")
            run = para.add_run(url)
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(59, 130, 246)

    # --- Rejected source URLs ---
    if rejected:
        label_para = doc.add_paragraph()
        _set_paragraph_outline_level(label_para, 8)
        label_run = label_para.add_run(
            "Unsupported / rejected sources (cited by the model but not present in web_search results):"
        )
        label_run.font.size = Pt(9)
        label_run.bold = True
        label_run.font.color.rgb = RGBColor(192, 0, 0)

        rej_para = doc.add_paragraph()
        _set_paragraph_outline_level(rej_para, 8)
        rej_para.paragraph_format.space_after = Pt(3)
        for i, entry in enumerate(rejected):
            if i > 0:
                rej_para.add_run("  •  ")
            url = entry.get("url") if isinstance(entry, dict) else str(entry)
            reason = entry.get("reason") if isinstance(entry, dict) else ""
            url_run = rej_para.add_run(url or "(empty)")
            url_run.font.size = Pt(9)
            url_run.font.color.rgb = RGBColor(128, 128, 128)
            url_run.font.italic = True
            if reason:
                reason_run = rej_para.add_run(f" [{reason}]")
                reason_run.font.size = Pt(9)
                reason_run.font.color.rgb = RGBColor(192, 0, 0)

    # --- Full-text sources consulted ---
    # When the verifier used web_fetch, list the URLs it pulled in full
    # in a dedicated sub-section so a reviewer can tell at a glance which
    # sources were skimmed (web_search snippets) vs. read in depth
    # (web_fetch). The accepted-citations block above ("Web/code
    # evidence") already mixes both kinds; this block answers the
    # narrower question "which pages did the verifier read in full?"
    # Suppressed when no fetches happened so the panel stays compact
    # on the common path.
    if fetched_sources:
        label_para = doc.add_paragraph()
        _set_paragraph_outline_level(label_para, 8)
        label_run = label_para.add_run(
            "Full-text sources consulted (retrieved via web_fetch):"
        )
        label_run.font.size = Pt(9)
        label_run.bold = True
        label_run.font.color.rgb = RGBColor(0, 100, 0)

        fetch_para = doc.add_paragraph()
        _set_paragraph_outline_level(fetch_para, 8)
        fetch_para.paragraph_format.space_after = Pt(3)
        for i, url in enumerate(fetched_sources):
            if i > 0:
                fetch_para.add_run("  •  ")
            url_run = fetch_para.add_run(url)
            url_run.font.size = Pt(9)
            url_run.font.color.rgb = RGBColor(59, 130, 246)

    # --- Force-refresh hint for cache replays ---
    # A workflow hint, not a programmatic feature: tells the reviewer
    # exactly where to delete the entry if they want fresh verification.
    # Rendered only for cache-hit results so non-replayed findings stay
    # uncluttered.
    if (getattr(vr, "cache_status", "") or "") == "hit":
        hint_para = doc.add_paragraph()
        _set_paragraph_outline_level(hint_para, 8)
        hint_run = hint_para.add_run(
            "To force re-verification of this finding, delete its entry from "
            f"{default_cache_path()}."
        )
        hint_run.font.size = Pt(9)
        hint_run.font.italic = True
        hint_run.font.color.rgb = RGBColor(128, 128, 128)
        hint_para.paragraph_format.space_after = Pt(3)


# ---------------------------------------------------------------------------
# Single finding entry (collapsible via Heading 3)
# ---------------------------------------------------------------------------

def _write_finding_entry(doc: Document, finding, index: int) -> None:
    """Write a single finding as a collapsible block.

    The finding header is rendered as a Heading 3 paragraph, which enables
    Word's native heading-collapse feature. Users can click the collapse
    triangle that appears on hover to hide the finding's body content.

    Trust-model rendering:
        - Adds a "Status" line right under the header so the trust-model
          status is the first thing readers see (avoid presenting all
          findings as equally certain).
        - Adds an "Edit eligibility" line so readers can tell at a glance
          whether the finding carries a suggested edit or is report-only.
        - Renames the spec quote / web sources / rationale / rejected
          sources sub-labels so the four evidence concepts are explicit
          rather than implied.

    Layout:
        Heading 3:  [SEVERITY] 92% — filename.docx — Section ref
        Normal:     Status: <status>  •  Edit: <edit_action>
        Normal:     Issue: ...
        Normal:     Action: ...
        Normal:     Spec evidence: ... (existingText, red)
        Normal:     Proposed replacement: ... (green)
        Normal:     Reference: ... (blue)
        Normal:     Verification verdict: ... (if applicable)
        Normal:     Verification rationale: ... (if applicable)
        Heading 4:  Sources (collapsed by default)
        Normal:       Web/code evidence: <accepted_sources>
        Normal:       Unsupported / rejected sources: <rejected_sources>
    """
    severity_color = SEVERITY_COLORS.get(finding.severity, RGBColor(0, 0, 0))
    conf_tier = _confidence_tier(finding.confidence)
    conf_color = CONFIDENCE_COLORS[conf_tier]

    # --- Finding header as Heading 3 (enables Word collapse) ---
    para = doc.add_paragraph()
    para.style = doc.styles['Heading 3']
    para.paragraph_format.space_before = Pt(12)
    para.paragraph_format.space_after = Pt(4)


    # Index + severity badge
    run = para.add_run(f"{index}. [{finding.severity}] ")
    run.bold = True
    run.font.color.rgb = severity_color
    run.font.size = Pt(11)
    # Confidence (the review model's pre-verification certainty).
    # Once verification reaches a verdict that supersedes it
    # (Verified — supported / contradicted / contested / Disputed), the
    # confidence % is dropped from the header: that finding's trust signal
    # is the verdict — shown on the Status line and the Verification
    # verdict line below — not this pre-verification number, which can read
    # misleadingly low on a confirmed finding (or high on a disputed one).
    # The number is preserved as a de-emphasized footnote on the Status
    # line. For not-yet-verified findings the % stays the prominent,
    # primary signal in the header.
    confidence_superseded = verdict_supersedes_confidence(finding)
    if not confidence_superseded:
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

    # --- Status + edit-action line ---
    # The trust-model status renders right under the header so readers
    # see "Verified — supported" / "Disputed" / "Insufficient evidence"
    # before they read the issue. The edit-action label sits on the
    # same line so a reader scanning by finding can also see whether
    # there's an actionable edit attached.
    status = classify_status(finding)
    edit_action = classify_edit_action(finding)
    status_color = STATUS_COLORS[status]
    action_color = EDIT_ACTION_COLORS[edit_action]

    status_para = doc.add_paragraph()
    status_label_run = status_para.add_run("Status: ")
    status_label_run.bold = True
    status_label_run.font.size = Pt(10)
    glyph_run = status_para.add_run(f"{status_glyph(status)} ")
    glyph_run.bold = True
    glyph_run.font.color.rgb = status_color
    glyph_run.font.size = Pt(10)
    value_run = status_para.add_run(status_label(status))
    value_run.bold = True
    value_run.font.color.rgb = status_color
    value_run.font.size = Pt(10)
    # When the verifier consumed its full
    # mode-scaled search budget without producing a grounded verdict,
    # append a "(search budget exhausted)" sub-label so the reviewer
    # sees the actionable signal inline. The status itself stays
    # INSUFFICIENT_EVIDENCE — the trust level is the same as any other
    # unground UNVERIFIED — but the sub-label distinguishes "verifier
    # had no headroom" from "verifier ran out of evidence at search 2
    # of 7". Same color as the status so the badge reads as part of
    # the status, not a separate field.
    if is_budget_exhausted(finding):
        budget_run = status_para.add_run(" (search budget exhausted)")
        budget_run.font.size = Pt(10)
        budget_run.font.italic = True
        budget_run.font.color.rgb = status_color
    # Separator + edit-action.
    sep_run = status_para.add_run("  •  ")
    sep_run.font.color.rgb = RGBColor(160, 160, 160)
    sep_run.font.size = Pt(10)
    edit_label_run = status_para.add_run("Edit: ")
    edit_label_run.bold = True
    edit_label_run.font.size = Pt(10)
    edit_value_run = status_para.add_run(edit_action_label(edit_action))
    edit_value_run.bold = True
    edit_value_run.font.color.rgb = action_color
    edit_value_run.font.size = Pt(10)

    # --- Cache-replay badge ---
    # When the verifier result came from a cache hit, render an inline
    # badge showing the entry's age so a reviewer can spot stale verdicts
    # without expanding the Sources panel. Color tier:
    #   amber  for <30 days, orange for 30-90 days, red for >90 days.
    # Suppressed for non-hit results, for legacy resume payloads with
    # no ``cache_entry_created_ts`` recorded, and for clock-skew
    # cases where the recorded timestamp is in the future.
    age_days = _cache_entry_age_days(getattr(finding, "verification", None))
    if age_days is not None:
        badge_color = CACHE_AGE_COLORS[_cache_age_tier(age_days)]
        cache_sep_run = status_para.add_run("  •  ")
        cache_sep_run.font.color.rgb = RGBColor(160, 160, 160)
        cache_sep_run.font.size = Pt(10)
        cache_badge_run = status_para.add_run(
            f"Cache replay — {age_days}d old"
        )
        cache_badge_run.bold = True
        cache_badge_run.font.color.rgb = badge_color
        cache_badge_run.font.size = Pt(10)

    # Review-confidence footnote. When the header suppressed the
    # confidence % (a verdict supersedes it), surface the review model's
    # pre-verification confidence here as a small, gray, explicitly
    # labeled footnote — the number is preserved for anyone who wants it,
    # but rendered so it can't be mistaken for the post-verification trust
    # signal carried by the status/verdict.
    if confidence_superseded:
        conf_sep_run = status_para.add_run("  •  ")
        conf_sep_run.font.color.rgb = RGBColor(170, 170, 170)
        conf_sep_run.font.size = Pt(9)
        conf_note_run = status_para.add_run(
            f"review confidence {finding.confidence:.0%} (pre-verification)"
        )
        conf_note_run.italic = True
        conf_note_run.font.size = Pt(9)
        conf_note_run.font.color.rgb = RGBColor(150, 150, 150)

    status_para.paragraph_format.space_after = Pt(3)

    # --- Issue ---
    para = doc.add_paragraph()
    para.add_run("Issue: ").bold = True
    para.add_run(finding.issue or "")
    para.paragraph_format.space_after = Pt(3)

    # --- Action / edit-proposal block ---
    # The report distinguishes findings that carry an edit proposal
    # from ones that don't. REPORT_ONLY findings render an explicit
    # "No edit proposal — surfaced for review only" line so readers see
    # the finding without expecting an edit; findings with a proposal
    # keep the original Action / Existing / Replace With layout.
    #
    # The "Existing Text" label becomes "Spec evidence" so the
    # quoted-from-the-spec source is explicitly the *spec evidence*
    # concept, distinct from web/code evidence (sources)
    # and verification rationale (explanation).
    proposal = finding.as_edit_proposal()
    if proposal is None:
        para = doc.add_paragraph()
        run = para.add_run("Action: REPORT_ONLY")
        run.bold = True
        para.paragraph_format.space_after = Pt(3)

        # When the parser demoted an EDIT/DELETE/ADD because a required
        # field was missing, surface the specific reason inline so a
        # reader sees "the model claimed EDIT but no existingText was
        # provided" instead of the generic coordination/interpretation
        # explanation. Native REPORT_ONLY emissions keep the original
        # note text.
        demotion = (getattr(finding, "demotion_reason", None) or "").strip()
        if demotion:
            note_text = (
                "Edit proposal demoted to REPORT_ONLY at parse time: "
                f"{demotion}. The underlying finding is preserved; manual "
                "review required to determine a clean textual fix."
            )
        else:
            note_text = (
                "No edit proposal — surfaced for review only (coordination, "
                "interpretation, or multi-paragraph rewrite required)."
            )
        note_para = doc.add_paragraph()
        note_run = note_para.add_run(note_text)
        note_run.font.italic = True
        note_run.font.size = Pt(10)
        note_run.font.color.rgb = RGBColor(100, 100, 100)
        note_para.paragraph_format.space_after = Pt(3)
    else:
        para = doc.add_paragraph()
        para.add_run("Action: ").bold = True
        para.add_run(proposal.action_type or "")
        para.paragraph_format.space_after = Pt(3)

        # --- Spec evidence (red) — the text quoted from the source spec ---
        if proposal.existing_text:
            para = doc.add_paragraph()
            para.add_run("Spec evidence: ").bold = True
            run = para.add_run(proposal.existing_text)
            run.font.color.rgb = RGBColor(192, 0, 0)
            para.paragraph_format.space_after = Pt(3)

        # --- Proposed replacement (green) ---
        if proposal.replacement_text:
            para = doc.add_paragraph()
            para.add_run("Proposed replacement: ").bold = True
            run = para.add_run(proposal.replacement_text)
            run.font.color.rgb = RGBColor(0, 128, 0)
            para.paragraph_format.space_after = Pt(3)

    # --- Code reference (blue) ---
    if finding.codeReference:
        para = doc.add_paragraph()
        para.add_run("Reference: ").bold = True
        run = para.add_run(finding.codeReference)
        run.font.color.rgb = RGBColor(59, 130, 246)
        para.paragraph_format.space_after = Pt(3)

    # --- Verification verdict + correction ---
    if finding.verification:
        vr = finding.verification
        verdict_color = VERDICT_COLORS.get(vr.verdict, VERDICT_COLORS["UNVERIFIED"])
        verdict_icon = VERDICT_ICONS.get(vr.verdict, "—")

        para = doc.add_paragraph()
        run = para.add_run(f"Verification verdict: {verdict_icon} {vr.verdict}")
        run.bold = True
        run.font.color.rgb = verdict_color
        para.paragraph_format.space_after = Pt(3)

        if vr.verdict == "CORRECTED" and vr.correction:
            para = doc.add_paragraph()
            para.add_run("Correction: ").bold = True
            run = para.add_run(vr.correction)
            run.font.color.rgb = RGBColor(204, 132, 0)  # Amber
            para.paragraph_format.space_after = Pt(3)

        # --- Evidence panel ---
        # Rendered under the existing collapsed-by-default "Sources" Heading
        # 4. Order: verifier model → verification mode → search budget →
        # source quote (blockquote) → verifier rationale → escalation
        # history → accepted source URLs → rejected source URLs.
        # All paragraphs in the panel carry outlineLvl=8 so Word's
        # open-time collapse zone hides them along with the URLs that
        # used to live alone under this heading.
        _write_evidence_panel(doc, finding, vr)


# ---------------------------------------------------------------------------
# Findings section
# ---------------------------------------------------------------------------

def _write_findings_section(doc: Document, review) -> None:
    """Write per-spec findings grouped by severity, then spec file, then confidence.

    Uses heading hierarchy for Word-native collapse support:
    - Title (level 0): "Findings" (stamped outlineLvl=0 — Title has no
      native outline level, so without it the header is body text to
      Word's collapse logic and invisible to the Navigation Pane)
    - Heading 1: Severity group (e.g., "CRITICAL (1)")
    - Heading 3: Individual finding header (collapsible)
    - Normal: Finding body content

    Within a severity group, findings are grouped by spec file so one spec's
    issues stay contiguous (the prior confidence-only sort scattered a single
    spec's findings across the whole severity block); confidence orders the
    findings within each file.
    """
    findings_heading = doc.add_heading("Findings", level=0)
    _set_paragraph_outline_level(findings_heading, 0)

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
        # Group by spec file (the fileName prefix is the CSI section number,
        # so a lexical sort yields CSI order), then confidence descending.
        # Keeps one spec's findings contiguous instead of interleaving every
        # spec by confidence across the severity block.
        severity_findings = sorted(
            [f for f in review.findings if f.severity == severity],
            key=lambda f: (f.fileName or "", -f.confidence),
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
    """Write cross-spec coordination section and explicit status.

    Cross-check findings are rendered with the same collapsible structure
    as per-spec findings.

    The section header must carry an explicit outline level and own its
    page break: the finding immediately before this section ends with a
    collapsed-by-default "Sources" Heading 4, whose collapse zone runs
    until the next paragraph with an outline level at or above it. A bare
    Title-styled header (no native outline level) and a standalone
    page-break paragraph are both body text to that logic, so Word folded
    the entire section banner into the last finding's collapsed Sources
    panel — hidden on open, absent from the Navigation Pane, with the
    coordination findings dangling under the last severity group.
    """
    if not cross_check_result:
        return

    heading = doc.add_heading("Cross-Spec Coordination", level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT
    heading.paragraph_format.page_break_before = True
    _set_paragraph_outline_level(heading, 0)

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
            f"Sonnet 4.6 coordination analysis — "
            f"{count} issue{'s' if count != 1 else ''} found."
        )
    run.font.size = Pt(11)
    run.font.italic = True
    run.font.color.rgb = RGBColor(128, 128, 128)
    subtitle.paragraph_format.space_after = Pt(12)

    if status in ("skipped", "failed"):
        return

    # Sort by severity, then group by spec file (fileName prefix is the CSI
    # section number, so a lexical sort yields CSI order), then confidence
    # descending — mirrors _write_findings_section so a spec's coordination
    # findings stay contiguous instead of scattering by confidence.
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}
    sorted_findings = sorted(
        cross_check_result.findings,
        key=lambda f: (severity_rank.get(f.severity, 99), f.fileName or "", -f.confidence),
    )

    for idx, finding in enumerate(sorted_findings, 1):
        _write_finding_entry(doc, finding, idx)

    # Coordination summary narrative
    if cross_check_result.thinking:
        doc.add_heading("Coordination Summary", level=2)
        _write_narrative_text(doc, cross_check_result.thinking)


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
) -> Path:
    """Export a complete review report to a Word document.

    Generates a formatted .docx file containing files reviewed, summary
    grid, alerts, per-spec findings, and cross-check findings.

    Each per-spec finding uses Heading 3 for its header line, enabling
    Word's native heading-collapse feature.

    Args:
        pipeline_result: PipelineResult from the review pipeline
        output_path: Path where the .docx file should be saved

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
    # Specs whose review failed/truncated (never produced findings).
    # Defensive getattr keeps legacy callers / test doubles at empty.
    failed_review_specs = [
        str(name)
        for name in (getattr(pipeline_result, "failed_review_specs", None) or [])
    ]
    failed_review_count = len(failed_review_specs)
    _write_title_block(
        doc,
        review,
        pipeline_result.files_reviewed,
        cycle_label=cycle_label,
        failed_review_count=failed_review_count,
    )

    # Run Diagnostics banner. Renders right
    # after the title block so the operational picture (edit-suggested
    # counts, cache replays, verification failures, parse-time
    # demotions, cross-check status) is the first thing a reviewer
    # sees. Derived from data already on the findings + verification
    # stats; no resume state or persistence changes needed.
    run_diagnostics = _summarize_run_diagnostics(
        findings=all_findings,
        status_counts=verification_stats.get("status_counts", {}),
        edit_action_counts=verification_stats.get("edit_action_counts", {}),
        cross_check_result=cross_check,
        pipeline_result=pipeline_result,
    )
    _write_run_diagnostics_banner(doc, run_diagnostics)

    _write_files_reviewed(
        doc,
        pipeline_result.files_reviewed,
        failed_review_specs=set(failed_review_specs),
    )
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

    # Trust-model histogram. Renders right after the severity
    # summary so the reader sees "how many issues are critical?" and
    # "how many of them are actually trustworthy?" together.
    _write_trust_model_summary(
        doc,
        verification_stats.get("status_counts", {}),
        verification_stats.get("edit_action_counts", {}),
    )

    # Fall back to ``getattr`` for the new alert lists so
    # ``_StubPipelineResult`` style ad-hoc test doubles (and any legacy
    # callers that build the result by hand without the new fields)
    # keep working. The real ``PipelineResult`` dataclass always has them.
    _write_alerts(
        doc,
        pipeline_result.leed_alerts,
        pipeline_result.placeholder_alerts,
        code_cycle_alerts=getattr(pipeline_result, "code_cycle_alerts", None),
        structural_alerts=getattr(pipeline_result, "structural_alerts", None),
        naming_alerts=getattr(pipeline_result, "naming_alerts", None),
        template_marker_alerts=getattr(pipeline_result, "template_marker_alerts", None),
        invalid_code_cycle_alerts=getattr(pipeline_result, "invalid_code_cycle_alerts", None),
        duplicate_paragraph_alerts=getattr(pipeline_result, "duplicate_paragraph_alerts", None),
    )
    _write_findings_section(doc, review)
    _write_cross_check_section(doc, cross_check)


    # Save
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))

    return output_path