"""Run the coordination experiment over a collected run (plan EX-06).

Off by default (``SPEC_CRITIC_CROSS_COORDINATION``). When on, a collection
runs this pass last — after cross-check, compliance, their verification, and
drawing impact — so it reads the cross-check plan and the final set of
specifications whose review succeeded. It is **observation only**:

- it adds no finding, edit instruction, report line, or sidecar entry, and
  changes none (the finding groups and occurrence ids a run emits are the
  same with it on and off);
- it verifies nothing — an observation names its two sides' modules, code
  bases, and governing-basis fingerprints, so that a later stage could verify
  each side under its own module, but nothing does so now;
- what it records goes to diagnostics (one summary event, plus one event per
  candidate or observation, bounded), for a person to adjudicate.

Two modes. ``candidates`` reads every specification for coordination facts
and records the pairs it would send, at no API cost. ``observe`` also sends
them, with both passages, to the cross-check model (``adjudication.py``).

Two scopes. ``module`` compares only specifications one module's chunked
cross-check planned apart (the plan's first stage). ``program`` also compares
specifications routed to different modules of a program.

The pass keeps the contracts every package pass keeps: each request is sized
before it is sent (``RequestBudget``), a failure never raises out of the pass
and never discards another request's observations, every paid request has an
attempt record, and whatever was not assessed — candidates over a limit,
requests that failed, candidates a response left out, modules whose plan was
not recorded, specifications whose review failed — is listed, so the result
never reads as complete coordination.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from ..core.api_config import (
    COORDINATION_MODEL_DEFAULT,
    CROSS_COORDINATION_CANDIDATES,
    CROSS_COORDINATION_OBSERVE,
    CROSS_COORDINATION_SCOPE_MODULE,
    CACHE_BREAKDOWN_NONE,
)
from ..core.attempt_usage import attempt_dicts, known_totals
from .adjudication import (
    ASSESSMENT_CANNOT_TELL,
    ASSESSMENT_CONFLICT,
    ASSESSMENT_NOT_CONFLICT,
    CANDIDATES_PER_REQUEST,
    Observation,
    RequestOutcome,
    batches,
    run_request,
)
from .candidates import (
    MAX_CANDIDATES,
    MAX_PER_FILE_PAIR,
    Candidate,
    CandidateSelection,
    SpecUnit,
    chunk_groups_from,
    select_candidates,
)
from .facts import POLICY_VERSION, extract_facts

LogFn = Callable[..., None]

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

#: At most this many candidate / observation events per pass reach
#: diagnostics; the rest are counted in the summary event.
MAX_RECORDED_ITEMS = 100
#: At most this many unassessed entries are listed in the summary event.
MAX_RECORDED_UNASSESSED = 25


def _noop_log(_msg: str, **_kwargs: object) -> None:
    return


@dataclass(frozen=True)
class ModuleInput:
    """What the pass needs from one module's collected run."""

    module_id: str
    display_name: str
    specs: tuple
    failed_files: tuple[str, ...] = ()
    chunk_groups: tuple[frozenset[str], ...] | None = None
    cross_check_status: str = ""
    cycle_label: str = ""
    basis_fingerprint: str = ""


@dataclass
class CoordinationResult:
    """One run of the pass. Runtime only; read by diagnostics and the harness."""

    status: str
    mode: str
    scope: str
    reason: str = ""
    policy_version: str = POLICY_VERSION
    model: str = COORDINATION_MODEL_DEFAULT
    modules: list[dict] = field(default_factory=list)
    files_read: int = 0
    facts: int = 0
    unattributed_values: int = 0
    selection: CandidateSelection | None = None
    observations: list[Observation] = field(default_factory=list)
    requests: list[RequestOutcome] = field(default_factory=list)
    unassessed: list[str] = field(default_factory=list)
    ignored_entries: int = 0
    elapsed_seconds: float = 0.0
    # Usage, as every pass carrier holds it: attempt records are the billing
    # input (``call_usage``) and the flat fields are their known totals.
    call_usage: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_creation_unknown_input_tokens: int = 0
    cache_creation_breakdown_status: str = CACHE_BREAKDOWN_NONE
    stop_reason: str | None = None
    structured_payload: dict | None = None

    @property
    def selected(self) -> list[Candidate]:
        return list(self.selection.selected) if self.selection else []

    @property
    def complete(self) -> bool:
        """Every eligible pair the rules joined was assessed (or, in candidates
        mode, recorded), and nothing was left unread."""
        return self.status == STATUS_COMPLETED and not self.unassessed

    def counts(self) -> dict:
        tally = {ASSESSMENT_CONFLICT: 0, ASSESSMENT_NOT_CONFLICT: 0, ASSESSMENT_CANNOT_TELL: 0}
        for observation in self.observations:
            tally[observation.assessment] = tally.get(observation.assessment, 0) + 1
        return tally

    def to_dict(self) -> dict:
        """The summary record (bounded; the items ride their own events)."""
        selection = self.selection
        return {
            "status": self.status,
            "mode": self.mode,
            "scope": self.scope,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "model": self.model if self.mode == CROSS_COORDINATION_OBSERVE else "",
            "complete": self.complete,
            "modules": list(self.modules),
            "files_read": self.files_read,
            "facts": self.facts,
            "unattributed_values": self.unattributed_values,
            "stats": dict(selection.stats) if selection else {},
            "selected": len(selection.selected) if selection else 0,
            "deferred": len(selection.deferred) if selection else 0,
            "requests": len(self.requests),
            "requests_failed": sum(1 for r in self.requests if r.status == "failed"),
            "requests_skipped": sum(1 for r in self.requests if r.status == "skipped"),
            "observations": len(self.observations),
            "assessments": self.counts(),
            "ignored_entries": self.ignored_entries,
            "unassessed_count": len(self.unassessed),
            "unassessed": list(self.unassessed[:MAX_RECORDED_UNASSESSED]),
            "attempts": len(self.call_usage),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }

    def item_records(self) -> list[dict]:
        """One record per selected candidate (with its observation, if any)."""
        by_id = {o.candidate_id: o for o in self.observations}
        records: list[dict] = []
        for candidate in self.selected:
            record = candidate.to_dict()
            for side, modules in (
                (record["sides"][0], candidate.modules_a),
                (record["sides"][1], candidate.modules_b),
            ):
                side["provenance"] = [
                    entry for entry in self.modules if entry.get("module_id") in modules
                ]
            observation = by_id.get(candidate.candidate_id)
            if observation is not None:
                record["observation"] = observation.to_dict()
            elif self.mode == CROSS_COORDINATION_OBSERVE:
                record["observation"] = None
            records.append(record)
        return records


def module_input_from_result(result: Any, *, display_name: str = "") -> ModuleInput:
    """A :class:`ModuleInput` from a ``PipelineResult`` (or a collected state).

    Reads the specifications (``extracted_specs``, else the submission's
    ``prepared_specs``), the files whose review failed, the cross-check plan,
    and the module's provenance (cycle label, governing-basis fingerprint).
    """
    specs = getattr(result, "extracted_specs", None)
    submission = getattr(result, "submission", None)
    if not specs and submission is not None:
        specs = getattr(submission, "prepared_specs", None)
    failed = getattr(result, "failed_review_specs", None)
    if failed is None:
        failed = getattr(result, "truncated_specs", None)
    module_id = str(
        getattr(result, "module_id", "")
        or getattr(submission, "module_id", "")
        or ""
    )
    cycle_label = str(
        getattr(result, "cycle_label", "") or getattr(submission, "cycle_label", "") or ""
    )
    basis = getattr(result, "governing_basis", None)
    if basis is None and submission is not None:
        basis = getattr(submission, "governing_basis", None)
    cross = getattr(result, "cross_check_result", None)
    return ModuleInput(
        module_id=module_id,
        display_name=display_name or module_id,
        specs=tuple(specs or ()),
        failed_files=tuple(failed or ()),
        chunk_groups=chunk_groups_from(cross),
        cross_check_status=str(getattr(cross, "cross_check_status", "") or ""),
        cycle_label=cycle_label,
        basis_fingerprint=_basis_fingerprint(basis),
    )


def _basis_fingerprint(basis: Any) -> str:
    """The governing basis's own fingerprint, or a note when it has none."""
    if not basis:
        return ""
    try:
        from ..verification.governing_context import basis_from_dict

        return basis_from_dict(basis).fingerprint() if isinstance(basis, dict) else basis.fingerprint()
    except Exception as exc:  # noqa: BLE001 — provenance only, never fatal
        return f"unreadable ({type(exc).__name__})"


def _default_client():
    from ..review.reviewer import _get_client

    return _get_client(sdk_retries=False)


def run_coordination(
    inputs: Sequence[ModuleInput],
    *,
    mode: str,
    scope: str = CROSS_COORDINATION_SCOPE_MODULE,
    log: LogFn = _noop_log,
    call_gate=None,
    client: Any = None,
    model: str = COORDINATION_MODEL_DEFAULT,
    max_candidates: int = MAX_CANDIDATES,
    max_per_file_pair: int = MAX_PER_FILE_PAIR,
    per_request: int = CANDIDATES_PER_REQUEST,
    count_client_factory: Callable[[], Any] | None = None,
    unavailable: Sequence[str] = (),
) -> CoordinationResult:
    """Run the pass over one or more modules' collected runs. Never raises.

    Deterministic up to the model call: the same inputs select the same
    candidates in the same order, and requests go out in that order.
    ``unavailable`` names what the caller could not give the pass (a routed
    module whose collection failed); each note is listed as not assessed.
    """
    started = time.time()
    result = CoordinationResult(status=STATUS_SKIPPED, mode=mode, scope=scope, model=model)
    result.unassessed.extend(str(note) for note in unavailable)
    try:
        _run(
            result, inputs, mode=mode, scope=scope, log=log, call_gate=call_gate,
            client=client, model=model, max_candidates=max_candidates,
            max_per_file_pair=max_per_file_pair, per_request=per_request,
            count_client_factory=count_client_factory,
        )
    except Exception as exc:  # noqa: BLE001 — an experiment never fails a run
        result.status = STATUS_FAILED
        result.reason = f"The coordination pass stopped: {type(exc).__name__}: {exc}"
        log(f"Coordination (experiment) stopped: {exc}", level="warning")
    result.elapsed_seconds = time.time() - started
    attempts = [a for outcome in result.requests for a in outcome.attempts]
    result.call_usage = attempt_dicts(attempts)
    totals = known_totals(attempts)
    for key, value in totals.items():
        if hasattr(result, key):
            setattr(result, key, value)
    return result


def _run(
    result: CoordinationResult,
    inputs: Sequence[ModuleInput],
    *,
    mode: str,
    scope: str,
    log: LogFn,
    call_gate,
    client,
    model: str,
    max_candidates: int,
    max_per_file_pair: int,
    per_request: int,
    count_client_factory,
) -> None:
    if mode not in (CROSS_COORDINATION_CANDIDATES, CROSS_COORDINATION_OBSERVE):
        result.reason = f"Unknown coordination mode {mode!r}."
        return
    units: list[SpecUnit] = []
    specs_by_file: dict[str, Any] = {}
    for item in inputs:
        failed = set(item.failed_files)
        files = []
        for spec in item.specs:
            name = str(getattr(spec, "filename", "") or "")
            if not name:
                continue
            if name in failed:
                continue
            files.append(name)
            specs_by_file.setdefault(name, spec)
        for name in sorted(failed):
            result.unassessed.append(
                f"{item.display_name}: {name} — its review failed, so it was not read"
            )
        units.append(SpecUnit(
            module_id=item.module_id,
            files=tuple(dict.fromkeys(files)),
            chunk_groups=item.chunk_groups,
            display_name=item.display_name,
        ))
        result.modules.append({
            "module_id": item.module_id,
            "display_name": item.display_name,
            "cycle_label": item.cycle_label,
            "governing_basis_fingerprint": item.basis_fingerprint,
            "cross_check_status": item.cross_check_status,
            "cross_check_requests": (
                len(item.chunk_groups) if item.chunk_groups is not None else None
            ),
            "files": len(files),
        })
    if not specs_by_file:
        result.reason = "No specification text is available to read."
        return

    facts = []
    for name, spec in specs_by_file.items():
        extraction = extract_facts(spec)
        result.files_read += 1
        result.unattributed_values += extraction.unattributed_values
        if not getattr(spec, "paragraph_map", None):
            result.unassessed.append(f"{name} — no paragraph map, so it was not read")
        if extraction.facts_over_limit:
            result.unassessed.append(
                f"{name} — {extraction.facts_over_limit} fact(s) over the per-specification limit were not read"
            )
        facts.extend(extraction.facts)
    result.facts = len(facts)

    selection = select_candidates(
        facts, units, scope=scope,
        max_candidates=max_candidates, max_per_file_pair=max_per_file_pair,
    )
    result.selection = selection
    result.unassessed.extend(selection.unassessed)
    for candidate, why in selection.deferred:
        result.unassessed.append(
            f"{candidate.candidate_id} ({candidate.side_a.file_name} / "
            f"{candidate.side_b.file_name}) — {why}"
        )
    log(
        f"Coordination (experiment, {mode}, {scope} scope): {result.facts} fact(s) in "
        f"{result.files_read} specification(s); {selection.joined} candidate pair(s), "
        f"{len(selection.selected)} selected.",
        level="info",
    )
    if mode == CROSS_COORDINATION_CANDIDATES or not selection.selected:
        result.status = STATUS_COMPLETED
        return

    if client is None:
        client = _default_client()
    if count_client_factory is None:
        # The client the pass sends with also sizes its requests (the
        # production client is the no-retry view, as for cross-check counts).
        count_client_factory = lambda: client  # noqa: E731
    module_names = {item.module_id: item.display_name for item in inputs}
    for batch in batches(selection.selected, per_request):
        outcome = run_request(
            batch,
            client=client,
            model=model,
            module_names=module_names,
            call_gate=call_gate,
            count_client_factory=count_client_factory,
        )
        result.requests.append(outcome)
        result.observations.extend(outcome.observations)
        result.ignored_entries += outcome.ignored_entries
        if outcome.status != "completed":
            for cid in outcome.candidate_ids:
                result.unassessed.append(f"{cid} — request {outcome.status}: {outcome.error}")
            continue
        for cid in outcome.candidate_ids:
            if cid not in outcome.returned_ids:
                result.unassessed.append(f"{cid} — the response gave no observation for it")
    if any(r.status == "completed" for r in result.requests):
        result.status = STATUS_COMPLETED
    else:
        result.status = STATUS_FAILED
        result.reason = "; ".join(r.error for r in result.requests if r.error) or (
            "Every coordination request failed."
        )
    tally = result.counts()
    log(
        f"Coordination (experiment): {tally[ASSESSMENT_CONFLICT]} conflict(s), "
        f"{tally[ASSESSMENT_NOT_CONFLICT]} not a conflict, "
        f"{tally[ASSESSMENT_CANNOT_TELL]} cannot tell; {len(result.unassessed)} "
        "area(s) not assessed. Observation only: nothing was added to the report.",
        level="info" if result.status == STATUS_COMPLETED else "warning",
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

PHASE = "coordination"


def record_coordination(diag, result: CoordinationResult | None) -> None:
    """Record the pass: one summary event, then one event per item (bounded).

    A pass that spent is recorded through ``record_pass_api_call`` so its
    attempts are priced once; a pass that did not (candidates mode, or no
    candidate) is an ordinary event and adds nothing to the estimate.
    """
    if not diag or result is None:
        return
    from ..orchestration.diagnostics import record_pass_api_call

    summary = result.to_dict()
    items = result.item_records()
    summary["items_recorded"] = min(len(items), MAX_RECORDED_ITEMS)
    summary["items_not_recorded"] = max(0, len(items) - MAX_RECORDED_ITEMS)
    message = (
        f"Coordination (experiment, {result.mode}): {result.status}, "
        f"{summary['selected']} candidate(s)"
    )
    level = "info" if result.status != STATUS_FAILED else "warning"
    if result.call_usage:
        record_pass_api_call(
            diag,
            result,
            phase=PHASE,
            operation="coordination",
            message=message,
            level=level,
            extra={"coordination": summary},
        )
    else:
        diag.log(PHASE, level, message, {"coordination": summary})
    for record in items[:MAX_RECORDED_ITEMS]:
        diag.log(PHASE, "info", f"Coordination item {record['candidate_id']}", {
            "coordination_item": record,
        })


def summarize_coordination(records: Iterable[dict], items: Iterable[dict]) -> dict:
    """The ``coordination`` rollup of a diagnostics summary."""
    records = [r for r in records if isinstance(r, dict)]
    items = [i for i in items if isinstance(i, dict)]
    tally = {ASSESSMENT_CONFLICT: 0, ASSESSMENT_NOT_CONFLICT: 0, ASSESSMENT_CANNOT_TELL: 0}
    for record in records:
        for key, value in (record.get("assessments") or {}).items():
            if isinstance(value, int):
                tally[key] = tally.get(key, 0) + value
    return {
        "passes": len(records),
        "modes": sorted({str(r.get("mode") or "") for r in records}),
        "scopes": sorted({str(r.get("scope") or "") for r in records}),
        "selected": sum(int(r.get("selected") or 0) for r in records),
        "deferred": sum(int(r.get("deferred") or 0) for r in records),
        "assessments": tally,
        "unassessed": sum(int(r.get("unassessed_count") or 0) for r in records),
        "complete": all(bool(r.get("complete")) for r in records) if records else False,
        "records": records,
        "items": items[:200],
        "items_not_listed": max(0, len(items) - 200),
    }


def summary_line(rollup: dict | None) -> str:
    """One line for ``DiagnosticsReport.to_text``; ``""`` when the pass never ran."""
    if not rollup:
        return ""
    tally = rollup.get("assessments") or {}
    return (
        f"Coordination (experiment, observation only): {rollup.get('selected', 0)} "
        f"candidate(s), {tally.get(ASSESSMENT_CONFLICT, 0)} conflict(s), "
        f"{tally.get(ASSESSMENT_NOT_CONFLICT, 0)} not a conflict, "
        f"{tally.get(ASSESSMENT_CANNOT_TELL, 0)} cannot tell, "
        f"{rollup.get('unassessed', 0)} area(s) not assessed"
    )


__all__ = [
    "CoordinationResult",
    "MAX_RECORDED_ITEMS",
    "ModuleInput",
    "PHASE",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "module_input_from_result",
    "record_coordination",
    "run_coordination",
    "summarize_coordination",
    "summary_line",
]
