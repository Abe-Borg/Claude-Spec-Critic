# CLAUDE.md — Spec Critic v2.10.0

This document is the engineering/operator reference for the Spec Critic codebase. It is intentionally implementation-focused and should be kept aligned with the actual runtime behavior in `src/`.

---

Spec Critic is a Python desktop application for reviewing mechanical and plumbing construction specifications for California K-12 DSA projects using Claude. It extracts text from `.docx` files, performs local preprocessing, runs per-spec reviews (real-time or batch), optionally runs cross-spec coordination checks, verifies findings via web search (Sonnet by default with Opus escalation), and presents results in-app or as exported Word reports. Optional auto-edit and annotation modes write a copy of each spec with surgical edits or yellow-highlighted suggestions.

Spec Critic is a Python desktop application that performs AI-assisted review of California mechanical/plumbing specification documents (`.docx`).

```
src/
├── __init__.py             # Package version (2.10.0)
├── gui.py                  # CustomTkinter GUI — all user interaction
├── widgets.py              # Reusable UI components
├── pipeline.py             # Core orchestration + FindingGroup/FindingOccurrence
├── api_config.py           # Centralized model/output-cap/feature-flag config
├── structured_schemas.py   # Tool-use schemas for review/cross-check/verification
├── review_modes.py         # Strict / Comprehensive / Safe-edit profiles
├── prompts.py              # System + user prompt builders (mode-aware)
├── reviewer.py             # Anthropic API client (streaming + tool-use parsing)
├── cross_checker.py        # Cross-spec coordination (chunked by CSI division)
├── verifier.py             # Verification (Sonnet/Opus routing, real-time fallback)
├── verification_router.py  # Initial / escalation model + local-skip classification
├── verification_cache.py   # Per-run claim-keyed verdict cache
├── verification_config.py  # Backward-compat re-exports from api_config
├── batch.py                # Anthropic Message Batches API wrapper
├── batch_runtime.py        # Bounded polling with progressive backoff
├── extractor.py            # DOCX text extraction (parallelized)
├── extraction_cache.py     # LRU caches for extraction + API token counts
├── preprocessor.py         # Local LEED/placeholder/stale-cycle/structural alerts
├── tokenizer.py            # Local + Anthropic token counting
├── edit_locator.py         # Exact / normalized / fuzzy / section-anchored matching
├── edit_candidates.py      # Edit safety categories
├── spec_editor.py          # Surgical edits + annotation/change-log mode
├── apply_edits.py          # locate → action build → apply / annotate
├── report_exporter.py      # Word (.docx) report generation
├── resume_state.py         # Durable resume state (with file-hash validation)
├── diagnostics.py          # In-memory diagnostics report
└── code_cycles.py          # California code cycle definitions
```

- identify likely code/compliance and coordination issues,
- classify findings with severity + confidence,
- verify findings with web-search-backed evidence,
- generate stakeholder-readable reports,
- optionally apply precise edits back to Word source files.

```
User selects .docx files
         │
         ▼
    extraction_cache.extract_multiple_specs_cached()
         │  (hits skipped; misses parsed in parallel via extractor.extract_multiple_specs)
         ▼
    preprocessor.preprocess_spec()
         │  → LEED alerts, placeholder alerts, stale-cycle alerts, structural alerts
         ▼
    tokenizer.count_tokens() (local) + count_tokens_via_api() (preflight)
         │
         ├──── Real-time path ────┐         ├──── Batch path ───────────────┐
         ▼                        │         ▼                                │
    reviewer.review_single_spec() │      batch.submit_review_batch()         │
       (forced tool_use:          │         (forced tool_use:                │
        submit_review_findings)   │          submit_review_findings)         │
         │                        │         │                                │
         ▼                        │         ▼                                │
    pipeline._deduplicate_findings (full-text SHA-256 keys)                  │
         │                                  │                                │
         ▼                                  ▼                                │
    cross_checker.run_chunked_cross_check (parallel with verification by default)
         │                                  │                                │
         ▼                                  ▼                                │
    verifier.verify_findings / verify_findings_batch                         │
       (Sonnet default, Opus escalation, claim cache, local-skip,            │
        real-time fallback for small retry tails)                            │
         │                                  │                                │
         ▼                                  ▼                                │
    pipeline.finalize_batch_result / PipelineResult                          │
         │                                                                   │
         ├──── View in App ──── widgets.ReportWindow + DiagnosticsWindow ────│
         ├──── Export Report ── report_exporter.export_report (.docx)        │
         └──── Apply edits ──── apply_edits.execute_edit_plan(mode=          │
                                "edit"|"annotate")                           │
```

## 2) Runtime Topology

### ExtractedSpec (extractor.py)
```python
@dataclass
class ExtractedSpec:
    filename: str
    content: str
    word_count: int
    source_path: str = ""
    source_format: str = ""
    paragraph_map: list[ParagraphMapping] | None = None
```

### ParagraphMapping (extractor.py)
Per-element record used by the locator. Includes `body_index`, `element_type`, `section_index`, plus Phase 4 formatting fields (`run_count`, `distinct_formatting_runs`).

### Finding (reviewer.py)
```python
@dataclass
class Finding:
    severity: str
    fileName: str
    section: str
    issue: str
    actionType: str         # ADD / EDIT / DELETE
    existingText: str | None
    replacementText: str | None
    codeReference: str | None
    confidence: float = 0.5
    verification: VerificationResult | None = None
    affected_files: list[str] = field(default_factory=list)
    anchorText: str | None = None        # ADD only
    insertPosition: str | None = None    # "before" | "after" (ADD only)
```

### FindingGroup / FindingOccurrence (pipeline.py)
Phase 1.3 formalization of the display-dedup vs. per-file edit-execution split. `group_findings(findings)` returns one `FindingGroup` per deduped finding, with one `FindingOccurrence` per file in `affected_files`. `expand_to_occurrences(findings)` flattens to per-file occurrences, skipping placeholders.

### ReviewResult (reviewer.py)
Adds Phase 2 prompt-cache telemetry:
```python
cache_creation_input_tokens: int = 0
cache_read_input_tokens: int = 0
```

### VerificationResult (verifier.py)
Phase 3 evidence model: `grounded`, `model_used`, `escalated`, `cache_status`, `web_search_requests`, `successful_source_count`, `search_error_count`. Verdicts cannot be CONFIRMED/CORRECTED unless `grounded` is True.

### BatchSubmission / CollectedBatchState (pipeline.py)
Plus `review_mode: str` so resume restores the exact prompt path.

### Batch and Recovery Layer

### api_config.py — Centralized API configuration

**Public API:**
- Model identifiers: `MODEL_OPUS_46`, `MODEL_OPUS_47`, `MODEL_SONNET_46`, `MODEL_HAIKU_45`
- Defaults: `REVIEW_MODEL_DEFAULT`, `CROSS_CHECK_MODEL_DEFAULT`, `VERIFICATION_MODEL_DEFAULT`, `VERIFICATION_ESCALATION_MODEL`
- Output caps: `review_max_tokens()`, `cross_check_max_tokens()`, `verification_max_tokens()`, `output_cap_for_model()`, `assert_extended_output_allowed()`
- Prompt caching: `prompt_caching_enabled()`, `system_prompt_with_cache()`, `tools_with_cache()`, `extract_cache_usage()`
- Token counting: `token_count_preflight_enabled()` (default on)
- Sonnet routing: `verification_sonnet_default_enabled()` (default on)
- Web-search tool: `WEB_SEARCH_TOOL` (web_search_20260209, blocked-only domain list, max_uses=5)

### structured_schemas.py — Tool-use schemas

**Public API:**
- `review_findings_tool()`, `review_tool_choice()`
- `cross_check_findings_tool()`, `cross_check_tool_choice()`
- `verification_verdict_tool()` (no forcing tool_choice; web_search runs first)
- `extract_tool_use_block(response, tool_name)` — pulls the matching tool's `input` off a response (works on SDK objects and plain dicts)
- `structured_outputs_enabled()` — env-toggle (default on)

### review_modes.py — Review mode profiles

`ReviewMode` enum with STRICT / COMPREHENSIVE / SAFE_EDIT. `coerce_review_mode(value)` accepts strings (`"strict"`, `"comprehensive"`, `"safe_edit"`) for convenience. `DEFAULT_REVIEW_MODE = COMPREHENSIVE`.

### Reporting + Diagnostics Layer

**Public API:**
- `get_system_prompt(cycle, mode=...)`
- `get_single_spec_user_message(spec_content, filename, project_context, *, cycle, mode=...)`

Both inject the active review mode banner, the mode-specific task text, and the editability clause. The system prompt instructs the model to call the structured tool (with a tagged-JSON fallback for compatibility).

### code_cycles.py — California code cycles

**Public API:** `CodeCycle`, `CALIFORNIA_2022`, `CALIFORNIA_2025`, `AVAILABLE_CYCLES`, `DEFAULT_CYCLE` (= `CALIFORNIA_2025`).

### resume_state.py — Durable resume state

### `Finding` (`reviewer.py`)
Canonical issue object containing:

**Public API:**
- Phase constants: `PHASE_REVIEW_POLL`, `PHASE_REVIEW_COLLECT`, `PHASE_VERIFICATION_POLL`, `PHASE_VERIFICATION_WAVE_POLL`, `PHASE_CROSS_CHECK`, `PHASE_CROSS_CHECK_VERIFICATION_POLL`, `PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL`, `PHASE_FINALIZE`
- `SUPPORTED_PHASES`
- `build_resume_state(...) -> dict`
- `deserialize_resume_state(payload) -> dict`

Phase 5.5: `serialize_extracted_spec` records SHA-256 digests of both the extracted content and the underlying source file. `deserialize_extracted_spec` warns when either differs from the saved digest at resume time.

- raw model content,
- token usage,
- elapsed time,
- stop reason,
- parse status,
- optional error context.

- `submit_review_batch(specs, ..., mode)` — emits requests with the structured tool when enabled
- `poll_batch(batch_id) -> BatchStatus`
- `retrieve_review_results(job, *, model)` — extracts findings from the tool_use block (falls back to text)
- `submit_verification_batch(...)`, `retrieve_verification_results(...)`, `cancel_batch(...)`

### batch_runtime.py — Polling runtime

Progressive poll backoff: base interval for ~5 minutes, then linearly ramps to 120 s, then holds. `PollPolicy` carries timeout / error-threshold / no-progress thresholds.

### `VerificationResult` (`verifier.py`)
Structured post-review verdict and evidence summary attached per finding.

- `run_cross_check(specs, existing_findings, ...)` — single-pass
- `run_chunked_cross_check(specs, existing_findings, ...)` — chunks by CSI division (Div 21 / 22 / 23 / Controls / 25 + 01) when the combined input exceeds the recommended cap; merges chunk results locally

---

- `verify_findings(findings, *, progress, cycle, cache)` — real-time path (Sonnet default, Opus escalation)
- `verify_findings_batch(findings, *, log, progress, ...)` — multi-wave batch path
- `verify_finding(finding, *, max_retries=2, cycle, model, cache, escalated)` — single finding
- `prepare_findings_for_verification(findings, *, cycle, cache, log)` — Phase 3 pre-pass (resolves local-skip and cache hits in place)
- `start_verification_batch(...)`, `collect_verification_batch_results(..., realtime_fallback_threshold=5)`
- `_verdict_from_tool_use(message)` — unpack the strict `submit_verification_verdict` tool input (preferred over text parsing)

### verification_router.py — Phase 3 routing

- `initial_verification_model()` / `escalation_verification_model()`
- `should_escalate_verification(finding, *, verdict, grounded, ...)` — fires for CRITICAL/HIGH UNVERIFIED when Sonnet was the initial verifier
- `classify_finding_for_verification(finding) -> "web_required" | "local_skip"` — local-skip default-on; only GRIPES with no codeReference and a placeholder/LEED/typo/duplicate/internal-contradiction keyword

### verification_cache.py — Per-run cache

`VerificationCache.make_cache_key(finding, cycle)` includes `cycle_label | actionType | codeReference | sha256(claim_summary)`. Only `grounded=True` results are cached. Hits are tagged `cache_status="hit"`.

### pipeline.py — Orchestration

Phased batch APIs used by the GUI:
- `collect_review_batch_results(submission)`
- `run_cross_check_for_batch(state, specs, ...)`
- `prepare_verification_work(state)`
- `start_batch_verification(findings, *, cycle, log, progress, cache=None)` — returns `None` when every finding resolved locally
- `collect_batch_verification_results(job, findings, *, cache=None, ...)`
- `finalize_batch_result(state)`

Convenience wrapper: `collect_batch_results(submission, ...)`.

Helpers:
- `_phase_tagged_log(log, phase)` / `_phase_tagged_progress(progress, phase)` — let the verifier path tag its callbacks so the GUI doesn't keyword-sniff message text
- `group_findings(findings)` / `expand_to_occurrences(findings)` — Phase 1.3 formal types
- `_parallel_cross_check_enabled()` — default on; cross-check runs concurrently with verification poll, then `_drop_cross_check_findings_with_disputed_upstream` filters cross-check findings whose upstream review verdict became DISPUTED
- `_recover_retryable_review_batch_results(...)` — small repair batch for parse_error / incomplete review specs

### preprocessor.py — Local preflight

- `preprocess_spec(content, filename, *, cycle=None)` returns LEED alerts, placeholder alerts, code-cycle alerts, structural alerts
- `detect_stale_code_cycle_references`, `detect_empty_sections`, `detect_duplicate_headings`, `detect_inconsistent_file_naming`

### extractor.py / extraction_cache.py

- `extract_text(filepath) -> ExtractedSpec` / `extract_text_from_docx(filepath)`
- `extract_multiple_specs(filepaths)` — bounded ThreadPoolExecutor (max 8 workers); deterministic order
- `extract_multiple_specs_cached(filepaths)` — uses the LRU cache keyed on `(absolute_path, size, mtime_ns)`; falls back to parallel extraction for misses
- `token_count_cache_key(model, system_prompt, user_message, project_context, cycle_label, mode)` — SHA-256 of inputs; LRU bounded to 256 entries

## 5) Prompting and Code-Cycle Behavior

- `MAX_CONTEXT_TOKENS = 1_000_000`
- `MAX_OUTPUT_TOKENS_OPUS = 128_000`
- `MAX_OUTPUT_TOKENS_SONNET = 64_000`
- `RECOMMENDED_MAX = 500_000`
- `CROSS_CHECK_OVERHEAD = 50_000`
- `CROSS_CHECK_OUTPUT_BUDGET = 128_000`
- `CROSS_CHECK_RECOMMENDED_MAX = 822_000`
- `count_tokens(text)` — local cl100k_base
- `count_tokens_via_api(model, system, messages, *, client=None)` — Anthropic exact (`None` on failure)

### edit_locator.py — Locator

- `locate_edits(findings, paragraph_map)` — returns one `LocatorResult` per finding
- `LocatorResult.safety_category` (Phase 4) — AUTO_SAFE / AUTO_WITH_CAUTION / MANUAL_REVIEW / REPORT_ONLY
- `_fuzzy_match` (Phase 9.3) — length-ratio + `quick_ratio` prefilters before paying for `SequenceMatcher.ratio()`
- `_section_anchored_match` — narrows by section header neighborhood

### edit_candidates.py — Safety categories

Constants `SAFETY_AUTO_SAFE`, `SAFETY_AUTO_WITH_CAUTION`, `SAFETY_MANUAL_REVIEW`, `SAFETY_REPORT_ONLY`. `EditCandidate.safety_category` defaults to REPORT_ONLY.

### spec_editor.py — DOCX edits + annotation

- `apply_edits_to_spec(source_path, output_path, edit_actions)` — surgical edits in safe order (in-place replacements → ADDs (descending body_index) → whole-paragraph DELETEs (descending)); revalidates preconditions immediately before mutation
- `annotate_spec_with_suggestions(source_path, output_path, edit_actions)` — Phase 4.6: writes a copy with a yellow-highlighted suggestion paragraph after each anchor; the original text is never changed
- `build_edit_actions(locator_results, *, allow_caution=True)` — gates auto-application by `safety_category`

### apply_edits.py — Orchestration

`execute_edit_plan(selected_finding_indices, all_findings, cross_check_findings, extracted_specs, source_paths, output_dir, *, log, mode="edit"|"annotate")`. Fans out to every entry in `Finding.affected_files` so multi-file findings edit (or annotate) every affected spec.

### report_exporter.py — Word export

`export_report(result, output_path, *, project_context, cross_check_enabled, cycle_label)`.

### diagnostics.py — Diagnostics report

`DiagnosticsReport.summary()` returns a dict with totals + `failed_specs`, `skipped_specs`, `edit_skip_reasons`, `ambiguous_locator_count`, `edits_applied_total/skipped_total/failed_total`, `verification_evidence` (grounded / ungrounded / escalated / cache_hits / local_skips / search_errors / search_requests), `output_telemetry` (max_observed / p50 / p95 / truncated_calls / max_cap_observed), `search_budget` (ceiling / saturated_calls / p50 / p95). The `DiagnosticsWindow` widget renders all of these inline; `to_text()` and `to_dict()` produce the export formats.

---

- Code-cycle selector segmented control (`2022` / `2025`)
- Review-mode segmented control (Strict / Comprehensive / Safe edit)
- Mode labels: `Real-time (FAST: Expensive!)` and `Batch (SLOW: Cheap!)`
- Real-time cost confirmation dialog with batch-switch option
- Token gauge labels approximate vs. exact (API) counts; runs the API count async after the live cl100k_base estimate
- `_make_diag_log` / `_make_diag_progress` honor the explicit `phase=` kwarg from pipeline callers (no message keyword sniffing)
- Resume state uses `resume_state.py` serializers/deserializers; legacy v1 migration path retained
- File browser filter restricted to `.docx`

## Feature flags

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_PROMPT_CACHE` | `1` | `0` disables prompt caching |
| `SPEC_CRITIC_STRUCTURED_OUTPUTS` | `1` | `0` falls back to tagged-JSON parsing |
| `SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT` | `1` | `0` skips Anthropic count_tokens |
| `SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT` | `1` | `0` reverts to Opus-everywhere |
| `SPEC_CRITIC_LOCAL_VERIFICATION_SKIP` | `1` | `0` web-verifies all findings |
| `SPEC_CRITIC_PARALLEL_CROSS_CHECK` | `1` | `0` runs cross-check after verification |
| `SPEC_CRITIC_REALTIME_FALLBACK_THRESHOLD` | `5` | Real-time fallback when retry tail ≤ N |
| `SPEC_CRITIC_VERIFICATION_MAX_USES` | `5` | Web-search tool max_uses |
| `SPEC_CRITIC_REVIEW_MODEL` | `claude-opus-4-6` | Override review model |
| `SPEC_CRITIC_VERIFICATION_MODEL` | (auto) | Override verifier model |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | `claude-opus-4-6` | Override escalation model |
| `SPEC_CRITIC_EXTRACTION_CACHE` | `1` | `0` disables file-extraction cache |

## Dependencies

