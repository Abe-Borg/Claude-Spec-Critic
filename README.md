# Spec Critic v1.8.2

A desktop tool that reviews mechanical and plumbing construction specifications for California K-12 DSA projects using Claude. Load `.docx` spec files, run the review, and see color-coded findings rendered in the app or exported to a Word document.

## What It Does

1. Extracts text from `.docx` specification files (paragraphs + tables)
2. Detects LEED references and unresolved placeholders locally (no API call needed)
3. Performs pre-flight token analysis with an animated visual gauge
4. Reviews each spec independently via streaming API calls to the selected model (Opus 4.6 or Sonnet 4.6)
5. Deduplicates findings that appear across multiple specs
6. Optionally runs a cross-spec coordination check (Sonnet 4.6) to catch inter-spec conflicts
7. Verifies all CRITICAL/HIGH/MEDIUM findings via web search (Sonnet 4.6)
8. Outputs results based on user selection:
   - **View in App**: Renders a full report in-app with a pop-out window
   - **Export Report**: Saves a formatted Word document (.docx) with all findings

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

1. `spec_critic_api_key.txt` file in the Spec Critic config directory (`%LOCALAPPDATA%\SpecCritic` on Windows)
2. `spec_critic_api_key.txt` file in the project root (legacy location)
3. `ANTHROPIC_API_KEY` environment variable
4. Manual entry in the API Key field within the app

For day-to-day use, drop a `spec_critic_api_key.txt` file in the project root or the config directory. The app will auto-load it on launch.

## How to Use

1. Launch the app with `python main.py`
2. Enter your API key (or let it auto-load from file)
3. Click **Browse** to select `.docx` specs
4. (Optional) Enter project context in the **Project Context** field
5. Select the **Review Model**: Opus 4.6 (thorough) or Sonnet 4.6 (fast/cheap)
6. (Optional) Check **Cross-spec coordination check** to enable inter-spec analysis
7. Select the **Output** mode: View in App or Export Report
8. The token gauge fills to show capacity usage — the run is blocked only if any single file exceeds the 150k per-call limit
9. Expand the **FILES** panel to check/uncheck individual specs if needed
10. Click **Run Review** (or **Submit Batch** in batch mode)
11. When complete:
    - **View in App**: The report renders in-app and a pop-out window opens automatically
    - **Export Report**: A save dialog appears — choose where to save the `.docx` report

### Output Mode

Choose how you want to receive the review results:

- **View in App** (default): Results render in the app as interactive collapsible cards with a pop-out report window. Best for small reviews (1-5 specs).
- **Export Report**: Results are saved to a Word document (.docx) without rendering in the app. Best for large reviews where in-app rendering would be slow. The exported report contains everything the in-app report shows.

### Review Model Selection

Choose between two models for the first-stage review:

- **Opus 4.6** (default): Most thorough analysis. Recommended for final reviews, DSA submittals, and when you want the highest quality findings.
- **Sonnet 4.6**: Faster and cheaper. Good for quick reviews, draft specs, or when you're iterating on spec content.

Verification and cross-spec coordination checks always use Sonnet 4.6 regardless of your model selection.

### Cross-Spec Coordination Check

The cross-spec coordination check is an optional pass that runs after the per-spec reviews. When enabled (via the checkbox in the INPUTS card), it sends a condensed summary of all specs — section headers and existing findings — to Claude Sonnet 4.6 in a single API call.

**What it catches:**
- Cross-references to specs that aren't in the submitted set
- Contradictory values across specs (e.g., different CHW supply temperatures)
- Division-of-work gaps (scope items not covered by any spec)
- Division-of-work overlaps (both specs claim the same scope)
- Inconsistent terminology across specs
- Equipment schedule conflicts between specs
- Missing coordination sections

**When to use it:**
- When reviewing 2+ specs from the same project
- When mechanical and plumbing specs are both included
- When you want to check that specs reference each other correctly

**How it works:**
- Uses **Sonnet 4.6** (cheaper and faster than Opus)
- Sends section headers + existing findings (not full spec text) to keep token usage low
- Findings appear in a dedicated **CROSS-SPEC COORDINATION** section in the report
- Coordination findings go through web search verification like any other finding
- Requires 2+ specs — automatically skipped with only 1 spec
- Gracefully skips if the condensed input exceeds the token limit
- Works in both real-time and batch mode (v1.8.2)
- Not available when resuming a batch after app restart (spec content not preserved in state file)

### Project Context

The Project Context field is an optional free-text area where you can describe your project. For example:

- "New 2-story elementary school, 45,000 SF, gas heat pumps, VRF for admin wing"
- "TI of existing middle school gymnasium, replacing RTUs"
- "New K-8 school, central plant with CHW/HHW, classroom unit ventilators"

When provided, this context is included in the message sent to Claude so the reviewer can tailor its analysis to your specific project.

### Confidence Scores

Each finding includes a confidence score (0.0–1.0) indicating how certain the model is about that issue:

- **Green (85–100%)** — High confidence
- **Amber (60–84%)** — Moderate confidence
- **Red (below 60%)** — Low-moderate confidence

Findings below 35% confidence are not created — the model mentions these in the analysis summary instead.

### Finding Deduplication

When reviewing multiple specs, the same issue often appears across several files. The deduplication engine consolidates these into a single finding that lists all affected files.

### Collapsible Finding Cards

Each finding card has a clickable header. Click to collapse/expand. Use **Collapse All** and **Expand All** buttons to toggle all cards at once.

### Pop-Out Report Window

When the review finishes in "View in App" mode, a separate report window opens automatically with the full results.

### Batch Mode

Batch mode submits all specs as a single Anthropic Message Batch at 50% cost. Both the review stage and the verification stage are batched for maximum cost savings. The cross-spec coordination check (if enabled) runs as a real-time call between the two batches.

**Persistent batch state**: If you close the app while a batch is processing, the batch state is saved. When you reopen the app, a dialog offers to resume polling or discard the batch. Batch state expires after 24 hours.

**Terminal failure handling**: If a batch enters a terminal failure state (failed, expired, or canceled), polling stops automatically and the user is informed. No infinite polling loops.

**Note**: When resuming a batch after app restart, the cross-spec coordination check is not available (spec content is not preserved in the state file).

### Verification

All CRITICAL, HIGH, and MEDIUM findings are automatically verified by Sonnet 4.6 with web search. This includes both per-spec findings and cross-check coordination findings.

In batch mode, verification is also batched via the Batches API for 50% cost savings. If batch verification fails (submission error or terminal batch state), it falls back to sequential verification automatically.

**Verdict meanings:**
- **CONFIRMED** (green) — Finding is correct
- **CORRECTED** (amber) — Right idea, wrong details — correction provided
- **DISPUTED** (red) — Finding appears incorrect
- **UNVERIFIED** (gray) — Could not find evidence either way

### Report Panel

After review (in "View in App" mode), the report renders with:
- **Summary grid**: Severity counts plus cross-check count (if applicable)
- **Alerts**: LEED references and placeholders detected locally
- **Findings**: Per-spec findings grouped by severity, sorted by confidence
- **Cross-Spec Coordination**: Dedicated section for coordination findings (cyan accent)
- **Reviewer's Notes**: Claude's personality-driven analysis summary

### Exported Report (.docx)

When using "Export Report" mode, the Word document contains the same information with clean Word-native formatting:
- **Title**: Centered heading with generation metadata
- **Files Reviewed**: Bullet list of all spec filenames
- **Summary table**: Table Grid style with color-coded severity cell shading
- **Token usage & time**: Separate labeled lines
- **Alerts**: LEED references and placeholders with sub-headings and bullet lists
- **Findings**: Grouped by severity (colored sub-headings), sorted by confidence, with structured labeled rows per finding (Section, Issue, Action, Existing Text in red, Replace With in green, Reference in blue, Verification verdict)
- **Cross-Spec Coordination**: Own page with findings and coordination summary (if cross-check was enabled)
- **Reviewer's Notes**: Own page with italic subtitle and multi-paragraph analysis summary

### Export Options

- **Export Report**: Full Word document via the Output mode selector (before review)
- **Export JSON**: Saves findings, cross-check findings, alerts, and metadata to `.json` (after review)
- **Copy Summary**: Copies the analysis summary to clipboard (after review)

## Project Structure

```
spec-review/
├── src/
│   ├── __init__.py        # Package version
│   ├── gui.py             # Main application window
│   ├── widgets.py         # Custom UI widgets
│   ├── pipeline.py        # Core orchestration (single source of truth)
│   ├── report_exporter.py # Word document report generation
│   ├── cross_checker.py   # Cross-spec coordination check (Sonnet 4.6)
│   ├── batch.py           # Anthropic Message Batches API (review + verification)
│   ├── verifier.py        # Web search verification (Sonnet 4.6)
│   ├── extractor.py       # DOCX text extraction
│   ├── preprocessor.py    # LEED/placeholder detection
│   ├── tokenizer.py       # Token counting with tiktoken
│   ├── prompts.py         # System prompt for Claude
│   └── reviewer.py        # Anthropic API client with streaming
├── main.py                # Entry point
├── pyproject.toml         # Project metadata & dependencies
└── README.md
```

## Architecture

### Design Decisions

- **Single pipeline**: All workflow logic lives in `pipeline.py`.
- **User-selectable review model**: Opus 4.6 or Sonnet 4.6 for the first-stage review.
- **User-selectable output mode**: View in App or Export Report (.docx).
- **Sonnet for support tasks**: Verification and cross-check always use Sonnet 4.6.
- **Verification batching**: In batch mode, verification is also batched for 50% savings.
- **Persistent batch state**: Batch state survives app restarts via `batch_state.json` in user state directory.
- **Terminal batch handling**: Failed/expired/canceled batches stop polling immediately.
- **No document mutation**: Analysis only. Document cleanup belongs in SpecCleanse.
- **Advisory only**: This tool assists human reviewers. Not an AHJ substitute.
- **Cross-check is optional**: Controlled by checkbox, default off. Works in both real-time and batch mode.
- **Cross-check is separate**: Dedicated report section, not mixed with per-spec findings.
- **Export report is separate**: `report_exporter.py` accepts `PipelineResult` — pipeline is output-agnostic.
- **Word-native formatting**: Export uses real heading styles, Table Grid, List Bullet, and Arial 11pt.
- **Per-file token gating**: Run is blocked only if any single file exceeds the per-call limit, not by total across files.
- **Robust JSON parsing**: Sentinel tags with heuristic fallback for reliable extraction.
- **Frozen build support**: Config and state files stored in user-writable directories via `platformdirs`.

## What Claude Reviews

The system prompt instructs Claude to check:

- Code compliance: CBC, CMC, CPC, California Energy Code, CALGreen
- DSA-specific requirements: seismic restraint, certification, submittals
- ASHRAE, SMACNA, ASPE, NFPA, MSS, ASTM standards
- Product specifications, internal consistency, cross-spec coordination
- Constructability issues

Claude classifies findings into four severity levels with confidence scores:

- **CRITICAL**: DSA rejection risks, code violations, safety hazards
- **HIGH**: Significant technical errors, coordination conflicts
- **MEDIUM**: Wrong code years, discontinued products, minor inconsistencies
- **GRIPES**: Typos, formatting, overly restrictive requirements

## Dependencies

```
anthropic          # Claude API client
python-docx        # DOCX text extraction + report export
tiktoken           # Token counting (cl100k_base encoding)
customtkinter      # Modern themed Tkinter widgets
platformdirs       # OS-appropriate config/state directories
```

## Changelog

### v1.8.2 — Reliability, Correctness, and UX Fixes

- **Fix**: File dialogs now use `tkinter.filedialog` instead of `ctk.filedialog` — Browse, Export JSON, and Export Report dialogs no longer crash with `AttributeError`
- **Fix**: Batch polling handles terminal failure states (`failed`, `expired`, `canceled`) — no more infinite polling loops
- **Fix**: Verification batch polling also handles terminal states — falls back to sequential verification on failure
- **Fix**: Exiting report mode now restores the log with `fill="both", expand=True` — log properly fills available space
- **Fix**: Batch results now report accurate elapsed time using the batch creation timestamp instead of hardcoded 0.0
- **Fix**: Cross-spec coordination check now works in batch mode — `ExtractedSpec` objects are stored during token analysis and passed to `collect_batch_results()`
- **Fix**: Token gating uses per-file logic — multiple medium files no longer falsely blocked when each fits within the per-call limit; over-limit files are identified by name
- **Improvement**: JSON parsing uses `<FINDINGS_JSON>` sentinel tags for reliable extraction with heuristic fallback for backward compatibility
- **Improvement**: Config and state files (API key, batch state) stored in OS-appropriate user-writable directories via `platformdirs` — frozen PyInstaller builds no longer silently fail to persist data
- **Improvement**: Batch state save failures are logged instead of silently swallowed
- **Improvement**: Unknown batch statuses are handled defensively with a warning instead of silent continuation
- **Dependency**: Added `platformdirs` for cross-platform config/state directory resolution

### v1.8.1 — Restyled Word Report

- **Improvement**: Report now uses Word-native formatting — real heading styles (`doc.add_heading()`), `'Table Grid'` table style, `'List Bullet'` style, and Arial 11pt default font
- **Improvement**: Added "Files Reviewed" section with bullet list of spec filenames
- **Improvement**: Summary table uses colored cell shading with Table Grid borders for professional appearance
- **Improvement**: Token usage and processing time displayed as separate labeled lines instead of cramped metadata
- **Improvement**: Alerts section uses proper Word sub-headings and bullet lists
- **Improvement**: Each finding uses structured labeled rows on separate lines (Section, Issue, Action, Existing Text in red, Replace With in green, Reference in blue) — much more scannable
- **Improvement**: Finding headers show numbered index + severity badge + confidence + filename
- **Improvement**: Cross-check section and Reviewer's Notes each start on their own page with italic subtitles
- **Improvement**: Multi-paragraph narrative text properly split on double newlines
- **Improvement**: Removed hacky thin-rule border separators between findings — clean spacing instead
- **Improvement**: Margins widened from 0.8" to 1.0" sides for better readability
- **Removed**: Generic `_add_paragraph()` and `_add_run()` helpers replaced with purpose-built functions

### v1.8.0 — Export Report to Word Document

- **Feature**: Output mode selector — users can choose between "View in App" (in-app rendering) and "Export Report" (saves .docx file) via a segmented button in the INPUTS card
- **Feature**: New `report_exporter.py` module generates formatted Word documents from PipelineResult data
- **Feature**: Exported report contains everything the in-app report shows: summary table, alerts, per-spec findings, verification verdicts, cross-check findings, reviewer's notes
- **Feature**: Color-coded severity indicators in the Word document (cell shading + colored text)
- **Feature**: Save dialog with default filename when export completes
- **Performance**: Export mode skips all in-app widget rendering — no GUI freezing for large reviews
- **Update**: GUI INPUTS card reorganized: Row 5 = Output, Row 6 = Options
- **Update**: `CLAUDE.md` and `README.md` fully updated for v1.8.0

### v1.7.0 — Verification Batching + Model Selection + Persistent Batch State

- **Feature**: Review model selection — users can choose between Opus 4.6 (thorough) and Sonnet 4.6 (fast/cheap) for the first-stage review via a segmented button in the INPUTS card
- **Feature**: Verification batching — in batch mode, verification requests are submitted as a second Anthropic Message Batch (50% cost savings), with automatic fallback to sequential if batch submission fails
- **Feature**: Persistent batch state — batch metadata is saved to `batch_state.json` on submission and loaded on app launch, enabling resume after app restart
- **Feature**: Resume dialog on launch — if a pending batch is detected, a dialog offers Resume or Discard options with batch ID, file count, model, and age information
- **Feature**: 24-hour batch state expiry — stale state files are automatically discarded
- **Update**: `reviewer.py` exports `MODEL_SONNET_46` and `REVIEW_MODELS` dict for GUI model selector
- **Update**: `batch.py` gains `submit_verification_batch()` and `retrieve_verification_results()` for verification batching
- **Update**: `verifier.py` gains `verify_findings_batch()` as batch-mode alternative to `verify_findings()`
- **Update**: `pipeline.py` `collect_batch_results()` uses `verify_findings_batch()` for verification in batch mode
- **Update**: `pipeline.py` `BatchSubmission` gains `model` field
- **Update**: Model parameter flows through `run_review()`, `start_batch_review()`, `review_single_spec()`
- **Update**: GUI INPUTS card reorganized: Row 3 = Review Model, Row 4 = Mode, Row 5 = Options
- **Update**: Header subtitle simplified (no longer hardcodes model name)
- **Update**: `batch_state.json` added to `.gitignore`

### v1.6.0 — Cross-Spec Coordination Check

- **Feature**: Optional cross-spec coordination check — after per-spec reviews, a single Sonnet 4.6 call analyzes section headers and existing findings to catch inter-spec coordination issues
- **Feature**: New `cross_checker.py` module with dedicated system prompt and condensed input builder
- **Feature**: "Cross-spec coordination check" checkbox in the INPUTS card
- **Feature**: Dedicated "CROSS-SPEC COORDINATION" section in report with cyan accent
- **Feature**: Cross-check findings included in JSON export and web search verification

### v1.5.0 — Confidence Scoring + Deduplication + Always-On Verification

- **Feature**: Confidence scoring — each finding includes a numeric `confidence` field (0.0–1.0)
- **Feature**: Confidence badges in finding cards (green/amber/red)
- **Feature**: Finding deduplication across specs
- **Feature**: Always-on verification (checkbox removed)
- **Update**: Verification model upgraded to Claude Sonnet 4.6

### v1.4.0 — Per-Spec Siloed Review + Batch Processing

- **Feature**: Per-spec siloed review (one API call per spec)
- **Feature**: Batch mode (50% cost savings via Message Batches API)
- **Feature**: Web search self-verification (Sonnet + web_search tool)
- **Feature**: Determinate progress bar, partial failure resilience

### v1.3.0

- **Feature**: Project Context text field
- **Fix**: Activity Log collapse space reclamation
- **Rename**: App renamed to "Spec Critic"

### v1.2.0

- **Performance**: Reduced animation frame rates
- **Performance**: EnhancedLog rewritten with single CTkTextbox
- **Performance**: Batched token-analysis callbacks

### v1.1.0

- Collapsible finding cards
- Pop-out report window

### v1.0.0

- Initial release with in-app ReportPanel

## Copyright Notice

**Copyright © 2025 Abraham Borg. All Rights Reserved.**

This software and associated documentation files (the "Software") are the proprietary property of Abraham Borg.

**Unauthorized copying, modification, distribution, or use of this Software, via any medium, is strictly prohibited without express written permission from the copyright holder.**