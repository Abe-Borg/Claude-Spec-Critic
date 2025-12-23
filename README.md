# MEP Spec Review

A CLI tool for reviewing mechanical and plumbing (M&P) specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction.
Current implementation uses Anthropic API for LLM analysis. But this code can easily be adjusted to use any LLM.

## Features

- **Batch Review**: Process multiple specification documents at once. Only limitation is LLM context window.
- **LEED Detection**: Alerts when LEED references are found.
- **Placeholder Detection**: Flags unresolved placeholders like `[INSERT...]`
- **Token Management**: Pre-flight token counting with warnings before API calls
- **Severity Classification**: Issues categorized as CRITICAL, HIGH, MEDIUM and GRIPES
- **JSON Output**: Structured findings for further processing

## Converting .doc Files

This tool only supports `.docx` files. If you have older `.doc` files (Word 97-2003 format), convert them
using the following library: https://github.com/Abe-Borg/convert-doc-to-docx

##

The Word docs should be scrubbed of unnecessary components and fluff/garbage for best results. Use this
library: https://github.com/Abe-Borg/Spec_Cleanse

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

### Basic Review

```bash
spec-review review -i ./specs -o ./output
```

### With Verbose Output

```bash
spec-review review -i ./specs -o ./output --verbose
```

### Dry Run (No API Call)

Test the preprocessing without calling the API:

```bash
spec-review review -i ./specs -o ./output --dry-run --verbose
```

### Command Options

| Option | Short | Description |
|--------|-------|-------------|
| `--input-dir` | `-i` | Input directory containing .docx files (required) |
| `--output-dir` | `-o` | Output directory for reports (default: `./output`) |
| `--verbose` | `-v` | Show detailed processing information |
| `--dry-run` | | Process files but do not call API |


### Model
**Opus 4.5**: Higher quality analysis, better at catching subtle issues.
- Max output: 32,768 tokens

```bash
spec-review review -i ./specs --opus
```

## Output Structure

Each run creates a timestamped folder:

```
output/
└── review_YYYY-MM-DD_HHMMSS/
    ├── report.docx
    ├── findings.json
    ├── raw_response.txt
    ├── inputs_combined.txt
    ├── token_summary.json
    └── error.txt              # only if failure

```

### findings.json Structure

```json
{
  "metadata": {
    "timestamp": "2024-01-15T14:30:22",
    "model": "claude-opus-4-5-20251101",
    "input_tokens": 37942,
    "output_tokens": 8500,
    "elapsed_seconds": 120.5,
    "files_reviewed": ["23 05 00.docx", "23 21 13.docx"]
  },
  "summary": {
    "critical": 2,
    "high": 5,
    "medium": 3,
    "gripes": 2,
    "total": 13
  },
  "alerts": {
    "leed_references": [...],
    "placeholders": [...]
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
  ]
}
```

## Severity Definitions

| Level | Description |
|-------|-------------|
| **CRITICAL** | DSA rejection, code violations, safety hazards |
| **HIGH** | Significant technical errors, outdated CSI format |
| **MEDIUM** | Wrong code editions, obsolete products |
| **GRIPES** | Grumpy engineer complaints (not code/safety issues), editorial, formatting, terminology |

## What It Checks

- California code compliance (CBC, CMC, CPC, CEC, CALGreen)
- DSA-specific requirements (seismic, certification, submittals)
- ASHRAE, SMACNA, ASPE, NFPA standards
- Technical accuracy of performance criteria
- Product specifications
- Submittal and QA requirements
- Internal consistency within each spec
- Cross-spec coordination (when multiple specs provided)
- Constructability issues

## What Gets Alerted 
These items trigger alerts so the Human can review and edit them:
- **LEED references**: Any mention of LEED, USGBC, or LEED credits
- **Placeholders**: `[INSERT...]`, `[SPECIFY...]`, `[VERIFY...]`, `___`, `[TBD]`, etc.

## Project Structure

```
spec-review/
├── src/
│   ├── cli.py           # CLI entry point
│   ├── extractor.py     # DOCX text extraction
│   ├── preprocessor.py  # alert detection
│   ├── tokenizer.py     # Token counting
│   ├── prompts.py       # System prompt
│   ├── reviewer.py      # Claude API client
│   └── report.py        # Word report generation
├── requirements.txt
├── pyproject.toml
├── spec-review.spec     # PyInstaller config
├── build.bat            # Build script for Windows
└── README.md
```

## Development Status

- [x] Phase 1: Project skeleton, extraction, preprocessing
- [x] Phase 2: Claude API integration
- [x] Phase 3: Response parsing
- [x] Phase 4: Word report generation
- [x] Phase 5: Polish and error handling
- [x] Phase 6: PyInstaller packaging

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

## License

MIT License
