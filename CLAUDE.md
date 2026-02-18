# CLAUDE.md

This file provides guidance for AI assistants working on the **Spec Critic** codebase.

## Project Overview

Spec Critic is a GUI tool for reviewing Mechanical & Plumbing specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction. It uses Claude Opus 4.6 for AI-powered analysis of `.docx` specification files and renders results in-app as color-coded finding cards.

- **Version**: 1.5.0 (confidence scoring + Sonnet 4.6 verification)
- **Python**: >= 3.11 (uses `X | Y` union type syntax)
- **Review Model**: Claude Opus 4.6 (`claude-opus-4-6`), hardcoded — no model selection flags
- **Verification Model**: Claude Sonnet 4.6 (`claude-sonnet-4-6-20250610`)
- **Output**: In-app only. No files are written during a review. The only file output is the optional Export JSON button.

## Repository Structure

```
spec-review/
├── main.py                  # Entry point
├── src/                     # Core package
│   ├── __init__.py          # Package version ("1.5.0")
│   ├── gui.py               # CustomTkinter app window, input handling, threading
│   ├── widgets.py           # Custom UI widgets (TokenGauge, FileListPanel,
│   │                        #   EnhancedLog, AnimatedButton, ReportPanel, ReportWindow)
│   ├── pipeline.py          # SINGLE SOURCE OF TRUTH for review workflow
│   ├── batch.py             # Anthropic Message Batches API integration
│   ├── verifier.py          # Web search self-verification (Sonnet 4.6 + web_search)
│   ├── extractor.py         # .docx text extraction (paragraphs + tables)
│   ├── preprocessor.py      # Local LEED/placeholder detection (NOT sent to LLM)
│   ├── tokenizer.py         # tiktoken-based token counting + limit enforcement
│   ├── prompts.py           # System prompt and user message construction
│   └── reviewer.py          # Anthropic API client with streaming + retry logic
├── pyproject.toml           # Modern Python packaging config
├── .gitignore               # Excludes specs/, venv/, build/, dist/
└── README.md                # User-facing documentation
```

## Architecture

### Core Design Principle

`pipeline.py` is the **single source of truth** for the review workflow. The GUI (`gui.py`) calls `pipeline.run_review()` and receives a `PipelineResult` containing all data needed to render the in-app report. Never duplicate pipeline logic in the GUI module.

### Pipeline Stages (in order)

1. Extract text from `.docx` files → `ExtractedSpec` objects
2. Detect LEED references and placeholders locally (regex, not sent to LLM)
3. Combine specs with `===== FILE:` header delimiters
4. Enforce 150k token limit (hard stop, no silent truncation)
5. Stream API call to Claude Opus 4.6
6. Parse JSON findings (including confidence scores) + analysis summary from response
7. Return `PipelineResult` to GUI for in-app rendering

### Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `gui.py` | App window, input handling (including project context field, mode toggle), threading, review orchestration, batch polling, report expand/collapse mode, pop-out report window lifecycle |
| `widgets.py` | All custom CustomTkinter widgets with animations, shared report rendering helpers, ReportWindow toplevel, confidence badge rendering |
| `pipeline.py` | Orchestration — ties all modules together, returns `PipelineResult`. Provides `run_review()` for real-time and `start_batch_review()` + `collect_batch_results()` for batch |
| `batch.py` | Anthropic Message Batches API integration — submission, polling, result retrieval, cancellation |
| `verifier.py` | Web search self-verification — builds verification prompts, calls Sonnet 4.6 with web_search tool, parses verdicts. Verifies in ascending confidence order. |
| `extractor.py` | `.docx` → plain text (paragraphs + tables) |
| `preprocessor.py` | Local regex detection of LEED refs and placeholders |
| `tokenizer.py` | Token counting (tiktoken cl100k_base) + limit enforcement |
| `prompts.py` | XML-structured system prompt with parameterized code cycle, confidence scoring schema, + enriched user message |
| `reviewer.py` | Anthropic API streaming client with retry logic + JSON parsing (including confidence field) |

### Data Flow

```
.docx files
    → extractor.py (text extraction)
    → preprocessor.py (LEED/placeholder detection, local only)
    → tokenizer.py (token counting, limit check)
    → reviewer.py (streaming API call to Claude Opus 4.6)
    → pipeline.py (orchestration, returns PipelineResult)
    → gui.py (renders ReportPanel + opens ReportWindow)
```

### Data Flow Classes

- `ExtractedSpec` — filename, content, word_count (from extractor)
- `PreprocessResult` — leed_alerts, placeholder_alerts (from preprocessor)
- `TokenCount` / `TokenSummary` — per-file and total token analysis (from tokenizer)
- `Finding` — severity, fileName, section, issue, actionType, **confidence (0.0-1.0)**, etc., **plus optional `verification` field** (from reviewer)
- `ReviewResult` — findings, raw_response, thinking, model, tokens, etc. (from reviewer)
- `PipelineResult` — review_result, files_reviewed, leed/placeholder alerts (from pipeline)

All data containers use `@dataclass` decorators.

## Confidence Scoring (v1.5.0)

### How It Works

Each finding includes a numeric `confidence` field (0.0–1.0):
- **0.85–1.0**: HIGH — model is quite sure, can cite specific code section
- **0.60–0.84**: MODERATE — fairly sure but uncertain on details
- **0.35–0.59**: LOW-MODERATE — suspected issue, flagged with caveats
- **Below 0.35**: Not flagged as a finding — mentioned in narrative summary only

### Where Confidence Is Used

1. **Parsing**: `_parse_findings()` in `reviewer.py` extracts and clamps confidence to [0.0, 1.0], defaults to 0.5 if missing or invalid
2. **Card rendering**: `_render_collapsible_card()` in `widgets.py` shows a color-coded confidence badge (green/amber/red) in each card header
3. **Sorting**: `_render_findings_section()` sorts findings by confidence descending within each severity tier
4. **Verification priority**: `verify_findings()` in `verifier.py` processes findings in ascending confidence order (least confident first)
5. **JSON export**: `_finding_to_dict()` includes the confidence field

## Prompt Architecture

### System Prompt (`prompts.py`)

The system prompt uses XML-tagged sections for clear structural hierarchy:

| Section | Purpose |
|---------|---------|
| `<task>` | Core instruction: review specs, classify findings, assign confidence |
| `<personality>` | Tone calibration with three example ranges + narrative budget (2-4 paragraphs) |
| `<severity_definitions>` | CRITICAL / HIGH / MEDIUM / GRIPES with concrete examples |
| `<review_priorities>` | Three-tier weighted checklist (Tier 1 = always check, Tier 3 = when relevant) |
| `<what_not_to_flag>` | LEED, placeholders, low-confidence hunches |
| `<confidence_guidance>` | Numeric 0.0-1.0 spectrum with score ranges and examples |
| `<edge_cases>` | Single spec, non-MEP only, very short specs, mixed disciplines |
| `<duplicate_issues>` | Consolidation rule for repeated problems |
| `<file_delimiters>` | How input files are separated |
| `<output_format>` | JSON schema (including confidence field) + examples showing ADD, EDIT, DELETE with confidence scores |
| `<critical_checks>` | Five mandatory verification steps |

### Code Cycle Parameters

Code references are parameterized at the top of `prompts.py`:

```python
CURRENT_CBC = "2025"
CURRENT_ASCE7 = "7-22"
PREVIOUS_CBC = "2022"
PREVIOUS_ASCE7 = "7-16"
```

When California adopts a new code cycle, update these constants. All references in the system prompt and user message update automatically via f-string interpolation.

### User Messages

There are two user message builders in `prompts.py`:

1. **`get_user_message()`** — Multi-spec combined mode (original). Takes concatenated spec content with FILE delimiters, file count, and optional project context. Analysis summary budget: 2-4 paragraphs.

2. **`get_single_spec_user_message()`** — Per-spec siloed mode (v1.4.0). Takes a single spec's content and filename. Analysis summary budget: 1-2 paragraphs. Used by `review_single_spec()` for per-spec siloed review.

Both builders accept an optional `project_context` parameter and remind the model to include confidence scores.

### Project Context

The `get_user_message()` and `get_single_spec_user_message()` functions accept an optional `project_context` parameter. If non-empty, it is inserted as a `<project_context>` XML-tagged block in the user message, before the spec content. This gives Claude project-specific information (building type, systems, scope) to inform the review. The project context text is counted toward the token limit in the GUI.

## Reviewer Architecture (v1.4.0+)

The reviewer module provides two public review functions and one internal helper:

- **`review_specs()`** — Original combined-review path. Sends all specs in one API call.
- **`review_single_spec()`** — Per-spec siloed review (v1.4.0). Sends one spec per API call.
- **`_stream_review()`** — Internal helper that handles streaming, retry logic, response parsing, and token tracking. Both public functions delegate to this after constructing their respective user messages.

This refactor eliminates duplication: retry logic, JSON parsing (including confidence extraction), and token counting exist in exactly one place (`_stream_review`).

## Token Limits

- **Max context**: 200,000 tokens (hard)
- **Recommended max input**: 150,000 tokens (enforced)
- **Safety buffer**: 50,000 tokens (for system prompt ~2-3k, max output 32,768, tokenizer variance)
- **Warning levels**: CRITICAL (>200k), WARNING (>150k), NOTE (>80% of 150k)

## Entry Point

```bash
python main.py
```

## Dependencies

### Core (from pyproject.toml)
- `anthropic` — Claude API client with streaming
- `customtkinter` — Modern dark-theme GUI toolkit
- `python-docx` — Word document reading
- `tiktoken` — Token counting (cl100k_base encoding)

### Install
```bash
pip install -e .
```

## API Key Configuration

The Anthropic API key is resolved in this order:
1. File: `spec_critic_api_key.txt` (in project root)
2. Environment variable: `ANTHROPIC_API_KEY`
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
- `ProgressFn = Callable[[float, str], None]` — progress updates

### Error Handling
- Early validation with `FileNotFoundError` / `ValueError`
- Hard stop on token limit exceeded (no silent truncation)
- Retry with exponential backoff for API errors:
  - `RateLimitError`: 10s, 20s, 40s
  - `APIConnectionError`: 5s, 10s, 20s
- Graceful fallbacks for missing optional fields (default to `None` or `""`)
- Confidence defaults to 0.5 if missing or invalid, clamped to [0.0, 1.0]

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

Each finding also carries a confidence score (0.0–1.0) that is independent of severity.

## GUI Architecture

The GUI uses CustomTkinter with a dark theme. All custom widgets live in `widgets.py`:
- `TokenGauge` — Animated fill gauge showing token capacity usage
- `FileListPanel` — Checkbox list with per-file token counts
- `EnhancedLog` — Scrollable log using a single `CTkTextbox` with colored text tags (v1.2.0)
- `AnimatedButton` — Run button with pulse/glow animations
- `ReportPanel` — In-app report with summary grid, alerts, collapsible severity-colored finding cards (with confidence badges), reviewer's notes, Expand button, Export JSON, and Copy Summary
- `ReportWindow` — Pop-out toplevel window with the full report (opens automatically on review completion)

### Collapsible Finding Cards

Each finding card has a clickable header row (severity badge + confidence badge + filename + section). Clicking the header toggles the card body (issue, existing/replacement text, code reference) between visible and collapsed. The findings section includes Collapse All / Expand All buttons for bulk toggling.

Within each severity tier, findings are sorted by confidence in descending order (most confident first).

All heavy operations (folder analysis, API calls) run in background threads. GUI updates are scheduled via `after()` to stay on the main thread.

## Important Patterns to Preserve

1. **Pipeline is the single source of truth** — Do not add workflow logic to `gui.py`. All review logic goes through `pipeline.run_review()`.
2. **No file output** — All results render in-app. The only file output is the optional Export JSON button.
3. **Preprocessor results are local-only** — LEED/placeholder alerts are NOT sent to the LLM. They are detected locally and displayed as alerts in the ReportPanel.
4. **Token limits are enforced before API calls** — Never allow API calls that exceed the 150k recommended limit.
5. **Streaming is used internally** — The reviewer always streams responses from the API. Streaming chunks are not displayed to the user; the complete response is parsed and rendered in the ReportPanel when finished.
6. **No model selection** — Claude Opus 4.6 is hardcoded for review. Claude Sonnet 4.6 is hardcoded for verification. There are no flags to change models.
7. **No document mutation** — This tool only analyzes specs. Document cleanup belongs in the separate SpecCleanse tool.
8. **Advisory only** — This tool assists human reviewers. It is not an AHJ substitute.
9. **Code cycle is parameterized** — Update the constants at the top of `prompts.py` when California adopts a new code cycle. All prompt references update automatically.
10. **Project context is optional** — If the user leaves the field empty, the user message is unchanged from previous versions. The `<project_context>` block is only added when text is present.
11. **Confidence defaults gracefully** — If the model omits confidence or returns an invalid value, it defaults to 0.5 and is clamped to [0.0, 1.0].

## Common Development Tasks

### Adding a new finding field
1. Update the `Finding` dataclass in `reviewer.py`
2. Update JSON parsing in `reviewer._parse_findings()`
3. Update the prompt schema in `prompts.py` `<output_format>` section
4. Update card rendering in `widgets.py` `_render_collapsible_card()`
5. Update `_finding_to_dict()` in `widgets.py` if the field should appear in JSON export

### Adding a new preprocessor check
1. Add detection function in `preprocessor.py` (follow `detect_leed_references` pattern)
2. Add results to `PreprocessResult` dataclass
3. Wire into `preprocess_spec()` / `preprocess_specs()`
4. Add alerts rendering in `widgets.py` `_render_alerts()`
5. Update summary stats in `pipeline.py`

### Modifying the system prompt
Edit `prompts.py`. The system prompt is organized into XML-tagged sections. Each section has a clear purpose documented in this file under "Prompt Architecture." Changes to the prompt affect all review behavior.

### Updating the code cycle
Edit the constants at the top of `prompts.py`:
```python
CURRENT_CBC = "2025"    # ← update this
CURRENT_ASCE7 = "7-22"  # ← and this
PREVIOUS_CBC = "2022"   # ← and these for comparison references
PREVIOUS_ASCE7 = "7-16"
```

## Testing

There is no formal test suite. Validation approaches:
- **Dry-run mode**: `dry_run=True` parameter skips the API call but exercises all other pipeline stages
- **Verbose mode**: `verbose=True` parameter outputs detailed logs for debugging
- **Modular design**: Each module is independently importable and testable
- **Callback injection**: Log/progress callbacks can be replaced with test harnesses
- **Confidence parsing tests**: `_parse_findings()` can be tested with synthetic JSON data to verify clamping, defaulting, and type coercion

## Files to Never Commit

- `specs/` — Contains user specification files
- `spec_critic_api_key.txt` — Contains API credentials
- `.env` files — May contain secrets
- `build/`, `dist/` — Build artifacts (if any)