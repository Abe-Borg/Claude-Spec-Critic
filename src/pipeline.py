"""
Core orchestration pipeline for Spec Critic.

This module is the SINGLE SOURCE OF TRUTH for the review workflow.
The GUI calls run_review() for real-time mode or start_batch_review()
+ collect_batch_results() for batch mode.

v2.3.0 — Opus-only pipeline. All stages (review, cross-check, verification)
    use Claude Opus 4.6.

v1.9.0 — PDF support.
v1.7.0 — Verification batching + model selection.
v1.6.0 — Cross-spec coordination pass (optional).
v1.5.0 — Confidence scoring, always-on verification, finding deduplication.
v1.4.0 — Per-spec siloed review + batch processing support.
v1.3.0 — project_context parameter.
v1.1.0 — All output is in-app. No files emitted.

Design decisions:
    - Hard stop on token limit exceeded (no silent truncation)
    - LEED/placeholder detection is LOCAL, not sent to LLM (saves tokens)
    - Streaming callback enables real-time GUI updates
    - No files are written — everything renders in the UI
    - Per-spec errors are collected, not raised immediately
    - Deduplication is local (no API call) and runs before verification
    - Cross-check is optional and uses Opus 4.6
    - Cross-check findings are kept separate for distinct rendering
    - In batch mode, verification also goes through the Batches API
      for 50% cost savings
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .extractor import extract_text, ExtractedSpec, SUPPORTED_EXTENSIONS
from .preprocessor import preprocess_spec
from .prompts import get_system_prompt
from .tokenizer import RECOMMENDED_MAX, count_tokens, exceeds_per_call_limit
from .reviewer import (
    review_single_spec,
    ReviewResult,
    Finding,
    MODEL_OPUS_46,
    StreamCallback,
)
from .batch import BatchJob, BatchStatus, submit_review_batch, poll_batch, retrieve_review_results
from .verifier import verify_findings, verify_findings_batch, VerificationResult
from .cross_checker import run_cross_check

# Type aliases for callback signatures
LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]  # percent (0-100), message


def _noop_log(_: str) -> None:
    return


def _noop_progress(_: float, __: str) -> None:
    return


@dataclass
class PipelineResult:
    """Container for all results from a pipeline run."""
    review_result: Optional[ReviewResult]
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None


def _get_spec_files(input_dir: Path) -> list[Path]:
    """Get all supported spec files from a directory, excluding temp files."""
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(input_dir.glob(f"*{ext}"))
    return sorted([p for p in files if not p.name.startswith("~$")], key=lambda p: p.name.lower())


# ---------------------------------------------------------------------------
# Finding deduplication (v1.5.0)
# ---------------------------------------------------------------------------

def _normalize_issue_text(text: str) -> str:
    normalized = re.sub(r"\d{2}\s?\d{2}\s?\d{2}[^.]*\.(docx|pdf)", "", text, flags=re.IGNORECASE)
    normalized = re.sub(r"(?:Part\s+\d+,?\s*)?Article\s+[\d.]+[A-Z]*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Consolidate duplicate findings across multiple specs."""
    if len(findings) <= 1:
        return findings

    groups: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        key = (_normalize_issue_text(f.issue), (f.codeReference or "").strip().lower(), f.actionType)
        groups.setdefault(key, []).append(f)

    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}

    deduplicated: list[Finding] = []
    for key, group in groups.items():
        if len(group) == 1:
            deduplicated.append(group[0])
            continue

        group.sort(key=lambda f: (severity_rank.get(f.severity, 99), -f.confidence))
        representative = group[0]

        filenames = list(dict.fromkeys(f.fileName for f in group if f.fileName))
        file_list = ", ".join(filenames)

        consolidated_issue = (
            f"{representative.issue} "
            f"(found in {len(filenames)} specs: {file_list})"
        )

        deduplicated.append(Finding(
            severity=representative.severity,
            fileName=filenames[0] if filenames else representative.fileName,
            section=representative.section,
            issue=consolidated_issue,
            actionType=representative.actionType,
            existingText=representative.existingText,
            replacementText=representative.replacementText,
            codeReference=representative.codeReference,
            confidence=max(f.confidence for f in group),
        ))

    return deduplicated


@dataclass
class _PreparedSpecs:
    """Internal container for extracted and validated specs."""
    specs: list[ExtractedSpec]
    leed_alerts: list[dict]
    placeholder_alerts: list[dict]


def _prepare_specs(
    *,
    input_dir: Path,
    files: Optional[list[Path]] = None,
    project_context: str = "",
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> _PreparedSpecs:
    """Extract, preprocess, and validate specs. Shared by real-time and batch modes."""
    input_dir = Path(input_dir)

    spec_files = [Path(f) for f in files] if files else _get_spec_files(input_dir)

    if not spec_files:
        raise FileNotFoundError(f"No specification files found in: {input_dir}")

    progress(0.0, "Extracting text from specifications...")
    specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []

    total_files = len(spec_files)
    for i, p in enumerate(spec_files, start=1):
        log(f"Loading: {p.name}")
        try:
            spec = extract_text(p)
        except Exception as e:
            log(f"Error extracting {p.name}: {e}")
            progress((i / total_files) * 25.0, f"Skipped {p.name} (extraction error)")
            continue

        if getattr(spec, "is_probably_scanned", False) and spec.word_count == 0:
            log(f"Skipping {p.name}: scanned/image PDF with no extractable text")
            progress((i / total_files) * 25.0, f"Skipped {p.name} (scanned PDF)")
            continue

        if spec.word_count == 0 or not spec.content.strip():
            log(f"Skipping {p.name}: no extractable text content")
            progress((i / total_files) * 25.0, f"Skipped {p.name} (empty)")
            continue

        specs.append(spec)

        pre = preprocess_spec(spec.content, spec.filename)
        leed_alerts.extend(pre.leed_alerts)
        placeholder_alerts.extend(pre.placeholder_alerts)

        progress((i / total_files) * 25.0, f"Loaded {i}/{total_files}")

    if not specs:
        raise FileNotFoundError(
            f"All {len(spec_files)} files failed extraction. No specs to review."
        )

    progress(30.0, "Checking token limits...")
    system_prompt = get_system_prompt()
    system_prompt_tokens = count_tokens(system_prompt)
    context_tokens = count_tokens(project_context) if project_context else 0

    for spec in specs:
        spec_tokens = count_tokens(spec.content)
        estimated_call_tokens = system_prompt_tokens + context_tokens + spec_tokens

        if exceeds_per_call_limit(spec_tokens, system_prompt_tokens + context_tokens):
            raise ValueError(
                f"Spec '{spec.filename}' is too large for a single API call: "
                f"~{estimated_call_tokens:,} input tokens before output/padding "
                f"(recommended max: {RECOMMENDED_MAX:,}). "
                "Split this spec before review."
            )

    return _PreparedSpecs(
        specs=specs,
        leed_alerts=leed_alerts,
        placeholder_alerts=placeholder_alerts,
    )


# ---------------------------------------------------------------------------
# Batch mode entry points
# ---------------------------------------------------------------------------

@dataclass
class BatchSubmission:
    """Returned by start_batch_review()."""
    job: BatchJob
    files_reviewed: list[str] = field(default_factory=list)
    review_request_ids: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    model: str = MODEL_OPUS_46
    project_context: str = ""
    prepared_specs: list[ExtractedSpec] | None = None


def start_batch_review(
    *,
    input_dir: Path,
    files: Optional[list[Path]] = None,
    project_context: str = "",
    model: str = MODEL_OPUS_46,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> BatchSubmission:
    """Extract specs and submit them as a Message Batch (50% cost savings)."""
    prepared = _prepare_specs(
        input_dir=input_dir,
        files=files,
        project_context=project_context,
        log=log,
        progress=progress,
    )

    progress(35.0, "Submitting batch...")
    log(f"Submitting {len(prepared.specs)} specs to Anthropic Batch API...")

    job = submit_review_batch(
        prepared.specs,
        project_context=project_context,
        model=model,
    )

    log(f"Batch submitted: {job.batch_id}")
    progress(40.0, f"Batch submitted — {len(prepared.specs)} specs queued")

    ordered_request_ids = [
        custom_id
        for custom_id, _meta in sorted(
            job.request_map.items(),
            key=lambda item: item[1]["index"],
        )
    ]

    return BatchSubmission(
        job=job,
        files_reviewed=[s.filename for s in prepared.specs],
        review_request_ids=ordered_request_ids,
        leed_alerts=prepared.leed_alerts,
        placeholder_alerts=prepared.placeholder_alerts,
        model=model,
        project_context=project_context,
        prepared_specs=prepared.specs,
    )


def collect_batch_results(
    submission: BatchSubmission,
    *,
    verify: bool = True,
    cross_check: bool = False,
    specs: list[ExtractedSpec] | None = None,
    project_context: str = "",
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> PipelineResult:
    """Retrieve and aggregate results from a completed batch."""
    model = getattr(submission, "model", MODEL_OPUS_46)
    if not submission.review_request_ids:
        submission.review_request_ids = [
            custom_id
            for custom_id, _meta in sorted(
                submission.job.request_map.items(),
                key=lambda item: item[1]["index"],
            )
        ]
    results_by_request = retrieve_review_results(submission.job, model=model)

    all_findings: list[Finding] = []
    all_thinking: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    errors: list[str] = []

    for request_id in submission.review_request_ids:
        meta = submission.job.request_map.get(request_id)
        if not meta:
            errors.append(f"{request_id}: Missing request metadata")
            continue

        filename = meta["filename"]
        result = results_by_request.get(request_id)
        if result is None:
            errors.append(f"{filename}: No result returned from batch")
            continue

        if result.error:
            errors.append(f"{filename}: {result.error}")
            continue

        all_findings.extend(result.findings)
        if result.thinking:
            all_thinking.append(f"--- {filename} ---\n{result.thinking}")
        total_input_tokens += result.input_tokens
        total_output_tokens += result.output_tokens

    if errors and not all_findings and not all_thinking:
        raise RuntimeError(
            f"All batch results failed:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    pre_dedup_count = len(all_findings)
    all_findings = _deduplicate_findings(all_findings)
    post_dedup_count = len(all_findings)
    if pre_dedup_count != post_dedup_count:
        log(f"Deduplicated: {pre_dedup_count} findings → {post_dedup_count} unique findings")

    combined_result = ReviewResult(
        findings=all_findings,
        raw_response="",
        thinking="\n\n".join(all_thinking),
        model=model,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        elapsed_seconds=time.time() - submission.job.created_at,
    )

    if errors:
        combined_result.thinking += (
            f"\n\n--- Batch Errors ---\n"
            f"The following specs had errors:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    # Cross-spec coordination check (optional)
    cross_check_result = None
    if cross_check and specs and len(specs) >= 2:
        progress(55.0, "Running cross-spec coordination check...")
        log(f"Running cross-spec coordination check across {len(specs)} specs (Opus 4.6)...")

        cross_check_result = run_cross_check(
            specs,
            all_findings,
            project_context=project_context,
        )

        if cross_check_result.error:
            log(f"Cross-check error: {cross_check_result.error}")
        elif cross_check_result.findings:
            log(f"Cross-check found {len(cross_check_result.findings)} coordination issues")
        else:
            log("Cross-check found no coordination issues")

    # Verification — batch mode uses batched verification
    all_verifiable = list(all_findings)
    if cross_check_result and cross_check_result.findings:
        all_verifiable.extend(cross_check_result.findings)

    if verify and all_verifiable:
        try:
            verifiable_count = len(all_verifiable)
            progress(60.0, f"Submitting {verifiable_count} findings for batch verification...")
            log(f"Submitting {verifiable_count} findings for batch verification (Opus 4.6, 50% savings)...")

            def _batch_verify_progress(pct: float, msg: str):
                overall_pct = 60.0 + (pct / 100.0) * 35.0
                progress(overall_pct, msg)

            verify_findings_batch(
                all_verifiable,
                log=log,
                progress=_batch_verify_progress,
            )

            verdicts = {}
            for f in all_verifiable:
                if f.verification:
                    v = f.verification.verdict
                    verdicts[v] = verdicts.get(v, 0) + 1
            log(f"Verification complete: {', '.join(f'{v}: {c}' for v, c in sorted(verdicts.items()))}")
        except Exception as e:
            log(f"Verification failed: {e}. Returning results without verification.")
            for f in all_verifiable:
                if f.verification is None:
                    f.verification = VerificationResult(
                        verdict="UNVERIFIED",
                        explanation=f"Verification unavailable: {e}",
                    )

    progress(100.0, "Done.")
    return PipelineResult(
        review_result=combined_result,
        files_reviewed=submission.files_reviewed,
        leed_alerts=submission.leed_alerts,
        placeholder_alerts=submission.placeholder_alerts,
        cross_check_result=cross_check_result,
    )


def run_review(
    *,
    input_dir: Path,
    files: Optional[list[Path]] = None,
    project_context: str = "",
    model: str = MODEL_OPUS_46,
    verify: bool = True,
    cross_check: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    stream_callback: Optional[StreamCallback] = None,
) -> PipelineResult:
    """Execute the full specification review pipeline."""
    start_time = time.time()
    input_dir = Path(input_dir)

    prepared = _prepare_specs(
        input_dir=input_dir,
        files=files,
        project_context=project_context,
        log=log,
        progress=progress,
    )
    specs = prepared.specs
    leed_alerts = prepared.leed_alerts
    placeholder_alerts = prepared.placeholder_alerts
    total_files = len(specs)

    if dry_run:
        log("Dry-run enabled: skipping API calls.")
        dummy = ReviewResult(findings=[], raw_response="", model=model)
        progress(100.0, "Dry run complete.")
        return PipelineResult(
            review_result=dummy,
            files_reviewed=[s.filename for s in specs],
            leed_alerts=leed_alerts,
            placeholder_alerts=placeholder_alerts,
        )

    # Per-spec siloed review
    all_findings: list[Finding] = []
    all_thinking: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    errors: list[str] = []

    review_end_pct = 55.0

    for i, spec in enumerate(specs):
        spec_num = i + 1
        progress_base = 35.0
        progress_range = review_end_pct - progress_base
        spec_progress = progress_base + (i / total_files) * progress_range

        progress(spec_progress, f"Reviewing {spec.filename} ({spec_num}/{total_files})...")
        log(f"Reviewing: {spec.filename} ({spec_num}/{total_files})")

        result = review_single_spec(
            spec_content=spec.content,
            filename=spec.filename,
            project_context=project_context,
            model=model,
            verbose=verbose,
            stream_callback=stream_callback,
        )

        if result.error:
            error_msg = f"{spec.filename}: {result.error}"
            errors.append(error_msg)
            log(f"Error reviewing {spec.filename}: {result.error}")
            continue

        all_findings.extend(result.findings)
        if result.thinking:
            all_thinking.append(f"--- {spec.filename} ---\n{result.thinking}")
        total_input_tokens += result.input_tokens
        total_output_tokens += result.output_tokens

        log(f"  {spec.filename}: {len(result.findings)} findings")

    if errors and not all_findings and not all_thinking:
        raise RuntimeError(
            f"All {len(errors)} spec reviews failed:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    # Finding deduplication
    pre_dedup_count = len(all_findings)
    all_findings = _deduplicate_findings(all_findings)
    post_dedup_count = len(all_findings)
    if pre_dedup_count != post_dedup_count:
        log(f"Deduplicated: {pre_dedup_count} findings → {post_dedup_count} unique findings")
    progress(review_end_pct, f"Review complete — {post_dedup_count} unique findings")

    # Cross-spec coordination check (optional)
    cross_check_result = None
    if cross_check and len(specs) >= 2:
        cross_check_start_pct = 56.0
        cross_check_end_pct = 65.0
        progress(cross_check_start_pct, f"Running cross-spec coordination check across {len(specs)} specs...")
        log(f"Running cross-spec coordination check across {len(specs)} specs (Opus 4.6)...")

        cross_check_result = run_cross_check(
            specs,
            all_findings,
            project_context=project_context,
            verbose=verbose,
        )

        if cross_check_result.error:
            log(f"Cross-check error: {cross_check_result.error}")
        elif cross_check_result.findings:
            log(f"Cross-check found {len(cross_check_result.findings)} coordination issues")
        else:
            log("Cross-check found no coordination issues")

        progress(cross_check_end_pct, "Cross-check complete")
    elif cross_check and len(specs) < 2:
        log("Cross-spec coordination skipped: need 2+ specs")

    # Web search verification (always enabled)
    all_verifiable = list(all_findings)
    if cross_check_result and cross_check_result.findings:
        all_verifiable.extend(cross_check_result.findings)

    verify_start_pct = 66.0

    if verify and all_verifiable:
        try:
            verifiable_count = len(all_verifiable)
            progress(verify_start_pct, f"Verifying {verifiable_count} findings via web search...")
            log(f"Verifying {verifiable_count} findings with Opus 4.6 + web search...")

            def _verify_progress(current: int, total: int, filename: str):
                verify_pct = verify_start_pct + (current / total) * (95.0 - verify_start_pct)
                progress(verify_pct, f"Verifying finding {current}/{total} ({filename})...")

            verify_findings(all_verifiable, progress=_verify_progress)

            verdicts = {}
            for f in all_verifiable:
                if f.verification:
                    v = f.verification.verdict
                    verdicts[v] = verdicts.get(v, 0) + 1

            verdict_summary = ", ".join(f"{v}: {c}" for v, c in sorted(verdicts.items()))
            log(f"Verification complete: {verdict_summary}")
        except Exception as e:
            log(f"Verification failed: {e}. Returning results without verification.")
            for f in all_verifiable:
                if f.verification is None:
                    f.verification = VerificationResult(
                        verdict="UNVERIFIED",
                        explanation=f"Verification unavailable: {e}",
                    )

    # Aggregate results
    elapsed = time.time() - start_time

    combined_result = ReviewResult(
        findings=all_findings,
        raw_response="",
        thinking="\n\n".join(all_thinking),
        model=model,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        elapsed_seconds=elapsed,
    )

    if errors:
        error_note = (
            f"\n\n--- Review Errors ---\n"
            f"The following specs could not be reviewed:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )
        combined_result.thinking += error_note

    progress(100.0, "Done.")
    return PipelineResult(
        review_result=combined_result,
        files_reviewed=[s.filename for s in specs],
        leed_alerts=leed_alerts,
        placeholder_alerts=placeholder_alerts,
        cross_check_result=cross_check_result,
    )