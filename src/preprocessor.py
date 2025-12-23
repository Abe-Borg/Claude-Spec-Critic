"""
Preprocessor module (DETECTION-ONLY).

This codebase no longer "cleans" DOCX content. It only detects:
- LEED references
- unresolved placeholders / editorial markers

If you want actual cleanup/scrubbing, that belongs in the other repo (SpecCleanse).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class PreprocessResult:
    """Result of detection-only preprocessing for a single spec."""
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)


# LEED detection patterns
LEED_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\bLEED\b", "LEED reference"),
    (r"(?i)\bLEED[-\s]?NC\b", "LEED-NC reference"),
    (r"(?i)\bLEED[-\s]?CI\b", "LEED-CI reference"),
    (r"(?i)\bLEED[-\s]?EB\b", "LEED-EB reference"),
    (r"(?i)\bUSGBC\b", "USGBC reference"),
    (r"(?i)\bGreen\s+Building\b", "Green Building reference"),
]

# Placeholder patterns (unresolved editorial markers)
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


def _find_matches(patterns: Iterable[tuple[str, str]], content: str, filename: str, max_matches: int) -> list[dict]:
    alerts: list[dict] = []
    for pattern, description in patterns:
        try:
            for match in re.finditer(pattern, content):
                # Context window
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
            # If a regex is bad, skip it rather than killing the run.
            continue
    return alerts


def detect_leed_references(content: str, filename: str, max_matches: int = 50) -> list[dict]:
    """Detect LEED-related references in spec content."""
    return _find_matches(LEED_PATTERNS, content, filename, max_matches=max_matches)


def detect_placeholders(content: str, filename: str, max_matches: int = 200) -> list[dict]:
    """Detect unresolved placeholders/editorial markers in spec content."""
    return _find_matches(PLACEHOLDER_PATTERNS, content, filename, max_matches=max_matches)


def preprocess_spec(content: str, filename: str) -> PreprocessResult:
    """
    Detection-only preprocessing for a single specification.
    Returns alerts; does NOT modify content.
    """
    return PreprocessResult(
        leed_alerts=detect_leed_references(content, filename),
        placeholder_alerts=detect_placeholders(content, filename),
    )


def preprocess_specs(specs: list[tuple[str, str]]) -> tuple[list[PreprocessResult], dict]:
    """
    Process multiple specs and return per-spec results plus summary.
    """
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
