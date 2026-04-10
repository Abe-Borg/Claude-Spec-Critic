# Spec Critic

**v2.8.3** — AI-assisted M&P specification review for California K-12 DSA projects.

## What It Does

Spec Critic reviews mechanical and plumbing CSI-format `.docx` specifications against California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) using Claude Opus 4.6. It produces structured findings with severity classifications, confidence scores, verification verdicts backed by web search, and optional cross-spec coordination analysis.

## Pipeline Stages

1. **Text Extraction** — Reads `.docx` files locally (paragraphs, tables, headers/footers)
2. **Local Pre-Screening** — Detects LEED references and unresolved placeholders without API calls
3. **Per-Spec Review** — Each spec sent individually to Claude Opus 4.6 for code compliance review
4. **Deduplication** — Consolidates identical findings across multiple specs
5. **Cross-Spec Coordination** *(optional)* — Full-content analysis of all specs together in a single 1M-token-context call to find inter-spec contradictions, scope gaps, and coordination issues
6. **Verification** — Every finding verified by a secondary Claude Opus 4.6 pass with web search (multi-wave batch with retry/continuation support)
7. **Edit Application** *(optional)* — Fuzzy-matched surgical edits applied directly to source `.docx` files

## Modes

- **Real-time** — Immediate in-session processing (streaming API, higher cost)
- **Batch** — Queued processing at 50% cost savings (usually 45 min–2 hrs, 24 hrs max)

Both modes use identical review prompts and criteria. Batch state is persisted to disk and survives app restarts — resume from any phase.

## Output Options

- **View in App** — Interactive report window with collapsible finding cards, severity grouping, and JSON export
- **Export Report** — Formatted `.docx` report with Word-native heading collapse, colored severity table, verification verdicts with sources, and coordination summary

## Code Cycles

Supports California 2022 and 2025 code cycles. The selected cycle determines which editions of CBC, CMC, CPC, Energy Code, CALGreen, and ASCE 7 the reviewer checks against.

## Key Files

| File | Purpose |
|---|---|
| `main.py` | PyInstaller entry point |
| `src/gui.py` | CustomTkinter GUI — inputs, mode selection, batch resume, diagnostics |
| `src/pipeline.py` | Core orchestration — preparation, review, cross-check, verification, finalization |
| `src/reviewer.py` | Claude API client — streaming review, JSON extraction, finding parsing |
| `src/verifier.py` | Web search verification — multi-wave batch with retry/continuation |
| `src/batch.py` | Anthropic Message Batches API — submit, poll, retrieve for review and verification |
| `src/batch_runtime.py` | Bounded polling runtime with timeout, no-progress detection, error thresholds |
| `src/cross_checker.py` | Cross-spec coordination reviewer — full-content multi-spec analysis |
| `src/extractor.py` | DOCX text extraction with paragraph mapping (body elements, tables, headers/footers) |
| `src/preprocessor.py` | Local detection of LEED references and unresolved placeholders |
| `src/prompts.py` | System prompt and user message construction |
| `src/tokenizer.py` | Token counting, per-call limits, cross-check budget |
| `src/edit_locator.py` | Fuzzy/exact/normalized/section-anchored paragraph matching |
| `src/edit_candidates.py` | Eligibility classification for finding-to-edit selection |
| `src/spec_editor.py` | Surgical DOCX edit application (paragraph replace, delete, add, table cell edits) |
| `src/apply_edits.py` | Orchestration of locate → action build → apply workflow |
| `src/report_exporter.py` | Word document report generation with severity table, verdicts, sources, and coordination |
| `src/resume_state.py` | Durable serialization for batch resume across all pipeline phases |
| `src/verification_config.py` | Shared config for verification model, tools, batch output beta |
| `src/code_cycles.py` | California code cycle definitions (2022, 2025) |
| `src/diagnostics.py` | In-memory diagnostics report with event timeline and phase durations |
| `src/widgets.py` | Custom GUI widgets — TokenGauge, FileListPanel, EnhancedLog, ReportWindow, EditSelectionDialog, DiagnosticsWindow |

## Requirements

- Python 3.11+
- Anthropic API key (Claude Opus 4.6)
- Dependencies: `anthropic`, `python-docx`, `customtkinter`, `tiktoken`, `platformdirs`

## Changelog

### v2.8.3
- Verbose Word report now includes verification source URLs for each finding (rendered inline with blue text after the verdict/explanation/correction block)

### v2.8.2
- Retryable connection error handling for transient httpx/urllib3 failures during streaming review
- Per-spec errors surfaced on combined ReviewResult for clear reporting
- Zero-findings-with-errors distinguished from clean passes in GUI

### v2.8.0
- Batch-only enforcement for verification (real-time verification removed)
- Multi-wave verification batch with retry/continuation support
- Bounded polling runtime with configurable timeouts and error thresholds
- Durable resume state serialization across all pipeline phases