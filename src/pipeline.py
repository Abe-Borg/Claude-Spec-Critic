"""
Core orchestration pipeline for MEP Spec Review.

This module is the SINGLE SOURCE OF TRUTH for the review workflow. Both
the CLI (cli.py) and GUI (gui.py) call run_review() and receive identical
behavior. This design ensures consistency and makes testing straightforward.

Pipeline stages:
    1. Create timestamped output directory
    2. Extract text from .docx files (extractor.py)
    3. Detect LEED references and placeholders locally (preprocessor.py)
    4. Analyze token usage and enforce limits (tokenizer.py)
    5. Combine specs with file delimiters for the LLM
    6. Call Claude Opus 4.6 via streaming API (reviewer.py)
    7. Parse JSON findings from response
    8. Generate Word report (report.py)
    9. Write all artifacts to output directory

Output artifacts (per run):
    review_YYYY-MM-DD_HHMMSS/
    ├── report.docx          # Human-readable findings
    ├── findings.json        # Machine-readable findings + metadata
    ├── raw_response.txt     # Raw Claude response (debugging)
    ├── inputs_combined.txt  # Exact text sent to API (reproducibility)
    ├── token_summary.json   # Token breakdown by file
    └── error.txt            # Only if run failed

Design decisions:
    - Hard stop on token limit exceeded (no silent truncation)
    - LEED/placeholder detection is LOCAL, not sent to LLM (saves tokens)
    - Streaming callback enables real-time GUI updates
    - dry_run mode skips API call but generates all other artifacts

Usage:
    from pipeline import run_review, PipelineOutputs
    
    result = run_review(
        input_dir=Path("./specs"),
        output_dir=Path("./output"),
        dry_run=False,
        verbose=True,
        log=print,
        progress=lambda pct, msg: print(f"{pct}% {msg}"),
        stream_callback=lambda chunk: print(chunk, end=""),
    )
    print(f"Report: {result.report_docx}")
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .extractor import extract_text_from_docx, ExtractedSpec
from .preprocessor import preprocess_spec
from .prompts import get_system_prompt
from .tokenizer import analyze_token_usage, RECOMMENDED_MAX
from .reviewer import review_specs, ReviewResult, MODEL_OPUS_46, StreamCallback
from .report import generate_report

# Type aliases for callback signatures
LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]  # percent (0-100), message


def _noop_log(_: str) -> None:
    return


def _noop_progress(_: float, __: str) -> None:
    return


@dataclass
class PipelineOutputs:
    """
    Container for all paths and results from a pipeline run.
    
    Attributes:
        run_dir: Timestamped directory containing all outputs
        report_docx: Path to Word report with findings
        findings_json: Path to JSON file with findings + metadata
        raw_response_txt: Path to raw Claude response text
        inputs_combined_txt: Path to combined spec text sent to API
        token_summary_json: Path to token usage breakdown
        review_result: Parsed ReviewResult (None if dry_run)
        leed_alert_count: Number of LEED references detected locally
        placeholder_alert_count: Number of placeholders detected locally
    """
    run_dir: Path
    report_docx: Path
    findings_json: Path
    raw_response_txt: Path
    inputs_combined_txt: Path
    token_summary_json: Path
    review_result: Optional[ReviewResult]
    leed_alert_count: int
    placeholder_alert_count: int


def _normalize_alerts(alerts: list[dict]) -> list[dict]:
    """
    Convert alert dicts from preprocessor schema to report schema.
    
    The preprocessor returns alerts with 'position' and 'context' keys,
    but report.py expects 'line' and 'text' keys.
    
    Args:
        alerts: List of alert dicts from preprocessor
        
    Returns:
        List of alert dicts with {filename, line, text} structure
    """
    out: list[dict] = []
    for a in alerts:
        out.append({
            "filename": a.get("filename", ""),
            "line": a.get("line", a.get("position", "")),  # fallback to char position
            "text": a.get("text", a.get("context", a.get("match", ""))),  # readable snippet
        })
    return out



def _get_docx_files(input_dir: Path) -> list[Path]:
    """
    Get all .docx files from a directory, excluding temp files.
    
    Word creates temp files starting with ~$ when documents are open.
    These are filtered out to avoid processing partial/locked files.
    
    Args:
        input_dir: Directory to scan
        
    Returns:
        Sorted list of .docx file paths
    """
    return sorted([p for p in input_dir.glob("*.docx") if not p.name.startswith("~$")])


def _create_run_dir(output_dir: Path) -> Path:
    """
    Create a timestamped directory for this run's outputs.
    
    Format: review_YYYY-MM-DD_HHMMSS
    
    Args:
        output_dir: Parent directory for outputs
        
    Returns:
        Path to created run directory
    """
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = output_dir / f"review_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _combine_specs(specs: list[ExtractedSpec]) -> str:
    """
    Combine multiple specs into a single string with file delimiters.
    
    The delimiter format matches what prompts.py tells Claude to expect:
        ===== FILE: <filename> =====
    
    Args:
        specs: List of extracted specifications
        
    Returns:
        Combined string with all specs separated by file headers
    """
    blocks = []
    for s in specs:
        blocks.append(f"===== FILE: {s.filename} =====\n{s.content}")
    return "\n\n".join(blocks)


def run_review(
    *,
    input_dir: Path,
    output_dir: Path,
    files: Optional[list[Path]] = None,
    dry_run: bool = False,
    verbose: bool = False,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    stream_callback: Optional[StreamCallback] = None,
) -> PipelineOutputs:
    """
    Execute the full specification review pipeline.
    
    This is the main entry point called by both CLI and GUI. All workflow
    logic is here — callers just provide callbacks for logging and progress.
    
    Args:
        input_dir: Directory containing .docx specification files
        output_dir: Parent directory for output folder (timestamped subfolder created)
        files: Optional list of specific files to process. If None, all .docx
               files in input_dir are processed.
        dry_run: If True, skip the API call. Useful for testing extraction
                 and token counting without spending API credits.
        verbose: Passed to reviewer for additional stdout logging
        log: Callback for log messages. Called with single string argument.
        progress: Callback for progress updates. Called with (percent, message).
                  Percent ranges from 0.0 to 100.0.
        stream_callback: Optional callback for real-time streaming. Called with
                         each text chunk as Claude generates it. Enables live
                         display in GUI.
    
    Returns:
        PipelineOutputs with paths to all generated files and parsed results
        
    Raises:
        FileNotFoundError: If input_dir has no .docx files
        ValueError: If total tokens exceed RECOMMENDED_MAX (150k)
        RuntimeError: If API call fails after retries
        
    Example:
        >>> result = run_review(
        ...     input_dir=Path("./specs"),
        ...     output_dir=Path("./output"),
        ...     log=lambda msg: print(f"[LOG] {msg}"),
        ... )
        >>> print(f"Found {result.review_result.total_count} issues")
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    run_dir = _create_run_dir(output_dir)

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
    # Stage 2: Token analysis and limit enforcement
    # -------------------------------------------------------------------------
    system_prompt = get_system_prompt()
    spec_contents = [(s.filename, s.content) for s in specs]

    progress(40.0, "Analyzing tokens...")
    token_summary = analyze_token_usage(spec_contents, system_prompt=system_prompt)

    token_summary_json = run_dir / "token_summary.json"
    token_summary_json.write_text(
        json.dumps(
            {
                "model": MODEL_OPUS_46,
                "recommended_max_tokens": RECOMMENDED_MAX,
                "within_limit": token_summary.within_limit,
                "total_tokens": token_summary.total_tokens,
                "system_prompt_tokens": token_summary.system_prompt_tokens,
                "items": [
                    {"name": t.name, "tokens": t.tokens, "chars": t.chars}
                    for t in token_summary.items
                ],
                "warning_message": token_summary.warning_message,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


    # -------------------------------------------------------------------------
    # Stage 3: Combine specs for API call
    # -------------------------------------------------------------------------
    # Always write combined inputs snapshot (useful for reproducing runs)
    progress(45.0, "Preparing combined input...")
    combined = _combine_specs(specs)
    inputs_combined_txt = run_dir / "inputs_combined.txt"
    inputs_combined_txt.write_text(combined, encoding="utf-8")

    # Hard stop if over limit — both CLI and GUI behave identically
    if not token_summary.within_limit:
        raise ValueError(
            f"Token limit exceeded: {token_summary.total_tokens:,} > {RECOMMENDED_MAX:,}. "
            "Split the input specs and re-run."
        )


    # -------------------------------------------------------------------------
    # Stage 4: Dry run exit point
    # -------------------------------------------------------------------------
    if dry_run:
        log("Dry-run enabled: skipping API call.")
        # Still generate a report with 0 findings so you get the artifact structure
        dummy = ReviewResult(findings=[], raw_response="", model=MODEL_OPUS_46)
        report_docx = run_dir / "report.docx"

        generate_report(
            review_result=dummy,
            files_reviewed=[s.filename for s in specs],
            leed_alerts=_normalize_alerts(leed_alerts),
            placeholder_alerts=_normalize_alerts(placeholder_alerts),
            output_path=report_docx,
            analysis_summary=None,
        )

        findings_json = run_dir / "findings.json"
        findings_json.write_text(
            json.dumps(
                {
                    "meta": {"model": MODEL_OPUS_46, "dry_run": True},
                    "findings": [],
                    "alerts": {
                        "leed_alerts": leed_alerts,
                        "placeholder_alerts": placeholder_alerts,
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        raw_response_txt = run_dir / "raw_response.txt"
        raw_response_txt.write_text("", encoding="utf-8")

        progress(100.0, "Dry run complete.")
        return PipelineOutputs(
            run_dir=run_dir,
            report_docx=report_docx,
            findings_json=findings_json,
            raw_response_txt=raw_response_txt,
            inputs_combined_txt=inputs_combined_txt,
            token_summary_json=token_summary_json,
            review_result=None,
            leed_alert_count=len(leed_alerts),
            placeholder_alert_count=len(placeholder_alerts),
        )


    # -------------------------------------------------------------------------
    # Stage 5: API call with streaming
    # -------------------------------------------------------------------------
    progress(55.0, "Calling Opus 4.6...")
    review_result = review_specs(
        combined_content=combined,
        verbose=verbose,
        stream_callback=stream_callback,
    )

    raw_response_txt = run_dir / "raw_response.txt"
    raw_response_txt.write_text(review_result.raw_response or "", encoding="utf-8")

    if review_result.error:
        err_path = run_dir / "error.txt"
        err_path.write_text(review_result.error, encoding="utf-8")
        raise RuntimeError(review_result.error)

    # -------------------------------------------------------------------------
    # Stage 6: Write findings JSON
    # -------------------------------------------------------------------------
    findings_json = run_dir / "findings.json"
    findings_json.write_text(
        json.dumps(
            {
                "meta": {
                    "model": review_result.model,
                    "input_tokens": review_result.input_tokens,
                    "output_tokens": review_result.output_tokens,
                    "elapsed_seconds": review_result.elapsed_seconds,
                },
                "findings": [f.__dict__ for f in review_result.findings],
                "alerts": {
                    "leed_alerts": leed_alerts,
                    "placeholder_alerts": placeholder_alerts,
                },
                "analysis_summary": review_result.thinking,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


    # -------------------------------------------------------------------------
    # Stage 7: Generate Word report
    # -------------------------------------------------------------------------
    progress(85.0, "Generating report.docx...")
    report_docx = run_dir / "report.docx"
    
    generate_report(
        review_result=review_result,
        files_reviewed=[s.filename for s in specs],
        leed_alerts=_normalize_alerts(leed_alerts),
        placeholder_alerts=_normalize_alerts(placeholder_alerts),
        output_path=report_docx,
        analysis_summary=review_result.thinking,
    )

    progress(100.0, "Done.")
    return PipelineOutputs(
        run_dir=run_dir,
        report_docx=report_docx,
        findings_json=findings_json,
        raw_response_txt=raw_response_txt,
        inputs_combined_txt=inputs_combined_txt,
        token_summary_json=token_summary_json,
        review_result=review_result,
        leed_alert_count=len(leed_alerts),
        placeholder_alert_count=len(placeholder_alerts),
    )