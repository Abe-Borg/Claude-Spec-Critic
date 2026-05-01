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
    token_count_cache_key,
)
from .preprocessor import preprocess_spec, detect_inconsistent_file_naming
from .tokenizer import RECOMMENDED_MAX, count_tokens, count_tokens_via_api, exceeds_per_call_limit
from .reviewer import review_single_spec, ReviewResult, Finding, MODEL_OPUS_46, StreamCallback
from .batch import BatchJob, submit_review_batch, retrieve_review_results
from .batch_runtime import DEFAULT_REVIEW_POLL_POLICY, poll_batch_bounded
from .api_config import token_count_preflight_enabled
from .verifier import (
    verify_findings,
    verify_findings_batch,
    start_verification_batch,
    collect_verification_batch_results,
    prepare_findings_for_verification,
    VerificationResult,
)
from .verification_cache import VerificationCache
from .cross_checker import run_cross_check, run_chunked_cross_check
from .code_cycles import CodeCycle, DEFAULT_CYCLE, AVAILABLE_CYCLES
from .prompts import get_system_prompt
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


@dataclass
class PipelineResult:
    review_result: Optional[ReviewResult]
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None
    cycle_label: str = DEFAULT_CYCLE.label
    total_elapsed_seconds: float | None = None


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


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    if len(findings) <= 1:
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
            out.append(f)
            continue
        group.sort(key=lambda f: (rank.get(f.severity, 99), -f.confidence))
        rep = group[0]
        files = list(dict.fromkeys([f.fileName for f in group if f.fileName]))
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
        ))
    return out


@dataclass
class _PreparedSpecs:
    """Phase 9 plan 13.1: preflight alerts ride alongside the leed/placeholder
    alerts. Pipeline callers log them via diagnostics; the GUI/report can pick
    them up in a follow-up commit without breaking serialization here."""
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


def _prepare_specs(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle = DEFAULT_CYCLE, mode: ReviewMode = DEFAULT_REVIEW_MODE) -> _PreparedSpecs:
    spec_files = [Path(f) for f in files] if files else _get_spec_files(Path(input_dir))
    if not spec_files:
        raise FileNotFoundError(f"No specification files found in: {input_dir}")

    specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []
    code_cycle_alerts: list[dict] = []
    structural_alerts: list[dict] = []
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
        progress((i / len(spec_files)) * 25.0, f"Loaded {i}/{len(spec_files)}")
    if not specs:
        raise FileNotFoundError("All files failed extraction. No specs to review.")

    # Phase 9 plan 13.1: project-level naming consistency check. Logged so
    # users see it before submission; never raises.
    naming_alerts = detect_inconsistent_file_naming([s.filename for s in specs])
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

    system_prompt = get_system_prompt(cycle, mode=mode)
    system_tokens = count_tokens(system_prompt)
    ctx_tokens = count_tokens(project_context) if project_context else 0
    for spec in specs:
        spec_tokens = count_tokens(spec.content)
        est = system_tokens + ctx_tokens + spec_tokens
        if exceeds_per_call_limit(spec_tokens, system_tokens + ctx_tokens):
            raise ValueError(f"Spec '{spec.filename}' is too large for a single API call: ~{est:,} tokens (recommended max: {RECOMMENDED_MAX:,}).")

    # Optional Anthropic token-counting preflight. Plan section 6.3: prefer
    # exact counts for final routing/guardrail decisions; fall back to the
    # local estimate when the API call fails. This is opt-in via env to
    # avoid forcing a network round-trip for every preview.
    if token_count_preflight_enabled() and specs:
        # Pick the largest spec by local count and verify the API agrees.
        biggest = max(specs, key=lambda s: count_tokens(s.content))
        from .prompts import get_single_spec_user_message
        user_message = get_single_spec_user_message(biggest.content, biggest.filename, project_context=project_context, cycle=cycle, mode=mode)
        # Phase 9 plan 13.2: re-use the exact API count when nothing relevant
        # changed. The cache key includes model + cycle + mode + content so a
        # later run with a different cycle still preflights correctly.
        cache_key = token_count_cache_key(
            model=MODEL_OPUS_46,
            system_prompt=system_prompt,
            user_message=user_message,
            project_context=project_context,
            cycle_label=cycle.label,
            mode=mode.value,
        )
        exact = get_cached_token_count(cache_key)
        if exact is None:
            exact = count_tokens_via_api(
                model=MODEL_OPUS_46,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            if exact is not None:
                cache_token_count(cache_key, exact)
        if exact is not None:
            local = system_tokens + ctx_tokens + count_tokens(biggest.content)
            log(f"Token preflight ({biggest.filename}): local~{local:,} | exact={exact:,}", level="info")
            if exact > RECOMMENDED_MAX:
                log(
                    f"WARNING: exact token count {exact:,} exceeds recommended {RECOMMENDED_MAX:,}. "
                    "Response may be truncated.",
                    level="warning",
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
    )


@dataclass
class BatchSubmission:
    job: BatchJob
    files_reviewed: list[str] = field(default_factory=list)
    review_request_ids: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    model: str = MODEL_OPUS_46
    project_context: str = ""
    prepared_specs: list[ExtractedSpec] | None = None
    cycle_label: str = DEFAULT_CYCLE.label
    cross_check_enabled: bool = False
    export_mode: bool = False
    # Phase 8 / plan section 12.1: review mode that produced this batch.
    # Stored as the enum string value so resume-state JSON serialization is
    # trivial. ``coerce_review_mode`` handles None / unknown labels.
    review_mode: str = DEFAULT_REVIEW_MODE.value


def start_batch_review(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", model: str = MODEL_OPUS_46, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle = DEFAULT_CYCLE, cross_check_enabled: bool = False, export_mode: bool = False, mode: ReviewMode | str | None = None) -> BatchSubmission:
    review_mode = coerce_review_mode(mode)
    prepared = _prepare_specs(input_dir=input_dir, files=files, project_context=project_context, log=log, progress=progress, cycle=cycle, mode=review_mode)
    job = submit_review_batch(prepared.specs, project_context=project_context, model=model, cycle=cycle, mode=review_mode)
    ordered_ids = [cid for cid, _ in sorted(job.request_map.items(), key=lambda item: item[1]["index"])]
    return BatchSubmission(job=job, files_reviewed=[s.filename for s in prepared.specs], review_request_ids=ordered_ids, leed_alerts=prepared.leed_alerts, placeholder_alerts=prepared.placeholder_alerts, model=model, project_context=project_context, prepared_specs=prepared.specs, cycle_label=cycle.label, cross_check_enabled=cross_check_enabled, export_mode=export_mode, review_mode=review_mode.value)


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
    repair_job = submit_review_batch(
        repair_specs,
        project_context=submission.project_context,
        model=submission.model,
        cycle=cycle,
        retry_instruction=(
            "This is a retry of a previously truncated review. Return ONLY the findings JSON "
            "inside <findings_json> tags. Do not include an analysis summary. Focus on "
            "completing the structured output."
        ),
        mode=coerce_review_mode(submission.review_mode),
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


def _drop_cross_check_findings_with_disputed_upstream(
    cross_findings: list[Finding],
    review_findings: list[Finding],
    *,
    log: LogFn = _noop_log,
) -> list[Finding]:
    """Filter cross-check findings whose upstream review findings are DISPUTED.

    Phase 5.3 (audit Section 9.3): when cross-check ran in parallel with
    review verification, it may reference a review finding that ended up
    DISPUTED. Coordination claims rooted in discredited upstream evidence
    should not consume verification tokens. Match by ``(filename, section)``
    overlap with at least one DISPUTED review finding; if no match is found
    the cross-check finding is kept (provisional).
    """
    disputed_keys: set[tuple[str, str]] = set()
    for f in review_findings:
        if not f.verification or f.verification.verdict != "DISPUTED":
            continue
        files = list(f.affected_files) or [f.fileName]
        for fname in files:
            disputed_keys.add(((fname or "").strip().lower(), (f.section or "").strip().lower()))
    if not disputed_keys:
        return cross_findings
    kept: list[Finding] = []
    dropped = 0
    for f in cross_findings:
        files = list(f.affected_files) or [f.fileName]
        section_key = (f.section or "").strip().lower()
        depends_on_disputed = any(
            ((fname or "").strip().lower(), section_key) in disputed_keys
            for fname in files
        )
        if depends_on_disputed:
            dropped += 1
            continue
        kept.append(f)
    if dropped:
        log(
            f"Cross-check: dropping {dropped} finding(s) whose upstream review "
            "finding was DISPUTED.",
            level="warning",
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
    cache = VerificationCache()

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
        # them. The reference is by section + filename + issue overlap;
        # if no upstream is identifiable we keep the finding (provisional).
        if parallel and cross_verifiable:
            cross_verifiable = _drop_cross_check_findings_with_disputed_upstream(
                cross_verifiable,
                state.review_result.findings,
                log=log,
            )
            if state.cross_check_result is not None:
                state.cross_check_result.findings = cross_verifiable
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
    return collect_verification_batch_results(
        job,
        findings,
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
    )


def run_review(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", model: str = MODEL_OPUS_46, verify: bool = True, cross_check: bool = False, dry_run: bool = False, verbose: bool = False, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, stream_callback: Optional[StreamCallback] = None, cycle: CodeCycle = DEFAULT_CYCLE, mode: ReviewMode | str | None = None) -> PipelineResult:
    start = time.time()
    review_mode = coerce_review_mode(mode)
    prepared = _prepare_specs(input_dir=Path(input_dir), files=files, project_context=project_context, log=log, progress=progress, cycle=cycle, mode=review_mode)
    specs = prepared.specs
    if dry_run:
        return PipelineResult(review_result=ReviewResult(findings=[], model=model), files_reviewed=[s.filename for s in specs], leed_alerts=prepared.leed_alerts, placeholder_alerts=prepared.placeholder_alerts, cycle_label=cycle.label, total_elapsed_seconds=time.time() - start)

    findings: list[Finding] = []
    thinking: list[str] = []
    in_tok = out_tok = 0
    errors: list[str] = []
    for i, spec in enumerate(specs, start=1):
        progress(25.0 + ((i - 1) / len(specs)) * 25.0, f"Reviewing {spec.filename} ({i}/{len(specs)})...")
        rr = review_single_spec(spec.content, spec.filename, project_context=project_context, model=model, verbose=verbose, stream_callback=stream_callback, cycle=cycle, mode=review_mode)
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
    cache = VerificationCache()
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
    progress(100.0, "Done.")
    return PipelineResult(review_result=combined, files_reviewed=[s.filename for s in specs], leed_alerts=prepared.leed_alerts, placeholder_alerts=prepared.placeholder_alerts, cross_check_result=cross, cycle_label=cycle.label, total_elapsed_seconds=combined.elapsed_seconds)