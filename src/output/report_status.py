"""Report trust-model statuses for Spec Critic findings.

A single closed set of statuses every finding maps to for display
(:class:`ReportStatus`), plus a matching closed set of edit-action labels
(:class:`EditActionLabel`). Both are *derived* from already-stored Finding
fields (``verification``, ``suppression_reason``, ``edit_proposal``) —
nothing on the Finding itself changes. Reports use these to make
uncertainty visible: a CONFIRMED + grounded finding renders differently
from a DISPUTED one, and a finding carrying a suggested edit renders
differently from a coordination claim with no proposal.

This app emits edit instructions but never applies them, so
:class:`EditActionLabel` is a simple "does this finding carry a suggested
edit?" classification — ``EDIT_SUGGESTED`` / ``REPORT_ONLY`` /
``SUPPRESSED``. Any confidence- or verdict-based gating for *applying* an
edit is a downstream applier's responsibility; the finding's verification
status and ``edit_confidence`` ride along in the report and JSON sidecar
for that purpose.

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
    # Chunk 12 / Trust Upgrade: the initial verifier (Sonnet) and the
    # escalation verifier (Opus) returned different verdicts AND both
    # passes were grounded (each had at least one accepted citation).
    # Distinct from VERIFIED_SUPPORTED / VERIFIED_CONTRADICTED because
    # the disagreement itself is a quality signal the reviewer needs to
    # see: even though the final verdict may be grounded and supported
    # in isolation, the fact that two capable models reading the same
    # sources reached different conclusions is reason enough to flag the
    # finding for manual review so a downstream applier can withhold the
    # edit. The status overrides the per-verdict classifications so a
    # CONFIRMED final verdict that disagreed with an initial DISPUTED
    # renders as VERIFIED_CONTESTED, not VERIFIED_SUPPORTED.
    VERIFIED_CONTESTED = "VERIFIED_CONTESTED"


class EditActionLabel(str, Enum):
    """Whether a finding carries a suggested edit for a downstream applier.

    This app no longer applies edits — it emits them. ``EDIT_SUGGESTED``
    marks a finding that carries a structured edit proposal (existing →
    replacement); ``REPORT_ONLY`` has no proposal; ``SUPPRESSED`` was
    dropped by cross-check dependency tracking.
    """

    EDIT_SUGGESTED = "EDIT_SUGGESTED"
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
    ReportStatus.VERIFIED_CONTESTED: "Verified — but models disagreed",
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
    # Chunk 12 / Trust Upgrade: lightning bolt signals "two verifiers,
    # different verdicts." Distinct from ⚠ (operational failure) and
    # the verdict glyphs (✓ / ✎ / ✗).
    ReportStatus.VERIFIED_CONTESTED: "⚡",
}

EDIT_ACTION_LABELS: Final[dict[EditActionLabel, str]] = {
    EditActionLabel.EDIT_SUGGESTED: "Edit suggested",
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
    4. ``models_disagreed`` sentinel set → ``VERIFIED_CONTESTED``
       (Chunk 12 / Trust Upgrade). Set when the initial and escalated
       passes returned different verdicts and BOTH were grounded.
       Placed above the verdict-based branches so a CONFIRMED+grounded
       final verdict that disagreed with the initial DISPUTED still
       renders as VERIFIED_CONTESTED — the disagreement itself is the
       quality signal the reviewer needs to see, not the headline
       verdict.
    5. ``cache_status == "local_skip"`` → ``LOCALLY_CLASSIFIED``.
    6. Verdict ``CONFIRMED`` + grounded + accepted citation
       → ``VERIFIED_SUPPORTED``.
    7. Verdict ``CORRECTED`` + grounded + accepted citation
       → ``VERIFIED_CONTRADICTED``.
    8. Verdict ``DISPUTED`` → ``DISPUTED``.
    9. Everything else (UNVERIFIED, an ungrounded CONFIRMED/CORRECTED
       that slipped past :func:`_enforce_grounding_invariant`, a
       CONFIRMED/CORRECTED with no accepted citation, unknown verdict
       strings) → ``INSUFFICIENT_EVIDENCE``.

    Chunk 5 — the explicit accepted-citation check on rules 6/7 is
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
    # Chunk 12: models-disagreed sentinel beats the verdict-based
    # branches below. The verifier only sets this when the initial and
    # escalated passes BOTH grounded their verdicts AND reached
    # different conclusions, so the result represents a real
    # disagreement worth surfacing. A swap during escalation could
    # leave ``verdict`` looking CONFIRMED-and-grounded — without this
    # short-circuit the report would render VERIFIED_SUPPORTED and hide
    # the disagreement, which is exactly the bug Chunk 12 fixes.
    if bool(getattr(verification, "models_disagreed", False)):
        return ReportStatus.VERIFIED_CONTESTED
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


def is_budget_exhausted(finding) -> bool:
    """Return True when the verifier consumed its full search budget.

    Chunk 13 / Trust Upgrade. Surfaces the actionable signal that an
    operator could grant more budget by raising the finding's severity
    (severity-tiered budgets in ``api_config._SEVERITY_MAX_USES``). The
    verifier sets ``VerificationResult.budget_exhausted=True`` whenever
    ``web_search_requests >= decision.web_search_max_uses`` on an
    UNVERIFIED result; this helper just defensively reads the flag so
    the renderer / banner code can branch on it without poking the
    private verifier state.

    Returns False when the finding has no verification, when the
    flag is absent (legacy resume payload / cache replay — the field
    defaults to False on those), or when the underlying boolean is
    False. The helper deliberately does not check the verdict here —
    the field is only ever set on UNVERIFIED in the production paths,
    so a stray-set on a CONFIRMED would still surface the badge to a
    reviewer, which is the safer failure mode.
    """
    verification = getattr(finding, "verification", None)
    if verification is None:
        return False
    return bool(getattr(verification, "budget_exhausted", False))


def summarize_budget_exhausted(findings: Iterable) -> int:
    """Return the count of findings whose verifier exhausted its budget.

    Chunk 13 / Trust Upgrade. Used by the Run Diagnostics banner so a
    reviewer can see at a glance how many findings hit the search
    budget without resolving — actionable input for "should I re-run
    these at higher severity?" The flag round-trips through resume
    state so the count survives a resumed run.
    """
    return sum(1 for finding in findings if is_budget_exhausted(finding))


def classify_edit_action(finding) -> EditActionLabel:
    """Map a :class:`Finding` to its :class:`EditActionLabel`.

    Rules in priority order:

    1. ``suppression_reason`` set → ``SUPPRESSED``.
    2. No edit proposal → ``REPORT_ONLY``.
    3. Otherwise → ``EDIT_SUGGESTED``.

    This app emits edit instructions but never applies them, so the
    label is a simple "does this finding carry a suggested edit?"
    classification. Any confidence- or verdict-based gating for
    *applying* the edit is the downstream applier's responsibility.
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
    return EditActionLabel.EDIT_SUGGESTED


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
# VERIFIED_CONTESTED (Chunk 12) sits between VERIFIED_CONTRADICTED and
# DISPUTED — it's a verified-with-caveat verdict (the disagreement
# itself is the caveat) so it reads naturally next to the other
# verified buckets, before the uncertain block (LOCALLY_CLASSIFIED,
# INSUFFICIENT_EVIDENCE, DISPUTED).
STATUS_DISPLAY_ORDER: Final[tuple[ReportStatus, ...]] = (
    ReportStatus.VERIFIED_SUPPORTED,
    ReportStatus.VERIFIED_CONTRADICTED,
    ReportStatus.VERIFIED_CONTESTED,
    ReportStatus.LOCALLY_CLASSIFIED,
    ReportStatus.INSUFFICIENT_EVIDENCE,
    ReportStatus.DISPUTED,
    ReportStatus.VERIFICATION_FAILED,
    ReportStatus.NOT_CHECKED,
    ReportStatus.MANUAL_REVIEW_REQUIRED,
)

EDIT_ACTION_DISPLAY_ORDER: Final[tuple[EditActionLabel, ...]] = (
    EditActionLabel.EDIT_SUGGESTED,
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
