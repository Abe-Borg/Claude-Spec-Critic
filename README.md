# Spec Critic

**v2.10.0** — AI-assisted M&P specification review for California K-12 DSA projects.

The project is optimized for DSA-oriented K-12 workflows and California code cycle selection (2022/2025), with an emphasis on:

Spec Critic reviews mechanical and plumbing CSI-format `.docx` specifications against California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) using Claude. It produces structured findings with severity classifications, confidence scores, verification verdicts backed by web search, optional cross-spec coordination analysis, and either inline edits or yellow-highlighted suggestion annotations on a copy of each spec.

---

1. **Text Extraction** — Reads `.docx` files locally (paragraphs, tables, headers/footers). Cached by file hash so repeated runs over the same files do not re-parse.
2. **Local Pre-Screening** — Detects LEED references, unresolved placeholders (`[SELECT]`, `[VERIFY]`, `TBD`, etc.), stale code-cycle references, empty sections, duplicate headings, and inconsistent file naming, all without API calls.
3. **Per-Spec Review** — Each spec sent individually to Claude Opus 4.6 for code compliance review. Output is delivered through a forced tool-use schema (`submit_review_findings`), so the parse-failure class is eliminated.
4. **Deduplication** — Consolidates identical findings across multiple specs (full-text SHA-256 keys; per-file occurrences tracked separately so multi-file edits fan out correctly).
5. **Cross-Spec Coordination** *(optional)* — Full-content analysis of all specs together. Large projects are chunked by CSI division (21/22/23/controls) and merged. Runs in parallel with verification by default.
6. **Verification** — Findings verified by Claude Sonnet 4.6 with web search; CRITICAL/HIGH UNVERIFIED findings escalate to Opus. Verdicts are returned via the strict `submit_verification_verdict` tool. A per-run cache (claim-keyed, not just code-reference) and local-skip classification (placeholder/typo/duplicate GRIPES) avoid redundant searches. Small batch retry tails fall back to real-time verification.
7. **Edit Application** *(optional)* — Either:
   * **Edit mode** — Fuzzy-matched surgical edits applied to a copy of each spec. Ambiguous, table, header/footer, or rich-formatted matches are downgraded to manual review.
   * **Annotate mode** — Yellow-highlighted suggestion paragraphs inserted after each anchor; the original text is never mutated. Safer for table cells, header/footer text, and richly formatted paragraphs.

- **App version:** `2.8.3` (package `src.__version__`).
- **Packaging version:** `2.8.0` in `pyproject.toml` (if you cut a release, sync this with `src/__init__.py`).
- **Runtime:** Python 3.11+ desktop app (CustomTkinter + TkinterDnD2).
- **Model stack:** Anthropic Claude Opus 4.6 for review/cross-check/verification.

- **Real-time** — Immediate in-session processing (streaming API, higher cost).
- **Batch** — Queued processing at 50% cost savings (usually 45 min–2 hrs, 24 hrs max).

Both modes use identical review prompts and criteria. Batch state is persisted to disk with content + source-file SHA-256 digests and survives app restarts — resume from any phase.

## Review Modes

The reviewer prompt has three modes that adjust scope and edit safety:

| Mode | Scope | Auto-edit |
|---|---|---|
| Strict | Evidence-backed contradictions and code-cycle issues only | Allowed |
| Comprehensive *(default)* | Strict scope + constructability, TAB/commissioning, equipment schedule conflicts, Division 01 coordination, warranty, basis-of-design, controls sequence, DSA/HCAI/Title 24 closeout, fire/smoke damper access, seismic restraints, sprinkler/hydraulic, pipe/duct material, submittal/O&M | Allowed |
| Safe-edit | Findings with exact editable anchors and low-risk replacements only | Allowed |

1. **Load spec files (`.docx` only).**
2. **Extract body + table + header/footer text** while preserving useful paragraph mapping metadata.
3. **Run local pre-screen checks** (LEED references, unresolved placeholders) without API calls.
4. **Run primary compliance review** per spec (real-time or batch mode).
5. **Deduplicate findings** across specs.
6. **Optionally run cross-spec coordination check** to catch contradictions, scope gaps, and interface misses.
7. **Run verification phase** with web-search-backed adjudication for each finding.
8. **Present results** in GUI report windows.
9. **Optionally export `.docx` review report.**
10. **Optionally generate and apply surgical edits** back into source Word documents.

- **View in App** — Interactive report window with collapsible finding cards, severity grouping, JSON export, and a diagnostics window showing token usage, cache hits, verification grounding, output telemetry, and search-budget consumption.
- **Export Report** — Formatted `.docx` report with Word-native heading collapse, colored severity table, verification verdicts with sources, and coordination summary.

## Core Capabilities

### 1) Review Modes

## Module Map

| File | Purpose |
|---|---|
| `main.py` | PyInstaller entry point |
| `src/gui.py` | CustomTkinter GUI — inputs, mode selection, batch resume, diagnostics |
| `src/widgets.py` | Custom GUI widgets — TokenGauge (with API-exact preflight), FileListPanel, EnhancedLog, ReportWindow, EditSelectionDialog, DiagnosticsWindow (renders token/cache/evidence/output/search telemetry) |
| `src/pipeline.py` | Core orchestration — preparation, review, cross-check, verification, finalization. Defines `FindingGroup` / `FindingOccurrence` for display vs. edit-execution split |
| `src/api_config.py` | Centralized model identifiers, output-token caps, prompt-cache helpers, web-search tool config, and feature flags |
| `src/structured_schemas.py` | Tool-use schemas for review, cross-check, and verification (eliminates fragile tag-and-regex JSON parsing) |
| `src/review_modes.py` | Strict / Comprehensive / Safe-edit mode definitions |
| `src/reviewer.py` | Claude API client — streaming review, tool-use parsing, finding parsing |
| `src/cross_checker.py` | Cross-spec coordination reviewer — chunked by CSI division for large projects |
| `src/verifier.py` | Web-search verification — Sonnet-default with Opus escalation, real-time fallback for small retry tails |
| `src/verification_router.py` | Initial / escalation model selection and local-skip classification |
| `src/verification_cache.py` | Per-run claim-keyed verification cache (only grounded results stored) |
| `src/batch.py` | Anthropic Message Batches API — submit, poll, retrieve for review and verification |
| `src/batch_runtime.py` | Bounded polling runtime with progressive backoff and error thresholds |
| `src/extractor.py` | DOCX text extraction with paragraph mapping (parallelized across files) |
| `src/extraction_cache.py` | LRU cache for extraction and exact API token counts (keyed by file mtime + config hash) |
| `src/preprocessor.py` | Local detection: LEED, placeholders, stale code cycles, empty sections, duplicate headings, file naming |
| `src/prompts.py` | System prompt and user message construction (mode-aware) |
| `src/tokenizer.py` | Token counting (cl100k_base + Anthropic count_tokens), per-call limits, cross-check budget |
| `src/edit_locator.py` | Fuzzy/exact/normalized/section-anchored paragraph matching (with length-ratio + quick_ratio prefilters) |
| `src/edit_candidates.py` | Edit safety categories (AUTO_SAFE / AUTO_WITH_CAUTION / MANUAL_REVIEW / REPORT_ONLY) |
| `src/spec_editor.py` | Surgical DOCX edits + annotation/change-log mode |
| `src/apply_edits.py` | Locate → action build → apply (or annotate) workflow |
| `src/report_exporter.py` | Word document report generation |
| `src/resume_state.py` | Durable serialization with content + source-file SHA-256 digests for change detection |
| `src/diagnostics.py` | In-memory diagnostics report — events, phase durations, token + cache usage, verification evidence, output / search-budget telemetry, edit skip reasons, ambiguous locator counts |
| `src/code_cycles.py` | California code cycle definitions (2022, 2025) |

## Feature Flags

All flags read from environment variables; the listed default applies when the variable is unset.

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_PROMPT_CACHE` | `1` (on) | `0` disables prompt caching |
| `SPEC_CRITIC_STRUCTURED_OUTPUTS` | `1` (on) | `0` falls back to tagged-JSON parsing |
| `SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT` | `1` (on) | `0` skips Anthropic count_tokens before submission |
| `SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT` | `1` (on) | `0` reverts to Opus-everywhere |
| `SPEC_CRITIC_LOCAL_VERIFICATION_SKIP` | `1` (on) | `0` web-verifies all findings |
| `SPEC_CRITIC_PARALLEL_CROSS_CHECK` | `1` (on) | `0` runs cross-check after verification |
| `SPEC_CRITIC_REALTIME_FALLBACK_THRESHOLD` | `5` | Items remaining at which a small retry tail flips to real-time |
| `SPEC_CRITIC_VERIFICATION_MAX_USES` | `5` | Web-search tool max_uses per verification |
| `SPEC_CRITIC_REVIEW_MODEL` | `claude-opus-4-6` | Override review model |
| `SPEC_CRITIC_VERIFICATION_MODEL` | (auto from sonnet flag) | Override verification model |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | `claude-opus-4-6` | Override escalation model |
| `SPEC_CRITIC_EXTRACTION_CACHE` | `1` (on) | `0` disables file-extraction cache |

- Built-in support for:
  - **California 2022 code cycle**
  - **California 2025 code cycle**
- Cycle selection drives prompt framing and code reference expectations across review + verification.

- Python 3.11+
- Anthropic API key
- Dependencies: `anthropic`, `python-docx`, `customtkinter`, `tiktoken`, `platformdirs`

Verification is not a trivial post-process; it includes:

### v2.10.0
- Structured outputs: review, cross-check, and verification now use Anthropic tool-use schemas instead of `<findings_json>` regex parsing
- Sonnet 4.6 is the default verifier (Opus escalates for CRITICAL/HIGH UNVERIFIED)
- Local-skip classification, parallel cross-check, real-time retry tail (default threshold 5), and Anthropic token-count preflight are now on by default
- Annotation / change-log edit mode (yellow-highlighted suggestion paragraphs; never mutates source text)
- `FindingGroup` / `FindingOccurrence` types formalize the display-dedup vs. per-file edit split
- Resume state stores content and source-file SHA-256 digests; warns on disk-side changes
- Fuzzy matcher gains length-ratio + `quick_ratio` prefilters for large documents
- Token gauge labels approximate vs. exact (API) counts; runs the API count async after the live estimate
- DiagnosticsWindow renders cache, verification evidence, edit skip reasons, ambiguous locators, output telemetry, and search-budget telemetry inline
- GUI no longer keyword-sniffs log messages; pipeline passes explicit `phase=` to log/progress callbacks
- Stale `CODE_EXECUTION_TOOL` export removed from `verification_config.py`

### v2.8.3
- Verbose Word report now includes verification source URLs for each finding (rendered inline with blue text after the verdict/explanation/correction block)

### 4) Editable Output Pipeline

### v2.8.0
- Batch-only enforcement for verification (real-time verification removed)
- Multi-wave verification batch with retry/continuation support
- Bounded polling runtime with configurable timeouts and error thresholds
- Durable resume state serialization across all pipeline phases
