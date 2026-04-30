"""
Preprocessor module for local detection of LEED references and placeholders.

This module performs DETECTION ONLY — it does not modify spec content.
Detected items are reported separately from LLM findings to:
    1. Save tokens (no need to ask Claude to find [INSERT] placeholders)
    2. Provide instant feedback (no API call required)
    3. Keep concerns separate (editorial markers vs. technical issues)

If you need actual document cleanup/scrubbing (removing boilerplate, fixing
formatting, etc.), use the separate SpecCleanse tool:
https://github.com/Abe-Borg/Spec_Cleanse

Detection categories:
    - LEED references: LEED, LEED-NC, LEED-CI, USGBC
      (K-12 DSA projects typically aren't LEED — these are likely copy/paste errors)
    - Placeholders: [INSERT...], [VERIFY...], [TBD], ___, etc.
      (Unresolved editorial markers that need attention before issuing)

Usage:
    from preprocessor import preprocess_spec, PreprocessResult
    
    result = preprocess_spec(spec_content, "23 21 13 - Hydronic Piping.docx")
    print(f"Found {len(result.leed_alerts)} LEED references")
    print(f"Found {len(result.placeholder_alerts)} placeholders")
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .code_cycles import CodeCycle


@dataclass
class PreprocessResult:
    """
    Result of detection-only preprocessing for a single spec.

    Attributes:
        leed_alerts: List of detected LEED references with context
        placeholder_alerts: List of detected placeholders with context
        code_cycle_alerts: Phase 9 (plan 13.1) — references to a stale California
            code cycle (e.g. ``2019 CBC`` when the selected cycle is 2025).
        structural_alerts: Phase 9 (plan 13.1) — empty sections and duplicate
            headings detected without spending model tokens.

    Each alert is a dict with keys:
        - filename: Source file name
        - type: Description of what was matched (e.g., "LEED reference")
        - match: The actual matched text
        - context: ~120 char window around the match for human review
        - position: Character offset in the document
    """
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Detection Patterns
# -----------------------------------------------------------------------------

LEED_PATTERNS: list[tuple[str, str]] = [
    # Specific patterns first so they claim spans before the generic \bLEED\b
    (r"(?i)\bLEED[-\s]?NC\b", "LEED-NC reference"),
    (r"(?i)\bLEED[-\s]?CI\b", "LEED-CI reference"),
    (r"(?i)\bLEED[-\s]?EB\b", "LEED-EB reference"),
    (r"(?i)\bUSGBC\b", "USGBC reference"),
    (r"(?i)\bLEED\b", "LEED reference"),  # Generic last
]

PLACEHOLDER_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\[\s*INSERT[^\]]*\]", "INSERT placeholder"),
    (r"(?i)\[\s*VERIFY[^\]]*\]", "VERIFY placeholder"),
    (r"(?i)\[\s*EDIT[^\]]*\]", "EDIT placeholder"),
    (r"(?i)\[\s*SELECT[^\]]*\]", "SELECT placeholder"),
    (r"(?i)\[\s*COORDINATE[^\]]*\]", "COORDINATE placeholder"),
    (r"(?i)\[\s*TO\s+BE\s+DETERMINED[^\]]*\]", "TBD placeholder"),
    (r"(?i)\[\s*TBD[^\]]*\]", "TBD placeholder"),
    (r"(?i)\[\s*N\/A[^\]]*\]", "N/A placeholder"),
    (r"(?i)\[\s*OPTION[^\]]*\]", "OPTION placeholder"),
    (r"(?i)<\s*VERIFY[^>]*>", "VERIFY tag"),
    (r"(?i)<\s*EDIT[^>]*>", "EDIT tag"),
    (r"(?i)<\s*INSERT[^>]*>", "INSERT tag"),
    (r"_{3,}", "Underscore placeholder"),
    (r"\[\s*\.\.\.\s*\]", "Ellipsis placeholder"),
]


# -----------------------------------------------------------------------------
# Detection Functions
# -----------------------------------------------------------------------------
def _find_matches(patterns: Iterable[tuple[str, str]], content: str, filename: str, max_matches: int) -> list[dict]:
    """Find all matches for a set of regex patterns in content.

    Uses span-based deduplication: if a match's character range is fully
    contained within an already-recorded span, it is skipped. This prevents
    e.g. "LEED-NC" from producing both a "LEED-NC reference" alert and a
    duplicate "LEED reference" alert for the "LEED" substring.
    """
    alerts: list[dict] = []
    seen_spans: list[tuple[int, int]] = []
    for pattern, description in patterns:
        try:
            for match in re.finditer(pattern, content):
                m_start, m_end = match.start(), match.end()
                # Skip if this span overlaps with an already-seen span
                if any(s <= m_start and m_end <= e for s, e in seen_spans):
                    continue
                seen_spans.append((m_start, m_end))

                ctx_start = max(0, m_start - 60)
                ctx_end = min(len(content), m_end + 60)
                ctx = content[ctx_start:ctx_end].replace("\n", " ").strip()

                alerts.append(
                    {
                        "filename": filename,
                        "type": description,
                        "match": match.group(0),
                        "context": ctx,
                        "position": m_start,
                    }
                )

                if len(alerts) >= max_matches:
                    return alerts
        except re.error:
            continue
    return alerts


def detect_leed_references(content: str, filename: str, max_matches: int = 50) -> list[dict]:
    """Detect LEED-related references in spec content."""
    return _find_matches(LEED_PATTERNS, content, filename, max_matches=max_matches)


def detect_placeholders(content: str, filename: str, max_matches: int = 200) -> list[dict]:
    """Detect unresolved placeholders and editorial markers in spec content."""
    return _find_matches(PLACEHOLDER_PATTERNS, content, filename, max_matches=max_matches)


# -----------------------------------------------------------------------------
# Phase 9 (plan section 13.1) — additional local preflight checks.
#
# These run before any model call and surface deterministic issues that should
# never need an LLM round-trip. Keeping them here means a re-run with toggled
# project options does not pay tokens for catching a stale ``2019 CBC``
# reference or an empty section heading.
# -----------------------------------------------------------------------------

# Years that should trigger a stale-cycle alert when they sit next to a
# California code abbreviation. Limited to a recent window so we do not flag
# legitimate historical references far from the current cycle.
_PLAUSIBLE_CODE_YEARS = {"2010", "2013", "2016", "2019", "2022", "2025"}

# Code abbreviations recognised on the right-hand side of "<year> <code>".
_CODE_ABBREVS = ("CBC", "CMC", "CPC", "CEC", "CFC", "CALGreen", "CalGreen", "CRC")

# Captures "2019 CBC", "CBC 2019", "2019 California Building Code", etc.
_STALE_CYCLE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(
        r"\b(20\d{2})\s+(?:" + "|".join(_CODE_ABBREVS) + r")\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:" + "|".join(_CODE_ABBREVS) + r")[\s,]+(20\d{2})\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(20\d{2})\s+California\s+(?:Building|Mechanical|Plumbing|"
        r"Electrical|Fire|Energy|Green\s+Building|Residential)\s+Code\b",
        flags=re.IGNORECASE,
    ),
)

# ASCE 7 edition references. Only flag editions older than the cycle's
# nominal ASCE 7 edition (e.g. 7-10 / 7-05 when cycle says 7-22).
_ASCE7_PATTERN = re.compile(r"\bASCE[\s-]*7[\s-]*(\d{2})\b", flags=re.IGNORECASE)
_ASCE7_PLAUSIBLE_EDITIONS = {"05", "10", "16", "22"}


def detect_stale_code_cycle_references(
    content: str,
    filename: str,
    cycle: CodeCycle,
    *,
    max_matches: int = 200,
) -> list[dict]:
    """Flag year/edition references that do not match the selected cycle.

    A reference is "stale" when it pins a California code year that is
    different from ``cycle.cbc`` (e.g. ``2019 CBC`` selected against the 2025
    cycle), or when an ASCE 7 edition is older than ``cycle.asce7``.

    The detector is intentionally narrow: it never flags the cycle's own year
    or its prior cycle's year if the prior cycle is being referenced as
    historical context (the model still has the project context to qualify
    that). Callers can downgrade alerts by post-processing the returned
    dicts; this function does not call the API.
    """
    if not cycle:
        return []
    target_year = (cycle.cbc or "").strip()
    if not target_year:
        return []

    alerts: list[dict] = []
    seen_spans: list[tuple[int, int]] = []
    for pattern in _STALE_CYCLE_PATTERNS:
        for match in pattern.finditer(content):
            year = next((g for g in match.groups() if g and g in _PLAUSIBLE_CODE_YEARS), None)
            if year is None or year == target_year:
                continue
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in seen_spans):
                continue
            seen_spans.append(span)
            ctx_start = max(0, span[0] - 60)
            ctx_end = min(len(content), span[1] + 60)
            alerts.append(
                {
                    "filename": filename,
                    "type": f"Stale code cycle reference ({year} vs selected {target_year})",
                    "match": match.group(0),
                    "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "position": span[0],
                    "expected_year": target_year,
                    "found_year": year,
                }
            )
            if len(alerts) >= max_matches:
                return alerts

    target_asce = re.sub(r"\D", "", cycle.asce7 or "")
    if target_asce and len(target_asce) >= 2:
        target_asce_yr = target_asce[-2:]
        for match in _ASCE7_PATTERN.finditer(content):
            edition = match.group(1)
            if (
                edition not in _ASCE7_PLAUSIBLE_EDITIONS
                or edition == target_asce_yr
                or int(edition) >= int(target_asce_yr)
            ):
                continue
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in seen_spans):
                continue
            seen_spans.append(span)
            ctx_start = max(0, span[0] - 60)
            ctx_end = min(len(content), span[1] + 60)
            alerts.append(
                {
                    "filename": filename,
                    "type": f"Stale ASCE 7 edition (7-{edition} vs selected {cycle.asce7})",
                    "match": match.group(0),
                    "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "position": span[0],
                    "expected_edition": cycle.asce7,
                    "found_edition": f"7-{edition}",
                }
            )
            if len(alerts) >= max_matches:
                return alerts
    return alerts


# Numbered CSI-style heading at the start of a paragraph: "1.01", "2.3 ",
# "PART 1", "1.0 GENERAL", etc. We anchor at the start of a paragraph
# (preceded by paragraph delimiter "\n\n" or string start).
_HEADING_LINE_RE = re.compile(
    r"(?:^|\n\n)\s*(?P<num>(?:PART\s+\d+|\d+(?:\.\d+){0,2}))\s+(?P<title>[^\n]{1,120})",
    flags=re.IGNORECASE,
)


def _iter_section_headings(content: str):
    """Yield ``(number, title, start, end)`` tuples for spec section headings."""
    for match in _HEADING_LINE_RE.finditer(content):
        number = match.group("num").strip().upper()
        title = match.group("title").strip().rstrip(":").strip()
        if not title:
            continue
        yield number, title, match.start("num"), match.end("title")


def detect_empty_sections(
    content: str,
    filename: str,
    *,
    max_matches: int = 50,
) -> list[dict]:
    """Flag numbered headings whose body content is empty or whitespace.

    "Empty" means: the heading is followed by another heading (or end of
    document) with no body paragraph between them. This catches templated
    DSA specs where an editor deleted the body without removing the
    heading scaffold.
    """
    headings = list(_iter_section_headings(content))
    if not headings:
        return []
    alerts: list[dict] = []
    for i, (number, title, h_start, h_end) in enumerate(headings):
        body_end = headings[i + 1][2] if i + 1 < len(headings) else len(content)
        body = content[h_end:body_end].strip()
        if body:
            continue
        ctx_start = max(0, h_start - 40)
        ctx_end = min(len(content), body_end + 40)
        alerts.append(
            {
                "filename": filename,
                "type": "Empty section",
                "match": f"{number} {title}",
                "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                "position": h_start,
                "section_number": number,
                "section_title": title,
            }
        )
        if len(alerts) >= max_matches:
            break
    return alerts


def detect_duplicate_headings(
    content: str,
    filename: str,
    *,
    max_matches: int = 50,
) -> list[dict]:
    """Flag the same section number appearing more than once.

    DSA specs occasionally end up with a second copy of section ``2.01`` after
    a copy/paste edit. The reviewer can still flag it, but catching it
    locally avoids paying tokens for a deterministic structural mistake.
    """
    counts: dict[str, list[tuple[str, int]]] = {}
    for number, title, h_start, _ in _iter_section_headings(content):
        counts.setdefault(number, []).append((title, h_start))

    alerts: list[dict] = []
    for number, occurrences in counts.items():
        if len(occurrences) < 2:
            continue
        # Report each occurrence after the first so users see every duplicate.
        for title, h_start in occurrences[1:]:
            ctx_start = max(0, h_start - 60)
            ctx_end = min(len(content), h_start + 120)
            alerts.append(
                {
                    "filename": filename,
                    "type": "Duplicate section heading",
                    "match": f"{number} {title}",
                    "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "position": h_start,
                    "section_number": number,
                    "occurrence_count": len(occurrences),
                }
            )
            if len(alerts) >= max_matches:
                return alerts
    return alerts


# CSI-style filenames: "23 21 13 - Hydronic Piping.docx" etc. We accept either
# space-separated triples or hyphen-separated triples but flag mixed styles
# within a single project.
_CSI_FILENAME_RE = re.compile(
    r"^\s*(\d{2})\s*(?P<sep>[\s-])\s*(\d{2})\s*(?P=sep)\s*(\d{2})\b"
)


def detect_inconsistent_file_naming(filenames: list[str]) -> list[dict]:
    """Project-level (cross-file) check for mixed CSI naming conventions.

    Returns one alert per non-conforming file when the project uses a
    dominant naming style. Used by the GUI/pipeline to warn before
    submission. No model tokens are spent.
    """
    if len(filenames) < 2:
        return []
    sep_counts: dict[str, int] = {"space": 0, "dash": 0, "other": 0}
    parsed: dict[str, str] = {}
    for fname in filenames:
        match = _CSI_FILENAME_RE.match(fname)
        if not match:
            parsed[fname] = "other"
            sep_counts["other"] += 1
            continue
        sep = match.group("sep")
        style = "space" if sep == " " else "dash"
        parsed[fname] = style
        sep_counts[style] += 1

    dominant = max(sep_counts, key=lambda k: sep_counts[k])
    if sep_counts[dominant] == 0 or dominant == "other":
        return []
    alerts: list[dict] = []
    for fname, style in parsed.items():
        if style == dominant:
            continue
        alerts.append(
            {
                "filename": fname,
                "type": f"Inconsistent CSI filename style (expected {dominant}-separated)",
                "match": fname,
                "context": fname,
                "position": 0,
                "dominant_style": dominant,
                "found_style": style,
            }
        )
    return alerts


def preprocess_spec(
    content: str,
    filename: str,
    *,
    cycle: Optional[CodeCycle] = None,
) -> PreprocessResult:
    """Run all detection passes on a single specification.

    Phase 9 (plan section 13.1): when ``cycle`` is provided, also run stale
    code-cycle detection and structural checks. ``cycle=None`` preserves the
    pre-Phase-9 behavior so callers that have not yet plumbed the cycle do
    not regress.
    """
    code_cycle_alerts: list[dict] = []
    if cycle is not None:
        code_cycle_alerts = detect_stale_code_cycle_references(content, filename, cycle)
    structural_alerts = (
        detect_empty_sections(content, filename)
        + detect_duplicate_headings(content, filename)
    )
    return PreprocessResult(
        leed_alerts=detect_leed_references(content, filename),
        placeholder_alerts=detect_placeholders(content, filename),
        code_cycle_alerts=code_cycle_alerts,
        structural_alerts=structural_alerts,
    )
