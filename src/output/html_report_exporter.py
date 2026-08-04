"""Standalone, self-contained HTML report exporter.

An output-only adapter beside the Word exporter: it consumes an
already-completed ``PipelineResult`` (or ``ProgramPipelineResult``)
**read-only** and renders one portable UTF-8 HTML file with every style and
script inlined — no external assets, no network access, no API key. It never
imports or drives the pipeline, never mutates the result, and performs no API
call; the pipeline does not know this module exists.

Content parity: the section walk, gates, strings, value formats, ordering,
and color semantics mirror ``report_exporter`` (the parity contract is
recorded in ``docs/html_report_baseline_evidence.md``). Wherever a value is
*computed* — run-diagnostics counts, verification outcome stats, status /
edit-action classification, pinned-editions prose, cache-age tiers — this
module imports the same pure helpers the Word exporter uses, so the two
reports cannot drift apart on semantics. Rendering (typography, layout,
interactivity) is HTML-native.

Deliberate, documented deviations from the Word report:

* **Alerts are never truncated.** The Word report shows the first 5 alerts
  per file ("... and N more"); the HTML report is the complete-content
  artifact, so every alert renders (grouped per file, identical wording
  otherwise).
* **The cache force-refresh hint names no absolute local path.** The Word
  report interpolates the resolved cache path; a portable HTML file must not
  leak local filesystem paths, so the hint names the setting and its default
  (``SPEC_CRITIC_CACHE_PATH``, ``~/.spec_critic/verification_cache.json``)
  instead.
* **The collapsibility tip is platform-appropriate** (click-to-collapse /
  Expand all / print behavior instead of Word's heading-triangle tip).
* **Source URLs are clickable** (https-only, ``rel="noopener noreferrer"``);
  the Word report renders them as colored text. Non-https "URLs" render as
  plain text, never as links.

Security: every report-derived string is HTML-escaped (body, attributes, and
the embedded JSON payload, which additionally escapes ``</`` so hostile text
cannot close the data block); the single executable inline script is
whitelisted by a Content-Security-Policy hash computed over the exact bytes
written (``write_html_report`` writes in binary so platform newline
translation can never invalidate the hash); ``default-src 'none'`` blocks
every fetch a hostile string could try to smuggle in.
"""
from __future__ import annotations

import base64
import hashlib
import html as _html
import json
from datetime import datetime
from pathlib import Path

from ..core.api_config import (
    CROSS_CHECK_MODEL_DEFAULT,
    web_search_max_uses_for_severity,
)
from ..core.pricing import price_for
from ..core.project_profile import ProjectProfile
from ..modules.registry import get_module
from ..research.requirements_research import (
    PROFILE_CATEGORY_SECTIONS,
    PROFILE_SECTION_ORDER,
    RequirementsProfile,
)
from .report_exporter import (
    CACHE_AGE_COLORS,
    CONFIDENCE_COLORS,
    EDIT_ACTION_COLORS,
    SEVERITY_COLORS,
    SEVERITY_ORDER,
    STATUS_COLORS,
    STATUS_SHADING,
    VERDICT_COLORS,
    VERDICT_ICONS,
    _cache_age_tier,
    _cache_entry_age_days,
    _confidence_tier,
    _COVERAGE_STATUS_STYLES,
    _DRAWING_IMPACT_LEVEL_STYLES,
    _DRAWING_RELATIONSHIP_STYLES,
    _EDITION_CATEGORIES,
    _render_pinned_editions_note,
    _sanitize_markdown_line,
    _summarize_run_diagnostics,
    _summarize_verification_outcomes,
    _verification_mode_label,
)
from .report_status import (
    EDIT_ACTION_DISPLAY_ORDER,
    STATUS_DISPLAY_ORDER,
    classify_edit_action,
    classify_status,
    edit_action_label,
    is_budget_exhausted,
    status_glyph,
    status_label,
    verdict_supersedes_confidence,
)

HTML_REPORT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _e(value: object) -> str:
    """HTML-escape any value for text or attribute position."""
    return _html.escape("" if value is None else str(value), quote=True)


def _css(color: object) -> str:
    """Render a docx RGBColor (hex-string-like) or plain hex string as CSS."""
    text = str(color).strip().lstrip("#")
    return f"#{text}"


def _plural(count: int) -> str:
    return "s" if count != 1 else ""


def _is_safe_link(url: str) -> bool:
    return url.strip().lower().startswith("https://")


def _link_or_text(url: object, css_class: str = "sc-src") -> str:
    """Render a source URL as a safe https link, or inert escaped text."""
    text = "" if url is None else str(url)
    if _is_safe_link(text):
        return (
            f'<a class="{css_class}" href="{_e(text.strip())}" target="_blank" '
            f'rel="noopener noreferrer">{_e(text)}</a>'
        )
    return f'<span class="{css_class}">{_e(text)}</span>'


def _sources_line(urls: list, css_class: str = "sc-src") -> str:
    return '<span class="sc-sep">  •  </span>'.join(
        _link_or_text(u, css_class) for u in urls
    )


def _narrative_paragraphs(text: str) -> list[str]:
    """Mirror ``report_exporter._write_narrative_text`` splitting rules."""
    if not text:
        return []
    chunks = text.split("\n\n") if "\n\n" in text else text.split("\n")
    out = []
    for chunk in chunks:
        cleaned = _sanitize_markdown_line(chunk.strip())
        if cleaned:
            out.append(cleaned)
    return out


def _anchor_for_finding(finding) -> str:
    fid = str(getattr(finding, "finding_id", "") or "")
    return f"f-{fid}" if fid else ""


# ---------------------------------------------------------------------------
# JSON payload (machine-readable mirror for tooling / future report chat)
# ---------------------------------------------------------------------------


def _serialize_verification(vr) -> dict | None:
    if vr is None:
        return None
    return {
        "verdict": getattr(vr, "verdict", "") or "",
        "explanation": getattr(vr, "explanation", "") or "",
        "correction": getattr(vr, "correction", None),
        "grounded": bool(getattr(vr, "grounded", False)),
        "sources": list(getattr(vr, "sources", None) or []),
        "accepted_sources": list(getattr(vr, "accepted_sources", None) or []),
        "rejected_sources": [
            entry if isinstance(entry, dict) else {"url": str(entry), "reason": ""}
            for entry in (getattr(vr, "rejected_sources", None) or [])
        ],
        "fetched_sources": list(getattr(vr, "fetched_sources", None) or []),
        "initial_sources": list(getattr(vr, "initial_sources", None) or []),
        "source_quote": getattr(vr, "source_quote", "") or "",
        "model_used": getattr(vr, "model_used", "") or "",
        "verification_mode": getattr(vr, "verification_mode", "") or "",
        "verification_profile": getattr(vr, "verification_profile", "") or "",
        "web_search_requests": int(getattr(vr, "web_search_requests", 0) or 0),
        "web_fetch_requests": int(getattr(vr, "web_fetch_requests", 0) or 0),
        "cache_status": getattr(vr, "cache_status", "") or "",
        "cache_age_days": _cache_entry_age_days(vr),
        "escalated": bool(getattr(vr, "escalated", False)),
        "escalation_attempted": bool(getattr(vr, "escalation_attempted", False)),
        "initial_model": getattr(vr, "initial_model", "") or "",
        "initial_verdict": getattr(vr, "initial_verdict", "") or "",
        "escalation_changed_verdict": bool(
            getattr(vr, "escalation_changed_verdict", False)
        ),
        "escalation_reason": getattr(vr, "escalation_reason", "") or "",
        "models_disagreed": bool(getattr(vr, "models_disagreed", False)),
        "verification_failed": bool(getattr(vr, "verification_failed", False)),
        "budget_exhausted": bool(getattr(vr, "budget_exhausted", False)),
        "requires_elevated_confidence": bool(
            getattr(vr, "requires_elevated_confidence", False)
        ),
    }


def _serialize_edit_proposal(proposal) -> dict | None:
    if proposal is None:
        return None
    return {
        "action_type": proposal.action_type,
        "existing_text": proposal.existing_text,
        "replacement_text": proposal.replacement_text,
        "anchor_text": proposal.anchor_text,
        "insert_position": proposal.insert_position,
        "target_element_id": proposal.target_element_id,
        "edit_confidence": proposal.edit_confidence,
    }


def _serialize_finding(finding, origin: str) -> dict:
    proposal = (
        finding.as_edit_proposal() if hasattr(finding, "as_edit_proposal") else None
    )
    return {
        "finding_id": str(getattr(finding, "finding_id", "") or ""),
        "origin": origin,
        "severity": getattr(finding, "severity", "") or "",
        "fileName": getattr(finding, "fileName", "") or "",
        "affected_files": list(getattr(finding, "affected_files", None) or []),
        "section": getattr(finding, "section", "") or "",
        "issue": getattr(finding, "issue", "") or "",
        "codeReference": getattr(finding, "codeReference", None),
        "confidence": float(getattr(finding, "confidence", 0.0) or 0.0),
        "actionType": getattr(finding, "actionType", "") or "",
        "demotion_reason": getattr(finding, "demotion_reason", None),
        "report_status": classify_status(finding).value,
        "edit_action": classify_edit_action(finding).value,
        "budget_exhausted": is_budget_exhausted(finding),
        "edit_proposal": _serialize_edit_proposal(proposal),
        "verification": _serialize_verification(getattr(finding, "verification", None)),
    }


def _jsonify(value):
    """Coerce summary dicts (enum keys, nested) into plain-JSON shapes."""
    if isinstance(value, dict):
        return {
            (k.value if hasattr(k, "value") else str(k)): _jsonify(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _build_payload_single(pipeline_result, generated_at: datetime) -> dict:
    review = pipeline_result.review_result
    cross_check = pipeline_result.cross_check_result
    compliance = getattr(pipeline_result, "compliance_result", None)
    findings = [(f, "review") for f in review.findings]
    if cross_check and cross_check.findings:
        findings.extend((f, "cross_check") for f in cross_check.findings)
    if compliance is not None and compliance.findings:
        findings.extend((f, "compliance") for f in compliance.findings)
    stats = _summarize_verification_outcomes([f for f, _ in findings])
    diagnostics = _summarize_run_diagnostics(
        findings=[f for f, _ in findings],
        status_counts=stats.get("status_counts", {}),
        edit_action_counts=stats.get("edit_action_counts", {}),
        cross_check_result=cross_check,
        pipeline_result=pipeline_result,
        compliance_result=compliance,
    )
    drawing = getattr(pipeline_result, "drawing_impact_result", None)
    return {
        "schema_version": HTML_REPORT_SCHEMA_VERSION,
        "report_type": "single",
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M"),
        "module_id": getattr(pipeline_result, "module_id", None),
        "cycle_label": getattr(pipeline_result, "cycle_label", None),
        "project": getattr(pipeline_result, "project_profile", None),
        "files_reviewed": list(pipeline_result.files_reviewed),
        "failed_review_specs": [
            str(n) for n in (getattr(pipeline_result, "failed_review_specs", None) or [])
        ],
        "review": {
            "model": review.model,
            "input_tokens": review.input_tokens,
            "output_tokens": review.output_tokens,
            "elapsed_seconds": review.elapsed_seconds,
            "error": review.error,
            "thinking": review.thinking,
            "severity_counts": {
                "CRITICAL": review.critical_count,
                "HIGH": review.high_count,
                "MEDIUM": review.medium_count,
                "GRIPES": review.gripe_count,
                "TOTAL": review.total_count,
            },
        },
        "total_elapsed_seconds": getattr(pipeline_result, "total_elapsed_seconds", None),
        "verification_stats": _jsonify(stats),
        "run_diagnostics": _jsonify(diagnostics),
        "findings": [_serialize_finding(f, origin) for f, origin in findings],
        "alerts": {
            "leed": list(pipeline_result.leed_alerts or []),
            "placeholder": list(pipeline_result.placeholder_alerts or []),
            "template_marker": list(
                getattr(pipeline_result, "template_marker_alerts", None) or []
            ),
            "code_cycle": list(
                getattr(pipeline_result, "code_cycle_alerts", None) or []
            ),
            "invalid_code_cycle": list(
                getattr(pipeline_result, "invalid_code_cycle_alerts", None) or []
            ),
            "structural": list(
                getattr(pipeline_result, "structural_alerts", None) or []
            ),
            "duplicate_paragraph": list(
                getattr(pipeline_result, "duplicate_paragraph_alerts", None) or []
            ),
            "naming": list(getattr(pipeline_result, "naming_alerts", None) or []),
            "polity": list(getattr(pipeline_result, "polity_alerts", None) or []),
        },
        "cross_check": None
        if cross_check is None
        else {
            "status": getattr(cross_check, "cross_check_status", None),
            "thinking": cross_check.thinking,
            "error": cross_check.error,
            "chunk_failures": int(getattr(cross_check, "chunk_failures", 0) or 0),
            "chunk_skips": int(getattr(cross_check, "chunk_skips", 0) or 0),
        },
        "compliance": None
        if compliance is None
        else {
            "status": getattr(compliance, "cross_check_status", None),
            "thinking": compliance.thinking,
            "error": compliance.error,
            "coverage": list(getattr(compliance, "coverage", None) or []),
        },
        "requirements_profile": getattr(pipeline_result, "requirements_profile", None),
        "drawing_impact": None
        if drawing is None
        else {
            "status": str(getattr(drawing, "status", "") or ""),
            "impact_level": str(getattr(drawing, "impact_level", "") or ""),
            "narrative": str(getattr(drawing, "narrative", "") or ""),
            "links": [
                {
                    "finding_id": str(getattr(link, "finding_id", "") or ""),
                    "relationship": str(getattr(link, "relationship", "") or ""),
                    "explanation": str(getattr(link, "explanation", "") or ""),
                    "sheet_references": [
                        str(r) for r in (getattr(link, "sheet_references", None) or [])
                    ],
                }
                for link in (getattr(drawing, "finding_links", None) or [])
            ],
            "error": str(getattr(drawing, "error", "") or ""),
        },
    }


# ---------------------------------------------------------------------------
# Section renderers. Each returns (html, plaintext_lines) so the visible page
# and the embedded plain-text digest cannot drift.
# ---------------------------------------------------------------------------


def _meta_lines_single(pipeline_result, review, module, profile, generated_at) -> list[str]:
    files_reviewed = pipeline_result.files_reviewed
    failed_review_count = len(
        getattr(pipeline_result, "failed_review_specs", None) or []
    )
    submitted = len(files_reviewed)
    if failed_review_count > 0:
        reviewed = max(0, submitted - failed_review_count)
        files_line = (
            f"Files Reviewed: {reviewed} of {submitted} "
            f"({failed_review_count} failed review)"
        )
    else:
        files_line = f"Files Reviewed: {submitted}"
    cycle_label = getattr(pipeline_result, "cycle_label", "2025") or "2025"
    jurisdiction = module.detector_vocabulary.jurisdiction_label.strip()
    cycle_line = (
        f"Code Cycle: {jurisdiction} {cycle_label}"
        if jurisdiction
        else f"Code Cycle: {cycle_label}"
    )
    lines = [
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M')}",
        f"Model: {review.model}",
        files_line,
        cycle_line,
    ]
    if profile is not None:
        lines.extend(profile.project_meta_lines())
    return lines


def _render_title_block(title: str, meta_lines: list[str]) -> tuple[str, list[str]]:
    parts = [f'<section id="sc-title" class="sc-titleblock">']
    parts.append(f"<h1>{_e(title)}</h1>")
    for line in meta_lines:
        parts.append(f'<p class="sc-meta">{_e(line)}</p>')
    parts.append("</section>")
    return "\n".join(parts), [title, *meta_lines, ""]


def _banner_rows_and_hints(summary: dict) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str]]]:
    """Mirror ``_write_run_diagnostics_banner`` row/hint construction exactly."""
    failed_review_specs = [str(n) for n in (summary.get("failed_review_specs") or [])]
    failed_review_count = int(
        summary.get("failed_review_count", len(failed_review_specs)) or 0
    )
    verification_failed = int(summary.get("verification_failed", 0) or 0)
    cache_count = int(summary.get("cache_replay_count", 0) or 0)
    oldest_age = summary.get("oldest_cache_age_days")
    extraction_warnings = int(summary.get("extraction_warning_count", 0) or 0)
    budget_exhausted_count = int(summary.get("budget_exhausted_count", 0) or 0)
    tracked_changes = int(summary.get("tracked_changes_spec_count", 0) or 0)

    cache_text = (
        f"{cache_count} (oldest {oldest_age}d old)"
        if cache_count > 0 and oldest_age is not None
        else str(cache_count)
    )
    rows: list[tuple[str, str, bool]] = [
        ("Edit suggested", str(summary.get("edit_suggested", 0)), False),
        ("Report-only", str(summary.get("report_only", 0)), False),
        (
            "Specs that failed review (not reviewed)",
            str(failed_review_count),
            failed_review_count > 0,
        ),
        ("Cache replays", cache_text, False),
        (
            "Verification failures (operational)",
            str(verification_failed),
            verification_failed > 0,
        ),
        ("REPORT_ONLY demotions at parse time", str(summary.get("demotion_count", 0)), False),
        (
            "Spec content extraction warnings",
            str(extraction_warnings),
            extraction_warnings > 0,
        ),
        (
            "Budget-exhausted findings",
            str(budget_exhausted_count),
            budget_exhausted_count > 0,
        ),
    ]
    if tracked_changes > 0:
        rows.append(
            (
                "Specs with tracked changes (read as accept-all)",
                str(tracked_changes),
                False,
            )
        )
    cross_check = summary.get("cross_check")
    if cross_check is not None:
        cc_status = cross_check.get("status") or "completed"
        cc_count = int(cross_check.get("finding_count", 0) or 0)
        incomplete = int(cross_check.get("chunk_failures", 0) or 0) + int(
            cross_check.get("chunk_skips", 0) or 0
        )
        if cc_status == "skipped":
            rows.append(("Cross-spec coordination", "skipped", True))
        elif cc_status == "failed":
            rows.append(("Cross-spec coordination", "failed", True))
        else:
            cc_value = f"{cc_count} finding{_plural(cc_count)}"
            highlight = False
            if incomplete > 0:
                cc_value += f" — {incomplete} chunk{_plural(incomplete)} not analyzed"
                highlight = True
            rows.append(("Cross-spec coordination", cc_value, highlight))
    research = summary.get("research")
    if research is not None:
        completed = int(research.get("dimensions_completed", 0) or 0)
        total = int(research.get("dimensions_total", 0) or 0)
        failed = int(research.get("dimensions_failed", 0) or 0)
        items = int(research.get("item_count", 0) or 0)
        ungrounded = int(research.get("ungrounded_count", 0) or 0)
        rows.append(
            (
                "Location/client research",
                f"{completed} of {total} dimensions completed; "
                f"{items} item{_plural(items)} ({ungrounded} ungrounded)",
                failed > 0,
            )
        )
    compliance = summary.get("compliance")
    if compliance is not None:
        comp_status = compliance.get("status") or "completed"
        comp_count = int(compliance.get("finding_count", 0) or 0)
        missing = int(compliance.get("missing", 0) or 0)
        contradicted = int(compliance.get("contradicted", 0) or 0)
        incomplete_chunks = int(compliance.get("chunk_failures", 0) or 0) + int(
            compliance.get("chunk_skips", 0) or 0
        )
        if comp_status in ("skipped", "failed"):
            rows.append(("Local-code compliance", comp_status, True))
        else:
            comp_value = (
                f"{comp_count} finding{_plural(comp_count)} — "
                f"{missing} missing / {contradicted} contradicted"
            )
            if incomplete_chunks > 0:
                comp_value += (
                    f" — {incomplete_chunks} chunk{_plural(incomplete_chunks)} not analyzed"
                )
            rows.append(
                (
                    "Local-code compliance",
                    comp_value,
                    bool(missing or contradicted or incomplete_chunks),
                )
            )
    drawing_impact = summary.get("drawing_impact")
    if drawing_impact is not None:
        if str(drawing_impact.get("status", "")) == "completed":
            di_level = str(drawing_impact.get("impact_level", "") or "minimal")
            di_linked = int(drawing_impact.get("linked_finding_count", 0) or 0)
            rows.append(
                (
                    "Drawing analysis impact",
                    f"{di_level} — {di_linked} finding{_plural(di_linked)} linked",
                    False,
                )
            )
        else:
            rows.append(("Drawing analysis impact", "analysis failed", True))

    hints: list[tuple[str, str]] = []
    if failed_review_count > 0:
        names = ", ".join(failed_review_specs) if failed_review_specs else "(names unavailable)"
        plural = failed_review_count != 1
        hints.append(
            (
                f"⚠ {failed_review_count} spec{'s' if plural else ''} failed review and "
                f"{'were' if plural else 'was'} NOT reviewed: {names}. "
                f"{'Their' if plural else 'Its'} review truncated, failed to parse, or "
                f"errored, so {'they' if plural else 'it'} produced no findings — the "
                f"absence of findings does NOT mean {'they are' if plural else 'it is'} "
                f"compliant. Re-run {'these specs' if plural else 'this spec'} "
                "individually to obtain a review.",
                "#C00000",
            )
        )
    if verification_failed > 0:
        hints.append(
            (
                f"{verification_failed} finding{_plural(verification_failed)} failed "
                "verification due to operational errors (network, rate limit, parse "
                "failures). These are visually marked with the ⚠ glyph in the findings "
                "below. Re-running the review will re-attempt verification for these "
                "findings; the cache does not persist operational-failure results so a "
                "re-run sees them fresh.",
                "#B22222",
            )
        )
    if budget_exhausted_count > 0:
        budget_str = ", ".join(
            f"{sev} {web_search_max_uses_for_severity(sev)}" for sev in SEVERITY_ORDER
        )
        hints.append(
            (
                f"{budget_exhausted_count} finding{_plural(budget_exhausted_count)} "
                "exhausted the verifier's web_search budget without grounding a verdict "
                "(rendered as 'Insufficient evidence (search budget exhausted)' inline). "
                f"Per-severity web_search budgets are {budget_str}; re-running a "
                "lower-severity finding at a higher severity grants more headroom "
                "(CRITICAL findings already receive the maximum budget).",
                "#CC8400",
            )
        )
    if tracked_changes > 0:
        plural = tracked_changes != 1
        hints.append(
            (
                f"{tracked_changes} spec{'s' if plural else ''} contained pending "
                f"tracked changes (Word 'Track Changes'). "
                f"{'They were' if plural else 'It was'} reviewed as if all changes were "
                "accepted — insertions kept, deletions removed — i.e. the text that "
                "will remain once the redline is accepted. Confirm that is the version "
                "you intend to review.",
                "#CC8400",
            )
        )
    if research is not None and int(research.get("dimensions_failed", 0) or 0) > 0:
        failed = int(research.get("dimensions_failed", 0) or 0)
        hints.append(
            (
                f"{failed} location/client research dimension{_plural(failed)} failed, "
                "so the Project Requirements Profile — and every check made against it "
                "— is missing that coverage. The Jurisdiction & Client Requirements "
                "section names the failed dimension(s); re-running the review retries "
                "them.",
                "#CC8400",
            )
        )
    if compliance is not None:
        comp_status = compliance.get("status") or "completed"
        missing = int(compliance.get("missing", 0) or 0)
        contradicted = int(compliance.get("contradicted", 0) or 0)
        if comp_status in ("skipped", "failed"):
            hints.append(
                (
                    f"⚠ The local-code compliance evaluation {comp_status} "
                    f"({compliance.get('reason') or 'no reason recorded'}). The specs "
                    "were NOT evaluated against the researched location/client "
                    "requirements — absence of compliance findings carries no "
                    "information.",
                    "#C00000",
                )
            )
        elif missing or contradicted:
            hints.append(
                (
                    f"The compliance evaluation classified {missing} researched "
                    f"requirement{_plural(missing)} as missing from the package and "
                    f"{contradicted} as contradicted. See the Requirements Coverage "
                    "table and the Local-Code Compliance findings for the specifics.",
                    "#C00000",
                )
            )
    if drawing_impact is not None and str(drawing_impact.get("status", "")) != "completed":
        err = str(drawing_impact.get("error", "") or "")
        hints.append(
            (
                "The drawing-impact analysis did not complete"
                + (f" ({err})" if err else "")
                + ". The attached drawings still informed the review as context; only "
                "the explanatory summary is missing. Re-running the review "
                "re-attempts it.",
                "#CC8400",
            )
        )
    return rows, hints


_BANNER_INTRO = (
    "Operational summary of this run. Use this section to spot at a glance "
    "whether any findings failed verification, were replayed from a stale "
    "cache, or had model-output shape issues that needed parse-time demotion."
)


def _render_run_diagnostics(summary: dict, *, heading_level: int = 2) -> tuple[str, list[str]]:
    rows, hints = _banner_rows_and_hints(summary)
    h = f"h{heading_level}"
    parts = ['<section id="sc-diagnostics">']
    parts.append(f"<{h}>Run Diagnostics</{h}>")
    parts.append(f'<p class="sc-note">{_e(_BANNER_INTRO)}</p>')
    parts.append('<div class="sc-tablewrap"><table class="sc-banner">')
    for label, value, highlight in rows:
        cls = ' class="sc-flag"' if highlight else ""
        parts.append(
            f"<tr><th>{_e(label)}</th><td{cls}>{_e(value)}</td></tr>"
        )
    parts.append("</table></div>")
    for text, color in hints:
        parts.append(f'<p class="sc-hint" style="color:{color}">{_e(text)}</p>')
    parts.append("</section>")
    text_lines = ["Run Diagnostics", _BANNER_INTRO]
    text_lines.extend(f"  {label}: {value}" for label, value, _ in rows)
    text_lines.extend(f"  {text}" for text, _ in hints)
    text_lines.append("")
    return "\n".join(parts), text_lines


def _render_files_reviewed(
    files_reviewed: list[str],
    failed_review_specs: set[str],
    *,
    heading_level: int = 2,
    id_prefix: str = "",
) -> tuple[str, list[str]]:
    h = f"h{heading_level}"
    parts = [f'<section id="{id_prefix}sc-files">', f"<{h}>Files Reviewed</{h}>", "<ul>"]
    text_lines = ["Files Reviewed"]
    for filename in files_reviewed:
        if filename in failed_review_specs:
            annotated = f"{filename} — review failed (not reviewed)"
            parts.append(f'<li class="sc-failedspec">{_e(annotated)}</li>')
            text_lines.append(f"  - {annotated}")
        else:
            parts.append(f"<li>{_e(filename)}</li>")
            text_lines.append(f"  - {filename}")
    parts.append("</ul></section>")
    text_lines.append("")
    return "\n".join(parts), text_lines


def _requirements_detail_line(item) -> str:
    parts = []
    if item.authority:
        parts.append(f"Authority: {item.authority}")
    if item.code_reference:
        parts.append(f"Ref: {item.code_reference}")
    if item.accepted_sources:
        parts.append("Sources: " + ", ".join(item.accepted_sources))
    parts.append(f"confidence {round(item.confidence * 100)}%")
    return " • ".join(parts)


def _render_requirements_section(
    requirements_profile: RequirementsProfile,
    compliance_result,
    *,
    heading_level: int = 2,
    id_prefix: str = "",
) -> tuple[str, list[str]]:
    h = f"h{heading_level}"
    h2 = f"h{heading_level + 1}"
    parts = [f'<section id="{id_prefix}sc-requirements">']
    text_lines: list[str] = []
    parts.append(f"<{h}>Jurisdiction &amp; Client Requirements</{h}>")
    text_lines.append("Jurisdiction & Client Requirements")

    project = ProjectProfile.from_dict(requirements_profile.project)
    total = len(requirements_profile.dimension_statuses)
    searches = sum(
        s.web_search_requests for s in requirements_profile.dimension_statuses
    )
    identity = (
        f"Project: {project.city}, {project.state_display}, "
        f"{project.country_display} — Client: {project.client_name}. "
        if project is not None
        else ""
    )
    intro = (
        f"{identity}Requirements researched via location/client web research "
        f"({requirements_profile.completed_dimensions} of {total} dimensions "
        f"completed, {searches} web searches), researched "
        f"{requirements_profile.research_date}. Edition and process facts are "
        "as-of that date. Items marked [UNVERIFIED] could not be grounded in "
        "retrieved sources and are never treated as controlling."
    )
    parts.append(f'<p class="sc-note">{_e(intro)}</p>')
    text_lines.append(intro)

    failed_dimensions = [
        s for s in requirements_profile.dimension_statuses if s.status != "completed"
    ]
    if failed_dimensions:
        warning = (
            f"⚠ {len(failed_dimensions)} research dimension(s) failed "
            f"({', '.join(s.dimension_id for s in failed_dimensions)}); the "
            "profile below is missing their requirements."
        )
        parts.append(f'<p class="sc-hint" style="color:#C00000">{_e(warning)}</p>')
        text_lines.append(warning)

    spec_items = [
        i for i in requirements_profile.items if not i.is_process_advisory
    ]
    sections: dict[str, list] = {name: [] for name in PROFILE_SECTION_ORDER}
    for item in spec_items:
        sections[PROFILE_CATEGORY_SECTIONS.get(item.category, "OTHER")].append(item)
    for section_name in PROFILE_SECTION_ORDER:
        section_items = sections[section_name]
        if not section_items:
            continue
        parts.append(f"<{h2}>{_e(section_name.title())}</{h2}>")
        text_lines.append(section_name.title())
        parts.append("<ul>")
        for item in section_items:
            marker = (
                '<span class="sc-unverified">  [UNVERIFIED]</span>'
                if not item.grounded
                else ""
            )
            detail = _requirements_detail_line(item)
            parts.append(
                "<li>"
                f'<span class="sc-itemid">[{_e(item.item_id)}] </span>'
                f"{_e(item.requirement)}{marker}"
                f'<br><span class="sc-detail">{_e(detail)}</span>'
                "</li>"
            )
            text_lines.append(
                f"  - [{item.item_id}] {item.requirement}"
                + ("  [UNVERIFIED]" if not item.grounded else "")
            )
            text_lines.append(f"    {detail}")
        parts.append("</ul>")

    edition_items = [i for i in spec_items if i.category in _EDITION_CATEGORIES]
    if edition_items:
        parts.append(f"<{h2}>Adopted &amp; Referenced Editions</{h2}>")
        text_lines.append("Adopted & Referenced Editions")
        note = (
            f"Edition facts researched {requirements_profile.research_date}; "
            "verify against the adopting instrument before relying on them."
        )
        parts.append(f'<p class="sc-note">{_e(note)}</p>')
        text_lines.append(note)
        parts.append('<div class="sc-tablewrap"><table class="sc-grid">')
        parts.append(
            "<tr><th>Topic</th><th>Requirement / edition</th>"
            "<th>Authority &amp; reference</th></tr>"
        )
        for item in edition_items:
            requirement = item.requirement + (
                "" if item.grounded else "  [UNVERIFIED]"
            )
            authority = "; ".join(
                p for p in (item.authority, item.code_reference) if p
            )
            parts.append(
                f"<tr><td>{_e(item.topic or item.category)}</td>"
                f"<td>{_e(requirement)}</td><td>{_e(authority)}</td></tr>"
            )
            text_lines.append(
                f"  {item.topic or item.category} | {requirement} | {authority}"
            )
        parts.append("</table></div>")

    coverage = list(getattr(compliance_result, "coverage", None) or [])
    if coverage:
        items_by_id = {i.item_id: i for i in requirements_profile.items}
        parts.append(f"<{h2}>Requirements Coverage</{h2}>")
        text_lines.append("Requirements Coverage")
        parts.append('<div class="sc-tablewrap"><table class="sc-grid">')
        parts.append("<tr><th>Requirement</th><th>Coverage</th><th>Evidence</th></tr>")
        for entry in coverage:
            rid = entry.get("requirement_id") or ""
            item = items_by_id.get(rid)
            requirement_text = item.requirement if item is not None else "(unknown requirement)"
            status = str(entry.get("status") or "unclear")
            label, fill, text_color = _COVERAGE_STATUS_STYLES.get(
                status, _COVERAGE_STATUS_STYLES["unclear"]
            )
            evidence = " — ".join(
                p for p in (entry.get("evidence"), entry.get("fileName")) if p
            )
            parts.append(
                f"<tr><td>[{_e(rid)}] {_e(requirement_text)}</td>"
                f'<td style="background:{_css(fill)};color:{_css(text_color)};'
                f'font-weight:bold">{_e(label)}</td>'
                f"<td>{_e(evidence)}</td></tr>"
            )
            text_lines.append(f"  [{rid}] {requirement_text} | {label} | {evidence}")
        parts.append("</table></div>")
    elif compliance_result is not None and (
        (getattr(compliance_result, "cross_check_status", None) or "completed")
        in ("skipped", "failed")
    ):
        status = getattr(compliance_result, "cross_check_status", None) or "completed"
        reason = (
            getattr(compliance_result, "thinking", "")
            if status == "skipped"
            else getattr(compliance_result, "error", "")
        )
        unavailable = (
            f"⚠ Compliance coverage unavailable — the compliance pass {status}: {reason}"
        )
        parts.append(f'<p class="sc-hint" style="color:#C00000">{_e(unavailable)}</p>')
        text_lines.append(unavailable)

    advisories = [i for i in requirements_profile.items if i.is_process_advisory]
    if advisories:
        parts.append(f"<{h2}>Process &amp; Schedule Advisories</{h2}>")
        text_lines.append("Process & Schedule Advisories")
        note = (
            "Permit, schedule, and process facts the project team must act on. "
            "These are not specification content and are never counted as "
            "missing spec coverage."
        )
        parts.append(f'<p class="sc-note">{_e(note)}</p>')
        text_lines.append(note)
        parts.append("<ul>")
        for item in advisories:
            marker = (
                '<span class="sc-unverified">  [UNVERIFIED]</span>'
                if not item.grounded
                else ""
            )
            detail = _requirements_detail_line(item)
            parts.append(
                f"<li>{_e(item.requirement)}{marker}"
                f'<br><span class="sc-detail">{_e(detail)}</span></li>'
            )
            text_lines.append(
                f"  - {item.requirement}"
                + ("  [UNVERIFIED]" if not item.grounded else "")
            )
            text_lines.append(f"    {detail}")
        parts.append("</ul>")
    parts.append("</section>")
    text_lines.append("")
    return "\n".join(parts), text_lines


def _methodology_paragraphs(
    *,
    cross_check_enabled: bool,
    cycle_label: str,
    cross_check_status: str | None,
    cross_check_reason: str,
    verification_stats: dict | None,
    module,
) -> list[str]:
    """Mirror ``_write_methodology_note`` prose (with the HTML-native tip)."""
    domain_phrase = module.report_context_phrase
    para1 = (
        "This report was generated by Spec Critic, an AI-assisted specification "
        "review tool. Each specification was analyzed by Claude for code "
        "compliance issues, coordination problems, and technical errors relevant "
        f"to {domain_phrase}. Findings are classified by severity (Critical, "
        "High, Medium, Gripe) and assigned a confidence score reflecting the "
        "review model’s certainty at review time, before verification. The "
        "confidence score and the verification verdict are distinct signals: "
        "confidence is the review model’s own pre-verification estimate, "
        "while the verdict is a separate verification pass’s grounded "
        "assessment. For any finding the verifier went on to confirm, correct, "
        "contest, or dispute, the verification verdict — not the review "
        "confidence — is the authoritative trust signal, so the report "
        "de-emphasizes the confidence on those findings (shown small beside the "
        "status, marked “pre-verification”) and lets the verdict stand "
        "as the headline. The confidence score stays prominent only where "
        "verification did not reach a verdict (for example, not checked or "
        "insufficient evidence)."
    )
    stats = verification_stats or {}
    if stats.get("all_unverified"):
        para2 = (
            "Verification was attempted but did not return usable results. "
            "Findings have not been independently verified."
        )
    elif stats.get("partial_unverified"):
        para2 = (
            "Findings were checked in a secondary AI verification pass with web "
            "search access. Some findings could not be verified — see individual "
            "verdicts."
        )
    elif int(stats.get("with_verification", 0) or 0) > 0:
        para2 = (
            "All findings were checked in a secondary AI verification pass with "
            "web search access. Verification verdicts (Confirmed, Corrected, "
            "Disputed, or Unverified) reflect the verifier model's assessment "
            "and should be treated as advisory."
        )
    else:
        para2 = (
            "No verification outcomes were recorded for this run. Findings "
            "should be treated as unverified unless noted otherwise."
        )
    jurisdiction = module.detector_vocabulary.jurisdiction_label.strip()
    cycle_phrase = f"{jurisdiction} {cycle_label}" if jurisdiction else cycle_label
    para2 += f" This review used {cycle_phrase} code cycle references."
    if cross_check_enabled:
        if cross_check_status == "completed":
            para2 += " Cross-check completed."
        elif cross_check_status == "skipped":
            para2 += f" Cross-check was skipped: {cross_check_reason}"
        elif cross_check_status == "failed":
            para2 += f" Cross-check failed: {cross_check_reason}"
    para2 += (
        " This report is advisory — findings should be reviewed by the engineer "
        "of record before acting on them."
    )
    paragraphs = [para1, para2]
    pinning_text = _render_pinned_editions_note(module.cycle, jurisdiction)
    if pinning_text:
        paragraphs.append(pinning_text)
    paragraphs.append(
        "Tip: Click a finding's header line to collapse or expand it, or use "
        "the Expand all / Collapse all controls in the toolbar. Printing the "
        "report automatically expands every finding."
    )
    return paragraphs


def _render_methodology(paragraphs: list[str], *, heading_level: int = 2, id_prefix: str = "") -> tuple[str, list[str]]:
    h = f"h{heading_level}"
    parts = [f'<section id="{id_prefix}sc-methodology">', f"<{h}>About This Review</{h}>"]
    for para in paragraphs:
        parts.append(f"<p>{_e(para)}</p>")
    parts.append("</section>")
    return "\n".join(parts), ["About This Review", *paragraphs, ""]


def _render_drawing_impact(drawing_impact, findings_by_id: dict, *, heading_level: int = 2) -> tuple[str, list[str]]:
    h = f"h{heading_level}"
    h2 = f"h{heading_level + 1}"
    parts = ['<section id="sc-drawing-impact">']
    text_lines: list[str] = []
    parts.append(f"<{h}>How the Drawings Informed This Review</{h}>")
    text_lines.append("How the Drawings Informed This Review")
    intro = (
        "Construction drawings were attached to this run and analyzed into a "
        "text digest that was supplied as reference context to every stage of "
        "the review. This section reports how that drawing content bore on the "
        "findings — where the drawings corroborate, contradict, or add context "
        "to a finding, cited to the drawing sheet pages."
    )
    parts.append(f'<p class="sc-note">{_e(intro)}</p>')
    text_lines.append(intro)
    status = str(getattr(drawing_impact, "status", "") or "")
    if status != "completed":
        err = str(getattr(drawing_impact, "error", "") or "")
        note = (
            "⚠ The drawing-impact analysis did not complete"
            + (f" ({err})" if err else "")
            + ". The drawings were still provided to the review as context; this "
            "run simply could not produce the explanatory summary. Re-running "
            "the review will re-attempt it."
        )
        parts.append(f'<p class="sc-hint" style="color:#C00000">{_e(note)}</p>')
        parts.append("</section>")
        text_lines.extend([note, ""])
        return "\n".join(parts), text_lines

    level = str(getattr(drawing_impact, "impact_level", "") or "").strip().lower()
    label, color = _DRAWING_IMPACT_LEVEL_STYLES.get(
        level, _DRAWING_IMPACT_LEVEL_STYLES["minimal"]
    )
    parts.append(
        '<p class="sc-impact"><strong>Overall drawing impact: </strong>'
        f'<strong style="color:{_css(color)}">{_e(label)}</strong></p>'
    )
    text_lines.append(f"Overall drawing impact: {label}")
    narrative = str(getattr(drawing_impact, "narrative", "") or "").strip()
    for chunk in narrative.split("\n\n"):
        chunk = chunk.strip()
        if chunk:
            parts.append(f"<p>{_e(chunk)}</p>")
            text_lines.append(chunk)
    links = list(getattr(drawing_impact, "finding_links", None) or [])
    if links:
        parts.append(f"<{h2}>Findings the drawings bear on</{h2}>")
        text_lines.append("Findings the drawings bear on")
        parts.append("<ul>")
        for link in links:
            relationship = str(getattr(link, "relationship", "") or "")
            rel_label, rel_color = _DRAWING_RELATIONSHIP_STYLES.get(
                relationship, _DRAWING_RELATIONSHIP_STYLES["contextualized"]
            )
            fid = str(getattr(link, "finding_id", "") or "")
            finding = findings_by_id.get(fid)
            severity = getattr(finding, "severity", "") or "" if finding else ""
            file_name = getattr(finding, "fileName", "") or "" if finding else ""
            issue = getattr(finding, "issue", "") or "" if finding else ""
            meta_bits = [b for b in (severity, file_name) if b]
            meta = f"  [{fid}]" + (f" — {' · '.join(meta_bits)}" if meta_bits else "")
            snippet = issue.strip()
            if len(snippet) > 200:
                snippet = snippet[:199] + "…"
            explanation = str(getattr(link, "explanation", "") or "").strip()
            refs = [
                str(r)
                for r in (getattr(link, "sheet_references", None) or [])
                if str(r).strip()
            ]
            detail_parts = [p for p in (explanation, "Sheets: " + ", ".join(refs) if refs else "") if p]
            fid_html = (
                f'<a href="#f-{_e(fid)}">[{_e(fid)}]</a>' if fid and finding else f"[{_e(fid)}]"
            )
            li = (
                f'<strong style="color:{_css(rel_color)}">{_e(rel_label)}</strong>'
                f'<span class="sc-detail">  {fid_html}'
                + (f" — {_e(' · '.join(meta_bits))}" if meta_bits else "")
                + "</span>"
            )
            if snippet:
                li += f"<br>{_e(snippet)}"
            if detail_parts:
                li += f'<br><span class="sc-detail">{_e(" • ".join(detail_parts))}</span>'
            parts.append(f"<li>{li}</li>")
            text_lines.append(f"  - {rel_label}{meta}")
            if snippet:
                text_lines.append(f"    {snippet}")
            if detail_parts:
                text_lines.append(f"    {' • '.join(detail_parts)}")
        parts.append("</ul>")
    else:
        none_note = (
            "No individual finding turned on drawing content — on this run the "
            "drawings served as background context rather than the basis for a "
            "specific finding."
        )
        parts.append(f'<p class="sc-note">{_e(none_note)}</p>')
        text_lines.append(none_note)
    parts.append("</section>")
    text_lines.append("")
    return "\n".join(parts), text_lines


_SUMMARY_HEX = {
    "CRITICAL": "C00000",
    "HIGH": "FF6600",
    "MEDIUM": "C09800",
    "GRIPES": "800080",
    "TOTAL": "333333",
    "CROSS-CHECK": "06B6D4",
}


def _render_summary(review, cross_check_result, *, total_elapsed_seconds, heading_level: int = 2, id_prefix: str = "") -> tuple[str, list[str]]:
    h = f"h{heading_level}"
    cc_count = (
        len(cross_check_result.findings)
        if cross_check_result and cross_check_result.findings
        else 0
    )
    columns = [
        ("CRITICAL", review.critical_count),
        ("HIGH", review.high_count),
        ("MEDIUM", review.medium_count),
        ("GRIPES", review.gripe_count),
        ("TOTAL", review.total_count),
    ]
    if cc_count > 0:
        columns.append(("CROSS-CHECK", cc_count))
    parts = [f'<section id="{id_prefix}sc-summary">', f"<{h}>Summary</{h}>"]
    text_lines = ["Summary"]
    parts.append('<div class="sc-tablewrap"><table class="sc-severity">')
    header_cells = []
    count_cells = []
    for label, count in columns:
        hexcode = _SUMMARY_HEX[label]
        text_color = "#000000" if label == "MEDIUM" else "#FFFFFF"
        header_cells.append(
            f'<th style="background:#{hexcode};color:{text_color}">{_e(label)}</th>'
        )
        count_cells.append(f'<td class="sc-bigcount">{count}</td>')
        text_lines.append(f"  {label}: {count}")
    parts.append("<tr>" + "".join(header_cells) + "</tr>")
    parts.append("<tr>" + "".join(count_cells) + "</tr>")
    parts.append("</table></div>")

    token_line = f"{review.input_tokens:,} input → {review.output_tokens:,} output"
    parts.append(
        f"<p><strong>Review Stage Tokens: </strong>{_e(token_line)}</p>"
    )
    text_lines.append(f"Review Stage Tokens: {token_line}")
    note_line = (
        "Cross-check and verification token usage are not included in the "
        "totals above."
    )
    parts.append(f"<p><strong>Note: </strong>{_e(note_line)}</p>")
    text_lines.append(f"Note: {note_line}")
    processing_seconds = (
        total_elapsed_seconds
        if total_elapsed_seconds is not None
        else review.elapsed_seconds
    )
    parts.append(
        f"<p><strong>Processing Time: </strong>{processing_seconds:.1f} seconds</p>"
    )
    text_lines.append(f"Processing Time: {processing_seconds:.1f} seconds")

    all_findings = list(review.findings)
    if cross_check_result and cross_check_result.findings:
        all_findings.extend(cross_check_result.findings)
    verdicts: dict[str, int] = {}
    for f in all_findings:
        if f.verification:
            verdicts[f.verification.verdict] = verdicts.get(f.verification.verdict, 0) + 1
    if verdicts:
        verdict_line = ", ".join(
            f"{verdicts[v]} {v.lower()}"
            for v in ["CONFIRMED", "CORRECTED", "DISPUTED", "UNVERIFIED"]
            if v in verdicts
        )
        parts.append(f"<p><strong>Verification: </strong>{_e(verdict_line)}</p>")
        text_lines.append(f"Verification: {verdict_line}")
        if verdicts.get("UNVERIFIED", 0) > 0:
            unverified_findings = [
                f
                for f in all_findings
                if str(getattr(f.verification, "verdict", "") or "").upper()
                == "UNVERIFIED"
            ]
            status_counts: dict = {}
            for f in unverified_findings:
                status = classify_status(f)
                status_counts[status] = status_counts.get(status, 0) + 1
            breakdown = [
                f"{status_counts[s]} {status_label(s).lower()}"
                for s in STATUS_DISPLAY_ORDER
                if status_counts.get(s, 0) > 0
            ]
            if breakdown:
                breakdown_line = (
                    "Of the unverified-verdict findings: " + ", ".join(breakdown) + "."
                )
                parts.append(f'<p class="sc-note">{_e(breakdown_line)}</p>')
                text_lines.append(breakdown_line)
    parts.append("</section>")
    text_lines.append("")
    return "\n".join(parts), text_lines


_TRUST_INTRO = (
    "Every finding is tagged with a trust status and an edit-action label. The "
    "two histograms below give the at-a-glance picture; individual findings "
    "carry the same labels inline."
)


def _render_trust_model(status_counts: dict, edit_action_counts: dict, *, heading_level: int = 2) -> tuple[str, list[str]]:
    total = sum(status_counts.values()) if status_counts else 0
    if total == 0:
        return "", []
    h = f"h{heading_level}"
    parts = ['<section id="sc-trust">', f"<{h}>Trust Model Summary</{h}>"]
    text_lines = ["Trust Model Summary", _TRUST_INTRO]
    parts.append(f'<p class="sc-note">{_e(_TRUST_INTRO)}</p>')
    present = [s for s in STATUS_DISPLAY_ORDER if status_counts.get(s, 0) > 0]
    if present:
        parts.append('<div class="sc-tablewrap"><table class="sc-severity">')
        parts.append(
            "<tr>"
            + "".join(
                f'<th style="background:{_css(STATUS_SHADING[s])};color:#FFFFFF">'
                f"{_e(status_label(s))}</th>"
                for s in present
            )
            + "</tr>"
        )
        parts.append(
            "<tr>"
            + "".join(
                f'<td class="sc-bigcount" style="color:{_css(STATUS_COLORS[s])}">'
                f"{status_counts.get(s, 0)}</td>"
                for s in present
            )
            + "</tr>"
        )
        parts.append("</table></div>")
        text_lines.extend(
            f"  {status_label(s)}: {status_counts.get(s, 0)}" for s in present
        )
    action_bits = []
    action_text = []
    for action in EDIT_ACTION_DISPLAY_ORDER:
        count = edit_action_counts.get(action, 0)
        if count > 0:
            action_bits.append(
                f'<strong style="color:{_css(EDIT_ACTION_COLORS[action])}">'
                f"{count} {_e(edit_action_label(action).lower())}</strong>"
            )
            action_text.append(f"{count} {edit_action_label(action).lower()}")
    if action_bits:
        parts.append(
            "<p><strong>Edit eligibility: </strong>"
            + '<span class="sc-sep">  •  </span>'.join(action_bits)
            + "</p>"
        )
        text_lines.append("Edit eligibility: " + "  •  ".join(action_text))
    parts.append("</section>")
    text_lines.append("")
    return "\n".join(parts), text_lines


_ALERTS_INTRO = (
    "These items were detected locally by deterministic rules; no LLM tokens "
    "were spent on them. They sit alongside the LLM findings below."
)


def _alert_sections_spec(module) -> list[tuple[str, str, str]]:
    """The nine (key, title, description) alert sections in DOCX order."""
    vocab = module.detector_vocabulary
    jurisdiction = vocab.jurisdiction_label.strip()
    j_prefix = f"{jurisdiction} " if jurisdiction else ""
    published_years = ", ".join(vocab.plausible_cycle_years)
    from .report_exporter import _uniform_cycle_cadence

    cadence = _uniform_cycle_cadence(vocab.plausible_cycle_years)
    if published_years and jurisdiction and cadence:
        known_years = (
            f" ({jurisdiction} publishes cycles every {cadence} years: "
            f"{published_years})"
        )
    elif published_years:
        known_years = f" (known cycle years: {published_years})"
    else:
        known_years = ""
    return [
        (
            "leed",
            "LEED References Detected",
            "The following LEED references were found. Since this is not a "
            "LEED project, these should be removed:",
        ),
        (
            "placeholder",
            "Unresolved Placeholders",
            "The following editorial placeholders need to be resolved:",
        ),
        (
            "template_marker",
            "Unresolved Template Markers",
            "The following TODO / FIXME / XXX / ??? markers are still in the "
            "spec and should be resolved before issuing:",
        ),
        (
            "code_cycle",
            f"Stale {j_prefix}Code Cycle References",
            f"The following references cite a historical {j_prefix}code cycle "
            "rather than the one selected for this review:",
        ),
        (
            "invalid_code_cycle",
            f"Invalid {j_prefix}Code Cycle Years",
            f"The following references cite a year that is not a real "
            f"{j_prefix}code cycle{known_years}. These are likely typos:",
        ),
        (
            "structural",
            "Structural Issues",
            "Empty sections and duplicate section headings detected in the "
            "spec body:",
        ),
        (
            "duplicate_paragraph",
            "Duplicate Paragraphs",
            "These paragraphs appear verbatim more than once in the same spec "
            "— likely copy-paste mistakes:",
        ),
        (
            "naming",
            "Inconsistent Filenames",
            "These files use a CSI naming style that differs from the "
            "project's dominant style:",
        ),
        (
            "polity",
            "Wrong-Polity Tokens",
            "These tokens belong to a different country's code/regulatory "
            "regime than the project location. Each entry notes why the token "
            "is suspicious for this project's country:",
        ),
    ]


def _render_alerts(alert_lists: dict[str, list[dict]], module, *, heading_level: int = 2, id_prefix: str = "") -> tuple[str, list[str]]:
    if not any(alert_lists.get(key) for key, _, _ in _alert_sections_spec(module)):
        return "", []
    h = f"h{heading_level}"
    h2 = f"h{heading_level + 1}"
    parts = [f'<section id="{id_prefix}sc-alerts">', f"<{h}>Alerts</{h}>"]
    text_lines = ["Alerts", _ALERTS_INTRO]
    parts.append(f'<p class="sc-note">{_e(_ALERTS_INTRO)}</p>')
    for key, title, description in _alert_sections_spec(module):
        alerts = alert_lists.get(key) or []
        if not alerts:
            continue
        heading = f"{title} (deterministic check)"
        parts.append(f"<{h2}>{_e(heading)}</{h2}>")
        parts.append(f'<p class="sc-note">{_e(description)}</p>')
        text_lines.extend([heading, description])
        by_file: dict[str, list[dict]] = {}
        for alert in alerts:
            by_file.setdefault(alert.get("filename", ""), []).append(alert)
        for filename, items in by_file.items():
            parts.append(f"<p><strong>{_e(filename)}</strong></p>")
            text_lines.append(f"  {filename}")
            parts.append("<ul>")
            # Deliberate deviation from the Word report: every alert renders
            # (the DOCX truncates at 5 per file); the HTML is the
            # complete-content artifact.
            for alert in items:
                entry = alert.get("context", alert.get("match", ""))
                parts.append(f"<li>{_e(entry)}</li>")
                text_lines.append(f"    - {entry}")
            parts.append("</ul>")
    parts.append("</section>")
    text_lines.append("")
    return "\n".join(parts), text_lines


# ---------------------------------------------------------------------------
# Finding cards
# ---------------------------------------------------------------------------


def _render_evidence_panel(finding, vr) -> tuple[str, list[str]]:
    parts = ['<details class="sc-evidence"><summary>Sources</summary>']
    text_lines = ["  Sources:"]
    model_used = (getattr(vr, "model_used", "") or "").strip()
    if model_used:
        parts.append(
            f'<p><strong>Verifier model: </strong>{_e(model_used)}</p>'
        )
        text_lines.append(f"    Verifier model: {model_used}")
    mode_raw = getattr(vr, "verification_mode", "") or ""
    mode_label = _verification_mode_label(mode_raw)
    if mode_label:
        parts.append(
            f"<p><strong>Verification mode: </strong>{_e(mode_label)}</p>"
        )
        text_lines.append(f"    Verification mode: {mode_label}")
    if (mode_raw or "").strip().lower() != "local_skip":
        searches = int(getattr(vr, "web_search_requests", 0) or 0)
        fetches = int(getattr(vr, "web_fetch_requests", 0) or 0)
        budget = web_search_max_uses_for_severity(getattr(finding, "severity", None))
        if fetches > 0:
            budget_text = (
                f"Searches: {searches} of {budget}, Full-page fetches: {fetches}"
            )
        else:
            budget_text = f"{searches} of {budget} searches used"
        parts.append(
            f"<p><strong>Search budget used: </strong>{_e(budget_text)}</p>"
        )
        text_lines.append(f"    Search budget used: {budget_text}")
    source_quote = (getattr(vr, "source_quote", "") or "").strip()
    if source_quote:
        parts.append(
            "<p><strong>Source quote (verbatim from search result):</strong></p>"
            f'<blockquote class="sc-quote">{_e(source_quote)}</blockquote>'
        )
        text_lines.append(
            f"    Source quote (verbatim from search result): {source_quote}"
        )
    explanation = (getattr(vr, "explanation", "") or "").strip()
    if explanation:
        parts.append(
            f"<p><strong>Verification rationale: </strong>{_e(explanation)}</p>"
        )
        text_lines.append(f"    Verification rationale: {explanation}")
    if bool(getattr(vr, "escalation_attempted", False)):
        initial_verdict = getattr(vr, "initial_verdict", "") or ""
        initial_model = getattr(vr, "initial_model", "") or ""
        final_verdict = getattr(vr, "verdict", "") or ""
        final_model = model_used
        escalation_reason = getattr(vr, "escalation_reason", "") or ""
        changed = bool(getattr(vr, "escalation_changed_verdict", False))
        disagreed = bool(getattr(vr, "models_disagreed", False))
        sentence_parts = []
        if initial_verdict:
            bit = f"Initial verdict: {initial_verdict}"
            if initial_model:
                bit += f" from {initial_model}"
            sentence_parts.append(bit)
        if final_verdict:
            bit = f"Final verdict: {final_verdict}"
            if final_model:
                bit += f" from {final_model}"
            sentence_parts.append(bit)
        sentence = " → ".join(sentence_parts) if sentence_parts else "escalated"
        if escalation_reason:
            sentence += f". Reason: {escalation_reason}"
        if disagreed:
            sentence += (
                " The two models disagreed on this finding while both produced "
                "grounded verdicts; manual review recommended."
            )
        elif changed:
            sentence += " (models disagreed)"
        sentence += "."
        color = "#800080" if disagreed else ("#B22222" if changed else "#646464")
        parts.append(
            f'<p style="color:{color}"><strong>Escalation history: </strong>'
            f"{_e(sentence)}</p>"
        )
        text_lines.append(f"    Escalation history: {sentence}")
        initial_sources = list(getattr(vr, "initial_sources", []) or [])
        if disagreed and initial_sources:
            label = f"Initial verifier sources ({initial_model or 'initial pass'}):"
            parts.append(
                f'<p class="sc-srclabel" style="color:#800080"><strong>{_e(label)}'
                f"</strong><br>{_sources_line(initial_sources)}</p>"
            )
            text_lines.append(f"    {label} " + ", ".join(initial_sources))
    accepted = list(vr.sources or [])
    if accepted:
        label = "Web/code evidence (cited and found in search results):"
        parts.append(
            f'<p class="sc-srclabel" style="color:#006400"><strong>{_e(label)}'
            f"</strong><br>{_sources_line(accepted)}</p>"
        )
        text_lines.append(f"    {label} " + ", ".join(accepted))
    rejected = list(getattr(vr, "rejected_sources", []) or [])
    if rejected:
        label = (
            "Unsupported / rejected sources (cited by the model but not present "
            "in web_search results):"
        )
        bits = []
        text_bits = []
        for entry in rejected:
            url = entry.get("url") if isinstance(entry, dict) else str(entry)
            reason = entry.get("reason") if isinstance(entry, dict) else ""
            url_text = url or "(empty)"
            bit = f'<span class="sc-rejected">{_e(url_text)}</span>'
            if reason:
                bit += f'<span style="color:#C00000"> [{_e(reason)}]</span>'
            bits.append(bit)
            text_bits.append(url_text + (f" [{reason}]" if reason else ""))
        parts.append(
            f'<p class="sc-srclabel" style="color:#C00000"><strong>{_e(label)}'
            "</strong><br>"
            + '<span class="sc-sep">  •  </span>'.join(bits)
            + "</p>"
        )
        text_lines.append(f"    {label} " + ", ".join(text_bits))
    fetched = list(getattr(vr, "fetched_sources", []) or [])
    if fetched:
        label = "Full-text sources consulted (retrieved via web_fetch):"
        parts.append(
            f'<p class="sc-srclabel" style="color:#006400"><strong>{_e(label)}'
            f"</strong><br>{_sources_line(fetched)}</p>"
        )
        text_lines.append(f"    {label} " + ", ".join(fetched))
    if (getattr(vr, "cache_status", "") or "") == "hit":
        # Deliberate deviation from the Word report: no absolute local path in
        # a portable file — name the setting and its default instead.
        hint = (
            "To force re-verification of this finding, delete its entry from "
            "the verification cache file (SPEC_CRITIC_CACHE_PATH, default "
            "~/.spec_critic/verification_cache.json)."
        )
        parts.append(f'<p class="sc-detail">{_e(hint)}</p>')
        text_lines.append(f"    {hint}")
    parts.append("</details>")
    return "\n".join(parts), text_lines


def _render_finding_entry(finding, index: int) -> tuple[str, list[str]]:
    severity = finding.severity
    status = classify_status(finding)
    edit_action = classify_edit_action(finding)
    supersedes = verdict_supersedes_confidence(finding)
    sev_color = _css(SEVERITY_COLORS.get(severity, "000000"))
    anchor = _anchor_for_finding(finding)
    file_name = finding.fileName or "Unknown"
    attrs = (
        f' data-severity="{_e(severity)}"'
        f' data-file="{_e(file_name)}"'
        f' data-status="{_e(status.value)}"'
        f' data-action="{_e(edit_action.value)}"'
    )
    id_attr = f' id="{_e(anchor)}"' if anchor else ""
    parts = [f'<details class="sc-finding" open{id_attr}{attrs}>']
    header_bits = [f'<span style="color:{sev_color}">{index}. [{_e(severity)}] </span>']
    text_header = f"{index}. [{severity}] "
    if not supersedes:
        tier = _confidence_tier(finding.confidence)
        conf_color = _css(CONFIDENCE_COLORS[tier])
        header_bits.append(
            f'<span style="color:{conf_color}">{finding.confidence:.0%} </span>'
        )
        text_header += f"{finding.confidence:.0%} "
    header_bits.append('<span class="sc-dash">— </span>')
    header_bits.append(f"<span>{_e(file_name)}</span>")
    text_header += f"— {file_name}"
    if finding.section:
        header_bits.append(f'<span class="sc-section"> — {_e(finding.section)}</span>')
        text_header += f" — {finding.section}"
    parts.append("<summary>" + "".join(header_bits) + "</summary>")
    text_lines = [text_header]

    status_color = _css(STATUS_COLORS[status])
    status_html = (
        "<p><strong>Status: </strong>"
        f'<strong style="color:{status_color}">{_e(status_glyph(status))} '
        f"{_e(status_label(status))}</strong>"
    )
    status_text = f"  Status: {status_glyph(status)} {status_label(status)}"
    if is_budget_exhausted(finding):
        status_html += (
            f'<em style="color:{status_color}"> (search budget exhausted)</em>'
        )
        status_text += " (search budget exhausted)"
    status_html += (
        '<span class="sc-sep">  •  </span><strong>Edit: </strong>'
        f'<strong style="color:{_css(EDIT_ACTION_COLORS[edit_action])}">'
        f"{_e(edit_action_label(edit_action))}</strong>"
    )
    status_text += f"  •  Edit: {edit_action_label(edit_action)}"
    age_days = _cache_entry_age_days(getattr(finding, "verification", None))
    if age_days is not None:
        badge_color = _css(CACHE_AGE_COLORS[_cache_age_tier(age_days)])
        status_html += (
            '<span class="sc-sep">  •  </span>'
            f'<strong style="color:{badge_color}">Cache replay — {age_days}d old</strong>'
        )
        status_text += f"  •  Cache replay — {age_days}d old"
    if supersedes:
        status_html += (
            '<span class="sc-sep">  •  </span>'
            f'<em class="sc-confnote">review confidence {finding.confidence:.0%} '
            "(pre-verification)</em>"
        )
        status_text += (
            f"  •  review confidence {finding.confidence:.0%} (pre-verification)"
        )
    status_html += "</p>"
    parts.append(status_html)
    text_lines.append(status_text)

    parts.append(f"<p><strong>Issue: </strong>{_e(finding.issue or '')}</p>")
    text_lines.append(f"  Issue: {finding.issue or ''}")

    proposal = finding.as_edit_proposal()
    if proposal is None:
        parts.append("<p><strong>Action: REPORT_ONLY</strong></p>")
        text_lines.append("  Action: REPORT_ONLY")
        demotion = (getattr(finding, "demotion_reason", None) or "").strip()
        if demotion:
            note = (
                f"Edit proposal demoted to REPORT_ONLY at parse time: {demotion}. "
                "The underlying finding is preserved; manual review required to "
                "determine a clean textual fix."
            )
        else:
            note = (
                "No edit proposal — surfaced for review only (coordination, "
                "interpretation, or multi-paragraph rewrite required)."
            )
        parts.append(f'<p class="sc-note">{_e(note)}</p>')
        text_lines.append(f"  {note}")
    else:
        parts.append(
            f"<p><strong>Action: </strong>{_e(proposal.action_type or '')}</p>"
        )
        text_lines.append(f"  Action: {proposal.action_type or ''}")
        if proposal.existing_text:
            parts.append(
                "<p><strong>Spec evidence: </strong>"
                f'<span style="color:#C00000">{_e(proposal.existing_text)}</span></p>'
            )
            text_lines.append(f"  Spec evidence: {proposal.existing_text}")
        if proposal.replacement_text:
            parts.append(
                "<p><strong>Proposed replacement: </strong>"
                f'<span style="color:#008000">{_e(proposal.replacement_text)}</span></p>'
            )
            text_lines.append(f"  Proposed replacement: {proposal.replacement_text}")

    if finding.codeReference:
        parts.append(
            "<p><strong>Reference: </strong>"
            f'<span style="color:#3B82F6">{_e(finding.codeReference)}</span></p>'
        )
        text_lines.append(f"  Reference: {finding.codeReference}")

    if finding.verification:
        vr = finding.verification
        verdict = vr.verdict
        verdict_color = _css(VERDICT_COLORS.get(verdict, VERDICT_COLORS["UNVERIFIED"]))
        icon = VERDICT_ICONS.get(verdict, "—")
        parts.append(
            f'<p><strong style="color:{verdict_color}">Verification verdict: '
            f"{_e(icon)} {_e(verdict)}</strong></p>"
        )
        text_lines.append(f"  Verification verdict: {icon} {verdict}")
        if verdict == "CORRECTED" and vr.correction:
            parts.append(
                "<p><strong>Correction: </strong>"
                f'<span style="color:#CC8400">{_e(vr.correction)}</span></p>'
            )
            text_lines.append(f"  Correction: {vr.correction}")
        panel_html, panel_text = _render_evidence_panel(finding, vr)
        parts.append(panel_html)
        text_lines.extend(panel_text)

    parts.append("</details>")
    return "\n".join(parts), text_lines


def _render_findings_section(review, *, heading_level: int = 2, id_prefix: str = "") -> tuple[str, list[str]]:
    h = f"h{heading_level}"
    h2 = f"h{heading_level + 1}"
    section_id = f"{id_prefix}sc-findings"
    parts = [f'<section id="{section_id}" class="sc-findinggroup" data-group="findings">']
    parts.append(f"<{h}>Findings <span class='sc-count' data-scope='{section_id}'></span></{h}>")
    text_lines = ["Findings"]
    if review.total_count == 0:
        parts.append('<p class="sc-clean">No issues found.</p>')
        parts.append("</section>")
        text_lines.extend(["No issues found.", ""])
        return "\n".join(parts), text_lines
    finding_number = 0
    for severity in SEVERITY_ORDER:
        severity_findings = sorted(
            (f for f in review.findings if f.severity == severity),
            key=lambda f: (f.fileName or "", -f.confidence),
        )
        if not severity_findings:
            continue
        sev_color = _css(SEVERITY_COLORS.get(severity, "000000"))
        parts.append(
            f'<{h2} class="sc-sevhead" style="color:{sev_color}">{_e(severity)} '
            f"({len(severity_findings)})</{h2}>"
        )
        text_lines.append(f"{severity} ({len(severity_findings)})")
        for finding in severity_findings:
            finding_number += 1
            html_part, text_part = _render_finding_entry(finding, finding_number)
            parts.append(html_part)
            text_lines.extend(text_part)
    parts.append("</section>")
    text_lines.append("")
    return "\n".join(parts), text_lines


_CC_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}


def _render_coordination_like_section(
    result,
    *,
    section_id: str,
    heading: str,
    skipped_prefix: str,
    failed_prefix: str,
    clean_text: str,
    subtitle_for_count,
    narrative_heading: str,
    heading_level: int = 2,
) -> tuple[str, list[str]]:
    if not result:
        return "", []
    h = f"h{heading_level}"
    h2 = f"h{heading_level + 1}"
    status = getattr(result, "cross_check_status", None)
    count = len(result.findings)
    parts = [f'<section id="{section_id}" class="sc-findinggroup" data-group="{section_id}">']
    parts.append(
        f"<{h}>{_e(heading)} <span class='sc-count' data-scope='{section_id}'></span></{h}>"
    )
    text_lines = [heading]
    if status == "skipped":
        subtitle = f"{skipped_prefix}{result.thinking}"
    elif status == "failed":
        subtitle = f"{failed_prefix}{result.error}"
    elif status == "completed" and count == 0:
        subtitle = clean_text
    else:
        subtitle = subtitle_for_count(count)
    parts.append(f'<p class="sc-subtitle">{_e(subtitle)}</p>')
    text_lines.append(subtitle)
    if status in ("skipped", "failed"):
        parts.append("</section>")
        text_lines.append("")
        return "\n".join(parts), text_lines
    sorted_findings = sorted(
        result.findings,
        key=lambda f: (
            _CC_SEVERITY_RANK.get(f.severity, 99),
            f.fileName or "",
            -f.confidence,
        ),
    )
    for index, finding in enumerate(sorted_findings, 1):
        html_part, text_part = _render_finding_entry(finding, index)
        parts.append(html_part)
        text_lines.extend(text_part)
    if result.thinking:
        parts.append(f"<{h2}>{_e(narrative_heading)}</{h2}>")
        text_lines.append(narrative_heading)
        for para in _narrative_paragraphs(result.thinking):
            parts.append(f"<p>{_e(para)}</p>")
            text_lines.append(para)
    parts.append("</section>")
    text_lines.append("")
    return "\n".join(parts), text_lines


def _cross_check_model_label() -> str:
    price = price_for(CROSS_CHECK_MODEL_DEFAULT)
    return price.label if price else CROSS_CHECK_MODEL_DEFAULT


def _render_cross_check_section(cross_check_result, *, heading_level: int = 2, id_prefix: str = "") -> tuple[str, list[str]]:
    return _render_coordination_like_section(
        cross_check_result,
        section_id=f"{id_prefix}sc-crosscheck",
        heading="Cross-Spec Coordination",
        skipped_prefix="Cross-check was skipped: ",
        failed_prefix="Cross-check failed: ",
        clean_text="Cross-check completed — no coordination issues found.",
        subtitle_for_count=lambda count: (
            f"{_cross_check_model_label()} coordination analysis — "
            f"{count} issue{_plural(count)} found."
        ),
        narrative_heading="Coordination Summary",
        heading_level=heading_level,
    )


def _render_compliance_section(compliance_result, *, heading_level: int = 2, id_prefix: str = "") -> tuple[str, list[str]]:
    return _render_coordination_like_section(
        compliance_result,
        section_id=f"{id_prefix}sc-compliance",
        heading="Local-Code Compliance",
        skipped_prefix="Compliance evaluation was skipped: ",
        failed_prefix="Compliance evaluation failed: ",
        clean_text=(
            "Compliance evaluation completed — no missing or contradicted "
            "requirements found."
        ),
        subtitle_for_count=lambda count: (
            "Evaluation against the researched location/client requirements — "
            f"{count} issue{_plural(count)} found."
        ),
        narrative_heading="Compliance Summary",
        heading_level=heading_level,
    )


# ---------------------------------------------------------------------------
# Toolbar / TOC / static assets
# ---------------------------------------------------------------------------


def _render_toolbar(findings: list, files: list[str]) -> str:
    severities = [s for s in SEVERITY_ORDER if any(f.severity == s for f in findings)]
    statuses: list = []
    for f in findings:
        status = classify_status(f)
        if status not in statuses:
            statuses.append(status)
    statuses.sort(key=lambda s: STATUS_DISPLAY_ORDER.index(s))
    actions: list = []
    for f in findings:
        action = classify_edit_action(f)
        if action not in actions:
            actions.append(action)
    parts = ['<div class="sc-controls" id="sc-controls">']
    parts.append(
        '<input type="search" id="sc-search" placeholder="Search findings…" '
        'aria-label="Search findings">'
    )

    def _select(select_id: str, label: str, options: list[tuple[str, str]]) -> str:
        opts = [f'<option value="">{_e(label)}: all</option>']
        opts.extend(
            f'<option value="{_e(value)}">{_e(text)}</option>' for value, text in options
        )
        return (
            f'<select id="{select_id}" aria-label="{_e(label)}">'
            + "".join(opts)
            + "</select>"
        )

    parts.append(
        _select("sc-filter-severity", "Severity", [(s, s) for s in severities])
    )
    parts.append(_select("sc-filter-file", "File", [(f, f) for f in files]))
    parts.append(
        _select(
            "sc-filter-status",
            "Status",
            [(s.value, status_label(s)) for s in statuses],
        )
    )
    parts.append(
        _select(
            "sc-filter-action",
            "Edit action",
            [(a.value, edit_action_label(a)) for a in actions],
        )
    )
    parts.append('<button type="button" id="sc-expand">Expand all</button>')
    parts.append('<button type="button" id="sc-collapse">Collapse all</button>')
    parts.append('<button type="button" id="sc-copy">Copy full report</button>')
    parts.append('<button type="button" id="sc-print">Print / PDF</button>')
    parts.append('<span id="sc-copy-status" role="status" aria-live="polite"></span>')
    parts.append("</div>")
    return "\n".join(parts)


def _render_toc(entries: list[tuple[str, str]]) -> str:
    links = "".join(
        f'<a href="#{_e(anchor)}">{_e(title)}</a>' for anchor, title in entries
    )
    return f'<nav class="sc-toc" aria-label="Contents">{links}</nav>'


_APP_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  font-family: Arial, "Helvetica Neue", Helvetica, sans-serif;
  margin: 0; padding: 0; background: #f5f5f2; color: #1a1a1a;
  font-size: 15px; line-height: 1.45;
}
main { max-width: 1080px; margin: 0 auto; padding: 0 24px 80px; }
h1 { font-size: 1.7em; text-align: center; margin: 18px 0 6px; }
h2 { font-size: 1.3em; border-bottom: 2px solid #d8d5ce; padding-bottom: 4px; margin-top: 34px; }
h3 { font-size: 1.1em; margin-top: 24px; }
h4 { font-size: 1.0em; margin-top: 18px; }
section { margin-bottom: 8px; }
.sc-topbar {
  position: sticky; top: 0; z-index: 30; background: #ffffff;
  border-bottom: 1px solid #d8d5ce; padding: 8px 24px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.sc-topbar .sc-appname { font-weight: bold; margin-right: 12px; white-space: nowrap; }
.sc-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.sc-controls input[type="search"] { flex: 1 1 220px; min-width: 160px; padding: 6px 8px;
  border: 1px solid #c5c2ba; border-radius: 6px; font-size: 14px; }
.sc-controls select, .sc-controls button { padding: 6px 8px; border: 1px solid #c5c2ba;
  border-radius: 6px; background: #faf9f7; font-size: 13px; cursor: pointer; }
.sc-controls button:hover, .sc-controls select:hover { background: #f0efec; }
#sc-copy-status { font-size: 12px; color: #646464; }
.sc-toc { display: flex; flex-wrap: wrap; gap: 4px 14px; padding: 6px 0 2px; font-size: 13px; }
.sc-toc a { color: #3B82F6; text-decoration: none; }
.sc-toc a:hover { text-decoration: underline; }
.sc-titleblock { text-align: center; margin-top: 18px; }
.sc-meta { color: #646464; font-size: 0.85em; margin: 2px 0; }
.sc-note { color: #646464; font-size: 0.85em; font-style: italic; }
.sc-hint { font-size: 0.85em; font-style: italic; }
.sc-subtitle { color: #808080; font-style: italic; }
.sc-detail { color: #787878; font-size: 0.8em; }
.sc-itemid { color: #808080; font-size: 0.85em; }
.sc-unverified { color: #C00000; font-weight: bold; font-size: 0.85em; }
.sc-clean { color: #008000; font-size: 1.1em; }
.sc-sep { color: #A0A0A0; }
.sc-confnote { color: #969696; font-size: 0.85em; }
.sc-section { color: #646464; font-size: 0.92em; }
.sc-dash { color: #808080; }
.sc-failedspec { color: #C00000; font-weight: bold; }
.sc-tablewrap { overflow-x: auto; }
table { border-collapse: collapse; margin: 10px 0; background: #ffffff; }
th, td { border: 1px solid #b9b6ae; padding: 6px 10px; text-align: left;
  font-size: 0.88em; vertical-align: top; }
table.sc-banner th { background: #F0F0F0; width: 20em; }
table.sc-banner td { font-weight: bold; }
td.sc-flag { background: #FFE5E5; color: #C00000; }
table.sc-severity th { text-align: center; }
table.sc-severity td.sc-bigcount { text-align: center; font-size: 1.35em; font-weight: bold; }
table.sc-grid th { background: #F0F0F0; }
details.sc-finding {
  background: #ffffff; border: 1px solid #ddd9d0; border-radius: 8px;
  padding: 4px 14px; margin: 10px 0;
}
details.sc-finding > summary {
  cursor: pointer; font-weight: bold; padding: 8px 0; font-size: 1.0em;
  list-style-position: outside;
}
details.sc-finding p { margin: 6px 0; }
details.sc-evidence {
  background: #faf9f7; border: 1px solid #e4e1da; border-radius: 6px;
  padding: 2px 12px; margin: 8px 0; font-size: 0.9em; color: #505050;
}
details.sc-evidence > summary { cursor: pointer; font-weight: bold; padding: 6px 0; }
.sc-quote { border-left: 4px solid #c5c2ba; margin: 6px 0 6px 8px; padding: 2px 10px;
  color: #505050; font-style: italic; }
.sc-srclabel a, a.sc-src { color: #3B82F6; word-break: break-all; }
.sc-rejected { color: #808080; font-style: italic; word-break: break-all; }
.sc-hidden { display: none !important; }
.sc-impact { font-size: 1.05em; }
@media (max-width: 720px) {
  main { padding: 0 12px 60px; }
  .sc-topbar { padding: 6px 12px; }
  .sc-controls input[type="search"] { flex-basis: 100%; }
}
@media print {
  .sc-topbar { display: none; }
  body { background: #ffffff; font-size: 12px; }
  details.sc-finding, details.sc-evidence { border: 1px solid #bbb; page-break-inside: avoid; }
  a { color: inherit; text-decoration: none; }
}
"""


_APP_JS = """
(function () {
  "use strict";
  function all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  var searchBox = document.getElementById("sc-search");
  var selSeverity = document.getElementById("sc-filter-severity");
  var selFile = document.getElementById("sc-filter-file");
  var selStatus = document.getElementById("sc-filter-status");
  var selAction = document.getElementById("sc-filter-action");
  var findings = all("details.sc-finding");

  function matches(card) {
    var q = (searchBox && searchBox.value || "").trim().toLowerCase();
    if (selSeverity && selSeverity.value && card.getAttribute("data-severity") !== selSeverity.value) return false;
    if (selFile && selFile.value && card.getAttribute("data-file") !== selFile.value) return false;
    if (selStatus && selStatus.value && card.getAttribute("data-status") !== selStatus.value) return false;
    if (selAction && selAction.value && card.getAttribute("data-action") !== selAction.value) return false;
    if (q && card.textContent.toLowerCase().indexOf(q) === -1) return false;
    return true;
  }

  function updateSeverityHeadings(groupEl) {
    var current = null;
    var visible = 0;
    Array.prototype.slice.call(groupEl.children).forEach(function (el) {
      if (el.classList && el.classList.contains("sc-sevhead")) {
        if (current) current.classList.toggle("sc-hidden", visible === 0);
        current = el;
        visible = 0;
      } else if (el.matches && el.matches("details.sc-finding")) {
        if (!el.classList.contains("sc-hidden")) visible += 1;
      }
    });
    if (current) current.classList.toggle("sc-hidden", visible === 0);
  }

  function apply() {
    var groups = {};
    findings.forEach(function (card) {
      var show = matches(card);
      card.classList.toggle("sc-hidden", !show);
      var group = card.closest(".sc-findinggroup");
      if (group) {
        var id = group.getAttribute("id");
        groups[id] = groups[id] || { shown: 0, total: 0, el: group };
        groups[id].total += 1;
        if (show) groups[id].shown += 1;
      }
    });
    Object.keys(groups).forEach(function (id) {
      var info = groups[id];
      var badge = info.el.querySelector(".sc-count");
      if (badge) {
        badge.textContent = info.shown === info.total
          ? "(" + info.total + ")"
          : "(showing " + info.shown + " of " + info.total + ")";
      }
      updateSeverityHeadings(info.el);
    });
  }

  [searchBox, selSeverity, selFile, selStatus, selAction].forEach(function (el) {
    if (!el) return;
    el.addEventListener("input", apply);
    el.addEventListener("change", apply);
  });

  var expandBtn = document.getElementById("sc-expand");
  var collapseBtn = document.getElementById("sc-collapse");
  if (expandBtn) expandBtn.addEventListener("click", function () {
    all("details.sc-finding, details.sc-evidence").forEach(function (d) { d.open = true; });
  });
  if (collapseBtn) collapseBtn.addEventListener("click", function () {
    all("details.sc-finding, details.sc-evidence").forEach(function (d) { d.open = false; });
  });

  var copyBtn = document.getElementById("sc-copy");
  var copyStatus = document.getElementById("sc-copy-status");
  function reportCopy(ok) {
    if (copyStatus) copyStatus.textContent = ok ? "Copied." : "Copy failed — select the text manually.";
    if (copyStatus) setTimeout(function () { copyStatus.textContent = ""; }, 4000);
  }
  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    document.body.removeChild(area);
    reportCopy(ok);
  }
  if (copyBtn) copyBtn.addEventListener("click", function () {
    var source = document.getElementById("sc-plaintext");
    var text = source ? source.textContent : "";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { reportCopy(true); }, function () { fallbackCopy(text); });
    } else {
      fallbackCopy(text);
    }
  });

  var printBtn = document.getElementById("sc-print");
  if (printBtn) printBtn.addEventListener("click", function () { window.print(); });
  window.addEventListener("beforeprint", function () {
    all("details.sc-finding, details.sc-evidence").forEach(function (d) {
      if (!d.open) { d.setAttribute("data-sc-reclose", "1"); d.open = true; }
    });
  });
  window.addEventListener("afterprint", function () {
    all("details[data-sc-reclose]").forEach(function (d) {
      d.open = false; d.removeAttribute("data-sc-reclose");
    });
  });

  apply();
})();
"""


def _script_hash(script_text: str) -> str:
    digest = hashlib.sha256(script_text.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def _json_for_embed(payload: dict) -> str:
    """Serialize the payload so hostile text is inert inside the data block.

    ``&``, ``<``, and ``>`` are replaced with their JSON ``\\uXXXX`` escapes
    (valid only inside strings, and outside strings JSON never contains these
    characters), so the embedded block can never contain a raw ``<script`` /
    ``</script`` sequence regardless of report content. ``json.loads`` reads
    the original text back unchanged.
    """
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return (
        text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )


def _assemble_document(
    *,
    title: str,
    body_sections: list[str],
    toc_entries: list[tuple[str, str]],
    toolbar_html: str,
    payload: dict,
    plaintext_lines: list[str],
) -> str:
    script = _APP_JS
    csp = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src '{_script_hash(script)}'; img-src data:; "
        "base-uri 'none'; form-action 'none'"
    )
    plaintext = "\n".join(plaintext_lines).strip() + "\n"
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">',
        f"<title>{_e(title)}</title>",
        f"<style>{_APP_CSS}</style>",
        "</head>",
        "<body>",
        '<header class="sc-topbar">',
        '<div class="sc-controls-row"><span class="sc-appname">Spec Critic</span>',
        toolbar_html,
        "</div>",
        _render_toc(toc_entries),
        "</header>",
        "<main>",
        *body_sections,
        "</main>",
        f'<script type="application/json" id="sc-report-data">{_json_for_embed(payload)}</script>',
        f'<pre id="sc-plaintext" hidden>{_e(plaintext)}</pre>',
        f"<script>{script}</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Top-level renderers
# ---------------------------------------------------------------------------


def _render_single_module_body(
    pipeline_result,
    generated_at: datetime,
    *,
    heading_level: int = 2,
    id_prefix: str = "",
    include_title: bool = True,
    include_diagnostics: bool = True,
    include_trust: bool = True,
    include_drawing_impact: bool = True,
) -> tuple[list[str], list[tuple[str, str]], list[str], list]:
    """Render one module's report walk; returns (sections, toc, text, findings)."""
    review = pipeline_result.review_result
    cross_check = pipeline_result.cross_check_result
    compliance = getattr(pipeline_result, "compliance_result", None)
    requirements_profile = RequirementsProfile.from_dict(
        getattr(pipeline_result, "requirements_profile", None)
    )
    all_findings = list(review.findings)
    if cross_check and cross_check.findings:
        all_findings.extend(cross_check.findings)
    if compliance is not None and compliance.findings:
        all_findings.extend(compliance.findings)
    verification_stats = _summarize_verification_outcomes(all_findings)
    cycle_label = getattr(pipeline_result, "cycle_label", "2025") or "2025"
    module = get_module(getattr(pipeline_result, "module_id", None))
    project_profile = ProjectProfile.from_dict(
        getattr(pipeline_result, "project_profile", None)
    )
    failed_review_specs = [
        str(name)
        for name in (getattr(pipeline_result, "failed_review_specs", None) or [])
    ]

    sections: list[str] = []
    toc: list[tuple[str, str]] = []
    text_lines: list[str] = []

    if include_title:
        meta_lines = _meta_lines_single(
            pipeline_result, review, module, project_profile, generated_at
        )
        html_part, text_part = _render_title_block(module.report_title, meta_lines)
        sections.append(html_part)
        text_lines.extend(text_part)

    if include_diagnostics:
        diagnostics = _summarize_run_diagnostics(
            findings=all_findings,
            status_counts=verification_stats.get("status_counts", {}),
            edit_action_counts=verification_stats.get("edit_action_counts", {}),
            cross_check_result=cross_check,
            pipeline_result=pipeline_result,
            compliance_result=compliance,
        )
        html_part, text_part = _render_run_diagnostics(
            diagnostics, heading_level=heading_level
        )
        sections.append(html_part)
        text_lines.extend(text_part)
        toc.append(("sc-diagnostics", "Run Diagnostics"))

    html_part, text_part = _render_files_reviewed(
        pipeline_result.files_reviewed,
        set(failed_review_specs),
        heading_level=heading_level,
        id_prefix=id_prefix,
    )
    sections.append(html_part)
    text_lines.extend(text_part)
    toc.append((f"{id_prefix}sc-files", "Files Reviewed"))

    if requirements_profile is not None:
        html_part, text_part = _render_requirements_section(
            requirements_profile,
            compliance,
            heading_level=heading_level,
            id_prefix=id_prefix,
        )
        sections.append(html_part)
        text_lines.extend(text_part)
        toc.append((f"{id_prefix}sc-requirements", "Jurisdiction & Client Requirements"))

    cross_check_status = (
        getattr(cross_check, "cross_check_status", None) if cross_check else None
    )
    cross_check_reason = ""
    if cross_check and cross_check_status == "skipped":
        cross_check_reason = cross_check.thinking or ""
    elif cross_check and cross_check_status == "failed":
        cross_check_reason = cross_check.error or ""
    paragraphs = _methodology_paragraphs(
        cross_check_enabled=(cross_check is not None),
        cycle_label=cycle_label,
        cross_check_status=cross_check_status,
        cross_check_reason=cross_check_reason,
        verification_stats=verification_stats,
        module=module,
    )
    html_part, text_part = _render_methodology(
        paragraphs, heading_level=heading_level, id_prefix=id_prefix
    )
    sections.append(html_part)
    text_lines.extend(text_part)
    toc.append((f"{id_prefix}sc-methodology", "About This Review"))

    drawing_impact = getattr(pipeline_result, "drawing_impact_result", None)
    if include_drawing_impact and drawing_impact is not None:
        findings_by_id = {
            f.finding_id: f for f in all_findings if getattr(f, "finding_id", "")
        }
        html_part, text_part = _render_drawing_impact(
            drawing_impact, findings_by_id, heading_level=heading_level
        )
        sections.append(html_part)
        text_lines.extend(text_part)
        toc.append(("sc-drawing-impact", "Drawing Impact"))

    html_part, text_part = _render_summary(
        review,
        cross_check,
        total_elapsed_seconds=getattr(pipeline_result, "total_elapsed_seconds", None),
        heading_level=heading_level,
        id_prefix=id_prefix,
    )
    sections.append(html_part)
    text_lines.extend(text_part)
    toc.append((f"{id_prefix}sc-summary", "Summary"))

    if include_trust:
        html_part, text_part = _render_trust_model(
            verification_stats.get("status_counts", {}),
            verification_stats.get("edit_action_counts", {}),
            heading_level=heading_level,
        )
        if html_part:
            sections.append(html_part)
            text_lines.extend(text_part)
            toc.append(("sc-trust", "Trust Model"))

    alert_lists = {
        "leed": pipeline_result.leed_alerts,
        "placeholder": pipeline_result.placeholder_alerts,
        "template_marker": getattr(pipeline_result, "template_marker_alerts", None),
        "code_cycle": getattr(pipeline_result, "code_cycle_alerts", None),
        "invalid_code_cycle": getattr(
            pipeline_result, "invalid_code_cycle_alerts", None
        ),
        "structural": getattr(pipeline_result, "structural_alerts", None),
        "duplicate_paragraph": getattr(
            pipeline_result, "duplicate_paragraph_alerts", None
        ),
        "naming": getattr(pipeline_result, "naming_alerts", None),
        "polity": getattr(pipeline_result, "polity_alerts", None),
    }
    html_part, text_part = _render_alerts(
        {k: list(v or []) for k, v in alert_lists.items()},
        module,
        heading_level=heading_level,
        id_prefix=id_prefix,
    )
    if html_part:
        sections.append(html_part)
        text_lines.extend(text_part)
        toc.append((f"{id_prefix}sc-alerts", "Alerts"))

    html_part, text_part = _render_findings_section(
        review, heading_level=heading_level, id_prefix=id_prefix
    )
    sections.append(html_part)
    text_lines.extend(text_part)
    toc.append((f"{id_prefix}sc-findings", "Findings"))

    html_part, text_part = _render_cross_check_section(
        cross_check, heading_level=heading_level, id_prefix=id_prefix
    )
    if html_part:
        sections.append(html_part)
        text_lines.extend(text_part)
        toc.append((f"{id_prefix}sc-crosscheck", "Cross-Spec Coordination"))

    html_part, text_part = _render_compliance_section(
        compliance, heading_level=heading_level, id_prefix=id_prefix
    )
    if html_part:
        sections.append(html_part)
        text_lines.extend(text_part)
        toc.append((f"{id_prefix}sc-compliance", "Local-Code Compliance"))

    return sections, toc, text_lines, all_findings


def _render_single(pipeline_result, generated_at: datetime) -> str:
    if pipeline_result.review_result is None:
        raise ValueError("Cannot export report: no review results available")
    module = get_module(getattr(pipeline_result, "module_id", None))
    sections, toc, text_lines, findings = _render_single_module_body(
        pipeline_result, generated_at
    )
    payload = _build_payload_single(pipeline_result, generated_at)
    toolbar = _render_toolbar(findings, list(pipeline_result.files_reviewed))
    return _assemble_document(
        title=module.report_title,
        body_sections=sections,
        toc_entries=toc,
        toolbar_html=toolbar,
        payload=payload,
        plaintext_lines=text_lines,
    )


_PROGRAM_REPORT_TITLE = (
    "Spec Critic — Hyperscale Data Center Specification Review Report"
)


def _render_program(program_result, generated_at: datetime) -> str:
    from ..modules.registry import require_module
    from ..programs.catalog import get_program

    if not program_result.module_results:
        raise ValueError("Cannot export program report: no module results available")
    program = get_program(program_result.program_id)
    aggregate_review = program_result.review_result

    meta_lines = [
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M')}",
        f"Program: {program.display_name}",
        f"Model: {aggregate_review.model}",
        f"Selected Specifications: {len(program_result.selected_files)}",
        "Routed Specifications Submitted: "
        f"{len(program_result.files_reviewed)} of "
        f"{len(program_result.expected_files_reviewed)}",
        "Routed Review Requests Submitted: "
        f"{program_result.routed_request_count} of "
        f"{program_result.expected_routed_request_count}",
        "Code Basis: Determined separately by each routed review module",
    ]
    profile = ProjectProfile.from_dict(program_result.project_profile)
    if profile is not None:
        meta_lines.extend(
            [
                f"Project: {profile.city}, {profile.state_display}, {profile.country_display}",
                f"Client: {profile.client_name}",
            ]
        )

    sections: list[str] = []
    toc: list[tuple[str, str]] = []
    text_lines: list[str] = []
    all_findings: list = []

    html_part, text_part = _render_title_block(_PROGRAM_REPORT_TITLE, meta_lines)
    sections.append(html_part)
    text_lines.extend(text_part)

    routing_intro = (
        "Each specification was routed independently. A file may be reviewed by "
        "more than one module; unsupported files are retained as explicit "
        "coverage gaps. Cross-check and compliance passes run within each "
        "module, not across disciplines."
    )
    routing_parts = ['<section id="sc-routing">', "<h2>Review Coverage and Routing</h2>"]
    routing_parts.append(f'<p class="sc-note">{_e(routing_intro)}</p>')
    routing_parts.append('<div class="sc-tablewrap"><table class="sc-grid">')
    routing_parts.append(
        "<tr><th>Specification</th><th>Assigned reviewer(s)</th>"
        "<th>Routing</th><th>Confidence</th></tr>"
    )
    text_lines.extend(["Review Coverage and Routing", routing_intro])
    for assignment in program_result.assignments:
        module_names = ", ".join(
            require_module(mid).display_name for mid in assignment.module_ids
        ) or "None — unsupported/skipped"
        routing = (
            "User confirmed"
            if assignment.decision.is_user_overridden
            else assignment.state.value.title()
        )
        confidence = f"{round(assignment.decision.confidence * 100)}%"
        routing_parts.append(
            f"<tr><td>{_e(assignment.spec_id)}</td><td>{_e(module_names)}</td>"
            f"<td>{_e(routing)}</td><td>{_e(confidence)}</td></tr>"
        )
        text_lines.append(
            f"  {assignment.spec_id} | {module_names} | {routing} | {confidence}"
        )
    routing_parts.append("</table></div></section>")
    sections.append("\n".join(routing_parts))
    toc.append(("sc-routing", "Routing"))
    text_lines.append("")

    html_part, text_part = _render_summary(
        aggregate_review,
        program_result.cross_check_result,
        total_elapsed_seconds=program_result.total_elapsed_seconds,
    )
    sections.append(html_part)
    text_lines.extend(text_part)
    toc.append(("sc-summary", "Summary"))

    skipped = list(getattr(program_result, "skipped_files", None) or [])
    missing_modules = list(getattr(program_result, "missing_module_ids", None) or [])
    module_errors = dict(getattr(program_result, "module_errors", None) or {})
    if skipped or missing_modules or module_errors:
        details: list[str] = []
        if skipped:
            details.append(
                f"{len(skipped)} selected specification(s) were unsupported and skipped"
            )
        if missing_modules:
            labels = ", ".join(
                require_module(module_id).display_name for module_id in missing_modules
            )
            details.append(f"no completed result is available for {labels}")
        if module_errors:
            failures = "; ".join(
                f"{require_module(module_id).display_name}: {message}"
                for module_id, message in module_errors.items()
            )
            details.append(f"module collection error(s): {failures}")
        warning = "Coverage status: PARTIAL. " + "; ".join(details) + "."
        sections.append(
            '<p class="sc-hint" style="color:#C07000"><strong>'
            + _e(warning)
            + "</strong></p>"
        )
        text_lines.extend([warning, ""])

    drawing_impact = getattr(program_result, "drawing_impact_result", None)
    if drawing_impact is not None:
        qualified_findings: dict[str, object] = {}
        for module_id, child in program_result.module_results.items():
            for phase_result in (
                child.review_result,
                child.cross_check_result,
                child.compliance_result,
            ):
                for finding in getattr(phase_result, "findings", None) or []:
                    finding_id = str(getattr(finding, "finding_id", "") or "")
                    if finding_id:
                        qualified_findings[f"{module_id}::{finding_id}"] = finding
        html_part, text_part = _render_drawing_impact(
            drawing_impact, qualified_findings
        )
        sections.append(html_part)
        text_lines.extend(text_part)
        toc.append(("sc-drawing-impact", "Drawing Impact"))

    module_payloads: dict[str, dict] = {}
    for position, module_id in enumerate(program.implemented_module_ids):
        child = program_result.module_results.get(module_id)
        if child is None:
            continue
        module = require_module(module_id)
        prefix = f"m-{module_id}-"
        module_section = [f'<section class="sc-module" id="{_e(prefix)}top">']
        module_section.append(f"<h2 class='sc-modulehead'>{_e(module.display_name)}</h2>")
        basis = (
            f"{module.detector_vocabulary.jurisdiction_label} {module.cycle.label}; "
            + ", ".join(
                code.name + " " + code.year for code in module.cycle.base_codes
            )
        )
        module_section.append(
            f"<p><strong>Module code basis: </strong>{_e(basis)}</p>"
        )
        text_lines.extend([module.display_name, f"Module code basis: {basis}"])
        child_sections, child_toc, child_text, child_findings = (
            _render_single_module_body(
                child,
                generated_at,
                heading_level=3,
                id_prefix=prefix,
                include_title=False,
                include_diagnostics=False,
                include_trust=False,
                include_drawing_impact=False,
            )
        )
        module_section.extend(child_sections)
        module_section.append("</section>")
        sections.append("\n".join(module_section))
        toc.append((f"{prefix}top", module.display_name))
        text_lines.extend(child_text)
        all_findings.extend(child_findings)
        module_payloads[module_id] = _build_payload_single(child, generated_at)

    payload = {
        "schema_version": HTML_REPORT_SCHEMA_VERSION,
        "report_type": "program",
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M"),
        "program_id": program_result.program_id,
        "project": getattr(program_result, "project_profile", None),
        "selected_files": list(program_result.selected_files),
        "files_reviewed": list(program_result.files_reviewed),
        "skipped_files": skipped,
        "missing_module_ids": missing_modules,
        "module_errors": module_errors,
        "routing": [
            {
                "spec_id": a.spec_id,
                "module_ids": list(a.module_ids),
                "state": a.state.value,
                "user_overridden": a.decision.is_user_overridden,
                "confidence": a.decision.confidence,
            }
            for a in program_result.assignments
        ],
        "total_elapsed_seconds": program_result.total_elapsed_seconds,
        "modules": module_payloads,
    }
    files = sorted({f.fileName or "Unknown" for f in all_findings})
    toolbar = _render_toolbar(all_findings, files)
    return _assemble_document(
        title=_PROGRAM_REPORT_TITLE,
        body_sections=sections,
        toc_entries=toc,
        toolbar_html=toolbar,
        payload=payload,
        plaintext_lines=text_lines,
    )


def render_html_report(pipeline_result, *, generated_at: datetime | None = None) -> str:
    """Render a completed result as one self-contained HTML document string.

    Read-only: the result object is never mutated, no API call is made, and
    no file is touched. ``generated_at`` pins the generation timestamp for
    deterministic output; it defaults to now.
    """
    stamp = generated_at if generated_at is not None else datetime.now()
    if hasattr(pipeline_result, "module_results") and hasattr(
        pipeline_result, "program_id"
    ):
        return _render_program(pipeline_result, stamp)
    return _render_single(pipeline_result, stamp)


def write_html_report(
    pipeline_result, output_path: Path, *, generated_at: datetime | None = None
) -> Path:
    """Write the HTML report to ``output_path`` and return the path.

    Writes bytes (UTF-8) so the CSP script hash always matches the file's
    exact contents — platform newline translation can never corrupt it.
    """
    document = render_html_report(pipeline_result, generated_at=generated_at)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(document.encode("utf-8"))
    return output_path
