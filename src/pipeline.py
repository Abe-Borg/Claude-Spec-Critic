"""Core orchestration pipeline for Spec Critic."""

from __future__ import annotations

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


@dataclass
class CollectedBatchState:
    submission: BatchSubmission
    review_result: ReviewResult
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None
    cross_check_skipped_due_to_missing_specs: bool = False


def _get_spec_files(input_dir: Path) -> list[Path]:
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(input_dir.glob(f"*{ext}"))
    return sorted([p for p in files if not p.name.startswith("~$")], key=lambda p: p.name.lower())


def _normalize_issue_text(text: str) -> str:
    normalized = re.sub(r"\d{2}\s?\d{2}\s?\d{2}[^.]*\.docx", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _dedup_key(f: Finding) -> tuple:
    return (
        _normalize_issue_text(f.issue),
        (f.section or "").strip().lower(),
        (f.codeReference or "").strip().lower(),
        f.actionType,
        (f.existingText or "").strip().lower()[:200],
        (f.replacementText or "").strip().lower()[:200],
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
    return BatchSubmission(job=job, files_reviewed=[s.filename for s in prepared.specs], review_request_ids=ordered_ids, leed_alerts=prepared.leed_alerts, placeholder_alerts=prepared.placeholder_alerts, model=model, project_context=project_context, prepared_specs=prepared.specs if cross_check_enabled else None, cycle_label=cycle.label, cross_check_enabled=cross_check_enabled, export_mode=export_mode)


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
    all_findings: list[Finding] = []
    all_thinking: list[str] = []
    errors: list[str] = []
    in_tok = out_tok = 0

    for rid in submission.review_request_ids:
        meta = submission.job.request_map.get(rid)
        filename = meta["filename"] if meta else rid
        rr = results_by_request.get(rid)
        if rr is None:
            errors.append(f"{filename}: No result returned from batch")
            continue
        if rr.parse_status == "incomplete":
            log(f"  {filename}: Response incomplete — model ran out of output tokens. No findings extracted.")
        if rr.error:
            errors.append(f"{filename}: {rr.error}")
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

    return CollectedBatchState(
        submission=submission,
        review_result=combined,
        files_reviewed=submission.files_reviewed,
        leed_alerts=submission.leed_alerts,
        placeholder_alerts=submission.placeholder_alerts,
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
    verified_findings = [
        f for f in state.review_result.findings
        if f.verification and f.verification.verdict in ("CONFIRMED", "CORRECTED")
    ]
    cross = run_cross_check(specs, verified_findings, project_context=project_context, cycle=cycle)
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
    )


def run_review(*, input_dir: Path, files: Optional[list[Path]] = None, project_context: str = "", model: str = MODEL_OPUS_46, verify: bool = True, cross_check: bool = False, dry_run: bool = False, verbose: bool = False, log: LogFn = _noop_log, progress: ProgressFn = _noop_progress, stream_callback: Optional[StreamCallback] = None, cycle: CodeCycle = DEFAULT_CYCLE) -> PipelineResult:
    start = time.time()
    prepared = _prepare_specs(input_dir=Path(input_dir), files=files, project_context=project_context, log=log, progress=progress, cycle=cycle)
    specs = prepared.specs
    if dry_run:
        return PipelineResult(review_result=ReviewResult(findings=[], model=model), files_reviewed=[s.filename for s in specs], leed_alerts=prepared.leed_alerts, placeholder_alerts=prepared.placeholder_alerts, cycle_label=cycle.label)

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
        verified_for_cross = [f for f in findings if f.verification and f.verification.verdict in ("CONFIRMED", "CORRECTED")]
        progress(75.0, "Running cross-check on verified findings...")
        cross = run_cross_check(specs, verified_for_cross, project_context=project_context, verbose=verbose, cycle=cycle)
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
    progress(100.0, "Done.")
    return PipelineResult(review_result=combined, files_reviewed=[s.filename for s in specs], leed_alerts=prepared.leed_alerts, placeholder_alerts=prepared.placeholder_alerts, cross_check_result=cross, cycle_label=cycle.label)
