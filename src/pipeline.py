"""Core orchestration pipeline for Spec Critic."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .extractor import extract_text, ExtractedSpec, SUPPORTED_EXTENSIONS
from .preprocessor import preprocess_spec
from .tokenizer import RECOMMENDED_MAX, count_tokens, exceeds_per_call_limit
from .reviewer import review_single_spec, ReviewResult, Finding, MODEL_OPUS_46, StreamCallback
from .batch import BatchJob, submit_review_batch, retrieve_review_results
from .batch_runtime import DEFAULT_REVIEW_POLL_POLICY, poll_batch_bounded
from .verifier import (
    verify_findings,
    verify_findings_batch,
    start_verification_batch,
    collect_verification_batch_results,
    VerificationResult,
)
from .cross_checker import run_cross_check
from .code_cycles import CodeCycle, DEFAULT_CYCLE, AVAILABLE_CYCLES
from .prompts import get_system_prompt

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]


def _noop_log(_: str) -> None: return

def _noop_progress(_: float, __: str) -> None: return


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


def _normalize_issue_text(text: str) -> str:
    normalized = re.sub(r"\d{2}\s?\d{2}\s?\d{2}[^.]*\.docx", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _full_text_digest(text: str | None) -> str:
    normalized = (text or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _dedup_key(f: Finding) -> tuple:
    return (
        _normalize_issue_text(f.issue),
        (f.section or "").strip().lower(),
        (f.codeReference or "").strip().lower(),
        f.actionType,
        _full_text_digest(f.existingText),
        _full_text_digest(f.replacementText),
        _full_text_digest(f.anchorText),
        (f.insertPosition or "").strip().lower(),
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
            anchorText=rep.anchorText,
            insertPosition=rep.insertPosition,
        ))
    return out


@dataclass
class _PreparedSpecs:
    specs: list[ExtractedSpec]
    leed_alerts: list[dict]
    placeholder_alerts: list[dict]


def _prepare_specs(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle = DEFAULT_CYCLE) -> _PreparedSpecs:
    spec_files = [Path(f) for f in files] if files else _get_spec_files(Path(input_dir))
    if not spec_files:
        raise FileNotFoundError(f"No specification files found in: {input_dir}")

    specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []
    progress(0.0, "Extracting text from specifications...")
    for i, p in enumerate(spec_files, start=1):
        spec = extract_text(p)
        if spec.word_count == 0 or not spec.content.strip():
            log(f"Skipping {p.name}: no extractable text content")
            continue
        specs.append(spec)
        pre = preprocess_spec(spec.content, spec.filename)
        leed_alerts.extend(pre.leed_alerts)
        placeholder_alerts.extend(pre.placeholder_alerts)
        progress((i / len(spec_files)) * 25.0, f"Loaded {i}/{len(spec_files)}")
    if not specs:
        raise FileNotFoundError("All files failed extraction. No specs to review.")

    system_tokens = count_tokens(get_system_prompt(cycle))
    ctx_tokens = count_tokens(project_context) if project_context else 0
    for spec in specs:
        spec_tokens = count_tokens(spec.content)
        est = system_tokens + ctx_tokens + spec_tokens
        if exceeds_per_call_limit(spec_tokens, system_tokens + ctx_tokens):
            raise ValueError(f"Spec '{spec.filename}' is too large for a single API call: ~{est:,} tokens (recommended max: {RECOMMENDED_MAX:,}).")

    return _PreparedSpecs(specs=specs, leed_alerts=leed_alerts, placeholder_alerts=placeholder_alerts)


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


def start_batch_review(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", model: str = MODEL_OPUS_46, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle = DEFAULT_CYCLE, cross_check_enabled: bool = False, export_mode: bool = False) -> BatchSubmission:
    prepared = _prepare_specs(input_dir=input_dir, files=files, project_context=project_context, log=log, progress=progress, cycle=cycle)
    job = submit_review_batch(prepared.specs, project_context=project_context, model=model, cycle=cycle)
    ordered_ids = [cid for cid, _ in sorted(job.request_map.items(), key=lambda item: item[1]["index"])]
    return BatchSubmission(job=job, files_reviewed=[s.filename for s in prepared.specs], review_request_ids=ordered_ids, leed_alerts=prepared.leed_alerts, placeholder_alerts=prepared.placeholder_alerts, model=model, project_context=project_context, prepared_specs=prepared.specs, cycle_label=cycle.label, cross_check_enabled=cross_check_enabled, export_mode=export_mode)


def _is_retryable_batch_review_result(rr: ReviewResult | None) -> bool:
    if rr is None:
        return True
    if rr.parse_status in ("parse_error", "incomplete", "batch_failed"):
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
        log("Batch review fallback skipped: original extracted specs are unavailable.")
        return results_by_request

    cycle = AVAILABLE_CYCLES.get(submission.cycle_label, DEFAULT_CYCLE)
    repair_specs: list[ExtractedSpec] = []
    repair_id_map: dict[str, str] = {}
    for rid in retryable_request_ids:
        meta = submission.job.request_map.get(rid) or {}
        spec_index = meta.get("index")
        if not isinstance(spec_index, int) or spec_index < 0 or spec_index >= len(submission.prepared_specs):
            log(f"Review repair skipped for {rid}: original spec index is unavailable.")
            continue
        spec = submission.prepared_specs[spec_index]
        repair_specs.append(spec)
        repair_id_map[spec.filename] = rid

    if not repair_specs:
        log("No specs eligible for review repair batch.")
        return results_by_request

    log(f"Submitting review repair batch for {len(repair_specs)} failed item(s)...")
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
            "will appear as failed in the report."
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
    log(f"Review repair batch recovered {recovered}/{len(repair_specs)} item(s).")
    return results_by_request


def _log_cross_check_status(log: LogFn, cross: ReviewResult):
    if cross.cross_check_status == "completed":
        if cross.findings:
            log(f"Cross-check found {len(cross.findings)} coordination issues")
        else:
            log("Cross-check completed — no coordination issues found")
    elif cross.cross_check_status == "skipped":
        log(f"Cross-check skipped: {cross.thinking}")
    elif cross.cross_check_status == "failed":
        log(f"Cross-check failed: {cross.error}")


def collect_batch_results(submission: BatchSubmission, *, verify: bool = True, cross_check: bool | None = None, specs: list[ExtractedSpec] | None = None, project_context: str | None = None, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, cycle: CodeCycle | None = None) -> PipelineResult:
    if cross_check is None:
        cross_check = submission.cross_check_enabled
    if specs is None:
        specs = submission.prepared_specs
    if project_context is None:
        project_context = submission.project_context
    if cycle is None:
        cycle = AVAILABLE_CYCLES.get(submission.cycle_label, DEFAULT_CYCLE)

    state = collect_review_batch_results(submission, log=log)
    if verify and state.review_result.findings:
        try:
            progress(55.0, f"Submitting {len(state.review_result.findings)} verification requests...")
            verification_submission = start_batch_verification(state.review_result.findings, cycle=cycle, log=log, progress=progress)
            collect_batch_verification_results(verification_submission, state.review_result.findings, cycle=cycle, log=log, progress=progress)
        except Exception as e:
            log(f"Verification failed: {e}. Returning results without verification.")
            for f in state.review_result.findings:
                if f.verification is None:
                    f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unavailable: {e}")

    if cross_check:
        state = run_cross_check_for_batch(state, specs=specs, project_context=project_context, cycle=cycle, log=log)
        cross_verifiable = list(state.cross_check_result.findings) if state.cross_check_result and state.cross_check_result.findings else []
        if verify and cross_verifiable:
            try:
                progress(90.0, f"Submitting {len(cross_verifiable)} cross-check verification requests...")
                verification_submission = start_batch_verification(cross_verifiable, cycle=cycle, log=log, progress=progress)
                collect_batch_verification_results(verification_submission, cross_verifiable, cycle=cycle, log=log, progress=progress)
            except Exception as e:
                log(f"Cross-check verification failed: {e}.")
                for f in cross_verifiable:
                    if f.verification is None:
                        f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unavailable: {e}")

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
        if rr.parse_status == "batch_failed":
            errors.append(
                f"{filename}: {rr.error or 'Batch request failed.'} "
                "No findings extracted. Re-run this spec individually."
            )
            truncated_specs.append(filename)
            continue
        if rr.error:
            errors.append(f"{filename}: {rr.error}")
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
    failed_filenames = set(state.truncated_specs)
    cross_check_specs = [s for s in specs if s.filename not in failed_filenames]
    if failed_filenames:
        log(
            f"Cross-check excluding {len(failed_filenames)} spec(s) that failed review: "
            f"{', '.join(sorted(failed_filenames))}"
        )
    if len(cross_check_specs) < 2:
        skipped = ReviewResult(
            findings=[],
            cross_check_status="skipped",
            thinking=(
                "Cross-check skipped: fewer than two specs reviewed successfully "
                f"(failed: {', '.join(sorted(failed_filenames)) or 'none'})."
            ),
        )
        state.cross_check_result = skipped
        _log_cross_check_status(log, skipped)
        return state
    dedup_findings = [
        f for f in state.review_result.findings
        if not (f.verification and f.verification.verdict == "DISPUTED")
    ]
    cross = run_cross_check(cross_check_specs, dedup_findings, project_context=project_context, cycle=cycle)
    state.cross_check_result = cross
    _log_cross_check_status(log, cross)
    return state


def prepare_verification_work(state: CollectedBatchState) -> list[Finding]:
    all_verifiable = list(state.review_result.findings)
    if state.cross_check_result and state.cross_check_result.findings:
        all_verifiable.extend(state.cross_check_result.findings)
    return all_verifiable


def start_batch_verification(findings: list[Finding], *, cycle: CodeCycle = DEFAULT_CYCLE, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress) -> BatchJob:
    progress(60.0, f"Submitting {len(findings)} verification requests...")
    job = start_verification_batch(findings, cycle=cycle)
    log(f"Verification batch submitted: {job.batch_id}")
    return job


def collect_batch_verification_results(job: BatchJob, findings: list[Finding], *, cycle: CodeCycle = DEFAULT_CYCLE, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, poll_interval: int = 15) -> list[Finding]:
    return collect_verification_batch_results(
        job,
        findings,
        cycle=cycle,
        log=log,
        progress=lambda p, m: progress(60.0 + (p / 100.0) * 35.0, m),
        poll_interval=poll_interval,
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


def run_review(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", model: str = MODEL_OPUS_46, verify: bool = True, cross_check: bool = False, dry_run: bool = False, verbose: bool = False, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, stream_callback: Optional[StreamCallback] = None, cycle: CodeCycle = DEFAULT_CYCLE) -> PipelineResult:
    start = time.time()
    prepared = _prepare_specs(input_dir=Path(input_dir), files=files, project_context=project_context, log=log, progress=progress, cycle=cycle)
    specs = prepared.specs
    if dry_run:
        return PipelineResult(review_result=ReviewResult(findings=[], model=model), files_reviewed=[s.filename for s in specs], leed_alerts=prepared.leed_alerts, placeholder_alerts=prepared.placeholder_alerts, cycle_label=cycle.label, total_elapsed_seconds=time.time() - start)

    findings: list[Finding] = []
    thinking: list[str] = []
    in_tok = out_tok = 0
    errors: list[str] = []
    for i, spec in enumerate(specs, start=1):
        progress(25.0 + ((i - 1) / len(specs)) * 25.0, f"Reviewing {spec.filename} ({i}/{len(specs)})...")
        rr = review_single_spec(spec.content, spec.filename, project_context=project_context, model=model, verbose=verbose, stream_callback=stream_callback, cycle=cycle)
        if rr.parse_status == "incomplete":
            log(f"  {spec.filename}: Response incomplete — model ran out of output tokens. No findings extracted.")
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
    if verify and findings:
        try:
            verify_findings(findings, progress=lambda c, t, fn: progress(50.0 + (c / max(t, 1)) * 25.0, f"Verifying {c}/{t} ({fn})..."), cycle=cycle)
        except Exception as e:
            log(f"Verification failed: {e}. Returning results without verification.")
            for f in findings:
                if f.verification is None:
                    f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unavailable: {e}")
    if cross_check:
        dedup_for_cross = [f for f in findings if not (f.verification and f.verification.verdict == "DISPUTED")]
        progress(75.0, "Running cross-check with dedup context...")
        cross = run_cross_check(specs, dedup_for_cross, project_context=project_context, verbose=verbose, cycle=cycle)
        _log_cross_check_status(log, cross)
        if verify and cross and cross.findings:
            try:
                verify_findings(cross.findings, progress=lambda c, t, fn: progress(90.0 + (c / max(t, 1)) * 5.0, f"Verifying cross-check {c}/{t} ({fn})..."), cycle=cycle)
            except Exception as e:
                log(f"Cross-check verification failed: {e}.")
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