# MEP Spec Review

A CLI + GUI tool for reviewing mechanical and plumbing (M&P) specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction.

Uses Anthropic Claude Opus 4.5 for LLM analysis.

## Features

- **Batch Review**: Process multiple specification documents at once (limited by LLM context window)
- **LEED Detection**: Alerts when LEED references are found
- **Placeholder Detection**: Flags unresolved placeholders like `[INSERT...]`
- **Token Management**: Pre-flight token counting with warnings before API calls
- **Severity Classification**: Issues categorized as CRITICAL, HIGH, MEDIUM, and GRIPES
- **Dual Output**: Human-readable Word report + machine-readable JSON
- **GUI + CLI**: Desktop interface or command-line workflow

## Prerequisites

### Converting .doc Files

This tool only supports `.docx` files. If you have older `.doc` files (Word 97-2003 format), convert them first:
https://github.com/Abe-Borg/convert-doc-to-docx

### Cleaning Specifications

For best results, scrub Word docs of unnecessary components before review:
https://github.com/Abe-Borg/Spec_Cleanse

## Installation

```bash
# Unzip and enter the project directory
unzip spec-review.zip
cd spec-review

# Install dependencies
pip install -r requirements.txt

# Install the CLI tool
pip install -e .
```

## Configuration

Set your Anthropic API key as an environment variable:

**Windows (Command Prompt):**
```cmd
set ANTHROPIC_API_KEY=your-api-key-here
```

**Windows (PowerShell):**
```powershell
$env:ANTHROPIC_API_KEY="your-api-key-here"
```

**Mac/Linux:**
```bash
export ANTHROPIC_API_KEY="your-api-key-here"
```

## Usage

### Directory Structure

Put your .docx spec files in an input directory:

```
my-project/
├── specs/                    # Your input directory
│   ├── 23 05 00 - Common Work Results.docx
│   ├── 23 21 13 - Hydronic Piping.docx
│   └── 22 05 00 - Common Work Results Plumbing.docx
└── output/                   # Output will be created here
```

### CLI: Basic Review

```bash
spec-review review -i ./specs -o ./output
```

### CLI: With Verbose Output

```bash
spec-review review -i ./specs -o ./output --verbose
```

### CLI: Dry Run (No API Call)

Test extraction and preprocessing without calling the API:

```bash
spec-review review -i ./specs -o ./output --dry-run --verbose
```

### GUI

Launch the desktop interface:

```bash
python src/gui.py
```

Or if using the compiled executable:

```cmd
spec-review-gui.exe
```

### Command Options

| Option | Short | Description |
|--------|-------|-------------|
| `--input-dir` | `-i` | Input directory containing .docx files (required) |
| `--output-dir` | `-o` | Output directory for reports (default: `./output`) |
| `--verbose` | `-v` | Show detailed processing information |
| `--dry-run` | | Process files but skip API call |

### Model

This tool uses **Claude Opus 4.5** (`claude-opus-4-5-20251101`) exclusively.

- Context window: 200,000 tokens
- Max output: 32,768 tokens
- Recommended input limit: 150,000 tokens (leaves buffer for system prompt + response)

## Output Structure

Each run creates a timestamped folder:

```
output/
└── review_YYYY-MM-DD_HHMMSS/
    ├── report.docx           # Human-readable findings report
    ├── findings.json         # Machine-readable findings + alerts
    ├── raw_response.txt      # Raw Claude response (for debugging)
    ├── inputs_combined.txt   # Combined spec text sent to API
    ├── token_summary.json    # Token usage breakdown
    └── error.txt             # Only present if failure occurred
```

### findings.json Structure

```json
{
  "meta": {
    "model": "claude-opus-4-5-20251101",
    "input_tokens": 37942,
    "output_tokens": 8500,
    "elapsed_seconds": 120.5
  },
  "findings": [
    {
      "severity": "CRITICAL",
      "fileName": "23 21 13 - Hydronic Piping.docx",
      "section": "Part 2, Article 2.3.A",
      "issue": "Seismic bracing requirements reference ASCE 7-16 instead of ASCE 7-22 as required by CBC 2022",
      "actionType": "EDIT",
      "existingText": "Seismic design per ASCE 7-16",
      "replacementText": "Seismic design per ASCE 7-22 as adopted by CBC 2022",
      "codeReference": "CBC 2022 Chapter 16, DSA IR A-6"
    }
  ],
  "alerts": {
    "leed_alerts": [...],
    "placeholder_alerts": [...]
  }
}
```

## Severity Definitions

| Level | Description |
|-------|-------------|
| **CRITICAL** | DSA rejection risk, code violations, safety hazards |
| **HIGH** | Significant technical errors, outdated CSI format |
| **MEDIUM** | Wrong code editions, obsolete products |
| **GRIPES** | Editorial issues, formatting, terminology (not code/safety) |

## What It Checks

- California code compliance (CBC, CMC, CPC, CEC, CALGreen)
- DSA-specific requirements (seismic, certification, submittals)
- ASHRAE standards (62.1, 90.1, 55, etc.)
- SMACNA standards (duct construction, seismic restraint)
- ASPE standards (plumbing engineering practice)
- NFPA standards (fire pumps, special hazards)
- MSS standards (pipe hangers and supports)
- ASTM standards (materials and testing)
- Technical accuracy of performance criteria
- Product specifications (manufacturer names, model numbers, ratings)
- Submittal and QA requirements
- Internal consistency within each spec
- Cross-spec coordination (when multiple specs provided)
- Constructability issues

## What Gets Alerted (Not Sent to LLM)

These items are detected locally and reported separately:

- **LEED references**: Any mention of LEED, USGBC, or LEED credits
- **Placeholders**: `[INSERT...]`, `[SPECIFY...]`, `[VERIFY...]`, `___`, `[TBD]`, etc.

## Project Structure

```
spec-review/
├── src/
│   ├── __init__.py      # Package version
│   ├── cli.py           # CLI entry point (thin shell)
│   ├── gui.py           # Tkinter GUI (thin shell)
│   ├── pipeline.py      # Core orchestration (single source of truth)
│   ├── extractor.py     # DOCX text extraction
│   ├── preprocessor.py  # LEED/placeholder detection (no mutation)
│   ├── tokenizer.py     # Token counting with tiktoken
│   ├── prompts.py       # System prompt for Claude
│   ├── reviewer.py      # Anthropic API client
│   └── report.py        # Word report generation
├── requirements.txt
├── pyproject.toml
├── main.py              # PyInstaller entry point
├── spec-review.spec     # PyInstaller config
├── build.bat            # Build script for Windows
└── README.md
```

## Architecture Notes

- **Single pipeline**: All workflow logic lives in `pipeline.py`. CLI and GUI are thin shells.
- **Single model**: Hardcoded to Claude Opus 4.5. No model selection flags.
- **No document mutation**: This repo only analyzes specs. Cleanup belongs in Spec_Cleanse.
- **Advisory only**: This tool assists human reviewers. It is not an AHJ substitute.

## Building the Executable

To create a standalone `.exe` that doesn't require Python:

```bash
# Option 1: Use the build script
build.bat

# Option 2: Run PyInstaller directly
pip install pyinstaller
pyinstaller spec-review.spec --clean
```

The executable will be created at `dist/spec-review.exe`.

**Using the executable:**

```cmd
REM Set your API key
set ANTHROPIC_API_KEY=your-key-here

REM Run the tool
spec-review.exe review -i C:\path\to\specs -o C:\path\to\output
```

You can copy `spec-review.exe` to any Windows machine — no Python installation required.

## Troubleshooting

### Token Limit Exceeded

If you see "Token limit exceeded", split your input specs into smaller batches and run separately.

### API Key Not Set

Ensure `ANTHROPIC_API_KEY` is set in your environment before running.

### No .docx Files Found

- Verify files have `.docx` extension (not `.doc`)
- Check that files aren't temp files (`~$filename.docx`)

## License

MIT License