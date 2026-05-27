# CLAUDE.md — Spec Critic v3.0.0

Engineering reference for the Spec Critic codebase. Focuses on non-obvious invariants and orientation — read the source for full type signatures.

---

## Working agreements

**PR workflow (standing instruction):** After pushing commits to a feature branch, open a pull request against `master` without waiting to be asked — update the existing open PR if one is already open for the branch. This durably authorizes PR creation and overrides the default "don't open a PR unless explicitly asked" behavior. Still confirm before merging, force-pushing, or other destructive / irreversible actions.

---

## 1) What it is

Python desktop app (CustomTkinter) for reviewing California K-12 DSA mechanical/plumbing `.docx` specs. Extracts text, runs deterministic local pre-screens, sends per-spec reviews through Claude's Message Batches API, optionally runs cross-spec coordination, verifies findings against web search, and exports a Word report plus a machine-readable JSON sidecar of suggested edits.

**The app emits edit instructions but does not apply them.** Each finding may carry a structured edit proposal (action / existing text / replacement text); these are rendered in the report and written to a `<report-stem>.edits.json` sidecar for a separate, future applier program to ingest. The surgical-edit / write-back machinery was removed in v3.0.0.

The per-spec review runs through the Message Batches API on Claude Opus 4.7. The 300k extended-output path lifts the batch review output cap for inputs ≥200k tokens (`output-300k-2026-03-24` beta header, batch-only by API design); smaller inputs use the shared baseline cap. Verification also runs as a batch, with a synchronous fallback for small unresolved tails (see "Real-time fallback") and a synchronous cross-spec coordination pass.

## Source layout

```
src/
├── __init__.py             # Package version (3.0.0)

# Core config
├── core/
│   ├── api_config.py           # Models / output caps / feature-flag config
│   ├── api_key_store.py        # API key loading and persistence
│   ├── app_paths.py            # Platform config/state directories
│   ├── code_cycles.py          # California code cycle definitions
│   └── tokenizer.py            # Local + Anthropic token counting

# UI
├── gui/
│   ├── gui.py                  # CustomTkinter app shell
│   ├── widgets.py              # Reusable UI components
│   ├── about_usage_dialogs.py  # About / API-usage dialogs
│   └── *_controller.py         # 7 thin bridges between widgets and pipeline
│                               # (batch, context, diagnostics, file_selection,
│                               #  report, review_run, token_analysis)

# Orchestration / state
├── orchestration/
│   ├── pipeline.py             # Core orchestration + FindingGroup/FindingOccurrence
│   ├── resume_state.py         # Durable resume state (with file-hash validation)
│   └── diagnostics.py          # In-memory diagnostics report

# Review
├── review/
│   ├── reviewer.py             # Anthropic API client (streaming + tool-use parsing)
│   ├── review_request_builder.py # Central review request shape builder
│   ├── structured_schemas.py   # Tool-use schemas for review/cross-check/verification
│   ├── prompts.py              # System + user prompt builders
│   └── prompt_serialization.py # Escape/wrap helpers for prompt boundaries

# Cross-spec coordination
├── cross_check/
│   └── cross_checker.py        # Cross-spec coordination (chunked by CSI division)

# Verification
├── verification/
│   ├── verifier.py             # Real-time + batch verification orchestrator
│   ├── verification_router.py  # Local pre-classification (local_skip / web_required)
│   ├── verification_cache.py   # Persistent claim-keyed verdict cache (JSON on disk)
│   ├── verification_profiles.py # Profile classifier + severity-based search budget
│   ├── verification_modes.py   # Verification modes + per-mode policy
│   ├── verification_routing.py # Unified routing decision + request builder
│   ├── source_grounding.py     # URL normalization + cited-source validation
│   ├── retry_policy.py         # Retry, continuation, and batch-failure taxonomy
│   └── triage.py               # Haiku-based verification triage (opt-in)

# Batch
├── batch/
│   ├── batch.py                # Anthropic Message Batches API wrapper
│   ├── batch_runtime.py        # Bounded polling with progressive backoff
│   └── batch_state_store.py    # Atomic JSON state store for batch resume

# Spec input
├── input/
│   ├── extractor.py            # DOCX text extraction (parallelized)
│   ├── extraction_cache.py     # LRU caches for extraction + API token counts
│   └── preprocessor.py         # Deterministic local detectors

# Tracing
├── tracing/
│   ├── config.py               # Env-var parsing + capture-level enum
│   ├── session.py              # TraceSession: per-run directory + run.json writer
│   ├── recorder.py             # TraceRecorder: global singleton, start/stop
│   ├── spans.py                # SpanHandle + span-kind constants
│   ├── capture_hooks.py        # Defensive integration hooks (never escape to pipeline)
│   ├── redaction.py            # API key / bearer-token redaction before serialization
│   ├── cli.py                  # list / show / prune subcommands
│   ├── __main__.py             # python -m src.tracing entry point
│   └── viewer/
│       └── trace_viewer.html   # Single-file zero-build HTML replay viewer

# Output
└── output/
    ├── report_exporter.py      # Word (.docx) report generation
    ├── edit_sidecar.py         # Machine-readable JSON sidecar of suggested edits
    └── report_status.py        # ReportStatus / EditActionLabel + classifiers
```

## High-level flow

```
.docx files
  → extraction_cache.extract_multiple_specs_cached  (parallel; LRU keyed by mtime + content fingerprint)
  → preprocessor.preprocess_spec                    (LEED/placeholder/stale-cycle/structural alerts)
  → tokenizer.count_tokens + count_tokens_via_api   (preflight)
  → batch.submit_review_batch
  → pipeline._deduplicate_findings                  (full-text SHA-256 keys)
  → cross_checker.run_chunked_cross_check           (parallel with verification by default)
  → verifier.verify_findings / verify_findings_batch
  → pipeline.finalize_batch_result
  → report_exporter.export_report
  → edit_sidecar.write_edit_instructions_sidecar  (JSON feed for a future applier)
```

---

## 2) Non-obvious Invariants

These are the contracts the agent should preserve when editing the code. Field-level / signature-level details live in the source.

### Grounding invariant
`CONFIRMED` / `CORRECTED` verdicts require **at least one accepted external citation** — a model-cited URL whose normalized form matched a URL the `web_search` tool actually retrieved. Enforced in three places:
- `verifier._apply_source_grounding` — partitions sources into searched / cited / accepted / rejected and downgrades when every cited URL is ungrounded.
- `verifier._enforce_grounding_invariant` — defensive downgrade for verified-but-source-less verdicts.
- `verification_cache.VerificationCache.put` — refuses to cache a CONFIRMED/CORRECTED without an accepted citation; `_CACHE_SCHEMA_VERSION` drops v1 entries that might violate this.

`VerificationResult.sources` is the *accepted* list, not the cited list — reports and cache never persist model-invented URLs.

### Cross-check dependency suppression
`pipeline.classify_cross_check_dependencies` drops a cross-check finding only when **every** cited `upstreamFindingIds` is `DISPUTED` *and* `independentEvidenceIds` is empty. Otherwise the finding survives. Findings without cited ids fall back to a `(filename, section)` heuristic — labeled as such in logs. Dropped findings land on `suppressed_findings` with `suppression_reason` set so the report can explain the decision.

### FindingGroup vs FindingOccurrence
`Finding.occurrence_originals` holds per-file pre-merge member findings when `_deduplicate_findings` collapses across files, so per-file `existingText` / `replacementText` differences survive the merge for the report and the edit-instruction sidecar. Singletons leave it empty (the finding is its own original).

### REPORT_ONLY action
The structured tool schema includes `REPORT_ONLY` so coordination/interpretation findings don't have to fabricate `existingText` / `replacementText`. `validate_edit_shape` demotes EDIT/DELETE/ADD findings that lack action-specific required fields to REPORT_ONLY with `demotion_reason` stamped.

### Edit instructions are emitted, not applied
`Finding.as_edit_proposal()` (in `reviewer.py`) is the single accessor for a finding's structured edit proposal (`action_type` / `existing_text` / `replacement_text` / `anchor_text` / `insert_position` / `target_element_id` / `edit_confidence`), reconstructing one from legacy fields when needed and returning `None` for REPORT_ONLY / invalid shapes. Proposals are rendered in the report and serialized to the `<report-stem>.edits.json` sidecar by `edit_sidecar.write_edit_instructions_sidecar` (reusing `resume_state.serialize_edit_proposal`). Nothing in this codebase locates or applies edits — that is a future, separate program's job.

### Prompt-cache breakpoint stability
The instruction prefix in front of `<spec ` must stay byte-identical across calls so cache breakpoints land in the same place. The `<final_task>` block sits *after* the spec body (and after `<pre_detected>` when alerts fire) for this reason. `prompt_serialization.py` is the single source of truth for escaping wrapper attributes/bodies.

### Token preflight raises (not warns)
`pipeline._prepare_specs` raises `ValueError` when the exact Anthropic count exceeds `RECOMMENDED_MAX`. Earlier behavior was log-only with cl100k as the only hard gate.

### Model capability whitelist
`api_config.model_capabilities(model)` is the single source of truth for adaptive-thinking / extended-output / 1M-context eligibility. Whitelist covers Opus 4.7, Sonnet 4.6, Haiku 4.5. **Unknown model ids degrade to safe defaults that disable every capability flag** — a misconfigured env var produces a smaller request, never an API rejection. Haiku phases (triage) never carry the `thinking` key.

### Verification cache key
`cycle_label | actionType | codeReference | sha256(claim_summary)`. Intentionally omits the verifier model — `VerificationResult.model_used` is stored as provenance inside the entry. Switching `SPEC_CRITIC_VERIFICATION_MODEL` does NOT invalidate existing entries; switching the code cycle does. Claim digest is 24 hex chars; older 16-char entries miss → re-ground → write new 24-char entries (`_CACHE_SCHEMA_VERSION` bump drops the legacy shape).

### Cache-replay visibility (Chunk 5 / Trust Upgrade)
`_clone_for_hit` stamps the sidecar `_CacheEntry.created_ts` onto `VerificationResult.cache_entry_created_ts` so the report can render an inline "Cache replay — Nd old" badge (amber <30d / orange 30-90d / red >90d) without re-reading the cache file. Per-finding evidence panel surfaces the configured cache path so a reviewer can locate and delete a single entry to force re-verification. Default TTL is now 60 days (down from no-expiry); set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS=0` to restore the legacy database behavior.

### Run Diagnostics banner (Chunk 6 / Trust Upgrade)
`report_exporter._write_run_diagnostics_banner` renders a styled table right after the title block surfacing operational health: edit-suggested / report-only / suppressed counts (from the edit-action histogram), cache replays + oldest entry age (Chunk 5's `cache_entry_created_ts`), verification failures (Chunk 3's `VERIFICATION_FAILED` status — highlighted red when > 0), parse-time REPORT_ONLY demotions (`Finding.demotion_reason`), spec content extraction warnings (slot reserved for Chunk 10), and cross-spec coordination status (skipped/failed highlighted red). When verification failures > 0, a recovery-hint paragraph below the banner explains the ⚠ glyph and notes that the cache does not persist operational-failure results, so a re-run sees them fresh. All values are derived from existing `Finding` / `VerificationResult` fields — no new persistence; `_summarize_run_diagnostics` is the pure helper used by the renderer (and unit tests).

### Pinned standards editions (Chunk 7 / Trust Upgrade)
`CodeCycle` carries adopted-edition fields for NFPA 13 / 14 / 20 / 24 / 25 / 72, ASHRAE 62.1 / 90.1 / 15, IAPMO Uniform Plumbing TSC, and UL listings (UL 300, UL 555, UL 555S, UL 268, UL 1479). `CALIFORNIA_2025` is populated from the California Building Standards Commission adoption matrix — verify against the published matrix before changing edition strings. UL editions are a `tuple[tuple[str, str], ...]` (not a dict) so the dataclass stays hashable under `frozen=True`. The reviewer system prompt's "Code edition misalignment" category lists NFPA 13 / 72 and ASHRAE 62.1 / 90.1 explicitly. The verifier system prompt renders a "Pinned standards editions" block right after the cycle context (built by `verifier._pinned_standards_lines`) listing every populated edition and instructing the model to flag drift. The methodology note in the exported report (`report_exporter._render_pinned_editions_note`) enumerates the pinned editions for the cycle. Empty edition fields are silently dropped from all three surfaces so future cycles that don't populate the new fields degrade gracefully.

### Deterministic-rule ids are public
Every preprocessor alert carries a stable `deterministic_rule` id (exposed as `DETERMINISTIC_RULE_*` constants). The verification router's local-skip keyword list recognizes the rule names, so a GRIPES finding mentioning `todo` / `lorem ipsum` / `duplicate paragraph` / etc. is locally skipped. CRITICAL/HIGH and any non-empty `codeReference` still force `web_required`.

### Stale-cycle suppression window
`preprocessor._should_suppress_stale_cycle` scans up to 80 chars on each side for whole-word negation/historical terms (`previously`, `formerly`, `superseded`, `withdrawn`, `obsolete`, `no longer`, `prior`, `historical`, plus auxiliary-verb negations). The window narrows at the nearest sentence terminator. Bare `not` is intentionally not a suppressor. Active stale requirements ("Comply with 2019 CBC") still flag.

### Edit-action labels
`report_status.classify_edit_action` is the single source of truth and is intentionally simple now that the app emits — but never applies — edits:
- `suppression_reason` set → `SUPPRESSED`,
- no edit proposal → `REPORT_ONLY`,
- otherwise → `EDIT_SUGGESTED`.

There is no confidence gate, no supportive-status filter, and no numeric/standards demotion — those existed only to decide *auto-apply*, which this app no longer does. A finding's verification status (`VERIFIED_SUPPORTED` / `VERIFICATION_FAILED` / `VERIFIED_CONTESTED` / etc.) and `edit_confidence` ride along in the report and the JSON sidecar so a downstream applier can do its own gating. `summarize_edit_actions` feeds the Run Diagnostics banner's edit-suggested / report-only / suppressed counts.

### LOCALLY_CLASSIFIED keyword tightening (Chunk 10 / Trust Upgrade)
`verification_router._LOCAL_SKIP_KEYWORDS` no longer contains `"formatting"` — too broad, a real CMC formatting requirement ("label valves per ASME A13.1 color formatting") could match and bypass verification. `"leed"` and `"internal contradiction"` were moved to `_LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED`; they still route to `local_skip` (web search adds no signal for either) but `local_skip_requires_elevated_confidence(finding)` returns `True` for them. `_local_skip_result()` accepts a `requires_elevated_confidence=` kwarg and stamps it onto `VerificationResult.requires_elevated_confidence`. The flag round-trips through `resume_state` (and is carried into the sidecar) but never reaches the verification cache (local-skip results aren't grounded, so the cache's `grounded` guard drops them; no schema bump needed). It is retained as telemetry for a downstream applier — nothing in this app consumes it for routing anymore. A finding matching BOTH a regular keyword and an elevated keyword takes the regular path with no flag set — the regular-list match (placeholder, TODO, duplicate paragraph) is the stronger signal because it maps directly to a deterministic detector. Haiku-triaged local skips never get the flag.

### DOCX content-loss warning (Chunk 10 / Trust Upgrade)
`extractor._detect_content_loss_warning(body)` counts direct children of `<w:body>` (paragraphs and tables, skipping `<w:sectPr>` which is metadata) that contain at least one descendant `<w:drawing>` / `<w:pict>` / `<w:object>` element. When that proportion exceeds `_CONTENT_LOSS_WARNING_THRESHOLD` (0.20, strict `>`), the helper returns a warning string of the form `"Spec contains {N}% non-text elements ({drawings} drawings, {pictures} pictures, {objects} OLE objects). Some content may not have been extracted for review. Verify visually."` The threshold is strict (>) so a borderline 20% spec doesn't generate noise on every run. The warning is appended to `ExtractedSpec.extraction_warnings` (new list field on the dataclass). `PipelineResult.extracted_specs` carries the list of extracted specs through `finalize_batch_result` so `report_exporter._summarize_run_diagnostics` can read each spec's `extraction_warnings` and count the number of affected specs. The Run Diagnostics banner's "Spec content extraction warnings" row (slot reserved in Chunk 6) now shows the real count and the value cell is shaded red (`FFE5E5`) when > 0. The list round-trips through `resume_state.serialize_extracted_spec` / `deserialize_extracted_spec`; legacy state files (no key) load as an empty list so the banner shape stays stable. The banner reports affected-spec count, NOT total warning count — a single spec with three warnings still counts as one affected file, since the "verify visually" prompt is one-per-document anyway.

### Web-fetch for follow-up reads (Chunk 11 / Trust Upgrade)
`api_config.build_web_fetch_tool()` returns the `web_fetch_20260209` server-tool dict (citations enabled, `max_uses=DEFAULT_VERIFICATION_MAX_FETCHES=3`, `max_content_tokens=WEB_FETCH_MAX_CONTENT_TOKENS=50_000`, blocklist mirrored from `web_search` so the two server tools share one source-quality policy). `verification_routing.build_verification_tools_from_decision` appends the tool for `VerificationMode.STANDARD_REASONING` and `VerificationMode.DEEP_REASONING` only; STRICT_STRUCTURED and LOCAL_SKIP intentionally omit it (those modes are explicitly cheap/narrow and don't benefit from a deeper read). The verdict tool stays at the end of the tool list so `tools_with_cache` attaches the trailing cache breakpoint to the right tool.

**Web fetch is generally available and takes NO `anthropic-beta` header.** Chunk 11 originally shipped attaching `extra_headers={"anthropic-beta": "web-fetch-2026-02-09"}` on the assumption that the header was "harmless when the API treats web_fetch as generally available, required when still gated." That was wrong on both counts: web_fetch is GA (the `web_fetch_20260209` tool dict alone enables it, at no extra cost beyond fetched-content tokens), and an *unrecognized* `anthropic-beta` value is rejected with HTTP 400 `invalid_request_error: Unexpected value(s) ... for the anthropic-beta header`, not silently ignored. Every verification request routed to STANDARD_REASONING / DEEP_REASONING (the common path) carried the retired header and crashed the run at batch/stream submit. The fix: `build_verification_request` attaches **no** beta header for web_fetch — `extra_headers` stays the empty SDK transport seam (still split out from `params` because the batch API rejects unknown keys inside per-item `params`). The `web_fetch_20260209` tool itself is current and valid, so it is attached unconditionally for the two fetch-eligible modes. All other Chunk 11 plumbing (telemetry fields, report rendering, grounding-accepts-fetched) is unchanged.

`VerificationResult` gains `web_fetch_requests: int = 0` and `fetched_sources: list[str]` (default factory). `_collect_fetch_evidence_detailed(message)` parses `server_tool_use` blocks with `name="web_fetch"` and the paired `web_fetch_tool_result` blocks; `_web_fetch_count(message)` reads `usage.server_tool_use.web_fetch_requests`. Both real-time (`_run_verification_call`) and batch wave (`_classify_wave_results`) paths sum search successes + fetch successes into the grounded check, so a CONFIRMED verdict that converged purely via fetch still clears the grounding gate. `_apply_source_grounding` accepts an optional `fetched: list[SearchedSource] | None = None`; the pool of accepted-citation URLs is `searched ∪ fetched` so a cited URL the model fetched (but didn't search) still validates. `searched_sources` on the result is NOT augmented with fetched URLs — the report's separate "Full-text sources consulted" sub-section renders them from `fetched_sources` so snippet-grounded vs. fetch-grounded evidence stays visually distinct.

Telemetry round-trips through both the verification cache (`_result_to_dict` / load path / `_clone_for_store` / `_clone_for_hit`) and resume state (`serialize_verification_result` / `deserialize_verification_result`) — runtime telemetry, not verdict semantics, so no cache schema bump is required and legacy v3 rows without the keys load with defaults of 0 / []. The evidence panel renders "Searches: N, Full-page fetches: M" when `web_fetch_requests > 0` (plain "N of M searches used" otherwise — keeps the line short for the common path) and a "Full-text sources consulted (retrieved via web_fetch):" sub-section listing the fetched URLs, omitted when the list is empty so empty sub-sections never appear.

### Escalation disagreement surfacing (Chunk 12 / Trust Upgrade)
`VerificationResult` gains `models_disagreed: bool = False` and `initial_sources: list[str]` (default factory). `verify_finding` snapshots `initial_grounded_snapshot = bool(result.grounded)` and `initial_sources_snapshot = list(result.sources or [])` BEFORE running the escalation call so the snapshots survive the potential `result = esc_result` swap. After the swap, `result.initial_sources = initial_sources_snapshot` is set unconditionally (so the evidence panel can still show "Initial: UNVERIFIED, no sources" for non-contested escalations), and `result.models_disagreed = initial_grounded_snapshot and bool(esc_result.grounded) and esc_result.verdict != initial_verdict_snapshot` — strictly tighter than `escalation_changed_verdict` because an initial-UNVERIFIED-then-CONFIRMED escalation should NOT register as "models disagreed" (the initial pass didn't actually ground anything to disagree about; the escalation path was doing its job).

`ReportStatus.VERIFIED_CONTESTED` (glyph `⚡`, purple `800080`) is registered in `STATUS_LABELS` / `STATUS_GLYPHS` / `STATUS_COLORS` / `STATUS_SHADING` and sits in `STATUS_DISPLAY_ORDER` between `VERIFIED_CONTRADICTED` and `LOCALLY_CLASSIFIED`. `classify_status` checks `models_disagreed` BEFORE the `local_skip` and verdict-based branches so a swapped-in CONFIRMED-grounded final verdict still classifies as `VERIFIED_CONTESTED`. A contested finding's `VERIFIED_CONTESTED` status is carried into the report and the JSON sidecar so a downstream applier sees the disagreement and can withhold the edit — the disagreement itself is the signal that the finding needs human eyes. (`classify_edit_action` still labels it `EDIT_SUGGESTED` if it carries a proposal; the app emits, it does not apply.)

Telemetry round-trips through both the verification cache (`_result_to_dict` / load path / `_clone_for_store` / `_clone_for_hit`) and resume state — runtime telemetry, not verdict semantics, so no cache schema bump is required and legacy v3 rows without the keys load with defaults of False / []. The evidence panel's Escalation history line uses the purple `VERIFIED_CONTESTED` color when `models_disagreed=True` (red-orange when only `escalation_changed_verdict=True`, gray when neither fires) and appends an expanded "manual review recommended" sentence; a dedicated "Initial verifier sources:" sub-section (rendered only when `models_disagreed=True` AND `initial_sources` is non-empty) lists the initial verifier's citations side-by-side with the final verifier's citations in the regular "Web/code evidence" sub-section.

### Budget-exhaustion sentinel (Chunk 13 / Trust Upgrade)
`VerificationResult` gains `budget_exhausted: bool = False`. The verifier sets it on UNVERIFIED results where `web_search_requests >= decision.web_search_max_uses` — "the model spent its full mode-scaled budget without grounding a verdict", distinct from `verification_failed` (operational) and from a plain UNVERIFIED (model ran out of evidence early). Real-time detection is in `_run_verification_call`: a `budget_was_exhausted = budget > 0 and total_search_requests >= budget` boolean is computed once before the not-grounded early returns, threaded into every `_make_unverified(budget_exhausted=...)` call, and stamped onto the success-path result AFTER `_enforce_grounding_invariant` so a downgraded-to-UNVERIFIED CONFIRMED still picks up the flag when its searches were consumed. Two specific over-budget paths (pause_turn-loop exceeded 2x ceiling, max-continuations-without-complete) pass `budget_exhausted=True` directly. The batch wave path applies the same condition in `_classify_wave_results` using the stored routing decision's `web_search_max_uses` (or the re-derived first-wave decision).

The flag round-trips through `orchestration.resume_state.serialize_verification_result` / `deserialize_verification_result` so resumed reports keep the sub-label; missing keys on legacy state files default to `False`. Runtime telemetry — `VerificationCache.put` refuses to persist `budget_exhausted=True` results for the same transient-signal reason it refuses `verification_failed=True` (a re-run at higher severity allocates more budget; freezing the shortfall would suppress re-verification). The `grounded` guard already drops every UNVERIFIED, so the explicit `budget_exhausted` guard is defense-in-depth against a future call site that constructs a grounded+exhausted result directly. Cache schema is unchanged — no new persisted keys.

`report_status.is_budget_exhausted(finding)` is the public helper that reads the flag defensively (returns `False` for missing `verification` / missing attribute). `summarize_budget_exhausted(findings)` aggregates the count. **Crucially, the flag does NOT change classification semantics**: `classify_status` still returns `INSUFFICIENT_EVIDENCE` for exhausted findings — no new top-level `ReportStatus` enum value — because the trust level is the same as any other unground UNVERIFIED. The sub-label is purely a rendering enrichment.

The report exporter (`report_exporter._write_finding_entry`) appends an italic " (search budget exhausted)" sub-label to the status line when `is_budget_exhausted(finding)` is True, colored to match the INSUFFICIENT_EVIDENCE status so the badge reads as part of the status rather than a separate field. The Run Diagnostics banner has a new "Budget-exhausted findings" row (highlighted red when count > 0) and a calmer-amber recovery hint paragraph below the table when count > 0, naming the severity-tiered budget knob (CRITICAL=8, HIGH=7, MEDIUM=5, GRIPES=3 — `api_config._SEVERITY_MAX_USES`, rendered dynamically via `web_search_max_uses_for_severity` so the hint can't drift from policy) as the actionable remedy. The hint paragraph is distinct from the verification-failure red hint above it because the cause and remedy differ: failures are transient (re-run sees them fresh); budget exhaustion is policy-driven (re-run at the same severity exhausts the same budget).

The calibration harness (`evals/calibration/harness.py:_apply_budget_exhaustion`) mirrors the production detection after grounding so fixtures with `captured_verifier_response.web_search_requests` at or above the severity budget surface the flag through the scorer. `FixtureOutcome` gains a `budget_exhausted` field; the scorer's summary header reports `budget_exhausted_count` so a recheck can confirm telemetry flowed through end-to-end. The `tp_unverified_budget_exhausted` fixture is the canonical example (HIGH-severity DSA bulletin lookup that consumed all 7 searches without grounding the cited section).

### Code cycle: California 2025 only
`DEFAULT_CYCLE = CALIFORNIA_2025`. The 2022-cycle mapping was removed — **do not reintroduce it**. Cycle label is in the verification cache key, so a cycle bump naturally invalidates prior entries.

### Per-finding evidence panel
The report exporter renders one collapsed "Sources" Heading 4 per finding with a verification result. Contents (in order, below the heading): verifier model, verification mode, search budget (`N of M searches used`), source quote (verbatim from a web_search snippet — Chunk 2 schema), verifier rationale (moved here from above the heading), escalation history when `escalation_attempted` (with the Chunk 12 expanded sentence + initial-verifier-sources sub-section when `models_disagreed=True`), accepted source URLs, rejected source URLs. The finding's proposed edit (existing text → replacement) renders inline above the panel; the machine-readable form goes to the JSON sidecar. There is no locator / "Edit Target Evidence" panel — locating an edit target is the downstream applier's job.

---

## 3) Verification Routing

### Profiles (`verification_profiles.classify_finding_profile`)

Profile picks the priority-source language attached to the verifier system prompt. The web-search budget is severity-based and identical across profiles. Priority order: internal-coordination → California/AHJ → manufacturer → code-standard (or non-empty `codeReference`) → constructability.

| Profile | When |
|---|---|
| `california_ahj` | mentions California / DSA / HCAI / Title 24 / AHJ |
| `code_standard` | cites a code section or standards body without California signals |
| `manufacturer` | mentions a manufacturer / model number / datasheet / submittal |
| `constructability` | default for substantive technical claims |
| `internal_coordination` | mentions internal contradiction / placeholder / LEED / typo / duplicate paragraph |

### Search budget (`api_config._SEVERITY_MAX_USES`)

Flat severity-based budget, same for every profile:

| Severity | `max_uses` |
|---|---|
| CRITICAL | 8 |
| HIGH | 7 |
| MEDIUM | 5 |
| GRIPES | 3 |

`profile_max_uses` ignores the profile arg and delegates to `web_search_max_uses_for_severity` so the web-search tool builder and the verifier read from one map.

### Modes (`verification_modes.select_verification_mode`)

Priority order: cache-hit replay → local_skip → escalated → CRITICAL `california_ahj` initial pass → GRIPES → non-GRIPES `internal_coordination` → default.

| Mode | When | Model | Thinking | Search budget | web_fetch (Chunk 11) | Escalates? |
|---|---|---|---|---|---|---|
| `local_skip` | keyword classifier or Haiku triage said `local_skip` | (none) | n/a | 0 | no | no |
| `strict_structured` | GRIPES OR non-GRIPES `internal_coordination` profile | Sonnet | off | severity-based | no | no |
| `standard_reasoning` | default for substantive technical claims | Sonnet | on | severity-based | yes (3 fetches) | yes |
| `deep_reasoning` | escalated, OR initial pass for CRITICAL `california_ahj` | Opus | on | severity-based | yes (3 fetches) | no (terminal) |

`verification_routing.select_routing` is the unified pure-function selector that returns the full policy bundle; `build_verification_request` builds the kwargs dict used by every verification path (real-time, batch initial, batch retry, batch continuation). The fetch eligibility flag is derived from the routed mode in `build_verification_tools_from_decision` (STANDARD/DEEP only) so a future mode that opts into fetch adds itself to the eligible set in one place.

### Local-skip safety
`triage.is_eligible_for_haiku_triage` hard contract: findings with any non-empty `codeReference` are never eligible; CRITICAL/HIGH are never eligible; on API failure or parse error all affected findings default to `web_required`.

### Real-time fallback
When a batch retry tail shrinks below `_REALTIME_FALLBACK_THRESHOLD` (5), the remainder flips to real-time rather than waiting another batch cycle.

---

## 4) Trust Model / Report Output

`report_status.py` defines closed sets:

| `ReportStatus` | When |
|---|---|
| `VERIFIED_SUPPORTED` | `CONFIRMED`, grounded |
| `VERIFIED_CONTRADICTED` | `CORRECTED`, grounded |
| `DISPUTED` | explicit DISPUTED, or grounding downgrade |
| `INSUFFICIENT_EVIDENCE` | `UNVERIFIED` with no contradictory citation; verifier ran cleanly but couldn't ground a claim |
| `LOCALLY_CLASSIFIED` | `local_skip` resolved (deterministic detector, keyword classifier, or Haiku triage) |
| `NOT_CHECKED` | no verification ran |
| `MANUAL_REVIEW_REQUIRED` | suppressed by cross-check, or precondition / parser failure |
| `VERIFICATION_FAILED` | `VerificationResult.verification_failed=True` — verifier hit a transient operational error (rate limit, server error, network error, parse error, `INVALID_REQUEST`, `BATCH_CANCELED`, real-time fallback crash). Distinct from `INSUFFICIENT_EVIDENCE`; the cache refuses to persist these results so a re-run re-attempts verification. |
| `VERIFIED_CONTESTED` | `VerificationResult.models_disagreed=True` — initial (Sonnet) and escalated (Opus) verifiers BOTH grounded their verdicts (each with at least one accepted citation) AND reached different conclusions. Distinct from `VERIFIED_SUPPORTED`/`CONTRADICTED`; the disagreement itself is the quality signal, carried into the report and sidecar so a downstream applier can withhold the edit. |

| `EditActionLabel` | When |
|---|---|
| `EDIT_SUGGESTED` | finding carries an edit proposal |
| `REPORT_ONLY` | no edit proposal |
| `SUPPRESSED` | `suppression_reason` set |

Both labels are *derived* from existing `Finding` fields (`suppression_reason`, `edit_proposal`) — no new persistence column. The app emits edit instructions but never applies them, so the label is a simple "is there a suggested edit?" classification; verification status and `edit_confidence` travel alongside for a downstream applier to gate on.

---

## 5) Deterministic Pre-Screen

Detectors run before any API call. Every alert carries a stable `deterministic_rule` id and is rendered under a `(deterministic check)` heading in the report.

| `deterministic_rule` | What it catches |
|---|---|
| `leed_reference` | LEED mentions inappropriate for project context |
| `placeholder` | `[SELECT]`, `[VERIFY]`, `TBD`, etc. |
| `template_marker` | `TODO:` / `FIXME` / `XXX` / `???` / lorem ipsum |
| `stale_code_cycle` / `stale_asce7` | real published cycle that isn't the current one |
| `invalid_code_cycle` | year/code combinations that aren't a real cycle (e.g. `2018 CBC`) |
| `empty_section` | section headings with no body |
| `duplicate_heading` | repeated heading within one document |
| `duplicate_paragraph` | substantial paragraphs (≥80 chars) repeated verbatim |
| `inconsistent_filename` | CSI-number / filename mismatches |

Stale = real historical cycle; invalid = fabricated year. Disjoint by construction.

---

## 6) Token Budgets

Output caps live in `api_config._PHASE_OUTPUT_BUDGET` and clamp to the selected model's ceiling via `phase_output_cap(phase, *, model)`. Unknown phases fall back to the verification cap (most conservative).

| Phase | Cap |
|---|---|
| Review / batch review | 128k |
| Extended batch review | 300k (batch-only, inputs ≥200k) |
| Cross-check | 96k |
| Verification (+ retry / continuation) | 16k |
| Triage | 8k |

Context limits (`tokenizer.py`): `MAX_CONTEXT_TOKENS=1_000_000`, `RECOMMENDED_MAX=500_000` (per-spec input — preflight raises), `CROSS_CHECK_RECOMMENDED_MAX=822_000`.

When the exact Anthropic count is unavailable, `tokenizer.safe_local_estimate` pads the local cl100k count by a model-specific safety multiplier (Opus/Sonnet 1.10×, Haiku 1.15×, unknown 1.20×).

---

## 7) Prompt Caching

`api_config.cache_policy_for(phase)` is the single source of truth. TTL is hardcoded to `1h`.

| Phase | Cached? | Why |
|---|---|---|
| Review / batch review / cross-check / verification (+ retry/continuation) | yes | reused across specs/waves |
| Triage | no | one-off and below the 2048-token Haiku cache minimum |

---

## 8) Environment Variables

Model-id overrides plus a handful of operator switches for rollback / cache control. Boolean flags accept `0` / `false` / `no` / `off` (case-insensitive) to disable; anything else leaves the default-enabled behavior in place.

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_REVIEW_MODEL` | Opus 4.7 | Override review model |
| `SPEC_CRITIC_VERIFICATION_MODEL` | Sonnet 4.6 | Override verifier initial-pass model |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | Opus 4.7 | Override escalation model |
| `SPEC_CRITIC_TRIAGE_MODEL` | Haiku 4.5 | Override triage model |
| `SPEC_CRITIC_ELEMENT_IDS` | on | Disable to revert to legacy plain-body spec rendering (no `<para id="...">` wrappers) |
| `SPEC_CRITIC_VERIFICATION_CACHE_PERSIST` | on | Disable to keep the verification cache in-memory only |
| `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` | `60` days | Age-based pruning on cache load. Explicit `0` restores the legacy "no expiry" behavior; malformed/negative values fall back to the 60-day default so a typo never silently turns the cache into a permanent database. |
| `SPEC_CRITIC_CACHE_PATH` | `~/.spec_critic/verification_cache.json` | Override the on-disk cache file path; `~` and `$VAR` are expanded |
| `SPEC_CRITIC_TRACE` | on | Disable with `0` / `false` / `no` / `off`. Writes a forensic JSONL trace to `~/.spec_critic/traces/<run_id>/`. |
| `SPEC_CRITIC_TRACE_DEEP` | off | Enable with any truthy value to record per-stream chunks, full web_search snippet bodies, batch-verification thinking / tool-use blocks, untruncated raw responses, and inline prompts. Implies trace enabled. |
| `SPEC_CRITIC_TRACE_DIR` | `~/.spec_critic/traces/` | Override the trace root directory. `~` and `$VAR` are expanded. |

---

## 9) Resume State

Phase constants in `resume_state.py`: `PHASE_REVIEW_POLL`, `PHASE_REVIEW_COLLECT`, `PHASE_VERIFICATION_POLL`, `PHASE_VERIFICATION_WAVE_POLL`, `PHASE_CROSS_CHECK`, `PHASE_CROSS_CHECK_VERIFICATION_POLL`, `PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL`, `PHASE_FINALIZE`.

`serialize_extracted_spec` records SHA-256 of both the extracted content and the source file; deserialize warns when either differs at resume time. Active review mode is persisted so a resumed run uses the same prompt path it started with.

`structured_payload` and routing decisions stashed in `request_contexts` are in-memory only — not persisted.

---

## 10) Test Harness

Hermetic by default — no API key, no network, runs in a few seconds.

- `tests/conftest.py` injects a placeholder `ANTHROPIC_API_KEY`. `@pytest.mark.network` tests skip unless a real key is set.
- GUI tests skip at collection time when `tkinter` is unavailable.
- Markers registered in `pyproject.toml`: `token_budget`, `prompt_serialization`, `network`.
- Fake Anthropic response builders: `tests/fixtures/fake_anthropic.py` (tool-use, JSON-text fallback, `max_tokens` incomplete; `dict_shape=True` emits plain-dict variants for the batch retrieval path).
- In-memory DOCX builders: `tests/fixtures/docx_fixtures.py`.

---

## 11) Dependencies

Python 3.11+. Runtime packages: `anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic`. Pinned in `requirements.txt`.
