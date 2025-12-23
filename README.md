# MEP Spec Review

A CLI + GUI tool for reviewing mechanical and plumbing (M&P) specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction.

Uses Anthropic Claude Opus 4.5 for LLM analysis.

## Features

- **Batch Review**: Process multiple specification documents at once (limited by LLM context window)
- **LEED Detection**: Alerts when LEED references are found
- **Placeholder Detection**: Flags unresolved placeholders like `[INSERT...]`
- **Token Management**: Pre-flight token counting with warnings before API calls
- **Instant Token Analysis**: See token usage immediately when selecting a folder
- **Severity Classification**: Issues categorized as CRITICAL, HIGH, MEDIUM, and GRIPES
- **Dual Output**: Human-readable Word report + machine-readable JSON
- **Modern GUI**: Dark-themed CustomTkinter interface with animations and visual polish

## GUI Features (v0.3.0)

The CustomTkinter GUI includes:

- **Animated Token Gauge**: Visual meter with smooth fill animation showing token capacity usage
- **Paced Activity Log**: Entries appear at a readable pace (100ms for files, 200ms for status changes)
- **Fade-in Log Entries**: New log entries fade in smoothly instead of popping
- **Animated Run Button**: Gentle pulse during processing, glow effect on completion
- **Smooth Progress Bar**: Slower, calmer indeterminate animation during API calls
- **Collapsible Analysis Panel**: Smooth expand/collapse animation for Claude's analysis summary
- **Completion Effects**: Button glow and output folder button highlight when review finishes
- **Modern Dark Theme**: Professional dark interface with accent colors

### Animation Details

| Element | Animation |
|---------|-----------|
| Token Gauge | Smooth ease-out fill (700ms) with color gradient transition |
| Log Entries | Fade-in from background color (200ms) |
| Log Pacing | 200ms between file entries, 400ms between status entries |
| Run Button | Blue pulse effect while processing, glow on completion |
| Progress Bar | Standard CustomTkinter indeterminate animation |
| Thinking Panel | Smooth height animation for expand/collapse |

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

# Or install with pyproject.toml
pip install -e .
```

### Dependencies

- `anthropic>=0.40.0` — Claude API client
- `click>=8.1.0` — CLI framework
- `python-docx>=1.1.0` — Word document handling
- `rich>=13.0.0` — CLI formatting
- `tiktoken>=0.7.0` — Token counting
- `customtkinter>=5.2.0` — Modern GUI framework

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

**Or create a key file** (GUI only):
Create `spec_critic_api_key.txt` in the same directory as the executable, containing just your API key.

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

### GUI (Recommended)

Launch the modern interface:

```bash
python -m src.gui
```

Or if using the compiled executable:

```cmd
MEP-Spec-Review.exe
```

**GUI Workflow:**
1. Enter your API key (or let it auto-load from `spec_critic_api_key.txt`)
2. Click "Browse" to select your specs folder
3. **Watch the animated token gauge** — shows capacity usage with smooth fill
4. **Review the paced log** — entries appear at a readable speed
5. Click "Run Review"
6. **See the button pulse** while processing
7. Report opens automatically when complete

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
│   ├── gui.py           # CustomTkinter GUI with animations
│   ├── pipeline.py      # Core orchestration (single source of truth)
│   ├── extractor.py     # DOCX text extraction
│   ├── preprocessor.py  # LEED/placeholder detection (no mutation)
│   ├── tokenizer.py     # Token counting with tiktoken
│   ├── prompts.py       # System prompt for Claude
│   ├── reviewer.py      # Anthropic API client
│   └── report.py        # Word report generation
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

The executable will be created at `dist/MEP-Spec-Review.exe`.

**Using the executable:**

1. Place `spec_critic_api_key.txt` in the same folder as the `.exe` (optional, for auto-load)
2. Run `MEP-Spec-Review.exe`
3. Select your specs folder
4. Watch the animated token gauge fill
5. Click "Run Review"
6. Enjoy the smooth animations while processing

## Troubleshooting

### Token Limit Exceeded

If you see "Token limit exceeded", split your input specs into smaller batches and run separately. The token gauge shows your usage before you even click Run.

### API Key Not Set

- Ensure `ANTHROPIC_API_KEY` is set in your environment, OR
- Create `spec_critic_api_key.txt` next to the executable with your key

### No .docx Files Found

- Verify files have `.docx` extension (not `.doc`)
- Check that files aren't temp files (`~$filename.docx`)

### GUI Looks Wrong / Crashes

- Ensure you have `customtkinter>=5.2.0` installed
- For Python 3.12+, ensure tkinter is available: `pip install tk`

## Changelog

### v0.3.0
- **Paced log output**: File entries at 200ms, status at 400ms intervals
- **Log fade-in animation**: Entries fade in smoothly from background
- **Animated token gauge**: Smooth 700ms ease-out fill with color gradient
- **Button pulse animation**: Visible blue pulse while processing
- **Button glow on complete**: Brief success glow effect
- **Smooth panel expand/collapse**: Animated height transitions
- **Larger default window**: 800x800 for bigger log area
- **Removed output folder button**: Cleaner interface
- **Animation timing constants**: Centralized in ANIM dict for tuning

### v0.2.0
- New CustomTkinter GUI with dark theme
- Token gauge shows capacity on folder selection
- Enhanced activity log with colors and timestamps
- Visual feedback during processing

### v0.1.0
- Initial release with basic tkinter GUI
- CLI with --verbose and --dry-run options
- Streaming API support for Opus 4.5

## License

MIT License