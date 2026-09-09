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
an optional per-chunk summary heading, and the compliance pass's coverage
merge and finding filter hooks — is what keeps them from drifting again.

The engine is deliberately unaware of tracing, logging, token preflight, and
the "does the package fit?" decision: those differ per pass in text, level,
and hook function, so the thin adapters in ``cross_check.cross_checker`` and
``compliance.compliance_checker`` own them. It never touches the per-call
permit gate either — a runner acquires one permit around each API call and
the engine adds no outer hold, so a chunked pass takes exactly one permit
per call, never one for the whole pass.

Status rule (shared): ``completed`` when at least one chunk completed;
otherwise ``failed`` when at least one chunk failed (anything that is neither
``completed`` nor ``skipped`` counts as failed); otherwise ``skipped`` — a
pass with no chunks at all therefore reports ``skipped``, since nothing ran.
A ``failed`` combined result carries the joined per-chunk errors (fallback
``"All <pass> chunks failed."``); a partial completion stays ``completed``
with ``error=None`` and relies on the tally + telemetry to surface the
chunks that did not run.

This module lives in ``core`` so both passes can depend on it without a
cycle; its only first-party import is the shared result type.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Sequence

from .api_config import merge_cache_usage
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
) -> ChunkSynthesis:
    """Tally chunk statuses and merge findings + per-chunk summaries.

    Every completed chunk's findings survive (labelled with their own chunk),
    so a failed chunk never drops another chunk's output. The summary text is
    a ``"<summary_title> (N completed, N failed, N skipped). Per-chunk
    summaries follow."`` header followed by one ``--- <heading> ---`` section
    per chunk; ``summary_heading`` maps a chunk id to that heading (default:
    the chunk's group label).
    """
    heading_for = (
        summary_heading
        if summary_heading is not None
        else (lambda chunk_id: chunk_label(chunk_id, groups))
    )
    findings: list[Finding] = []
    summaries: list[str] = []
    completed = failed = skipped = 0

    for chunk_id, result in chunk_results:
        heading = heading_for(chunk_id)
        if result.cross_check_status == "completed":
            completed += 1
            for finding in result.findings:
                findings.append(label_finding_with_chunk(finding, chunk_id, groups))
            if result.thinking:
                summaries.append(f"--- {heading} ---\n{result.thinking.strip()}")
        elif result.cross_check_status == "skipped":
            skipped += 1
            summaries.append(
                f"--- {heading} ---\nSkipped: {result.thinking or 'no reason given'}"
            )
        else:
            failed += 1
            summaries.append(
                f"--- {heading} ---\nFailed: {result.error or 'unknown error'}"
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
    summary_text = header + "\n\n".join(summaries) if summaries else header
    return ChunkSynthesis(
        findings=findings,
        summary_text=summary_text,
        status=status,
        completed=completed,
        failed=failed,
        skipped=skipped,
    )


def run_chunked_pass(
    chunks: Sequence[tuple[str, list[ExtractedSpec]]],
    existing_findings: list[Finding],
    *,
    groups: Sequence[ChunkGroup],
    run_chunk: ChunkRunner,
    pass_name: str,
    summary_title: str,
    model: str,
    summary_heading: Callable[[str], str] | None = None,
    coverage_merge: Callable[[list[list[dict]]], list[dict]] | None = None,
    finding_filter: Callable[[list[Finding], list[dict]], list[Finding]] | None = None,
) -> ReviewResult:
    """Run ``run_chunk`` once per chunk, in order, and merge the results.

    ``chunks`` is the output of :func:`group_specs_by_chunk`; each runner
    call receives a :class:`ChunkJob` whose ``existing_findings`` are already
    scoped to that chunk's files. Runners follow the un-chunked passes'
    contract — failures land in the returned result, never raise — and are
    the only place a per-call permit is taken.

    ``pass_name`` names the pass in the all-chunks-failed fallback error
    (``"All <pass_name> chunks failed."``); ``summary_title`` heads the
    combined summary. ``coverage_merge`` (compliance) merges the *completed*
    chunks' coverage lists into the combined ``coverage``; ``finding_filter``
    then sees the labelled findings plus that merged coverage and returns the
    findings to keep. Without the hooks, findings pass through unfiltered
    and ``coverage`` stays empty.

    Token counters (input / output / cache creation / cache read) are summed
    over every chunk result and ``elapsed_seconds`` spans the whole loop, so
    the combined result carries the full cost of the pass.
    """
    started = time.time()
    chunk_results: list[tuple[str, ReviewResult]] = []
    for chunk_id, chunk_specs in chunks:
        job = ChunkJob(
            chunk_id=chunk_id,
            label=chunk_label(chunk_id, groups),
            specs=chunk_specs,
            existing_findings=filter_findings_for_chunk(
                existing_findings, {spec.filename for spec in chunk_specs}
            ),
        )
        chunk_results.append((chunk_id, run_chunk(job)))

    synthesis = synthesize_chunk_results(
        chunk_results,
        groups=groups,
        summary_title=summary_title,
        summary_heading=summary_heading,
    )
    findings = synthesis.findings
    coverage: list[dict] = []
    if coverage_merge is not None:
        coverage = coverage_merge(
            [
                result.coverage
                for _chunk_id, result in chunk_results
                if result.cross_check_status == "completed"
            ]
        )
    if finding_filter is not None:
        findings = finding_filter(findings, coverage)

    combined = ReviewResult(
        findings=findings,
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
        coverage=coverage,
    )
    if synthesis.status == "failed":
        # Zero chunks completed: carry WHY on the combined result so the
        # operator log reads "<Pass> failed: <errors>", never "... : None".
        combined.error = "; ".join(
            filter(None, (r.error for _cid, r in chunk_results if _is_failed(r)))
        ) or f"All {pass_name} chunks failed."
    return combined
