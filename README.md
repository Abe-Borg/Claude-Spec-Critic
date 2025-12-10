# MEP Spec Review

A CLI tool for reviewing mechanical and plumbing (M&P) specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction.

## Features

- **Batch Review**: Process up to 5 specification documents at once
- **Boilerplate Stripping**: Automatically removes specifier notes, copyright notices, and MasterSpec editorial instructions
- **Stripped File Export**: Saves cleaned specs to disk so you can review what was removed
- **LEED Detection**: Alerts when LEED references are found (since you don't work on LEED projects)
- **Placeholder Detection**: Flags unresolved placeholders like `[INSERT...]`
- **Token Management**: Pre-flight token counting with warnings before API calls
- **Severity Classification**: Issues categorized as CRITICAL, HIGH, MEDIUM, LOW, and GRIPES
- **Model Options**: Choose between Sonnet 4.5 (fast) or Opus 4.5 (thorough), with optional extended thinking
- **JSON Output**: Structured findings for further processing

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
| `--opus` | | Use Opus 4.5 instead of Sonnet 4.5 (higher quality, more expensive) |
| `--thinking` | | Enable extended thinking (Opus only, even more expensive) |

### Model Options

**Sonnet 4.5 (default)**: Fast, cost-effective, good for quick reviews.
- Max output: 16,384 tokens

```bash
spec-review review -i ./specs -o ./output
```

**Opus 4.5**: Higher quality analysis, better at catching subtle issues.
- Max output: 32,768 tokens

```bash
spec-review review -i ./specs --opus
```

**Opus 4.5 + Extended Thinking**: Maximum quality. Model "thinks" through the problem before responding. Best for complex specs with many interdependencies.
- Max output: 32,768 tokens
- Thinking budget: 50,000 tokens
- Uses streaming (required for long-running requests)

```bash
spec-review review -i ./specs --opus --thinking
```

## Output Structure

Each run creates a timestamped folder:

```
output/
└── review_2024-01-15_143022/
    ├── stripped/                          # Cleaned spec content for review
    │   ├── 23 05 00 - Common Work Results_stripped.txt
    │   └── 23 21 13 - Hydronic Piping_stripped.txt
    └── findings.json                      # Review results from Claude
```

### findings.json Structure

```json
{
  "metadata": {
    "timestamp": "2024-01-15T14:30:22",
    "model": "claude-opus-4-5-20251101",
    "input_tokens": 37942,
    "output_tokens": 8500,
    "thinking_tokens": 45000,
    "total_output_tokens": 53500,
    "elapsed_seconds": 120.5,
    "files_reviewed": ["23 05 00.docx", "23 21 13.docx"]
  },
  "summary": {
    "critical": 2,
    "high": 5,
    "medium": 3,
    "low": 1,
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

The `stripped/` folder contains text files showing exactly what content was sent to the LLM after boilerplate removal. Review these to verify the preprocessing is working correctly.

## Severity Definitions

| Level | Description |
|-------|-------------|
| **CRITICAL** | DSA rejection, code violations, safety hazards |
| **HIGH** | Significant technical errors, outdated CSI format |
| **MEDIUM** | Wrong code editions, obsolete products |
| **LOW** | Editorial, formatting, terminology |
| **GRIPES** | Grumpy engineer complaints (not code/safety issues) |

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

## What Gets Stripped (Removed from LLM Input)

The preprocessor removes content that adds no value to the review:
- `[Note to specifier...]` blocks and variations
- MasterSpec editorial instructions ("Retain or delete...", "Revise this Section...", etc.)
- Copyright notices (MasterSpec, ARCOM, BSD, SpecLink, Deltek, AIA)
- Separator lines (`****`, `----`, `====`)
- Page numbers
- Revision marks and hidden text markers

## What Gets Alerted (But Kept for LLM Review)

These items trigger alerts so you know about them, but they remain in the content so the LLM can also comment on them:
- **LEED references**: Any mention of LEED, USGBC, or LEED credits
- **Placeholders**: `[INSERT...]`, `[SPECIFY...]`, `[VERIFY...]`, `___`, `[TBD]`, etc.

## Project Structure

```
spec-review/
├── src/
│   ├── cli.py           # CLI entry point
│   ├── extractor.py     # DOCX text extraction
│   ├── preprocessor.py  # Boilerplate removal, alert detection
│   ├── tokenizer.py     # Token counting
│   ├── prompts.py       # System prompt
│   ├── reviewer.py      # Claude API client
│   └── report.py        # Word report generation (Phase 4)
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Development Status

- [x] Phase 1: Project skeleton, extraction, preprocessing, stripped file export
- [x] Phase 2: Claude API integration
- [x] Phase 3: Response parsing
- [ ] Phase 4: Word report generation
- [ ] Phase 5: Polish and error handling
- [ ] Phase 6: PyInstaller packaging

## License

Proprietary - Internal use only
