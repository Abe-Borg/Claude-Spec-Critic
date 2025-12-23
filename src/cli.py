from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from . import __version__
from .reviewer import MODEL_OPUS_45
from .pipeline import run_review


console = Console()


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
@click.option("--verbose", "-v", is_flag=True, help="Verbose logs")
@click.option("--dry-run", is_flag=True, help="Process files but do not call API")
def review(input_dir: str, output_dir: str, verbose: bool, dry_run: bool) -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]MEP SPEC REVIEW[/bold cyan]  [dim]v{__version__}[/dim]\n"
            f"[dim]Model: {MODEL_OPUS_45} (single-model)[/dim]",
            border_style="cyan",
        )
    )

    def log(msg: str) -> None:
        if verbose:
            console.print(f"[dim]{msg}[/dim]")

    def progress(pct: float, msg: str) -> None:
        # keep CLI simple; show milestone messages only
        if verbose:
            console.print(f"[dim]{pct:5.1f}%[/dim] {msg}")

    try:
        out = run_review(
            input_dir=Path(input_dir),
            output_dir=Path(output_dir),
            dry_run=dry_run,
            verbose=verbose,
            log=log,
            progress=progress,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(f"\n[green]Done.[/green] Outputs: {out.run_dir}")
    console.print(f"[dim]report.docx:[/dim] {out.report_docx}")
    console.print(f"[dim]findings.json:[/dim] {out.findings_json}")
    console.print(f"[dim]raw_response.txt:[/dim] {out.raw_response_txt}")
