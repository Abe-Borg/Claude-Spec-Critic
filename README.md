# MEP Spec Review

A CLI tool for reviewing MEP (Mechanical, Electrical, Plumbing) specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction.

## Features

- **Batch Review**: Process up to 5 specification documents at once
- **Boilerplate Stripping**: Automatically removes specifier notes, copyright notices, and master spec artifacts
- **LEED Detection**: Alerts when LEED references are found (since you don't work on LEED projects)
- **Placeholder Detection**: Flags unresolved placeholders like `[INSERT...]`
- **Token Management**: Pre-flight token counting with warnings before API calls
- **Severity Classification**: Issues categorized as CRITICAL, HIGH, MEDIUM, or LOW
- **Word Report Output**: Findings exported as a formatted .docx report

## Installation

```bash
# Clone or download the project
cd spec-review

# Install dependencies
pip install -e .
```

## Usage

### Basic Review

```bash
spec-review review "23 05 00 - Common Work Results.docx"
```

### Multiple Files

```bash
spec-review review "23 05 00.docx" "23 21 13.docx" "22 05 00.docx" --verbose
```

### Options

- `--verbose, -v`: Show detailed processing information (token counts, file sizes, etc.)
- `--output-dir, -o PATH`: Specify output directory for the report (default: current directory)
- `--dry-run`: Process files and show token analysis without calling the API

### Examples

```bash
# Verbose mode with custom output directory
spec-review review specs/*.docx -v -o ./reports

# Dry run to check token usage
spec-review review large-spec.docx --dry-run --verbose
```

## Configuration

Set your Anthropic API key as an environment variable:

```bash
export ANTHROPIC_API_KEY="your-api-key-here"
```

## What It Checks

- California code compliance (CBC, CMC, CPC, CEC, CALGreen)
- DSA-specific requirements
- ASHRAE, SMACNA, ASPE, NFPA standards
- Technical accuracy of performance criteria
- Product specifications
- Submittal and QA requirements
- Cross-spec coordination (when multiple specs provided)

## Severity Definitions

| Level | Description |
|-------|-------------|
| **CRITICAL** | DSA rejection, code violations, safety hazards |
| **HIGH** | Significant technical errors requiring correction |
| **MEDIUM** | Wrong code editions, obsolete products |
| **LOW** | Editorial, formatting, terminology |

## Project Structure

```
spec-review/
├── src/
│   ├── cli.py           # CLI entry point
│   ├── extractor.py     # DOCX text extraction
│   ├── preprocessor.py  # Boilerplate removal, alert detection
│   ├── tokenizer.py     # Token counting
│   ├── prompts.py       # System prompt
│   ├── reviewer.py      # Claude API client (Phase 2)
│   └── report.py        # Word report generation (Phase 4)
├── pyproject.toml
└── README.md
```

## Development Status

- [x] Phase 1: Project skeleton, extraction, preprocessing
- [ ] Phase 2: Claude API integration
- [ ] Phase 3: Response parsing
- [ ] Phase 4: Word report generation
- [ ] Phase 5: Polish and error handling
- [ ] Phase 6: PyInstaller packaging

## License

Proprietary - Internal use only
