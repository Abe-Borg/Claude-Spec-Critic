# CLAUDE.md — Spec Critic v2.11.0

This document is the engineering/operator reference for the Spec Critic codebase. It is intentionally implementation-focused and should be kept aligned with the actual runtime behavior in `src/`.

---

## 1) What it is

Spec Critic is a Python desktop application for reviewing mechanical and plumbing construction specifications for California K-12 DSA projects using Claude. It extracts text from `.docx` files, performs local preprocessing, runs per-spec reviews (real-time or batch), optionally runs cross-spec coordination checks, verifies findings via web search (Sonnet by default with Opus escalation), and exports the results as a Word report. Optional auto-edit and annotation modes write a copy of each spec with surgical edits or yellow-highlighted suggestions.

The two processing modes (real-time and batch) share identical prompts, models, tool schemas, output caps, and parsing logic, so findings should be functionally equivalent across modes. The only intentional asymmetry is the 300k extended-output path, which is gated to the batch API by the `output-300k-2026-03-24` beta header (Anthropic does not honor it on streaming requests) and only triggers for inputs ≥200k tokens. Real-time pays full per-token pricing for immediate results; batch pays ~50% for asynchronous results delivered within ~45 min – 24 h.

The tool's purpose is to:

- identify likely code/compliance and coordination issues,
- classify findings with severity + confidence,
- verify findings with web-search-backed evidence,
- generate stakeholder-readable reports,
- optionally apply precise edits back to Word source files.

### Source layout

```
src/
├── __init__.py             # Package version (2.11.0)
├── gui.py                  # CustomTkinter GUI — all user interaction
├── widgets.py              # Reusable UI components
├── pipeline.py             # Core orchestration + FindingGroup/FindingOccurrence
├── api_config.py           # Centralized model/output-cap/feature-flag config
├── structured_schemas.py   # Tool-use schemas for review/cross-check/verification
├── review_modes.py         # Strict / Comprehensive / Safe-edit profiles
├── prompts.py              # System + user prompt builders (mode-aware)
├── prompt_serialization.py # Central escape / wrap helpers for prompt boundaries
├── reviewer.py             # Anthropic API client (streaming + tool-use parsing)
├── cross_checker.py        # Cross-spec coordination (chunked by CSI division)
├── verifier.py             # Verification (Sonnet/Opus routing, real-time fallback)
├── verification_router.py  # Initial / escalation model + local-skip classification
├── verification_cache.py   # Persistent claim-keyed verdict cache (JSON on disk)
├── verification_profiles.py # Verification profile classifier + per-profile search budgets (Chunk H)
├── verification_modes.py   # Explicit verification modes + per-mode policy + routing (Chunk I)
├── source_grounding.py     # URL normalization + cited-source validation (Chunk H)
├── triage.py               # Haiku-based verification triage (opt-in)
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

### High-level flow

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
         ├──── Export Report ── report_exporter.export_report (.docx)        │
         └──── Apply edits ──── apply_edits.execute_edit_plan(mode=          │
                                "edit"|"annotate")                           │
```

---

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
Canonical issue object containing raw model content, token usage, elapsed time, stop reason, parse status, and optional error context. Schema:

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
Phase 3 evidence model: `grounded`, `model_used`, `escalated`, `cache_status`, `web_search_requests`, `successful_source_count`, `search_error_count`. Verdicts cannot be `CONFIRMED` / `CORRECTED` unless `grounded` is True.

Chunk H source-grounding evidence: `searched_sources` (URLs the web_search tool actually fetched), `cited_sources` (URLs the model emitted in its verdict payload), `accepted_sources` (cited URLs that matched a searched URL after normalization), `rejected_sources` (`[{"url", "reason"}]` for cited URLs that did not match any searched URL), and `verification_profile` (one of `code_standard` / `california_ahj` / `manufacturer` / `constructability` / `internal_coordination`). The public `sources` list is replaced with `accepted_sources` so reports never echo model-invented URLs. When the model emits citations but every citation is ungrounded, `CONFIRMED` / `CORRECTED` is downgraded to `UNVERIFIED` inside `_apply_source_grounding`.

Chunk I verification mode: `verification_mode` (one of `local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`) stamps the routing decision used for the verification call. `_local_skip_result()` stamps `local_skip`; `_run_verification_call` and `_classify_wave_results` stamp the mode returned by `verification_modes.select_verification_mode(...)`. The cache and resume state round-trip the field so a restored hit carries its original routing tag. Pre-Chunk-I records load with `verification_mode = ""`, which `mode_policy()` treats as STANDARD_REASONING for backward compatibility.

### BatchSubmission / CollectedBatchState (pipeline.py)
Carry `review_mode: str` so resume restores the exact prompt path.

---

## 3) Module Reference

### api_config.py — Centralized API configuration

**Public API:**
- Model identifiers: `MODEL_OPUS_46`, `MODEL_OPUS_47`, `MODEL_SONNET_46`, `MODEL_HAIKU_45`
- Defaults: `REVIEW_MODEL_DEFAULT` (Opus 4.7), `CROSS_CHECK_MODEL_DEFAULT` (Opus 4.7), `VERIFICATION_MODEL_DEFAULT` (Sonnet 4.6 by default), `VERIFICATION_ESCALATION_MODEL` (Opus 4.7), `SYNTHESIS_MODEL_DEFAULT` (Haiku 4.5), `TRIAGE_MODEL_DEFAULT` (Haiku 4.5)
- Output caps: `review_max_tokens()`, `cross_check_max_tokens()`, `verification_max_tokens(model, *, phase=PHASE_VERIFICATION)`, `synthesis_max_tokens()`, `triage_max_tokens()`, `output_cap_for_model()`, `phase_output_cap(phase, *, model)` (centralized phase→budget registry; every helper routes through it), `assert_extended_output_allowed()`
- Model capability policy (Chunk B): `ModelCapabilities` frozen dataclass, `model_capabilities(model)`, `model_supports_adaptive_thinking(model)`, `thinking_config_for(*, model, phase)`, `apply_thinking_config(kwargs, *, model, phase)`. Whitelist registry covers Opus 4.6/4.7, Sonnet 4.6, Haiku 4.5; unknown models fall back to safe defaults that disable every capability flag. The `thinking` request key is added only when both the model supports adaptive thinking and the phase is not in the opt-out set. Phase identifiers: `PHASE_REVIEW`, `PHASE_BATCH_REVIEW`, `PHASE_CROSS_CHECK`, `PHASE_SYNTHESIS`, `PHASE_VERIFICATION`, `PHASE_VERIFICATION_RETRY`, `PHASE_VERIFICATION_CONTINUATION`, `PHASE_TRIAGE`.
- Prompt caching: `prompt_caching_enabled()`, `system_prompt_with_cache()`, `tools_with_cache()`, `extract_cache_usage()`
- Token counting: `token_count_preflight_enabled()` (default on)
- Sonnet routing: `verification_sonnet_default_enabled()` (default on)
- Web-search tool: `WEB_SEARCH_TOOL` (web_search_20260209, blocked-only domain list, default `max_uses=5`); per-severity budget via `web_search_max_uses_for_severity(severity)` and `web_search_tool_for_severity(severity)`

### structured_schemas.py — Tool-use schemas

**Public API:**
- `review_findings_tool()`, `review_tool_choice()`
- `cross_check_findings_tool()`, `cross_check_tool_choice()`
- `verification_verdict_tool()` (no forcing tool_choice; web_search runs first)
- `extract_tool_use_block(response, tool_name)` — pulls the matching tool's `input` off a response (works on SDK objects and plain dicts)
- `structured_outputs_enabled()` — env-toggle (default on)

### review_modes.py — Review mode profiles

`ReviewMode` enum with STRICT / COMPREHENSIVE / SAFE_EDIT. `coerce_review_mode(value)` accepts strings (`"strict"`, `"comprehensive"`, `"safe_edit"`) for convenience. `DEFAULT_REVIEW_MODE = COMPREHENSIVE`.

### prompts.py — Prompt builders

**Public API:**
- `get_system_prompt(cycle, mode=...)`
- `get_single_spec_user_message(spec_content, filename, project_context, *, cycle, mode=...)`

Both inject the active review mode banner, the mode-specific task text, and the editability clause. The system prompt instructs the model to call the structured tool (with a tagged-JSON fallback for compatibility).

### prompt_serialization.py — Central prompt-boundary helpers (Chunk G)

Single source of truth for safely embedding untrusted content (spec bodies, project context, finding fields, filenames) in pseudo-XML wrappers. The previous behavior had three separate `_xml_escape` helpers that escaped element content only, leaving attribute values vulnerable to quote injection and several wrappers (spec body, project context, triage findings) entirely unescaped.

**Public API:**
- `escape_text(value)` — escape `&`, `<`, `>` for element content.
- `escape_attr(value)` — escape `&`, `<`, `>`, `"`, `'` for attribute values (the previous helpers only handled the three element-content reserved characters, so a filename like `weird".docx` silently truncated the opening tag).
- `wrap_data_block(tag, content, *, attrs=None)` — single-line `<tag k="v">body</tag>` with both halves escaped.
- `wrap_document_block(tag, content, *, attrs=None)` — multi-line equivalent for spec / context bodies; wrapper tags land on their own lines so the body's newline layout is preserved.
- `render_blocks(iterable)` — `\n`-join that drops empties.
- Wrapper-tag string constants: `TAG_SPEC`, `TAG_PROJECT_CONTEXT`, `TAG_CORPUS`, `TAG_ALREADY_IDENTIFIED`, `TAG_PRIOR_FINDING`, `TAG_FINDING`, `TAG_FINDINGS`, `TAG_CHUNK_FINDINGS`, `TAG_CHUNK`.

Used by `prompts.py`, `cross_checker.py`, `triage.py`, and `verifier.py`. The stable instruction prefix in each prompt builder is unchanged byte-for-byte, so prompt-caching breakpoints remain pinned (verified by `TestPromptCacheBreakpointSafety` in `tests/test_chunk_g_prompt_serialization.py`).

### code_cycles.py — California code cycles

**Public API:** `CodeCycle`, `CALIFORNIA_2025`, `AVAILABLE_CYCLES`, `DEFAULT_CYCLE` (= `CALIFORNIA_2025`).

### resume_state.py — Durable resume state

**Public API:**
- Phase constants: `PHASE_REVIEW_POLL`, `PHASE_REVIEW_COLLECT`, `PHASE_VERIFICATION_POLL`, `PHASE_VERIFICATION_WAVE_POLL`, `PHASE_CROSS_CHECK`, `PHASE_CROSS_CHECK_VERIFICATION_POLL`, `PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL`, `PHASE_FINALIZE`
- `SUPPORTED_PHASES`
- `build_resume_state(...) -> dict`
- `deserialize_resume_state(payload) -> dict`

Phase 5.5: `serialize_extracted_spec` records SHA-256 digests of both the extracted content and the underlying source file. `deserialize_extracted_spec` warns when either differs from the saved digest at resume time.

### batch.py — Anthropic Message Batches wrapper

- `submit_review_batch(specs, ..., mode)` — emits requests with the structured tool when enabled
- `poll_batch(batch_id) -> BatchStatus`
- `retrieve_review_results(job, *, model)` — extracts findings from the tool_use block (falls back to text)
- `submit_verification_batch(...)`, `retrieve_verification_results_detailed(...)`, `cancel_batch(...)`. The legacy text-only `retrieve_verification_results` was removed in Chunk D — wave parsing now lives in `verifier._classify_wave_results`, which routes through the canonical parser.

### batch_runtime.py — Polling runtime

Progressive poll backoff: base interval for ~5 minutes, then linearly ramps to 120 s, then holds. `PollPolicy` carries timeout / error-threshold / no-progress thresholds.

### cross_checker.py — Cross-spec coordination

- `run_cross_check(specs, existing_findings, ...)` — single-pass
- `run_chunked_cross_check(specs, existing_findings, ...)` — chunks by CSI division (Div 21 / 22 / 23 / Controls / 25 + 01) when the combined input exceeds the recommended cap; merges chunk results locally

### verifier.py — Web-search verification

- `verify_findings(findings, *, progress, cycle, cache)` — real-time path (Sonnet default, Opus escalation)
- `verify_findings_batch(findings, *, log, progress, ...)` — multi-wave batch path
- `verify_finding(finding, *, max_retries=2, cycle, model, cache, escalated)` — single finding
- `prepare_findings_for_verification(findings, *, cycle, cache, log)` — Phase 3 pre-pass (resolves local-skip and cache hits in place)
- `start_verification_batch(...)`, `collect_verification_batch_results(..., realtime_fallback_threshold=5)`
- `_verdict_from_tool_use(message)` — unpack the strict `submit_verification_verdict` tool input (preferred over text parsing)
- Canonical parser (Chunk D): `parse_verification_response(message_or_list) -> VerificationParseOutcome` returns a `(verdict, parse_status)` pair where `parse_status` is one of `PARSE_STATUS_STRUCTURED` / `PARSE_STATUS_TEXT` / `PARSE_STATUS_TEXT_PARSE_ERROR` / `PARSE_STATUS_NO_CONTENT`. Used by both `_run_verification_call` (real-time) and `_classify_wave_results` (batch). `classify_verification_stop_reason(stop_reason) -> STOP_CLASS_COMPLETE / STOP_CLASS_PAUSE / STOP_CLASS_INCOMPLETE` centralizes the stop-reason allowlist.

### verification_router.py — Phase 3 routing

- `initial_verification_model()` / `escalation_verification_model()`
- `should_escalate_verification(finding, *, verdict, grounded, ...)` — fires for CRITICAL/HIGH UNVERIFIED when Sonnet was the initial verifier
- `classify_finding_for_verification(finding) -> "web_required" | "local_skip"` — local-skip default-on; only GRIPES with no codeReference and a placeholder/LEED/typo/duplicate/internal-contradiction keyword

### verification_cache.py — Per-run cache (with disk persistence)

`VerificationCache.make_cache_key(finding, cycle)` includes `cycle_label | actionType | codeReference | sha256(claim_summary)`. Only `grounded=True` results are cached. Hits are tagged `cache_status="hit"`.

Phase 10 — disk persistence: `VerificationCache.load_from_disk(path)` and `save_to_disk(path)` round-trip the cache to JSON at `~/.spec_critic/verification_cache.json` (override via `SPEC_CRITIC_CACHE_PATH`). Atomic write via temp-file + rename. Each entry stores `created_ts` and `model_used` for future age/model-based pruning. Default behavior is database mode (no automatic expiration); set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` to a positive integer for opt-in TTL pruning. Cycle label remains in the key, so switching code cycles naturally invalidates entries from the prior cycle.

### triage.py — Haiku verification triage

Optional pre-pass that runs after the keyword classifier and cache lookup but before web verification. Classifies eligible findings as `web_required` or `local_skip` so internally-verifiable findings (e.g. internal contradictions where both sides are quoted, equipment-tag mismatches, formatting issues) skip the expensive Sonnet+web_search call.

Hard safety contract enforced in `is_eligible_for_haiku_triage`:
- Findings with a non-empty `codeReference` are never eligible.
- `CRITICAL` and `HIGH` severity findings are never eligible.
- API failure or parse error → all affected findings default to `web_required`.

Off by default; enable with `SPEC_CRITIC_HAIKU_TRIAGE=1` after validating quality on a representative run.

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

### tokenizer.py — Token accounting

- `count_tokens(text)` — local cl100k_base
- `count_tokens_via_api(model, system, messages, *, client=None)` — Anthropic exact (`None` on failure)
- Chunk E — model-aware fallback gate: `local_estimate_safety_factor(model)` returns a model-specific multiplier (Opus/Sonnet 1.10×, Haiku 1.15×, unknown/None 1.20×) applied to cl100k counts when the exact API count is unavailable. `safe_local_estimate(local_tokens, *, model)` rounds the padded estimate up; `exceeds_per_call_limit_for_model(spec_tokens, overhead_tokens, *, model)` is the model-aware version of `exceeds_per_call_limit`. The exact Anthropic count remains authoritative when available — these helpers only run on the fallback path. The pipeline preflight (`_prepare_specs`) now (a) uses the selected model for `count_tokens_via_api` instead of hard-coding Opus, and (b) raises `ValueError` when the exact count exceeds `RECOMMENDED_MAX` (previously it only logged a warning while the cl100k count was the only hard gate).

Constants:
- `MAX_CONTEXT_TOKENS = 1_000_000`
- `MAX_OUTPUT_TOKENS_OPUS = 128_000`
- `MAX_OUTPUT_TOKENS_SONNET = 64_000`
- `MAX_OUTPUT_TOKENS_HAIKU = 64_000`
- `RECOMMENDED_MAX = 500_000`
- `CROSS_CHECK_OVERHEAD = 50_000`
- `CROSS_CHECK_OUTPUT_BUDGET = 128_000`
- `CROSS_CHECK_RECOMMENDED_MAX = 822_000`

Output caps live in `api_config.py`:
- `REVIEW_OUTPUT_CAP = 128_000` (unified baseline; real-time and batch use the same cap so findings cannot diverge between modes)
- `REVIEW_OUTPUT_CAP_BATCH_EXTENDED = 300_000` (batch-only; requires the `output-300k-2026-03-24` beta header, which is not honored on streaming requests)
- `CROSS_CHECK_OUTPUT_CAP = 96_000`
- `VERIFICATION_OUTPUT_CAP = 16_000` (verdicts are 1–2 sentences; tightened from 32k)
- `SYNTHESIS_OUTPUT_CAP = 32_000` (cross-discipline synthesis on Haiku)
- `HAIKU_TRIAGE_OUTPUT_CAP = 8_000` (triage classifications)

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

## 4) GUI Notes (gui.py / widgets.py)

- Review-mode segmented control (Strict / Comprehensive / Safe edit)
- Mode labels: `Real-time (FAST: Expensive!)` and `Batch (SLOW: Cheap!)`
- Real-time cost confirmation dialog with batch-switch option
- Token gauge labels approximate vs. exact (API) counts; runs the API count async after the live cl100k_base estimate
- `_make_diag_log` / `_make_diag_progress` honor the explicit `phase=` kwarg from pipeline callers (no message keyword sniffing)
- Resume state uses `resume_state.py` serializers/deserializers; legacy v1 migration path retained
- File browser filter restricted to `.docx`

---

## 5) Prompting and Code-Cycle Behavior

- Prompts are mode-aware (Strict / Comprehensive / Safe-edit) and target the California 2025 code cycle.
- `get_system_prompt(cycle, mode=...)` injects the mode banner and editability clause; `get_single_spec_user_message(...)` emits the per-spec task text with project context.
- The system prompt instructs the model to call the structured tool (`submit_review_findings`); a tagged-JSON fallback exists for compatibility with older paths.
- `DEFAULT_CYCLE = CALIFORNIA_2025`. Cycle labels are part of the verification cache key, so switching cycles naturally invalidates prior entries.

---

## 6) Verification Routing and Web Search

### Verification profiles (Chunk H)

Every verification call classifies the finding into one of five `VerificationProfile` values before the request is built:

| Profile | When | `max_uses` ceiling (CRITICAL → HIGH → MEDIUM → GRIPES) |
|---|---|---|
| `california_ahj` | finding mentions California / DSA / HCAI / Title 24 / AHJ | 8 / 7 / 5 / 3 |
| `code_standard` | finding cites a code section or a standards body (CBC, NFPA, ASHRAE, IAPMO, …) without California signals | 7 / 7 / 5 / 3 |
| `manufacturer` | finding mentions a manufacturer / model number / datasheet / submittal | 6 / 5 / 4 / 3 |
| `constructability` | default for substantive technical claims with no clear kind signal | 5 / 5 / 4 / 3 |
| `internal_coordination` | finding mentions internal contradiction / placeholder / LEED / typo / duplicate paragraph | 2 / 2 / 1 / 1 |

`classify_finding_profile(finding)` lives in `src/verification_profiles.py`. Profile sets the ceiling and severity modulates within it (Chunk H Directive 7: severity is *subordinate* to profile). `build_verification_tools_for_profile(profile, severity)` in `batch.py` is the profile-aware variant of `build_verification_tools(severity)`; both real-time, batch initial, and batch retry / continuation builders route through it and stamp the profile string into `VerificationResult.verification_profile`.

### Verification modes (Chunk I)

`select_verification_mode(finding, *, local_skip, escalated, cached_mode)` in `src/verification_modes.py` picks one of four `VerificationMode` values:

| Mode | When | Model | Thinking | Search budget | Escalates? |
|---|---|---|---|---|---|
| `local_skip` | keyword classifier or Haiku triage said `local_skip` | (none — no API call) | n/a | 0 (no search) | no |
| `strict_structured` | GRIPES severity OR non-GRIPES `internal_coordination` profile | Sonnet | off | profile ceiling × 0.5, floor 1 | no |
| `standard_reasoning` | default for substantive technical claims | Sonnet (defers to `VERIFICATION_MODEL_DEFAULT`) | on | full profile ceiling | yes (via `should_escalate_verification`) |
| `deep_reasoning` | `escalated=True`, OR initial pass for CRITICAL `california_ahj` (when Sonnet-default is on) | Opus (defers to `VERIFICATION_ESCALATION_MODEL`) | on | full profile ceiling | no (terminal) |

Rules in priority order: cache-hit replay → local_skip → escalated → CRITICAL `california_ahj` initial pass → GRIPES → non-GRIPES `internal_coordination` → default. `mode_policy(mode)` returns the frozen `ModePolicy` bundle (`model`, `thinking_enabled`, `search_budget_multiplier`, `web_search_enabled`, `allows_escalation`); `mode_search_budget(mode, *, profile_ceiling)` composes the multiplier with `profile_max_uses(...)` (floor of 1). `_run_verification_call` stamps the routed mode on every result; `_classify_wave_results` re-derives the mode per wave finding so retry-wave entries are tagged `deep_reasoning`. Diagnostics' `summary()` exposes `verification_modes` and `verification_profiles` count dicts, rendered as `Modes:` / `Profiles:` lines in `to_text()`.

### Source grounding (Chunk H)

Once a verdict is parsed, `_apply_source_grounding` (verifier.py) partitions sources into four explicit concepts:

- `searched_sources` — URLs the web_search server tool actually retrieved.
- `cited_sources` — URLs the model emitted in its `submit_verification_verdict` payload.
- `accepted_sources` — cited URLs whose normalized form matched a searched URL.
- `rejected_sources` — `[{"url", "reason"}]` for cited URLs that did not match any searched URL.

Normalization (`source_grounding.normalize_url`) folds `http`/`https`, drops default ports / fragments / tracking params, sorts query params, and trims trailing slashes / cosmetic punctuation so trivial differences never cause a real citation to be rejected. The public `VerificationResult.sources` list is replaced with `accepted_sources` so reports and the verification cache never persist model-invented URLs. If the model emitted citations but **every citation was ungrounded**, `CONFIRMED` / `CORRECTED` is downgraded to `UNVERIFIED` with an explanation suffix (`(downgraded: model cited sources that did not appear in web_search results)`). Verdicts with no citations are not affected by this helper — the Phase 3 `_enforce_grounding_invariant` continues to handle the "no citations AND no searched sources" case.

### Source-quality blocklist

A blocked-domain list filters social/AI-assistant/forum/general-encyclopedia sources from `web_search_20260209`. California priority sources are documented in the verifier system prompt rather than encoded as an allow-list (mixing allow + block lists is unsupported by the tool).

---

## 7) Feature Flags

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_PROMPT_CACHE` | `1` | `0` disables prompt caching |
| `SPEC_CRITIC_STRUCTURED_OUTPUTS` | `1` | `0` falls back to tagged-JSON parsing |
| `SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT` | `1` | `0` skips Anthropic count_tokens |
| `SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT` | `1` | `0` reverts to Opus-everywhere |
| `SPEC_CRITIC_LOCAL_VERIFICATION_SKIP` | `1` | `0` web-verifies all findings |
| `SPEC_CRITIC_PARALLEL_CROSS_CHECK` | `1` | `0` runs cross-check after verification |
| `SPEC_CRITIC_REALTIME_FALLBACK_THRESHOLD` | `5` | Real-time fallback when retry tail ≤ N |
| `SPEC_CRITIC_VERIFICATION_MAX_USES` | `5` | Default web_search `max_uses` (when severity tiering doesn't apply) |
| `SPEC_CRITIC_REVIEW_MODEL` | `claude-opus-4-7` | Override review model |
| `SPEC_CRITIC_CROSS_CHECK_MODEL` | `claude-opus-4-7` | Override cross-check model |
| `SPEC_CRITIC_SYNTHESIS_MODEL` | `claude-haiku-4-5` | Override cross-discipline synthesis model |
| `SPEC_CRITIC_TRIAGE_MODEL` | `claude-haiku-4-5` | Override Haiku verification triage model |
| `SPEC_CRITIC_HAIKU_TRIAGE` | `0` | `1` enables Haiku verification triage augmenting the keyword classifier |
| `SPEC_CRITIC_VERIFICATION_MODEL` | (auto) | Override verifier model |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | `claude-opus-4-7` | Override escalation model |
| `SPEC_CRITIC_VERIFICATION_CACHE_PERSIST` | `1` | `0` disables on-disk verification cache (database mode) |
| `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` | `0` | Positive integer enables age-based cache pruning |
| `SPEC_CRITIC_CACHE_PATH` | `~/.spec_critic/verification_cache.json` | Override cache path |
| `SPEC_CRITIC_EXTRACTION_CACHE` | `1` | `0` disables file-extraction cache |

---

## 8) Dependencies

- Python 3.11+
- Anthropic API key (`ANTHROPIC_API_KEY`)
- Runtime packages: `anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic` (see `requirements.txt` for pinned versions)

---

## 9) Test Harness

The test suite is hermetic by default — no Anthropic API key, no network — and runs in a few seconds. Key conventions:

- `tests/conftest.py` injects a placeholder `ANTHROPIC_API_KEY` so production modules import cleanly. Tests that require a real Anthropic endpoint use `@pytest.mark.network`; they are skipped unless `ANTHROPIC_API_KEY` is set to a non-placeholder value.
- GUI-dependent tests (`test_core_regressions.py`, `test_gui_refactor_modules.py`) skip automatically at collection time when `tkinter` is unavailable — see `pytest_ignore_collect` in `tests/conftest.py`.
- Test markers (declared in `pyproject.toml`):
  - `smoke` — fast import/compile checks (`test_chunk_a_smoke.py`).
  - `fixtures` — round-trips fake Anthropic responses through production parsers (`test_chunk_a_fixtures.py`).
  - `request_shape` — captures kwargs passed to the Anthropic SDK without network (`test_request_payload_shape.py`).
  - `slow` / `network` — opt-in.
- Fake Anthropic response builders: `tests/fixtures/fake_anthropic.py`. Cases covered: structured review tool call, structured verification verdict tool call (incl. `stop_reason="tool_use"`), JSON-text fallback, `max_tokens` incomplete. Each builder accepts `dict_shape=True` to emit plain-dict responses (the batch retrieval path can return either form).
- In-memory DOCX builders: `tests/fixtures/docx_fixtures.py` for paragraph / table / real-world-section specs used by locator and edit-safety tests.
- Request-shape test plumbing: `FakeClient` in `test_request_payload_shape.py` captures `messages.stream`, `messages.batches.create`, and `beta.messages.batches.create` kwargs into `CapturedRequest` / `CapturedBatch`. Use `fake_client` (which monkeypatches `_get_client` in `reviewer` / `batch` / `verifier` / `cross_checker`) to exercise any request-building code path without a real client.
