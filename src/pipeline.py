"""
Core orchestration pipeline for Spec Critic.

This module is the SINGLE SOURCE OF TRUTH for the review workflow.
The GUI calls run_review() and receives a PipelineResult containing
all data needed to render the in-app report.

v1.4.0 — Per-spec siloed review: each spec gets its own API call via
    review_single_spec() instead of combining all specs into one giant
    context. Benefits:
        - Each spec gets the model's full attention (no dilution)
        - Avoids the 150k token limit bottleneck for large projects
        - Enables per-spec progress tracking in the GUI
        - Foundation for batch processing (Phase 2)
    The combined-spec path (review_specs) is preserved for potential
    future use but is no longer the default.

v1.3.0 — project_context parameter plumbed through to user message.
v1.1.0 — All output is in-app. No files emitted.

Pipeline stages:
    1. Extract text from .docx files (extractor.py)
    2. Detect LEED references and placeholders locally (preprocessor.py)
    3. Per-spec token limit check (each spec + system prompt must fit)
    4. Review each spec individually via streaming API (reviewer.py)
    5. Aggregate findings, thinking, and token counts
    6. Return PipelineResult to caller

Design decisions:
    - Hard stop on token limit exceeded (no silent truncation)
    - LEED/placeholder detection is LOCAL, not sent to LLM (saves tokens)
    - Streaming callback enables real-time GUI updates
    - No files are written — everything renders in the UI
    - Per-spec errors are collected, not raised immediately, so partial
      results can still be displayed
"""

from __future__ import annotations

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
    StreamCallback,
)

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
    """
    review_result: Optional[ReviewResult]
    files_reviewed: list[str] = field(default_factory=list)
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)


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


def run_review(
    *,
    input_dir: Path,
    files: Optional[list[Path]] = None,
    project_context: str = "",
    dry_run: bool = False,
    verbose: bool = False,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    stream_callback: Optional[StreamCallback] = None,
) -> PipelineResult:
    """
    Execute the full specification review pipeline.

    v1.4.0: Reviews each spec independently via review_single_spec() instead
    of combining all specs into one API call. Findings, thinking, and token
    counts are aggregated across all per-spec results.

    Args:
        input_dir: Directory containing .docx specification files
        files: Optional list of specific files to process. If None, all .docx
               files in input_dir are processed.
        project_context: Optional free-text project description. If non-empty,
            included in each per-spec user message as a <project_context> block.
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

    # Use provided files list, or scan directory
    if files:
        docx_files = [Path(f) for f in files]
    else:
        docx_files = _get_docx_files(input_dir)

    if not docx_files:
        raise FileNotFoundError(f"No .docx files found in: {input_dir}")

    # -------------------------------------------------------------------------
    # Stage 1: Extract text from DOCX files
    # -------------------------------------------------------------------------
    progress(0.0, "Extracting DOCX text...")
    specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []

    total_files = len(docx_files)
    for i, p in enumerate(docx_files, start=1):
        log(f"Loading: {p.name}")
        spec = extract_text_from_docx(p)
        specs.append(spec)

        # Local detection (not sent to LLM)
        pre = preprocess_spec(spec.content, spec.filename)
        leed_alerts.extend(pre.leed_alerts)
        placeholder_alerts.extend(pre.placeholder_alerts)

        progress((i / total_files) * 25.0, f"Loaded {i}/{total_files}")

    # -------------------------------------------------------------------------
    # Stage 2: Per-spec token limit check
    # -------------------------------------------------------------------------
    progress(30.0, "Checking token limits...")
    system_prompt = get_system_prompt()
    system_prompt_tokens = count_tokens(system_prompt)

    for spec in specs:
        spec_tokens = count_tokens(spec.content)
        # Each per-spec call includes: system prompt + user message wrapper + spec content
        # The user message wrapper adds ~200 tokens of boilerplate around the spec content
        estimated_call_tokens = system_prompt_tokens + spec_tokens + 200
        if estimated_call_tokens > RECOMMENDED_MAX:
            raise ValueError(
                f"Spec '{spec.filename}' is too large for a single API call: "
                f"~{estimated_call_tokens:,} tokens (limit: {RECOMMENDED_MAX:,}). "
                "This spec would need to be split to review."
            )

    # -------------------------------------------------------------------------
    # Stage 3: Dry run exit point
    # -------------------------------------------------------------------------
    if dry_run:
        log("Dry-run enabled: skipping API calls.")
        dummy = ReviewResult(findings=[], raw_response="", model=MODEL_OPUS_46)
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

    for i, spec in enumerate(specs):
        spec_num = i + 1
        progress_base = 35.0
        progress_range = 60.0  # 35% to 95% of the bar is for reviews
        spec_progress = progress_base + (i / total_files) * progress_range

        progress(spec_progress, f"Reviewing {spec.filename} ({spec_num}/{total_files})...")
        log(f"Reviewing: {spec.filename} ({spec_num}/{total_files})")

        result = review_single_spec(
            spec_content=spec.content,
            filename=spec.filename,
            project_context=project_context,
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
    # Stage 5: Aggregate results
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
        raw_response="",  # No single raw response in per-spec mode
        thinking="\n\n".join(all_thinking),
        model=MODEL_OPUS_46,
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
    )