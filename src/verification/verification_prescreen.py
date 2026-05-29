"""Pre-screening decisions made before a finding is verified.

Two responsibilities, both answering "what verification treatment does this
finding get?" before any request is built:

* **Local pre-classification** — flag findings that do not need external web
  grounding (placeholders, TODOs, duplicate paragraphs, …) so we don't pay
  tokens verifying them.
* **Escalation policy** — pick the Sonnet-first initial model and decide
  when an UNVERIFIED result warrants an Opus escalation pass.

Distinct from :mod:`verification_routing`, which takes these decisions and
builds the concrete request (tools, headers, budgets) sent to the API.
"""
from __future__ import annotations


from ..core.api_config import (
    VERIFICATION_ESCALATION_MODEL,
    VERIFICATION_MODEL_DEFAULT,
)
from ..review.reviewer import Finding


# Severities that warrant Opus escalation when the first pass returns
# UNVERIFIED. CRITICAL/HIGH findings drive go/no-go decisions in DSA review.
_ESCALATION_SEVERITIES = frozenset({"CRITICAL", "HIGH"})


# ---------------------------------------------------------------------------
# Local pre-classification (plan section 7.3)
# ---------------------------------------------------------------------------

# Tokens that strongly indicate a finding is a local quality gripe / placeholder
# / duplicate, where web search adds no signal. Conservative on purpose.
#
# Extended with the additional rule names produced by the new
# deterministic checks. A GRIPES-severity finding whose ``issue`` text says
# "duplicate paragraph" or "invalid code cycle year" should not pay for a
# Sonnet+web_search round-trip because the preprocessor already detected
# the same problem locally. Keep this aligned with the
# ``preprocessor.DETERMINISTIC_RULE_*`` constants for parity with the rule
# labels the report renders.
#
# Tightened: ``"formatting"`` was removed because a real CMC
# formatting requirement (e.g. "label valves per ASME A13.1 color
# formatting") could match and silently bypass verification. ``"leed"``
# and ``"internal contradiction"`` were moved to
# :data:`_LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED` so they still route to
# local_skip (web search adds no signal for either) but are tagged with
# ``requires_elevated_confidence=True`` on the verification result,
# raising the bar for the residual-risk classes.
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
    "missing placeholder",
    "typo",
    "invalid code cycle",
    "invalid california code cycle",
    "template marker",
    "inconsistent csi",
    "inconsistent filename",
)

# Keywords that still route to local_skip (web search adds no
# signal) but tag the resulting :class:`VerificationResult` with
# ``requires_elevated_confidence=True``. The flag is retained as telemetry
# for a downstream applier; nothing in this app consumes it for routing.
# These are the residual-risk classes: a model-reported "LEED reference is
# inappropriate" claim or an "internal contradiction" claim is locally
# diagnosable, but the model's confidence in *which* text to edit is lower
# than for a plain placeholder / template marker, so a downstream applier
# may want a higher bar before acting on them. Web verification wouldn't
# add evidence for either class anyway.
_LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED = (
    "leed",
    "internal contradiction",
)


def local_skip_enabled() -> bool:
    """Whether to short-circuit verification for clearly local findings.

    Always True. Classifying placeholder/LEED/typo/duplicate-paragraph
    GRIPES as ``local_skip`` avoids paying for web searches that add no
    signal.
    """
    return True


def _normalized_finding_text(finding: Finding) -> str:
    return " ".join(
        s for s in (
            finding.issue or "",
            finding.existingText or "",
            finding.replacementText or "",
        ) if s
    ).lower()


def classify_finding_for_verification(finding: Finding) -> str:
    """Classify how a finding should be verified.

    Returns one of:
    - ``"web_required"``  — needs external grounding (default)
    - ``"local_skip"``    — locally diagnosable; no web search needed

    Keywords in :data:`_LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED`
    still route to ``"local_skip"`` (so the routing decision is unchanged
    for those keywords — they still avoid the web-search round trip), but
    callers should also consult :func:`local_skip_requires_elevated_confidence`
    to decide whether to stamp the resulting :class:`VerificationResult`
    with the elevated-confidence flag.
    """
    # Findings that cite a code reference always need external grounding.
    if (finding.codeReference or "").strip():
        return "web_required"

    severity = (finding.severity or "").strip().upper()
    # Only the lowest-severity bucket is eligible for skip. Anything higher
    # gets web verification even without a code reference.
    if severity != "GRIPES":
        return "web_required"

    text = _normalized_finding_text(finding)
    if not text:
        return "web_required"

    if any(keyword in text for keyword in _LOCAL_SKIP_KEYWORDS):
        return "local_skip"
    if any(keyword in text for keyword in _LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED):
        return "local_skip"
    return "web_required"


def local_skip_requires_elevated_confidence(finding: Finding) -> bool:
    """Return True iff a local-skip finding matched an elevated-confidence keyword.

    ``"leed"`` and ``"internal contradiction"`` were moved from
    :data:`_LOCAL_SKIP_KEYWORDS` into
    :data:`_LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED`. The routing decision
    is unchanged (those keywords still route to local_skip), but a finding
    that matched ONLY the elevated list should carry the flag as telemetry
    so a downstream applier can apply a higher bar before acting on that
    finding.

    A finding matching BOTH the regular keyword list and the elevated
    list does NOT get the flag — the regular-list match is the stronger
    signal (the preprocessor's deterministic detectors map directly to
    those keywords) and the residual-risk concern only applies when the
    elevated list is the sole reason the finding is local-skip eligible.
    Returns False when the finding wouldn't route to local_skip at all.
    """
    if (finding.codeReference or "").strip():
        return False
    severity = (finding.severity or "").strip().upper()
    if severity != "GRIPES":
        return False
    text = _normalized_finding_text(finding)
    if not text:
        return False
    if any(keyword in text for keyword in _LOCAL_SKIP_KEYWORDS):
        return False
    return any(keyword in text for keyword in _LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED)


# ---------------------------------------------------------------------------
# Model routing (plan section 7.1)
# ---------------------------------------------------------------------------


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
    # Escalate when the first pass failed to verify high-stakes findings,
    # or when search returned nothing usable despite trying.
    if verdict_upper == "UNVERIFIED":
        return True
    if not grounded:
        return True
    if search_error_count > 0 and successful_source_count == 0:
        return True
    return False
