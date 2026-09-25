"""Trust-model gating: which sidecar entries this run is allowed to write.

Spec Critic classifies every finding into one of nine ``ReportStatus`` values
and deliberately declines to gate on them itself — the app emits, it does not
apply, and ``classify_edit_action`` is a bare "is there a proposal?" check
whose docstring says verification status and ``edit_confidence`` "ride along
in the report and the JSON sidecar so a downstream applier can do its own
gating". This module is that gating.

Three named policies rather than a pile of flags, because the interesting
question a reviewer asks is "how much do I trust this batch?", not "which of
nine enum members do I enumerate today":

``strict``
    Only findings a verifier confirmed **as the review model stated them**.
``conservative`` *(default)*
    Adds the locally-classified findings — deterministic detector hits like a
    ``[SELECT]`` placeholder or a TODO marker, where a web search adds no
    signal and the defect is not a matter of code interpretation.
``all``
    Every entry, including disputed and contested ones. Still location-gated
    and still tracked-changes by default, so "all" means "put it in front of
    me in Word", not "trust it".

Three statuses are withheld from **every** policy including ``all``, and
that asymmetry is the one worth defending.

``DISPUTED`` is the verdict that tells a reviewer to *discard* the finding,
and ``VERIFIED_CONTESTED`` means the two verifiers reached different grounded
conclusions. Writing either into a document — even as a tracked change —
inverts the signal the trust model exists to send.

``VERIFIED_CONTRADICTED`` is withheld for a different and less obvious
reason. It is the `CORRECTED` verdict: the verifier grounded a **correction**
to the finding's claim, which `VerificationResult.correction` carries and both
report exporters render. **The sidecar does not serialize that correction**,
and nothing regenerates the proposal after verification — so the
`edit_proposal` an applier receives is still the review model's original,
*pre-correction* wording. Applying it can write in exactly the text the
verifier refuted. The calibration fixture
`tp_dc_corrected_misattributed_amendment` is the worked example: the proposal
rewrites "Test fire pumps annually per NFPA 25." to cite a provincial
amendment, and the verifier's correction is "the base standard interval
governs — no provincial amendment shortens it". The correct action is to
leave the clause alone; the sidecar alone cannot tell an applier that, because
it carries the verdict and not the correction. Until a proposal is
regenerated from the correction, this status is a *read the report* signal,
not an executable instruction.

``--force-status`` exists for the reviewer who has read the evidence panel and
the correction line and decided anyway; nothing else opens that door.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.output.report_status import ReportStatus

from .models import EditEntry


class Policy(str, Enum):
    STRICT = "strict"
    CONSERVATIVE = "conservative"
    ALL = "all"


#: A verifier grounded the claim **as the review model stated it**, so the
#: proposal it carries is the one the verdict is about. ``CORRECTED`` is
#: deliberately absent — see ``SUPERSEDED`` below.
_SETTLED = frozenset({ReportStatus.VERIFIED_SUPPORTED.value})

#: Resolved without a web call because a deterministic detector or the
#: keyword/triage classifier already answered it.
_LOCAL = frozenset({ReportStatus.LOCALLY_CLASSIFIED.value})

#: No verdict was reached. Not evidence of a problem; not evidence of none.
_UNSETTLED = frozenset(
    {
        ReportStatus.INSUFFICIENT_EVIDENCE.value,
        ReportStatus.NOT_CHECKED.value,
        ReportStatus.VERIFICATION_FAILED.value,
        ReportStatus.MANUAL_REVIEW_REQUIRED.value,
    }
)

#: The verdict argues against acting on the finding at all.
COUNTERSIGNALLED = frozenset(
    {
        ReportStatus.DISPUTED.value,
        ReportStatus.VERIFIED_CONTESTED.value,
    }
)

#: The verifier corrected the finding's claim, but the sidecar carries the
#: pre-correction proposal and not the correction itself, so the instruction
#: an applier holds no longer matches the verdict attached to it.
SUPERSEDED = frozenset({ReportStatus.VERIFIED_CONTRADICTED.value})

#: Never applied by any policy. Only ``--force-status`` reaches these.
NEVER_AUTOMATIC = COUNTERSIGNALLED | SUPERSEDED

_ALLOWED_BY_POLICY: dict[Policy, frozenset[str]] = {
    Policy.STRICT: _SETTLED,
    Policy.CONSERVATIVE: _SETTLED | _LOCAL,
    Policy.ALL: _SETTLED | _LOCAL | _UNSETTLED,
}


def _describe_status(status: str) -> str:
    if status in SUPERSEDED:
        return (
            "the verifier corrected this finding's claim, and the sidecar "
            "carries the original pre-correction proposal rather than the "
            "correction — applying it risks writing in the wording the "
            "verifier refuted; read the report's Correction line for this "
            "finding"
        )
    if status in COUNTERSIGNALLED:
        if status == ReportStatus.DISPUTED.value:
            return (
                "verification disputed this finding — applying it would write "
                "in a change the evidence argues against"
            )
        return (
            "the initial and escalated verifiers reached different grounded "
            "conclusions; the disagreement is the signal"
        )
    if status in _UNSETTLED:
        return f"no verdict was reached ({status or 'no status'})"
    return f"status {status or 'unknown'} is outside this policy"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class PolicyConfig:
    """The gate one run applies to every entry."""

    policy: Policy = Policy.CONSERVATIVE
    #: Minimum ``edit_confidence`` (the review model's self-rating of the
    #: edit, frozen before verification ran). Default 0.0 — off — because
    #: the status gate is the meaningful one and stacking a second numeric
    #: threshold by default would hide entries for a reason the reviewer
    #: never chose.
    min_edit_confidence: float = 0.0
    #: Report statuses to admit regardless of policy, including the two
    #: countersignalled ones. An explicit override by a reviewer who has
    #: read the evidence.
    force_statuses: frozenset[str] = frozenset()
    #: Restrict to these ids, when supplied (``--only``). A finding id
    #: (``rf-…`` / ``cf-…`` / ``lc-…``) selects every entry of that finding —
    #: in schemas 6 and 7, every place it applies — and an occurrence id
    #: (``oc-…``) selects exactly one place.
    only_finding_ids: frozenset[str] = frozenset()

    def decide(self, entry: EditEntry) -> PolicyDecision:
        if self.only_finding_ids and not (
            entry.finding_id in self.only_finding_ids
            or (entry.occurrence_id and entry.occurrence_id in self.only_finding_ids)
        ):
            return PolicyDecision(False, "not in the requested ids (--only)")
        status = entry.report_status or ""
        if status in self.force_statuses:
            forced = PolicyDecision(True, f"forced by --force-status {status}")
        elif status in NEVER_AUTOMATIC:
            return PolicyDecision(
                False,
                f"{_describe_status(status)}; pass --force-status {status} to "
                "override",
            )
        elif status not in _ALLOWED_BY_POLICY[self.policy]:
            return PolicyDecision(
                False,
                f"{_describe_status(status)} under --policy {self.policy.value}",
            )
        else:
            forced = PolicyDecision(True)
        if entry.edit_confidence < self.min_edit_confidence:
            return PolicyDecision(
                False,
                f"edit_confidence {entry.edit_confidence:.2f} is below the "
                f"--min-edit-confidence {self.min_edit_confidence:.2f} "
                "threshold",
            )
        return forced


def parse_force_statuses(values: list[str] | None) -> frozenset[str]:
    """Normalize ``--force-status`` values against the real status set.

    An unknown status raises rather than being ignored: a typo that silently
    forces nothing would read as "I overrode the gate" while the gate held.
    """
    if not values:
        return frozenset()
    known = {member.value for member in ReportStatus}
    normalized: set[str] = set()
    for value in values:
        candidate = value.strip().upper()
        if candidate not in known:
            raise ValueError(
                f"Unknown report status {value!r}. Known statuses: "
                f"{', '.join(sorted(known))}"
            )
        normalized.add(candidate)
    return frozenset(normalized)
