"""What a batch collection finished, and whether its saved state may go (plan WP-14).

A review batch can come back usable while the work it paid for is not done.
The collect step submits a *repair* batch for the items that failed in a
retryable way, and that batch can still be running, or out of reach, when the
primary results are ready. So two questions have separate answers:

* **Is there something to report?** :attr:`CollectionOutcome.reportable` — at
  least one submitted specification produced a usable review (a valid review
  with zero findings counts).
* **Is the remote work finished?** :attr:`CollectionOutcome.remote_settled` —
  no repair batch is still pending or unreachable.

A collection whose remote work is not settled is *provisional*: its primary
results can be shown, but every stage that depends on the full review
(finding verification, cross-spec coordination, local-code compliance, and
drawing-impact analysis) waits for the repair, so that stage runs once, on
the inputs it will finally have. The saved record needed to collect the
repair is kept.

:func:`decide_saved_state_cleanup` is the one keep-or-clear rule every entry
point uses: the GUI's single-module and program collection, and
``scripts/recover_batch.py``. It is stdlib-only, like
``compliance/completeness.py``, so the drivers cannot drift apart and the rule
is easy to test on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

# ---------------------------------------------------------------------------
# Repair states
# ---------------------------------------------------------------------------

#: No retryable item failed, so no repair was needed.
REPAIR_NOT_NEEDED = "not_needed"
#: A repair batch exists and was still processing when polling stopped.
REPAIR_PENDING = "pending"
#: A repair batch exists, but its status or results could not be read
#: (polling failed, the retrieval raised). Unknown is not finished.
REPAIR_UNREACHABLE = "unreachable"
#: The repair batch ended and its results were merged onto the primary items.
REPAIR_CONSUMED = "consumed"
#: The repair batch ended expired, failed, or canceled; its results are gone.
REPAIR_UNUSABLE = "unusable"
#: A repair was needed but no repair batch was ever created (the submission
#: failed, or the specifications it needs were unavailable).
REPAIR_NOT_SUBMITTED = "not_submitted"

REPAIR_STATES: tuple[str, ...] = (
    REPAIR_NOT_NEEDED,
    REPAIR_PENDING,
    REPAIR_UNREACHABLE,
    REPAIR_CONSUMED,
    REPAIR_UNUSABLE,
    REPAIR_NOT_SUBMITTED,
)

#: States whose remote work may still produce results. A collection in one
#: of these is provisional, and its saved record must survive.
OUTSTANDING_REPAIR_STATES = frozenset({REPAIR_PENDING, REPAIR_UNREACHABLE})

# ---------------------------------------------------------------------------
# Stages that wait for an outstanding repair
# ---------------------------------------------------------------------------

STAGE_VERIFICATION = "verification"
STAGE_CROSS_CHECK = "cross_check"
STAGE_COMPLIANCE = "compliance"
STAGE_DRAWING_IMPACT = "drawing_impact"

#: Display order and wording for the stages a provisional report names.
STAGE_LABELS: dict[str, str] = {
    STAGE_VERIFICATION: "finding verification",
    STAGE_CROSS_CHECK: "cross-spec coordination",
    STAGE_COMPLIANCE: "local-code compliance",
    STAGE_DRAWING_IMPACT: "drawing-impact analysis",
}


def stage_labels(stages: Iterable[str]) -> list[str]:
    """Human labels for ``stages``, in display order, without duplicates."""
    wanted = set(stages)
    return [label for stage, label in STAGE_LABELS.items() if stage in wanted]


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairOutcome:
    """What the collect step's repair pass did, and where that leaves it.

    ``batch_id`` names the repair batch the pass ended on (submitted now or
    re-attached from saved state); ``replaced_batch_id`` names a saved repair
    that ended unusable and was replaced in this collection. ``submitted`` is
    ``True`` only when this collection created (and so paid for) a new repair
    batch; ``reattached`` when it picked up one an earlier collection had
    submitted.
    """

    state: str = REPAIR_NOT_NEEDED
    batch_id: str | None = None
    specs: tuple[str, ...] = ()
    submitted: bool = False
    reattached: bool = False
    replaced_batch_id: str | None = None
    recovered: int = 0
    detail: str = ""
    #: What the repair batches cost (plan WP-15): one attempt record
    #: (``core.attempt_usage.AttemptUsage``) per repair request — known usage
    #: once its results were read, unknown while its batch is pending or
    #: unreachable, or when a saved repair ended unusable before its results
    #: were read. Spend telemetry only: not part of the outcome's equality or
    #: of :meth:`to_dict`, so the sidecar and every saved shape are unchanged.
    attempts: tuple = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.state not in REPAIR_STATES:
            raise ValueError(f"unknown repair state {self.state!r}")
        object.__setattr__(self, "specs", tuple(str(s) for s in self.specs))
        object.__setattr__(self, "attempts", tuple(self.attempts))

    @property
    def outstanding(self) -> bool:
        """True while the repair may still produce results."""
        return self.state in OUTSTANDING_REPAIR_STATES

    @property
    def settled_batch_ids(self) -> frozenset[str]:
        """Repair batch ids this collection finished with or superseded."""
        return frozenset(
            bid for bid in (self.batch_id, self.replaced_batch_id) if bid
        )

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "batch_id": self.batch_id,
            "specs": list(self.specs),
            "submitted": self.submitted,
            "reattached": self.reattached,
            "replaced_batch_id": self.replaced_batch_id,
            "recovered": self.recovered,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CollectionOutcome:
    """One batch collection's result, apart from its findings.

    ``submitted_specs`` / ``failed_specs`` are per review request (a file name
    per request). ``deferred_stages`` is filled in by the driver that held the
    dependent stages back; it stays empty on a settled collection.
    """

    batch_id: str
    module_id: str = ""
    transport: str = "batch"
    repair: RepairOutcome = field(default_factory=RepairOutcome)
    submitted_specs: tuple[str, ...] = ()
    failed_specs: tuple[str, ...] = ()
    deferred_stages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "submitted_specs", tuple(str(s) for s in self.submitted_specs)
        )
        object.__setattr__(
            self, "failed_specs", tuple(str(s) for s in self.failed_specs)
        )
        object.__setattr__(
            self, "deferred_stages", tuple(str(s) for s in self.deferred_stages)
        )

    @property
    def reportable(self) -> bool:
        """At least one submitted specification produced a usable review."""
        return len(self.failed_specs) < len(self.submitted_specs)

    @property
    def remote_settled(self) -> bool:
        """No repair batch is still pending or unreachable."""
        return not self.repair.outstanding

    @property
    def provisional(self) -> bool:
        """The results shown now are not final: a repair is outstanding."""
        return not self.remote_settled

    @property
    def all_failed(self) -> bool:
        """Every submitted specification failed review."""
        return bool(self.submitted_specs) and not self.reportable

    @property
    def awaiting_repair_specs(self) -> tuple[str, ...]:
        """Specifications whose repaired review has not been collected yet."""
        return self.repair.specs if self.repair.outstanding else ()

    def with_deferred_stages(self, stages: Iterable[str]) -> "CollectionOutcome":
        return replace(self, deferred_stages=tuple(stages))

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "module_id": self.module_id,
            "transport": self.transport,
            "provisional": self.provisional,
            "reportable": self.reportable,
            "remote_settled": self.remote_settled,
            "repair": self.repair.to_dict(),
            "submitted_specs": list(self.submitted_specs),
            "failed_specs": list(self.failed_specs),
            "awaiting_repair_specs": list(self.awaiting_repair_specs),
            "deferred_stages": list(self.deferred_stages),
        }


def outstanding_phrase(outcome: CollectionOutcome, *, label: str = "") -> str:
    """One sentence fragment naming what ``outcome`` is still waiting for."""
    repair = outcome.repair
    specs = list(repair.specs)
    count = len(specs)
    names = f" ({', '.join(specs)})" if specs else ""
    where = f"{label}: " if label else ""
    status = (
        "is still running"
        if repair.state == REPAIR_PENDING
        else "could not be reached"
    )
    return (
        f"{where}review repair batch {repair.batch_id or '(unknown id)'} for "
        f"{count} spec{'s' if count != 1 else ''}{names} {status}"
    )


def deferred_stages_phrase(stages: Iterable[str]) -> str:
    """``"finding verification, cross-spec coordination and …"``."""
    return _join(stage_labels(stages))


def result_deferred_stages(result) -> tuple[str, ...]:
    """Every stage a result held back: each module's plus the program's own."""
    stages: list[str] = []
    for outcome in collection_outcomes_of(result).values():
        if outcome is not None:
            stages.extend(outcome.deferred_stages)
    stages.extend(getattr(result, "deferred_program_stages", None) or ())
    wanted = set(stages)
    return tuple(stage for stage in STAGE_LABELS if stage in wanted)


def provisional_notice(result, *, label_for=None) -> str:
    """One sentence naming what a provisional result waits for, or ``""``.

    ``label_for`` maps a module id to its display name (a routed program's
    outcomes are keyed by module id); omitted, the id is used.
    """
    outcomes = collection_outcomes_of(result)
    waiting = [
        outstanding_phrase(
            outcome,
            label=(label_for(key) if label_for else key) if len(outcomes) > 1 else "",
        )
        for key, outcome in outcomes.items()
        if outcome is not None and outcome.provisional
    ]
    if not waiting:
        return ""
    stages = result_deferred_stages(result)
    deferred = (
        f" Deferred until it finishes: {deferred_stages_phrase(stages)}."
        if stages
        else ""
    )
    return (
        "Provisional result: "
        + "; ".join(waiting)
        + "."
        + deferred
        + " Collect this run again later: the repair batch is picked up, not "
        "resubmitted."
    )


# ---------------------------------------------------------------------------
# The one cleanup decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupDecision:
    """Whether a finished collection may delete its saved run record.

    ``complete`` says the run needs nothing more: every module was collected,
    no repair is outstanding, and at least one specification produced a
    usable review. ``clear`` is ``complete`` for a batch run whose caller did
    not ask to keep the record. ``saved_state_applies`` is ``False`` for a
    real-time run, which never saves a record (so there is nothing to keep
    or clear). ``reason`` is a plain sentence for the log.
    """

    clear: bool
    complete: bool
    reason: str
    saved_state_applies: bool = True


def collection_outcomes_of(result) -> dict[str, CollectionOutcome | None]:
    """Every collection outcome a pipeline result carries, keyed by module.

    Reads a routed program's ``collection_outcomes`` mapping, or a
    single-module result's ``collection_outcome``. A result without either
    yields ``{"": None}``: a missing outcome is recorded as missing, never
    dropped, so it cannot read as "nothing outstanding".
    """
    many = getattr(result, "collection_outcomes", None)
    if isinstance(many, Mapping):
        return {str(key): value for key, value in many.items()}
    one = getattr(result, "collection_outcome", None)
    key = str(getattr(one, "module_id", "") or getattr(result, "module_id", "") or "")
    return {key: one}


def run_transport(result) -> str:
    """The review transport of a pipeline result (``"batch"`` when unknown)."""
    transport = getattr(result, "review_transport", None)
    if transport:
        return str(transport)
    for outcome in collection_outcomes_of(result).values():
        if outcome is not None:
            return outcome.transport
    return "batch"


def _incomplete_reason(result, outcomes: Mapping[str, CollectionOutcome | None]) -> str:
    """Why the run still needs something, or ``""`` when it is complete."""
    module_errors = dict(getattr(result, "module_errors", None) or {})
    if module_errors:
        return (
            f"{len(module_errors)} module(s) could not be collected ("
            + "; ".join(f"{key}: {value}" for key, value in module_errors.items())
            + ")"
        )
    if not outcomes or any(value is None for value in outcomes.values()):
        return "the collection recorded no outcome, so completion is unknown"
    provisional = [
        outstanding_phrase(outcome, label=key if len(outcomes) > 1 else "")
        for key, outcome in outcomes.items()
        if outcome.provisional
    ]
    if provisional:
        return "; ".join(provisional)
    submitted = sum(len(o.submitted_specs) for o in outcomes.values())
    failed = sum(len(o.failed_specs) for o in outcomes.values())
    if submitted and failed >= submitted:
        return "every submitted specification failed review"
    return ""


def decide_saved_state_cleanup(result, *, keep_requested: bool = False) -> CleanupDecision:
    """The keep-or-clear rule for a run's saved record, used by every entry point.

    The run is incomplete, and its record is kept, when:

    1. a routed module could not be collected at all (its remote results are
       still retrievable, and a retry needs the record);
    2. a module recorded no collection outcome — missing metadata never
       authorizes deleting a paid run's only handle;
    3. a review repair batch is pending or unreachable (retrievable, not yet
       consumed);
    4. every submitted specification failed review — an all-failed run stays
       recoverable; discarding it is a separate, explicit user action.

    Otherwise the run is complete (a valid review with zero findings is
    complete), and its record is cleared unless the caller asked to keep it.
    A real-time run never saved a record, so nothing applies to it: clearing
    there could only delete another run's.
    """
    outcomes = collection_outcomes_of(result)
    reason = _incomplete_reason(result, outcomes)
    complete = not reason
    if run_transport(result) == "realtime":
        return CleanupDecision(
            clear=False,
            complete=complete,
            reason=reason or "a real-time run keeps no saved batch state",
            saved_state_applies=False,
        )
    if not complete:
        return CleanupDecision(clear=False, complete=False, reason=reason)
    if keep_requested:
        return CleanupDecision(clear=False, complete=True, reason="kept on request")
    return CleanupDecision(
        clear=True, complete=True, reason="collection complete; nothing outstanding"
    )


def saved_state_identity(result) -> dict[str, frozenset[str]]:
    """``{primary batch id: repair batch ids this collection settled}``.

    The identity a saved record must match before it is cleared: every
    primary batch the record names must belong to this run, and any repair
    batch it names must be one this collection finished with (or replaced).
    A record naming a repair this run never touched describes paid work that
    was not consumed, and is kept.
    """
    identity: dict[str, frozenset[str]] = {}
    for outcome in collection_outcomes_of(result).values():
        if outcome is None or not outcome.batch_id:
            continue
        identity[outcome.batch_id] = outcome.repair.settled_batch_ids
    return identity


def record_owned_by(
    record_identity: Mapping[str, str | None],
    run_identity: Mapping[str, Iterable[str]],
) -> bool:
    """True when a saved record (``{primary: saved repair id}``) belongs to a run.

    Every primary batch in the record must be one of the run's, and every
    saved repair id must be one the run settled. An empty record identity is
    never owned: an unidentifiable record is not ours to delete.
    """
    if not record_identity:
        return False
    for primary, repair in record_identity.items():
        if primary not in run_identity:
            return False
        if repair and repair not in set(run_identity[primary]):
            return False
    return True
