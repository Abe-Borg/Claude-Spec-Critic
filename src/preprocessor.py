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
    (r"(?i)\bLEED\b", "LEED reference"),
    (r"(?i)\bLEED[-\s]?NC\b", "LEED-NC reference"),
    (r"(?i)\bLEED[-\s]?CI\b", "LEED-CI reference"),
    (r"(?i)\bLEED[-\s]?EB\b", "LEED-EB reference"),
    (r"(?i)\bUSGBC\b", "USGBC reference"),
    (r"(?i)\bGreen\s+Building\b", "Green Building reference"),
]

PLACEHOLDER_PATTERNS: list[tuple[str, str]] = [
    (r"\[\s*INSERT[^\]]*\]", "INSERT placeholder"),
    (r"\[\s*VERIFY[^\]]*\]", "VERIFY placeholder"),
    (r"\[\s*EDIT[^\]]*\]", "EDIT placeholder"),
    (r"\[\s*SELECT[^\]]*\]", "SELECT placeholder"),
    (r"\[\s*COORDINATE[^\]]*\]", "COORDINATE placeholder"),
    (r"\[\s*TO\s+BE\s+DETERMINED[^\]]*\]", "TBD placeholder"),
    (r"\[\s*TBD[^\]]*\]", "TBD placeholder"),
    (r"\[\s*N\/A[^\]]*\]", "N/A placeholder"),
    (r"\[\s*OPTION[^\]]*\]", "OPTION placeholder"),
    (r"<\s*VERIFY[^>]*>", "VERIFY tag"),
    (r"<\s*EDIT[^>]*>", "EDIT tag"),
    (r"<\s*INSERT[^>]*>", "INSERT tag"),
    (r"_{3,}", "Underscore placeholder"),
    (r"\[\s*\.\.\.\s*\]", "Ellipsis placeholder"),
]


# -----------------------------------------------------------------------------
# Detection Functions
# -----------------------------------------------------------------------------
def _find_matches(patterns: Iterable[tuple[str, str]], content: str, filename: str, max_matches: int) -> list[dict]:
    """Find all matches for a set of regex patterns in content."""
    alerts: list[dict] = []
    for pattern, description in patterns:
        try:
            for match in re.finditer(pattern, content):
                start = max(0, match.start() - 60)
                end = min(len(content), match.end() + 60)
                ctx = content[start:end].replace("\n", " ").strip()

                alerts.append(
                    {
                        "filename": filename,
                        "type": description,
                        "match": match.group(0),
                        "context": ctx,
                        "position": match.start(),
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


def preprocess_specs(specs: list[tuple[str, str]]) -> tuple[list[PreprocessResult], dict]:
    """Process multiple specs and return per-spec results plus aggregate summary."""
    results: list[PreprocessResult] = []
    all_leed: list[dict] = []
    all_ph: list[dict] = []

    for filename, content in specs:
        r = preprocess_spec(content, filename)
        results.append(r)
        all_leed.extend(r.leed_alerts)
        all_ph.extend(r.placeholder_alerts)

    summary = {
        "leed_alert_count": len(all_leed),
        "placeholder_alert_count": len(all_ph),
        "all_leed_alerts": all_leed,
        "all_placeholder_alerts": all_ph,
    }
    return results, summary