"""Core orchestration pipeline for Spec Critic."""

from __future__ import annotations

import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .extractor import extract_text, extract_multiple_specs, ExtractedSpec, SUPPORTED_EXTENSIONS
from .extraction_cache import (
    cache_token_count,
    extract_multiple_specs_cached,
    extraction_cache_stats,
    get_cached_token_count,
)
from .preprocessor import preprocess_spec, detect_inconsistent_file_naming
from .tokenizer import (
    RECOMMENDED_MAX,
    count_tokens,
    count_tokens_via_api,
    exceeds_per_call_limit_for_model,
    local_estimate_safety_factor,
    safe_local_estimate,
)
from .reviewer import review_single_spec, ReviewResult, Finding, MODEL_OPUS_47, StreamCallback
from .review_request_builder import (
    ReviewRequestSpec,
    build_token_count_request,
    estimate_local_request_tokens,
    review_request_cache_key,
)
from .batch import BatchJob, submit_review_batch, retrieve_review_results
from .batch_runtime import DEFAULT_REVIEW_POLL_POLICY, poll_batch_bounded
from .api_config import REVIEW_MODEL_DEFAULT, token_count_preflight_enabled
from .verifier import (
    verify_findings,
    verify_findings_batch,
    start_verification_batch,
    collect_verification_batch_results,
    prepare_findings_for_verification,
    VerificationResult,
)
from .verification_cache import VerificationCache, cache_persist_enabled
from .cross_checker import run_cross_check, run_chunked_cross_check
from .code_cycles import CodeCycle, DEFAULT_CYCLE, AVAILABLE_CYCLES
from .review_modes import DEFAULT_REVIEW_MODE, ReviewMode, coerce_review_mode

# Phase 7.1 (audit Section 11.1): log/progress callbacks accept explicit
# ``level`` and ``phase`` keywords so pipeline code can categorize messages
# (info / success / warning / error / step / muted) and route them to the
# right diagnostics bucket without the GUI keyword-sniffing the message text.
# Older single-arg callers still work — kwargs default cleanly.
LogFn = Callable[..., None]
ProgressFn = Callable[..., None]


def _noop_log(_msg: str, **_kwargs: object) -> None: return


def _phase_tagged_log(log: LogFn, phase: str) -> LogFn:
    """Wrap a log callback so all calls carry an explicit ``phase`` kwarg.

    Used to retag verification log calls when the underlying callback is
    bucketed by phase (e.g., the GUI diagnostics writer). ``phase`` is set
    via ``setdefault`` so callers can still override per-call.
    """
    def _log(msg: str, **kwargs: object) -> None:
        kwargs.setdefault("phase", phase)
        log(msg, **kwargs)
    return _log


def _phase_tagged_progress(progress: ProgressFn, phase: str) -> ProgressFn:
    def _on_progress(pct: float, msg: str, **kwargs: object) -> None:
        kwargs.setdefault("phase", phase)
        progress(pct, msg, **kwargs)
    return _on_progress

def _noop_progress(_: float, __: str, **_kwargs: object) -> None: return


def _make_verification_cache(*, log: LogFn = _noop_log) -> VerificationCache:
    """Construct a verification cache, prepopulated from disk when enabled.

    Persistence is opt-out (``SPEC_CRITIC_VERIFICATION_CACHE_PERSIST=0``
    disables it). Load failures (missing file on first run, corrupt JSON,
    schema mismatch) silently fall back to an empty cache so a fresh user
    is never blocked by cache state.
    """
    cache = VerificationCache()
    if not cache_persist_enabled():
        return cache
    try:
        loaded = cache.load_from_disk()
    except Exception as exc:  # pragma: no cover - defensive
        log(f"Verification cache: load failed ({exc}); starting fresh.", level="warning")
        return cache
    if loaded:
        stats = cache.stats()
        expired_part = (
            f", {stats['expired_on_load']} expired"
            if stats.get("expired_on_load")
            else ""
        )
        log(
            f"Verification cache: loaded {loaded} entry(ies) from disk"
            f"{expired_part}.",
            level="info",
        )
    return cache


def _persist_verification_cache(cache: VerificationCache, *, log: LogFn = _noop_log) -> None:
    """Save the in-memory cache to disk if persistence is enabled.

    Failures are logged but never raised — a save failure should not abort
    a run that has already produced findings.
    """
    if not cache_persist_enabled():
        return
    try:
        size = cache.save_to_disk()
    except Exception as exc:  # pragma: no cover - defensive
        log(f"Verification cache: save failed ({exc}).", level="warning")
        return
    if size:
        log(f"Verification cache: saved {size} entry(ies) to disk.", level="info")


@dataclass
class PipelineResult:
    review_result: Optional[ReviewResult]
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None
    cycle_label: str = DEFAULT_CYCLE.label
    total_elapsed_seconds: float | None = None
    # Chunk O — the remaining deterministic alert types collected during
    # preflight. Previously only ``leed_alerts`` / ``placeholder_alerts``
    # made it to the result, so the report could not render the rest.
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    naming_alerts: list[dict] = field(default_factory=list)
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)


@dataclass
class CollectedBatchState:
    submission: BatchSubmission
    review_result: ReviewResult
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None
    cross_check_skipped_due_to_missing_specs: bool = False
    truncated_specs: list[str] = field(default_factory=list)
    # Chunk O — propagate the rest of the deterministic alerts through the
    # collect / finalize handoff so the resulting PipelineResult carries
    # them. Existing resume-state payloads load with empty defaults.
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    naming_alerts: list[dict] = field(default_factory=list)
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)


def _get_spec_files(input_dir: Path) -> list[Path]:
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(input_dir.glob(f"*{ext}"))
    return sorted([p for p in files if not p.name.startswith("~$")], key=lambda p: p.name.lower())


# ---------------------------------------------------------------------------
# Phase 1.3: formal grouping vs occurrence types.
#
# Audit Section 5.3 / plan Sprint 1 item 3 asked for a clean separation
# between the *display* concept ("the same issue appears in N files") and the
# *executable edit* concept ("apply this change to file X at location Y").
# The pipeline already achieves the behavioral goal via ``Finding.affected_files``,
# but having explicit types makes it harder to lose per-file occurrences when
# new code paths get added (e.g., the report exporter, edit dialog, comments
# mode). These dataclasses are produced by ``group_findings()`` and consumed
# by code that needs the formal split. The legacy list-of-Finding API is
# preserved so existing callers do not need to change.
# ---------------------------------------------------------------------------


@dataclass
class FindingOccurrence:
    """One executable edit candidate: a finding bound to a single file."""
    occurrence_id: str
    file_name: str
    finding: Finding


@dataclass
class FindingGroup:
    """A display-level group of findings that share dedup identity.

    The ``representative`` finding is the highest-severity / highest-
    confidence example used to render the report. ``occurrences`` lists the
    per-file edit candidates so callers fanning out edits do not have to
    re-derive them from ``affected_files``.
    """
    group_id: str
    representative: Finding
    occurrences: list[FindingOccurrence] = field(default_factory=list)

    @property
    def file_names(self) -> list[str]:
        return [o.file_name for o in self.occurrences]


def _occurrence_id(group_id: str, file_name: str, idx: int) -> str:
    return f"{group_id}::{idx:03d}::{file_name}"


def group_findings(findings: list[Finding]) -> list[FindingGroup]:
    """Convert a deduplicated finding list into formal ``FindingGroup`` rows.

    Each group's ``occurrences`` list expands ``Finding.affected_files`` so a
    multi-file finding produces one ``FindingOccurrence`` per file. Findings
    with no ``affected_files`` and no ``fileName`` produce a single
    placeholder occurrence with an empty file name; downstream code should
    check for that and skip.
    """
    groups: list[FindingGroup] = []
    for idx, f in enumerate(findings):
        files = list(dict.fromkeys(f.affected_files)) or (
            [f.fileName] if f.fileName else [""]
        )
        group_id = f"grp-{idx:04d}"
        occurrences = [
            FindingOccurrence(
                occurrence_id=_occurrence_id(group_id, name, i),
                file_name=name,
                finding=f,
            )
            for i, name in enumerate(files)
        ]
        groups.append(FindingGroup(group_id=group_id, representative=f, occurrences=occurrences))
    return groups


def expand_to_occurrences(findings: list[Finding]) -> list[FindingOccurrence]:
    """Flatten findings to per-file occurrences for edit execution."""
    return [occ for grp in group_findings(findings) for occ in grp.occurrences if occ.file_name]


def _normalize_issue_text(text: str) -> str:
    normalized = re.sub(r"\d{2}\s?\d{2}\s?\d{2}[^.]*\.docx", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _normalized_text_digest(value: str | None) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    # Hash the full text so long passages can never collide just because
    # their first 200 characters happen to match. Truncating before hashing
    # silently merged distinct findings (audit Issue 2).
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedup_key(f: Finding) -> tuple:
    return (
        _normalize_issue_text(f.issue),
        (f.section or "").strip().lower(),
        (f.codeReference or "").strip().lower(),
        f.actionType,
        _normalized_text_digest(f.existingText),
        _normalized_text_digest(f.replacementText),
    )


def compute_finding_id(f: Finding) -> str:
    """Compute a stable, deterministic id for a finding.

    Chunk M / plan section "Cross-Check Dependency Tracking": each review
    finding gets a stable id at dedup time so the cross-check pass can cite
    those ids in ``upstreamFindingIds`` and the post-verification
    suppression filter can match dependencies deterministically instead of
    falling back to file/section overlap.

    The id is derived from the same key the dedup helper uses, so two
    findings with the same dedup identity share the same id (and a
    representative carries the id of the group). The hash is truncated to
    12 hex chars — collision risk is negligible at the per-run scale we
    operate at (typically <100 findings) and the short form keeps the id
    readable in transcripts and reports.
    """
    key = _dedup_key(f)
    digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
    return f"rf-{digest[:12]}"


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    if len(findings) <= 1:
        # Chunk M: singleton lists still need a stable finding_id so the
        # cross-check pass can cite the review finding via upstream ids.
        # The early-return path used to skip the loop below, which left
        # the one finding unstamped.
        for f in findings:
            if not f.finding_id:
                f.finding_id = compute_finding_id(f)
        return findings
    groups: dict[tuple, list[Finding]] = {}
    for f in findings:
        groups.setdefault(_dedup_key(f), []).append(f)

    rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}
    out: list[Finding] = []
    for group in groups.values():
        if len(group) == 1:
            f = group[0]
            if not f.affected_files and f.fileName:
                f.affected_files = [f.fileName]
            # Chunk M: stamp a stable finding id so the cross-check pass can
            # cite it via upstream_finding_ids. Computed from the dedup key
            # so the id is deterministic across runs of the same content.
            if not f.finding_id:
                f.finding_id = compute_finding_id(f)
            out.append(f)
            continue
        group.sort(key=lambda f: (rank.get(f.severity, 99), -f.confidence))
        rep = group[0]
        files = list(dict.fromkeys([f.fileName for f in group if f.fileName]))
        # Chunk L: carry the representative's edit_proposal (or legacy
        # equivalent) onto the merged finding so REPORT_ONLY findings stay
        # REPORT_ONLY after dedupe and so the locator/edit pipeline does
        # not see a freshly-constructed Finding that lost its proposal
        # half. ``as_edit_proposal()`` reconstructs from legacy fields
        # when the representative was loaded from an older resume state.
        merged_proposal = rep.as_edit_proposal()
        # Chunk M: derive the merged finding's id from the representative
        # before issue-text mutation. The dedup key already collapses the
        # whole group to one identity, so every member would hash to the
        # same id; using rep is the cheaper of the two equivalent paths.
        merged_id = rep.finding_id or compute_finding_id(rep)
        out.append(Finding(
            severity=rep.severity,
            fileName=files[0] if files else rep.fileName,
            section=rep.section,
            issue=f"{rep.issue} (found in {len(files)} specs: {', '.join(files)})",
            actionType=rep.actionType,
            existingText=rep.existingText,
            replacementText=rep.replacementText,
            codeReference=rep.codeReference,
            confidence=max(f.confidence for f in group),
            affected_files=files,
            anchorText=rep.anchorText,
            insertPosition=rep.insertPosition,
            evidenceElementId=rep.evidenceElementId,
            edit_proposal=merged_proposal,
            finding_id=merged_id,
        ))
    return out


@dataclass
class _PreparedSpecs:
    """Phase 9 plan 13.1: preflight alerts ride alongside the leed/placeholder
    alerts. Pipeline callers log them via diagnostics; the GUI/report can pick
    them up in a follow-up commit without breaking serialization here.

    Chunk O additions: ``template_marker_alerts`` (TODO/FIXME/XXX/???),
    ``invalid_code_cycle_alerts`` (year/code citations whose year is not a
    real California cycle), and ``duplicate_paragraph_alerts`` (verbatim
    long-paragraph duplicates) sit alongside the existing lists. The pipeline
    forwards every one of them through the submission / collected-state /
    pipeline-result chain so the report can render them — previously
    ``code_cycle_alerts`` / ``structural_alerts`` / ``naming_alerts`` were
    logged but silently dropped before the report saw them.
    """
    specs: list[ExtractedSpec]
    leed_alerts: list[dict]
    placeholder_alerts: list[dict]
    # Phase 9 (plan 13.1): deterministic preflight alerts surfaced before any
    # model call. ``code_cycle_alerts`` flags references to a stale California
    # cycle for the selected ``CodeCycle``; ``structural_alerts`` includes
    # empty sections and duplicate headings; ``naming_alerts`` is a project-
    # level CSI naming consistency check across all selected files.
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    naming_alerts: list[dict] = field(default_factory=list)
    # Chunk O additions
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)
    # Chunk D4.1: per-spec view of every deterministic alert that fired for
    # that filename. The reviewer / batch paths use this to populate the
    # ``<pre_detected>`` block in each per-spec user message so the model is
    # told what was already detected locally and does not duplicate it.
    # Naming-style alerts attach to the file they describe; the project-wide
    # ``inconsistent_filename`` rule is still surfaced because each alert is
    # tagged with the offending filename.
    pre_detected_by_filename: dict[str, list[dict]] = field(default_factory=dict)


# Chunk 3: how many specs we exact-count before falling back to a top-K
# selection. The Anthropic ``count_tokens`` endpoint is a real API call —
# every spec we count adds latency to preflight and consumes a token-
# counting request. For typical project sizes (≤ this many specs) we count
# every one so no spec slips past. Above the threshold we exact-count the
# top K candidates ranked by the FULL local request shape, not the raw
# spec body (plan task 7).
_PREFLIGHT_EXACT_COUNT_ALL_THRESHOLD = 8
_PREFLIGHT_EXACT_COUNT_TOP_K = 4


def _run_exact_token_preflight(
    request_specs: list[ReviewRequestSpec],
    *,
    model: str,
    log: LogFn,
) -> None:
    """Validate each request fits under :data:`RECOMMENDED_MAX` with exact counts.

    Chunk 3: counts the *same* request shape that the batch path will
    submit (system prompt + user message including the ``<pre_detected>``
    block + tool schema + cache controls). The cache is keyed on a hash
    of the full request shape so a cached count is only reused when those
    inputs are unchanged — adding or removing a ``pre_detected`` alert
    deterministically invalidates the entry.

    For small batches (``≤ _PREFLIGHT_EXACT_COUNT_ALL_THRESHOLD``) every
    spec is exact-counted. Above the threshold the top-K ranked by full
    local estimate are counted; the local-only gate in
    :func:`_prepare_specs` still applies the model-aware safety factor
    to the rest so an undercount cannot mask an overage.

    Raises ``ValueError`` when any exact count exceeds the recommended
    maximum. ``count_tokens_via_api`` returning ``None`` (preflight
    disabled, missing key, SDK mismatch) is treated as "preflight
    unavailable" — the local gate is the fallback authority.
    """
    if not request_specs:
        return

    if len(request_specs) <= _PREFLIGHT_EXACT_COUNT_ALL_THRESHOLD:
        candidates = list(request_specs)
    else:
        # Rank by the FULL local request shape (system + user_message
        # including pre_detected alerts). Reordering files cannot cause a
        # smaller raw spec to bypass exact-count when its alert block
        # makes the real request larger — plan task 7.
        scored = sorted(
            ((estimate_local_request_tokens(rs), idx, rs) for idx, rs in enumerate(request_specs)),
            key=lambda triple: triple[0],
            reverse=True,
        )
        candidates = [rs for _, _, rs in scored[:_PREFLIGHT_EXACT_COUNT_TOP_K]]

    for rs in candidates:
        cache_key = review_request_cache_key(rs)
        exact_tokens = get_cached_token_count(cache_key)
        if exact_tokens is None:
            _, count_kwargs = build_token_count_request(rs)
            exact_tokens = count_tokens_via_api(**count_kwargs)
            if exact_tokens is not None:
                cache_token_count(cache_key, exact_tokens)
        if exact_tokens is None:
            # Preflight unavailable for this spec — the local gate will
            # still apply the model-aware safety factor in the caller.
            continue
        local = estimate_local_request_tokens(rs)
        log(
            f"Token preflight ({rs.filename}, model={model}): "
            f"local~{local:,} | exact={exact_tokens:,}",
            level="info",
        )
        if exact_tokens > RECOMMENDED_MAX:
            raise ValueError(
                f"Spec '{rs.filename}' is too large for a single API call: "
                f"exact API token count {exact_tokens:,} exceeds recommended "
                f"maximum {RECOMMENDED_MAX:,} for model {model}."
            )


def _prepare_specs(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle = DEFAULT_CYCLE, mode: ReviewMode = DEFAULT_REVIEW_MODE, model: str = REVIEW_MODEL_DEFAULT) -> _PreparedSpecs:
    spec_files = [Path(f) for f in files] if files else _get_spec_files(Path(input_dir))
    if not spec_files:
        raise FileNotFoundError(f"No specification files found in: {input_dir}")

    specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []
    code_cycle_alerts: list[dict] = []
    structural_alerts: list[dict] = []
    template_marker_alerts: list[dict] = []
    invalid_code_cycle_alerts: list[dict] = []
    duplicate_paragraph_alerts: list[dict] = []
    # Chunk D4.1: per-filename view of the per-spec alerts so the reviewer
    # / batch paths can hand each spec only its own alerts when building
    # the ``<pre_detected>`` block.
    pre_detected_by_filename: dict[str, list[dict]] = {}
    progress(0.0, "Extracting text from specifications...")
    # Phase 5.2 (audit Section 9.2): parallel extraction. Order is preserved
    # by extract_multiple_specs, so deterministic file ordering and per-spec
    # progress reporting remain stable. Per-file errors still propagate to
    # the caller — the pool maintains the original semantics.
    # Phase 9 plan 13.2: cache extraction by file identity so repeated runs
    # with toggled options skip the DOCX parse. Falls through to the parallel
    # extractor for misses.
    extracted = extract_multiple_specs_cached(spec_files)
    for i, (p, spec) in enumerate(zip(spec_files, extracted), start=1):
        if spec.word_count == 0 or not spec.content.strip():
            log(f"Skipping {p.name}: no extractable text content", level="warning")
            progress((i / len(spec_files)) * 25.0, f"Loaded {i}/{len(spec_files)}")
            continue
        specs.append(spec)
        pre = preprocess_spec(spec.content, spec.filename, cycle=cycle)
        leed_alerts.extend(pre.leed_alerts)
        placeholder_alerts.extend(pre.placeholder_alerts)
        code_cycle_alerts.extend(pre.code_cycle_alerts)
        structural_alerts.extend(pre.structural_alerts)
        template_marker_alerts.extend(pre.template_marker_alerts)
        invalid_code_cycle_alerts.extend(pre.invalid_code_cycle_alerts)
        duplicate_paragraph_alerts.extend(pre.duplicate_paragraph_alerts)
        # Chunk D4.1: cache this spec's alerts under its filename so the
        # reviewer / batch paths can hand them to the prompt builder.
        # Naming-style alerts are appended below once the project-wide
        # check runs (they aren't part of ``preprocess_spec``).
        pre_detected_by_filename[spec.filename] = [
            *pre.leed_alerts,
            *pre.placeholder_alerts,
            *pre.code_cycle_alerts,
            *pre.structural_alerts,
            *pre.template_marker_alerts,
            *pre.invalid_code_cycle_alerts,
            *pre.duplicate_paragraph_alerts,
        ]
        progress((i / len(spec_files)) * 25.0, f"Loaded {i}/{len(spec_files)}")
    if not specs:
        raise FileNotFoundError("All files failed extraction. No specs to review.")

    # Phase 9 plan 13.1: project-level naming consistency check. Logged so
    # users see it before submission; never raises.
    naming_alerts = detect_inconsistent_file_naming([s.filename for s in specs])
    # Chunk D4.1: route project-level naming alerts back to the file they
    # describe so the model sees them in its ``<pre_detected>`` block too.
    for alert in naming_alerts:
        fname = alert.get("filename")
        if fname:
            pre_detected_by_filename.setdefault(fname, []).append(alert)
    if code_cycle_alerts:
        log(
            f"Preflight: {len(code_cycle_alerts)} stale code-cycle reference(s) "
            f"detected against the {cycle.label} cycle.",
            level="warning",
        )
    if structural_alerts:
        log(
            f"Preflight: {len(structural_alerts)} structural issue(s) "
            "(empty/duplicate sections) detected.",
            level="warning",
        )
    if naming_alerts:
        log(
            f"Preflight: {len(naming_alerts)} file(s) use a non-dominant CSI "
            "naming style.",
            level="warning",
        )
    if template_marker_alerts:
        log(
            f"Preflight: {len(template_marker_alerts)} unresolved template "
            "marker(s) (TODO/FIXME/XXX/???) detected.",
            level="warning",
        )
    if invalid_code_cycle_alerts:
        log(
            f"Preflight: {len(invalid_code_cycle_alerts)} invalid California "
            "code-cycle citation(s) detected (year is not a real cycle).",
            level="warning",
        )
    if duplicate_paragraph_alerts:
        log(
            f"Preflight: {len(duplicate_paragraph_alerts)} duplicate "
            "paragraph(s) detected (verbatim copy-paste).",
            level="warning",
        )

    # Chunk 3: build a ReviewRequestSpec per ExtractedSpec so the preflight
    # counts the same request shape that the batch path will submit. The
    # builder owns the prompt construction including the ``<pre_detected>``
    # alert block and the id-tagged paragraph rendering, so a spec with a
    # small body but a large alert block cannot slip past preflight.
    request_specs: list[ReviewRequestSpec] = [
        ReviewRequestSpec(
            spec_content=spec.content,
            filename=spec.filename,
            model=model,
            cycle=cycle,
            mode=mode,
            project_context=project_context,
            paragraph_map=spec.paragraph_map,
            pre_detected_alerts=pre_detected_by_filename.get(spec.filename),
            batch=True,
        )
        for spec in specs
    ]

    # Chunk E directive 3: when the Anthropic ``count_tokens`` endpoint
    # returns a number, that is the authoritative gate. The local
    # cl100k_base count is only used as a fast pre-check and as the
    # fallback when the API call is disabled or fails.
    #
    # Chunk 3 plan task 7: rank candidates by the FULL local request shape
    # (system + user_message including pre_detected alerts) rather than by
    # raw spec body length. Reordering files cannot cause a smaller raw
    # spec to bypass exact-count when its wrapper / alerts make the real
    # request larger.
    if token_count_preflight_enabled() and request_specs:
        _run_exact_token_preflight(
            request_specs,
            model=model,
            log=log,
        )

    # Per-spec local gate. Runs whether or not the exact preflight fired;
    # if exact counts are available the candidates are already known safe,
    # but every spec must still pass the local + safety-factor gate.
    # Chunk E directive 5: apply the model-specific safety multiplier so a
    # cl100k_base undercount cannot mask a real overage.
    # Chunk 3: the local gate also uses the *full* request shape (system +
    # the materialized user message with pre_detected alerts) so the gate
    # no longer undercounts when alerts dominate the request body.
    safety = local_estimate_safety_factor(model)
    for spec, rs in zip(specs, request_specs):
        total_local = estimate_local_request_tokens(rs)
        # The exceeds-limit helper compares (spec + overhead) against the
        # recommended max with the safety factor. We feed it ``total_local``
        # as the spec component and zero overhead so the existing helper
        # still applies the model-aware safety factor to the full count.
        if exceeds_per_call_limit_for_model(total_local, 0, model=model):
            padded = safe_local_estimate(total_local, model=model)
            raise ValueError(
                f"Spec '{spec.filename}' is too large for a single API call: "
                f"~{total_local:,} cl100k tokens (×{safety:.2f} safety factor "
                f"for {model} → ~{padded:,}) exceeds recommended max "
                f"{RECOMMENDED_MAX:,}."
            )

    cache_stats = extraction_cache_stats()
    if cache_stats["hits"]:
        log(
            f"Extraction cache: {cache_stats['hits']} hit(s) reused; "
            f"{cache_stats['misses']} miss(es).",
            level="info",
        )

    return _PreparedSpecs(
        specs=specs,
        leed_alerts=leed_alerts,
        placeholder_alerts=placeholder_alerts,
        code_cycle_alerts=code_cycle_alerts,
        structural_alerts=structural_alerts,
        naming_alerts=naming_alerts,
        template_marker_alerts=template_marker_alerts,
        invalid_code_cycle_alerts=invalid_code_cycle_alerts,
        duplicate_paragraph_alerts=duplicate_paragraph_alerts,
        pre_detected_by_filename=pre_detected_by_filename,
    )


@dataclass
class BatchSubmission:
    job: BatchJob
    files_reviewed: list[str] = field(default_factory=list)
    review_request_ids: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    model: str = MODEL_OPUS_47
    project_context: str = ""
    prepared_specs: list[ExtractedSpec] | None = None
    cycle_label: str = DEFAULT_CYCLE.label
    cross_check_enabled: bool = False
    # Phase 8 / plan section 12.1: review mode that produced this batch.
    # Stored as the enum string value so resume-state JSON serialization is
    # trivial. ``coerce_review_mode`` handles None / unknown labels.
    review_mode: str = DEFAULT_REVIEW_MODE.value
    # Chunk O — carry the remaining deterministic alert lists so the
    # collect / finalize path can hand them off to the final PipelineResult.
    # All default to empty lists so legacy callers that build BatchSubmission
    # without these fields keep working.
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    naming_alerts: list[dict] = field(default_factory=list)
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)


def start_batch_review(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", model: str = MODEL_OPUS_47, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle = DEFAULT_CYCLE, cross_check_enabled: bool = False, mode: ReviewMode | str | None = None) -> BatchSubmission:
    review_mode = coerce_review_mode(mode)
    prepared = _prepare_specs(input_dir=input_dir, files=files, project_context=project_context, log=log, progress=progress, cycle=cycle, mode=review_mode, model=model)
    job = submit_review_batch(
        prepared.specs,
        project_context=project_context,
        model=model,
        cycle=cycle,
        mode=review_mode,
        # Chunk D4.1: feed each spec's deterministic alerts to the prompt
        # builder so the model is told what local rules already detected
        # and skips duplicating those items as new findings.
        pre_detected_alerts=prepared.pre_detected_by_filename,
    )
    ordered_ids = [cid for cid, _ in sorted(job.request_map.items(), key=lambda item: item[1]["index"])]
    return BatchSubmission(
        job=job,
        files_reviewed=[s.filename for s in prepared.specs],
        review_request_ids=ordered_ids,
        leed_alerts=prepared.leed_alerts,
        placeholder_alerts=prepared.placeholder_alerts,
        model=model,
        project_context=project_context,
        prepared_specs=prepared.specs,
        cycle_label=cycle.label,
        cross_check_enabled=cross_check_enabled,
        review_mode=review_mode.value,
        code_cycle_alerts=prepared.code_cycle_alerts,
        structural_alerts=prepared.structural_alerts,
        naming_alerts=prepared.naming_alerts,
        template_marker_alerts=prepared.template_marker_alerts,
        invalid_code_cycle_alerts=prepared.invalid_code_cycle_alerts,
        duplicate_paragraph_alerts=prepared.duplicate_paragraph_alerts,
    )


def _is_retryable_batch_review_result(rr: ReviewResult | None) -> bool:
    if rr is None:
        return True
    if rr.parse_status in ("parse_error", "incomplete"):
        return True
    if not rr.error:
        return False
    lowered = rr.error.lower()
    return any(token in lowered for token in ("batch request errored", "batch request expired", "batch request canceled"))


def _recover_retryable_review_batch_results(
    submission: BatchSubmission,
    results_by_request: dict[str, ReviewResult],
    *,
    log: LogFn = _noop_log,
) -> dict[str, ReviewResult]:
    retryable_request_ids = [rid for rid in submission.review_request_ids if _is_retryable_batch_review_result(results_by_request.get(rid))]
    if not retryable_request_ids:
        return results_by_request
    if not submission.prepared_specs:
        log("Batch review fallback skipped: original extracted specs are unavailable.", level="warning")
        return results_by_request

    cycle = AVAILABLE_CYCLES.get(submission.cycle_label, DEFAULT_CYCLE)
    repair_specs: list[ExtractedSpec] = []
    repair_id_map: dict[str, str] = {}
    for rid in retryable_request_ids:
        meta = submission.job.request_map.get(rid) or {}
        spec_index = meta.get("index")
        if not isinstance(spec_index, int) or spec_index < 0 or spec_index >= len(submission.prepared_specs):
            log(f"Review repair skipped for {rid}: original spec index is unavailable.", level="warning")
            continue
        spec = submission.prepared_specs[spec_index]
        repair_specs.append(spec)
        repair_id_map[spec.filename] = rid

    if not repair_specs:
        log("No specs eligible for review repair batch.", level="warning")
        return results_by_request

    log(f"Submitting review repair batch for {len(repair_specs)} failed item(s)...", level="step")
    # Chunk D4.1: the repair batch reuses the same prompt builder, so it
    # should also tell the model what was already detected locally. Alerts
    # are deterministic given (content, filename, cycle), so we recompute
    # them here rather than threading the original map through resume state.
    repair_pre_detected: dict[str, list[dict]] = {}
    for spec in repair_specs:
        pre = preprocess_spec(spec.content, spec.filename, cycle=cycle)
        repair_pre_detected[spec.filename] = [
            *pre.leed_alerts,
            *pre.placeholder_alerts,
            *pre.code_cycle_alerts,
            *pre.structural_alerts,
            *pre.template_marker_alerts,
            *pre.invalid_code_cycle_alerts,
            *pre.duplicate_paragraph_alerts,
        ]
    repair_job = submit_review_batch(
        repair_specs,
        project_context=submission.project_context,
        model=submission.model,
        cycle=cycle,
        retry_instruction=(
            "This is a retry of a previously truncated review. Submit findings via the "
            "submit_review_findings tool with analysis_summary set to an empty string. "
            "Spend the entire output budget on the findings array."
        ),
        mode=coerce_review_mode(submission.review_mode),
        pre_detected_alerts=repair_pre_detected,
    )
    outcome = poll_batch_bounded(
        repair_job.batch_id,
        policy=DEFAULT_REVIEW_POLL_POLICY,
        log=log,
        progress_cb=lambda _status: None,
    )
    if outcome.detached or outcome.poll_failed:
        log(
            f"Review repair batch did not complete. {len(retryable_request_ids)} item(s) "
            "will appear as failed in the report.",
            level="warning",
        )
        return results_by_request

    repair_results = retrieve_review_results(repair_job, model=submission.model)
    recovered = 0
    for repair_custom_id, repair_rr in repair_results.items():
        repair_meta = repair_job.request_map.get(repair_custom_id) or {}
        original_rid = repair_id_map.get(repair_meta.get("filename", ""))
        if original_rid and repair_rr and not repair_rr.error:
            results_by_request[original_rid] = repair_rr
            recovered += 1
    repair_level = "success" if recovered == len(repair_specs) else "warning"
    log(f"Review repair batch recovered {recovered}/{len(repair_specs)} item(s).", level=repair_level)
    return results_by_request


def _log_cross_check_status(log: LogFn, cross: ReviewResult):
    if cross.cross_check_status == "completed":
        if cross.findings:
            log(f"Cross-check found {len(cross.findings)} coordination issues", level="info")
        else:
            log("Cross-check completed — no coordination issues found", level="success")
    elif cross.cross_check_status == "skipped":
        log(f"Cross-check skipped: {cross.thinking}", level="warning")
    elif cross.cross_check_status == "failed":
        log(f"Cross-check failed: {cross.error}", level="error")


def classify_cross_check_dependencies(
    cross_findings: list[Finding],
    review_findings: list[Finding],
    *,
    log: LogFn = _noop_log,
) -> tuple[list[Finding], list[Finding]]:
    """Partition cross-check findings into (kept, suppressed) by upstream verdict.

    Chunk M / plan section "Cross-Check Dependency Tracking": when a
    cross-check finding cites upstream review ids in ``upstream_finding_ids``,
    those ids are the authoritative dependency check. The rules are:

    * If at least one cited upstream is NOT DISPUTED, keep the finding —
      the dependency still holds on the surviving upstream(s).
    * If every cited upstream is DISPUTED but the finding has
      ``independent_evidence_ids``, keep it — raw spec evidence stands on
      its own even when the upstream finding falls.
    * If every cited upstream is DISPUTED and there is no independent
      evidence, suppress the finding with a reason naming the disputed
      upstream(s) so the report can explain the decision.
    * If the finding cites no upstream ids, fall back to the prior file +
      section overlap heuristic. The fallback is labeled as such so
      operators can see when the model failed to emit ids — typically a
      sign that a future ID rollout is incomplete or that the fallback
      tagged-JSON parser was used. ``suppression_reason`` carries the
      fallback marker so the report can render the reduced confidence.

    Returns ``(kept, suppressed)``. Suppressed findings have their
    ``suppression_reason`` field set; callers should stash them on
    ``ReviewResult.suppressed_findings`` rather than dropping them silently.
    """
    # Build lookups keyed by stable finding id (Chunk M) and by the legacy
    # (filename, section) tuple (pre-Chunk-M heuristic fallback). The id
    # path is preferred when both are populated.
    id_to_verdict: dict[str, str] = {}
    id_to_finding: dict[str, Finding] = {}
    for f in review_findings:
        if not f.finding_id:
            continue
        verdict = (f.verification.verdict if f.verification else "") or ""
        id_to_verdict[f.finding_id] = verdict
        id_to_finding[f.finding_id] = f

    disputed_keys: set[tuple[str, str]] = set()
    for f in review_findings:
        if not f.verification or f.verification.verdict != "DISPUTED":
            continue
        files = list(f.affected_files) or [f.fileName]
        for fname in files:
            disputed_keys.add(((fname or "").strip().lower(), (f.section or "").strip().lower()))

    kept: list[Finding] = []
    suppressed: list[Finding] = []
    id_dropped = 0
    fallback_dropped = 0

    for f in cross_findings:
        upstream_ids = [uid for uid in (f.upstream_finding_ids or []) if uid]
        if upstream_ids:
            # ID-based path. Inspect every cited upstream's verdict to decide.
            cited = [(uid, id_to_verdict.get(uid, "")) for uid in upstream_ids]
            disputed_upstream = [
                (uid, id_to_finding.get(uid))
                for uid, verdict in cited
                if verdict == "DISPUTED"
            ]
            non_disputed = [uid for uid, verdict in cited if verdict != "DISPUTED"]
            if non_disputed:
                # At least one upstream still stands — keep the finding.
                kept.append(f)
                continue
            if f.independent_evidence_ids:
                # Independent raw-spec evidence holds even when every cited
                # upstream is discredited.
                kept.append(f)
                continue
            # Every cited upstream disputed, no independent evidence — drop.
            disputed_labels: list[str] = []
            for uid, upstream in disputed_upstream:
                if upstream is None:
                    disputed_labels.append(uid)
                    continue
                label_parts = [uid]
                if upstream.fileName:
                    label_parts.append(upstream.fileName)
                if upstream.section:
                    label_parts.append(upstream.section)
                disputed_labels.append(" — ".join(label_parts))
            reason = (
                "All cited upstream review findings were DISPUTED and no "
                "independent spec evidence was provided. Disputed upstream(s): "
                + "; ".join(disputed_labels)
                + "."
            )
            f.suppression_reason = reason
            suppressed.append(f)
            id_dropped += 1
            continue

        # Fallback path: the model did not cite upstream ids (older payload,
        # tagged-JSON fallback, or a coordination claim the model genuinely
        # could not attribute). Use the legacy file+section heuristic.
        if not disputed_keys:
            kept.append(f)
            continue
        files = list(f.affected_files) or [f.fileName]
        section_key = (f.section or "").strip().lower()
        matched_keys = [
            ((fname or "").strip().lower(), section_key)
            for fname in files
            if ((fname or "").strip().lower(), section_key) in disputed_keys
        ]
        if not matched_keys:
            kept.append(f)
            continue
        reason = (
            "Heuristic fallback: matched a DISPUTED review finding by "
            f"(file, section) overlap on {matched_keys[0][0] or '<no file>'} / "
            f"{matched_keys[0][1] or '<no section>'} because the cross-check "
            "finding did not cite an upstream id."
        )
        f.suppression_reason = reason
        suppressed.append(f)
        fallback_dropped += 1

    if id_dropped:
        log(
            f"Cross-check: suppressing {id_dropped} finding(s) whose every "
            "cited upstream review finding was DISPUTED (id-based).",
            level="warning",
        )
    if fallback_dropped:
        log(
            f"Cross-check: suppressing {fallback_dropped} finding(s) whose "
            "upstream review finding was DISPUTED (heuristic fallback by "
            "file/section overlap because the cross-check finding did not "
            "cite an upstream id).",
            level="warning",
        )
    return kept, suppressed


def _drop_cross_check_findings_with_disputed_upstream(
    cross_findings: list[Finding],
    review_findings: list[Finding],
    *,
    log: LogFn = _noop_log,
) -> list[Finding]:
    """Backward-compatible wrapper around :func:`classify_cross_check_dependencies`.

    Returns only the kept findings so older callers (and the Phase 5 / 7
    tests) keep their existing list-of-Finding signature. New pipeline code
    should call :func:`classify_cross_check_dependencies` directly so it
    can stash suppressed findings on the cross-check result for reporting.
    """
    kept, _suppressed = classify_cross_check_dependencies(
        cross_findings, review_findings, log=log,
    )
    return kept


def _parallel_cross_check_enabled() -> bool:
    """Phase 5.3 (audit Section 9.3): parallel cross-check overlap.

    Cross-check runs concurrently with the review verification batch poll;
    findings whose upstream review verdict became DISPUTED are dropped
    after both join. Set SPEC_CRITIC_PARALLEL_CROSS_CHECK=0 to revert to
    the prior sequential flow.
    """
    return os.environ.get("SPEC_CRITIC_PARALLEL_CROSS_CHECK", "1").strip() not in {"0", "false", "no"}


def collect_batch_results(submission: BatchSubmission, *, verify: bool = True, cross_check: bool | None = None, specs: list[ExtractedSpec] | None = None, project_context: str | None = None, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle | None = None) -> PipelineResult:
    if cross_check is None:
        cross_check = submission.cross_check_enabled
    if specs is None:
        specs = submission.prepared_specs
    if project_context is None:
        project_context = submission.project_context
    if cycle is None:
        cycle = AVAILABLE_CYCLES.get(submission.cycle_label, DEFAULT_CYCLE)

    # Phase 3: one cache per pipeline run lets the cross-check verification
    # phase reuse evidence already gathered during review verification.
    # Phase 10: the cache also persists to disk between runs so the same
    # claim verified yesterday isn't re-verified today.
    cache = _make_verification_cache(log=log)

    state = collect_review_batch_results(submission, log=log)

    parallel = cross_check and _parallel_cross_check_enabled() and bool(state.review_result.findings)
    cross_check_future = None
    cross_check_executor: ThreadPoolExecutor | None = None
    if parallel:
        # Kick off cross-check before we start polling the verification batch.
        # The cross-check call blocks on a remote streaming response, so it
        # would otherwise sit idle while we poll. Start it on a worker thread
        # so the two long-running calls overlap.
        cross_check_executor = ThreadPoolExecutor(max_workers=1)
        cross_check_future = cross_check_executor.submit(
            run_cross_check_for_batch,
            state,
            specs=specs,
            project_context=project_context,
            cycle=cycle,
            log=log,
        )

    if verify and state.review_result.findings:
        try:
            progress(55.0, f"Submitting {len(state.review_result.findings)} verification requests...")
            verification_submission = start_batch_verification(
                state.review_result.findings, cycle=cycle, log=log, progress=progress, cache=cache,
            )
            if verification_submission is not None:
                collect_batch_verification_results(
                    verification_submission, state.review_result.findings,
                    cycle=cycle, log=log, progress=progress, cache=cache,
                )
        except Exception as e:
            log(f"Verification failed: {e}. Returning results without verification.", level="error")
            for f in state.review_result.findings:
                if f.verification is None:
                    f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unavailable: {e}")

    if cross_check:
        if cross_check_future is not None:
            try:
                state = cross_check_future.result()
            except Exception as e:
                log(f"Cross-check failed during parallel run: {e}.", level="error")
                if state.cross_check_result is None:
                    state.cross_check_result = ReviewResult(
                        findings=[],
                        cross_check_status="failed",
                        error=str(e),
                    )
            finally:
                if cross_check_executor is not None:
                    cross_check_executor.shutdown(wait=False)
        else:
            state = run_cross_check_for_batch(state, specs=specs, project_context=project_context, cycle=cycle, log=log)

        cross_verifiable = list(state.cross_check_result.findings) if state.cross_check_result and state.cross_check_result.findings else []
        # Phase 5.3 (audit Section 9.3): when running in parallel the
        # cross-check did not have access to verified findings yet, so it
        # may have built coordination claims on review findings that have
        # since been DISPUTED. Drop those before spending tokens verifying
        # them.
        #
        # Chunk M: the suppression is now ID-based when the model cited
        # upstream review finding ids in ``upstream_finding_ids``. Findings
        # that cited no ids fall back to the legacy (file, section)
        # heuristic, labeled as a fallback in logs. Both paths stash the
        # dropped findings on ``cross_check_result.suppressed_findings``
        # with an explanatory ``suppression_reason`` so the report can
        # explain the decision rather than silently making the finding
        # disappear.
        if parallel and cross_verifiable:
            cross_verifiable, suppressed = classify_cross_check_dependencies(
                cross_verifiable,
                state.review_result.findings,
                log=log,
            )
            if state.cross_check_result is not None:
                state.cross_check_result.findings = cross_verifiable
                # Preserve any prior suppressions (e.g., from a resumed
                # session) and append the ones produced this round.
                state.cross_check_result.suppressed_findings = (
                    list(state.cross_check_result.suppressed_findings) + suppressed
                )
        if verify and cross_verifiable:
            try:
                progress(90.0, f"Submitting {len(cross_verifiable)} cross-check verification requests...")
                verification_submission = start_batch_verification(
                    cross_verifiable, cycle=cycle, log=log, progress=progress, cache=cache,
                )
                if verification_submission is not None:
                    collect_batch_verification_results(
                        verification_submission, cross_verifiable,
                        cycle=cycle, log=log, progress=progress, cache=cache,
                    )
            except Exception as e:
                log(f"Cross-check verification failed: {e}.", level="error")
                for f in cross_verifiable:
                    if f.verification is None:
                        f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unavailable: {e}")
    cache_stats = cache.stats()
    if cache_stats["hits"] or cache_stats["size"]:
        log(
            f"Verification cache: {cache_stats['hits']} hits, "
            f"{cache_stats['misses']} misses across {cache_stats['size']} unique claim(s).",
            level="info",
        )
    _persist_verification_cache(cache, log=log)

    progress(100.0, "Done.")
    return finalize_batch_result(state)


def collect_review_batch_results(submission: BatchSubmission, *, log: LogFn = _noop_log) -> CollectedBatchState:
    results_by_request = retrieve_review_results(submission.job, model=submission.model)
    results_by_request = _recover_retryable_review_batch_results(submission, results_by_request, log=log)
    all_findings: list[Finding] = []
    all_thinking: list[str] = []
    errors: list[str] = []
    truncated_specs: list[str] = []
    in_tok = out_tok = 0

    for rid in submission.review_request_ids:
        meta = submission.job.request_map.get(rid)
        filename = meta["filename"] if meta else rid
        rr = results_by_request.get(rid)
        if rr is None:
            errors.append(f"{filename}: No result returned from batch")
            truncated_specs.append(filename)
            continue
        if rr.parse_status == "incomplete":
            errors.append(
                f"{filename}: Review response truncated — output exceeded token limit. "
                "No findings extracted. Re-run this spec individually."
            )
            truncated_specs.append(filename)
            continue
        if rr.parse_status == "parse_error":
            errors.append(
                f"{filename}: Could not parse review output. "
                "No findings extracted. Re-run this spec individually."
            )
            truncated_specs.append(filename)
            continue
        if rr.error:
            errors.append(f"{filename}: {rr.error}")
            # Errored/expired/canceled batch requests previously fell through
            # silently as "no findings" — cross-check would then run as if the
            # spec had been reviewed cleanly (audit Issue 7). Surface them as
            # truncated so the GUI flags the spec and downstream filters can
            # exclude it.
            truncated_specs.append(filename)
            continue
        all_findings.extend(rr.findings)
        if rr.thinking:
            all_thinking.append(f"--- {filename} ---\n{rr.thinking}")
        in_tok += rr.input_tokens
        out_tok += rr.output_tokens

    all_findings = _deduplicate_findings(all_findings)
    combined = ReviewResult(findings=all_findings, thinking="\n\n".join(all_thinking), model=submission.model, input_tokens=in_tok, output_tokens=out_tok, elapsed_seconds=time.time() - submission.job.created_at)
    if errors:
        combined.thinking += "\n\n--- Batch Errors ---\n" + "\n".join(f"  - {e}" for e in errors)
        # --- FIX 2a: Surface per-spec errors on combined result ---
        combined.error = f"{len(errors)} spec(s) had errors: " + "; ".join(errors)

    return CollectedBatchState(
        submission=submission,
        review_result=combined,
        files_reviewed=submission.files_reviewed,
        leed_alerts=submission.leed_alerts,
        placeholder_alerts=submission.placeholder_alerts,
        truncated_specs=truncated_specs,
        # Chunk O — forward every alert list the submission carries so
        # finalize_batch_result can ship them on the PipelineResult.
        code_cycle_alerts=list(submission.code_cycle_alerts),
        structural_alerts=list(submission.structural_alerts),
        naming_alerts=list(submission.naming_alerts),
        template_marker_alerts=list(submission.template_marker_alerts),
        invalid_code_cycle_alerts=list(submission.invalid_code_cycle_alerts),
        duplicate_paragraph_alerts=list(submission.duplicate_paragraph_alerts),
    )


def run_cross_check_for_batch(state: CollectedBatchState, *, specs: list[ExtractedSpec] | None = None, project_context: str | None = None, cycle: CodeCycle = DEFAULT_CYCLE, log: LogFn = _noop_log) -> CollectedBatchState:
    if not state.submission.cross_check_enabled:
        return state
    if specs is None:
        specs = state.submission.prepared_specs
    if project_context is None:
        project_context = state.submission.project_context
    if not specs:
        state.cross_check_skipped_due_to_missing_specs = True
        skipped = ReviewResult(findings=[], cross_check_status="skipped", thinking="Cross-check skipped: original extracted spec content is not available.")
        state.cross_check_result = skipped
        _log_cross_check_status(log, skipped)
        return state
    # Exclude specs whose individual review failed/truncated so cross-check
    # does not make coordination claims based on a spec that was never
    # successfully reviewed (audit Issue 7).
    failed_filenames = set(state.truncated_specs or [])
    if failed_filenames:
        filtered_specs = [s for s in specs if s.filename not in failed_filenames]
        if len(filtered_specs) != len(specs):
            log(
                "Cross-check excluding "
                f"{len(specs) - len(filtered_specs)} spec(s) that failed review: "
                + ", ".join(sorted(failed_filenames)),
                level="warning",
            )
        specs = filtered_specs
    if not specs:
        state.cross_check_skipped_due_to_missing_specs = True
        skipped = ReviewResult(findings=[], cross_check_status="skipped", thinking="Cross-check skipped: every spec failed review.")
        state.cross_check_result = skipped
        _log_cross_check_status(log, skipped)
        return state
    dedup_findings = [
        f for f in state.review_result.findings
        if not (f.verification and f.verification.verdict == "DISPUTED")
    ]
    # Phase 8 / plan section 12.3: chunk by CSI division when the combined
    # input would otherwise exceed the cross-check token budget. This used
    # to surface as a ``skipped`` status — large projects therefore got no
    # coordination review at all. ``run_chunked_cross_check`` falls back to
    # the original single-pass ``run_cross_check`` when the input fits.
    cross = run_chunked_cross_check(specs, dedup_findings, project_context=project_context, cycle=cycle, log=log)
    state.cross_check_result = cross
    _log_cross_check_status(log, cross)
    return state


def prepare_verification_work(state: CollectedBatchState) -> list[Finding]:
    all_verifiable = list(state.review_result.findings)
    if state.cross_check_result and state.cross_check_result.findings:
        all_verifiable.extend(state.cross_check_result.findings)
    return all_verifiable


def start_batch_verification(
    findings: list[Finding],
    *,
    cycle: CodeCycle = DEFAULT_CYCLE,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    cache: VerificationCache | None = None,
) -> BatchJob | None:
    """Submit a verification batch, applying Phase 3 pre-pass first.

    Returns ``None`` if every finding resolved locally (local-skip or cache
    hit) — callers should treat that as "verification complete" without
    polling. Returns the BatchJob otherwise.
    """
    remaining = prepare_findings_for_verification(findings, cycle=cycle, cache=cache, log=log)
    if not remaining:
        progress(60.0, "Verification: all findings resolved locally / cached.")
        return None
    progress(60.0, f"Submitting {len(remaining)} verification requests...")
    job = start_verification_batch(remaining, cycle=cycle)
    job.submitted_findings = remaining
    log(f"Verification batch submitted: {job.batch_id}", level="step")
    return job


def collect_batch_verification_results(
    job: BatchJob,
    findings: list[Finding],
    *,
    cycle: CodeCycle = DEFAULT_CYCLE,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    poll_interval: int = 15,
    cache: VerificationCache | None = None,
) -> list[Finding]:
    submitted = job.submitted_findings if job.submitted_findings is not None else findings
    return collect_verification_batch_results(
        job,
        submitted,
        cycle=cycle,
        log=log,
        progress=lambda p, m: progress(60.0 + (p / 100.0) * 35.0, m),
        poll_interval=poll_interval,
        cache=cache,
    )


def finalize_batch_result(state: CollectedBatchState) -> PipelineResult:
    return PipelineResult(
        review_result=state.review_result,
        files_reviewed=state.files_reviewed,
        leed_alerts=state.leed_alerts,
        placeholder_alerts=state.placeholder_alerts,
        cross_check_result=state.cross_check_result,
        cycle_label=state.submission.cycle_label,
        total_elapsed_seconds=time.time() - state.submission.job.created_at,
        # Chunk O — pass the deterministic-check lists through to the report.
        code_cycle_alerts=list(state.code_cycle_alerts),
        structural_alerts=list(state.structural_alerts),
        naming_alerts=list(state.naming_alerts),
        template_marker_alerts=list(state.template_marker_alerts),
        invalid_code_cycle_alerts=list(state.invalid_code_cycle_alerts),
        duplicate_paragraph_alerts=list(state.duplicate_paragraph_alerts),
    )


def run_review(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", model: str = MODEL_OPUS_47, verify: bool = True, cross_check: bool = False, dry_run: bool = False, verbose: bool = False, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, stream_callback: Optional[StreamCallback] = None, cycle: CodeCycle = DEFAULT_CYCLE, mode: ReviewMode | str | None = None) -> PipelineResult:
    start = time.time()
    review_mode = coerce_review_mode(mode)
    prepared = _prepare_specs(input_dir=Path(input_dir), files=files, project_context=project_context, log=log, progress=progress, cycle=cycle, mode=review_mode, model=model)
    specs = prepared.specs
    if dry_run:
        return PipelineResult(
            review_result=ReviewResult(findings=[], model=model),
            files_reviewed=[s.filename for s in specs],
            leed_alerts=prepared.leed_alerts,
            placeholder_alerts=prepared.placeholder_alerts,
            cycle_label=cycle.label,
            total_elapsed_seconds=time.time() - start,
            code_cycle_alerts=prepared.code_cycle_alerts,
            structural_alerts=prepared.structural_alerts,
            naming_alerts=prepared.naming_alerts,
            template_marker_alerts=prepared.template_marker_alerts,
            invalid_code_cycle_alerts=prepared.invalid_code_cycle_alerts,
            duplicate_paragraph_alerts=prepared.duplicate_paragraph_alerts,
        )

    findings: list[Finding] = []
    thinking: list[str] = []
    in_tok = out_tok = 0
    errors: list[str] = []
    for i, spec in enumerate(specs, start=1):
        progress(25.0 + ((i - 1) / len(specs)) * 25.0, f"Reviewing {spec.filename} ({i}/{len(specs)})...")
        rr = review_single_spec(
            spec.content,
            spec.filename,
            project_context=project_context,
            model=model,
            verbose=verbose,
            stream_callback=stream_callback,
            cycle=cycle,
            mode=review_mode,
            # Chunk K2: forward the paragraph map so the prompt builder
            # can render id-tagged elements and the model can cite ids.
            paragraph_map=spec.paragraph_map,
            # Chunk D4.1: forward the per-spec deterministic alerts so the
            # model is told what local rules already detected and skips
            # duplicating them as new findings.
            pre_detected_alerts=prepared.pre_detected_by_filename.get(spec.filename),
        )
        if rr.parse_status == "incomplete":
            log(f"  {spec.filename}: Response incomplete — model ran out of output tokens. No findings extracted.", level="warning")
        if rr.error:
            errors.append(f"{spec.filename}: {rr.error}")
            continue
        findings.extend(rr.findings)
        if rr.thinking:
            thinking.append(f"--- {spec.filename} ---\n{rr.thinking}")
        in_tok += rr.input_tokens
        out_tok += rr.output_tokens

    findings = _deduplicate_findings(findings)
    cross = None
    cache = _make_verification_cache(log=log)
    verify_progress = _phase_tagged_progress(progress, "verification")
    if verify and findings:
        try:
            verify_findings(findings, progress=lambda c, t, fn: verify_progress(50.0 + (c / max(t, 1)) * 25.0, f"Verifying {c}/{t} ({fn})..."), cycle=cycle, cache=cache)
        except Exception as e:
            log(f"Verification failed: {e}. Returning results without verification.", level="error", phase="verification")
            for f in findings:
                if f.verification is None:
                    f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unavailable: {e}")
    if cross_check:
        dedup_for_cross = [f for f in findings if not (f.verification and f.verification.verdict == "DISPUTED")]
        progress(75.0, "Running cross-check with dedup context...", phase="cross_check")
        cross = run_chunked_cross_check(specs, dedup_for_cross, project_context=project_context, verbose=verbose, cycle=cycle, log=_phase_tagged_log(log, "cross_check"))
        _log_cross_check_status(log, cross)
        if verify and cross and cross.findings:
            try:
                verify_findings(cross.findings, progress=lambda c, t, fn: verify_progress(90.0 + (c / max(t, 1)) * 5.0, f"Verifying cross-check {c}/{t} ({fn})..."), cycle=cycle, cache=cache)
            except Exception as e:
                log(f"Cross-check verification failed: {e}.", level="error", phase="verification")
                for f in cross.findings:
                    if f.verification is None:
                        f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unavailable: {e}")

    combined = ReviewResult(findings=findings, thinking="\n\n".join(thinking), model=model, input_tokens=in_tok, output_tokens=out_tok, elapsed_seconds=time.time() - start)
    if errors:
        combined.thinking += "\n\n--- Review Errors ---\n" + "\n".join(f"  - {e}" for e in errors)
        # --- FIX 2b: Surface per-spec errors on combined result ---
        combined.error = f"{len(errors)} spec(s) had errors: " + "; ".join(errors)
    _persist_verification_cache(cache, log=log)
    progress(100.0, "Done.")
    return PipelineResult(
        review_result=combined,
        files_reviewed=[s.filename for s in specs],
        leed_alerts=prepared.leed_alerts,
        placeholder_alerts=prepared.placeholder_alerts,
        cross_check_result=cross,
        cycle_label=cycle.label,
        total_elapsed_seconds=combined.elapsed_seconds,
        code_cycle_alerts=prepared.code_cycle_alerts,
        structural_alerts=prepared.structural_alerts,
        naming_alerts=prepared.naming_alerts,
        template_marker_alerts=prepared.template_marker_alerts,
        invalid_code_cycle_alerts=prepared.invalid_code_cycle_alerts,
        duplicate_paragraph_alerts=prepared.duplicate_paragraph_alerts,
    )