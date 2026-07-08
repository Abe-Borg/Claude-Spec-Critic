# CLAUDE.md — Spec Critic v3.0.0

Engineering reference for the Spec Critic codebase. Focuses on non-obvious invariants and orientation — read the source for full type signatures.

---

## Working agreements

**PR workflow (standing instruction):** After pushing commits to a feature branch, open a pull request against `master` without waiting to be asked — update the existing open PR if one is already open for the branch. This durably authorizes PR creation and overrides the default "don't open a PR unless explicitly asked" behavior. Still confirm before merging, force-pushing, or other destructive / irreversible actions.

---

## 1) What it is

Python desktop app (CustomTkinter) for reviewing construction-specification `.docx` files under a selectable **review module** — one validated domain configuration (jurisdiction/code basis, prompt content, detector vocabulary, routing keywords, chunk map) picked from `src/modules/`. The default and currently only module is **California K-12 DSA mechanical/plumbing** (`california_k12_mep`); a data-center fire-suppression module is specified but not yet built (`docs/datacenter_fire_module_plan.md`). The engine extracts text, runs deterministic local pre-screens, sends per-spec reviews through Claude's Message Batches API, optionally runs cross-spec coordination, verifies findings against web search, and exports a Word report plus a machine-readable JSON sidecar of suggested edits — and contains no domain-specific content of its own.

**The app emits edit instructions but does not apply them.** Each finding may carry a structured edit proposal (action / existing text / replacement text); these are rendered in the report and written to a `<report-stem>.edits.json` sidecar for a separate, future applier program to ingest. The surgical-edit / write-back machinery was removed in v3.0.0.

The per-spec review runs through the Message Batches API on Claude Opus 4.8 (the default; override via `SPEC_CRITIC_REVIEW_MODEL`). The 300k extended-output path lifts the batch review output cap for inputs ≥200k tokens (`output-300k-2026-03-24` beta header, batch-only by API design); smaller inputs use the shared baseline cap. Verification also runs as a batch, with a synchronous fallback for small unresolved tails (see "Real-time fallback") and a synchronous cross-spec coordination pass.

A submitted review batch is persisted to disk (`orchestration/batch_resume.py`, default `~/.spec_critic/pending_batch.json`) so a detached or interrupted run can reconnect to the still-running remote batch and finish (re-poll → collect → verify → cross-check → report) without re-submitting or re-paying for the review. The persisted state carries the batch's `request_map` verbatim (so review results return even if the source files moved) and the project-context text (which may include text extracted from attached context files), but never serializes spec bodies — they are re-extracted deterministically from the recorded inputs. The GUI startup resume prompt (`offer_batch_resume`) rejoins a still-running batch from this saved state; the manual **Recover batch…** action and the standalone `scripts/recover_batch.py` recover a batch by id with no saved state (`thin_submission_from_batch_results`), rebuilding the `request_map` from the batch's results — which requires the batch to have ended first.

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
│   ├── pricing.py              # Model pricing table + request cost estimate (USD; dependency-free)
│   ├── tokenizer.py            # Local + Anthropic token counting
│   └── ui_state.py             # Persisted GUI selections (selected module)

# Domain modules (module-extraction refactor, in progress)
├── modules/
│   ├── base.py                 # ReviewModule frozen dataclass + registry validation
│   ├── california_k12_mep.py   # The CA K-12 DSA M&P module (the original domain)
│   └── registry.py             # AVAILABLE_MODULES / DEFAULT_MODULE / get_module

# UI
├── gui/
│   ├── gui.py                  # CustomTkinter app shell
│   ├── widgets.py              # Reusable UI components
│   ├── about_usage_dialogs.py  # About / API-usage dialogs
│   ├── context_attachment.py   # Pure (tkinter-free) Project Context merge / token-cap / attachment-wrap helpers
│   └── *_controller.py         # 7 thin bridges between widgets and pipeline
│                               # (batch, context, diagnostics, file_selection,
│                               #  report, review_run, token_analysis)

# Orchestration / state
├── orchestration/
│   ├── pipeline.py             # Core orchestration + FindingGroup/FindingOccurrence
│   ├── batch_resume.py         # Persisted pending-batch state for resume / recovery
│   └── diagnostics.py          # In-memory diagnostics report

# Review
├── review/
│   ├── reviewer.py             # Anthropic client factory + Finding model + tool-use/JSON parsing
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
│   ├── verification_prescreen.py  # Local pre-classification (local_skip / web_required)
│   ├── verification_cache.py   # Persistent claim-keyed verdict cache (JSON on disk)
│   ├── verification_profiles.py # Profile classifier + severity-based search budget
│   ├── verification_modes.py   # Verification modes + per-mode policy
│   ├── verification_routing.py # Unified routing decision + request builder
│   ├── source_grounding.py     # URL normalization + cited-source validation
│   ├── retry_policy.py         # Retry, continuation, and batch-failure taxonomy
│   └── triage.py               # Haiku-based verification triage (automatic; needs API key)

# Batch
├── batch/
│   ├── batch.py                # Anthropic Message Batches API wrapper
│   └── batch_runtime.py        # Bounded polling with progressive backoff

# Spec input
├── input/
│   ├── extractor.py            # DOCX spec extraction (parallelized) + context attachments (.docx/.pdf/.md/.txt)
│   ├── extraction_cache.py     # LRU caches for extraction + API token counts
│   └── preprocessor.py         # Deterministic local detectors

# Tracing
├── tracing/
│   ├── config.py               # Env-var parsing + capture-level enum
│   ├── session.py              # Recorder lifecycle helpers (start / reattach / stop)
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
  → verifier.verify_findings / verify_findings_batch
  → cross_checker.run_chunked_cross_check           (sequential after verification; uses verified
                                                     verdicts as input — DISPUTED findings are
                                                     filtered out of the "already identified" context)
  → verifier.verify_findings_batch                  (second pass: cross-check findings)
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

### FindingGroup vs FindingOccurrence
`Finding.occurrence_originals` holds per-file pre-merge member findings when `_deduplicate_findings` collapses across files, so per-file `existingText` / `replacementText` / `anchorText` / `evidenceElementId` differences survive the merge for the report and the edit-instruction sidecar. Singletons leave it empty (the finding is its own original). `group_findings()` expands a deduplicated finding into one `FindingOccurrence` per `affected_files` entry, binding each to its per-file original (`executable_finding()` returns that original, or falls back to the representative when none was recorded — `has_original()` distinguishes the two). **`edit_sidecar.build_edit_instructions` consumes this** to fan a merged multi-file finding out into one sidecar entry per affected file (TRUST_AUDIT P0-1 / P0-2); it is no longer test-only code.

### Finding-id namespacing (review `rf-` vs cross-check `cf-`)
`compute_finding_id(f, *, prefix="rf")` is the single content-addressed id minter — `sha256(repr(_dedup_key(f)))[:12]` under a 2-char prefix. Review findings are id-stamped inside `_deduplicate_findings` (prefix `rf-`). Cross-check / coordination findings never flow through that dedup pass, so `pipeline.assign_cross_check_finding_ids` stamps them with prefix `cf-` in `run_cross_check_for_batch`, *before* they enter cross-check verification and the edit sidecar. Without it every coordination finding reaches the sidecar with `finding_id=""` (all colliding on the empty key) and correlates as `unknown` in the trace viewer. Because the id is purely content-derived, the prefix is what guarantees a review finding and a coordination finding that share an identical dedup key never collapse into one sidecar entry (same 12-hex digest tail, different prefix). Same-content coordination findings intentionally share a `cf-` id — that is the dedup signal a downstream applier keys on, mirroring review ids post-dedup. The helper is idempotent (only fills empty ids) and chains (returns the same list). (STRUCTURAL_AUDIT P1-1.)

### Cross-check chunking: within-discipline only when chunked (TRUST_AUDIT P1-3)
`run_chunked_cross_check` falls back to per-CSI-division chunks (`_group_specs_by_chunk` → Division 21 / 22 / 23 / Controls+Commissioning 25+01 / `general`) only when the combined input exceeds `CROSS_CHECK_RECOMMENDED_MAX`; smaller projects take the single un-chunked `run_cross_check` path unchanged. **No finding is dropped or mis-attributed across chunks:** every spec lands in exactly one chunk via `_assign_chunk` (an unparseable CSI prefix routes to `general`, never dropped), singleton-division chunks are pooled into `general` so each chunk has ≥2 specs, the union of chunk specs equals the input, and each finding carries its own chunk label in `section` (`_label_finding_with_chunk`). **Known limitation:** each chunk is cross-checked in isolation (one `run_cross_check` call sees one chunk's specs), so a coordination conflict spanning two *different* divisions in *different* chunks is **not detectable once chunking is active** — a chunked run is a within-discipline pass. This is an intentional tractability trade-off (the alternative is the prior all-or-nothing `skipped`), documented in the `run_chunked_cross_check` docstring and surfaced to the operator in the chunking log line. Partial chunk failure does not drop the other chunks' findings: `_synthesize_chunk_findings` keeps every completed chunk's findings and the combined `cross_check_status` is `completed` when **≥1** chunk completed (`failed`/`skipped` only when *zero* completed); the per-chunk completed/failed/skipped tally is recorded in the summary (`thinking`) header. Because a partial failure stays `completed`, `run_chunked_cross_check` also stamps `ReviewResult.chunk_failures` / `chunk_skips` (in-memory telemetry, default 0 for non-chunked runs) so the Run Diagnostics banner can red-flag the "Cross-spec coordination" row with "— N chunk(s) not analyzed" instead of showing a falsely-clean green count (`report_exporter._summarize_run_diagnostics` reads them defensively). Locked in by `tests/test_cross_check_chunking.py` (completeness, the cross-division limitation, partial-failure preservation + counts, no mis-attribution) and `tests/test_diagnostic_banner.py` (banner red-flag render).

### REPORT_ONLY action
The structured tool schema includes `REPORT_ONLY` so coordination/interpretation findings don't have to fabricate `existingText` / `replacementText`. `validate_edit_shape` demotes EDIT/DELETE/ADD findings that lack action-specific required fields to REPORT_ONLY with `demotion_reason` stamped. It also demotes a **no-op EDIT** — one whose `existingText` is byte-for-byte identical to `replacementText` (TRUST_AUDIT P1-1) — so a "find X, replace with the same X" instruction never reaches the report or the edit sidecar. The check is exact-equality only: a case- or whitespace-only delta is not byte-equal and is intentionally allowed through (defined-term capitalization, spacing fixes are legitimate edits). Because both the parse-time path (`_parse_findings`) and the defensive `Finding.as_edit_proposal()` accessor route through `validate_edit_shape`, the no-op is rejected for parser output and directly-constructed/legacy findings alike.

### Edit instructions are emitted, not applied
`Finding.as_edit_proposal()` (in `reviewer.py`) is the single accessor for a finding's structured edit proposal (`action_type` / `existing_text` / `replacement_text` / `anchor_text` / `insert_position` / `target_element_id` / `edit_confidence`), reconstructing one from legacy fields when needed and returning `None` for REPORT_ONLY / invalid shapes. Proposals are rendered in the report and serialized to the `<report-stem>.edits.json` sidecar by `edit_sidecar.write_edit_instructions_sidecar` (which owns its own `_serialize_edit_proposal` flattener). Nothing in this codebase locates or applies edits — that is a future, separate program's job.

The sidecar (schema v3) emits **one entry per affected file**, not one per finding: a defect deduplicated across N templated specs produces N actionable entries, each with that file's own `fileName` / `evidenceElementId` / `edit_proposal` (via `group_findings()` + `executable_finding()`), so a downstream applier never silently skips the identical defect in files `b` / `c` because it was merged into representative `a`. Emission is gated on the *representative* carrying a proposal (REPORT_ONLY findings emit nothing, matching the report). Display / verification fields (`issue` / `severity` / `verification_verdict` / `report_status`) are sourced from the representative because verification runs *after* dedup and only the representative carries a `VerificationResult`; per-file originals carry none. Per-file entries share the representative's `finding_id`, list the whole group in `affected_files`, and carry `has_per_file_original` (False ⇒ the locator was borrowed from the representative). The natural unique key for an entry is `(finding_id, fileName)`.

### Prompt-cache breakpoint stability
The instruction prefix in front of `<spec ` must stay byte-identical across calls so cache breakpoints land in the same place. The `<final_task>` block sits *after* the spec body (and after `<pre_detected>` when alerts fire) for this reason. `prompt_serialization.py` is the single source of truth for escaping wrapper attributes/bodies.

### Project Context attachments
Project Context is free-text that ships on **every** review, cross-check, AND verification call, so anything spliced into it makes all phases aware of that content at plain-text cost. **File attachments** feed it — `.docx` / `.pdf` (text extracted) and `.md` / `.txt` (read verbatim) via `input/extractor.extract_context_text`, wrapped + merged + cap-checked by the pure (tkinter-free, unit-testable) helpers in `gui/context_attachment.py` (stable delimiter shape + the hard `PROJECT_CONTEXT_MAX_TOKENS` (100k) cap; an over-cap merge is **refused, never truncated**). Locked in by `tests/test_context_attachments.py` (file-attachment extraction + merge/cap helpers, hermetic).

### Token preflight raises (not warns)
`pipeline._prepare_specs` raises `ValueError` when the exact Anthropic count exceeds `RECOMMENDED_MAX`. Earlier behavior was log-only with cl100k as the only hard gate.

### Model capability whitelist
`api_config.model_capabilities(model)` is the single source of truth for adaptive-thinking / extended-output / 1M-context / strict-tool-use eligibility. Whitelist covers Opus 4.8, Sonnet 4.6, Haiku 4.5 (all three carry `supports_strict_tools=True`; the tool builders in `structured_schemas` consult it via `_strict_for_model`, so `strict: true` never reaches a model the whitelist doesn't vouch for). Opus 4.8 carries adaptive thinking, 128k output, the `output-300k-2026-03-24` batch beta, 1M context, and `effort` support per Anthropic's published Opus 4.8 capabilities — and like every Opus id it must appear in **both** `_MODEL_CAPABILITIES` **and** `OPUS_MODELS` (the latter drives the 128k output ceiling in `output_cap_for_model` and the high-effort escalation tier in `effort_config_for`; missing from it = silent clamp to the Sonnet 64k ceiling / medium effort). **Unknown model ids degrade to safe defaults that disable every capability flag** — a misconfigured env var produces a smaller request, never an API rejection — but the degradation is **no longer silent**: `model_capabilities` emits one `WARNING` per unrecognized id (deduped via `_WARNED_UNKNOWN_MODELS` so the per-request hot path can't spam the log) naming the conservative caps it fell back to, so a stale whitelist that quietly under-powers a newer/better model (TRUST_AUDIT P0-3) is visible to the operator. The default models now track Opus 4.8: `REVIEW_MODEL_DEFAULT` and `VERIFICATION_ESCALATION_MODEL` both default to Opus 4.8 (at first-party flagship pricing). The initial verifier stays Sonnet 4.6 and cross-check stays Sonnet 4.6 — only the deep-reasoning review and the escalation tier ride the newest Opus. Override review / the initial verifier / the escalation tier / triage via the matching `SPEC_CRITIC_*_MODEL` env var; **cross-check has no env override** — it is bound directly to `CROSS_CHECK_MODEL_DEFAULT`. Haiku phases (triage) never carry the `thinking` key. Effort defaults to `xhigh` for review and cross-check (Anthropic's recommended starting point for coding/agentic work on Opus 4.8 — the deepest-reasoning phases), `high` for the Opus escalation tier, and `medium` for the Sonnet verification initial pass. **`xhigh` is Opus-4.8-only**: Sonnet 4.6's supported set is `{low, medium, high, max}` and it rejects `xhigh` at submit with a 400 (`This model does not support effort level 'xhigh'`). Because `supports_effort` is a coarse boolean (true for both Opus and Sonnet), `effort_config_for` clamps `xhigh` → `high` via `_clamp_effort_for_model` on any non-`OPUS_MODELS` model. This is load-bearing for cross-check, which *defaults* to `xhigh` but **always** runs on Sonnet 4.6 (`CROSS_CHECK_MODEL_DEFAULT`), so without the clamp every cross-spec coordination pass 400'd at submit and produced zero findings; the same clamp also protects a `SPEC_CRITIC_REVIEW_MODEL`-overridden-to-Sonnet review. Locked in by `tests/test_capability_policy.py::TestXhighClampsOnNonOpus`.

### Verification cache key
`cycle_label | standards_fingerprint | actionType | codeReference | sha256(claim_summary)`. Intentionally omits the verifier model — `VerificationResult.model_used` is stored as provenance inside the entry. Switching `SPEC_CRITIC_VERIFICATION_MODEL` does NOT invalidate existing entries; switching the code cycle does. The `standards_fingerprint` (`_standards_fingerprint` → `sha256` of `cycle.edition_summary_lines()`) closes the gap where correcting an *edition string within* a cycle (label unchanged, e.g. fixing an UNVERIFIED ASHRAE edition) left verdicts grounded against the old edition silently cached — an edition change now produces fresh keys and re-grounds. It tracks editions only, not provenance: flipping a `source` off `UNVERIFIED` without changing the edition keeps entries warm (the verification question is unchanged). Claim digest is 24 hex chars; older 16-char entries miss → re-ground → write new 24-char entries (`_CACHE_SCHEMA_VERSION` bump — now v4, the standards-fingerprint bump — drops the legacy shape).

### Cache-replay visibility
`_clone_for_hit` stamps the sidecar `_CacheEntry.created_ts` onto `VerificationResult.cache_entry_created_ts` so the report can render an inline "Cache replay — Nd old" badge (amber <30d / orange 30-90d / red >90d) without re-reading the cache file. Per-finding evidence panel surfaces the configured cache path so a reviewer can locate and delete a single entry to force re-verification. Default TTL is now 60 days (down from no-expiry); set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS=0` to restore the legacy database behavior.

### Run Diagnostics banner
`report_exporter._write_run_diagnostics_banner` renders a styled table right after the title block surfacing operational health: edit-suggested / report-only counts (from the edit-action histogram), **specs that failed review** (`failed_review_count` — highlighted red when > 0, see "Review-stage failure surfacing"), cache replays + oldest entry age (`cache_entry_created_ts`), verification failures (the `VERIFICATION_FAILED` status — highlighted red when > 0), parse-time REPORT_ONLY demotions (`Finding.demotion_reason`), spec content extraction warnings, and cross-spec coordination status (skipped/failed highlighted red). When verification failures > 0, a recovery-hint paragraph below the banner explains the ⚠ glyph and notes that the cache does not persist operational-failure results, so a re-run sees them fresh. Most values are derived from existing `Finding` / `VerificationResult` fields — no new persistence; `_summarize_run_diagnostics` is the pure helper used by the renderer (and unit tests).

### Review-stage failure surfacing
A spec whose individual review *failed* (truncated / parse-error / errored / no result) produces **zero findings** — indistinguishable from a genuinely-clean spec in the final artifact unless flagged. `collect_review_batch_results` already records these in `CollectedBatchState.truncated_specs` (one entry per failed spec, paired with an `errors` entry and a `combined.error` summary); `finalize_batch_result` now carries that list onto **`PipelineResult.failed_review_specs`** (the single field that closes the honesty gap — `truncated_specs` was previously visible only in the GUI log + diagnostics, never the exported report).

Three report surfaces consume it, all gated on `failed_review_count > 0` so a clean run is byte-identical to before:
- **Run Diagnostics banner** — a "Specs that failed review (not reviewed)" row (placed at the top of the operational-health rows, red-shaded `FFE5E5` when > 0) plus a red recovery-hint paragraph naming each failed spec and stating that the absence of findings does NOT imply compliance. Rendered *first* among the hint paragraphs (above the verification-failure / budget hints).
- **Title block** — the "Files Reviewed" line switches from `{N}` to `{reviewed} of {submitted} ({failed} failed review)`.
- **Files Reviewed bullet list** — failed specs are annotated in red as "— review failed (not reviewed)".

GUI terminal state: `review_run_controller.on_review_complete` routes a run with `review_result.error` set to the amber `AnimatedButton.set_complete_with_errors()` state (vs. the green `set_complete()`) and finalizes diagnostics at `warning` level (never bare `success`), so a partially-failed run never presents the same terminal state as a clean one. `failed_review_specs` is empty on a clean run and defaults to `[]` everywhere (the report exporter reads it via defensive `getattr`, so legacy callers / test doubles stay at 0).

### Pinned standards editions
`CodeCycle.standards` is an ordered `tuple[StandardEdition, ...]` — a single collection that replaced the legacy flat `nfpa13` / `ashrae_62_1` / … fields and the `ul_listing_editions` tuple-of-tuples (refactored for readability + to kill the triplicate hand-enumeration in the three render sites). Each `StandardEdition` carries `name`, `edition` (the base edition California adopted), `ca_amended` (descriptive flag), `note` (a *rendered* descriptor such as "California Edition"), and `source` (maintainer provenance, **never rendered into any prompt**; prefix with `UNVERIFIED` when the edition has not been confirmed against the published code). `edition_phrase` renders one-directionally — `"2025, as amended by California"` — so the relationship (California adopts/amends the national standard, not the reverse) can't be misread; `description` prepends the name. `tuple` + frozen `StandardEdition` keeps `CodeCycle` hashable under `frozen=True`. `CALIFORNIA_2025` pins NFPA 13/14/20/24/25/72 **verified against the California Fire Code 2025, Ch. 80 adoption table** (13=2025 CA-amended, 14=2024, 20=2025 CA-amended, 24=2025, 25=2013 California Edition, 72=2025 CA-amended) and ASHRAE 15=2022, plus — marked `UNVERIFIED` pending confirmation against the published Title 24 Part 6 standards table — ASHRAE 62.1=2019 / 90.1=2019, IAPMO TSC=2024, and the UL listings (UL 300/555/555S/268/1479). The three consumers render from `cycle.standards` via two shared helpers so the list can't drift: the verifier block (`verifier._pinned_standards_lines` → `cycle.edition_summary_lines()`, `- {name}: {edition_phrase}` bullets), the report methodology note (`report_exporter._render_pinned_editions_note`, `"; "`-joined `description`s), and the reviewer prompt (`prompts.get_system_prompt` category #2 + `get_single_spec_user_message` → `cycle.edition_inline_phrase()`, comma-joined `description`s with a `"current editions"` fallback). The reviewer prompt now names **every** pinned standard rather than the legacy hardcoded NFPA 13/72 + ASHRAE 62.1/90.1 subset, so a stale NFPA 25 / ASHRAE 15 edition is in review scope, and adding a standard to the cycle updates all surfaces automatically. Standards with an empty `edition` are dropped from every surface, so a cycle that pins nothing degrades gracefully. `cycle.unverified_standards()` lists the entries still flagged `UNVERIFIED`; `docs/standards_provenance.md` records the per-standard source, date checked, and confidence. **Verify edition strings against the published code before relying on them.** Note: the verification cache key folds in a fingerprint of the cycle's pinned editions (`_standards_fingerprint` → `sha256` of `cycle.edition_summary_lines()`), so correcting an edition string *within* the 2025 cycle now DOES invalidate the affected cached verdicts automatically — no manual cache-clear needed. (A `source`-only change that leaves the edition intact keeps entries warm, since the verification question is unchanged.) Locked in by `tests/test_pinned_standards_editions.py`.

### Deterministic-rule ids are public
Every preprocessor alert carries a stable `deterministic_rule` id (exposed as `DETERMINISTIC_RULE_*` constants). The verification router's local-skip keyword list recognizes the rule names, so a GRIPES finding mentioning `todo` / `lorem ipsum` / `duplicate paragraph` / etc. is locally skipped. CRITICAL/HIGH and any non-empty `codeReference` still force `web_required`.

### Stale-cycle suppression window
`preprocessor._should_suppress_stale_cycle` scans up to 80 chars on each side for whole-word negation/historical terms (`previously`, `formerly`, `superseded`, `withdrawn`, `obsolete`, `no longer`, `prior`, `historical`, plus auxiliary-verb negations). The window narrows at the nearest sentence terminator. Bare `not` is intentionally not a suppressor. Active stale requirements ("Comply with 2019 CBC") still flag.

### Edit-action labels
`report_status.classify_edit_action` is the single source of truth and is intentionally simple now that the app emits — but never applies — edits:
- no edit proposal → `REPORT_ONLY`,
- otherwise → `EDIT_SUGGESTED`.

There is no confidence gate, no supportive-status filter, and no numeric/standards demotion — those existed only to decide *auto-apply*, which this app no longer does. A finding's verification status (`VERIFIED_SUPPORTED` / `VERIFICATION_FAILED` / `VERIFIED_CONTESTED` / etc.) and `edit_confidence` ride along in the report and the JSON sidecar so a downstream applier can do its own gating. `summarize_edit_actions` feeds the Run Diagnostics banner's edit-suggested / report-only counts.

### LOCALLY_CLASSIFIED keyword tightening
`verification_prescreen._LOCAL_SKIP_KEYWORDS` no longer contains `"formatting"` — too broad, a real CMC formatting requirement ("label valves per ASME A13.1 color formatting") could match and bypass verification. `"leed"` and `"internal contradiction"` were moved to `_LOCAL_SKIP_KEYWORDS_REQUIRES_ELEVATED`; they still route to `local_skip` (web search adds no signal for either) but `local_skip_requires_elevated_confidence(finding)` returns `True` for them. `_local_skip_result()` accepts a `requires_elevated_confidence=` kwarg and stamps it onto `VerificationResult.requires_elevated_confidence`. The flag is carried into the sidecar but never reaches the verification cache (local-skip results aren't grounded, so the cache's `grounded` guard drops them; no schema bump needed). It is retained as telemetry for a downstream applier — nothing in this app consumes it for routing anymore. A finding matching BOTH a regular keyword and an elevated keyword takes the regular path with no flag set — the regular-list match (placeholder, TODO, duplicate paragraph) is the stronger signal because it maps directly to a deterministic detector. Haiku-triaged local skips never get the flag.

### DOCX content-loss warning
`extractor._detect_content_loss_warning(body)` counts direct children of `<w:body>` (paragraphs and tables, skipping `<w:sectPr>` which is metadata) that contain at least one descendant `<w:drawing>` / `<w:pict>` / `<w:object>` element. When that proportion exceeds `_CONTENT_LOSS_WARNING_THRESHOLD` (0.20, strict `>`), the helper returns a warning string of the form `"Spec contains {N}% non-text elements ({drawings} drawings, {pictures} pictures, {objects} OLE objects). Some content may not have been extracted for review. Verify visually."` The threshold is strict (>) so a borderline 20% spec doesn't generate noise on every run. The warning is appended to `ExtractedSpec.extraction_warnings` (new list field on the dataclass). `PipelineResult.extracted_specs` carries the list of extracted specs through `finalize_batch_result` so `report_exporter._summarize_run_diagnostics` can read each spec's `extraction_warnings` and count the number of affected specs. The Run Diagnostics banner's "Spec content extraction warnings" row (slot reserved earlier) now shows the real count and the value cell is shaded red (`FFE5E5`) when > 0. The banner reports affected-spec count, NOT total warning count — a single spec with three warnings still counts as one affected file, since the "verify visually" prompt is one-per-document anyway.

### DOCX supplemental content extraction (body-walk-invisible text)
`extract_text_from_docx` walks `<w:body>` for paragraphs and tables, but a requirement can be authored in places that walk never reaches. Four such sources are extracted as **labeled blocks appended after the body**, each via the shared `_append_supplemental_block(paragraphs, paragraph_map, …)` helper, which appends a delimiter + its entries to *both* lists in lockstep so the reconstruction invariant (`"\n\n".join(m.text for m in paragraph_map) == content`, enforced at the end of extraction — it raises `ValueError` on mismatch) holds: **text boxes** (`_collect_textbox_mappings` — every `<w:txbxContent>` reached by a body-level `.//` descendant search, covering DrawingML `wps:txbx` and legacy VML `v:textbox`; one entry per non-empty direct-child `<w:p>`, prefixed `[Text Box] `, ids `tb<box>p<para>`, delimiter `meta:tb`), **footnotes** and **endnotes** (`_collect_note_mappings` — locates the `word/footnotes.xml` / `word/endnotes.xml` package part **by content type** since relationship ids aren't stable, `parse_xml`s it defensively (any parse failure → `[]`, never sinks extraction), skips the structural `separator`/`continuationSeparator` notes by `w:type`, prefixes `[Footnote <id>] ` / `[Endnote <id>] `, ids `fn<id>p<para>` / `en<id>p<para>`, delimiters `meta:fn` / `meta:en`), and **headers/footers** (the pre-existing per-section walk, refactored onto the same helper; prefix `[Header]`/`[Footer]`, ids `s<n>h<i>`/`s<n>f<i>`, delimiter `meta:hf`). Block order is text boxes → footnotes → endnotes → header/footer. Every collector **no-ops on absence**, so a spec with none of these produces byte-identical output to before (the common case). A text-box-bearing body paragraph has empty `Paragraph.text` (the text is nested), so it contributes no body entry and the box text is *not* double-counted; a paragraph with both visible text and a box captures each exactly once. The content-loss warning is intentionally **not** adjusted for now-extracted text boxes — a text-box drawing still counts toward the drawing proportion, over-warning slightly in the safe "verify visually" direction. Known remaining gaps (TRUST_AUDIT P0-6 "Remaining"): SmartArt / grouped-shape text, text boxes anchored inside headers/footers, and tables nested inside a text box or note. New element ids are additive and never collide with existing ones; downstream consumers (preprocessor, tokenizer, report, sidecar) treat the extra text/ids transparently.

### Web-fetch for follow-up reads
`api_config.build_web_fetch_tool()` returns the `web_fetch_20260209` server-tool dict (citations enabled, `max_uses=DEFAULT_VERIFICATION_MAX_FETCHES=3`, `max_content_tokens=WEB_FETCH_MAX_CONTENT_TOKENS=50_000`, blocklist mirrored from `web_search` so the two server tools share one source-quality policy). `verification_routing.build_verification_tools_from_decision` appends the tool for `VerificationMode.STANDARD_REASONING` and `VerificationMode.DEEP_REASONING` only; STRICT_STRUCTURED and LOCAL_SKIP intentionally omit it (those modes are explicitly cheap/narrow and don't benefit from a deeper read). The verdict tool stays at the end of the tool list so `tools_with_cache` attaches the trailing cache breakpoint to the right tool.

**Web fetch is generally available and takes NO `anthropic-beta` header.** This code originally shipped attaching `extra_headers={"anthropic-beta": "web-fetch-2026-02-09"}` on the assumption that the header was "harmless when the API treats web_fetch as generally available, required when still gated." That was wrong on both counts: web_fetch is GA (the `web_fetch_20260209` tool dict alone enables it, at no extra cost beyond fetched-content tokens), and an *unrecognized* `anthropic-beta` value is rejected with HTTP 400 `invalid_request_error: Unexpected value(s) ... for the anthropic-beta header`, not silently ignored. Every verification request routed to STANDARD_REASONING / DEEP_REASONING (the common path) carried the retired header and crashed the run at batch/stream submit. The fix: `build_verification_request` attaches **no** beta header for web_fetch — `extra_headers` stays the empty SDK transport seam (still split out from `params` because the batch API rejects unknown keys inside per-item `params`). The `web_fetch_20260209` tool itself is current and valid, so it is attached unconditionally for the two fetch-eligible modes. All other web_fetch plumbing (telemetry fields, report rendering, grounding-accepts-fetched) is unchanged.

`VerificationResult` gains `web_fetch_requests: int = 0` and `fetched_sources: list[str]` (default factory). `_collect_fetch_evidence_detailed(message)` parses `server_tool_use` blocks with `name="web_fetch"` and the paired `web_fetch_tool_result` blocks; `_web_fetch_count(message)` reads `usage.server_tool_use.web_fetch_requests`. Both real-time (`_run_verification_call`) and batch wave (`_classify_wave_results`) paths sum search successes + fetch successes into the grounded check, so a CONFIRMED verdict that converged purely via fetch still clears the grounding gate. `_apply_source_grounding` accepts an optional `fetched: list[SearchedSource] | None = None`; the pool of accepted-citation URLs is `searched ∪ fetched` so a cited URL the model fetched (but didn't search) still validates. `searched_sources` on the result is NOT augmented with fetched URLs — the report's separate "Full-text sources consulted" sub-section renders them from `fetched_sources` so snippet-grounded vs. fetch-grounded evidence stays visually distinct.

Telemetry round-trips through the verification cache (`_result_to_dict` / load path / `_clone_for_store` / `_clone_for_hit`) — runtime telemetry, not verdict semantics, so no cache schema bump is required and legacy v3 rows without the keys load with defaults of 0 / []. The evidence panel renders "Searches: N, Full-page fetches: M" when `web_fetch_requests > 0` (plain "N of M searches used" otherwise — keeps the line short for the common path) and a "Full-text sources consulted (retrieved via web_fetch):" sub-section listing the fetched URLs, omitted when the list is empty so empty sub-sections never appear.

### Escalation disagreement surfacing
`VerificationResult` gains `models_disagreed: bool = False` and `initial_sources: list[str]` (default factory). `verify_finding` snapshots `initial_grounded_snapshot = bool(result.grounded)` and `initial_sources_snapshot = list(result.sources or [])` BEFORE running the escalation call so the snapshots survive the potential `result = esc_result` swap. After the swap, `result.initial_sources = initial_sources_snapshot` is set unconditionally (so the evidence panel can still show "Initial: UNVERIFIED, no sources" for non-contested escalations), and `result.models_disagreed = initial_grounded_snapshot and bool(esc_result.grounded) and esc_result.verdict != initial_verdict_snapshot` — strictly tighter than `escalation_changed_verdict` because an initial-UNVERIFIED-then-CONFIRMED escalation should NOT register as "models disagreed" (the initial pass didn't actually ground anything to disagree about; the escalation path was doing its job).

`ReportStatus.VERIFIED_CONTESTED` (glyph `⚡`, purple `800080`) is registered in `STATUS_LABELS` / `STATUS_GLYPHS` / `STATUS_COLORS` / `STATUS_SHADING` and sits in `STATUS_DISPLAY_ORDER` between `VERIFIED_CONTRADICTED` and `LOCALLY_CLASSIFIED`. `classify_status` checks `models_disagreed` BEFORE the `local_skip` and verdict-based branches so a swapped-in CONFIRMED-grounded final verdict still classifies as `VERIFIED_CONTESTED`. A contested finding's `VERIFIED_CONTESTED` status is carried into the report and the JSON sidecar so a downstream applier sees the disagreement and can withhold the edit — the disagreement itself is the signal that the finding needs human eyes. (`classify_edit_action` still labels it `EDIT_SUGGESTED` if it carries a proposal; the app emits, it does not apply.)

Telemetry round-trips through the verification cache (`_result_to_dict` / load path / `_clone_for_store` / `_clone_for_hit`) — runtime telemetry, not verdict semantics, so no cache schema bump is required and legacy v3 rows without the keys load with defaults of False / []. The evidence panel's Escalation history line uses the purple `VERIFIED_CONTESTED` color when `models_disagreed=True` (red-orange when only `escalation_changed_verdict=True`, gray when neither fires) and appends an expanded "manual review recommended" sentence; a dedicated "Initial verifier sources:" sub-section (rendered only when `models_disagreed=True` AND `initial_sources` is non-empty) lists the initial verifier's citations side-by-side with the final verifier's citations in the regular "Web/code evidence" sub-section.

### Budget-exhaustion sentinel
`VerificationResult` gains `budget_exhausted: bool = False`. The verifier sets it on UNVERIFIED results where `web_search_requests >= decision.web_search_max_uses` — "the model spent its full mode-scaled budget without grounding a verdict", distinct from `verification_failed` (operational) and from a plain UNVERIFIED (model ran out of evidence early). Real-time detection is in `_run_verification_call`: a `budget_was_exhausted = budget > 0 and total_search_requests >= budget` boolean is computed once before the not-grounded early returns, threaded into every `_make_unverified(budget_exhausted=...)` call, and stamped onto the success-path result AFTER `_enforce_grounding_invariant` so a downgraded-to-UNVERIFIED CONFIRMED still picks up the flag when its searches were consumed. Two specific over-budget paths (pause_turn-loop exceeded 2x ceiling, max-continuations-without-complete) pass `budget_exhausted=True` directly. The batch wave path applies the same condition in `_classify_wave_results` using the stored routing decision's `web_search_max_uses` (or the re-derived first-wave decision).

The flag is in-memory runtime telemetry — `VerificationCache.put` refuses to persist `budget_exhausted=True` results for the same transient-signal reason it refuses `verification_failed=True` (a re-run at higher severity allocates more budget; freezing the shortfall would suppress re-verification). The `grounded` guard already drops every UNVERIFIED, so the explicit `budget_exhausted` guard is defense-in-depth against a future call site that constructs a grounded+exhausted result directly. Cache schema is unchanged — no new persisted keys.

`report_status.is_budget_exhausted(finding)` is the public helper that reads the flag defensively (returns `False` for missing `verification` / missing attribute). `summarize_budget_exhausted(findings)` aggregates the count. **Crucially, the flag does NOT change classification semantics**: `classify_status` still returns `INSUFFICIENT_EVIDENCE` for exhausted findings — no new top-level `ReportStatus` enum value — because the trust level is the same as any other unground UNVERIFIED. The sub-label is purely a rendering enrichment.

The report exporter (`report_exporter._write_finding_entry`) appends an italic " (search budget exhausted)" sub-label to the status line when `is_budget_exhausted(finding)` is True, colored to match the INSUFFICIENT_EVIDENCE status so the badge reads as part of the status rather than a separate field. The Run Diagnostics banner has a new "Budget-exhausted findings" row (highlighted red when count > 0) and a calmer-amber recovery hint paragraph below the table when count > 0, naming the severity-tiered budget knob (CRITICAL=8, HIGH=7, MEDIUM=5, GRIPES=3 — `api_config._SEVERITY_MAX_USES`, rendered dynamically via `web_search_max_uses_for_severity` so the hint can't drift from policy) as the actionable remedy. The hint paragraph is distinct from the verification-failure red hint above it because the cause and remedy differ: failures are transient (re-run sees them fresh); budget exhaustion is policy-driven (re-run at the same severity exhausts the same budget).

The calibration harness (`evals/calibration/harness.py:_apply_budget_exhaustion`) mirrors the production detection after grounding so fixtures with `captured_verifier_response.web_search_requests` at or above the severity budget surface the flag through the scorer. `FixtureOutcome` gains a `budget_exhausted` field; the scorer's summary header reports `budget_exhausted_count` so a recheck can confirm telemetry flowed through end-to-end. The `tp_unverified_budget_exhausted` fixture is the canonical example (HIGH-severity DSA bulletin lookup that consumed all 7 searches without grounding the cited section).

### Code basis: one per module; the CA module is California 2025 only
Each module pins exactly one `CodeCycle` (its code basis); `DEFAULT_CYCLE = CALIFORNIA_2025` backs the default module. The CA module's legacy 2022-cycle mapping was removed — **do not reintroduce it**; a different code basis means a different module with its own registry-unique cycle label. Cycle label is in the verification cache key (and backs the `module_for_cycle` bridge), so a basis change naturally invalidates prior entries and can never collide across modules.

### Module registry (module-extraction refactor, Phases 1-5)
`src/modules/` introduces `ReviewModule` — one frozen object per reviewable domain, carrying identity (`module_id`, display strings), the `CodeCycle`, the **prompt content slots** (Phase 2): `reviewer_persona`, `review_user_intro`, `review_severity_definitions`, `review_confidence_high_example`, `review_categories_template`, `review_examples`, `cross_check_persona`, `cross_check_severity_definitions`, `verifier_persona`, `verifier_source_priorities` — and (Phase 3) the **code-basis line templates** (`review_user_code_basis_line`, `cross_check_code_basis_line`, `verifier_system_code_basis_lines`, `verifier_user_code_basis_lines`) plus the **`detector_vocabulary`** (`DetectorVocabulary`), and (Phases 4-5) the **`profile_keywords`** (`ProfileKeywords`: jurisdictional / manufacturer / code_standard / internal_coordination term tuples — classifier precedence stays engine), the **`cross_check_chunk_groups`** (`ChunkGroup` tuples — chunking invariants stay engine; registration rejects duplicate ids, the reserved `general` id, and a CSI prefix in two groups), and the **`report_context_phrase`** (report methodology wording). All template slots format against `code_basis_format_kwargs(cycle)` — one placeholder per `BaseCode.key` plus `asce7` / `asce7_prev` / `pinned_standards`. The registry (`AVAILABLE_MODULES` / `DEFAULT_MODULE` / `get_module`) mirrors the `AVAILABLE_CYCLES` pattern: unknown/missing ids degrade to the default `california_k12_mep` module, and `validate_module_registry` fails at import on duplicate module ids, duplicate cycle labels (**cycle labels are registry-unique — they namespace the verification cache AND back the `module_for_cycle` bridge**), empty prompt slots, any template slot that doesn't format against the module's own cycle, empty/duplicate-keyed `base_codes`, an inconsistent detector vocabulary, or few-shot examples that violate the real parse contract (every JSON example is run through `reviewer.validate_edit_shape`; examples must not mention `evidenceElementId` / element-id tags — the block sits inside the cached system-prompt prefix).

**Code basis (Phase 3).** `CodeCycle`'s California-shaped flat fields (`cbc`/`cmc`/`cpc`/`energy_code`/`calgreen`) were replaced by `base_codes: tuple[BaseCode, ...]` — each `BaseCode(key, name, year, source)` where `key` is the template-placeholder id and `name` is informational (module templates carry their own per-surface display labels, e.g. the verifier user prompt says "CEC" where the reviewer says "Energy Code"). **The first base code is the primary** — `cycle.primary_code_year` is the stale-detector comparison target. `asce7`/`asce7_previous` stay dedicated fields (the detector does century-aware edition arithmetic on them). The deterministic detectors' *vocabulary* is module data (`DetectorVocabulary`: `code_abbreviations`, `plausible_cycle_years` ⊆ `valid_cycle_years` — the subset invariant keeps the stale and invalid detectors disjoint by construction, enforced at registration — `asce7_plausible_editions`, `stale_cycle_extra_patterns` regex sources capturing the year as group 1, `flag_leed_references`, `jurisdiction_label`); the detector *logic* (regex assembly, span dedup, negation suppression window) stays engine-owned in `preprocessor.py`, which resolves the vocabulary via `module_for_cycle` and caches compiled patterns per (hashable) vocabulary.

**Protocol vs. domain split (Phase 2).** The prompt builders (`prompts.get_system_prompt` / `get_single_spec_user_message`, `cross_checker._cross_system_prompt`, `verifier._get_verification_system_prompt`) own the *protocol* — task framing, output/tool contracts, confidence-rubric bands, review procedure, grounding language, the verifier's source-tier framing rules — byte-identical across modules so a module author cannot break the parsers. Domain content renders from the module's slots. The builders still take `cycle=` and resolve their module via **`module_for_cycle(cycle)`** — the registry's unique-label reverse lookup — so Phase 2 changed zero public signatures; the bridge retires when later phases thread `module=` through the content layers explicitly.

Orchestration entry points (`start_batch_review`, `reconstruct_batch_submission`, `thin_submission_from_batch_results`, `start_batch_verification`, `collect_batch_verification_results`) take `module=` instead of `cycle=`; stage functions with access to a submission derive the module from `submission.module_id` (`run_cross_check_for_batch`, the review-repair path, `run_batch_collection_headless`) so one state object can't pair one module's identity with another's cycle. `module_id` persists on `BatchSubmission` / `PipelineResult` / `PendingBatch` (additive field, **no pending-batch schema bump** — legacy state files load with the default module) and lands in trace `run.json`. **Phase 4 routing generalization:** `VerificationProfile.CALIFORNIA_AHJ` became `JURISDICTIONAL` (value `jurisdictional`); `parse_verification_profile` maps the legacy `california_ahj` string from persisted rows. `classify_finding_profile` / `select_verification_mode` take optional `keywords=`; `select_routing` takes `cycle=` and resolves the owning module's keywords via the bridge (all cycle-bearing verifier/batch call sites thread it; `None` degrades to the default module). **Phase 5 GUI/report surfaces:** the GUI header has a module selector (selection persisted via `core/ui_state.py`, stale ids degrade to the default module); the report methodology note renders the module's `report_context_phrase` (resolved from `PipelineResult.module_id`); `DiagnosticsReport.module_id` rides into the run-summary line and the About dialog subtitle is module-driven. The Phase 0 pins (`tests/test_golden_domain_surfaces.py` goldens + `tests/test_domain_routing_pins.py`) must stay byte-green through every extraction phase; `tests/test_module_registry.py` locks the registry, slot-validation, bridge, and round-trip contracts. **Authoring a new module** is pure module-data work — no engine edits; the full work order (contract, research protocol, tests, hard constraints) is `docs/datacenter_fire_module_plan.md`, which doubles as the general authoring guide.

### Per-finding evidence panel
The report exporter renders one collapsed "Sources" Heading 4 per finding with a verification result. Contents (in order, below the heading): verifier model, verification mode, search budget (`N of M searches used`), source quote (verbatim from a web_search snippet), verifier rationale (moved here from above the heading), escalation history when `escalation_attempted` (with the expanded escalation sentence + initial-verifier-sources sub-section when `models_disagreed=True`), accepted source URLs, rejected source URLs. The finding's proposed edit (existing text → replacement) renders inline above the panel; the machine-readable form goes to the JSON sidecar. There is no locator / "Edit Target Evidence" panel — locating an edit target is the downstream applier's job.

### Confidence vs. verdict — de-emphasis once verified
`Finding.confidence` is the **review** model's self-rated certainty, assigned *before* any verification ran (the rubric in `prompts.py` maps it to the same high/moderate/low bands the report colors). It is content the model emits and is never recomputed downstream — verification, escalation, and grounding leave it untouched (dedup takes the group `max`). It is **display/sort-only**: nothing routes, gates, or classifies on it (`classify_status` / `classify_edit_action` never read it). Because it is frozen at review time, it diverges from the post-verification trust signal — a CONFIRMED-grounded finding can carry a low review confidence (the example that motivated this), and a DISPUTED finding a high one.

`report_status.verdict_supersedes_confidence(finding)` (backed by the `VERDICT_SUPERSEDES_CONFIDENCE` frozenset = `VERIFIED_SUPPORTED` / `VERIFIED_CONTRADICTED` / `VERIFIED_CONTESTED` / `DISPUTED`) is the single predicate for "verification reached a verdict that supersedes the review %." When it's True, `report_exporter._write_finding_entry` **drops the bold colored % from the Heading-3 header** and re-renders the same number as a small gray italic footnote on the Status line — `… • review confidence 55% (pre-verification)` — so the value is preserved but can't be mistaken for the post-verification trust signal (the verdict). When it's False (no verdict reached: `NOT_CHECKED` / `INSUFFICIENT_EVIDENCE` / `LOCALLY_CLASSIFIED` / `VERIFICATION_FAILED`), the % stays the prominent header signal — it's the only trust signal those findings have. The distinction is documented for the reader in the "About This Review" methodology note. Locked in by `tests/test_report_status.py` (`TestVerdictSupersedesConfidence`, `TestConfidenceDeEmphasisRendering`).

---

## 3) Verification Routing

### Profiles (`verification_profiles.classify_finding_profile`)

Profile picks the priority-source language attached to the verifier system prompt. The web-search budget is severity-based and identical across profiles. Priority order: internal-coordination → California/AHJ → manufacturer → code-standard (or non-empty `codeReference`) → constructability.

| Profile | When |
|---|---|
| `jurisdictional` | mentions the module's jurisdiction/AHJ terms (CA module: California / DSA / HCAI / Title 24 / AHJ). Formerly `california_ahj`; legacy strings map via `parse_verification_profile`. |
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

Priority order: cache-hit replay → local_skip → escalated → CRITICAL `jurisdictional` initial pass → GRIPES → non-GRIPES `internal_coordination` → default.

| Mode | When | Model | Thinking | Search budget | web_fetch | Escalates? |
|---|---|---|---|---|---|---|
| `local_skip` | keyword classifier or Haiku triage said `local_skip` | (none) | n/a | 0 | no | no |
| `strict_structured` | GRIPES OR non-GRIPES `internal_coordination` profile | Sonnet | off | severity-based | no | no |
| `standard_reasoning` | default for substantive technical claims | Sonnet | on | severity-based | yes (3 fetches) | yes |
| `deep_reasoning` | escalated, OR initial pass for CRITICAL `jurisdictional` | Opus | on | severity-based | yes (3 fetches) | no (terminal) |

`verification_routing.select_routing` is the unified pure-function selector that returns the full policy bundle; `build_verification_request` builds the kwargs dict used by every verification path (real-time, batch initial, batch retry, batch continuation). The fetch eligibility flag is derived from the routed mode in `build_verification_tools_from_decision` (STANDARD/DEEP only) so a future mode that opts into fetch adds itself to the eligible set in one place.

### Local-skip safety
`triage.is_eligible_for_haiku_triage` hard contract: findings with any non-empty `codeReference` are never eligible; CRITICAL/HIGH are never eligible; on API failure or parse error all affected findings default to `web_required`.

### Real-time fallback
When a batch retry tail shrinks below `_REALTIME_FALLBACK_THRESHOLD` (5), the remainder flips to real-time rather than waiting another batch cycle.

**Exactly-once terminal result invariant (STRUCTURAL_AUDIT P1-2).** Every finding in `collect_verification_batch_results` ends with exactly one `VerificationResult` — never dropped, never double-written. Three properties guarantee it and must be preserved together: (1) the post-loop tail at `verifier.py:3157-3159` is the safety net — any finding still at `verification is None` after the wave loop *and* the escalation wave becomes a terminal UNVERIFIED, which is also what catches the detach-on-final-wave `break`; (2) the real-time fallback (last wave + `break`) and the follow-up-wave submit (non-last waves only) are **mutually exclusive**, success/terminal outcomes are written back *and* marked `resolved=True`, and resolved findings are excluded from `active_contexts`/`next_contexts`, so a batch-resolved finding is never also handed to `verify_finding`; (3) `_run_batch_escalation_wave` skips findings whose `verification.escalation_attempted` is set — exactly the flag the real-time fallback's `verify_finding` stamps inline — so a fallback finding is not re-escalated. Locked in by `tests/test_batch_fallback_handoff.py` (fallback enabled / disabled / detached-final-wave).

**Continuation-cap parity (STRUCTURAL_AUDIT P2-1).** The batch wave loop's per-finding continuation check is `if continuation_counts[stable_key] > cap` (NOT `>=`) — deliberate, and it must stay `>`. It gives the batch path exact parity with the real-time pause-turn loop, which runs `for _ in range(max_continuations + 1)` (one initial call + up to `cap` resumes; terminal on pause #(cap+1)). Both submit a continuation for pause #k iff `k <= cap`, so a pause-turn-only finding rides up to `cap + 1` waves (one initial + `cap` continuations) — the same number of attempts each path allows. `>=` would give the batch path one *fewer* continuation than real-time. The cap is separately clamped by `MAX_VERIFICATION_WAVES = 3`; `DEEP_MAX_CONTINUATIONS = 4 > 3` is intentional (it is the real-time budget ported in, not a tighter-than-`max_waves` early exit). A cap-terminal result is a clean UNVERIFIED with `verification_failed=False` → `INSUFFICIENT_EVIDENCE` (the model kept needing to continue, not an operational failure). Locked in by `tests/test_batch_continuation_cap.py`.

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
| `MANUAL_REVIEW_REQUIRED` | reserved for precondition / parser failures (no current producer in `classify_status`) |
| `VERIFICATION_FAILED` | `VerificationResult.verification_failed=True` — verifier hit a transient operational error (rate limit, server error, network error, parse error, `INVALID_REQUEST`, `BATCH_CANCELED`, real-time fallback crash). Distinct from `INSUFFICIENT_EVIDENCE`; the cache refuses to persist these results so a re-run re-attempts verification. |
| `VERIFIED_CONTESTED` | `VerificationResult.models_disagreed=True` — initial (Sonnet) and escalated (Opus) verifiers BOTH grounded their verdicts (each with at least one accepted citation) AND reached different conclusions. Distinct from `VERIFIED_SUPPORTED`/`CONTRADICTED`; the disagreement itself is the quality signal, carried into the report and sidecar so a downstream applier can withhold the edit. |

| `EditActionLabel` | When |
|---|---|
| `EDIT_SUGGESTED` | finding carries an edit proposal |
| `REPORT_ONLY` | no edit proposal |

Both labels are *derived* from existing `Finding` fields (`edit_proposal`) — no new persistence column. The app emits edit instructions but never applies them, so the label is a simple "is there a suggested edit?" classification; verification status and `edit_confidence` travel alongside for a downstream applier to gate on.

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

**Cache-miss debugging (opt-in).** `api_config.cache_diagnostics_*` wires the `cache-diagnosis-2026-04-07` beta into the synchronous verification continuation loop (`verifier._run_verification_call`) when `SPEC_CRITIC_CACHE_DIAGNOSTICS` is truthy. Each `pause_turn` resume carries `diagnostics.previous_message_id` (the prior turn's id) via the SDK `extra_body`/`extra_headers` seams; the response-side `diagnostics` object is read by `extract_cache_diagnostics` (defensive — never raises) and recorded as the `cache_diagnostics` trace event. Default off ⇒ byte-identical request shape. First-party Claude API only; produces no signal on the Batch API (batch items have no prior message id to diff against), so it observes the sync tail, not the dominant batch path.

---

## 8) Environment Variables

Model-id overrides plus a handful of operator switches for rollback / cache control. Boolean flags accept `0` / `false` / `no` / `off` (case-insensitive) to disable; anything else leaves the default-enabled behavior in place.

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_REVIEW_MODEL` | Opus 4.8 | Override review model |
| `SPEC_CRITIC_VERIFICATION_MODEL` | Sonnet 4.6 | Override verifier initial-pass model |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | Opus 4.8 | Override escalation model |
| `SPEC_CRITIC_TRIAGE_MODEL` | Haiku 4.5 | Override triage model |
| `SPEC_CRITIC_STRICT_TOOL_USE` | on | Disable to drop `strict: true` from the review / cross-check / verification / triage tool schemas, restoring the legacy lenient tool shape. Strict mode grammar-constrains tool input to the schema, closing the malformed-/truncated-payload parse failures the tagged-JSON fallback absorbs (on the review path those surfaced as "failed review" specs with zero findings). Anthropic's structured-outputs docs list strict tool use as compatible with adaptive thinking, streaming, and the Batches API; `tests/test_network_smoke.py::test_strict_tool_use_smoke` sends the exact production strict shape live (run `pytest -m network` with a real key after SDK/model bumps — smoke #2 pins the disabled/rollback shape). Strict attaches only on models the capability whitelist marks `supports_strict_tools` (Opus 4.8 / Sonnet 4.6 / Haiku 4.5): the tool builders take `model=` and an unknown `SPEC_CRITIC_*_MODEL` override degrades to the lenient tool shape — smaller request, never a 400 — like every other optional capability. The schemas stay inside the strict supported subset (no `minimum`/`maximum`/`minLength` — confidence is clamped at parse time, triage indices are membership-filtered at the call site). The fallback parsers stay reachable as defense-in-depth: strict makes the payload contractual only *when* the model calls the tool; `tool_choice` is still `auto`. |
| `SPEC_CRITIC_CACHE_DIAGNOSTICS` | off | Enable with any truthy value to request the `cache-diagnosis-2026-04-07` beta on the synchronous verification continuation loop (reports the first prompt-cache-prefix divergence point into the trace). First-party Claude API only; no signal on the Batch API. Default off; rollback with `0` / `false` / `no` / `off`. |
| `SPEC_CRITIC_ELEMENT_IDS` | on | Disable to revert to legacy plain-body spec rendering (no `<para id="...">` wrappers) |
| `SPEC_CRITIC_VERIFICATION_CACHE_PERSIST` | on | Disable to keep the verification cache in-memory only |
| `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` | `60` days | Age-based pruning on cache load. Explicit `0` restores the legacy "no expiry" behavior; malformed/negative values fall back to the 60-day default so a typo never silently turns the cache into a permanent database. |
| `SPEC_CRITIC_CACHE_PATH` | `~/.spec_critic/verification_cache.json` | Override the on-disk cache file path; `~` and `$VAR` are expanded |
| `SPEC_CRITIC_PENDING_BATCH_PATH` | `~/.spec_critic/pending_batch.json` | Override the pending-batch state file used for review-batch resume / recovery; `~` and `$VAR` are expanded |
| `SPEC_CRITIC_TRACE` | on | Disable with `0` / `false` / `no` / `off`. Writes a forensic JSONL trace to `~/.spec_critic/traces/<run_id>/`. |
| `SPEC_CRITIC_TRACE_DEEP` | off | Enable with any truthy value to record per-stream chunks, full web_search snippet bodies, batch-verification thinking / tool-use blocks, untruncated raw responses, and inline prompts. Implies trace enabled. |
| `SPEC_CRITIC_TRACE_DIR` | `~/.spec_critic/traces/` | Override the trace root directory. `~` and `$VAR` are expanded. |

---

## 9) Test Harness

Hermetic by default — no API key, no network, runs in a few seconds.

- `tests/conftest.py` injects a placeholder `ANTHROPIC_API_KEY`. `@pytest.mark.network` tests skip unless a real key is set.
- GUI tests skip at collection time when `tkinter` is unavailable.
- Markers registered in `pyproject.toml`: `token_budget`, `prompt_serialization`, `network`.
- Fake Anthropic response builders: `tests/fixtures/fake_anthropic.py` (tool-use, JSON-text fallback, `max_tokens` incomplete; `dict_shape=True` emits plain-dict variants for the batch retrieval path).
- DOCX inputs are built inline per test via `python-docx` (`from docx import Document`); there is no shared DOCX-fixture module — `tests/fixtures/` ships only `fake_anthropic.py`.

---

## 10) Dependencies

Python 3.11+. Runtime packages: `anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic`. Pinned in `requirements.txt`.
