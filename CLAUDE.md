# CLAUDE.md

This file provides guidance for AI assistants working on the **MEP Spec Review** codebase (Claude-Spec-Critic).

## Project Overview

MEP Spec Review is a GUI tool for reviewing Mechanical, Electrical & Plumbing specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction. It uses Claude Opus 4.6 for AI-powered analysis of `.docx` specification files and renders results in-app — no files are written to disk.

- **Version**: 1.0.0
- **Python**: >= 3.11 (uses `X | Y` union type syntax)
- **Model**: Claude Opus 4.6 (`claude-opus-4-6`), hardcoded — no model selection flags

## Repository Structure

```
Claude-Spec-Critic/
├── src/                     # Core package
│   ├── __init__.py          # Package version ("1.0.0")
│   ├── gui.py               # CustomTkinter GUI application (~267 lines)
│   ├── widgets.py           # Custom GUI widgets (~642 lines)
│   ├── pipeline.py          # SINGLE SOURCE OF TRUTH for review workflow
│   ├── extractor.py         # .docx text extraction (paragraphs + tables)
│   ├── preprocessor.py      # Local LEED/placeholder detection (NOT sent to LLM)
│   ├── tokenizer.py         # tiktoken-based token counting + limit enforcement
│   ├── prompts.py           # System prompt and user message construction
│   └── reviewer.py          # Anthropic API client with streaming + retry logic
├── pyproject.toml           # Python packaging config
├── requirements.txt         # Pinned dependency versions
├── .gitignore               # Excludes specs/, venv/, etc.
└── README.md                # Documentation
```

## Architecture

### Core Design Principle

`pipeline.py` is the **single source of truth** for the review workflow. The GUI (`src/gui.py`) calls `pipeline.run_review()` and receives a `PipelineResult` with all data needed to render the in-app report. Never duplicate pipeline logic in the GUI module.

### Pipeline Stages (in order)

1. Extract text from `.docx` files → `ExtractedSpec` objects
2. Detect LEED references and placeholders locally (regex, not sent to LLM)
3. Combine specs with `===== FILE:` header delimiters
4. Call Claude Opus 4.6 via streaming API
5. Parse JSON findings from response
6. Return `PipelineResult` to caller for in-app rendering

### Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `gui.py` | CustomTkinter GUI — orchestrates widgets and threads |
| `widgets.py` | Custom widgets: TokenGauge, FileListPanel, EnhancedLog, StreamingPanel, AnimatedButton, ReportPanel |
| `pipeline.py` | Orchestration — ties all modules together, returns `PipelineResult` |
| `extractor.py` | `.docx` → plain text (paragraphs + tables) |
| `preprocessor.py` | Local regex detection of LEED refs and placeholders |
| `tokenizer.py` | Token counting (tiktoken cl100k_base) + limit enforcement |
| `prompts.py` | System prompt with personality + severity definitions |
| `reviewer.py` | Anthropic API streaming client with retry logic |

### Data Flow Classes

- `ExtractedSpec` — filename, content, word_count (from extractor)
- `PreprocessResult` — leed_alerts, placeholder_alerts (from preprocessor)
- `TokenCount` / `TokenSummary` — per-file and total token analysis (from tokenizer)
- `Finding` — severity, fileName, section, issue, actionType, etc. (from reviewer)
- `ReviewResult` — findings, raw_response, thinking, model, tokens, etc. (from reviewer)
- `PipelineResult` — review_result, files_reviewed, leed_alerts, placeholder_alerts (from pipeline)

All data containers use `@dataclass` decorators.

## Token Limits

- **Max context**: 200,000 tokens (hard)
- **Recommended max input**: 150,000 tokens (enforced)
- **Safety buffer**: 50,000 tokens (for system prompt ~2-3k, max output 32,768, tokenizer variance)
- **Warning levels**: CRITICAL (>200k), WARNING (>150k), NOTE (>120k)

## Entry Point

```bash
python -m src.gui
```

## Dependencies

### Core (from pyproject.toml)
- `anthropic` — Claude API client with streaming
- `customtkinter` — Modern dark-theme GUI toolkit
- `python-docx` — Word document reading (used by extractor)
- `tiktoken` — Token counting (cl100k_base encoding)

### Install
```bash
pip install -r requirements.txt
# OR for editable development install:
pip install -e .
```

## API Key Configuration

The Anthropic API key is resolved in this order:
1. Environment variable: `ANTHROPIC_API_KEY`
2. File: `spec_critic_api_key.txt` (in project root)
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

### Callback Pattern
The pipeline uses callback injection for decoupling from UI:
- `LogFn = Callable[[str], None]` — log messages
- `ProgressFn = Callable[[float, str], None]` — progress updates
- `StreamCallback = Callable[[str], None]` — streaming text chunks

### Error Handling
- Early validation with `FileNotFoundError` / `ValueError`
- Hard stop on token limit exceeded (no silent truncation)
- Retry with exponential backoff for API errors:
  - `RateLimitError`: 10s, 20s, 40s
  - `APIConnectionError`: 5s, 10s, 20s
- Graceful fallbacks for missing optional fields (default to `None` or `""`)

## Severity Levels

Findings are classified into four severity tiers:
- **CRITICAL** — Code violations, life-safety issues, DSA compliance failures
- **HIGH** — Significant technical errors, coordination problems
- **MEDIUM** — Best-practice deviations, unclear language
- **GRIPES** — Formatting, style, minor nitpicks

## GUI Architecture

The GUI (`src/gui.py`) uses CustomTkinter with a dark theme. Custom widgets live in `src/widgets.py`:
- `TokenGauge` — Animated fill gauge showing token capacity usage
- `FileListPanel` — Checkbox list with per-file token counts
- `EnhancedLog` — Scrollable log with paced entries and animations
- `StreamingPanel` — Real-time display of Claude's streaming response
- `AnimatedButton` — Run button with pulse/glow animations
- `ReportPanel` — In-app report with finding cards, alerts, and export buttons

All heavy operations (folder analysis, API calls) run in background threads. GUI updates are scheduled via `after()` to stay on the main thread.

## Important Patterns to Preserve

1. **Pipeline is the single source of truth** — Do not add workflow logic to `gui.py`. All review logic goes through `pipeline.run_review()`.
2. **Preprocessor results are local-only** — LEED/placeholder alerts are NOT sent to the LLM. They are detected locally and rendered in the ReportPanel.
3. **Token limits are enforced before API calls** — Never allow API calls that exceed the 150k recommended limit.
4. **Streaming is the default** — The reviewer always streams responses. The `stream_callback` parameter enables real-time display.
5. **No model selection** — Claude Opus 4.6 is hardcoded. There are no flags to change models.
6. **All output is in-app** — No files are written to disk. The ReportPanel renders findings, alerts, and analysis summary. Users can export JSON or copy the summary via in-app buttons.
7. **The `specs/` directory is gitignored** — User data never enters version control.

## Common Development Tasks

### Adding a new finding field
1. Update the `Finding` dataclass in `reviewer.py`
2. Update JSON parsing in `reviewer._parse_findings()`
3. Update the prompt schema in `prompts.py`
4. Update finding card rendering in `widgets.py` (`ReportPanel._render_card`)

### Adding a new preprocessor check
1. Add detection function in `preprocessor.py` (follow `detect_leed_references` pattern)
2. Add results to `PreprocessResult` dataclass
3. Wire into `preprocess_spec()` / `preprocess_specs()`
4. Add alerts rendering in `widgets.py` (`ReportPanel._render_alerts`)
5. Update summary stats in `pipeline.py`

### Modifying the system prompt
Edit `prompts.py`. The system prompt defines the reviewer personality, severity definitions, and expected output format (narrative + JSON array). Changes here affect all review behavior.

## Testing

There is no formal test suite. Validation approaches:
- **Dry-run mode**: `dry_run=True` parameter skips the API call but exercises all other pipeline stages
- **Verbose mode**: `verbose=True` parameter outputs detailed logs for debugging
- **Modular design**: Each module is independently importable and testable
- **Callback injection**: Log/progress/stream callbacks can be replaced with test harnesses

## Files to Never Commit

- `specs/` — Contains user specification files
- `spec_critic_api_key.txt` — Contains API credentials
- `.env` files — May contain secrets
