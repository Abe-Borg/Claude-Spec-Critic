"""CLI entry point for spec-review tool."""
from __future__ import annotations

import sys
import json
from pathlib import Path
from datetime import datetime

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from . import __version__
from .extractor import extract_text_from_docx, ExtractedSpec
from .preprocessor import preprocess_spec
from .tokenizer import analyze_token_usage, format_token_summary, RECOMMENDED_MAX
from .reviewer import review_specs, MODEL_OPUS_45
from .report import generate_report_docx


console = Console()


def print_header() -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]MEP SPEC REVIEW[/bold cyan]  [dim]v{__version__}[/dim]\n"
            f"[dim]Model: {MODEL_OPUS_45} (single-model)[/dim]",
            border_style="cyan",
        )
    )


def get_docx_files_from_directory(input_dir: Path) -> list[Path]:
    return sorted(
        [p for p in input_dir.glob("*.docx") if not p.name.startswith("~$")]
    )


def create_run_directory(output_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = output_dir / f"review_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


@click.group()
@click.version_option(version=__version__)
def main():
    """MEP Specification Review Tool."""
    pass


@main.command()
@click.option("--input-dir", "-i", type=click.Path(exists=True), required=True,
              help="Directory containing .docx specification files")
@click.option("--output-dir", "-o", type=click.Path(), default="./output",
              help="Output directory for review results")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed processing information")
@click.option("--dry-run", is_flag=True, help="Process files but do not call API")
def review(input_dir: str, output_dir: str, verbose: bool, dry_run: bool) -> None:
    """
    Review specification .docx files in a directory using Opus 4.5 only.
    """
    print_header()

    input_path = Path(input_dir)
    output_path = Path(output_dir)

    docx_files = get_docx_files_from_directory(input_path)
    if not docx_files:
        console.print(f"[red]Error: No .docx files found in {input_path}[/red]")
        sys.exit(1)

    run_dir = create_run_directory(output_path)
    console.print(f"[dim]Output: {run_dir}[/dim]\n")

    # Extract + preprocess (detection-only)
    extracted_specs: list[ExtractedSpec] = []
    leed_alerts: list[dict] = []
    placeholder_alerts: list[dict] = []

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console) as progress:
        task = progress.add_task("Extracting text from .docx files...", total=len(docx_files))
        for docx_path in docx_files:
            spec = extract_text_from_docx(docx_path)
            extracted_specs.append(spec)

            pre = preprocess_spec(spec.content, spec.filename)
            leed_alerts.extend(pre.leed_alerts)
            placeholder_alerts.extend(pre.placeholder_alerts)

            progress.advance(task)

    console.print(f"[green]Loaded {len(extracted_specs)} file(s)[/green]")

    # Token analysis on RAW extracted content (since we no longer "clean" here)
    spec_contents_for_tokens = [(s.filename, s.content) for s in extracted_specs]
    token_report = analyze_token_usage(spec_contents_for_tokens)

    console.print("\n[bold]Token Analysis:[/bold]")
    console.print(format_token_summary(token_report))

    if token_report.total_tokens > RECOMMENDED_MAX:
        console.print(
            f"\n[yellow]Warning:[/yellow] Estimated tokens exceed recommended max "
            f"({token_report.total_tokens:,} > {RECOMMENDED_MAX:,}). Consider splitting input."
        )

    # Alerts summary
    if leed_alerts or placeholder_alerts:
        console.print("\n[bold yellow]Alerts (not sent as findings targets):[/bold yellow]")
        alert_table = Table(show_header=True, header_style="bold")
        alert_table.add_column("Type")
        alert_table.add_column("Count", justify="right")
        alert_table.add_row("LEED references", str(len(leed_alerts)))
        alert_table.add_row("Placeholders", str(len(placeholder_alerts)))
        console.print(alert_table)

    # Build combined content for the LLM
    combined_content = "\n\n".join(
        f"===== FILE: {s.filename} =====\n{s.content}" for s in extracted_specs
    )

    # Save inputs snapshot (always)
    (run_dir / "inputs_combined.txt").write_text(combined_content, encoding="utf-8")

    if dry_run:
        console.print("\n[cyan]Dry run:[/cyan] Skipping API call.")
        sys.exit(0)

    # Call Claude
    console.print("\n[bold]Reviewing with Opus 4.5...[/bold]")
    review_result = review_specs(combined_content, verbose=verbose)

    if review_result.error:
        console.print(f"[red]Error:[/red] {review_result.error}")
        (run_dir / "error.txt").write_text(review_result.error, encoding="utf-8")
        sys.exit(1)

    # Save raw response + findings
    (run_dir / "raw_response.txt").write_text(review_result.raw_response or "", encoding="utf-8")

    findings_json = {
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
    }
    (run_dir / "findings.json").write_text(json.dumps(findings_json, indent=2), encoding="utf-8")

    # Generate report docx
    report_path = run_dir / "report.docx"
    generate_report_docx(
        findings=review_result.findings,
        output_path=report_path,
        leed_alerts=leed_alerts,
        placeholder_alerts=placeholder_alerts,
    )

    console.print(f"\n[green]Review complete![/green] ({review_result.elapsed_seconds:.1f}s)")
    if review_result.input_tokens or review_result.output_tokens:
        console.print(f"[dim]Tokens: {review_result.input_tokens:,} in → {review_result.output_tokens:,} out[/dim]")

    # Findings summary
    console.print("\n[bold]Findings Summary:[/bold]")
    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column("Severity", style="bold")
    summary_table.add_column("Count", justify="right")

    summary_table.add_row("CRITICAL", str(review_result.critical_count))
    summary_table.add_row("HIGH", str(review_result.high_count))
    summary_table.add_row("MEDIUM", str(review_result.medium_count))
    summary_table.add_row("GRIPES", str(review_result.gripe_count))
    summary_table.add_row("TOTAL", str(review_result.total_count))

    console.print(summary_table)
    console.print(f"[dim]Outputs written to: {run_dir}[/dim]")


if __name__ == "__main__":
    main()
