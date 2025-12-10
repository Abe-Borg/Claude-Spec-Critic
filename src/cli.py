"""CLI entry point for spec-review tool."""
import sys
from pathlib import Path
from datetime import datetime

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from . import __version__
from .extractor import extract_text_from_docx, ExtractedSpec
from .preprocessor import preprocess_spec, PreprocessResult
from .tokenizer import analyze_token_usage, format_token_summary, RECOMMENDED_MAX
from .prompts import get_system_prompt

console = Console()


def print_header():
    """Print the application header."""
    console.print(Panel.fit(
        "[bold blue]MEP Spec Review[/bold blue]\n"
        f"[dim]v{__version__} • California K-12 DSA Projects[/dim]",
        border_style="blue"
    ))


def print_files_loaded(specs: list[ExtractedSpec], verbose: bool):
    """Print information about loaded files."""
    console.print(f"\n[bold]Files loaded:[/bold] {len(specs)}")
    
    if verbose:
        for spec in specs:
            console.print(f"  • {spec.filename} ({spec.word_count:,} words)")


def print_preprocessing_summary(results: list[PreprocessResult], summary: dict, verbose: bool):
    """Print preprocessing summary."""
    if verbose and summary['total_chars_removed'] > 0:
        console.print(
            f"\n[dim]Boilerplate stripped: {summary['total_chars_removed']:,} chars "
            f"({summary['reduction_percent']:.1f}% reduction)[/dim]"
        )


def print_alerts(summary: dict):
    """Print LEED and placeholder alerts."""
    if summary['leed_alert_count'] > 0 or summary['placeholder_alert_count'] > 0:
        console.print("\n[bold yellow]⚠ ALERTS DETECTED[/bold yellow]")
        
        if summary['leed_alert_count'] > 0:
            console.print(f"\n  [yellow]LEED References ({summary['leed_alert_count']}):[/yellow]")
            for alert in summary['all_leed_alerts'][:10]:  # Limit display
                console.print(f"    • {alert['filename']} (line {alert['line']}): {alert['text']}")
            if summary['leed_alert_count'] > 10:
                console.print(f"    [dim]... and {summary['leed_alert_count'] - 10} more[/dim]")
        
        if summary['placeholder_alert_count'] > 0:
            console.print(f"\n  [yellow]Unresolved Placeholders ({summary['placeholder_alert_count']}):[/yellow]")
            for alert in summary['all_placeholder_alerts'][:10]:
                console.print(f"    • {alert['filename']} (line {alert['line']}): {alert['text']}")
            if summary['placeholder_alert_count'] > 10:
                console.print(f"    [dim]... and {summary['placeholder_alert_count'] - 10} more[/dim]")


def print_token_summary(token_summary, verbose: bool):
    """Print token usage summary."""
    if verbose:
        console.print(f"\n[bold]Token Analysis:[/bold]")
        for item in token_summary.items:
            console.print(f"  • {item.name}: {item.tokens:,} tokens")
        console.print(f"  System prompt: {token_summary.system_prompt_tokens:,} tokens")
        console.print(f"  [bold]Total: {token_summary.total_tokens:,} / {RECOMMENDED_MAX:,}[/bold]")
    
    if token_summary.warning_message:
        if "CRITICAL" in token_summary.warning_message:
            console.print(f"\n[bold red]{token_summary.warning_message}[/bold red]")
        elif "WARNING" in token_summary.warning_message:
            console.print(f"\n[bold yellow]{token_summary.warning_message}[/bold yellow]")
        else:
            console.print(f"\n[dim]{token_summary.warning_message}[/dim]")
    elif verbose:
        console.print("  [green]✓ Within recommended limits[/green]")


def validate_files(files: tuple[str, ...]) -> list[Path]:
    """Validate input files exist and are .docx format."""
    validated = []
    
    for f in files:
        path = Path(f)
        
        if not path.exists():
            console.print(f"[red]Error: File not found: {f}[/red]")
            sys.exit(1)
        
        if path.suffix.lower() != '.docx':
            console.print(f"[red]Error: Not a .docx file: {f}[/red]")
            sys.exit(1)
        
        validated.append(path)
    
    return validated


@click.group()
@click.version_option(version=__version__)
def main():
    """MEP Specification Review Tool for California K-12 DSA Projects."""
    pass


@main.command()
@click.argument('files', nargs=-1, required=True, type=click.Path(exists=False))
@click.option('--verbose', '-v', is_flag=True, help='Show detailed processing information')
@click.option('--output-dir', '-o', type=click.Path(), default='.', help='Output directory for report')
@click.option('--dry-run', is_flag=True, help='Process files but do not call API')
def review(files: tuple[str, ...], verbose: bool, output_dir: str, dry_run: bool):
    """
    Review MEP specifications for code compliance and technical issues.
    
    Accepts 1-5 .docx specification files.
    
    Example:
        spec-review review "23 05 00.docx" "23 21 13.docx" --verbose
    """
    print_header()
    
    # Validate file count
    if len(files) > 5:
        console.print("[red]Error: Maximum of 5 specification files allowed.[/red]")
        sys.exit(1)
    
    if len(files) == 0:
        console.print("[red]Error: At least one specification file required.[/red]")
        sys.exit(1)
    
    # Validate files exist and are .docx
    validated_files = validate_files(files)
    
    # Extract text from files
    specs: list[ExtractedSpec] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Extracting text from documents...", total=len(validated_files))
        
        for filepath in validated_files:
            try:
                spec = extract_text_from_docx(filepath)
                specs.append(spec)
                progress.advance(task)
            except Exception as e:
                console.print(f"[red]Error extracting {filepath.name}: {e}[/red]")
                sys.exit(1)
    
    print_files_loaded(specs, verbose)
    
    # Preprocess specs
    preprocess_results = []
    all_leed_alerts = []
    all_placeholder_alerts = []
    total_original = 0
    total_cleaned = 0
    
    for spec in specs:
        result = preprocess_spec(spec.content, spec.filename)
        preprocess_results.append(result)
        all_leed_alerts.extend(result.leed_alerts)
        all_placeholder_alerts.extend(result.placeholder_alerts)
        total_original += result.original_length
        total_cleaned += result.cleaned_length
    
    preprocess_summary = {
        'total_chars_removed': total_original - total_cleaned,
        'reduction_percent': ((total_original - total_cleaned) / total_original * 100) if total_original > 0 else 0,
        'leed_alert_count': len(all_leed_alerts),
        'placeholder_alert_count': len(all_placeholder_alerts),
        'all_leed_alerts': all_leed_alerts,
        'all_placeholder_alerts': all_placeholder_alerts,
    }
    
    print_preprocessing_summary(preprocess_results, preprocess_summary, verbose)
    print_alerts(preprocess_summary)
    
    # Analyze token usage
    spec_contents = [
        (result.cleaned_content, specs[i].filename) 
        for i, result in enumerate(preprocess_results)
    ]
    # Flip the tuple order for analyze_token_usage
    spec_contents_for_tokens = [(specs[i].filename, result.cleaned_content) for i, result in enumerate(preprocess_results)]
    
    system_prompt = get_system_prompt()
    token_summary = analyze_token_usage(spec_contents_for_tokens, system_prompt)
    
    print_token_summary(token_summary, verbose)
    
    # Check if we can proceed
    if not token_summary.within_limit:
        console.print("\n[bold red]Cannot proceed: Token limit exceeded.[/bold red]")
        sys.exit(1)
    
    if dry_run:
        console.print("\n[yellow]Dry run complete. No API call made.[/yellow]")
        return
    
    # API call would go here
    console.print("\n[bold green]Phase 1 complete.[/bold green]")
    console.print("[dim]API integration (Phase 2) not yet implemented.[/dim]")
    
    # Show what would be sent
    if verbose:
        console.print("\n[dim]Combined content preview (first 500 chars):[/dim]")
        combined = "\n\n".join([
            f"=== FILE: {specs[i].filename} ===\n{result.cleaned_content[:200]}..."
            for i, result in enumerate(preprocess_results)
        ])
        console.print(f"[dim]{combined[:500]}...[/dim]")


@main.command()
def version():
    """Show version information."""
    console.print(f"spec-review v{__version__}")


if __name__ == '__main__':
    main()
