# Spec Critic v1.4.0

A desktop tool that reviews mechanical and plumbing construction specifications for California K-12 DSA projects using Claude Opus 4.6. Load `.docx` spec files, run the review, and see color-coded findings rendered directly in the app.

## What It Does

1. Extracts text from `.docx` specification files (paragraphs + tables)
2. Detects LEED references and unresolved placeholders locally (no API call needed)
3. Performs pre-flight token analysis with an animated visual gauge
4. Sends combined spec content to Claude Opus 4.6 via streaming API
5. Parses structured JSON findings from the response
6. Renders a full report in-app: summary grid, alerts, severity-colored finding cards, reviewer's notes
7. Opens the report in a separate pop-out window for dedicated viewing

## Running the Application

```bash
python main.py
```

### First-Time Setup

```bash
# Clone the repo
git clone <your-repo-url>
cd spec-review

# Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux

# Install dependencies
pip install -e .

# Run
python main.py
```

## API Key

The tool looks for your Anthropic API key in this order:

1. `spec_critic_api_key.txt` file in the project root containing just your key
2. `ANTHROPIC_API_KEY` environment variable
3. Manual entry in the API Key field within the app

For day-to-day use, drop a `spec_critic_api_key.txt` file in the project root. The app will auto-load it on launch.

## How to Use

1. Launch the app with `python main.py`
2. Enter your API key (or let it auto-load from file)
3. Click **Browse** to select `.docx` specs
4. (Optional) Enter project context in the **Project Context** field — describe the project so Claude has additional context for the review
5. The token gauge fills to show capacity usage — stay under the 150k limit
6. Expand the **FILES** panel to check/uncheck individual specs if needed
7. Click **Run Review**
8. When complete, the report renders in-app and a **pop-out report window** opens automatically
9. Click **Expand** to view the in-app report full-screen, or **← Back to Review** to return

### Project Context

The Project Context field is an optional free-text area where you can describe your project. For example:

- "New 2-story elementary school, 45,000 SF, gas heat pumps, VRF for admin wing"
- "TI of existing middle school gymnasium, replacing RTUs"
- "New K-8 school, central plant with CHW/HHW, classroom unit ventilators"

When provided, this context is included in the message sent to Claude so the reviewer can tailor its analysis to your specific project. The project context text is counted toward the token limit. If left empty, the review proceeds without project-specific context (same behavior as previous versions).

### Collapsible Finding Cards

Each finding card in the report has a clickable header. Click the header row (severity badge, filename, section) to collapse that card down to a single line. Click again to expand it. This lets you dismiss findings you've already reviewed and focus on the ones that still need attention.

Use the **Collapse All** and **Expand All** buttons in the findings toolbar to toggle all cards at once.

### Pop-Out Report Window

When the review finishes, a separate report window opens automatically with the full results. This window has the same collapsible cards, export, and copy functionality as the embedded report. You can work with the pop-out window independently — close it any time, or keep it alongside the main app while you prepare a new review.

### Batch Mode

Batch mode submits all specs as a single Anthropic Message Batch at 50% cost compared to real-time streaming. The tradeoff is turnaround time: batches typically complete in 15-60 minutes instead of immediately.

**How to use batch mode:**

1. Select your specs and configure project context as normal
2. In the INPUTS card, switch the **Mode** toggle from "Real-time" to "Batch (50% off)"
3. The run button changes to **Submit Batch**
4. Click Submit Batch — the app extracts specs, runs local checks, and submits the batch
5. The app polls automatically every 15 seconds and updates the activity log with progress
6. When the batch completes, results are collected and displayed in the same pop-out report window

**When to use batch mode:**

- Large reviews (5+ specs) where cost matters more than speed
- Overnight or end-of-day reviews where you don't need results immediately
- Any review where 50% cost savings justifies a 15-60 minute wait

**Notes:**

- The report format is identical between real-time and batch mode
- You can close and reopen the app while a batch is processing — however, the current version does not persist batch state, so you would need to manually check the Anthropic dashboard for results
- Batch errors are handled gracefully — if some specs fail in the batch, the rest still produce results

### Report Panel

After the review completes, the activity log collapses and the report panel renders with:

- **Summary grid**: Five color-coded cards showing Critical, High, Medium, Gripes, and Total counts
- **Token/time metadata**: Input/output token counts and processing duration
- **Alerts**: LEED references and unresolved placeholders detected locally (grouped by file)
- **Findings**: Cards grouped by severity (CRITICAL → HIGH → MEDIUM → GRIPES), each showing:
  - Clickable header with severity badge and filename (click to collapse/expand)
  - Section reference (CSI format)
  - Issue description
  - Existing text in red monospace
  - Replacement text in green monospace
  - Code reference in blue
- **Reviewer's Notes**: Claude's personality-driven analysis summary

Click **Expand** to hide all input panels and give the report the full window. Click **← Back to Review** to restore the normal layout.

### Export Options

- **Export JSON**: Opens a save dialog to write findings, alerts, metadata (including project context), to a `.json` file
- **Copy Summary**: Copies the reviewer's analysis summary text to your clipboard

## Project Structure

```
spec-review/
├── src/
│   ├── __init__.py      # Package version
│   ├── gui.py           # Main application window
│   ├── widgets.py       # Custom UI widgets (TokenGauge, FileListPanel,
│   │                    #   EnhancedLog, AnimatedButton, ReportPanel, ReportWindow)
│   ├── pipeline.py      # Core orchestration (single source of truth)
│   ├── batch.py         # Anthropic Message Batches API integration
│   ├── extractor.py     # DOCX text extraction
│   ├── preprocessor.py  # LEED/placeholder detection (no mutation)
│   ├── tokenizer.py     # Token counting with tiktoken
│   ├── prompts.py       # System prompt for Claude
│   └── reviewer.py      # Anthropic API client with streaming + retry
├── main.py              # Entry point
├── pyproject.toml       # Project metadata & dependencies
└── README.md
```

## Architecture

### Design Decisions

- **Single pipeline**: All workflow logic lives in `pipeline.py`. The GUI is a thin shell that calls `run_review()` and renders the result. This eliminates drift and makes testing straightforward.
- **Single model**: Hardcoded to Claude Opus 4.6 (`claude-opus-4-6`). No model selection flags, no alternatives.
- **No document mutation**: This repo only analyzes specs. Document cleanup belongs in the separate SpecCleanse tool.
- **No file output**: All results render in-app. The only file output is the optional Export JSON button. No output directories, no intermediate files.
- **Advisory only**: This tool assists human reviewers. It is not an AHJ substitute.
- **Report expand mode**: After a review, the report renders below the input panels. The Expand button hides all input panels so the report fills the entire window.
- **Pop-out window**: The report also opens in a separate `ReportWindow` toplevel for dedicated viewing.
- **Collapsible cards**: Finding cards can be individually collapsed/expanded, with bulk Collapse All / Expand All controls.

### Module Responsibilities

| Module | Purpose |
|---|---|
| `gui.py` | App window, input handling, threading, review orchestration, report expand/collapse, pop-out window lifecycle |
| `widgets.py` | All custom CustomTkinter widgets with animations, shared report rendering helpers, ReportWindow toplevel |
| `pipeline.py` | Single source of truth for the review workflow |
| `extractor.py` | DOCX text extraction (paragraphs + tables) |
| `preprocessor.py` | Local detection of LEED refs and placeholders |
| `tokenizer.py` | Token counting via tiktoken, limit enforcement |
| `prompts.py` | System prompt with personality and severity definitions |
| `reviewer.py` | Anthropic API client, streaming, JSON parsing, retry logic |

### Data Flow

```
.docx files
    → extractor.py (text extraction)
    → preprocessor.py (LEED/placeholder detection, local only)
    → tokenizer.py (token counting, limit check)
    → reviewer.py (streaming API call to Claude Opus 4.6)
    → pipeline.py (orchestration, returns PipelineResult)
    → gui.py (renders ReportPanel with findings + opens ReportWindow)
```

## What Claude Reviews

The system prompt instructs Claude to check:

- Code compliance: CBC, CMC, CPC, California Energy Code, CALGreen
- DSA-specific requirements: seismic restraint, certification, submittals
- ASHRAE standards: 62.1, 90.1, 55, etc.
- SMACNA standards: duct construction, seismic restraint
- ASPE standards: plumbing engineering practice
- NFPA standards: fire pumps, special hazards
- MSS, ASTM standards: pipe hangers, materials, testing
- Product specifications: manufacturer names, model numbers, ratings
- Internal consistency: within each spec and across multiple specs
- Coordination: mechanical vs. plumbing, and cross-discipline if non-MEP specs are included
- Constructability: issues that could cause delays or cost overruns

Claude classifies findings into four severity levels:

- **CRITICAL**: DSA rejection risks, code violations, safety hazards
- **HIGH**: Significant technical errors, outdated CSI formatting, coordination conflicts
- **MEDIUM**: Wrong code years, discontinued products, minor inconsistencies
- **GRIPES**: Typos, formatting issues, overly restrictive requirements

LEED references and unresolved placeholders (`[INSERT]`, `[VERIFY]`, `[TBD]`, etc.) are detected locally by `preprocessor.py` and displayed as alerts — they are not sent to Claude.

## Troubleshooting

### Token Limit Exceeded

If the token gauge shows "Capacity Exceeded" and turns red, your combined specs exceed the 150k token input limit. Use the FILES panel to uncheck some specs and bring the count under the limit. The run button is disabled while over capacity.

### API Key Not Loading

Make sure `spec_critic_api_key.txt` is in the project root. The file should contain only the API key with no extra whitespace or newlines.

### Streaming Stalls or Errors

Claude Opus 4.6 with large context windows requires streaming. If you see connection errors, the reviewer will automatically retry with exponential backoff (up to 3 attempts). Rate limit errors wait 10s/20s/40s between retries; connection errors wait 5s/10s/20s.

## Dependencies

```
anthropic          # Claude API client
python-docx        # DOCX text extraction
tiktoken           # Token counting (cl100k_base encoding)
customtkinter      # Modern themed Tkinter widgets
```

## Changelog

### v1.4.0 — Per-Spec Siloed Review + Batch Processing

**Phase 1: Per-Spec Siloed Review**

- **Feature**: Per-spec siloed review — each spec file now gets its own API call instead of concatenating all specs into one giant context. This gives each spec the model's full attention, avoids token limit bottlenecks for large projects, and is the foundation for batch processing
- **Feature**: `get_single_spec_user_message()` in `prompts.py` — focused user message for single-spec review with a shorter analysis summary budget (1-2 paragraphs)
- **Feature**: `review_single_spec()` in `reviewer.py` — reviews one spec per API call. Shares streaming, retry, and parsing logic with `review_specs()` via an internal `_stream_review()` helper
- **Feature**: `Finding.verification` field — optional slot for web search verification results (populated in Phase 3)
- **Feature**: Determinate progress bar — the progress bar now shows actual per-spec progress instead of an indeterminate spinner. Log messages show which spec is being reviewed (e.g., "Reviewing 23 05 00.docx (2/5)")
- **Feature**: Partial failure resilience — if one spec's API call fails, the remaining specs are still reviewed. Errors are reported in the Reviewer's Notes section
- **Refactor**: Pipeline token check is now per-spec instead of combined total. Each spec + system prompt must fit within 150k individually
- **Refactor**: Core streaming/retry/parsing logic extracted into `_stream_review()` to eliminate duplication
- **Refactor**: Shared `_prepare_specs()` helper extracts extraction + preprocessing + token checking logic used by both real-time and batch modes

**Phase 2: Batch Processing**

- **Feature**: Batch mode toggle in the Inputs card — a segmented button lets you switch between "Real-time" (streaming, immediate results) and "Batch (50% off)" (queued, 15-60 min turnaround). The run button text updates to "Submit Batch" when batch mode is selected
- **Feature**: `batch.py` module — standalone Anthropic Message Batches API integration with `submit_review_batch()`, `poll_batch()`, `retrieve_review_results()`, and `cancel_batch()`
- **Feature**: `start_batch_review()` in `pipeline.py` — extracts and validates specs, submits the batch, returns a `BatchSubmission` for the GUI to poll
- **Feature**: `collect_batch_results()` in `pipeline.py` — retrieves and aggregates results from a completed batch into the same `PipelineResult` shape as real-time mode
- **Feature**: Automatic batch polling — after submission, the GUI polls every 15 seconds and updates the progress bar and activity log with batch status (succeeded/processing/errored counts)
- **Feature**: Results are collected and displayed in the same report window as real-time mode — the report format is identical regardless of which mode was used

**Upcoming:**
- Phase 3: Web search self-verification of findings

### v1.3.0

- **Feature**: Project Context text field added to the INPUTS card. Optional free-text area where you describe your project (e.g., "New 2-story elementary school, 45,000 SF, gas heat pumps"). When provided, the context is included in the message to Claude as a `<project_context>` XML block. Project context tokens are counted toward the token limit. The field is included in Export JSON output under `meta.project_context`.
- **Fix**: Activity Log collapse now fully reclaims vertical space. Previously, collapsing the log hid the textbox but the parent frame retained its expanded height, leaving a gap. The fix uses `pack_propagate(False)` with a fixed collapsed height so the frame shrinks to just the header bar.
- **Rename**: App renamed from "MEP Spec Review" to "Spec Critic" throughout — window title, header, subtitle, report window title, report header card, and export filenames.

### v1.2.0

- **Performance**: Animation frame rates reduced — pulse and glow animations now run at 15fps (67ms) instead of 60fps (16ms); token gauge fill runs at 30fps (33ms). This frees the main thread during long API calls, eliminating noticeable UI lag when scrolling, clicking, or typing.
- **Performance**: `EnhancedLog` rewritten to use a single read-only `CTkTextbox` with colored text tags instead of creating one `CTkLabel` per log line. Reduces widget creation from N labels to 1 textbox during token analysis and review progress.
- **Performance**: Token analysis callbacks batched — the background thread accumulates filenames and schedules a single `after(0)` callback via `log_file_batch()` instead of one per file.

### v1.1.0

- Collapsible finding cards: click a card header to minimize it to a single line
- Collapse All / Expand All buttons in the findings toolbar
- Pop-out report window: opens automatically when the review completes
- Shared rendering helpers extracted for DRY report rendering between ReportPanel and ReportWindow
- New Review button in report toolbar closes the pop-out window
- ReportWindow added to widgets.py

### v1.0.0

- In-app ReportPanel with expand/collapse full-screen mode
- All custom widgets extracted into `widgets.py`
- Moved `gui.py` into `src/` package
- No file output (no report.docx, findings.json, raw_response.txt, etc.)
- No CLI mode, debug mode, or output folder picker
- No executable build (PyInstaller removed)
- Updated system prompt (richer severity definitions, cross-discipline coordination, CRITICAL CHECKS section)
- Hardcoded to Claude Opus 4.6
- Simplified `pipeline.py` to return in-memory `PipelineResult` only

## Copyright Notice

**Copyright © 2025 Abraham Borg. All Rights Reserved.**

This software and associated documentation files (the "Software") are the proprietary property of Abraham Borg. 

**Unauthorized copying, modification, distribution, or use of this Software, via any medium, is strictly prohibited without express written permission from the copyright holder.**

This Software is provided for review and reference purposes only. No license or right to use, copy, modify, or distribute this Software for any purpose, commercial or non-commercial, is granted.