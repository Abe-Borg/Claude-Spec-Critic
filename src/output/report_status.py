"""Report trust-model statuses for Spec Critic findings (Chunk N).

Single closed set of statuses every finding maps to for display, plus a
matching closed set of "what should be done with this finding" edit
labels. Both are *derived* from already-stored Finding fields
(``verification``, ``suppression_reason``, ``edit_proposal``) — nothing
on the Finding itself changes. Reports use these to make uncertainty
visible: a CONFIRMED + grounded finding renders differently from a
DISPUTED one, and a high-confidence auto-edit candidate renders
differently from a coordination claim with no proposal.

The plan (Chunk N, Directive 1) enumerates seven concepts:

- Verified supported
- Verified contradicted
- Disputed
- Insufficient evidence
- Not checked
- Locally classified / deterministic
- Manual review required

and (Directive 4) four edit-action labels:

- Auto-edit candidate
- Manual edit candidate
- Report only
- Suppressed

The rules in :func:`classify_status` / :func:`classify_edit_action`
assign exactly one of each to every finding so the report never has to
make a runtime decision about "does this finding count as verified?"
inline with rendering.
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Final, Iterable


# ---------------------------------------------------------------------------
# Closed enums (Chunk N Directive 1 & 4)
# ---------------------------------------------------------------------------

class ReportStatus(str, Enum):
    """The single trust-model status applied to a finding for display.

    Inheriting from ``str`` keeps comparisons with stored / serialized
    strings ergonomic — ``ReportStatus.DISPUTED == "DISPUTED"`` is True
    so callers can persist the value as JSON without bespoke encoders.
    """

    # External verification confirmed the claim with grounded sources.
    VERIFIED_SUPPORTED = "VERIFIED_SUPPORTED"
    # External verification corrected the claim with grounded sources.
    VERIFIED_CONTRADICTED = "VERIFIED_CONTRADICTED"
    # Verifier explicitly disputed the claim (not just unverified).
    DISPUTED = "DISPUTED"
    # Verifier ran but could not produce a grounded verdict.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    # Router decided no external verification was warranted.
    LOCALLY_CLASSIFIED = "LOCALLY_CLASSIFIED"
    # Finding never reached the verifier.
    NOT_CHECKED = "NOT_CHECKED"
    # Cross-check suppression or other manual-review-required path.
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    # Chunk 3 / Trust Upgrade: the verifier attempted to run but failed
    # operationally (rate limit, server error, network failure, parse
    # error, batch cancellation, INVALID_REQUEST). Distinct from
    # INSUFFICIENT_EVIDENCE — that status means "verifier ran cleanly
    # but couldn't ground a claim"; this one means "verifier broke,
    # nothing was checked." Operators need the distinction so they can
    # re-run the failures rather than treating them as verifier silence.
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class EditActionLabel(str, Enum):
    """How (or whether) a finding's edit proposal should be applied."""

    AUTO_EDIT_CANDIDATE = "AUTO_EDIT_CANDIDATE"
    MANUAL_EDIT_CANDIDATE = "MANUAL_EDIT_CANDIDATE"
    REPORT_ONLY = "REPORT_ONLY"
    SUPPRESSED = "SUPPRESSED"


# ---------------------------------------------------------------------------
# Display metadata
# ---------------------------------------------------------------------------

STATUS_LABELS: Final[dict[ReportStatus, str]] = {
    ReportStatus.VERIFIED_SUPPORTED: "Verified — supported",
    ReportStatus.VERIFIED_CONTRADICTED: "Verified — contradicted (correction available)",
    ReportStatus.DISPUTED: "Disputed",
    ReportStatus.INSUFFICIENT_EVIDENCE: "Insufficient evidence",
    ReportStatus.LOCALLY_CLASSIFIED: "Locally classified (deterministic)",
    ReportStatus.NOT_CHECKED: "Not checked",
    ReportStatus.MANUAL_REVIEW_REQUIRED: "Manual review required",
    ReportStatus.VERIFICATION_FAILED: "Verification failed (operational)",
}

# Short single-character glyphs for inline display.
STATUS_GLYPHS: Final[dict[ReportStatus, str]] = {
    ReportStatus.VERIFIED_SUPPORTED: "✓",
    ReportStatus.VERIFIED_CONTRADICTED: "✎",
    ReportStatus.DISPUTED: "✗",
    ReportStatus.INSUFFICIENT_EVIDENCE: "?",
    ReportStatus.LOCALLY_CLASSIFIED: "◆",
    ReportStatus.NOT_CHECKED: "—",
    ReportStatus.MANUAL_REVIEW_REQUIRED: "!",
    ReportStatus.VERIFICATION_FAILED: "⚠",
}

EDIT_ACTION_LABELS: Final[dict[EditActionLabel, str]] = {
    EditActionLabel.AUTO_EDIT_CANDIDATE: "Auto-edit candidate",
    EditActionLabel.MANUAL_EDIT_CANDIDATE: "Manual edit candidate",
    EditActionLabel.REPORT_ONLY: "Report only",
    EditActionLabel.SUPPRESSED: "Suppressed",
}


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

_VERDICT_CONFIRMED = "CONFIRMED"
_VERDICT_CORRECTED = "CORRECTED"
_VERDICT_DISPUTED = "DISPUTED"

# cache_status sentinel from verifier._local_skip_result.
_LOCAL_SKIP = "local_skip"

# Edit confidence required for AUTO_EDIT_CANDIDATE. Mirrors the
# SAFETY_AUTO_SAFE / SAFETY_AUTO_WITH_CAUTION split in edit_candidates:
# high-confidence + supported verdicts default-selected, lower needs
# manual review.
#
# Chunk 8 / Trust Upgrade: the effective floor is read at call time
# from :func:`auto_edit_confidence_floor`. ``AUTO_EDIT_CONFIDENCE_FLOOR``
# is kept as the public default value so other modules / tests can
# reference the baseline without recomputing it. ``classify_edit_action``
# always goes through the function so a process-wide env override is
# honored without restarting the interpreter.
AUTO_EDIT_CONFIDENCE_FLOOR: Final[float] = 0.7


def auto_edit_confidence_floor() -> float:
    """Effective auto-edit composite-confidence floor.

    Chunk 8 / Trust Upgrade: the floor was previously a hardcoded
    constant. Operators can now override it via the
    ``SPEC_CRITIC_AUTO_EDIT_CONFIDENCE_FLOOR`` env var without a code
    change — useful for tightening the gate while a calibration eval is
    underway, or for routing every edit through manual review during a
    cautious roll-out.

    Semantics:

    - Default: ``AUTO_EDIT_CONFIDENCE_FLOOR`` (0.7).
    - Values ``>= 1.01`` effectively disable AUTO_EDIT: composite
      confidence is bounded above by 1.0, so a threshold above 1.0 means
      no finding can ever clear it and everything routes to
      ``MANUAL_EDIT_CANDIDATE``. This is the recommended kill switch
      for emergency rollback.
    - Malformed input (non-numeric strings, blanks) or negative values
      fall back to the default so a typo never silently turns the floor
      into ``0.0`` (which would auto-apply every edit).
    - Whitespace around the value is tolerated.
    """
    raw = os.environ.get("SPEC_CRITIC_AUTO_EDIT_CONFIDENCE_FLOOR")
    if raw is None or not raw.strip():
        return AUTO_EDIT_CONFIDENCE_FLOOR
    try:
        value = float(raw.strip())
    except ValueError:
        return AUTO_EDIT_CONFIDENCE_FLOOR
    if value < 0.0:
        return AUTO_EDIT_CONFIDENCE_FLOOR
    return value


def composite_edit_confidence(finding) -> float:
    """Composite confidence used for AUTO_EDIT eligibility (Chunk 8).

    The old gate compared the model's self-reported ``edit_confidence``
    directly against the floor. That gives the model a single dimension
    to be wrong on: a confidently-stated edit with a noisy locator
    match or an ungrounded verdict could still slip through. The
    composite multiplies four independent dimensions so weakness on any
    one of them pulls the overall number down. Calibration data drives
    where the floor should sit on top of this combined number.

    Multipliers:

    - Model edit confidence: the ``proposal.edit_confidence`` already
      stored on the finding. The product's base term.
    - Locator match confidence: ``Finding.locator_evidence["match_confidence"]``
      when populated (Chunk 4), else 1.0. A weak locator match (fuzzy /
      cross-paragraph) pulls the composite down even when the model is
      sure about the edit itself.
    - Grounding multiplier: 1.0 when the verifier ran with web-search
      grounding (``VerificationResult.grounded`` True), 0.5 otherwise.
      Ungrounded supportive verdicts (LOCALLY_CLASSIFIED, or a
      verifier verdict that did not produce accepted citations) keep
      their edit-action label but the bar to auto-apply rises.
    - Status multiplier: 1.0 for VERIFIED_SUPPORTED / VERIFIED_CONTRADICTED,
      0.85 for LOCALLY_CLASSIFIED, 0.6 otherwise. Non-supportive
      statuses are already filtered out by ``classify_edit_action``
      before the floor comparison, so the 0.6 branch only matters when
      the helper is called for display (e.g. the evidence panel
      shows the composite even on findings that won't auto-apply).

    Returns the product. When the finding has no edit proposal at all
    the composite is 0.0 — there's nothing to apply, so the number is
    not meaningful but the helper still returns a finite value rather
    than raising.
    """
    proposal = (
        finding.as_edit_proposal()
        if hasattr(finding, "as_edit_proposal")
        else None
    )
    if proposal is None:
        return 0.0

    edit_confidence = float(getattr(proposal, "edit_confidence", 0.5) or 0.0)

    locator_evidence = getattr(finding, "locator_evidence", None)
    if isinstance(locator_evidence, dict):
        locator_confidence = float(
            locator_evidence.get("match_confidence", 1.0) or 0.0
        )
    else:
        # No locator evidence stashed on the finding (legacy resume
        # payload, or the pipeline never had a paragraph map for this
        # file). Treat as locator-neutral so the composite does not
        # silently penalize the finding for missing telemetry.
        locator_confidence = 1.0

    verification = getattr(finding, "verification", None)
    grounded = bool(getattr(verification, "grounded", False))
    grounded_multiplier = 1.0 if grounded else 0.5

    status = classify_status(finding)
    if status in (
        ReportStatus.VERIFIED_SUPPORTED,
        ReportStatus.VERIFIED_CONTRADICTED,
    ):
        status_multiplier = 1.0
    elif status is ReportStatus.LOCALLY_CLASSIFIED:
        status_multiplier = 0.85
    else:
        status_multiplier = 0.6

    return (
        edit_confidence
        * locator_confidence
        * grounded_multiplier
        * status_multiplier
    )


def classify_status(finding) -> ReportStatus:
    """Map a :class:`Finding` to exactly one :class:`ReportStatus`.

    Rules in priority order (first match wins):

    1. ``suppression_reason`` set → ``MANUAL_REVIEW_REQUIRED``. Chunk M
       puts suppressed cross-check findings on a separate list, but the
       report still renders them in a dedicated section and they should
       not pretend to be supported.
    2. No ``verification`` → ``NOT_CHECKED``.
    3. ``verification_failed`` sentinel set → ``VERIFICATION_FAILED``
       (Chunk 3 / Trust Upgrade). Surfaces operational failures (rate
       limit, server error, parse error, INVALID_REQUEST, etc.) so
       reports can show them under a dedicated warning glyph instead of
       quietly conflating them with cleanly-UNVERIFIED claims.
    4. ``cache_status == "local_skip"`` → ``LOCALLY_CLASSIFIED``.
    5. Verdict ``CONFIRMED`` + grounded + accepted citation
       → ``VERIFIED_SUPPORTED``.
    6. Verdict ``CORRECTED`` + grounded + accepted citation
       → ``VERIFIED_CONTRADICTED``.
    7. Verdict ``DISPUTED`` → ``DISPUTED``.
    8. Everything else (UNVERIFIED, an ungrounded CONFIRMED/CORRECTED
       that slipped past :func:`_enforce_grounding_invariant`, a
       CONFIRMED/CORRECTED with no accepted citation, unknown verdict
       strings) → ``INSUFFICIENT_EVIDENCE``.

    Chunk 5 — the explicit accepted-citation check on rules 5/6 is
    belt-and-suspenders for the case where a finding reaches the report
    without going through :func:`src.verifier._enforce_grounding_invariant`
    (e.g. a future call site that bypasses the verifier wrapper, or a
    unit test that constructs the result directly). The verifier
    invariant already downgrades these to UNVERIFIED in production; the
    duplicate check here means the report cannot accidentally show
    "Verified — supported" for a source-less verdict.
    """
    if getattr(finding, "suppression_reason", None):
        return ReportStatus.MANUAL_REVIEW_REQUIRED
    verification = getattr(finding, "verification", None)
    if verification is None:
        return ReportStatus.NOT_CHECKED
    # Chunk 3: operational-failure sentinel beats the verdict-based
    # branches below. A finding whose verifier crashed must not be
    # reported as INSUFFICIENT_EVIDENCE (which implies the verifier ran
    # and found nothing). The sentinel is only set on transient failures
    # so it's safe to short-circuit here.
    if bool(getattr(verification, "verification_failed", False)):
        return ReportStatus.VERIFICATION_FAILED
    if getattr(verification, "cache_status", "") == _LOCAL_SKIP:
        return ReportStatus.LOCALLY_CLASSIFIED
    verdict = (getattr(verification, "verdict", "") or "").strip().upper()
    grounded = bool(getattr(verification, "grounded", False))
    has_accepted = bool(
        getattr(verification, "accepted_sources", None)
        or getattr(verification, "sources", None)
    )
    if verdict == _VERDICT_CONFIRMED and grounded and has_accepted:
        return ReportStatus.VERIFIED_SUPPORTED
    if verdict == _VERDICT_CORRECTED and grounded and has_accepted:
        return ReportStatus.VERIFIED_CONTRADICTED
    if verdict == _VERDICT_DISPUTED:
        return ReportStatus.DISPUTED
    return ReportStatus.INSUFFICIENT_EVIDENCE


# Supportive statuses for auto-edit eligibility. ``LOCALLY_CLASSIFIED``
# qualifies because the router decided no external check was needed
# (e.g. placeholder text, LEED references, internal duplicates) — these
# are self-evident from the spec itself. The locator/spec_editor
# preconditions still gate the actual mutation, so a false-supportive
# router result cannot cause a wrong-text replacement.
_SUPPORTIVE_STATUSES: Final[frozenset[ReportStatus]] = frozenset({
    ReportStatus.VERIFIED_SUPPORTED,
    ReportStatus.VERIFIED_CONTRADICTED,
    ReportStatus.LOCALLY_CLASSIFIED,
})


def classify_edit_action(finding) -> EditActionLabel:
    """Map a :class:`Finding` to its :class:`EditActionLabel`.

    Rules in priority order:

    1. Suppressed → ``SUPPRESSED``.
    2. No edit proposal → ``REPORT_ONLY``.
    3. Non-supportive status (DISPUTED / INSUFFICIENT_EVIDENCE /
       NOT_CHECKED / MANUAL_REVIEW_REQUIRED / VERIFICATION_FAILED) →
       ``MANUAL_EDIT_CANDIDATE`` regardless of confidence. A finding
       whose verifier disagreed (or never ran) is never auto-applied.
    4. Supportive status AND ``composite_edit_confidence(finding) >=
       auto_edit_confidence_floor()`` → ``AUTO_EDIT_CANDIDATE``.
    5. Else → ``MANUAL_EDIT_CANDIDATE``.

    Chunk 8 / Trust Upgrade: rule 4 used to compare the model's raw
    ``edit_confidence`` against a hardcoded floor. The composite
    accounts for locator match quality, web-search grounding, and the
    trust-model status so weakness on any dimension correctly pulls the
    finding out of the auto-apply bucket — even when the model itself
    is confident about the edit text. The floor is now overridable via
    ``SPEC_CRITIC_AUTO_EDIT_CONFIDENCE_FLOOR``; values ``>= 1.01``
    disable AUTO_EDIT entirely.
    """
    if getattr(finding, "suppression_reason", None):
        return EditActionLabel.SUPPRESSED
    # Findings constructed in legacy tests may not have ``as_edit_proposal``.
    proposal = (
        finding.as_edit_proposal()
        if hasattr(finding, "as_edit_proposal")
        else None
    )
    if proposal is None:
        return EditActionLabel.REPORT_ONLY
    status = classify_status(finding)
    if status not in _SUPPORTIVE_STATUSES:
        return EditActionLabel.MANUAL_EDIT_CANDIDATE
    composite = composite_edit_confidence(finding)
    if composite < auto_edit_confidence_floor():
        return EditActionLabel.MANUAL_EDIT_CANDIDATE
    return EditActionLabel.AUTO_EDIT_CANDIDATE


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def status_label(status: ReportStatus | str) -> str:
    """Human-readable label for a status (accepts the enum or the raw string)."""
    if isinstance(status, ReportStatus):
        return STATUS_LABELS[status]
    try:
        return STATUS_LABELS[ReportStatus(status)]
    except ValueError:
        return str(status)


def status_glyph(status: ReportStatus | str) -> str:
    """Short glyph for inline display (accepts the enum or the raw string)."""
    if isinstance(status, ReportStatus):
        return STATUS_GLYPHS[status]
    try:
        return STATUS_GLYPHS[ReportStatus(status)]
    except ValueError:
        return "?"


def edit_action_label(action: EditActionLabel | str) -> str:
    """Human-readable label for an edit-action (accepts the enum or string)."""
    if isinstance(action, EditActionLabel):
        return EDIT_ACTION_LABELS[action]
    try:
        return EDIT_ACTION_LABELS[EditActionLabel(action)]
    except ValueError:
        return str(action)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

# Stable display order for the summary table. Supportive first, then
# uncertain, then suppressed — matches the reading order on the report.
# VERIFICATION_FAILED sits next to NOT_CHECKED / MANUAL_REVIEW_REQUIRED
# (operational tail) so the supportive block stays compact at the top.
STATUS_DISPLAY_ORDER: Final[tuple[ReportStatus, ...]] = (
    ReportStatus.VERIFIED_SUPPORTED,
    ReportStatus.VERIFIED_CONTRADICTED,
    ReportStatus.LOCALLY_CLASSIFIED,
    ReportStatus.INSUFFICIENT_EVIDENCE,
    ReportStatus.DISPUTED,
    ReportStatus.VERIFICATION_FAILED,
    ReportStatus.NOT_CHECKED,
    ReportStatus.MANUAL_REVIEW_REQUIRED,
)

EDIT_ACTION_DISPLAY_ORDER: Final[tuple[EditActionLabel, ...]] = (
    EditActionLabel.AUTO_EDIT_CANDIDATE,
    EditActionLabel.MANUAL_EDIT_CANDIDATE,
    EditActionLabel.REPORT_ONLY,
    EditActionLabel.SUPPRESSED,
)


def summarize_statuses(findings: Iterable) -> dict[ReportStatus, int]:
    """Return the status histogram across an iterable of findings.

    The returned dict always contains every :class:`ReportStatus` key
    (zero-filled when the status is absent) so callers can build a
    stable table without first checking ``in``.
    """
    counts: dict[ReportStatus, int] = {s: 0 for s in ReportStatus}
    for finding in findings:
        counts[classify_status(finding)] += 1
    return counts


def summarize_edit_actions(findings: Iterable) -> dict[EditActionLabel, int]:
    """Return the edit-action histogram across an iterable of findings."""
    counts: dict[EditActionLabel, int] = {a: 0 for a in EditActionLabel}
    for finding in findings:
        counts[classify_edit_action(finding)] += 1
    return counts
