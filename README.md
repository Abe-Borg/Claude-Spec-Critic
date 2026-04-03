# Spec Critic

**AI-powered specification review for California K-12 DSA projects — catch code violations, coordination gaps, and costly errors before they reach the plan check counter.**

## What Is Spec Critic?

Spec Critic is a desktop application that reviews mechanical and plumbing construction specifications against California building codes and industry standards using Claude (Opus 4.6). It reads your `.docx` spec files, analyzes them for code compliance issues, cross-spec coordination problems, and constructability concerns, then presents prioritized, verified findings you can act on immediately.

Manual spec review is slow, inconsistent, and expensive when issues slip through to DSA plan check. A single rejection can delay a project by weeks and cost thousands in re-engineering. Spec Critic doesn't replace your engineering judgment — it augments it by systematically checking every section against current California codes, flagging issues a human reviewer might miss under deadline pressure.

Starting with v2.5, Spec Critic can also **apply fixes directly to your DOCX files**. Select the findings you want to act on, review the proposed edits, and generate corrected spec documents — no manual copy-paste required.

## Who Is This For?

- **Mechanical engineers** reviewing HVAC specifications for DSA submissions
- **Plumbing engineers** checking plumbing specs against CMC and CPC requirements
- **Specification writers** validating specs before they leave the office
- **Architects** coordinating mechanical and plumbing divisions on K-12 projects
- **Construction managers** reviewing submittals and spec packages for compliance gaps

If you work on California K-12 DSA projects and touch Division 23 or Division 22 specifications, this tool is built for you.

## Key Features

### AI-Powered Review
- **Claude Opus 4.6** reviews each spec with extended thinking for thorough analysis
- **California code compliance** — checks against CBC, CMC, CPC, California Energy Code, CALGreen, and ASCE 7
- **Code cycle support** — switch between 2022 and 2025 California code editions
- **Confidence scoring** — every finding includes a 0.0–1.0 confidence score so you know what to trust

### Multi-Spec Intelligence
- **Cross-spec coordination check** — catches contradictions, scope gaps, and broken cross-references across specs
- **Finding deduplication** — consolidates identical issues found across multiple spec files
- **Project context** — describe your project so the reviewer tailors analysis to your specific building

### Verification & Trust
- **Web search verification** — every finding is fact-checked against authoritative sources via web search
- **Four verdicts** — CONFIRMED, CORRECTED, DISPUTED, or UNVERIFIED for full transparency
- **Diagnostics reporting** — detailed pipeline telemetry for auditing and debugging

### Surgical Spec Editing (v2.5+)
- **Edit candidates** — findings are classified as eligible for auto-apply based on action type and verification status
- **Interactive selection** — choose which findings to apply, review proposed changes before committing
- **Three edit actions** — EDIT (in-place replacement), DELETE (remove text), ADD (insert new content)
- **Conflict detection** — overlapping edits in the same paragraph are detected and resolved by severity and confidence
- **Safe output** — edited specs are saved to new files; originals are never modified

### Processing & Output
- **Two processing modes** — Real-time (fast, higher cost) or Batch (slow, 50% cheaper)
- **Multiple output options** — View in App, Export Report (.docx), Export JSON, Copy Summary
- **Batch resume** — durable state survives app restarts; pick up where you left off
- **Verbose/concise toggle** — control how much detail appears in findings
- **Local LEED & placeholder detection** — flags LEED references and unresolved placeholders without an API call

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/Abe-Borg/claude-spec-critic.git
cd claude-spec-critic

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -e .
# or
pip install -r requirements.txt

# 4. Set up your Anthropic API key (see below)

# 5. Run the application
python main.py
```

## API Key Setup

Spec Critic looks for your Anthropic API key in this order (first found wins):

1. **Config directory file** — `spec_critic_api_key.txt` in the Spec Critic config directory (`%LOCALAPPDATA%\SpecCritic` on Windows, `~/.config/SpecCritic` on Linux/macOS)
2. **Project root file** — `spec_critic_api_key.txt` in the project root (legacy location)
3. **Environment variable** — `ANTHROPIC_API_KEY`
4. **Manual entry** — paste directly into the API Key field in the app

For daily use, drop a `spec_critic_api_key.txt` file in either location and the app will auto-load it on launch.

## How to Use

1. **Launch** the app with `python main.py`
2. **API key** — enter it or let it auto-load from file
3. **Browse** — click Browse to select one or more `.docx` spec files
4. **Code cycle** — select 2022 or 2025 via the segmented control in the INPUTS card
5. **Project context** (optional) — describe your project (e.g., "New 2-story elementary school, 45,000 SF, gas heat pumps")
6. **Cross-spec check** (optional) — enable the checkbox if reviewing 2+ related specs
7. **Verbose report** (optional) — leave checked for full detail, uncheck for action-oriented output only
8. **Output mode** — choose View in App or Export Report
9. **Review the token gauge** — it fills to show capacity usage per spec; the run is blocked only if any single file exceeds the 500K per-call limit
10. **File selection** — expand the FILES panel to include/exclude individual specs
11. **Click Run Review** and wait for results
12. **Review findings** — browse color-coded, collapsible finding cards in-app or open the exported Word document
13. **Apply edits** (optional) — select findings to apply, review proposed changes, and generate corrected DOCX files

## Processing Modes

| | Real-time | Batch |
|---|---|---|
| **Speed** | Fast (streaming) | Slow (queued) |
| **Cost** | Full price | 50% discount |
| **How it works** | One streaming API call per spec | All specs submitted as a single Message Batch |
| **Verification** | Sequential API calls | Also batched for additional savings |
| **Best for** | Quick checks on 1-2 specs | Large reviews with many specs |

Selecting Real-time mode prompts a cost confirmation dialog with a one-click option to switch to Batch.

## Output Modes

- **View in App** (default) — results render as interactive collapsible cards in a pop-out report window
- **Export Report** — saves a formatted Word document (.docx) with all findings, severity-coded and structured
- **Export JSON** — saves findings, alerts, and metadata to `.json` (available after review completes)
- **Copy Summary** — copies Claude's analysis summary to your clipboard (available after review completes)

## What Claude Reviews

The system prompt instructs Claude to check specifications against:

- **California codes**: CBC, CMC, CPC, California Energy Code, CALGreen
- **DSA-specific requirements**: seismic restraint, certification, submittals
- **Industry standards**: ASHRAE, SMACNA, ASPE, NFPA, MSS, ASTM, ASCE 7
- **Internal quality**: product specifications, consistency, coordination, constructability

### Severity Levels

- **CRITICAL** — DSA rejection risks, code violations, safety hazards
- **HIGH** — significant technical errors, coordination conflicts
- **MEDIUM** — wrong code years, discontinued products, minor inconsistencies
- **GRIPES** — typos, formatting issues, overly restrictive requirements

### Confidence Scoring

Each finding includes a confidence score (0.0–1.0):

- **Green (85–100%)** — high confidence
- **Amber (60–84%)** — moderate confidence
- **Red (below 60%)** — lower confidence

Findings below 35% confidence are suppressed and mentioned only in the analysis summary.

## Cross-Spec Coordination

When reviewing 2+ specs from the same project, enable the cross-spec coordination check to catch:

- Cross-references to specs not in the submitted set
- Contradictory values across specs (e.g., different CHW supply temperatures)
- Division-of-work gaps (scope items not covered by any spec)
- Division-of-work overlaps (both specs claim the same scope)
- Inconsistent terminology across specs
- Equipment schedule conflicts
- Missing coordination sections

The cross-check runs as a separate Opus 4.6 call after per-spec reviews. Findings appear in a dedicated **CROSS-SPEC COORDINATION** section and go through web search verification like any other finding.

## Verification

All findings (CRITICAL, HIGH, MEDIUM, and GRIPES) are automatically verified via Opus 4.6 with web search against authoritative sources. Non-authoritative sources (forums, social media, AI chatbots, Wikipedia) are blocked.

**Verdict meanings:**

| Verdict | Color | Meaning |
|---|---|---|
| CONFIRMED | Green | Finding is correct |
| CORRECTED | Amber | Right idea, wrong details — correction provided |
| DISPUTED | Red | Finding appears incorrect |
| UNVERIFIED | Gray | Could not find evidence either way |

In batch mode, review and verification stay batch-only. Failed or paused verification items are retried through additional batch waves (retry/continuation) up to a fixed wave limit, then marked UNVERIFIED with explicit reasons.

## Spec Editing

After review, you can apply findings directly to your DOCX files:

1. **Classify** — findings are automatically classified as eligible or ineligible for editing based on action type (EDIT/DELETE/ADD), presence of existing text, and verification status
2. **Select** — an interactive dialog shows all findings with eligibility status; CONFIRMED and CORRECTED findings are pre-selected
3. **Locate** — the edit locator maps each selected finding to its exact position in the document using multi-strategy matching (exact, normalized, fuzzy, section-anchored, cross-paragraph)
4. **Resolve conflicts** — overlapping edits targeting the same paragraph are resolved by severity rank, confidence score, and span size
5. **Apply** — edits are executed safely at the run level without restructuring the document
6. **Output** — edited specs are saved as new files (with `_edited` suffix); originals are never touched
7. **Report** — a detailed edit report shows applied, skipped, and failed edits with explanations

DISPUTED findings are excluded from editing. ADD actions insert new paragraphs with style matching. DELETE actions safely remove entire paragraphs or targeted text spans.

## Batch Mode & Resume

Batch mode submits specs through the Anthropic Message Batches API at 50% cost. Both the review and verification stages are batched.

**Durable state**: Pipeline progress is serialized across phases (review-poll, review-collect, verification-wave-poll, cross-check, cross-check-verification-wave-poll, finalize) using `resume_state.py`. If the app closes mid-batch, a resume dialog appears on next launch offering to continue or discard.

**Batch state expires after 24 hours.** Terminal failures (failed, expired, canceled batches) stop polling immediately.

**Note**: Cross-check requires the original spec content, which is not preserved in the resume state file. If resuming after a restart, cross-check is safely skipped with an explicit status message.

## Project Structure

```
claude-spec-critic/
├── src/
│   ├── __init__.py            # Package version (2.7.0)
│   ├── gui.py                 # CustomTkinter GUI — all user interaction
│   ├── widgets.py             # Reusable UI components
│   ├── pipeline.py            # Core orchestration and phased batch flow
│   ├── report_exporter.py     # Word document (.docx) report generation
│   ├── cross_checker.py       # Cross-spec coordination check (Opus 4.6)
│   ├── batch.py               # Anthropic Message Batches API wrapper
│   ├── verifier.py            # Web search verification (Opus 4.6)
│   ├── verification_config.py # Verification constants and web search config
│   ├── extractor.py           # DOCX text extraction with paragraph mapping
│   ├── preprocessor.py        # Local LEED/placeholder detection
│   ├── tokenizer.py           # Token counting and limits
│   ├── prompts.py             # System + user prompt builders
│   ├── reviewer.py            # Anthropic API client with streaming
│   ├── code_cycles.py         # California code cycle definitions (2022, 2025)
│   ├── resume_state.py        # Durable batch resume-state serialization
│   ├── edit_locator.py        # Multi-strategy finding-to-paragraph matching
│   ├── edit_candidates.py     # Finding eligibility classification for editing
│   ├── apply_edits.py         # Edit plan orchestration (locate → build → apply)
│   ├── spec_editor.py         # Surgical DOCX editor with conflict handling
│   └── diagnostics.py         # In-memory pipeline telemetry and reporting
├── main.py                    # Entry point
├── pyproject.toml             # Project metadata & dependencies
├── requirements.txt           # Pinned dependencies
└── README.md
```

## Dependencies

| Package | Purpose |
|---|---|
| `anthropic` | Claude API client |
| `python-docx` | DOCX text extraction and report/edit export |
| `tiktoken` | Token counting (cl100k_base encoding) |
| `customtkinter` | Modern themed Tkinter widgets |
| `platformdirs` | OS-appropriate config/state directories |
| `lxml` | XML processing for DOCX paragraph-level editing |

## Changelog

### v2.7.0

- **Feature**: Edit locator section-anchored matching for headings
- **Fix**: Targeted locator, conflict resolution, extraction, and diagnostics fixes

### v2.6.0

- **Feature**: Full edit candidate visibility — all findings shown in selection UI with eligibility status
- **Feature**: ADD action auto-apply support in spec editor
- **Feature**: Edit application workflow integrated into review GUI pipeline
- **Feature**: Finding selection and edit application dialogs
- **Feature**: Surgical DOCX spec editor module with conflict handling
- **Feature**: Edit locator module with multi-strategy matching (exact, normalized, fuzzy, section-anchored, cross-paragraph)
- **Feature**: Paragraph mapping metadata added to DOCX extraction
- **Removed**: Rejection triage workflow removed from export and GUI

### v2.5.0

- **Feature**: In-memory diagnostics report with per-run pipeline telemetry
- **Feature**: Findings sorted by filename before severity
- **Feature**: Retry transient Anthropic 500/529 errors across review pipeline
- **Feature**: Batch/real-time recovery semantics aligned with accurate timing
- **Feature**: Collapsible findings in Word export
- **Fix**: Batch verification retries for errored findings
- **Fix**: Verification domain duplication and cross-check summary rendering
- **Fix**: Verification parsing, reporting, and cross-check resume gaps

### v2.4.1

- **Fix**: Verification now accumulates web search evidence across multi-turn `pause_turn` continuations instead of only checking the final turn
- **Fix**: Cross-check dedup context correctly populated — verification returns real verdicts instead of cascading to UNVERIFIED
- **Feature**: Batch verification retries `pause_turn` findings via real-time verification, capped at 20 findings

### v2.3.0

- **Feature**: Real-time mode cost confirmation dialog with one-click switch to batch
- **Feature**: Updated mode labels to Real-time (FAST: Expensive!) and Batch (SLOW: Cheap!)
- **Removed**: PDF support — extractor now supports `.docx` only
- **Removed**: `pymupdf` dependency

### v2.2.0

- **Feature**: Code cycle selector (2022 / 2025)
- **Feature**: `code_cycles.py` module with `CodeCycle`, `CALIFORNIA_2022`, and `CALIFORNIA_2025`
- **Feature**: `resume_state.py` for durable batch resume-state serialization
- **Feature**: Phased batch pipeline APIs for granular resume support

### v2.1.0

- **Fix**: Prompt example JSON wrapped in sentinel tags for reliable parsing
- **Fix**: GUI and pipeline token gate use identical math via shared `exceeds_per_call_limit()`
- **Fix**: Multiple robustness fixes (stale file selection, batch state clearing, DOCX error handling, empty spec skipping, finding numbering, thread safety)
- **Improvement**: Fault-tolerant extraction, case-insensitive placeholder detection, dedup key includes actionType

### v2.0.0

- **Fix**: `pyproject.toml` build-backend corrected — `pip install -e .` now works
- **Fix**: DOCX extraction preserves document body order
- **Fix**: JSON parse hardening (sentinel tags, bracket validation, field validation)
- **Fix**: GRIPES findings now verified; fault-tolerant verification at pipeline level
- **Removed**: Dead code cleanup (`review_specs`, `_combine_specs`, `preprocess_specs`, `ReportPanel`)
- **Breaking**: `retrieve_review_results()` `model` parameter is now keyword-only

### v1.9.1

- **Fix**: Fault-tolerant verification — single failure cannot crash remaining findings

### v1.8.2

- **Fix**: File dialogs use `tkinter.filedialog` (no more crashes)
- **Fix**: Batch polling handles terminal failure states
- **Feature**: `platformdirs` for OS-appropriate config/state directories
- **Feature**: Cross-spec coordination check works in batch mode

### v1.8.1

- **Improvement**: Word-native report formatting (heading styles, Table Grid, List Bullet, Arial 11pt)

### v1.8.0

- **Feature**: Export Report to Word document (.docx)
- **Feature**: `report_exporter.py` module

### v1.7.0

- **Feature**: Verification batching (50% savings)
- **Feature**: Persistent batch state with resume on app restart

### v1.6.0

- **Feature**: Cross-spec coordination check

### v1.5.0

- **Feature**: Confidence scoring, finding deduplication, always-on verification

### v1.4.0

- **Feature**: Per-spec siloed review, batch processing, web search verification

### v1.3.0

- **Feature**: Project context field

### v1.2.0

- **Performance**: Reduced animation rates, rewritten log widget, batched callbacks

### v1.1.0

- **Feature**: Collapsible finding cards, pop-out report window

### v1.0.0

- Initial release

## Copyright Notice

**Copyright (c) 2025 Abraham Borg. All Rights Reserved.**

This software and associated documentation files (the "Software") are the proprietary property of Abraham Borg.

**Unauthorized copying, modification, distribution, or use of this Software, via any medium, is strictly prohibited without express written permission from the copyright holder.**
