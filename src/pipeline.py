"""
Core orchestration pipeline for Spec Critic.

This module is the SINGLE SOURCE OF TRUTH for the review workflow.
The GUI calls run_review() for real-time mode or start_batch_review()
+ collect_batch_results() for batch mode.

v1.7.0 — Verification batching + model selection.

    Verification batching:
        In batch mode, verification now routes through the Anthropic Message
        Batches API via verify_findings_batch() instead of making sequential
        real-time API calls. This saves 50% on verification costs. The
        real-time path continues using sequential verify_findings().

    Model selection:
        The user can now choose between Claude Opus 4.6 and Claude Sonnet 4.6
        for the first-stage review. The model parameter flows through
        run_review(), start_batch_review(), and collect_batch_results().
        Verification and cross-check always use Sonnet 4.6.

v1.6.0 — Cross-spec coordination pass (optional).

    After per-spec review and deduplication, an optional cross-spec
    coordination check runs a single Sonnet 4.6 call with section headers
    and existing findings to catch inter-spec coordination issues. This is
    controlled by the cross_check parameter (default False). Cross-check
    findings are returned separately in PipelineResult.cross_check_result
    and rendered in their own report section.

v1.5.0 — Confidence scoring, always-on verification, finding deduplication.

    Finding deduplication:
        Per-spec siloed review can produce duplicate findings when the same
        issue appears across multiple specs (e.g., "ASCE 7-16 instead of
        7-22" flagged independently in 5 specs). The _deduplicate_findings()
        post-processing step groups findings by (normalized issue, codeReference)
        and consolidates duplicates into a single representative finding that
        lists all affected files.

    Verification is always enabled (v1.5.0).

v1.4.0 — Per-spec siloed review + batch processing support.
v1.3.0 — project_context parameter plumbed through to user message.
v1.1.0 — All output is in-app. No files emitted.

Design decisions:
    - Hard stop on token limit exceeded (no silent truncation)
    - LEED/placeholder detection is LOCAL, not sent to LLM (saves tokens)
    - Streaming callback enables real-time GUI updates
    - No files are written — everything renders in the UI
    - Per-spec errors are collected, not raised immediately, so partial
      results can still be displayed
    - Deduplication is local (no API call) and runs before verification
      so duplicate findings don't waste verification API calls
    - Cross-check is optional and uses Sonnet 4.6 (cheaper than Opus)
    - Cross-check findings are kept separate from per-spec findings
      for distinct rendering in the report
    - In batch mode, verification also goes through the Batches API
      for 50% cost savings (v1.7.0)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .extractor import extract_text_from_docx, ExtractedSpec
from .preprocessor import preprocess_spec
from .prompts import get_system_prompt
from .tokenizer import RECOMMENDED_MAX, count_tokens
from .reviewer import (
    review_single_spec,
    review_specs,
    ReviewResult,
    Finding,
    MODEL_OPUS_46,
    MODEL_SONNET_46,
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
    """
    Container for all results from a pipeline run.

    No file paths — everything is in-memory for GUI rendering.

    Attributes:
        review_result: Parsed ReviewResult from Claude (None if dry_run)
        files_reviewed: List of filenames that were reviewed
        leed_alerts: List of LEED alert dicts
        placeholder_alerts: List of placeholder alert dicts
        cross_check_result: Optional ReviewResult from cross-spec coordination
            check. None if cross-check was not run or only 1 spec was provided.
    """
    review_result: Optional[ReviewResult]
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    cross_check_result: Optional[ReviewResult] = None


def _get_docx_files(input_dir: Path) -> list[Path]:
    """Get all .docx files from a directory, excluding temp files."""
    return sorted([p for p in input_dir.glob("*.docx") if not p.name.startswith("~$")])


def _combine_specs(specs: list[ExtractedSpec]) -> str:
    """Combine multiple specs into a single string with file delimiters.

    Preserved for potential future use (e.g., cross-spec coordination pass)
    but no longer used in the default review pipeline as of v1.4.0.
    """
    blocks = []
    for s in specs:
        blocks.append(f"===== FILE: {s.filename} =====\n{s.content}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Finding deduplication (v1.5.0)
# ---------------------------------------------------------------------------

def _normalize_issue_text(text: str) -> str:
    """Normalize issue text for deduplication comparison.

    Strips filenames, section references, and extra whitespace so that
    findings describing the same underlying issue across different specs
    can be grouped together.
    """
    # Remove common file references like "23 05 00.docx" or "23 21 13 - Hydronic Piping.docx"
    normalized = re.sub(r"\d{2}\s?\d{2}\s?\d{2}[^.]*\.docx", "", text, flags=re.IGNORECASE)
    # Remove section references like "Part 2, Article 2.3.A" or "Article 1.5.B"
    normalized = re.sub(r"(?:Part\s+\d+,?\s*)?Article\s+[\d.]+[A-Z]*", "", normalized, flags=re.IGNORECASE)
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Consolidate duplicate findings across multiple specs.

    Per-spec siloed review can produce the same finding independently in
    multiple specs (e.g., "ASCE 7-16 instead of 7-22" in 5 specs). This
    function groups findings by their normalized issue text and code reference,
    then consolidates each group into a single representative finding.

    The representative finding:
        - Uses the highest severity from the group
        - Uses the highest confidence from the group
        - Lists all affected filenames in the issue text
        - Keeps the existing/replacement text from the first occurrence
        - Preserves the code reference

    Findings that appear in only one spec are left unchanged.

    Args:
        findings: List of Finding objects from per-spec review

    Returns:
        Deduplicated list of Finding objects (potentially shorter)
    """
    if len(findings) <= 1:
        return findings

    # Build dedup key: (normalized_issue, codeReference or "")
    groups: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        key = (_normalize_issue_text(f.issue), (f.codeReference or "").strip().lower())
        groups.setdefault(key, []).append(f)

    # Severity ordering for picking the highest
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}

    deduplicated: list[Finding] = []
    for key, group in groups.items():
        if len(group) == 1:
            # Unique finding — pass through unchanged
            deduplicated.append(group[0])
            continue

        # Sort by severity (most severe first), then confidence (highest first)
        group.sort(key=lambda f: (severity_rank.get(f.severity, 99), -f.confidence))
        representative = group[0]

        # Collect all unique filenames
        filenames = list(dict.fromkeys(f.fileName for f in group if f.fileName))
        file_list = ", ".join(filenames)

        # Build consolidated issue text
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
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> _PreparedSpecs:
    """Extract, preprocess, and validate specs. Shared by real-time and batch modes.

    Handles:
        1. File discovery / validation
        2. DOCX text extraction
        3. Local LEED/placeholder detection
        4. Per-spec token limit check

    Args:
        input_dir: Directory containing .docx specification files
        files: Optional list of specific files to process
        log: Callback for log messages
        progress: Callback for progress updates

    Returns:
        _PreparedSpecs with extracted specs and alert lists

    Raises:
        FileNotFoundError: If no .docx files found
        ValueError: If any single spec exceeds the token limit
    """
    input_dir = Path(input_dir)

    if files:
        docx_files = [Path(f) for f in files]
    else:
        docx_files = _get_docx_files(input_dir)

    if not docx_files:
        raise FileNotFoundError(f"No .docx files found in: {input_dir}")

    # Extract text from DOCX files
    progress(0.0, "Extracting DOCX text...")
    specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []

    total_files = len(docx_files)
    for i, p in enumerate(docx_files, start=1):
        log(f"Loading: {p.name}")
        spec = extract_text_from_docx(p)
        specs.append(spec)

        pre = preprocess_spec(spec.content, spec.filename)
        leed_alerts.extend(pre.leed_alerts)
        placeholder_alerts.extend(pre.placeholder_alerts)

        progress((i / total_files) * 25.0, f"Loaded {i}/{total_files}")

    # Per-spec token limit check
    progress(30.0, "Checking token limits...")
    system_prompt = get_system_prompt()
    system_prompt_tokens = count_tokens(system_prompt)

    for spec in specs:
        spec_tokens = count_tokens(spec.content)
        estimated_call_tokens = system_prompt_tokens + spec_tokens + 200
        if estimated_call_tokens > RECOMMENDED_MAX:
            raise ValueError(
                f"Spec '{spec.filename}' is too large for a single API call: "
                f"~{estimated_call_tokens:,} tokens (limit: {RECOMMENDED_MAX:,}). "
                "This spec would need to be split to review."
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
    """Returned by start_batch_review() — holds everything the GUI needs to
    poll and eventually collect results.

    Attributes:
        job: BatchJob from the Anthropic Batches API
        files_reviewed: List of filenames submitted for review
        leed_alerts: Locally detected LEED alerts
        placeholder_alerts: Locally detected placeholder alerts
        model: Model ID used for the review batch
    """
    job: BatchJob
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    model: str = MODEL_OPUS_46


def start_batch_review(
    *,
    input_dir: Path,
    files: Optional[list[Path]] = None,
    project_context: str = "",
    model: str = MODEL_OPUS_46,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> BatchSubmission:
    """Extract specs and submit them as a Message Batch (50% cost savings).

    This function runs extraction, preprocessing, and token checking
    synchronously, then submits the batch and returns immediately.
    The GUI is responsible for polling via poll_batch() and collecting
    results via collect_batch_results() when the batch completes.

    Args:
        input_dir: Directory containing .docx specification files
        files: Optional list of specific files to process
        project_context: Optional project description for each review
        model: Model ID for review (default: Claude Opus 4.6)
        log: Callback for log messages
        progress: Callback for progress updates

    Returns:
        BatchSubmission with the BatchJob and preprocessor alerts

    Raises:
        FileNotFoundError: If no .docx files found
        ValueError: If any spec exceeds the token limit
    """
    prepared = _prepare_specs(
        input_dir=input_dir,
        files=files,
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

    return BatchSubmission(
        job=job,
        files_reviewed=[s.filename for s in prepared.specs],
        leed_alerts=prepared.leed_alerts,
        placeholder_alerts=prepared.placeholder_alerts,
        model=model,
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
    """Retrieve and aggregate results from a completed batch.

    Call this after poll_batch() shows status == "ended".

    v1.7.0: Verification now routes through the Batches API for 50% cost
    savings. Uses verify_findings_batch() instead of sequential verify_findings().

    Args:
        submission: BatchSubmission returned by start_batch_review()
        verify: If True, run web search verification on eligible findings
        cross_check: If True, run cross-spec coordination check after review
        specs: ExtractedSpec objects (needed for cross-check). If None and
            cross_check is True, cross-check is skipped.
        project_context: Project description for cross-check prompt
        log: Callback for log messages
        progress: Callback for progress updates

    Returns:
        PipelineResult with aggregated findings, same shape as run_review()
    """
    model = getattr(submission, "model", MODEL_OPUS_46)
    results_by_file = retrieve_review_results(submission.job, model=model)

    all_findings: list[Finding] = []
    all_thinking: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    errors: list[str] = []

    for filename in submission.files_reviewed:
        result = results_by_file.get(filename)
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

    # Deduplication (before cross-check and verification)
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
        elapsed_seconds=0.0,
    )

    if errors:
        combined_result.thinking += (
            f"\n\n--- Batch Errors ---\n"
            f"The following specs had errors:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    # Cross-spec coordination check (optional, v1.6.0)
    cross_check_result = None
    if cross_check and specs and len(specs) >= 2:
        progress(55.0, "Running cross-spec coordination check...")
        log(f"Running cross-spec coordination check across {len(specs)} specs...")

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

    # Verification — batch mode uses batched verification (v1.7.0)
    # Verify both per-spec findings and cross-check findings
    all_verifiable = list(all_findings)
    if cross_check_result and cross_check_result.findings:
        all_verifiable.extend(cross_check_result.findings)

    if verify and all_verifiable:
        verifiable_count = sum(
            1 for f in all_verifiable
            if f.severity != "GRIPES"
        )
        if verifiable_count > 0:
            progress(60.0, f"Submitting {verifiable_count} findings for batch verification...")
            log(f"Submitting {verifiable_count} findings for batch verification (50% savings)...")

            def _batch_verify_progress(pct: float, msg: str):
                # Map verification's 0-100% into 60-95% of overall progress
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
    """
    Execute the full specification review pipeline.

    v1.7.0: Adds model parameter for review model selection (Opus or Sonnet).

    v1.6.0: Adds optional cross-spec coordination pass after per-spec review
    and deduplication, before verification. Controlled by cross_check param.

    v1.5.0: Adds finding deduplication after per-spec review. Verification
    is always enabled (verify parameter kept for API compatibility but
    defaults to True).

    v1.4.0: Reviews each spec independently via review_single_spec() instead
    of combining all specs into one API call.

    Args:
        input_dir: Directory containing .docx specification files
        files: Optional list of specific files to process. If None, all .docx
               files in input_dir are processed.
        project_context: Optional free-text project description. If non-empty,
            included in each per-spec user message as a <project_context> block.
        model: Model ID for review (default: Claude Opus 4.6). Also supports
            Claude Sonnet 4.6 for faster/cheaper reviews.
        verify: If True, run web search verification on eligible findings
            after review. Defaults to True (always-on in v1.5.0).
        cross_check: If True, run cross-spec coordination check after per-spec
            review and deduplication. Requires 2+ specs. Defaults to False.
        dry_run: If True, skip the API call.
        verbose: Passed to reviewer for additional stdout logging
        log: Callback for log messages.
        progress: Callback for progress updates (percent, message).
        stream_callback: Optional callback for real-time streaming chunks.

    Returns:
        PipelineResult with review data for GUI rendering

    Raises:
        FileNotFoundError: If no .docx files found
        ValueError: If any single spec exceeds the token limit
        RuntimeError: If all API calls fail
    """
    start_time = time.time()
    input_dir = Path(input_dir)

    # -------------------------------------------------------------------------
    # Stages 1-2: Extract, preprocess, validate (shared with batch mode)
    # -------------------------------------------------------------------------
    prepared = _prepare_specs(
        input_dir=input_dir,
        files=files,
        log=log,
        progress=progress,
    )
    specs = prepared.specs
    leed_alerts = prepared.leed_alerts
    placeholder_alerts = prepared.placeholder_alerts
    total_files = len(specs)

    # -------------------------------------------------------------------------
    # Stage 3: Dry run exit point
    # -------------------------------------------------------------------------
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

    # -------------------------------------------------------------------------
    # Stage 4: Per-spec siloed review
    # -------------------------------------------------------------------------
    all_findings: list[Finding] = []
    all_thinking: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    errors: list[str] = []

    # Progress allocation:
    #   review 35-55%, dedup ~55%, cross-check 55-65%, verify 65-95%
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

    # -------------------------------------------------------------------------
    # Stage 4.5: Finding deduplication (before cross-check and verification)
    # -------------------------------------------------------------------------
    pre_dedup_count = len(all_findings)
    all_findings = _deduplicate_findings(all_findings)
    post_dedup_count = len(all_findings)
    if pre_dedup_count != post_dedup_count:
        log(f"Deduplicated: {pre_dedup_count} findings → {post_dedup_count} unique findings")
    progress(review_end_pct, f"Review complete — {post_dedup_count} unique findings")

    # -------------------------------------------------------------------------
    # Stage 5: Cross-spec coordination check (optional, v1.6.0)
    # -------------------------------------------------------------------------
    cross_check_result = None
    if cross_check and len(specs) >= 2:
        cross_check_start_pct = 56.0
        cross_check_end_pct = 65.0
        progress(cross_check_start_pct, f"Running cross-spec coordination check across {len(specs)} specs...")
        log(f"Running cross-spec coordination check across {len(specs)} specs (Sonnet 4.6)...")

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

    # -------------------------------------------------------------------------
    # Stage 6: Web search verification (always enabled in v1.5.0)
    # -------------------------------------------------------------------------
    # Verify both per-spec findings and cross-check findings
    all_verifiable = list(all_findings)
    if cross_check_result and cross_check_result.findings:
        all_verifiable.extend(cross_check_result.findings)

    verify_start_pct = 66.0

    if verify and all_verifiable:
        verifiable_count = sum(
            1 for f in all_verifiable
            if f.severity != "GRIPES"
        )
        if verifiable_count > 0:
            progress(verify_start_pct, f"Verifying {verifiable_count} findings via web search...")
            log(f"Verifying {verifiable_count} findings with Sonnet + web search...")

            def _verify_progress(current: int, total: int, filename: str):
                verify_pct = verify_start_pct + (current / total) * (95.0 - verify_start_pct)
                progress(verify_pct, f"Verifying finding {current}/{total} ({filename})...")

            verify_findings(all_verifiable, progress=_verify_progress)

            # Count verification verdicts
            verdicts = {}
            for f in all_verifiable:
                if f.verification:
                    v = f.verification.verdict
                    verdicts[v] = verdicts.get(v, 0) + 1

            verdict_summary = ", ".join(f"{v}: {c}" for v, c in sorted(verdicts.items()))
            log(f"Verification complete: {verdict_summary}")
        else:
            log("No findings eligible for verification (no code references).")

    # -------------------------------------------------------------------------
    # Stage 7: Aggregate results
    # -------------------------------------------------------------------------
    elapsed = time.time() - start_time

    # If ALL specs failed, raise an error
    if errors and not all_findings and not all_thinking:
        raise RuntimeError(
            f"All {len(errors)} spec reviews failed:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    # Combine per-spec results into a single ReviewResult for the GUI
    combined_result = ReviewResult(
        findings=all_findings,
        raw_response="",
        thinking="\n\n".join(all_thinking),
        model=model,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        elapsed_seconds=elapsed,
    )

    # If some (but not all) specs had errors, note it in the thinking
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