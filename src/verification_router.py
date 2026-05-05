"""Verification model routing and local pre-classification.

Phase 3 (plan sections 7.1, 7.3): split verification into Sonnet-first +
Opus-escalation routing, and locally classify findings that do not need
external web grounding so we don't pay tokens for them.

Both behaviors are feature-flagged so Phase 2 callers see no change unless
they opt in.
"""
from __future__ import annotations

import os
import re

from .api_config import (
    VERIFICATION_ESCALATION_MODEL,
    VERIFICATION_MODEL_DEFAULT,
    verification_sonnet_default_enabled,
)
from .reviewer import Finding


# Severities that warrant Opus escalation when the first pass returns
# UNVERIFIED. CRITICAL/HIGH findings drive go/no-go decisions in DSA review.
_ESCALATION_SEVERITIES = frozenset({"CRITICAL", "HIGH"})


# ---------------------------------------------------------------------------
# Local pre-classification (plan section 7.3)
# ---------------------------------------------------------------------------

# Tokens that strongly indicate a finding is a local quality gripe / placeholder
# / duplicate, where web search adds no signal. Conservative on purpose.
_LOCAL_SKIP_KEYWORDS = (
    "placeholder",
    "[select]",
    "[verify]",
    "[insert",
    "tbd",
    "duplicate paragraph",
    "duplicate heading",
    "internal contradiction",
    "missing placeholder",
    "leed",
    "formatting",
    "typo",
)


def local_skip_enabled() -> bool:
    """Whether to short-circuit verification for clearly local findings.

    On by default: classifying placeholder/LEED/typo/duplicate-paragraph
    GRIPES as ``local_skip`` avoids paying for web searches that add no
    signal. Set SPEC_CRITIC_LOCAL_VERIFICATION_SKIP=0 to disable.
    """
    return os.environ.get("SPEC_CRITIC_LOCAL_VERIFICATION_SKIP", "1") != "0"


def classify_finding_for_verification(finding: Finding) -> str:
    """Classify how a finding should be verified.

    Returns one of:
    - ``"web_required"``  — needs external grounding (default)
    - ``"local_skip"``    — locally diagnosable; no web search needed
    """
    if not local_skip_enabled():
        return "web_required"

    # Findings that cite a code reference always need external grounding.
    if (finding.codeReference or "").strip():
        return "web_required"

    severity = (finding.severity or "").strip().upper()
    # Only the lowest-severity bucket is eligible for skip. Anything higher
    # gets web verification even without a code reference.
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


# ---------------------------------------------------------------------------
# Model routing (plan section 7.1)
# ---------------------------------------------------------------------------


def initial_verification_model() -> str:
    """Model used for the first verification pass."""
    return VERIFICATION_MODEL_DEFAULT


def escalation_verification_model() -> str:
    """Model used when escalating a low-confidence verdict."""
    return VERIFICATION_ESCALATION_MODEL


def is_eligible_for_haiku_triage(finding: Finding) -> bool:
    """Re-export of the Haiku triage eligibility filter.

    The actual implementation lives in :mod:`triage` so the safety contract
    (CRITICAL/HIGH and code-citing findings can never be locally skipped)
    is co-located with the classifier itself. This shim exists so callers
    that already import routing helpers from this module get a single
    public surface for verification routing decisions.
    """
    from .triage import is_eligible_for_haiku_triage as _impl
    return _impl(finding)


def should_escalate_verification(
    finding: Finding,
    *,
    verdict: str,
    grounded: bool,
    successful_source_count: int,
    search_error_count: int,
) -> bool:
    """Decide whether to retry a verification with the escalation model.

    Escalation only fires when Sonnet is the initial verifier; if the user
    is already on Opus, there is nowhere to escalate to.
    """
    if not verification_sonnet_default_enabled():
        return False
    if initial_verification_model() == escalation_verification_model():
        return False

    severity = (finding.severity or "").strip().upper()
    if severity not in _ESCALATION_SEVERITIES:
        return False

    verdict_upper = (verdict or "").strip().upper()
    # Escalate when the first pass failed to verify high-stakes findings,
    # or when search returned nothing usable despite trying.
    if verdict_upper == "UNVERIFIED":
        return True
    if not grounded:
        return True
    if search_error_count > 0 and successful_source_count == 0:
        return True
    return False
