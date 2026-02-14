# CLAUDE.md

This file provides guidance for AI assistants working on the **MEP Spec Review** codebase (Claude-Spec-Critic).

## Project Overview

MEP Spec Review is a GUI tool for reviewing Mechanical, Electrical & Plumbing specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction. It uses Claude Opus 4.5 for AI-powered analysis of `.docx` specification files and produces Word report + JSON artifacts.

- **Version**: 0.5.0
- **Python**: >= 3.10 (uses `X | Y` union type syntax)
- **Model**: Claude Opus 4.5 (`claude-opus-4-5-20251101`), hardcoded — no model selection flags

## Repository Structure

```
Claude-Spec-Critic/
├── gui.py                   # CustomTkinter GUI application (~1,850 lines)
├── src/                     # Core package
│   ├── __init__.py          # Package version ("0.5.0")
│   ├── pipeline.py          # SINGLE SOURCE OF TRUTH for review workflow
│   ├── extractor.py         # .docx text extraction (paragraphs + tables)
│   ├── preprocessor.py      # Local LEED/placeholder detection (NOT sent to LLM)
│   ├── tokenizer.py         # tiktoken-based token counting + limit enforcement
│   ├── prompts.py           # System prompt and user message construction
│   ├── reviewer.py          # Anthropic API client with streaming + retry logic
│   └── report.py            # Word document (.docx) report generation
├── pyproject.toml           # Modern Python packaging config
├── requirements.txt         # Pinned dependency versions (pip freeze output)
├── build.bat                # Windows PyInstaller build script
├── spec-review.spec         # PyInstaller config → MEP-Spec-Review.exe
├── .gitignore               # Excludes output/, specs/, venv/, build/, dist/
└── README.md                # Full documentation (~440 lines)
```

## Architecture

### Core Design Principle

`pipeline.py` is the **single source of truth** for the review workflow. The GUI (`gui.py`) calls `pipeline.run_review()`. Never duplicate pipeline logic in the GUI module.

### Pipeline Stages (in order)

1. Create timestamped output directory (`review_YYYY-MM-DD_HHMMSS/`)
2. Extract text from `.docx` files → `ExtractedSpec` objects
3. Detect LEED references and placeholders locally (regex, not sent to LLM)
4. Analyze token usage with system prompt
5. Enforce 150k token limit (hard stop, no silent truncation)
6. Combine specs with `===== FILE:` header delimiters
7. Stream API call to Claude Opus 4.5
8. Parse JSON findings + analysis summary from response
9. Generate Word report organized by severity
10. Write all artifacts to output directory

### Output Artifacts

Each run produces a timestamped directory containing:
- `report.docx` — Human-readable findings
- `findings.json` — Machine-readable findings + metadata
- `raw_response.txt` — Raw Claude response (for debugging)
- `inputs_combined.txt` — Exact text sent to API (reproducibility)
- `token_summary.json` — Token breakdown by file
- `error.txt` — Only if run failed

### Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `pipeline.py` | Orchestration — ties all modules together |
| `extractor.py` | `.docx` → plain text (paragraphs + tables) |
| `preprocessor.py` | Local regex detection of LEED refs and placeholders |
| `tokenizer.py` | Token counting (tiktoken cl100k_base) + limit enforcement |
| `prompts.py` | System prompt with personality + severity definitions |
| `reviewer.py` | Anthropic API streaming client with retry logic |
| `report.py` | Word document generation with color-coded findings |
| `gui.py` | CustomTkinter GUI with animations and real-time streaming |

### Data Flow Classes

- `ExtractedSpec` — filename, content, word_count (from extractor)
- `PreprocessResult` — leed_alerts, placeholder_alerts (from preprocessor)
- `TokenCount` / `TokenSummary` — per-file and total token analysis (from tokenizer)
- `Finding` — severity, fileName, section, issue, actionType, etc. (from reviewer)
- `ReviewResult` — findings, raw_response, thinking, model, tokens, etc. (from reviewer)
- `PipelineOutputs` — all output paths and metadata (from pipeline)

All data containers use `@dataclass` decorators.

## Token Limits

- **Max context**: 200,000 tokens (hard)
- **Recommended max input**: 150,000 tokens (enforced)
- **Safety buffer**: 50,000 tokens (for system prompt ~2-3k, max output 32,768, tokenizer variance)
- **Warning levels**: CRITICAL (>200k), WARNING (>150k), NOTE (>120k)

## Entry Points

### GUI
```bash
python gui.py
# OR
python -m src.gui
```

### PyInstaller executable
```bash
# Build
pyinstaller spec-review.spec --clean --noconfirm
# OR
build.bat

# Run
dist/MEP-Spec-Review.exe
```

## Dependencies

### Core (from pyproject.toml)
- `anthropic` — Claude API client with streaming
- `customtkinter` — Modern dark-theme GUI toolkit
- `python-docx` — Word document read/write
- `tiktoken` — Token counting (cl100k_base encoding)

### Build
- `pyinstaller` — Standalone executable packaging

### Install
```bash
pip install -r requirements.txt
# OR for editable development install:
pip install -e .
```

## API Key Configuration

The Anthropic API key is resolved in this order:
1. Environment variable: `ANTHROPIC_API_KEY`
2. File: `spec_critic_api_key.txt` (next to executable or in project root)
3. Manual entry via GUI dialog

## Code Conventions

### Naming
- **Files**: `snake_case.py`
- **Functions/variables**: `snake_case`
- **Classes**: `PascalCase`
- **Constants**: `UPPER_SNAKE_CASE`
- **Private methods**: Leading underscore (`_toggle`, `_extract_json_array`)

### Type Hints
Type hints are used throughout with Python 3.10+ union syntax (`str | None` rather than `Optional[str]`). All public functions have type-annotated signatures.

### Docstrings
Google-style docstrings with `Args:`, `Returns:`, `Raises:`, `Example:` sections. Module-level docstrings explain purpose, design decisions, and usage.

### Callback Pattern
The pipeline uses callback injection for decoupling from UI:
- `LogFn = Callable[[str], None]` — log messages
- `ProgressFn = Callable[[str, int], None]` — progress updates
- `StreamCallback = Callable[[str], None]` — streaming text chunks

### Error Handling
- Early validation with `FileNotFoundError` / `ValueError`
- Hard stop on token limit exceeded (no silent truncation)
- Retry with exponential backoff for API errors:
  - `RateLimitError`: 10s, 20s, 40s
  - `APIConnectionError`: 5s, 10s, 20s
- Graceful fallbacks for missing optional fields (default to `None` or `""`)

### File Organization Pattern
```python
"""Module docstring."""

from __future__ import annotations

import stdlib_modules
from .relative_imports import internal_modules

# Constants
UPPER_SNAKE = value

# Type aliases
MyCallback = Callable[[str], None]

# Private helpers
def _helper(): ...

# Public classes
class MainClass: ...

# Public functions
def public_function(): ...
```

## Severity Levels

Findings are classified into four severity tiers:
- **CRITICAL** — Code violations, life-safety issues, DSA compliance failures
- **HIGH** — Significant technical errors, coordination problems
- **MEDIUM** — Best-practice deviations, unclear language
- **GRIPES** — Formatting, style, minor nitpicks

## GUI Architecture

The GUI (`gui.py`) uses CustomTkinter with a dark theme and these custom widgets:
- `TokenGauge` — Animated fill gauge showing token capacity usage
- `FileListPanel` — Checkbox list with per-file token counts
- `EnhancedLog` — Scrollable log with paced entries and animations
- `StreamingPanel` — Real-time display of Claude's streaming response
- `AnimatedButton` — Run button with pulse/glow animations

All heavy operations (folder analysis, API calls) run in background threads. GUI updates are scheduled via `after()` to stay on the main thread.

## Important Patterns to Preserve

1. **Pipeline is the single source of truth** — Do not add workflow logic to `gui.py`. All review logic goes through `pipeline.run_review()`.
2. **Preprocessor results are local-only** — LEED/placeholder alerts are NOT sent to the LLM. They are detected locally and added directly to the report.
3. **Token limits are enforced before API calls** — Never allow API calls that exceed the 150k recommended limit.
4. **Streaming is the default** — The reviewer always streams responses. The `stream_callback` parameter enables real-time display.
5. **No model selection** — Claude Opus 4.5 is hardcoded. There are no flags to change models.
6. **All output goes to timestamped directories** — Never overwrite previous results.
7. **The `output/` and `specs/` directories are gitignored** — User data never enters version control.

## Common Development Tasks

### Adding a new finding field
1. Update the `Finding` dataclass in `reviewer.py`
2. Update JSON parsing in `reviewer._parse_findings()`
3. Update the prompt schema in `prompts.py`
4. Update report rendering in `report.py`
5. Update `findings.json` output in `pipeline.py`

### Adding a new preprocessor check
1. Add detection function in `preprocessor.py` (follow `detect_leed_references` pattern)
2. Add results to `PreprocessResult` dataclass
3. Wire into `preprocess_spec()` / `preprocess_specs()`
4. Add alerts section rendering in `report.py`
5. Update summary stats in `pipeline.py`

### Modifying the system prompt
Edit `prompts.py`. The system prompt defines the reviewer personality, severity definitions, and expected output format (narrative + JSON array). Changes here affect all review behavior.

### Building the executable
```bash
# Windows only
build.bat
# OR
pyinstaller spec-review.spec --clean --noconfirm
```
Output: `dist/MEP-Spec-Review.exe`

## Testing

There is no formal test suite. Validation approaches:
- **Dry-run mode**: `dry_run=True` parameter skips the API call but exercises all other pipeline stages
- **Verbose mode**: `verbose=True` parameter outputs detailed logs for debugging
- **Modular design**: Each module is independently importable and testable
- **Callback injection**: Log/progress/stream callbacks can be replaced with test harnesses

## Files to Never Commit

- `output/` — Contains user review results
- `specs/` — Contains user specification files
- `spec_critic_api_key.txt` — Contains API credentials
- `.env` files — May contain secrets
- `build/`, `dist/` — Build artifacts
