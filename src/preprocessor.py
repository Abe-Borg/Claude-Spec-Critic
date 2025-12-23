"""Preprocessor module for cleaning spec content and detecting alerts."""
import re
from dataclasses import dataclass, field


@dataclass
class PreprocessResult:
    """Result of preprocessing a specification."""
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    
    

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





def preprocess_spec(content: str, filename: str) -> PreprocessResult:
    """
    Full preprocessing pipeline for a single specification.
    
    Args:
        content: Raw specification text
        filename: Name of the file
        
    Returns:
        PreprocessResult with cleaned content and alerts
    """
    
    # Detect alerts BEFORE stripping (so we have accurate line numbers)
    leed_alerts = detect_leed_references(content, filename)
    placeholder_alerts = detect_placeholders(content, filename)
        
    return PreprocessResult(
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
    all_leed_alerts = []
    all_placeholder_alerts = []
    
    for filename, content in specs:
        result = preprocess_spec(content, filename)
        results.append(result)
        all_leed_alerts.extend(result.leed_alerts)
        all_placeholder_alerts.extend(result.placeholder_alerts)
    
    summary = {
        'leed_alert_count': len(all_leed_alerts),
        'placeholder_alert_count': len(all_placeholder_alerts),
        'all_leed_alerts': all_leed_alerts,
        'all_placeholder_alerts': all_placeholder_alerts,
    }
    
    return results, summary
