# CLAUDE.md

This file provides guidance for AI assistants working on the **Spec Critic** codebase.

## Project Overview

Spec Critic is a GUI tool for reviewing Mechanical & Plumbing specifications for California K-12 projects under DSA (Division of the State Architect) jurisdiction. It uses Claude Opus 4.6 for AI-powered analysis of `.docx` specification files and renders results in-app as color-coded finding cards.

- **Version**: 1.6.0 (cross-spec coordination check)
- **Python**: >= 3.11 (uses `X | Y` union type syntax)
- **Review Model**: Claude Opus 4.6 (`claude-opus-4-6`), hardcoded — no model selection flags
- **Verification Model**: Claude Sonnet 4.6 (`claude-sonnet-4-6`)
- **Cross-Check Model**: Claude Sonnet 4.6 (`claude-sonnet-4-6`)
- **Output**: In-app only. No files are written during a review. The only file output is the optional Export JSON button.

## Repository Structure

```
spec-review/
├── main.py                  # Entry point
├── src/                     # Core package
│   ├── __init__.py          # Package version ("1.6.0")
│   ├── gui.py               # CustomTkinter app window, input handling, threading
│   ├── widgets.py           # Custom UI widgets (TokenGauge, FileListPanel,
│   │                        #   EnhancedLog, AnimatedButton, ReportPanel, ReportWindow)
│   ├── pipeline.py          # SINGLE SOURCE OF TRUTH for review workflow
│   ├── cross_checker.py     # Cross-spec coordination check (Sonnet 4.6)
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
3. Per-spec token limit check (hard stop, no silent truncation)
4. Per-spec siloed review via streaming API calls to Claude Opus 4.6
5. Parse JSON findings (including confidence scores) + analysis summary
6. Deduplicate findings across specs
7. **Optional**: Cross-spec coordination check via Sonnet 4.6 (if enabled and 2+ specs)
8. Web search verification of all CRITICAL/HIGH/MEDIUM findings via Sonnet 4.6
9. Return `PipelineResult` to GUI for in-app rendering

### Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `gui.py` | App window, input handling (project context, mode toggle, cross-check checkbox), threading, review orchestration, batch polling, report expand/collapse, pop-out window lifecycle |
| `widgets.py` | All custom CustomTkinter widgets with animations, shared report rendering helpers, ReportWindow toplevel, confidence badge rendering, cross-check section rendering |
| `pipeline.py` | Orchestration — ties all modules together, returns `PipelineResult`. Provides `run_review()` for real-time and `start_batch_review()` + `collect_batch_results()` for batch |
| `cross_checker.py` | Cross-spec coordination check — extracts section headers, builds condensed input, calls Sonnet 4.6, parses coordination findings |
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
    → reviewer.py (per-spec streaming API calls to Claude Opus 4.6)
    → pipeline.py (deduplication)
    → cross_checker.py (optional: coordination check via Sonnet 4.6)
    → verifier.py (web search verification via Sonnet 4.6)
    → pipeline.py (aggregation, returns PipelineResult)
    → gui.py (renders ReportPanel + opens ReportWindow)
```

### Data Flow Classes

- `ExtractedSpec` — filename, content, word_count (from extractor)
- `PreprocessResult` — leed_alerts, placeholder_alerts (from preprocessor)
- `TokenCount` / `TokenSummary` — per-file and total token analysis (from tokenizer)
- `Finding` — severity, fileName, section, issue, actionType, **confidence (0.0-1.0)**, etc., **plus optional `verification` field** (from reviewer)
- `ReviewResult` — findings, raw_response, thinking, model, tokens, etc. (from reviewer)
- `PipelineResult` — review_result, files_reviewed, leed/placeholder alerts, **cross_check_result** (from pipeline)

All data containers use `@dataclass` decorators.

## Cross-Spec Coordination Check (v1.6.0)

### How It Works

After per-spec reviews complete and findings are deduplicated, an optional coordination check looks across ALL specs for inter-spec issues:

1. **Section headers** are extracted from each spec (PART lines, numbered articles, all-caps titles)
2. These headers plus a summary of existing per-spec findings are sent to **Sonnet 4.6** in a single API call
3. Sonnet looks exclusively for cross-spec coordination problems (contradictions, missing references, division-of-work gaps)
4. Coordination findings are returned as a separate `ReviewResult` and rendered in their own report section

### Key Design Decisions

- **Optional**: Controlled by a checkbox in the GUI (default off)
- **Sonnet 4.6**: Uses the cheaper/faster model since this is a focused analytical task
- **Condensed input**: Section headers + existing findings, NOT full spec text — keeps token usage low
- **Separate section**: Cross-check findings render in a distinct "CROSS-SPEC COORDINATION" section with cyan accent
- **No batch mode**: Cross-check is always a real-time streaming call (even when review uses batch mode)
- **Requires 2+ specs**: Automatically skipped if only 1 spec is loaded
- **Token limit check**: If the condensed input exceeds 150k tokens, cross-check is gracefully skipped
- **Verification included**: Cross-check findings go through the same web search verification as per-spec findings

### Where Cross-Check Data Lives

1. **`cross_checker.py`**: `run_cross_check()` returns a `ReviewResult` with coordination findings
2. **`pipeline.py`**: `PipelineResult.cross_check_result` holds the cross-check `ReviewResult`
3. **`widgets.py`**: `_render_cross_check_section()` renders the dedicated report section
4. **JSON export**: Cross-check findings appear in `cross_check_findings` and `cross_check_summary` fields

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

### Cross-Check System Prompt (`cross_checker.py`)

The cross-check prompt is embedded in `cross_checker.py` (not in `prompts.py`) because it is a fundamentally different task with different instructions:

| Section | Purpose |
|---------|---------|
| `<task>` | Find ONLY cross-spec coordination issues |
| `<what_to_look_for>` | Seven specific coordination problem types |
| `<what_NOT_to_flag>` | Already-identified issues, within-spec issues |
| `<severity_guidance>` | Coordination issues are typically HIGH or CRITICAL |
| `<confidence_guidance>` | Same 0.0-1.0 scale as per-spec review |
| `<output_format>` | Same Finding JSON schema for seamless integration |

### Code Cycle Parameters

Code references are parameterized at the top of `prompts.py`:

```python
CURRENT_CBC = "2025"
CURRENT_ASCE7 = "7-22"
PREVIOUS_CBC = "2022"
PREVIOUS_ASCE7 = "7-16"
```

When California adopts a new code cycle, update these constants. All references in the system prompt and user message update automatically via f-string interpolation.

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
Google-style docstrings with `Args:`, `Returns:`, `Raises:`, `Example:` sections.

### Callback Pattern
The pipeline uses callback injection for decoupling from UI:
- `LogFn = Callable[[str], None]` — log messages
- `ProgressFn = Callable[[float, str], None]` — progress updates

### Error Handling
- Early validation with `FileNotFoundError` / `ValueError`
- Hard stop on token limit exceeded (no silent truncation)
- Retry with exponential backoff for API errors
- Graceful fallbacks for missing optional fields
- Cross-check gracefully skips if token limit exceeded or <2 specs

## Severity Levels

Findings are classified into four severity tiers:
- **CRITICAL** — Code violations, life-safety issues, DSA compliance failures
- **HIGH** — Significant technical errors, coordination problems
- **MEDIUM** — Best-practice deviations, unclear language
- **GRIPES** — Formatting, style, minor nitpicks

## GUI Architecture

The GUI uses CustomTkinter with a dark theme. Key widgets in `widgets.py`:
- `TokenGauge` — Animated fill gauge showing token capacity usage
- `FileListPanel` — Checkbox list with per-file token counts
- `EnhancedLog` — Scrollable log with colored text tags
- `AnimatedButton` — Run button with pulse/glow animations
- `ReportPanel` — In-app report with summary grid, alerts, findings, cross-check section, notes
- `ReportWindow` — Pop-out toplevel window with the full report

### Cross-Check Checkbox (v1.6.0)

Row 4 of the INPUTS card contains a "Cross-spec coordination check" checkbox with a hint label ("Sonnet 4.6 • finds inter-spec conflicts"). When checked, the pipeline runs the coordination pass after per-spec review. The checkbox state is captured at review start and passed through as `cross_check=True/False`.

## Important Patterns to Preserve

1. **Pipeline is the single source of truth** — Do not add workflow logic to `gui.py`.
2. **No file output** — All results render in-app. Only Export JSON writes files.
3. **Preprocessor results are local-only** — LEED/placeholder alerts are NOT sent to the LLM.
4. **Token limits are enforced before API calls** — Never allow calls exceeding 150k.
5. **Streaming is used internally** — Complete response is parsed when finished.
6. **No model selection** — Opus for review, Sonnet for verification and cross-check. Hardcoded.
7. **No document mutation** — Analysis only. Document cleanup belongs in SpecCleanse.
8. **Advisory only** — This tool assists human reviewers. Not an AHJ substitute.
9. **Code cycle is parameterized** — Update constants in `prompts.py`.
10. **Cross-check is optional** — Controlled by GUI checkbox, default off.
11. **Cross-check findings are separate** — Rendered in their own section, not mixed with per-spec findings.

## Common Development Tasks

### Adding a new finding field
1. Update the `Finding` dataclass in `reviewer.py`
2. Update JSON parsing in `reviewer._parse_findings()`
3. Update the prompt schema in `prompts.py` `<output_format>` section
4. Update card rendering in `widgets.py` `_render_collapsible_card()`
5. Update `_finding_to_dict()` in `widgets.py` if the field should appear in JSON export

### Modifying the cross-check behavior
1. Edit the system prompt in `cross_checker.py` (`_CROSS_CHECK_SYSTEM_PROMPT`)
2. Edit `_build_spec_summary()` to change what data is sent
3. Edit `extract_section_headers()` to change what headers are extracted
4. Wire any new parameters through `pipeline.py` → `gui.py`

### Updating the code cycle
Edit the constants at the top of `prompts.py`:
```python
CURRENT_CBC = "2025"    # ← update this
CURRENT_ASCE7 = "7-22"  # ← and this
```

## Files to Never Commit

- `specs/` — Contains user specification files
- `spec_critic_api_key.txt` — Contains API credentials
- `.env` files — May contain secrets
- `build/`, `dist/` — Build artifacts