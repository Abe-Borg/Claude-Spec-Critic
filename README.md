# Spec Critic v1.6.0

A desktop tool that reviews mechanical and plumbing construction specifications for California K-12 DSA projects using Claude Opus 4.6. Load `.docx` spec files, run the review, and see color-coded findings rendered directly in the app.

## What It Does

1. Extracts text from `.docx` specification files (paragraphs + tables)
2. Detects LEED references and unresolved placeholders locally (no API call needed)
3. Performs pre-flight token analysis with an animated visual gauge
4. Reviews each spec independently via streaming API calls to Claude Opus 4.6
5. Deduplicates findings that appear across multiple specs
6. Optionally runs a cross-spec coordination check (Sonnet 4.6) to catch inter-spec conflicts
7. Verifies all CRITICAL/HIGH/MEDIUM findings via web search (Sonnet 4.6)
8. Renders a full report in-app: summary grid, alerts, per-spec findings, coordination findings, reviewer's notes
9. Opens the report in a separate pop-out window for dedicated viewing

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
4. (Optional) Enter project context in the **Project Context** field
5. (Optional) Check **Cross-spec coordination check** to enable inter-spec analysis
6. The token gauge fills to show capacity usage — stay under the 150k limit
7. Expand the **FILES** panel to check/uncheck individual specs if needed
8. Click **Run Review**
9. When complete, the report renders in-app and a **pop-out report window** opens automatically
10. Click **Expand** to view the in-app report full-screen, or **← Back to Review** to return

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

When the review finishes, a separate report window opens automatically with the full results.

### Batch Mode

Batch mode submits all specs as a single Anthropic Message Batch at 50% cost. The cross-spec coordination check is not batched — it runs as a real-time call after batch results are collected.

### Verification

All CRITICAL, HIGH, and MEDIUM findings are automatically verified by Sonnet 4.6 with web search. This includes both per-spec findings and cross-check coordination findings.

**Verdict meanings:**
- **CONFIRMED** (green) — Finding is correct
- **CORRECTED** (amber) — Right idea, wrong details — correction provided
- **DISPUTED** (red) — Finding appears incorrect
- **UNVERIFIED** (gray) — Could not find evidence either way

### Report Panel

After review, the report renders with:
- **Summary grid**: Severity counts plus cross-check count (if applicable)
- **Alerts**: LEED references and placeholders detected locally
- **Findings**: Per-spec findings grouped by severity, sorted by confidence
- **Cross-Spec Coordination**: Dedicated section for coordination findings (cyan accent)
- **Reviewer's Notes**: Claude's personality-driven analysis summary

### Export Options

- **Export JSON**: Saves findings, cross-check findings, alerts, and metadata to `.json`
- **Copy Summary**: Copies the analysis summary to clipboard

## Project Structure

```
spec-review/
├── src/
│   ├── __init__.py        # Package version
│   ├── gui.py             # Main application window
│   ├── widgets.py         # Custom UI widgets
│   ├── pipeline.py        # Core orchestration (single source of truth)
│   ├── cross_checker.py   # Cross-spec coordination check (Sonnet 4.6)
│   ├── batch.py           # Anthropic Message Batches API
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
- **Single review model**: Hardcoded to Claude Opus 4.6 for review.
- **Sonnet for support tasks**: Verification and cross-check use Sonnet 4.6.
- **No document mutation**: Analysis only. Document cleanup belongs in SpecCleanse.
- **No file output**: All results render in-app. Only Export JSON writes files.
- **Advisory only**: This tool assists human reviewers. Not an AHJ substitute.
- **Cross-check is optional**: Controlled by checkbox, default off.
- **Cross-check is separate**: Dedicated report section, not mixed with per-spec findings.

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
python-docx        # DOCX text extraction
tiktoken           # Token counting (cl100k_base encoding)
customtkinter      # Modern themed Tkinter widgets
```

## Changelog

### v1.6.0 — Cross-Spec Coordination Check

- **Feature**: Optional cross-spec coordination check — after per-spec reviews, a single Sonnet 4.6 call analyzes section headers and existing findings to catch inter-spec coordination issues (contradictions, missing references, division-of-work gaps, terminology conflicts)
- **Feature**: New `cross_checker.py` module with `run_cross_check()`, `extract_section_headers()`, dedicated system prompt, and condensed input builder
- **Feature**: "Cross-spec coordination check" checkbox in the INPUTS card (Row 4) with hint label ("Sonnet 4.6 • finds inter-spec conflicts")
- **Feature**: Dedicated "CROSS-SPEC COORDINATION" section in report with cyan accent, separate from per-spec findings
- **Feature**: Cross-check count card in summary grid (cyan, only shown when cross-check produces findings)
- **Feature**: Cross-check findings included in JSON export (`cross_check_findings` and `cross_check_summary` fields)
- **Feature**: Cross-check findings go through web search verification alongside per-spec findings
- **Feature**: Graceful skip when <2 specs or token limit exceeded
- **Update**: `PipelineResult` gains `cross_check_result` field
- **Update**: `ReportWindow` and `ReportPanel` accept and render `cross_check_result`
- **Update**: `pipeline.run_review()` and `pipeline.collect_batch_results()` accept `cross_check` parameter
- **Update**: Progress allocation adjusted: review 35-55%, cross-check 55-65%, verification 65-95%

### v1.5.0 — Confidence Scoring + Deduplication + Always-On Verification

- **Feature**: Confidence scoring — each finding includes a numeric `confidence` field (0.0–1.0)
- **Feature**: Confidence badges in finding cards (green/amber/red)
- **Feature**: Confidence-based sorting within severity tiers
- **Feature**: Confidence-based verification ordering (least confident first)
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