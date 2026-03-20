# Spec Critic v2.4.1

A desktop tool that reviews mechanical and plumbing construction specifications for California K-12 DSA projects using Claude. Load `.docx` spec files, run the review, and see color-coded findings rendered in the app or exported to a Word document.

## Changelog

### v2.4.1

- Fixed: Verification now accumulates web search evidence across multi-turn `pause_turn` continuations instead of only checking the final turn.
- Fixed: Cross-check dedup context is correctly populated again because verification now returns real verdicts instead of cascading all findings to `UNVERIFIED`.
- Added: Batch verification now retries `pause_turn` findings via real-time verification, capped at 20 findings.

## What It Does

1. Extracts text from `.docx` specification files (paragraphs + tables)
2. Detects LEED references and unresolved placeholders locally (no API call needed)
3. Performs pre-flight token analysis with an animated visual gauge
4. Reviews each spec independently via streaming API calls (Opus 4.6)
5. Deduplicates findings that appear across multiple specs
6. Optionally runs a cross-spec coordination check (Opus 4.6) to catch inter-spec conflicts
7. Verifies all findings (CRITICAL/HIGH/MEDIUM/GRIPES) via web search (Opus 4.6)
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
# or
pip install -r requirements.txt

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
5. Review model is **Opus 4.6** (currently fixed)
6. (Optional) Check **Cross-spec coordination check** to enable inter-spec analysis
7. Select the **Output** mode: View in App or Export Report
8. The token gauge fills to show capacity usage — the run is blocked only if any single file exceeds the 500k per-call limit
9. Expand the **FILES** panel to check/uncheck individual specs if needed
10. Click **Run Review**
11. When complete:
    - **View in App**: The report renders in-app and a pop-out window opens automatically
    - **Export Report**: A save dialog appears — choose where to save the `.docx` report

### Output Mode

Choose how you want to receive the review results:

- **Real-time (FAST: Expensive!)**: Runs per-spec streaming review calls. Selecting this mode prompts a confirmation dialog warning about higher cost, with a one-click option to switch to batch.
- **Batch (SLOW: Cheap!)**: Submits the review and verification phases through the Anthropic Message Batches API for lower cost.
- **View in App** (default output): Results render in the app as interactive collapsible cards with a pop-out report window.
- **Export Report**: Results are saved to a Word document (.docx) without rendering in the app.

### Code Cycle

Select the California code cycle (2022 or 2025) via the segmented button in the INPUTS card. The default is 2025. This controls which code edition references (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) the reviewer uses when checking specifications.

### Review Model

The first-stage review currently uses **Opus 4.6**.
Verification and cross-spec coordination checks also use **Opus 4.6**.

### Cross-Spec Coordination Check

The cross-spec coordination check is an optional pass that runs after the per-spec reviews. When enabled (via the checkbox in the INPUTS card), it sends the combined full spec text and existing findings to Claude Opus 4.6 in a single API call.

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
- Uses **Opus 4.6** with adaptive thinking
- Sends all submitted spec content and existing findings to the model
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

**Persistent batch state**: Resume is durable across review-poll, review-collect, verification-poll, and finalize phases using `resume_state.py` serialization. Batch state expires after 24 hours.

**Terminal failure handling**: If a batch enters a terminal failure state (failed, expired, or canceled), polling stops automatically and the user is informed. No infinite polling loops.

**Note**: Cross-check requires preserved extracted spec content; if unavailable in resume payload, cross-check is skipped safely with an explicit status.

### Verification

All findings (CRITICAL, HIGH, MEDIUM, and GRIPES) are automatically verified by Opus 4.6 with web search. This includes both per-spec findings and cross-check coordination findings.

In batch mode, verification is also batched via the Batches API for 50% cost savings. If batch verification fails (submission error or terminal batch state), it falls back to sequential verification automatically.

**Verdict meanings:**
- **CONFIRMED** (green) — Finding is correct
- **CORRECTED** (amber) — Right idea, wrong details — correction provided
- **DISPUTED** (red) — Finding appears incorrect
- **UNVERIFIED** (gray) — Could not find evidence either way

### Report Window

After review (in "View in App" mode), the report renders in a pop-out window with:
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
- **Token totals note**: Displayed token totals currently reflect the review stage unless otherwise noted.
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
│   ├── cross_checker.py   # Cross-spec coordination check (Opus 4.6)
│   ├── batch.py           # Anthropic Message Batches API (review + verification)
│   ├── code_cycles.py     # California code cycle definitions (2022/2025)
│   ├── verifier.py        # Web search verification (Opus 4.6)
│   ├── extractor.py       # Text extraction (DOCX-only)
│   ├── preprocessor.py    # LEED/placeholder detection
│   ├── tokenizer.py       # Token counting with tiktoken
│   ├── prompts.py         # System + user prompt builders
│   ├── reviewer.py        # Anthropic API client with streaming
│   ├── resume_state.py    # Durable batch resume-state serialization
├── main.py                # Entry point
├── pyproject.toml         # Project metadata & dependencies
└── README.md
```

## Architecture

### Design Decisions

- **Single pipeline**: All workflow logic lives in `pipeline.py`.
- **DOCX-only extraction**: `extractor.py` supports `.docx` via a dispatcher — downstream modules only see `ExtractedSpec`.
- **Review model**: Opus 4.6 for the first-stage review.
- **User-selectable output mode**: View in App or Export Report (.docx).
- **Opus for support tasks**: Verification and cross-check use Opus 4.6.
- **Verification batching**: In batch mode, verification is also batched for 50% savings.
- **Persistent batch state**: Durable resume-state persists review and verification phases via `resume_state.py`.
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
- **DOCX-only input**: `.pdf` extraction and `pymupdf` dependency were removed in v2.3.0.
- **Fault-tolerant verification**: Individual verification failures cannot crash the remaining findings (v1.9.1). Pipeline-level try/except added in v2.0.0.
- **All findings verified**: GRIPES findings are now verified like any other severity (v2.0.0).
- **Bounded polling**: Both GUI and verification batch polling have maximum attempt limits (v2.0.0).

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

### v2.3.0 — DOCX-only extraction + UX clarity updates

- **Feature**: Real-time mode cost confirmation dialog with one-click switch to batch mode
- **Feature**: Updated mode labels to **Real-time (FAST: Expensive!)** and **Batch (SLOW: Cheap!)**
- **Removed**: PDF support removed — extractor now supports `.docx` only
- **Removed**: `pymupdf` dependency removed
- **Docs**: TokenGauge label updated to **LARGEST SPEC CAPACITY**

### v2.2.0 — Code cycles + phased batch resume architecture

- **Feature**: Code cycle selector (2022 / 2025) added to INPUTS card
- **Feature**: New `code_cycles.py` module with `CodeCycle`, `CALIFORNIA_2022`, and `CALIFORNIA_2025`
- **Feature**: New `resume_state.py` module for durable batch resume-state serialization/deserialization
- **Feature**: Phased batch pipeline APIs: review collect, cross-check phase, verification prep/start/collect, and finalize
- **Feature**: Batch resume expanded beyond review polling to verification-poll and finalize phases
- **Improvement**: Prompt and verification code references parameterized by selected cycle
- **Improvement**: Cross-check token budget refined to `CROSS_CHECK_RECOMMENDED_MAX = 822,000`

### v2.1.0 — Robustness, Correctness, and Quality-of-Life Improvements

- **Fix**: Prompt example now wraps JSON in `<FINDINGS_JSON>` sentinel tags matching the instructions — improves JSON parsing reliability
- **Fix**: GUI and pipeline token gate use identical math via shared `exceeds_per_call_limit()` — eliminates files passing GUI gate but rejected at runtime
- **Fix**: Stale file selection cleared when new analysis starts — prevents reviewing files from a previous selection if all new files fail extraction
- **Fix**: Batch state cleared on polling timeout — prevents stale resume dialog on next app launch
- **Fix**: Batch status string normalized (hyphens to underscores) — fixes "unexpected batch status: in-progress" log spam
- **Fix**: `_on_review_error()` clears batch state as safety net
- **Fix**: Broader DOCX error handling — catches `BadZipFile` and other exceptions beyond `PackageNotFoundError`
- **Fix**: Empty specs (zero extractable text) skipped in pipeline instead of wasting API calls
- **Fix**: Finding numbering in exported reports now sequential across all severity groups (1, 2, 3...) instead of restarting per group
- **Fix**: Findings with empty issue text dropped during parsing
- **Fix**: Cross-checker scope excerpts bounded by next PART header — prevents Part 1 excerpts bleeding into Part 2
- **Fix**: Thread-safe attribute writes in GUI token analysis — marshaled through `self.after()`
- **Fix**: `Finding.verification` type annotation corrected to `VerificationResult | None`
- **Improvement**: Extraction failures per-file are now fault-tolerant — one corrupted file no longer aborts the entire run
- **Improvement**: Placeholder patterns now case-insensitive — catches `[insert]`, `[Verify]`, `<Edit>` etc.
- **Improvement**: Dedup key includes `actionType` — prevents merging ADD and EDIT findings with similar wording
- **Improvement**: Verification prompt tightened for brevity — responses truncated to 500 chars
- **Improvement**: Verification response parser has natural-language fallback for non-JSON responses
- **Improvement**: Verification batch status polling uses normalized status strings
- **Improvement**: JSON export has error handling — shows dialog on write failure
- **Improvement**: "New Review" button added to header bar — wired to `_reset_for_new_review()`
- **Removed**: Dead `_should_verify()` function (always returned True)
- **Docs**: `cancel_batch()` return type corrected to `str` in CLAUDE.md
- **Docs**: `exceeds_per_call_limit()` and `PER_CALL_PADDING` documented in CLAUDE.md

### v2.0.0 — Correctness, Parse Hardening, Extraction Fidelity, and Cleanup

- **Fix**: `pyproject.toml` build-backend changed from invalid `setuptools.backends._legacy:_Backend` to `setuptools.build_meta` — `pip install -e .` now works
- **Fix**: DOCX extraction preserves document body order — paragraphs and tables interleaved correctly via `doc.element.body` iteration instead of two-pass approach
- **Fix**: Loose "no issues" heuristic removed from JSON parser — only `"[]"` treated as empty, preventing false negatives when model text happens to contain "no issues"
- **Fix**: Parse fallback (Strategy 2) validates bracket structure before `json.loads()` — prevents garbage parses
- **Fix**: Field validation in `_parse_findings()` — invalid severity skips finding, invalid actionType defaults to EDIT, text fields coerced to str
- **Fix**: GRIPES findings now verified in both real-time and batch mode — removed exclusion filter in pipeline
- **Fix**: Verification failures wrapped in try/except at pipeline level — errors produce UNVERIFIED verdicts instead of crashing
- **Fix**: Export cancel/failure now falls back to pop-out ReportWindow instead of losing results
- **Fix**: Widget state snapshotted before threads — eliminates thread-unsafe widget reads
- **Fix**: Combined-total token gate removed — only per-file gating remains (as documented)
- **Fix**: LEED detection deduplication — specific patterns (LEED-NC) no longer produce duplicate generic LEED alerts
- **Improvement**: Cross-checker input enriched with key numeric values and cross-references per spec
- **Improvement**: Cross-checker header patterns tightened — excludes body text lines containing "shall"/"must"/etc.
- **Improvement**: Cross-checker prompt adds anti-hallucination guidance (cite specific values, don't infer from absence)
- **Improvement**: Batch verification polling bounded (`max_poll_attempts=240`) with fallback to sequential after 5 consecutive errors
- **Improvement**: GUI batch polling bounded (max 300 attempts, 5 consecutive error limit)
- **Improvement**: Batch ID validated on resume (must start with "msgbatch_")
- **Improvement**: UNVERIFIED verdicts now displayed in both in-app and exported reports
- **Improvement**: `retrieve_review_results()` surfaces parse failures as error messages instead of silently swallowing
- **Removed**: Dead `review_specs()` function from `reviewer.py`
- **Removed**: Dead `_combine_specs()` function from `pipeline.py`
- **Removed**: Dead `preprocess_specs()` function from `preprocessor.py`
- **Removed**: Dead `ReportPanel` class and report expand/collapse mode from `widgets.py` and `gui.py`
- **Breaking**: `retrieve_review_results()` `model` parameter is now required (keyword-only)

### v1.9.1 — Verification Reliability Fix

- **Fix**: `verify_finding()` now catches all exception types (not just Anthropic-specific ones) — unexpected errors during a single verification no longer crash the entire pipeline
- **Fix**: `verify_findings()` sequential loop wraps each finding in its own try/except — a single verification failure cannot abort the remaining findings; previously, an uncaught exception from finding #3 would lose all work (findings #1–#2 verified, #4–#N never attempted, PipelineResult never constructed)

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

- Initial release

## Copyright Notice

**Copyright © 2025 Abraham Borg. All Rights Reserved.**

This software and associated documentation files (the "Software") are the proprietary property of Abraham Borg.

**Unauthorized copying, modification, distribution, or use of this Software, via any medium, is strictly prohibited without express written permission from the copyright holder.**
