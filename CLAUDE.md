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
├── review_request_builder.py # Central review request shape builder
├── cross_checker.py        # Cross-spec coordination (chunked by CSI division)
├── verifier.py             # Verification (Sonnet/Opus routing, real-time fallback)
├── verification_router.py  # Initial / escalation model + local-skip classification
├── verification_cache.py   # Persistent claim-keyed verdict cache (JSON on disk)
├── verification_profiles.py # Verification profile classifier + per-profile search budgets
├── verification_modes.py   # Explicit verification modes + per-mode policy
├── verification_routing.py # Unified routing decision + request builder
├── source_grounding.py     # URL normalization + cited-source validation
├── retry_policy.py         # Retry, continuation, and batch-failure taxonomy
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
├── report_status.py        # ReportStatus / EditActionLabel + classifiers
├── resume_state.py         # Durable resume state (with file-hash validation)
├── diagnostics.py          # In-memory diagnostics report
├── cost_estimator.py       # USD cost estimator (pricing table + phase aggregation)
├── api_key_store.py        # API key loading and persistence
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
       (tool: submit_review_findings; tagged-JSON fallback)                  │
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
    document_id: str = ""                # filename stem
```

### ParagraphMapping (extractor.py)
Per-element record used by the locator. Includes `body_index`, `element_type`, `section_index`, formatting fields (`run_count`, `distinct_formatting_runs`), `element_id` (stable per-run id — `p<body_index>` for body paragraphs, `t<table>r<row>` for table cells, `s<section><h|f><i>` for header/footer paragraphs, `meta:hf` for the synthetic header/footer delimiter), and `section_id` (most recent heading paragraph text seen during extraction; best-effort attribution via `_is_heading_paragraph`).

### Finding (reviewer.py)
Canonical issue object. Schema:

```python
@dataclass
class Finding:
    severity: str
    fileName: str
    section: str
    issue: str
    actionType: str         # ADD / EDIT / DELETE / REPORT_ONLY
    existingText: str | None
    replacementText: str | None
    codeReference: str | None
    confidence: float = 0.5
    verification: VerificationResult | None = None
    affected_files: list[str] = field(default_factory=list)
    anchorText: str | None = None        # ADD only
    insertPosition: str | None = None    # "before" | "after" (ADD only)
    evidenceElementId: str | None = None # cite a ParagraphMapping.element_id
    edit_proposal: EditProposal | None = None
    finding_id: str = ""
    upstream_finding_ids: list[str] = field(default_factory=list)
    independent_evidence_ids: list[str] = field(default_factory=list)
    suppression_reason: str | None = None
    demotion_reason: str | None = None
    occurrence_originals: list["Finding"] = field(default_factory=list)
```

`evidenceElementId` points to the `ParagraphMapping.element_id` that owns the finding's quote; the structured tool schema lists it as required-but-nullable. The locator's `_id_anchored_match` prefers this id when set and revalidates the quote against the live element.

`validate_edit_shape(action, *, existing_text, replacement_text, anchor_text=None, insert_position=None)` returns a demotion reason when an EDIT/DELETE/ADD action lacks an action-specific required field (EDIT needs both `existingText` and `replacementText`; DELETE needs `existingText`; ADD needs `anchorText` + `insertPosition` in {`before`, `after`} + `replacementText`). `_parse_findings` runs the validator on every payload; on demotion the action becomes REPORT_ONLY, edit fields are cleared, and `demotion_reason` is stamped. `as_edit_proposal()` re-runs the validator defensively so legacy resume payloads and directly-constructed test Findings with invalid shapes return None.

Cross-check findings carry `upstream_finding_ids` (review-finding ids the coordination claim depends on) and `independent_evidence_ids` (raw-spec `element_id` values supporting the claim). `pipeline.classify_cross_check_dependencies` drops a cross-check finding only when every cited upstream is DISPUTED *and* no independent spec evidence exists; otherwise the finding survives. Findings without cited ids fall back to a `(filename, section)` heuristic, labeled as such in logs. Dropped findings land on `ReviewResult.suppressed_findings` with `suppression_reason` set.

### FindingGroup / FindingOccurrence (pipeline.py)
Formal split between the display concept ("same issue appears in N files") and the executable-edit concept ("apply this change to file X at location Y"). `group_findings(findings)` returns one `FindingGroup` per deduped finding with one `FindingOccurrence` per file in `affected_files`. `expand_to_occurrences(findings)` flattens to per-file occurrences, skipping placeholders.

`Finding.occurrence_originals: list[Finding]` holds the per-file pre-merge member findings whenever `_deduplicate_findings` collapses findings across files. `FindingOccurrence.original_finding: Finding | None` plus `executable_finding()` / `has_original()` helpers let `apply_edits.execute_edit_plan` use each file's own `existingText` / `replacementText` / `anchorText` / `evidenceElementId` / `edit_proposal` instead of fanning the representative's text across files that may differ. Absent on a non-representative file → routed to manual review with an explicit `EditReport` warning.

### ReviewResult (reviewer.py)
Findings list plus prompt-cache telemetry (`cache_creation_input_tokens`, `cache_read_input_tokens`), elapsed time, stop reason, parse status, optional error, and `structured_payload: dict | None` holding the parsed `submit_review_findings` / `submit_cross_check_findings` tool input. Suppressed cross-check findings live on `suppressed_findings`. `structured_payload` is in-memory only — not persisted by `resume_state`.

### VerificationResult (verifier.py)
Verdict + evidence record. Verdicts cannot be `CONFIRMED` / `CORRECTED` unless `grounded` is True and at least one accepted external citation is present.

Source-grounding fields:
- `searched_sources` — URLs the web_search tool actually fetched.
- `cited_sources` — URLs the model emitted in its verdict payload.
- `accepted_sources` — cited URLs whose normalized form matched a searched URL (kept in sync as the public `sources` list).
- `rejected_sources` — `[{"url", "reason"}]` for cited URLs that did not match any searched URL.

Routing fields: `verification_profile` (one of `code_standard` / `california_ahj` / `manufacturer` / `constructability` / `internal_coordination`) and `verification_mode` (one of `local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`).

Escalation telemetry: `escalation_attempted`, `initial_model`, `initial_verdict`, `escalation_changed_verdict`, `escalation_reason` (one of `initial_unverified` / `initial_ungrounded` / `initial_all_search_errors` / `router_decision`). Aggregated by diagnostics into an `escalation_stats` block. Not persisted by the verification cache.

Other fields: `escalated`, `cache_status` (`n/a` / `miss` / `hit` / `local_skip`), `web_search_requests`, `successful_source_count`, `search_error_count`, `structured_payload` (parsed verdict tool input; in-memory only), `retry_telemetry` (`attempts`, `failure_class`, `terminal_reason`, `continuation_count`).

### BatchSubmission / CollectedBatchState (pipeline.py)
Carry `review_mode: str` so resume restores the exact prompt path, plus every deterministic alert list so the report can render them.

---

## 3) Module Reference

### api_config.py — Centralized API configuration

- Model identifiers: `MODEL_OPUS_46`, `MODEL_OPUS_47`, `MODEL_SONNET_46`, `MODEL_HAIKU_45`.
- Defaults: `REVIEW_MODEL_DEFAULT` (Opus 4.7), `CROSS_CHECK_MODEL_DEFAULT` (Opus 4.7), `VERIFICATION_MODEL_DEFAULT` (Sonnet 4.6 by default), `VERIFICATION_ESCALATION_MODEL` (Opus 4.7), `SYNTHESIS_MODEL_DEFAULT` (Haiku 4.5), `TRIAGE_MODEL_DEFAULT` (Haiku 4.5).
- Output caps: `review_max_tokens()`, `cross_check_max_tokens()`, `verification_max_tokens(model, *, phase=PHASE_VERIFICATION)`, `synthesis_max_tokens()`, `triage_max_tokens()`, `output_cap_for_model()`, `phase_output_cap(phase, *, model)` (centralized phase→budget registry; every helper routes through it), `assert_extended_output_allowed()`.
- Model capability policy: `ModelCapabilities` frozen dataclass, `model_capabilities(model)`, `model_supports_adaptive_thinking(model)`, `model_supports_effort(model)`, `model_supports_extended_output_beta(model)`, `thinking_config_for(*, model, phase)`, `apply_thinking_config(kwargs, *, model, phase)`. Whitelist registry covers Opus 4.6/4.7, Sonnet 4.6, Haiku 4.5; unknown models fall back to safe defaults that disable every capability flag.
- Phase identifiers: `PHASE_REVIEW`, `PHASE_BATCH_REVIEW`, `PHASE_CROSS_CHECK`, `PHASE_SYNTHESIS`, `PHASE_VERIFICATION`, `PHASE_VERIFICATION_RETRY`, `PHASE_VERIFICATION_CONTINUATION`, `PHASE_TRIAGE`.
- Effort policy: `effort_config_for(*, model, phase)`, `apply_effort_config(kwargs, *, model, phase)`. Sonnet verification: medium. Opus verification (escalation): high. Opus/Sonnet review and cross-check: high. Synthesis/triage (Haiku): omitted. `SPEC_CRITIC_EFFORT_OVERRIDE` forces a level globally; invalid values raise at request-build time.
- Prompt caching: `prompt_caching_enabled()`, `CachePolicy` frozen dataclass, `cache_policy_for(phase)`, `system_prompt_with_cache(prompt, *, phase=None)`, `tools_with_cache(tools, *, phase=None)`, `extract_cache_usage()`. Review / batch review / cross-check / verification (+ retry / continuation) cache both system prompt and tools at the global TTL; synthesis and triage skip caching because the prompts are below the cache minimum (1024 tokens for Sonnet/Opus, 2048 for Haiku). Operators can disable individual phases via `SPEC_CRITIC_CACHE_DISABLE` (comma-separated phase names).
- Service tier: `batch_service_tier()` returns the `service_tier` to set on batch params (default `auto`).
- Token counting preflight: `token_count_preflight_enabled()` (default on).
- Sonnet routing: `verification_sonnet_default_enabled()` (default on).
- Web-search tool: `WEB_SEARCH_TOOL` (`web_search_20260209`, blocked-only domain list, default `max_uses=5`); per-severity budget via `web_search_max_uses_for_severity(severity)` and `web_search_tool_for_severity(severity)`; `build_web_search_tool(max_uses=...)` is the underlying builder.

### structured_schemas.py — Tool-use schemas

- `review_findings_tool()`, `review_tool_choice()`.
- `cross_check_findings_tool()`, `cross_check_tool_choice()` — the cross-check schema extends the shared finding schema with two required arrays, `upstreamFindingIds` and `independentEvidenceIds` (both may be empty).
- `verification_verdict_tool()` (no forcing tool_choice; web_search runs first).
- `triage_classifications_tool()`, `triage_tool_choice()`.
- `extract_tool_use_block(response, tool_name)` — pulls the matching tool's `input` off a response (tolerates SDK Pydantic objects, plain dicts, and Pydantic-model `input` payloads).
- `structured_tool_output_enabled()` — env toggle (default on). Reads `SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT`, with `SPEC_CRITIC_STRUCTURED_OUTPUTS` accepted as a legacy alias. `structured_outputs_enabled()` is the deprecated alias that delegates here.

With `tool_choice={"type": "auto"}` (mandatory whenever adaptive thinking is enabled), the model is *instructed* but not *required* to call the custom tool. The tagged-JSON text fallback parsers remain reachable in `reviewer.py`, `cross_checker.py`, and `verifier.py` and must stay so until/unless a strict-tool-output mode is introduced as the default. `SPEC_CRITIC_STRICT_TOOLS=1` attaches `strict: true` to tool definitions for grammar-constrained sampling; off by default pending real-call verification under thinking.

### review_modes.py — Review mode profiles

`ReviewMode` enum: STRICT / COMPREHENSIVE / SAFE_EDIT. `coerce_review_mode(value)` accepts strings (`"strict"`, `"comprehensive"`, `"safe_edit"`) for convenience. `DEFAULT_REVIEW_MODE = COMPREHENSIVE`.

### prompts.py — Prompt builders

- `get_system_prompt(cycle, mode=...)` — injects the mode banner, mode-specific task text, severity rubric, four-example reference block, editability clause, and review-scope categories (17 categories in comprehensive mode).
- `get_single_spec_user_message(spec_content, filename, project_context, *, cycle, mode=..., paragraph_map=None, pre_detected_alerts=None)` — emits per-spec task text with project context, mode reminder, optional id-tagged spec rendering, optional `<pre_detected>` alerts block, and a trailing `<final_task>` block listing per-call reminders.

When `pre_detected_alerts` is supplied and `pre_detected_alerts_enabled()` is True, a compact `<pre_detected>` block is appended after the spec body. The block lists each `deterministic_rule` once with its match count and up to three example matches (each truncated to ~60 characters), instructing the model not to surface those items as new findings. Operator rollback: `SPEC_CRITIC_PRE_DETECTED_ALERTS=0`.

The `<final_task>` block sits after the spec body (and after `<pre_detected>` when alerts fire) so the stable instruction prefix in front of `<spec ` is unchanged for prompt-cache breakpoints. The "cite evidenceElementId" bullet is only emitted when the id-rendering path is active so `evidenceElementId` never leaks into the message when ids are off.

### prompt_serialization.py — Central prompt-boundary helpers

Single source of truth for safely embedding untrusted content (spec bodies, project context, finding fields, filenames) in pseudo-XML wrappers.

- `escape_text(value)` — escape `&`, `<`, `>` for element content.
- `escape_attr(value)` — escape `&`, `<`, `>`, `"`, `'` for attribute values.
- `wrap_data_block(tag, content, *, attrs=None)` — single-line `<tag k="v">body</tag>` with both halves escaped.
- `wrap_document_block(tag, content, *, attrs=None)` — multi-line equivalent for spec / context bodies; wrapper tags land on their own lines so the body's newline layout is preserved.
- `render_blocks(iterable)` — `\n`-join that drops empties.
- `render_spec_with_ids(content, paragraph_map, *, filename)` — emits one id-tagged `<para>` / `<row>` / `<heading>` element per `ParagraphMapping` so the model can cite `evidenceElementId` alongside the exact quote. `element_ids_enabled()` is the env toggle (`SPEC_CRITIC_ELEMENT_IDS=0` reverts to the plain-body `<spec>` rendering).
- `render_pre_detected_block(alerts, *, filename)` — compact `<pre_detected>` wrapper listing each rule's count and examples, filtered to the caller's filename. Examples truncated via `_PRE_DETECTED_MATCH_PREVIEW_CHARS` (default 60) and capped via `_PRE_DETECTED_EXAMPLES_PER_RULE` (default 3).
- Wrapper-tag string constants: `TAG_SPEC`, `TAG_PROJECT_CONTEXT`, `TAG_CORPUS`, `TAG_ALREADY_IDENTIFIED`, `TAG_PRIOR_FINDING`, `TAG_FINDING`, `TAG_FINDINGS`, `TAG_CHUNK_FINDINGS`, `TAG_CHUNK`, `TAG_PARA`, `TAG_ROW`, `TAG_HEADING`, `TAG_PRE_DETECTED`.

Used by `prompts.py`, `cross_checker.py`, `triage.py`, and `verifier.py`. Stable instruction prefixes are byte-identical across calls so prompt-caching breakpoints remain pinned.

### code_cycles.py — California code cycles

`CodeCycle`, `CALIFORNIA_2025`, `AVAILABLE_CYCLES`, `DEFAULT_CYCLE` (= `CALIFORNIA_2025`).

### resume_state.py — Durable resume state

- Phase constants: `PHASE_REVIEW_POLL`, `PHASE_REVIEW_COLLECT`, `PHASE_VERIFICATION_POLL`, `PHASE_VERIFICATION_WAVE_POLL`, `PHASE_CROSS_CHECK`, `PHASE_CROSS_CHECK_VERIFICATION_POLL`, `PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL`, `PHASE_FINALIZE`.
- `SUPPORTED_PHASES`.
- `build_resume_state(...) -> dict`, `deserialize_resume_state(payload) -> dict`.
- `serialize_extracted_spec` records SHA-256 digests of both the extracted content and the underlying source file; `deserialize_extracted_spec` warns when either differs at resume time.

### batch.py — Anthropic Message Batches wrapper

- `submit_review_batch(specs, ..., mode)` — emits requests with the structured tool when enabled.
- `poll_batch(batch_id) -> BatchStatus`.
- `retrieve_review_results(job, *, model)` — extracts findings from the tool_use block (falls back to text).
- `submit_verification_batch(...)`, `submit_verification_followup_wave(...)`, `retrieve_verification_results_detailed(...)`, `cancel_batch(...)`.
- `build_verification_tools(severity)` / `build_verification_tools_for_profile(profile, severity)` — single source of truth for verification tool payloads. The profile-aware variant uses `profile_max_uses(profile, severity)` so profile sets the ceiling and severity modulates within it.
- `verification_request_includes_verdict_tool()` — mirrors `structured_tool_output_enabled()` so the prompt and the request always agree on tool inclusion.
- `_extract_api_error_message(error_obj)` — clean human-readable error extraction.

### batch_runtime.py — Polling runtime

Progressive poll backoff: base interval for ~5 minutes, then linearly ramps to 120 s, then holds. `PollPolicy` carries `poll_interval_seconds`, `max_elapsed_seconds`, `max_no_progress_seconds`, `max_consecutive_errors`, `backoff_after_seconds`, `max_poll_interval_seconds`. `poll_batch_bounded(batch_id, *, policy, log, progress_cb, cancel_event=None) -> PollOutcome`.

### retry_policy.py — Centralized retry, continuation, and batch-failure policy

- `FailureClass` enum: `RATE_LIMIT`, `SERVER_ERROR`, `CONNECTION`, `INVALID_REQUEST`, `BATCH_ERRORED`, `BATCH_EXPIRED`, `BATCH_CANCELED`, `PARSE_ERROR`, `PAUSE_TURN`, `UNKNOWN`.
- `classify_exception(exc) -> FailureClass` — typed-SDK-first classifier (`isinstance` against `RateLimitError`, `InternalServerError`, `APIStatusError`, `APIConnectionError`, `APIError`); falls back to message-substring matching only for generic exceptions that escaped the SDK's translation layer.
- `classify_batch_failure(*, result_type, error_message, error_type) -> FailureClass`.
- `RetryPolicy` frozen dataclass with per-class backoff multipliers; `DEFAULT_REALTIME_RETRY_POLICY` (3 attempts, base 5s), `DEFAULT_VERIFICATION_RETRY_POLICY` (3 attempts, server-error 3x multiplier).
- `compute_backoff_seconds(policy, *, attempt, failure_class)`.
- `BatchWaveFailureTracker` — records `(custom_id → [FailureClass, ...])` across waves. Same class twice in a row → terminal. `INVALID_REQUEST` is terminal on first occurrence.
- `max_continuations_for_mode(mode_value)` — default 2; `deep_reasoning` gets 4.
- `retry_diagnostics_payload(...)` — JSON-safe dict for diagnostics stamping.

### cross_checker.py — Cross-spec coordination

- `run_cross_check(specs, existing_findings, ...)` — single-pass.
- `run_chunked_cross_check(specs, existing_findings, ...)` — chunks by CSI division (Div 21 / 22 / 23 / Controls / 25 + 01) when the combined input exceeds the recommended cap; merges chunk results locally.
- `_build_cross_check_input` renders every `<prior>` block with the review finding's stable `finding_id` as an `id` attribute so the cross-check model can cite review findings by id when emitting `upstreamFindingIds`. The system prompt has a `<dependency_tracking>` section that tells the model when to cite upstream ids and when to point at raw spec evidence via `independentEvidenceIds`.

### verifier.py — Web-search verification

- `verify_findings(findings, *, progress, cycle, cache)` — real-time path (Sonnet default, Opus escalation).
- `verify_findings_batch(findings, *, log, progress, ...)` — multi-wave batch path.
- `verify_finding(finding, *, max_retries=2, cycle, model, cache, escalated)` — single finding.
- `prepare_findings_for_verification(findings, *, cycle, cache, log)` — pre-pass resolving local-skip and cache hits in place.
- `start_verification_batch(...)`, `collect_verification_batch_results(..., realtime_fallback_threshold=5)`.
- `_verdict_from_tool_use(message)` — unpack the strict `submit_verification_verdict` tool input.
- `parse_verification_response(messages) -> VerificationParseOutcome` — canonical parser returning `(verdict, parse_status)` where status is one of `PARSE_STATUS_STRUCTURED` / `PARSE_STATUS_TEXT` / `PARSE_STATUS_TEXT_PARSE_ERROR` / `PARSE_STATUS_NO_CONTENT`. Used by both real-time (`_run_verification_call`) and batch (`_classify_wave_results`).
- `classify_verification_stop_reason(stop_reason)` — returns `STOP_CLASS_COMPLETE` (tool_use / end_turn) / `STOP_CLASS_PAUSE` (pause_turn) / `STOP_CLASS_INCOMPLETE` (any other).
- `_enforce_grounding_invariant(result)` — downgrades verified-but-ungrounded verdicts to UNVERIFIED, including the CONFIRMED/CORRECTED-without-accepted-citation case.
- `_apply_source_grounding(result, *, searched)` — partitions sources into searched / cited / accepted / rejected and downgrades CONFIRMED/CORRECTED when every cited URL is ungrounded.

### verification_router.py — Routing helpers

- `initial_verification_model()` / `escalation_verification_model()`.
- `should_escalate_verification(finding, *, verdict, grounded, ...)` — fires for CRITICAL/HIGH UNVERIFIED when Sonnet was the initial verifier.
- `classify_finding_for_verification(finding) -> "web_required" | "local_skip"` — local-skip default-on; only GRIPES with no codeReference and a placeholder/LEED/typo/duplicate/internal-contradiction keyword.
- `is_eligible_for_haiku_triage(finding)` — re-export of the triage eligibility filter.

### verification_profiles.py — Profile classifier

`VerificationProfile` enum (`CODE_STANDARD`, `CALIFORNIA_AHJ`, `MANUFACTURER`, `CONSTRUCTABILITY`, `INTERNAL_COORDINATION`). `classify_finding_profile(finding)` is a keyword classifier with priority order: internal-coordination → California/AHJ → manufacturer → code-standard (or non-empty `codeReference`) → constructability. `profile_max_uses(profile, severity)` returns the per-(profile, severity) `max_uses` ceiling. `profile_priority_domains(profile)` returns the authoritative-source guidance paragraph emitted into the verifier system prompt. `profile_web_search_required(profile)` returns False only for `INTERNAL_COORDINATION`.

### verification_modes.py — Verification modes

`VerificationMode` enum (`LOCAL_SKIP`, `STRICT_STRUCTURED`, `STANDARD_REASONING`, `DEEP_REASONING`). `ModePolicy` frozen dataclass holds `(mode, model, thinking_enabled, search_budget_multiplier, web_search_enabled, allows_escalation)`. `mode_policy(mode)` is the table lookup. `select_verification_mode(finding, *, local_skip, escalated, cached_mode)` picks a mode in priority order: cache-hit replay → local_skip → escalated → CRITICAL CALIFORNIA_AHJ initial pass → GRIPES → non-GRIPES INTERNAL_COORDINATION → default. `mode_search_budget(mode, *, profile_ceiling)` applies the per-mode multiplier with floor-of-1.

### verification_routing.py — Unified routing decision

`VerificationRoutingDecision` frozen dataclass holds the full policy bundle (`finding_id`, `severity`, `profile`, `mode`, `model`, `thinking_enabled`, `web_search_enabled`, `web_search_max_uses`, `include_verdict_tool`, `cache_phase`, `max_continuations`, `escalation_eligible`, `local_skip`, `escalated`, `trace_reason`). `to_dict()` / `from_dict(payload)` round-trip a JSON-safe form so the decision can be stashed in `request_map` / `request_contexts`.

`select_routing(finding, *, escalated, cached_mode, model_override, cache_phase, max_continuations, local_skip, include_verdict_tool)` is the pure-function selector. `build_verification_request(decision, *, prompt, system_prompt, assistant_content=None, include_service_tier=False)` builds the kwargs dict every verification path uses (real-time, batch initial, batch retry, batch continuation). `apply_routing_to_result(decision, result)` stamps the routed mode / profile / escalation flag onto a `VerificationResult`.

Trace tags: `TRACE_LOCAL_SKIP`, `TRACE_LOCAL_SKIP_BYPASSED`, `TRACE_CACHED_MODE`, `TRACE_ESCALATED`, `TRACE_CRITICAL_CALIFORNIA`, `TRACE_GRIPES_STRICT`, `TRACE_INTERNAL_COORD_STRICT`, `TRACE_DEFAULT_STANDARD`.

### verification_cache.py — Per-run cache (with disk persistence)

`VerificationCache.make_cache_key(finding, cycle)` includes `cycle_label | actionType | codeReference | sha256(claim_summary)`. It intentionally omits the verifier model: cached entries represent grounded verdict semantics for the same finding claim under the same code cycle, while `VerificationResult.model_used` is stored as provenance inside the entry. Only `grounded=True` results are cached; CONFIRMED/CORRECTED entries require at least one accepted external citation. Hits are tagged `cache_status="hit"`.

Persistence: `load_from_disk(path)` and `save_to_disk(path)` round-trip the cache to JSON at `~/.spec_critic/verification_cache.json` (override via `SPEC_CRITIC_CACHE_PATH`). Atomic write via temp-file + rename. Schema version 2; v1 entries are dropped silently on first load. Each entry stores `created_ts` and `model_used` for future age/model-based pruning, but changing the verifier model does not invalidate existing entries because model identity is not part of the key. Default behavior is database mode (no expiration); set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` to a positive integer for opt-in TTL pruning. `SPEC_CRITIC_VERIFICATION_CACHE_PERSIST=0` disables persistence. Cycle label remains in the key, so switching code cycles naturally invalidates entries from the prior cycle.

Claim digest is 24 hex chars (96 bits); the previous 16-char form is exported as `_LEGACY_CLAIM_DIGEST_LEN` so a future migration tool can detect and prune legacy-form keys explicitly. Lookups against legacy-length keys miss in the new cache, re-ground the claim, and write a fresh 24-char entry.

### source_grounding.py — URL normalization and cited-source validation

- `normalize_url(url)` — folds `http`/`https`, drops default ports / fragments / tracking params, sorts query params, trims trailing punctuation. Falsy / unparseable input returns `""`.
- `validate_cited_sources(cited, searched) -> SourceGroundingOutcome` — partitions cited URLs into accepted (matched a searched URL after normalization) and rejected (with reason: `ungrounded` / `malformed` / `empty`).
- `is_grounded_against_search_results(cited, searched) -> bool` — convenience wrapper.
- `SearchedSource` dataclass (`url`, `title`, `normalized` property). `dedupe_searched_sources(sources)` collapses equivalent URLs by normalized form; accepts `SearchedSource`, dicts, or bare strings.

### api_key_store.py — API key loading and persistence

`load_api_key_from_file()` resolves the Anthropic API key in priority order: OS keyring (if the optional `keyring` package is installed and a working backend is available) → platform config directory (`~/.config/SpecCritic/spec_critic_api_key.txt` on Linux, equivalent paths on macOS/Windows) → executable/source-parent fallback. Returns `""` when nothing is available.

- `keyring_available()` exposes the runtime capability.
- `save_api_key_to_file(value)` writes the primary fallback file with `0o600` permissions on POSIX.
- `load_api_key_from_file()` lazily tightens permissions of any pre-existing fallback file it successfully reads.
- `save_api_key_to_keyring(value)` returns `False` (not raises) when the keyring backend is unavailable.

### triage.py — Haiku verification triage

Optional pre-pass that runs after the keyword classifier and cache lookup but before web verification. Classifies eligible findings as `web_required` or `local_skip` so internally-verifiable findings (internal contradictions where both sides are quoted, equipment-tag mismatches, formatting issues) skip the expensive Sonnet+web_search call.

Safety contract enforced in `is_eligible_for_haiku_triage`:
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
- `_phase_tagged_log(log, phase)` / `_phase_tagged_progress(progress, phase)` — let the verifier path tag its callbacks so the GUI doesn't keyword-sniff message text.
- `group_findings(findings)` / `expand_to_occurrences(findings)`.
- `_parallel_cross_check_enabled()` — default on; cross-check runs concurrently with verification poll. `classify_cross_check_dependencies` partitions cross-check findings into `(kept, suppressed)` using the model-emitted `upstream_finding_ids` / `independent_evidence_ids`; findings without cited ids fall back to a `(filename, section)` heuristic. Dropped findings are stashed on `cross_check_result.suppressed_findings` with `suppression_reason` set. `_drop_cross_check_findings_with_disputed_upstream` is preserved as a thin wrapper returning only the kept list.
- `compute_finding_id(finding)` returns a stable `rf-<12hex>` id derived from `_dedup_key`. `_deduplicate_findings` stamps it on every review finding (singleton and merged-group paths alike).
- `_recover_retryable_review_batch_results(...)` — small repair batch for parse_error / incomplete review specs.

### preprocessor.py — Local preflight

`preprocess_spec(content, filename, *, cycle=None)` returns LEED alerts, placeholder alerts, code-cycle alerts, structural alerts, template-marker alerts, invalid-code-cycle alerts, and duplicate-paragraph alerts.

Deterministic detectors:
- `detect_stale_code_cycle_references` — flags references to superseded California cycles. `_should_suppress_stale_cycle(content, match_start, match_end)` scans up to `_STALE_CYCLE_SUPPRESS_WINDOW` (80) chars on each side for whole-word negation/historical terms (`previously`, `formerly`, `superseded`, `withdrawn`, `obsolete`, `no longer`, `prior`, `historical`, plus auxiliary-verb negations like `shall not` / `does not` / `cannot`). The window narrows at the nearest sentence terminator. Bare `not` is intentionally not a suppressor. Active stale requirements ("Comply with 2019 CBC") still flag. Same suppression runs against the ASCE-7 trail.
- `detect_empty_sections`, `detect_duplicate_headings`, `detect_inconsistent_file_naming`.
- `detect_unresolved_template_markers(content, filename)` — flags `TODO:` / `FIXME` / `XXX` / `???` / lorem-ipsum. Regexes are conservative so prose like "things to do" or model numbers like "XXX-12" do not trigger.
- `detect_invalid_code_cycle_strings(content, filename)` — flags California year/code citations whose year is not a real published cycle (e.g. `2018 CBC`). Disjoint from stale-cycle: stale = real historical cycles that aren't current; invalid = fabricated years.
- `detect_duplicate_paragraphs(content, filename, *, min_length=80)` — flags substantial paragraphs that appear verbatim more than once (whitespace-collapsed casefolded compare).

Every alert dict is stamped with a stable `deterministic_rule` id (`leed_reference`, `placeholder`, `stale_code_cycle`, `stale_asce7`, `empty_section`, `duplicate_heading`, `template_marker`, `invalid_code_cycle`, `duplicate_paragraph`, `inconsistent_filename`) — exposed as `DETERMINISTIC_RULE_*` constants and the `DETERMINISTIC_RULES` frozenset.

Pipeline plumbing: `_PreparedSpecs`, `BatchSubmission`, `CollectedBatchState`, and `PipelineResult` carry every alert list. `_prepare_specs` builds a `pre_detected_by_filename: dict[str, list[dict]]` per-spec view threaded through `submit_review_batch(..., pre_detected_alerts=...)` and `review_single_spec(..., pre_detected_alerts=...)`. The repair batch path recomputes the map from `preprocess_spec` because alerts are deterministic given the inputs.

Verification routing: `verification_router._LOCAL_SKIP_KEYWORDS` recognizes the deterministic rule names (`todo`, `fixme`, `xxx`, `???`, `lorem ipsum`, `duplicate paragraph`, `empty section`, `invalid code cycle`, `template marker`, `inconsistent csi`, `inconsistent filename`) so a GRIPES finding whose `issue` text mentions one is locally skipped. CRITICAL/HIGH and any non-empty `codeReference` still override into `web_required`.

Report rendering: `report_exporter._write_alerts` renders every alert category under a dedicated heading with a `(deterministic check)` suffix via the shared `_write_alert_section` helper. Section order: LEED, Placeholders, Template Markers, Stale Code Cycle, Invalid Code Cycle, Structural Issues, Duplicate Paragraphs, Inconsistent Filenames.

### extractor.py / extraction_cache.py

- `extract_text(filepath) -> ExtractedSpec` / `extract_text_from_docx(filepath)`.
- `extract_multiple_specs(filepaths)` — bounded `ThreadPoolExecutor` (max 8 workers); deterministic order.
- `extract_multiple_specs_cached(filepaths)` — LRU cache keyed on `(absolute_path, size, mtime_ns, content_fingerprint)`; falls back to parallel extraction for misses. Fingerprint is a SHA-256 of the file's first + last `_FINGERPRINT_SAMPLE_BYTES` (default 64 KiB) plus the size so a same-size in-place rewrite that preserves `mtime_ns` is detected and the cached extraction is invalidated.
- `token_count_cache_key(model, system_prompt, user_message, project_context, cycle_label, mode)` — SHA-256 of inputs; LRU bounded to 256 entries.

### tokenizer.py — Token accounting

- `count_tokens(text)` — local cl100k_base.
- `count_tokens_via_api(model, system, messages, *, client=None)` — exact Anthropic count (`None` on failure).
- `local_estimate_safety_factor(model)` — model-specific multiplier (Opus/Sonnet 1.10×, Haiku 1.15×, unknown/None 1.20×) applied to cl100k counts when the API count is unavailable.
- `safe_local_estimate(local_tokens, *, model)` rounds the padded estimate up.
- `exceeds_per_call_limit_for_model(spec_tokens, overhead_tokens, *, model)` — model-aware version of `exceeds_per_call_limit`. The exact Anthropic count remains authoritative when available.

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
- `VERIFICATION_OUTPUT_CAP = 16_000`
- `SYNTHESIS_OUTPUT_CAP = 32_000`
- `HAIKU_TRIAGE_OUTPUT_CAP = 8_000`

### review_request_builder.py — Centralized review request shape

Single source of truth for review API request construction. Real-time review (`reviewer._stream_review`), batch review (`batch.submit_review_batch`), and the token preflight in `pipeline._prepare_specs` all build their request kwargs through this module so the shape they count is the shape they send.

- `ReviewRequestSpec` (frozen) — input record describing one request fully.
- `build_review_request(spec) -> BuiltReviewRequest` — returns the kwargs dict + the raw prompt / user message / tools / phase.
- `build_realtime_review_kwargs(*, system_prompt, user_message, model)` — raw-prompt entry for the streaming path.
- `build_token_count_request(spec)` — returns `(built, count_kwargs)` for `count_tokens_via_api`. Cache-control wrappers are stripped because they are pricing hints, not part of the input token count.
- `review_request_cache_key(spec)` — SHA-256 of the inputs that materially affect the count (system prompt, user message, project context, cycle label, mode, tool schema, batch flag).
- `estimate_local_request_tokens(spec)` — local cl100k_base count of system + user message, used by preflight to rank specs.

### edit_locator.py — Locator

- `locate_edits(findings, paragraph_map)` — returns one `LocatorResult` per finding.
- `LocatorResult.safety_category` — `AUTO_SAFE` / `AUTO_WITH_CAUTION` / `MANUAL_REVIEW` / `REPORT_ONLY`.
- `_id_anchored_match(finding, existing_text, paragraph_map)` — fast path when `Finding.evidenceElementId` is set. Looks up the mapping by `element_id` and revalidates the recorded quote (exact substring first, then normalized). Success → `LocatorResult` with `match_method="id"` and AUTO_SAFE safety for body paragraphs (table cells stay AUTO_WITH_CAUTION so the table-cell precondition revalidation in `spec_editor` still gates the mutation). When the id is set but unusable (id missing from the map, or quote no longer matches the cited element) → `status="not_found"` with `SAFETY_MANUAL_REVIEW` — does NOT fall back to whole-document text matching. The fuzzy/text path is reached only when `evidenceElementId is None`.
- `_fuzzy_match` — length-ratio + `quick_ratio` prefilters before paying for `SequenceMatcher.ratio()`.
- `_section_anchored_match` — narrows by section header neighborhood. Tracks the underlying matcher type; fuzzy-derived results tag `match_method="section_anchored_fuzzy"` and route to `SAFETY_MANUAL_REVIEW`. Exact/normalized section-anchored matches stay `AUTO_WITH_CAUTION`.

### edit_candidates.py — Safety categories

Constants `SAFETY_AUTO_SAFE`, `SAFETY_AUTO_WITH_CAUTION`, `SAFETY_MANUAL_REVIEW`, `SAFETY_REPORT_ONLY`. `EditCandidate.safety_category` defaults to REPORT_ONLY.

### spec_editor.py — DOCX edits + annotation

- `apply_edits_to_spec(source_path, output_path, edit_actions)` — surgical edits in safe order (in-place replacements → ADDs (descending body_index) → whole-paragraph DELETEs (descending)); revalidates preconditions immediately before mutation.
- `annotate_spec_with_suggestions(source_path, output_path, edit_actions)` — writes a copy with a yellow-highlighted suggestion paragraph after each anchor; the original text is never changed.
- `build_edit_actions(locator_results, *, allow_caution=True)` — gates auto-application by `safety_category`.
- `_detect_and_resolve_conflicts` — groups edits by `(body_index, element_type, row_index)` and processes each group in descending start-offset order so a downstream edit is applied before any upstream edit can shift its offsets. `_resolve_overlap_winner(a, b)` returns `EditAction | None`: strict containment keeps the broader edit, identical spans collapse via severity/confidence tie-breakers, partial overlap returns `None` so the caller skips both edits with a "manual review" detail. `ambiguous_ranges` tracking ensures a third edit overlapping a discarded pair's union span is routed to manual review.
- `detect_unsafe_markup(element)` walks the paragraph / cell subtree for risky WordprocessingML constructs (hyperlinks, field characters, drawings, comments, tracked changes, bookmarks, `w:sdt` controls, footnotes, smart tags, custom XML) and returns an `UnsafeMarkupResult`. All four mutation sites route through `_refuse_unsafe_outcome` before mutating; a hit produces `EditOutcome.status="skipped"` with `refused_unsafe_markup=True`.
- Transactional output (default on): the mutated document is saved to an in-memory buffer, validated by reopening as a `Document`, then the disk write is suppressed entirely when any individual outcome ended in `failed`. Previously-applied outcomes demote to `skipped` with an "Output suppressed under all-or-none policy" detail; `EditReport.aborted_transactional` is set. Skipped outcomes (including unsafe-markup refusals) do NOT abort the transactional write. Operator switches: `SPEC_CRITIC_TABLE_CELL_AUTO_EDIT=0` refuses every table-cell auto-edit; `SPEC_CRITIC_EDIT_TRANSACTIONAL=0` reverts to legacy best-effort writes.

### apply_edits.py — Orchestration

`execute_edit_plan(selected_finding_indices, all_findings, cross_check_findings, extracted_specs, source_paths, output_dir, *, log, mode="edit"|"annotate", diagnostics=None)`. Fans out to every entry in `Finding.affected_files`. Looks up the per-file `occurrence_originals` entry for each affected file: present → auto-edit using the original's edit fields; absent on a non-representative file → routed to manual review with an explicit `EditReport` warning rather than guessing with the representative's text.

### report_exporter.py — Word export

`export_report(result, output_path, *, project_context, cross_check_enabled, cycle_label, estimated_cost=None)`. Every finding renders a `Status:` line (one of seven `ReportStatus` values) plus an `Edit:` action label. The "Trust Model Summary" section between the severity table and the alerts shows the per-status histogram and the edit-action breakdown. Finding bodies use explicit `Spec evidence:` / `Proposed replacement:` / `Verification rationale:` labels; the Sources sub-heading distinguishes `Web/code evidence` (accepted citations) from `Unsupported / rejected sources` (model-cited URLs the search tool never returned).

### report_status.py — Trust-model statuses

Closed `ReportStatus` set: `VERIFIED_SUPPORTED`, `VERIFIED_CONTRADICTED`, `DISPUTED`, `INSUFFICIENT_EVIDENCE`, `LOCALLY_CLASSIFIED`, `NOT_CHECKED`, `MANUAL_REVIEW_REQUIRED`. Closed `EditActionLabel` set: `AUTO_EDIT_CANDIDATE`, `MANUAL_EDIT_CANDIDATE`, `REPORT_ONLY`, `SUPPRESSED`. Both are derived from existing Finding fields (`verification`, `suppression_reason`, `edit_proposal`) — nothing on `Finding` changes and the verification cache doesn't need a new column.

`classify_status(finding)` applies rules in priority order: suppression beats no-verification beats local-skip beats verdict-based mapping. `classify_edit_action(finding)` short-circuits on `suppression_reason`, returns `REPORT_ONLY` for findings without an edit proposal, then splits remaining proposals into `AUTO_EDIT_CANDIDATE` (supportive status — `VERIFIED_SUPPORTED` / `VERIFIED_CONTRADICTED` / `LOCALLY_CLASSIFIED` — *and* `edit_confidence >= AUTO_EDIT_CONFIDENCE_FLOOR` (0.7)) vs `MANUAL_EDIT_CANDIDATE` (anything else with a proposal). `LOCALLY_CLASSIFIED` qualifies as supportive because the router decided the finding is self-evident from the spec itself; the locator/spec_editor preconditions still gate the actual mutation.

Public helpers: `status_label(status)`, `status_glyph(status)`, `edit_action_label(action)`, `summarize_statuses(findings)`, `summarize_edit_actions(findings)`, `STATUS_DISPLAY_ORDER`, `EDIT_ACTION_DISPLAY_ORDER`.

### diagnostics.py — Diagnostics report

`DiagnosticsReport.summary()` returns a dict with totals plus `failed_specs`, `skipped_specs`, `edit_skip_reasons` (includes an `unsafe_markup` bucket), `ambiguous_locator_count`, `edits_applied_total/skipped_total/failed_total`, `verification_evidence` (grounded / ungrounded / escalated / cache_hits / local_skips / search_errors / search_requests), `output_telemetry` (max_observed / p50 / p95 / truncated_calls / max_cap_observed), `search_budget` (ceiling / saturated_calls / p50 / p95), `verification_modes`, `verification_profiles`, `escalation_stats` (`attempts` / `changed_verdict` / `no_change` / `change_rate` / `by_reason` / `by_severity` / `by_initial_verdict` / `by_final_verdict`), `locator_methods`, `phase_telemetry` (per-phase rollup with `calls` / `input_tokens` / `output_tokens` / cache fields / `cache_hit_ratio` / `retries` / `continuations` / `truncated_calls` / `realtime_calls` / `batch_calls` / `models`), `cost_summary`, `estimated_cost`. The `DiagnosticsWindow` widget renders all of these inline; `to_text()` and `to_dict()` produce the export formats.

- `record_api_call(*, phase, model, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, web_search_requests, max_output_tokens, stop_reason, mode, retry_status, structured_payload=None, extra=...)` — standardized API-call recorder with normalized event payload.
- `record_locator_method(method)` — increments per-method counters (`id` / `exact` / `normalized` / `section_anchored` / `fuzzy`).
- `bound_structured_payload(payload, *, max_bytes=4096)` — JSON-serializes the dict and caps the byte size; the recorded form is `{"serialized": str, "bytes": int, "truncated": bool}`.

Bounded payloads: `max_event_data_bytes` (default 16 KiB per event), `max_total_data_bytes` (default 8 MiB across all events). `_bound_event_data` scrubs secrets, truncates string fields at `_MAX_STRING_FIELD_BYTES` (4 KiB) with a visible `...(truncated)` marker, and evicts the largest string-shaped fields when the event size still exceeds the cap (numeric telemetry is never replaced). Cumulative byte tracking drops oldest events when the global cap is breached. Counters surfaced in `summary()`: `events_truncated_by_size`, `secrets_redacted`, `bytes_dropped`, `total_data_bytes`.

Secret scrubbing: `_SECRET_KEY_PATTERN` matches secret-shaped key names (`api_key`, `password`, `bearer`, `client_secret`, etc.); `_SECRET_VALUE_PATTERNS` matches Anthropic API keys (`sk-ant-...`), AWS access keys (`AKIA...`), and `Bearer ...` tokens. Hits are replaced with `<redacted>` before byte-cap eviction.

Cost estimator (`src/cost_estimator.py`): pricing table for Opus 4.x / Sonnet 4.6 / Haiku 4.5 (rates as of `PRICING_AS_OF`). Returns `{available, total_usd, currency, pricing_as_of, by_phase, by_model, missing_pricing_models, missing_pricing_calls, priced_calls, web_search_requests, notes}`. Applies the batch-API 50% discount on input/output (cache writes/reads / web-search unaffected) and the documented Anthropic cache multipliers (1h cache write = 2× base input, cache read = 0.1× base input); server-side web_search adds $10 per 1,000 requests. Unknown models return `None` and surface as `missing_pricing_calls`. `format_usd(value)` renders amounts with conservative precision. Surfaced in the Word report between the severity table and the trust-model summary, in `to_text()`, in `DiagnosticsWindow._render_estimated_cost_section`, and in `review_run_controller.on_review_complete`.

---

## 4) GUI Notes (gui.py / widgets.py)

- Review-mode segmented control (Strict / Comprehensive / Safe edit).
- Mode labels: `Real-time (FAST: Expensive!)` and `Batch (SLOW: Cheap!)`.
- Real-time cost confirmation dialog with batch-switch option.
- Token gauge labels approximate vs. exact (API) counts; runs the API count async after the live cl100k_base estimate.
- `_make_diag_log` / `_make_diag_progress` honor the explicit `phase=` kwarg from pipeline callers (no message keyword sniffing).
- Resume state uses `resume_state.py` serializers/deserializers; legacy v1 migration path retained.
- File browser filter restricted to `.docx`.

---

## 5) Prompting and Code-Cycle Behavior

- Prompts are mode-aware (Strict / Comprehensive / Safe-edit) and target the California 2025 code cycle.
- `get_system_prompt(cycle, mode=...)` injects the mode banner, severity rubric, four-example reference block, editability clause, and review-scope categories.
- `get_single_spec_user_message(...)` emits the per-spec task text with project context, mode reminder, optional id-tagged spec rendering, optional `<pre_detected>` block, and a trailing `<final_task>` block.
- The system prompt instructs the model to call the structured tool (`submit_review_findings`); a tagged-JSON fallback exists for compatibility.
- `DEFAULT_CYCLE = CALIFORNIA_2025`. Cycle labels are part of the verification cache key, so switching cycles naturally invalidates prior entries.

---

## 6) Verification Routing and Web Search

### Profiles

Every verification call classifies the finding into one of five `VerificationProfile` values before the request is built:

| Profile | When | `max_uses` ceiling (CRITICAL → HIGH → MEDIUM → GRIPES) |
|---|---|---|
| `california_ahj` | finding mentions California / DSA / HCAI / Title 24 / AHJ | 8 / 7 / 5 / 3 |
| `code_standard` | finding cites a code section or a standards body (CBC, NFPA, ASHRAE, IAPMO, …) without California signals | 7 / 7 / 5 / 3 |
| `manufacturer` | finding mentions a manufacturer / model number / datasheet / submittal | 6 / 5 / 4 / 3 |
| `constructability` | default for substantive technical claims with no clear kind signal | 5 / 5 / 4 / 3 |
| `internal_coordination` | finding mentions internal contradiction / placeholder / LEED / typo / duplicate paragraph | 2 / 2 / 1 / 1 |

Profile sets the ceiling; severity modulates within it. `classify_finding_profile(finding)` lives in `src/verification_profiles.py`. `build_verification_tools_for_profile(profile, severity)` in `batch.py` is the profile-aware variant of `build_verification_tools(severity)`; both real-time, batch initial, and batch retry / continuation builders route through it and stamp the profile string into `VerificationResult.verification_profile`.

### Modes

`select_verification_mode(finding, *, local_skip, escalated, cached_mode)` in `src/verification_modes.py` picks one of four `VerificationMode` values:

| Mode | When | Model | Thinking | Search budget | Escalates? |
|---|---|---|---|---|---|
| `local_skip` | keyword classifier or Haiku triage said `local_skip` | (none — no API call) | n/a | 0 (no search) | no |
| `strict_structured` | GRIPES severity OR non-GRIPES `internal_coordination` profile | Sonnet | off | profile ceiling × 0.5, floor 1 | no |
| `standard_reasoning` | default for substantive technical claims | Sonnet (defers to `VERIFICATION_MODEL_DEFAULT`) | on | full profile ceiling | yes (via `should_escalate_verification`) |
| `deep_reasoning` | `escalated=True`, OR initial pass for CRITICAL `california_ahj` (when Sonnet-default is on) | Opus (defers to `VERIFICATION_ESCALATION_MODEL`) | on | full profile ceiling | no (terminal) |

Rules in priority order: cache-hit replay → local_skip → escalated → CRITICAL `california_ahj` initial pass → GRIPES → non-GRIPES `internal_coordination` → default. `mode_policy(mode)` returns the frozen `ModePolicy` bundle; `mode_search_budget(mode, *, profile_ceiling)` composes the multiplier with `profile_max_uses(...)` (floor of 1). Diagnostics' `summary()` exposes `verification_modes` and `verification_profiles` count dicts, rendered as `Modes:` / `Profiles:` lines in `to_text()`.

### Source grounding

Once a verdict is parsed, `_apply_source_grounding` (verifier.py) partitions sources into four concepts:

- `searched_sources` — URLs the web_search server tool actually retrieved.
- `cited_sources` — URLs the model emitted in its `submit_verification_verdict` payload.
- `accepted_sources` — cited URLs whose normalized form matched a searched URL.
- `rejected_sources` — `[{"url", "reason"}]` for cited URLs that did not match any searched URL.

Normalization (`source_grounding.normalize_url`) folds `http`/`https`, drops default ports / fragments / tracking params, sorts query params, and trims trailing slashes / cosmetic punctuation so trivial differences never cause a real citation to be rejected. The public `VerificationResult.sources` list is replaced with `accepted_sources` so reports and the verification cache never persist model-invented URLs. If the model emitted citations but every citation was ungrounded, `CONFIRMED` / `CORRECTED` is downgraded to `UNVERIFIED` with an explanation suffix.

`_enforce_grounding_invariant` additionally downgrades an externally-verified `CONFIRMED` / `CORRECTED` when `accepted_sources` (and the legacy `sources` fallback) are both empty, even if the search produced grounded blocks. The cache mirrors the invariant: `_CACHE_SCHEMA_VERSION` is bumped so v1 entries that may carry source-less CONFIRMED are dropped silently on first load; `VerificationCache.put` refuses a CONFIRMED/CORRECTED without an accepted citation; `load_from_disk` re-validates each entry before reinstating it. Local-skip findings are exempt by construction. `report_status.classify_status` has a belt-and-suspenders accepted-citation check on the `VERIFIED_SUPPORTED` / `VERIFIED_CONTRADICTED` branches.

### Source-quality blocklist

A blocked-domain list filters social/AI-assistant/forum/general-encyclopedia sources from `web_search_20260209`. California priority sources are documented in the verifier system prompt rather than encoded as an allow-list (mixing allow + block lists is unsupported by the tool).

---

## 7) Feature Flags

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_PROMPT_CACHE` | `1` | `0` disables prompt caching globally |
| `SPEC_CRITIC_PROMPT_CACHE_TTL` | `1h` | `5m` switches to ephemeral 5-minute cache (lower write cost, narrower payback window) |
| `SPEC_CRITIC_CACHE_DISABLE` | (empty) | Comma-separated phase names to opt out of caching individually (e.g. `verification,cross_check`) |
| `SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT` | `1` | `0` disables the custom-tool path so review / cross-check / verification fall back to tagged-JSON-in-text parsing. With the flag on, requests include the custom tool with `tool_choice={"type": "auto"}`; the model is expected but not required to call it (the API rejects forcing tool_choice when thinking is enabled). |
| `SPEC_CRITIC_STRUCTURED_OUTPUTS` | `1` | Legacy alias for `SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT`. If both are set, the preferred name wins. |
| `SPEC_CRITIC_STRICT_TOOLS` | `0` | `1` attaches `strict: true` to tool definitions for grammar-constrained sampling. Off by default pending real-call verification under thinking. |
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
| `SPEC_CRITIC_ELEMENT_IDS` | `1` | `0` reverts spec rendering to the legacy plain-body `<spec>` wrapper (no id-tagged `<para>`/`<row>`/`<heading>` elements) |
| `SPEC_CRITIC_PRE_DETECTED_ALERTS` | `1` | `0` disables the `<pre_detected>` block that lists deterministic preprocessor alerts inside each spec's user message |
| `SPEC_CRITIC_EFFORT_POLICY` | `1` | `0` disables the `output_config.effort` policy globally so requests omit the field |
| `SPEC_CRITIC_EFFORT_OVERRIDE` | (empty) | When set, forces every effort-capable request to use the given level (`low` / `medium` / `high` / `xhigh`). Invalid values raise at request-build time |
| `SPEC_CRITIC_TABLE_CELL_AUTO_EDIT` | `1` | `0` refuses every table-cell auto-edit regardless of markup |
| `SPEC_CRITIC_EDIT_TRANSACTIONAL` | `1` | `0` reverts to legacy best-effort writes |
| `SPEC_CRITIC_SERVICE_TIER` | `auto` | Service tier for batch request params. `standard_only` pins to standard; empty string omits the field |

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
