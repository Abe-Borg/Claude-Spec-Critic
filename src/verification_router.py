"""Verification model routing and local pre-classification.

Splits verification into Sonnet-first + Opus-escalation routing, and
locally classifies findings that do not need external web grounding so
we don't pay tokens for them.
"""
from __future__ import annotations

import re

from .api_config import (
    VERIFICATION_ESCALATION_MODEL,
    VERIFICATION_MODEL_DEFAULT,
)
from .reviewer import Finding


_ESCALATION_SEVERITIES = frozenset({"CRITICAL", "HIGH"})



_LOCAL_SKIP_KEYWORDS = (
    "placeholder",
    "[select]",
    "[verify]",
    "[insert",
    "tbd",
    "todo",
    "fixme",
    "xxx",
    "???",
    "lorem ipsum",
    "duplicate paragraph",
    "duplicate heading",
    "duplicate section",
    "empty section",
    "internal contradiction",
    "missing placeholder",
    "leed",
    "formatting",
    "typo",
    "invalid code cycle",
    "invalid california code cycle",
    "template marker",
    "inconsistent csi",
    "inconsistent filename",
)


def local_skip_enabled() -> bool:
    """Whether to short-circuit verification for clearly local findings.

    Always True. Classifying placeholder/LEED/typo/duplicate-paragraph
    GRIPES as ``local_skip`` avoids paying for web searches that add no
    signal.
    """
    return True


def classify_finding_for_verification(finding: Finding) -> str:
    """Classify how a finding should be verified.

    Returns one of:
    - ``"web_required"``  — needs external grounding (default)
    - ``"local_skip"``    — locally diagnosable; no web search needed
    """
    if (finding.codeReference or "").strip():
        return "web_required"

    severity = (finding.severity or "").strip().upper()
    if severity != "GRIPES":
        return "web_required"

    text = " ".join(
        s for s in (
            finding.issue or "",
            finding.existingText or "",
            finding.replacementText or "",
        ) if s
    ).lower()
    if not text:
        return "web_required"

    if any(keyword in text for keyword in _LOCAL_SKIP_KEYWORDS):
        return "local_skip"
    return "web_required"




def initial_verification_model() -> str:
    """Model used for the first verification pass."""
    return VERIFICATION_MODEL_DEFAULT


def should_escalate_verification(
    finding: Finding,
    *,
    verdict: str,
    grounded: bool,
    successful_source_count: int,
    search_error_count: int,
) -> bool:
    """Decide whether to retry a verification with the escalation model.

    Escalation only fires when the initial verifier is not already the
    escalation model.
    """
    if VERIFICATION_MODEL_DEFAULT == VERIFICATION_ESCALATION_MODEL:
        return False

    severity = (finding.severity or "").strip().upper()
    if severity not in _ESCALATION_SEVERITIES:
        return False

    verdict_upper = (verdict or "").strip().upper()
    if verdict_upper == "UNVERIFIED":
        return True
    if not grounded:
        return True
    if search_error_count > 0 and successful_source_count == 0:
        return True
    return False
