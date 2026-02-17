"""
Core orchestration pipeline for Spec Critic.

This module is the SINGLE SOURCE OF TRUTH for the review workflow.
The GUI calls run_review() and receives a PipelineResult containing
all data needed to render the in-app report.

v1.3.0 — project_context parameter plumbed through to user message.
v1.1.0 — All output is in-app. No files emitted.

Pipeline stages:
    1. Extract text from .docx files (extractor.py)
    2. Detect LEED references and placeholders locally (preprocessor.py)
    3. Combine specs with file delimiters for the LLM
    4. Call Claude Opus 4.6 via streaming API (reviewer.py)
    5. Parse JSON findings from response
    6. Return PipelineResult to caller

Design decisions:
    - Hard stop on token limit exceeded (no silent truncation)
    - LEED/placeholder detection is LOCAL, not sent to LLM (saves tokens)
    - Streaming callback enables real-time GUI updates
    - No files are written — everything renders in the UI
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .extractor import extract_text_from_docx, ExtractedSpec
from .preprocessor import preprocess_spec
from .prompts import get_system_prompt
from .tokenizer import RECOMMENDED_MAX
from .reviewer import review_specs, ReviewResult, MODEL_OPUS_46, StreamCallback

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
    """Combine multiple specs into a single string with file delimiters."""
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
    
    Args:
        input_dir: Directory containing .docx specification files
        files: Optional list of specific files to process. If None, all .docx
               files in input_dir are processed.
        project_context: Optional free-text project description. If non-empty,
            included in the user message as a <project_context> XML block.
        dry_run: If True, skip the API call.
        verbose: Passed to reviewer for additional stdout logging
        log: Callback for log messages.
        progress: Callback for progress updates (percent, message).
        stream_callback: Optional callback for real-time streaming chunks.
    
    Returns:
        PipelineResult with review data for GUI rendering
        
    Raises:
        FileNotFoundError: If no .docx files found
        ValueError: If total tokens exceed RECOMMENDED_MAX (150k)
        RuntimeError: If API call fails after retries
    """
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

    total = len(docx_files)
    for i, p in enumerate(docx_files, start=1):
        log(f"Loading: {p.name}")
        spec = extract_text_from_docx(p)
        specs.append(spec)

        # Local detection (not sent to LLM)
        pre = preprocess_spec(spec.content, spec.filename)
        leed_alerts.extend(pre.leed_alerts)
        placeholder_alerts.extend(pre.placeholder_alerts)

        progress((i / total) * 35.0, f"Loaded {i}/{total}")

    # -------------------------------------------------------------------------
    # Stage 2: Combine specs and enforce token limit
    # -------------------------------------------------------------------------
    progress(45.0, "Preparing combined input...")
    combined = _combine_specs(specs)

    # Token limit is enforced by the GUI before we get here, but double-check
    from .tokenizer import analyze_token_usage
    system_prompt = get_system_prompt()
    spec_contents = [(s.filename, s.content) for s in specs]
    token_summary = analyze_token_usage(spec_contents, system_prompt=system_prompt)

    if not token_summary.within_limit:
        raise ValueError(
            f"Token limit exceeded: {token_summary.total_tokens:,} > {RECOMMENDED_MAX:,}. "
            "Split the input specs and re-run."
        )

    # -------------------------------------------------------------------------
    # Stage 3: Dry run exit point
    # -------------------------------------------------------------------------
    if dry_run:
        log("Dry-run enabled: skipping API call.")
        dummy = ReviewResult(findings=[], raw_response="", model=MODEL_OPUS_46)
        progress(100.0, "Dry run complete.")
        return PipelineResult(
            review_result=dummy,
            files_reviewed=[s.filename for s in specs],
            leed_alerts=leed_alerts,
            placeholder_alerts=placeholder_alerts,
        )

    # -------------------------------------------------------------------------
    # Stage 4: API call with streaming
    # -------------------------------------------------------------------------
    progress(55.0, "Calling Opus 4.6...")
    review_result = review_specs(
        combined_content=combined,
        file_count=len(specs),
        project_context=project_context,
        verbose=verbose,
        stream_callback=stream_callback,
    )

    if review_result.error:
        raise RuntimeError(review_result.error)

    progress(100.0, "Done.")
    return PipelineResult(
        review_result=review_result,
        files_reviewed=[s.filename for s in specs],
        leed_alerts=leed_alerts,
        placeholder_alerts=placeholder_alerts,
    )