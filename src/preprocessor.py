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
    - LEED references: LEED, LEED-NC, LEED-CI, USGBC, Green Building
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
from typing import Iterable


@dataclass
class PreprocessResult:
    """"
    Result of detection-only preprocessing for a single spec.
    
    Attributes:
        leed_alerts: List of detected LEED references with context
        placeholder_alerts: List of detected placeholders with context
        
    Each alert is a dict with keys:
        - filename: Source file name
        - type: Description of what was matched (e.g., "LEED reference")
        - match: The actual matched text
        - context: ~120 char window around the match for human review
        - position: Character offset in the document
    """
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Detection Patterns
# -----------------------------------------------------------------------------

LEED_PATTERNS: list[tuple[str, str]] = [
    # Specific patterns first so they claim spans before the generic \bLEED\b
    (r"(?i)\bLEED[-\s]?NC\b", "LEED-NC reference"),
    (r"(?i)\bLEED[-\s]?CI\b", "LEED-CI reference"),
    (r"(?i)\bLEED[-\s]?EB\b", "LEED-EB reference"),
    (r"(?i)\bUSGBC\b", "USGBC reference"),
    (r"(?i)\bGreen\s+Building\b", "Green Building reference"),
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


def preprocess_spec(content: str, filename: str) -> PreprocessResult:
    """Run all detection passes on a single specification."""
    return PreprocessResult(
        leed_alerts=detect_leed_references(content, filename),
        placeholder_alerts=detect_placeholders(content, filename),
    )
