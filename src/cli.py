"""CLI entry point for spec-review tool."""
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
from .preprocessor import preprocess_spec, PreprocessResult
from .tokenizer import analyze_token_usage, format_token_summary, RECOMMENDED_MAX
from .prompts import get_system_prompt
from .reviewer import review_specs, ReviewResult, MODEL_SONNET, MODEL_OPUS

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


def print_preprocessing_summary(results: list[PreprocessResult], summary: dict, verbose: bool, stripped_dir: Path):
    """Print preprocessing summary."""
    if summary['total_chars_removed'] > 0:
        console.print(
            f"\n[dim]Boilerplate stripped: {summary['total_chars_removed']:,} chars "
            f"({summary['reduction_percent']:.1f}% reduction)[/dim]"
        )
    console.print(f"[dim]Stripped files saved to: {stripped_dir}[/dim]")


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


def get_docx_files_from_directory(input_dir: Path) -> list[Path]:
    """Get all .docx files from a directory."""
    if not input_dir.exists():
        console.print(f"[red]Error: Input directory not found: {input_dir}[/red]")
        sys.exit(1)
    
    if not input_dir.is_dir():
        console.print(f"[red]Error: Not a directory: {input_dir}[/red]")
        sys.exit(1)
    
    docx_files = sorted(input_dir.glob("*.docx"))
    
    # Filter out temp files (start with ~$)
    docx_files = [f for f in docx_files if not f.name.startswith("~$")]
    
    return docx_files


def save_stripped_content(specs: list[ExtractedSpec], results: list[PreprocessResult], output_dir: Path) -> Path:
    """
    Save stripped/cleaned spec content to text files for review.
    
    Args:
        specs: Original extracted specs (for filenames)
        results: Preprocessed results with cleaned content
        output_dir: Base output directory
        
    Returns:
        Path to the stripped files directory
    """
    stripped_dir = output_dir / "stripped"
    stripped_dir.mkdir(parents=True, exist_ok=True)
    
    for spec, result in zip(specs, results):
        # Change .docx to .txt
        output_filename = Path(spec.filename).stem + "_stripped.txt"
        output_path = stripped_dir / output_filename
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# Stripped content from: {spec.filename}\n")
            f.write(f"# Original length: {result.original_length:,} chars\n")
            f.write(f"# Cleaned length: {result.cleaned_length:,} chars\n")
            f.write(f"# Removed: {result.chars_removed:,} chars ({result.reduction_percent:.1f}%)\n")
            f.write("#" + "=" * 60 + "\n\n")
            f.write(result.cleaned_content)
    
    return stripped_dir


def setup_output_directory(output_dir: Path) -> Path:
    """
    Create timestamped output directory structure.
    
    Args:
        output_dir: Base output directory
        
    Returns:
        Path to the timestamped run directory
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = output_dir / f"review_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


@click.group()
@click.version_option(version=__version__)
def main():
    """MEP Specification Review Tool for California K-12 DSA Projects."""
    pass


@main.command()
@click.option('--input-dir', '-i', type=click.Path(exists=True), required=True, 
              help='Input directory containing .docx specification files')
@click.option('--output-dir', '-o', type=click.Path(), default='./output', 
              help='Output directory for reports and stripped files')
@click.option('--verbose', '-v', is_flag=True, help='Show detailed processing information')
@click.option('--dry-run', is_flag=True, help='Process files but do not call API')
@click.option('--opus', is_flag=True, help='Use Opus 4.5 instead of Sonnet 4.5 (higher quality, more expensive)')
@click.option('--thinking', is_flag=True, help='Enable extended thinking (Opus only, even more expensive)')
def review(input_dir: str, output_dir: str, verbose: bool, dry_run: bool, opus: bool, thinking: bool):
    """
    Review MEP specifications for code compliance and technical issues.
    
    Loads all .docx files from the input directory (max 5).
    
    Example:
        spec-review review -i ./specs -o ./output --verbose
        spec-review review -i ./specs --opus --thinking
    """
    print_header()
    
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    
    # Get .docx files from input directory
    docx_files = get_docx_files_from_directory(input_path)
    
    if len(docx_files) == 0:
        console.print(f"[red]Error: No .docx files found in {input_path}[/red]")
        sys.exit(1)
    
    if len(docx_files) > 5:
        console.print(f"[red]Error: Found {len(docx_files)} .docx files. Maximum of 5 allowed.[/red]")
        console.print("[dim]Remove some files from the input directory and try again.[/dim]")
        sys.exit(1)
    
    # Setup output directory
    run_dir = setup_output_directory(output_path)
    console.print(f"\n[dim]Output directory: {run_dir}[/dim]")
    
    # Extract text from files
    specs: list[ExtractedSpec] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Extracting text from documents...", total=len(docx_files))
        
        for filepath in docx_files:
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
    
    # Save stripped files
    stripped_dir = save_stripped_content(specs, preprocess_results, run_dir)
    
    print_preprocessing_summary(preprocess_results, preprocess_summary, verbose, stripped_dir)
    print_alerts(preprocess_summary)
    
    # Analyze token usage
    spec_contents_for_tokens = [(spec.filename, result.cleaned_content) 
                                 for spec, result in zip(specs, preprocess_results)]
    
    system_prompt = get_system_prompt()
    token_summary = analyze_token_usage(spec_contents_for_tokens, system_prompt)
    
    print_token_summary(token_summary, verbose)
    
    # Check if we can proceed
    if not token_summary.within_limit:
        console.print("\n[bold red]Cannot proceed: Token limit exceeded.[/bold red]")
        sys.exit(1)
    
    if dry_run:
        console.print("\n[yellow]Dry run complete. No API call made.[/yellow]")
        console.print(f"[dim]Review stripped files at: {stripped_dir}[/dim]")
        return
    
    # Build combined content for API
    combined_content = "\n\n".join([
        f"===== FILE: {spec.filename} =====\n{result.cleaned_content}"
        for spec, result in zip(specs, preprocess_results)
    ])
    
    # Determine model
    model = MODEL_OPUS if opus or thinking else MODEL_SONNET
    model_name = "Opus 4.5" if model == MODEL_OPUS else "Sonnet 4.5"
    thinking_str = " + Extended Thinking" if thinking else ""
    
    # Call Claude API
    console.print(f"\n[bold]Sending to Claude API ({model_name}{thinking_str})...[/bold]")
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Reviewing specifications...", total=None)
        review_result = review_specs(
            combined_content, 
            model=model, 
            use_thinking=thinking,
            verbose=verbose
        )
    
    if review_result.error:
        console.print(f"\n[bold red]Error: {review_result.error}[/bold red]")
        sys.exit(1)
    
    # Print results summary
    console.print(f"\n[green]Review complete![/green] ({review_result.elapsed_seconds:.1f}s)")
    
    # Token breakdown
    token_info = f"[dim]Tokens: {review_result.input_tokens:,} in"
    if review_result.thinking_tokens > 0:
        token_info += f" → {review_result.thinking_tokens:,} thinking + {review_result.output_tokens:,} output"
        token_info += f" = {review_result.total_output_tokens:,} total out"
    else:
        token_info += f" → {review_result.output_tokens:,} out"
    token_info += "[/dim]"
    console.print(token_info)
    
    # Print findings summary
    console.print(f"\n[bold]Findings Summary:[/bold]")
    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column("Severity", style="bold")
    summary_table.add_column("Count", justify="right")
    
    if review_result.critical_count > 0:
        summary_table.add_row("[red]CRITICAL[/red]", f"[red]{review_result.critical_count}[/red]")
    if review_result.high_count > 0:
        summary_table.add_row("[orange1]HIGH[/orange1]", f"[orange1]{review_result.high_count}[/orange1]")
    if review_result.medium_count > 0:
        summary_table.add_row("[yellow]MEDIUM[/yellow]", f"[yellow]{review_result.medium_count}[/yellow]")
    if review_result.low_count > 0:
        summary_table.add_row("[blue]LOW[/blue]", f"[blue]{review_result.low_count}[/blue]")
    if review_result.gripes_count > 0:
        summary_table.add_row("[magenta]GRIPES[/magenta]", f"[magenta]{review_result.gripes_count}[/magenta]")
    
    if review_result.total_count == 0:
        console.print("  [green]No issues found![/green]")
    else:
        console.print(summary_table)
    
    # Save raw JSON response
    json_output_path = run_dir / "findings.json"
    findings_data = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "model": review_result.model,
            "input_tokens": review_result.input_tokens,
            "output_tokens": review_result.output_tokens,
            "thinking_tokens": review_result.thinking_tokens,
            "total_output_tokens": review_result.total_output_tokens,
            "elapsed_seconds": review_result.elapsed_seconds,
            "files_reviewed": [spec.filename for spec in specs],
        },
        "summary": {
            "critical": review_result.critical_count,
            "high": review_result.high_count,
            "medium": review_result.medium_count,
            "low": review_result.low_count,
            "gripes": review_result.gripes_count,
            "total": review_result.total_count,
        },
        "alerts": {
            "leed_references": preprocess_summary['all_leed_alerts'],
            "placeholders": preprocess_summary['all_placeholder_alerts'],
        },
        "findings": [
            {
                "severity": f.severity,
                "fileName": f.fileName,
                "section": f.section,
                "issue": f.issue,
                "actionType": f.actionType,
                "existingText": f.existingText,
                "replacementText": f.replacementText,
                "codeReference": f.codeReference,
            }
            for f in review_result.findings
        ]
    }
    
    with open(json_output_path, "w", encoding="utf-8") as f:
        json.dump(findings_data, f, indent=2, ensure_ascii=False)
    
    console.print(f"\n[dim]Results saved to: {json_output_path}[/dim]")
    console.print(f"[dim]Stripped files at: {stripped_dir}[/dim]")
    
    # Print detailed findings if verbose
    if verbose and review_result.findings:
        console.print("\n[bold]Detailed Findings:[/bold]")
        for finding in review_result.findings:
            severity_colors = {
                "CRITICAL": "red",
                "HIGH": "orange1", 
                "MEDIUM": "yellow",
                "LOW": "blue",
                "GRIPES": "magenta"
            }
            color = severity_colors.get(finding.severity, "white")
            
            console.print(f"\n  [{color}]{finding.severity}[/{color}] - {finding.fileName}")
            console.print(f"  [dim]Section:[/dim] {finding.section}")
            console.print(f"  [dim]Issue:[/dim] {finding.issue}")
            if finding.actionType:
                console.print(f"  [dim]Action:[/dim] {finding.actionType}")
            if finding.existingText:
                console.print(f"  [dim]Existing:[/dim] {finding.existingText[:100]}...")
            if finding.replacementText:
                console.print(f"  [dim]Replace with:[/dim] {finding.replacementText[:100]}...")
            if finding.codeReference:
                console.print(f"  [dim]Reference:[/dim] {finding.codeReference}")


@main.command()
def version():
    """Show version information."""
    console.print(f"spec-review v{__version__}")


if __name__ == '__main__':
    main()
