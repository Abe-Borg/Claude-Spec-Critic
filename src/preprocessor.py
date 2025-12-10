"""Preprocessor module for cleaning spec content and detecting alerts."""
import re
from dataclasses import dataclass, field


@dataclass
class PreprocessResult:
    """Result of preprocessing a specification."""
    cleaned_content: str
    original_length: int
    cleaned_length: int
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    
    @property
    def chars_removed(self) -> int:
        return self.original_length - self.cleaned_length
    
    @property
    def reduction_percent(self) -> float:
        if self.original_length == 0:
            return 0.0
        return (self.chars_removed / self.original_length) * 100


# Patterns for boilerplate removal
BOILERPLATE_PATTERNS = [
    # Specifier notes - various formats
    (r'\[Note to [Ss]pecifier[:\s][^\]]*\]', 'specifier_note'),
    (r'\[Specifier[:\s][^\]]*\]', 'specifier_note'),
    (r'\[SPECIFIER[:\s][^\]]*\]', 'specifier_note'),
    (r'(?i)\*\*\s*note to specifier\s*\*\*[^\n]*(?:\n(?!\n)[^\n]*)*', 'specifier_note'),
    (r'(?i)<<\s*note to specifier[^>]*>>', 'specifier_note'),
    
    # Placeholder brackets that span multiple words (but not single-word technical terms)
    (r'\[INSERT[^\]]*\]', 'placeholder'),
    (r'<INSERT[^>]*>', 'placeholder'),
    (r'\[INCLUDE[^\]]*\]', 'placeholder'),
    (r'\[SELECT[^\]]*\]', 'placeholder'),
    (r'\[VERIFY[^\]]*\]', 'placeholder'),
    (r'\[COORDINATE[^\]]*\]', 'placeholder'),
    
    # Copyright notices
    (r'(?i)copyright\s*©?\s*\d{4}[^\n]*(?:masterspec|arcom|bsd|speclink|deltek)[^\n]*', 'copyright'),
    (r'(?i)©\s*\d{4}\s*(?:masterspec|arcom|bsd|speclink|deltek)[^\n]*', 'copyright'),
    (r'(?i)all rights reserved[^\n]*(?:masterspec|arcom|bsd|speclink|deltek)[^\n]*', 'copyright'),
    
    # Separator lines
    (r'^[\*]{4,}\s*$', 'separator'),
    (r'^[-]{4,}\s*$', 'separator'),
    (r'^[=]{4,}\s*$', 'separator'),
    (r'^[_]{4,}\s*$', 'separator'),
    
    # Page artifacts
    (r'(?i)^page\s+\d+\s*(?:of\s*\d+)?\s*$', 'page_number'),
    (r'(?i)^\d+\s*-\s*\d+\s*$', 'page_number'),  # Format: "23 05 00 - 1"
    
    # Empty option brackets (unresolved choices)
    (r'\[\s*Option\s*[A-Z]\s*\][^\n]*\n?', 'unresolved_option'),
    (r'\[\s*or\s*\][^\n]*', 'unresolved_option'),
    
    # Revision marks from editing
    (r'(?i)\{revision[^\}]*\}', 'revision_mark'),
    
    # Hidden text markers
    (r'(?i)<<[^>]*hidden[^>]*>>', 'hidden_text'),
]

# LEED detection patterns
LEED_PATTERNS = [
    (r'(?i)\bLEED\b', 'LEED reference'),
    (r'(?i)\bUSGBC\b', 'USGBC reference'),
    (r'(?i)\bU\.S\.\s*Green\s*Building\s*Council\b', 'USGBC reference'),
    (r'(?i)\b(?:EQ|MR|SS|WE|EA|ID|IN|RP)\s*(?:Credit|Prerequisite)\s*[\d\.]+', 'LEED credit reference'),
    (r'(?i)\bgreen\s*building\s*certification\b', 'Green building certification'),
]

# Placeholder patterns to alert on (but not remove - the model should see these)
PLACEHOLDER_ALERT_PATTERNS = [
    (r'\[(?:INSERT|SPECIFY|VERIFY|COORDINATE|SELECT|INCLUDE)[^\]]*\]', 'Bracketed placeholder'),
    (r'<(?:INSERT|SPECIFY|VERIFY|COORDINATE|SELECT|INCLUDE)[^>]*>', 'Angle bracket placeholder'),
    (r'_{3,}', 'Blank line placeholder'),
    (r'\[\s*\]', 'Empty brackets'),
    (r'<\s*>', 'Empty angle brackets'),
    (r'\[TBD\]', 'TBD marker'),
    (r'\[TBC\]', 'TBC marker'),
    (r'\[XXX+\]', 'XXX placeholder'),
    (r'(?i)\b(?:xx+|___+)\s*(?:inches|feet|mm|cfm|gpm|degrees|psi|kpa)\b', 'Numeric placeholder'),
]


def detect_leed_references(content: str, filename: str) -> list[dict]:
    """
    Detect LEED-related references in the content.
    
    Args:
        content: The specification text
        filename: Name of the file being processed
        
    Returns:
        List of dicts with LEED alert details
    """
    alerts = []
    lines = content.split('\n')
    
    for line_num, line in enumerate(lines, 1):
        for pattern, description in LEED_PATTERNS:
            matches = re.finditer(pattern, line)
            for match in matches:
                alerts.append({
                    'filename': filename,
                    'line': line_num,
                    'type': description,
                    'text': match.group(0),
                    'context': line.strip()[:100]
                })
    
    # Deduplicate by line number and type
    seen = set()
    unique_alerts = []
    for alert in alerts:
        key = (alert['filename'], alert['line'], alert['type'])
        if key not in seen:
            seen.add(key)
            unique_alerts.append(alert)
    
    return unique_alerts


def detect_placeholders(content: str, filename: str) -> list[dict]:
    """
    Detect unresolved placeholders in the content.
    
    Args:
        content: The specification text
        filename: Name of the file being processed
        
    Returns:
        List of dicts with placeholder alert details
    """
    alerts = []
    lines = content.split('\n')
    
    for line_num, line in enumerate(lines, 1):
        for pattern, description in PLACEHOLDER_ALERT_PATTERNS:
            matches = re.finditer(pattern, line)
            for match in matches:
                alerts.append({
                    'filename': filename,
                    'line': line_num,
                    'type': description,
                    'text': match.group(0),
                    'context': line.strip()[:100]
                })
    
    # Deduplicate
    seen = set()
    unique_alerts = []
    for alert in alerts:
        key = (alert['filename'], alert['line'], alert['text'])
        if key not in seen:
            seen.add(key)
            unique_alerts.append(alert)
    
    return unique_alerts


def strip_boilerplate(content: str) -> str:
    """
    Remove boilerplate content from specification text.
    
    Args:
        content: Raw specification text
        
    Returns:
        Cleaned text with boilerplate removed
    """
    cleaned = content
    
    for pattern, _ in BOILERPLATE_PATTERNS:
        cleaned = re.sub(pattern, '', cleaned, flags=re.MULTILINE)
    
    # Clean up excessive whitespace left behind
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
    cleaned = cleaned.strip()
    
    return cleaned


def preprocess_spec(content: str, filename: str) -> PreprocessResult:
    """
    Full preprocessing pipeline for a single specification.
    
    Args:
        content: Raw specification text
        filename: Name of the file
        
    Returns:
        PreprocessResult with cleaned content and alerts
    """
    original_length = len(content)
    
    # Detect alerts BEFORE stripping (so we have accurate line numbers)
    leed_alerts = detect_leed_references(content, filename)
    placeholder_alerts = detect_placeholders(content, filename)
    
    # Strip boilerplate
    cleaned = strip_boilerplate(content)
    
    return PreprocessResult(
        cleaned_content=cleaned,
        original_length=original_length,
        cleaned_length=len(cleaned),
        leed_alerts=leed_alerts,
        placeholder_alerts=placeholder_alerts
    )


def preprocess_multiple_specs(specs: list[tuple[str, str]]) -> tuple[list[PreprocessResult], dict]:
    """
    Preprocess multiple specifications.
    
    Args:
        specs: List of (filename, content) tuples
        
    Returns:
        Tuple of (list of PreprocessResults, summary dict)
    """
    results = []
    total_original = 0
    total_cleaned = 0
    all_leed_alerts = []
    all_placeholder_alerts = []
    
    for filename, content in specs:
        result = preprocess_spec(content, filename)
        results.append(result)
        total_original += result.original_length
        total_cleaned += result.cleaned_length
        all_leed_alerts.extend(result.leed_alerts)
        all_placeholder_alerts.extend(result.placeholder_alerts)
    
    summary = {
        'total_original_chars': total_original,
        'total_cleaned_chars': total_cleaned,
        'total_chars_removed': total_original - total_cleaned,
        'reduction_percent': ((total_original - total_cleaned) / total_original * 100) if total_original > 0 else 0,
        'leed_alert_count': len(all_leed_alerts),
        'placeholder_alert_count': len(all_placeholder_alerts),
        'all_leed_alerts': all_leed_alerts,
        'all_placeholder_alerts': all_placeholder_alerts,
    }
    
    return results, summary
