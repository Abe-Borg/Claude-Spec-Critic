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
from .reviewer import review_specs, ReviewResult, MODEL_OPUS_45
from .report import generate_report


LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]  # percent (0-100), message


def _noop_log(_: str) -> None:
    return


def _noop_progress(_: float, __: str) -> None:
    return


@dataclass
class PipelineOutputs:
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
    Convert alert dicts from preprocessor schema to the report schema expected by report.py:
    {filename, line, text}
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
    return sorted([p for p in input_dir.glob("*.docx") if not p.name.startswith("~$")])


def _create_run_dir(output_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = output_dir / f"review_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _combine_specs(specs: list[ExtractedSpec]) -> str:
    # Prompts.py expects this exact delimiter style:
    blocks = []
    for s in specs:
        blocks.append(f"===== FILE: {s.filename} =====\n{s.content}")
    return "\n\n".join(blocks)


def run_review(
    *,
    input_dir: Path,
    output_dir: Path,
    dry_run: bool = False,
    verbose: bool = False,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
) -> PipelineOutputs:
    """
    Single source of truth for the whole workflow.
    CLI and GUI both call this.
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    run_dir = _create_run_dir(output_dir)

    docx_files = _get_docx_files(input_dir)
    if not docx_files:
        raise FileNotFoundError(f"No .docx files found in: {input_dir}")

    progress(0.0, "Extracting DOCX text...")
    specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []

    total = len(docx_files)
    for i, p in enumerate(docx_files, start=1):
        log(f"Loading: {p.name}")
        spec = extract_text_from_docx(p)
        specs.append(spec)

        pre = preprocess_spec(spec.content, spec.filename)
        leed_alerts.extend(pre.leed_alerts)
        placeholder_alerts.extend(pre.placeholder_alerts)

        progress((i / total) * 35.0, f"Loaded {i}/{total}")

    system_prompt = get_system_prompt()
    spec_contents = [(s.filename, s.content) for s in specs]

    progress(40.0, "Analyzing tokens...")
    token_summary = analyze_token_usage(spec_contents, system_prompt=system_prompt)

    token_summary_json = run_dir / "token_summary.json"
    token_summary_json.write_text(
        json.dumps(
            {
                "model": MODEL_OPUS_45,
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

    # Always write combined inputs snapshot (useful for reproducing runs)
    progress(45.0, "Preparing combined input...")
    combined = _combine_specs(specs)
    inputs_combined_txt = run_dir / "inputs_combined.txt"
    inputs_combined_txt.write_text(combined, encoding="utf-8")

    if not token_summary.within_limit:
        # Hard stop: both CLI and GUI should behave the same
        raise ValueError(
            f"Token limit exceeded: {token_summary.total_tokens:,} > {RECOMMENDED_MAX:,}. "
            "Split the input specs and re-run."
        )

    if dry_run:
        log("Dry-run enabled: skipping API call.")
        # Still generate a report with 0 findings so you get the artifact structure
        dummy = ReviewResult(findings=[], raw_response="", model=MODEL_OPUS_45)
        report_docx = run_dir / "report.docx"
        generate_report(
            review_result=dummy,
            files_reviewed=[s.filename for s in specs],
            leed_alerts=_normalize_alerts(leed_alerts),
            placeholder_alerts=_normalize_alerts(placeholder_alerts),
            output_path=report_docx,
        )

        findings_json = run_dir / "findings.json"
        findings_json.write_text(
            json.dumps(
                {
                    "meta": {"model": MODEL_OPUS_45, "dry_run": True},
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

    progress(55.0, "Calling Opus 4.5...")
    review_result = review_specs(
        combined_content=combined,
        verbose=verbose,
    )

    raw_response_txt = run_dir / "raw_response.txt"
    raw_response_txt.write_text(review_result.raw_response or "", encoding="utf-8")

    if review_result.error:
        err_path = run_dir / "error.txt"
        err_path.write_text(review_result.error, encoding="utf-8")
        raise RuntimeError(review_result.error)

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
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    progress(85.0, "Generating report.docx...")
    report_docx = run_dir / "report.docx"
    generate_report(
        review_result=review_result,
        files_reviewed=[s.filename for s in specs],
        leed_alerts=leed_alerts,
        placeholder_alerts=placeholder_alerts,
        output_path=report_docx,
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
