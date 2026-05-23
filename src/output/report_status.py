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
AUTO_EDIT_CONFIDENCE_FLOOR: Final[float] = 0.7


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
    3. Supportive status AND ``edit_confidence >= AUTO_EDIT_CONFIDENCE_FLOOR``
       → ``AUTO_EDIT_CANDIDATE``.
    4. Else → ``MANUAL_EDIT_CANDIDATE``.
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
    edit_confidence = float(getattr(proposal, "edit_confidence", 0.5) or 0.0)
    if edit_confidence < AUTO_EDIT_CONFIDENCE_FLOOR:
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
