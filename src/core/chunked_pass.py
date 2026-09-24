"""Shared engine for the chunked package-level passes (cross-check, compliance).

Both passes read the whole specification package in one context. When the
package exceeds the recommended input ceiling they fall back to one call per
CSI chunk group and merge the chunk results into a single
:class:`~src.review.reviewer.ReviewResult`. Everything about that fallback
that is *not* the API call itself lives here, once:

- grouping specs into the module's chunk groups (:func:`group_specs_by_chunk`)
  with singleton pooling into the reserved ``general`` bucket, so every spec
  lands in exactly one chunk and the union of chunk specs equals the input;
- scoping the already-identified findings to each chunk
  (:func:`filter_findings_for_chunk`);
- running the pass-specific un-chunked callable once per chunk, in order;
- labelling each finding with its chunk (:func:`label_finding_with_chunk`);
- tallying completed / failed / skipped chunks and synthesizing the combined
  status, summary header, per-chunk summaries, ``error`` text, and the
  ``chunk_failures`` / ``chunk_skips`` telemetry
  (:func:`synthesize_chunk_results`, :func:`run_chunked_pass`).

The two passes used to be separate near-identical pipelines, and they
drifted: the cross-check merge once forgot to carry the chunk errors onto the
combined result while the compliance merge did. One engine, parameterized by
the pass-specific pieces — the runner callable, the pass name for messages,
an optional per-chunk summary heading, and the compliance pass's
``finalize`` hook, which sees every planned chunk's outcome (plan WP-09) —
is what keeps them from drifting again.

The engine is deliberately unaware of tracing, logging, and how a request is
built or counted: those differ per pass, so the thin adapters in
``cross_check.cross_checker`` and ``compliance.compliance_checker`` own them.
What it does own is the *shape* of the fit decision (plan WP-08):
:func:`plan_chunks` groups the specs, asks the adapter's ``measure`` callable
for the :class:`~src.core.request_budget.RequestBudget` of each group's real
request, and splits a group that does not fit into contiguous parts, each of
whose own request was measured and fits. A specification that cannot fit even
alone (with the context every request carries) becomes an explicit
*not analyzed* entry: it never reaches the runner, it is never truncated, and
it is counted and named in the combined result. It never touches the
per-call permit gate either — a runner (and a ``measure`` that calls the
count API) acquires one permit around each API call and the engine adds no
outer hold, so a chunked pass takes exactly one permit per call, never one
for the whole pass.

Status rule (shared): ``completed`` when at least one chunk completed;
otherwise ``failed`` when at least one chunk failed (anything that is neither
``completed`` nor ``skipped`` counts as failed); otherwise ``skipped`` — a
pass with no chunks at all therefore reports ``skipped``, since nothing ran.
A ``failed`` combined result carries the joined per-chunk errors (fallback
``"All <pass> chunks failed."``); a partial completion stays ``completed``
with ``error=None`` and relies on the tally + telemetry to surface the
chunks that did not run.

This module lives in ``core`` so both passes can depend on it without a
cycle; its first-party imports are the shared result type and the request
budget it plans with.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence

from .api_config import merge_cache_usage
from .request_budget import RequestBudget, oversize_reason
from ..review.reviewer import Finding, ReviewResult

if TYPE_CHECKING:  # pragma: no cover — annotations only, keeps ``core`` light
    from ..input.extractor import ExtractedSpec
    from ..modules.base import ChunkGroup


# Reserved pool for specs whose CSI prefix matches no chunk group and for the
# singletons pooled out of one-spec chunks. Module authors cannot claim it
# (``ChunkGroup`` registration rejects the id); the engine owns its label.
GENERAL_CHUNK_ID = "general"
GENERAL_CHUNK_LABEL = "Project-wide / Other"

# Leading CSI division number of a spec filename ("23 05 00 - HVAC.docx").
_CSI_PREFIX_RE = re.compile(r"^\s*(\d{2})\s?(\d{2})?")


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def csi_prefix(filename: str) -> str:
    """The two-digit CSI division at the start of ``filename``, or ``""``."""
    match = _CSI_PREFIX_RE.match(filename)
    if not match:
        return ""
    return match.group(1) or ""


def assign_chunk(filename: str, groups: Sequence[ChunkGroup]) -> str:
    """Chunk id for one spec: the first group claiming its CSI prefix.

    An unparseable or unclaimed prefix routes to ``general`` — never dropped.
    """
    prefix = csi_prefix(filename)
    if prefix:
        for group in groups:
            if prefix in group.csi_prefixes:
                return group.chunk_id
    return GENERAL_CHUNK_ID


def chunk_label(chunk_id: str, groups: Sequence[ChunkGroup]) -> str:
    """Human-readable label for a chunk id (``general`` / unknown → the pool label)."""
    for group in groups:
        if group.chunk_id == chunk_id:
            return group.label
    return GENERAL_CHUNK_LABEL


def group_specs_by_chunk(
    specs: Iterable[ExtractedSpec], groups: Sequence[ChunkGroup]
) -> list[tuple[str, list[ExtractedSpec]]]:
    """Group specs by chunk, preserving order, with singleton pooling.

    Returns ``(chunk_id, specs)`` pairs. A chunk with a single spec has
    nothing to coordinate against, so singletons are pooled into ``general``
    (which may itself end up holding one spec — the pool is the one bucket
    that is never pooled further). Stable order: the predefined groups in
    declaration order, then ``general`` last. Every input spec appears in
    exactly one chunk.
    """
    buckets: dict[str, list[ExtractedSpec]] = {}
    for spec in specs:
        buckets.setdefault(assign_chunk(spec.filename, groups), []).append(spec)

    merged: dict[str, list[ExtractedSpec]] = {}
    project_wide: list[ExtractedSpec] = []
    for chunk_id, group in buckets.items():
        if len(group) >= 2:
            merged[chunk_id] = group
        else:
            project_wide.extend(group)
    if project_wide:
        merged.setdefault(GENERAL_CHUNK_ID, []).extend(project_wide)

    ordered: list[tuple[str, list[ExtractedSpec]]] = []
    for group in groups:
        if group.chunk_id in merged:
            ordered.append((group.chunk_id, merged[group.chunk_id]))
    if GENERAL_CHUNK_ID in merged:
        ordered.append((GENERAL_CHUNK_ID, merged[GENERAL_CHUNK_ID]))
    # Defensive: a chunk id the groups no longer declare keeps insertion order.
    placed = {chunk_id for chunk_id, _ in ordered}
    for chunk_id, group in merged.items():
        if chunk_id not in placed:
            ordered.append((chunk_id, group))
    return ordered


# ---------------------------------------------------------------------------
# Token-aware planning (plan WP-08)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedChunk:
    """One chunk a pass will run, or report as not analyzed.

    ``group_id`` is the CSI chunk group the specs came from and decides how
    their findings are labelled; ``chunk_id`` equals it unless the group was
    split, when parts are ``"<group_id>:<n>"`` and ``label`` says
    ``"(part n of N)"``. ``budget`` is the measured budget of the chunk's own
    request (``None`` for a chunk too small to measure — a lone spec in a
    pass that needs two). ``unanalyzed_reason`` is set when the chunk cannot
    be sent at all; such a chunk never reaches the runner.
    """

    chunk_id: str
    group_id: str
    label: str
    specs: list
    budget: RequestBudget | None = None
    unanalyzed_reason: str | None = None
    group_label: str = ""

    @property
    def runnable(self) -> bool:
        return self.unanalyzed_reason is None


Measure = Callable[[list], RequestBudget]


def _names(specs: Sequence) -> str:
    return ", ".join(getattr(spec, "filename", str(spec)) for spec in specs)


def _largest_fitting_prefix(
    specs: list, start: int, *, measure: Measure, remainder: RequestBudget
) -> tuple[int, RequestBudget]:
    """Largest ``k`` with ``measure(specs[start:start + k]).fits``, by bisection.

    ``remainder`` is the already-measured budget of ``specs[start:]``. Only a
    ``k`` whose own request was measured as fitting is ever returned, so the
    answer is safe even if the counts are not monotone; monotone counts (more
    specs, more tokens) make it the largest such prefix. Returns ``(k,
    budget)``: the fitting prefix's budget when ``k >= 1``, otherwise the
    budget of the smallest measured prefix that does not fit (one spec), or a
    budget whose ``count`` is ``None`` when a measurement could not be sized.
    Makes at most ``ceil(log2(n)) + 1`` measurements.
    """
    n = len(specs) - start
    if remainder.fits:
        return n, remainder
    if remainder.count is None:
        return 0, remainder
    lo, lo_budget = 0, None
    hi, hi_budget = n, remainder
    while hi - lo > 1:
        mid = (lo + hi) // 2
        budget = measure(specs[start:start + mid])
        if budget.count is None:
            return 0, budget
        if budget.fits:
            lo, lo_budget = mid, budget
        else:
            hi, hi_budget = mid, budget
    if lo == 0:
        return 0, hi_budget
    return lo, lo_budget


# One planned part of a group: its specs, their measured budget, and the
# reason it cannot be sent (``None`` when it can).
Part = tuple[list, RequestBudget | None, str | None]


def _borrow_from_previous(
    parts: list[Part], run: list, *, measure: Measure, min_specs: int
) -> tuple[Part, Part] | None:
    """Lend a short ``run`` the last specs of the part before it, if both still fit.

    ``run`` fits on its own but has fewer than ``min_specs`` specs — a lone
    spec after a full part, most often the last spec of a group, which the
    largest-fitting-prefix rule would otherwise report as not analyzed (four
    specs of which any three fit became 3 + 1 instead of 2 + 2). Borrowing
    the fewest specs that make ``run`` long enough is the one move worth
    trying: ``run`` did not fit together with the spec after it (or has
    none), and borrowing more would only make the joined part larger. Both
    new parts are measured, and the move is taken
    only when both fit and the lending part keeps ``min_specs`` specs, so a
    spec the plan already covered is never given up. Returns the two
    replacement parts, or ``None`` to keep the plan as it is.
    """
    if not parts:
        return None
    previous, _budget, previous_reason = parts[-1]
    need = min_specs - len(run)
    if previous_reason is not None or need <= 0 or len(previous) - need < min_specs:
        return None
    kept, lent = previous[:-need], previous[-need:]
    joined_budget = measure(lent + run)
    if not joined_budget.fits:
        return None
    kept_budget = measure(kept)
    if not kept_budget.fits:
        return None
    return (kept, kept_budget, None), (lent + run, joined_budget, None)


def _split_group(
    specs: list, *, measure: Measure, min_specs: int, pass_name: str
) -> list[Part]:
    """``(specs, budget, unanalyzed_reason)`` parts covering ``specs`` in order."""
    if len(specs) < min_specs:
        # Too few specs for the pass to do anything with (a lone spec in the
        # pooled bucket of a pass that needs two). Nothing to size; the
        # runner reports it exactly as before.
        return [(specs, None, None)]
    whole = measure(specs)
    if whole.fits:
        return [(specs, whole, None)]
    parts: list[Part] = []
    start = 0
    remainder = whole
    while start < len(specs):
        if start > 0:
            remainder = measure(specs[start:])
        size, budget = _largest_fitting_prefix(
            specs, start, measure=measure, remainder=remainder
        )
        if budget.count is None:
            # Nothing past this point can be sized: report the rest together
            # rather than guess at a split.
            rest = specs[start:]
            parts.append((
                rest,
                budget,
                oversize_reason(
                    budget, what=f"{pass_name} request for {_names(rest)}"
                ),
            ))
            break
        if size >= max(1, min_specs):
            parts.append((specs[start:start + size], budget, None))
            start += size
            continue
        if size >= 1:
            borrowed = _borrow_from_previous(
                parts, specs[start:start + size], measure=measure, min_specs=min_specs
            )
            if borrowed is not None:
                parts[-1:] = list(borrowed)
                start += size
                continue
        lone = specs[start:start + 1]
        if size >= 1:
            # Fits alone, but neither with the next spec nor with any the part
            # before it could lend, and the pass needs at least ``min_specs``
            # specs in one request.
            reason = (
                f"{_names(lone)} fits in a {pass_name} request on its own "
                f"({budget.size_text()}, input ceiling {budget.input_ceiling:,}) "
                f"but could not be paired with a neighboring specification, and "
                f"a {pass_name} request needs at least {min_specs} "
                "specifications, so it was not compared with any other "
                "specification. Nothing was truncated."
            )
        else:
            reason = oversize_reason(
                budget,
                what=f"{pass_name} request for {_names(lone)} with its required context",
            )
        parts.append((lone, budget, reason))
        start += 1
    return parts


def plan_chunks(
    specs: Iterable[ExtractedSpec],
    groups: Sequence[ChunkGroup],
    *,
    measure: Measure,
    min_specs: int = 1,
    pass_name: str = "pass",
) -> list[PlannedChunk]:
    """Group ``specs`` and make every final chunk fit, in a stable order.

    Starts from :func:`group_specs_by_chunk` (every spec in exactly one
    group, singletons pooled). A group whose own request ``measure`` says
    fits stays one chunk with its group id — so a package that only needed
    CSI chunking plans exactly as before. A group that does not fit is split
    into contiguous parts, each the largest prefix of the remaining specs
    whose measured request fits (:func:`_largest_fitting_prefix`), numbered
    ``"<group_id>:1"`` onward. A run too short for the pass borrows the last
    specs of the part before it when both still fit
    (:func:`_borrow_from_previous`). A spec that cannot fit alone — or, when
    ``min_specs`` is 2, cannot be paired with a neighbor — becomes a
    not-analyzed part naming the reason. Order, spec ownership (each spec in
    exactly one chunk), and ids are deterministic for the same specs and
    counts.
    """
    planned: list[PlannedChunk] = []
    for group_id, group_specs in group_specs_by_chunk(specs, groups):
        label = chunk_label(group_id, groups)
        parts = _split_group(
            list(group_specs), measure=measure, min_specs=min_specs, pass_name=pass_name
        )
        if len(parts) == 1:
            part_specs, budget, reason = parts[0]
            planned.append(PlannedChunk(
                chunk_id=group_id, group_id=group_id, label=label,
                specs=part_specs, budget=budget, unanalyzed_reason=reason,
                group_label=label,
            ))
            continue
        total = len(parts)
        for index, (part_specs, budget, reason) in enumerate(parts, start=1):
            planned.append(PlannedChunk(
                chunk_id=f"{group_id}:{index}",
                group_id=group_id,
                label=f"{label} (part {index} of {total})",
                specs=part_specs,
                budget=budget,
                unanalyzed_reason=reason,
                group_label=label,
            ))
    return planned


def split_groups(plan: Sequence[PlannedChunk]) -> list[str]:
    """Labels of the CSI groups the plan had to split into parts, in order."""
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for entry in plan:
        counts[entry.group_id] = counts.get(entry.group_id, 0) + 1
        labels.setdefault(entry.group_id, entry.group_label or entry.group_id)
    return [labels[group_id] for group_id, count in counts.items() if count > 1]


def unanalyzed_specs(plan: Sequence[PlannedChunk]) -> list[str]:
    """File names of the specs the plan could not send, in order."""
    return [
        getattr(spec, "filename", str(spec))
        for entry in plan
        if not entry.runnable
        for spec in entry.specs
    ]


# ---------------------------------------------------------------------------
# Per-chunk scoping and labelling
# ---------------------------------------------------------------------------


def filter_findings_for_chunk(
    existing_findings: list[Finding], chunk_filenames: set[str]
) -> list[Finding]:
    """Restrict the already-identified context to findings inside a chunk.

    Per-spec review findings are noise to a chunk that does not contain
    their source file; a chunk sees only the findings that originate in
    (or affect) its own files. An empty filename set means "no scoping".
    """
    if not chunk_filenames:
        return list(existing_findings)
    return [
        f for f in existing_findings
        if f.fileName in chunk_filenames
        or any(name in chunk_filenames for name in f.affected_files)
    ]


def label_finding_with_chunk(
    finding: Finding, chunk_id: str, groups: Sequence[ChunkGroup]
) -> Finding:
    """Prefix ``finding.section`` with the chunk's label (idempotent, in place)."""
    label = chunk_label(chunk_id, groups)
    if not label:
        return finding
    section = finding.section or ""
    if label.lower() in section.lower():
        return finding
    finding.section = f"[{label}] {section}".strip().rstrip(":")
    return finding


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkJob:
    """One chunk's inputs, handed to the pass-specific runner."""

    chunk_id: str
    label: str
    specs: list[ExtractedSpec]
    existing_findings: list[Finding]


ChunkRunner = Callable[[ChunkJob], ReviewResult]


@dataclass(frozen=True)
class ChunkOutcome:
    """One planned chunk as it ended: its specifications and its result.

    Every planned chunk has one, whether it completed, failed, was skipped by
    its runner, or could not be sent at all (then ``result`` is the
    synthesized ``skipped`` result carrying the plan's reason). A pass's
    ``finalize`` hook receives all of them, so a conclusion about the whole
    package — compliance's "this requirement is missing everywhere" — can
    account for the chunks that produced nothing (plan WP-09).
    """

    chunk_id: str
    label: str
    filenames: tuple[str, ...]
    result: ReviewResult

    @property
    def completed(self) -> bool:
        return self.result.cross_check_status == "completed"


Finalize = Callable[[ReviewResult, Sequence[ChunkOutcome]], None]


@dataclass(frozen=True)
class ChunkSynthesis:
    """The merged, pass-neutral view of a list of chunk results."""

    findings: list[Finding]
    summary_text: str
    status: str
    completed: int
    failed: int
    skipped: int


def _is_failed(result: ReviewResult) -> bool:
    return result.cross_check_status not in ("completed", "skipped")


def synthesize_chunk_results(
    chunk_results: Sequence[tuple[str, ReviewResult]],
    *,
    groups: Sequence[ChunkGroup],
    summary_title: str,
    summary_heading: Callable[[str], str] | None = None,
    chunk_labels: Mapping[str, str] | None = None,
    chunk_groups: Mapping[str, str] | None = None,
    chunk_files: Mapping[str, Sequence[str]] | None = None,
    scope_note: str | None = None,
) -> ChunkSynthesis:
    """Tally chunk statuses and merge findings + per-chunk summaries.

    Every completed chunk's findings survive (labelled with their own chunk),
    so a failed chunk never drops another chunk's output. The summary text is
    a ``"<summary_title> (N completed, N failed, N skipped). Per-chunk
    summaries follow."`` header, then ``scope_note`` when given, then one
    ``--- <heading> ---`` section per chunk. ``summary_heading`` maps a chunk
    id to that heading; without it the heading is ``chunk_labels[chunk_id]``
    when given, else the chunk's group label. ``chunk_groups`` maps a chunk id
    to the CSI group whose label its findings carry (a part of a split group
    keeps its group's label; default: the chunk id itself). ``chunk_files``,
    when given, adds a ``Not analyzed: <files>`` line to every failed or
    skipped chunk's section, so the summary names exactly which
    specifications the pass did not cover.
    """
    labels = chunk_labels or {}
    group_of = chunk_groups or {}
    heading_for = (
        summary_heading
        if summary_heading is not None
        else (lambda chunk_id: labels.get(chunk_id) or chunk_label(chunk_id, groups))
    )

    def not_analyzed(chunk_id: str) -> str:
        if chunk_files is None:
            return ""
        names = list(chunk_files.get(chunk_id) or ())
        return f"\nNot analyzed: {', '.join(names)}" if names else ""

    findings: list[Finding] = []
    summaries: list[str] = []
    completed = failed = skipped = 0

    for chunk_id, result in chunk_results:
        heading = heading_for(chunk_id)
        if result.cross_check_status == "completed":
            completed += 1
            group_id = group_of.get(chunk_id, chunk_id)
            for finding in result.findings:
                findings.append(label_finding_with_chunk(finding, group_id, groups))
            if result.thinking:
                summaries.append(f"--- {heading} ---\n{result.thinking.strip()}")
        elif result.cross_check_status == "skipped":
            skipped += 1
            summaries.append(
                f"--- {heading} ---\nSkipped: {result.thinking or 'no reason given'}"
                + not_analyzed(chunk_id)
            )
        else:
            failed += 1
            summaries.append(
                f"--- {heading} ---\nFailed: {result.error or 'unknown error'}"
                + not_analyzed(chunk_id)
            )

    if completed:
        status = "completed"
    elif failed:
        status = "failed"
    else:
        status = "skipped"

    header = (
        f"{summary_title} ({completed} completed, {failed} failed, "
        f"{skipped} skipped). Per-chunk summaries follow.\n"
    )
    if scope_note:
        header += f"{scope_note.strip()}\n\n"
    summary_text = header + "\n\n".join(summaries) if summaries else header
    return ChunkSynthesis(
        findings=findings,
        summary_text=summary_text,
        status=status,
        completed=completed,
        failed=failed,
        skipped=skipped,
    )


def _as_planned(chunk, groups: Sequence[ChunkGroup]) -> PlannedChunk:
    """Accept a :class:`PlannedChunk` or a legacy ``(chunk_id, specs)`` pair."""
    if isinstance(chunk, PlannedChunk):
        return chunk
    chunk_id, chunk_specs = chunk
    label = chunk_label(chunk_id, groups)
    return PlannedChunk(
        chunk_id=chunk_id, group_id=chunk_id, label=label,
        specs=chunk_specs, group_label=label,
    )


def run_chunked_pass(
    chunks: Sequence[PlannedChunk | tuple[str, list[ExtractedSpec]]],
    existing_findings: list[Finding],
    *,
    groups: Sequence[ChunkGroup],
    run_chunk: ChunkRunner,
    pass_name: str,
    summary_title: str,
    model: str,
    summary_heading: Callable[[str], str] | None = None,
    finalize: Finalize | None = None,
    scope_note: str | None = None,
) -> ReviewResult:
    """Run ``run_chunk`` once per runnable chunk, in order, and merge the results.

    ``chunks`` is the output of :func:`plan_chunks` (or, for a caller that
    does its own sizing, of :func:`group_specs_by_chunk`); each runner call
    receives a :class:`ChunkJob` whose ``existing_findings`` are already
    scoped to that chunk's files. A planned chunk that cannot be sent
    (``unanalyzed_reason``) never reaches the runner: it becomes a
    ``skipped`` chunk carrying the reason, so it is counted in
    ``chunk_skips`` and named in the summary. Runners follow the un-chunked
    passes' contract — failures land in the returned result, never raise —
    and are the only place a per-call permit is taken.

    ``pass_name`` names the pass in the all-chunks-failed fallback error
    (``"All <pass_name> chunks failed."``); ``summary_title`` heads the
    combined summary. ``finalize`` (compliance) receives the combined result
    — the completed chunks' labelled findings, empty ``coverage`` — and one
    :class:`ChunkOutcome` per planned chunk, failed and not-analyzed chunks
    included, and may replace ``findings`` / ``coverage`` and set the pass's
    own fields (``coverage_completeness``). The execution status, the chunk
    tally, and the token counters stay the engine's: a hook decides what the
    output *means*, never whether a chunk ran. Without it, findings pass
    through unfiltered and ``coverage`` stays empty.

    Token counters (input / output / cache creation / cache read) are summed
    over every chunk result and ``elapsed_seconds`` spans the whole loop, so
    the combined result carries the full cost of the pass.
    """
    started = time.time()
    plan = [_as_planned(chunk, groups) for chunk in chunks]
    chunk_results: list[tuple[str, ReviewResult]] = []
    for entry in plan:
        if not entry.runnable:
            chunk_results.append((
                entry.chunk_id,
                ReviewResult(
                    findings=[],
                    thinking=entry.unanalyzed_reason,
                    model=model,
                    cross_check_status="skipped",
                ),
            ))
            continue
        job = ChunkJob(
            chunk_id=entry.chunk_id,
            label=entry.label,
            specs=entry.specs,
            existing_findings=filter_findings_for_chunk(
                existing_findings, {spec.filename for spec in entry.specs}
            ),
        )
        chunk_results.append((entry.chunk_id, run_chunk(job)))

    synthesis = synthesize_chunk_results(
        chunk_results,
        groups=groups,
        summary_title=summary_title,
        summary_heading=summary_heading,
        chunk_labels={entry.chunk_id: entry.label for entry in plan},
        chunk_groups={entry.chunk_id: entry.group_id for entry in plan},
        chunk_files={
            entry.chunk_id: [spec.filename for spec in entry.specs] for entry in plan
        },
        scope_note=scope_note,
    )
    combined = ReviewResult(
        findings=synthesis.findings,
        thinking=synthesis.summary_text,
        model=model,
        input_tokens=sum(r.input_tokens for _cid, r in chunk_results),
        output_tokens=sum(r.output_tokens for _cid, r in chunk_results),
        # Merged rather than summed key-wise: the merge keeps the per-TTL
        # accounting invariant and the sticky ``inconsistent`` warning, so a
        # chunk whose provider detail was untrustworthy stays visible in the
        # combined result instead of being averaged away.
        **merge_cache_usage(*(r for _cid, r in chunk_results)),
        elapsed_seconds=time.time() - started,
        cross_check_status=synthesis.status,
        chunk_failures=synthesis.failed,
        chunk_skips=synthesis.skipped,
    )
    if synthesis.status == "failed":
        # Zero chunks completed: carry WHY on the combined result so the
        # operator log reads "<Pass> failed: <errors>", never "... : None".
        combined.error = "; ".join(
            filter(None, (r.error for _cid, r in chunk_results if _is_failed(r)))
        ) or f"All {pass_name} chunks failed."
    if finalize is not None:
        # Last, so the hook sees the finished execution result (error set).
        finalize(
            combined,
            [
                ChunkOutcome(
                    chunk_id=entry.chunk_id,
                    label=entry.label,
                    filenames=tuple(spec.filename for spec in entry.specs),
                    result=result,
                )
                for entry, (_chunk_id, result) in zip(plan, chunk_results)
            ],
        )
    return combined
