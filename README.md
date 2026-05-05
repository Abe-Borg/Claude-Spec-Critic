# Spec Critic

**v2.11.0** — AI-assisted M&P specification review for California K-12 DSA projects.

Spec Critic reviews mechanical and plumbing CSI-format `.docx` specifications against California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) using Claude. It produces structured findings with severity classifications, confidence scores, verification verdicts backed by web search, optional cross-spec coordination analysis, and either inline edits or yellow-highlighted suggestion annotations on a copy of each spec.

The project is optimized for DSA-oriented K-12 workflows and California code cycle selection (2022 / 2025), with an emphasis on:

- structured, parseable model output (forced tool-use schemas — no fragile regex parsing),
- evidence-grounded verification (web-search-backed verdicts; CONFIRMED / CORRECTED only when grounded),
- cost-aware routing (Sonnet-default verifier with Opus escalation, optional Haiku triage, severity-tiered search budgets, persistent claim cache),
- robust batch processing (durable resume across all phases with content + source-file SHA-256 digests),
- safe Word output (surgical edits with safety categories, or non-destructive yellow-highlight annotation mode).

---

## Pipeline at a Glance

1. **Text Extraction** — Reads `.docx` files locally (paragraphs, tables, headers/footers). Cached by file hash so repeated runs over the same files do not re-parse.
2. **Local Pre-Screening** — Detects LEED references, unresolved placeholders (`[SELECT]`, `[VERIFY]`, `TBD`, etc.), stale code-cycle references, empty sections, duplicate headings, and inconsistent file naming, all without API calls.
3. **Per-Spec Review** — Each spec sent individually to Claude Opus 4.7 for code-compliance review. Output is delivered through a forced tool-use schema (`submit_review_findings`), so the parse-failure class is eliminated.
4. **Deduplication** — Consolidates identical findings across multiple specs (full-text SHA-256 keys; per-file occurrences tracked separately so multi-file edits fan out correctly).
5. **Cross-Spec Coordination** *(optional)* — Full-content analysis of all specs together. Large projects are chunked by CSI division (21 / 22 / 23 / Controls / 25 + 01) and merged. Runs in parallel with verification by default.
6. **Verification** — Findings verified by Claude Sonnet 4.6 with web search; CRITICAL/HIGH UNVERIFIED findings escalate to Opus 4.7. Verdicts are returned via the strict `submit_verification_verdict` tool. A persistent (disk-backed) claim-keyed cache and local-skip classification (placeholder/typo/duplicate GRIPES) avoid redundant searches. Optional Haiku verification triage further filters internally-verifiable findings. Small batch retry tails fall back to real-time verification.
7. **Edit Application** *(optional)* — Either:
   * **Edit mode** — Fuzzy-matched surgical edits applied to a copy of each spec. Ambiguous, table, header/footer, or rich-formatted matches are downgraded to manual review.
   * **Annotate mode** — Yellow-highlighted suggestion paragraphs inserted after each anchor; the original text is never mutated. Safer for table cells, header/footer text, and richly formatted paragraphs.

## Project Identity

- **App version:** `2.11.0` (package `src.__version__`).
- **Packaging version:** `2.8.0` in `pyproject.toml` (sync to `src/__init__.py` when cutting a release).
- **Runtime:** Python 3.11+ desktop app (CustomTkinter + TkinterDnD2).
- **Model stack:**
  - Review / Cross-check: Claude Opus 4.7 (default)
  - Verification (initial): Claude Sonnet 4.6 (default)
  - Verification (escalation): Claude Opus 4.7
  - Cross-discipline synthesis: Claude Haiku 4.5
  - Optional verification triage: Claude Haiku 4.5

## Processing Modes

- **Real-time** — Immediate in-session processing (streaming API, higher cost).
- **Batch** — Queued processing at 50% cost savings (usually 45 min – 2 hrs, 24 hrs max).

Both modes share identical prompts, models, tool schemas, output caps, and parsing logic, so findings should be functionally equivalent across modes. The 300k extended-output path is the only intentional asymmetry — batch-only by API design (`output-300k-2026-03-24` beta header is not honored on streaming requests) and only used for inputs ≥200k tokens. Batch state is persisted to disk with content + source-file SHA-256 digests and survives app restarts — resume from any phase: review-poll, review-collect, cross-check, verification (poll / wave-poll), or finalize.

## Review Modes

The reviewer prompt has three modes that adjust scope and edit safety:

| Mode | Scope | Auto-edit |
|---|---|---|
| Strict | Evidence-backed contradictions and code-cycle issues only | Allowed |
| Comprehensive *(default)* | Strict scope + constructability, TAB/commissioning, equipment schedule conflicts, Division 01 coordination, warranty, basis-of-design, controls sequence, DSA/HCAI/Title 24 closeout, fire/smoke damper access, seismic restraints, sprinkler/hydraulic, pipe/duct material, submittal/O&M | Allowed |
| Safe-edit | Findings with exact editable anchors and low-risk replacements only | Allowed |

The active mode is recorded in resume state so a resumed run uses the same prompt path it started with.

## End-to-End Workflow

1. Load spec files (`.docx` only).
2. Extract body + table + header/footer text while preserving useful paragraph mapping metadata.
3. Run local pre-screen checks (LEED references, unresolved placeholders, stale code cycles, empty sections, duplicate headings, file-naming inconsistencies) without API calls.
4. Run primary compliance review per spec (real-time or batch mode) via the `submit_review_findings` tool.
5. Deduplicate findings across specs and group them into `FindingGroup` / `FindingOccurrence` for display vs. per-file edit execution.
6. Optionally run cross-spec coordination check to catch contradictions, scope gaps, and interface misses (chunked by CSI division on large projects).
7. Run verification phase with web-search-backed adjudication for each finding (Sonnet default, Opus escalation, persistent claim cache, local-skip classification, optional Haiku triage).
8. Present results in GUI report windows.
9. Optionally export `.docx` review report.
10. Optionally generate and apply surgical edits — or non-destructive annotations — back into source Word documents.

## Output Surfaces

- **View in App** — Interactive report window with collapsible finding cards, severity grouping, JSON export, and a diagnostics window showing token usage, cache hits, verification grounding, output telemetry, and search-budget consumption.
- **Export Report** — Formatted `.docx` report with Word-native heading collapse, colored severity table, verification verdicts with sources (cited URLs rendered inline in blue after the verdict/explanation/correction block), and coordination summary.
- **Apply Edits** — `Finding.affected_files` drives multi-file fan-out so a deduped finding edits (or annotates) every affected spec.

## Module Map

| File | Purpose |
|---|---|
| `main.py` | PyInstaller entry point |
| `src/gui.py` | CustomTkinter GUI — inputs, mode selection, batch resume, diagnostics |
| `src/widgets.py` | Custom GUI widgets — TokenGauge (with API-exact preflight), FileListPanel, EnhancedLog, ReportWindow, EditSelectionDialog, DiagnosticsWindow (renders token / cache / evidence / output / search telemetry) |
| `src/pipeline.py` | Core orchestration — preparation, review, cross-check, verification, finalization. Defines `FindingGroup` / `FindingOccurrence` for display vs. edit-execution split |
| `src/api_config.py` | Centralized model identifiers, output-token caps, prompt-cache helpers, web-search tool config (severity-tiered max_uses, blocked-domain list), and feature flags |
| `src/structured_schemas.py` | Tool-use schemas for review, cross-check, and verification (eliminates fragile tag-and-regex JSON parsing) |
| `src/review_modes.py` | Strict / Comprehensive / Safe-edit mode definitions |
| `src/prompts.py` | System prompt and user message construction (mode-aware) |
| `src/reviewer.py` | Claude API client — streaming review, tool-use parsing, finding parsing |
| `src/cross_checker.py` | Cross-spec coordination reviewer — chunked by CSI division for large projects |
| `src/verifier.py` | Web-search verification — Sonnet-default with Opus escalation, real-time fallback for small retry tails |
| `src/verification_router.py` | Initial / escalation model selection and local-skip classification |
| `src/verification_cache.py` | Claim-keyed verification cache (only grounded results stored), with disk persistence at `~/.spec_critic/verification_cache.json` |
| `src/triage.py` | Optional Haiku-based verification triage (off by default) — classifies findings as `web_required` vs. `local_skip` with hard safety rails (CRITICAL/HIGH and any finding with a code reference are never eligible) |
| `src/verification_config.py` | Backward-compat re-exports from `api_config` |
| `src/batch.py` | Anthropic Message Batches API — submit, poll, retrieve for review and verification |
| `src/batch_runtime.py` | Bounded polling runtime with progressive backoff and error thresholds |
| `src/extractor.py` | DOCX text extraction with paragraph mapping (parallelized across files) |
| `src/extraction_cache.py` | LRU cache for extraction and exact API token counts (keyed by file mtime + config hash) |
| `src/preprocessor.py` | Local detection: LEED, placeholders, stale code cycles, empty sections, duplicate headings, file naming |
| `src/tokenizer.py` | Token counting (cl100k_base + Anthropic count_tokens), per-call limits, cross-check budget |
| `src/edit_locator.py` | Fuzzy / exact / normalized / section-anchored paragraph matching (with length-ratio + quick_ratio prefilters) |
| `src/edit_candidates.py` | Edit safety categories (AUTO_SAFE / AUTO_WITH_CAUTION / MANUAL_REVIEW / REPORT_ONLY) |
| `src/spec_editor.py` | Surgical DOCX edits + annotation/change-log mode |
| `src/apply_edits.py` | Locate → action build → apply (or annotate) workflow |
| `src/report_exporter.py` | Word document report generation |
| `src/resume_state.py` | Durable serialization with content + source-file SHA-256 digests for change detection |
| `src/diagnostics.py` | In-memory diagnostics report — events, phase durations, token + cache usage, verification evidence, output / search-budget telemetry, edit skip reasons, ambiguous locator counts |
| `src/code_cycles.py` | California code cycle definitions (2022, 2025) |

## Verification Architecture

Verification is not a trivial post-process; it includes:

- **Pre-pass classification.** A keyword classifier marks placeholder / LEED / typo / duplicate / internal-contradiction GRIPES (without a code reference) as `local_skip`, sparing them from web search. Persistent cache hits are also resolved before any API call.
- **Optional Haiku triage** (`SPEC_CRITIC_HAIKU_TRIAGE=1`). Augments the keyword classifier so internally-verifiable findings (internal contradictions where both sides are quoted, equipment-tag mismatches, formatting issues) skip the more expensive Sonnet+web_search call. Hard safety contract: findings with a non-empty `codeReference` are never eligible; `CRITICAL` / `HIGH` are never eligible; on API failure or parse error, all affected findings default to `web_required`.
- **Severity-tiered web-search budget.** CRITICAL/HIGH get `max_uses=7`, MEDIUM gets `5`, GRIPES get `3`, applied identically in real-time and batch paths.
- **Source quality.** A blocked-domain list filters social/AI-assistant/forum/general-encyclopedia sources. California priority sources are documented in the verifier system prompt rather than encoded as an allow-list (mixing allow + block lists is unsupported by `web_search_20260209`).
- **Grounding invariant.** A verdict cannot be `CONFIRMED` or `CORRECTED` unless `grounded=True`. Cap variation never lets weak verdicts through.
- **Sonnet-default routing with Opus escalation.** CRITICAL/HIGH UNVERIFIED findings re-verify on Opus 4.7. The router records `model_used` and `escalated` per result.
- **Persistent claim cache.** `VerificationCache.make_cache_key` keys on `cycle_label | actionType | codeReference | sha256(claim_summary)`. Only `grounded=True` results are cached. Cache round-trips to disk via atomic temp-file + rename. Default behavior is database mode (no automatic expiration); set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` for opt-in TTL pruning.
- **Real-time fallback.** When a batch retry tail shrinks below `SPEC_CRITIC_REALTIME_FALLBACK_THRESHOLD` (default 5), the remaining items flip to real-time verification rather than waiting another batch cycle.
- **Sources reporting.** `VerificationResult.sources` contains only the URLs the model cited in its tool input. The full URL count from all `web_search` calls is preserved on `successful_source_count` for diagnostics.

## Edit Safety Categories

The locator (`edit_locator.py`) and edit-action builder (`spec_editor.py`) gate auto-application by safety category:

| Category | Behavior |
|---|---|
| AUTO_SAFE | Exact or normalized match, plain paragraph, single formatting run — applied automatically |
| AUTO_WITH_CAUTION | Fuzzy or section-anchored match in a still-tractable paragraph — applied when `allow_caution=True` |
| MANUAL_REVIEW | Ambiguous / multi-run / table / header/footer — auto-edit suppressed; surfaced for review |
| REPORT_ONLY | Defaulted when no anchor can be located safely — finding shown in report only |

Annotate mode bypasses these gates by writing a yellow-highlighted suggestion paragraph after the anchor, never modifying the original text.

## Code Cycles

Built-in support for:

- **California 2022 code cycle**
- **California 2025 code cycle** *(default)*

Cycle selection drives prompt framing, code reference expectations, and cache keying. Switching cycles naturally invalidates persistent cache entries from the prior cycle (the cycle label is part of the cache key).

## Requirements

- Python 3.11+
- Anthropic API key (`ANTHROPIC_API_KEY`)
- Dependencies (see `requirements.txt`): `anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic`

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
| `SPEC_CRITIC_VERIFICATION_MAX_USES` | `5` | Default web_search `max_uses` (when severity tiering doesn't apply) |
| `SPEC_CRITIC_HAIKU_TRIAGE` | `0` (off) | `1` enables Haiku verification triage augmenting the keyword classifier |
| `SPEC_CRITIC_REVIEW_MODEL` | `claude-opus-4-7` | Override review model |
| `SPEC_CRITIC_CROSS_CHECK_MODEL` | `claude-opus-4-7` | Override cross-check model |
| `SPEC_CRITIC_SYNTHESIS_MODEL` | `claude-haiku-4-5` | Override cross-discipline synthesis model |
| `SPEC_CRITIC_TRIAGE_MODEL` | `claude-haiku-4-5` | Override Haiku verification triage model |
| `SPEC_CRITIC_VERIFICATION_MODEL` | (auto from sonnet flag) | Override verifier model |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | `claude-opus-4-7` | Override escalation model |
| `SPEC_CRITIC_VERIFICATION_CACHE_PERSIST` | `1` (on) | `0` disables on-disk verification cache (database mode) |
| `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` | `0` | Positive integer enables age-based cache pruning |
| `SPEC_CRITIC_CACHE_PATH` | `~/.spec_critic/verification_cache.json` | Override cache path |
| `SPEC_CRITIC_EXTRACTION_CACHE` | `1` (on) | `0` disables file-extraction cache |

## Token Limits

| Constant | Value | Purpose |
|---|---|---|
| `MAX_CONTEXT_TOKENS` | 1,000,000 | Hard ceiling for combined input+output |
| `MAX_OUTPUT_TOKENS_OPUS` | 128,000 | Per-call Opus output cap |
| `MAX_OUTPUT_TOKENS_SONNET` | 64,000 | Per-call Sonnet output cap |
| `MAX_OUTPUT_TOKENS_HAIKU` | 64,000 | Per-call Haiku output cap |
| `REVIEW_OUTPUT_CAP` | 128,000 | Unified per-spec review cap (real-time and batch) |
| `REVIEW_OUTPUT_CAP_BATCH_EXTENDED` | 300,000 | Batch-only; requires the 300k beta header |
| `CROSS_CHECK_OUTPUT_CAP` | 96,000 | Cross-check needs more than verify |
| `CROSS_CHECK_OUTPUT_BUDGET` | 128,000 | Reserved for cross-check output during budgeting |
| `VERIFICATION_OUTPUT_CAP` | 16,000 | Verdicts are 1–2 sentences (tightened from 32k) |
| `SYNTHESIS_OUTPUT_CAP` | 32,000 | Cross-discipline synthesis on Haiku |
| `HAIKU_TRIAGE_OUTPUT_CAP` | 8,000 | Triage classifications |
| `RECOMMENDED_MAX` | 500,000 | Per-spec input warning threshold |
| `CROSS_CHECK_OVERHEAD` | 50,000 | Reserved for system + user-prompt overhead in cross-check |
| `CROSS_CHECK_RECOMMENDED_MAX` | 822,000 | Combined input ceiling for single-pass cross-check |

## Resume Phases

`resume_state.py` exposes the canonical phase set:

- `PHASE_REVIEW_POLL`
- `PHASE_REVIEW_COLLECT`
- `PHASE_VERIFICATION_POLL`
- `PHASE_VERIFICATION_WAVE_POLL`
- `PHASE_CROSS_CHECK`
- `PHASE_CROSS_CHECK_VERIFICATION_POLL`
- `PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL`
- `PHASE_FINALIZE`

`build_resume_state(...)` and `deserialize_resume_state(payload)` round-trip a run-state payload. `serialize_extracted_spec` records SHA-256 digests of both the extracted content and the underlying source file; `deserialize_extracted_spec` warns when either differs at resume time. The active review mode is also persisted so a resumed run uses the same prompt path it started with.

## Changelog

### v2.11.0
- Default review/cross-check model upgraded to Claude Opus 4.7; escalation model also Opus 4.7
- Persistent verification cache: claim-keyed verdicts round-trip to `~/.spec_critic/verification_cache.json` via atomic temp-file + rename; database mode by default with optional TTL pruning (`SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS`)
- Optional Haiku 4.5 verification triage (`SPEC_CRITIC_HAIKU_TRIAGE=1`) augments the keyword classifier with hard safety contract (CRITICAL/HIGH and findings with a code reference are never eligible; API failure → `web_required`)
- Cross-discipline synthesis model exposed (Haiku 4.5 default; `SPEC_CRITIC_SYNTHESIS_MODEL` override)
- Severity-tiered web-search budgets standardized: CRITICAL/HIGH=7, MEDIUM=5, GRIPES=3
- Verification output cap tightened to 16k (verdicts are 1–2 sentences); `SYNTHESIS_OUTPUT_CAP` and `HAIKU_TRIAGE_OUTPUT_CAP` added
- `VerificationResult.sources` reports only cited URLs; total retrieved URLs preserved on `successful_source_count`
- Cross-check chunking grouping refined (Div 21 / 22 / 23 / Controls / 25 + 01)

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

### v2.8.0
- Batch-only enforcement for verification (real-time verification removed at the time; later restored as a small-tail fallback in v2.10.0)
- Multi-wave verification batch with retry/continuation support
- Bounded polling runtime with configurable timeouts and error thresholds
- Durable resume state serialization across all pipeline phases
