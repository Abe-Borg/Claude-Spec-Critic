# CLAUDE.md — Spec Critic v2.11.0

Engineering reference for the Spec Critic codebase. Focuses on non-obvious invariants and orientation — read the source for full type signatures.

---

## 1) What it is

Python desktop app (CustomTkinter) for reviewing California K-12 DSA mechanical/plumbing `.docx` specs. Extracts text, runs deterministic local pre-screens, sends per-spec reviews (real-time or batch) through Claude, optionally runs cross-spec coordination, verifies findings against web search, exports a Word report, and optionally writes surgical edits back to a copy of each spec.

Real-time and batch share identical prompts, models, tool schemas, output caps, and parsing logic. The 300k extended-output path is the only intentional asymmetry — batch-only by API design (`output-300k-2026-03-24` beta header is not honored on streaming) and only used for inputs ≥200k tokens.

## Source layout

```
src/
├── __init__.py             # Package version (2.11.0)

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
│   └── *_controller.py         # 8 thin bridges between widgets and pipeline
│                               # (batch, context, diagnostics, edit_workflow,
│                               #  file_selection, report, review_run, token_analysis)

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

# Edits
├── editing/
│   ├── edit_locator.py         # Exact / normalized / fuzzy / id-anchored matching
│   ├── edit_candidates.py      # Edit safety categories
│   ├── spec_editor.py          # Surgical DOCX edits (transactional)
│   ├── replacement_style.py    # Per-document typographic profile + replacement normalizer
│   └── apply_edits.py          # locate → action build → apply

# Output
└── output/
    ├── report_exporter.py      # Word (.docx) report generation
    └── report_status.py        # ReportStatus / EditActionLabel + classifiers
```

## High-level flow

```
.docx files
  → extraction_cache.extract_multiple_specs_cached  (parallel; LRU keyed by mtime + content fingerprint)
  → preprocessor.preprocess_spec                    (LEED/placeholder/stale-cycle/structural alerts)
  → tokenizer.count_tokens + count_tokens_via_api   (preflight)
  → reviewer.review_single_spec  OR  batch.submit_review_batch
  → pipeline._deduplicate_findings                  (full-text SHA-256 keys)
  → cross_checker.run_chunked_cross_check           (parallel with verification by default)
  → verifier.verify_findings / verify_findings_batch
  → pipeline.finalize_batch_result
  → report_exporter.export_report
  → apply_edits.execute_edit_plan (optional)
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

### Id-anchored locator does not fall back
When `Finding.evidenceElementId` is set, `edit_locator._id_anchored_match` revalidates the recorded quote against the cited paragraph. If the id is missing or the quote no longer matches, the locator returns `SAFETY_MANUAL_REVIEW` — it does **not** fall back to whole-document text matching. A quoted-text match elsewhere in the document is treated as suspect.

### Transactional edit writes
`spec_editor.apply_edits_to_spec` saves to an in-memory buffer first and re-opens it as a `Document` to validate before writing to disk. If any individual outcome ended in `failed`, the disk write is suppressed entirely and previously-applied outcomes demote to `skipped` with `EditReport.aborted_transactional` set. Unsafe-markup skips do NOT abort the transactional write.

### Conflict resolution order
`spec_editor._detect_and_resolve_conflicts` processes overlapping edits in descending start-offset order within each `(body_index, element_type, row_index)` group so downstream edits apply before upstream edits shift their offsets. Strict containment → broader edit wins; identical spans → severity/confidence tie-break; partial overlap → both edits skipped to manual review. `ambiguous_ranges` tracking ensures a third edit overlapping a discarded pair's union span is also routed to manual review.

### Cross-check dependency suppression
`pipeline.classify_cross_check_dependencies` drops a cross-check finding only when **every** cited `upstreamFindingIds` is `DISPUTED` *and* `independentEvidenceIds` is empty. Otherwise the finding survives. Findings without cited ids fall back to a `(filename, section)` heuristic — labeled as such in logs. Dropped findings land on `suppressed_findings` with `suppression_reason` set so the report can explain the decision.

### FindingGroup vs FindingOccurrence
`Finding.occurrence_originals` holds per-file pre-merge member findings when `_deduplicate_findings` collapses across files. `apply_edits.execute_edit_plan` uses each file's own original edit fields. Non-representative files missing from `occurrence_originals` → routed to manual review rather than fanning the representative's text across files that may differ.

### REPORT_ONLY action
The structured tool schema includes `REPORT_ONLY` so coordination/interpretation findings don't have to fabricate `existingText` / `replacementText`. `validate_edit_shape` demotes EDIT/DELETE/ADD findings that lack action-specific required fields to REPORT_ONLY with `demotion_reason` stamped.

### Prompt-cache breakpoint stability
The instruction prefix in front of `<spec ` must stay byte-identical across calls so cache breakpoints land in the same place. The `<final_task>` block sits *after* the spec body (and after `<pre_detected>` when alerts fire) for this reason. `prompt_serialization.py` is the single source of truth for escaping wrapper attributes/bodies.

### Token preflight raises (not warns)
`pipeline._prepare_specs` raises `ValueError` when the exact Anthropic count exceeds `RECOMMENDED_MAX`. Earlier behavior was log-only with cl100k as the only hard gate.

### Model capability whitelist
`api_config.model_capabilities(model)` is the single source of truth for adaptive-thinking / extended-output / 1M-context eligibility. Whitelist covers Opus 4.7, Sonnet 4.6, Haiku 4.5. **Unknown model ids degrade to safe defaults that disable every capability flag** — a misconfigured env var produces a smaller request, never an API rejection. Haiku phases (triage) never carry the `thinking` key.

### Verification cache key
`cycle_label | actionType | codeReference | sha256(claim_summary)`. Intentionally omits the verifier model — `VerificationResult.model_used` is stored as provenance inside the entry. Switching `SPEC_CRITIC_VERIFICATION_MODEL` does NOT invalidate existing entries; switching the code cycle does. Claim digest is 24 hex chars; older 16-char entries miss → re-ground → write new 24-char entries (`_CACHE_SCHEMA_VERSION` bump drops the legacy shape).

### Deterministic-rule ids are public
Every preprocessor alert carries a stable `deterministic_rule` id (exposed as `DETERMINISTIC_RULE_*` constants). The verification router's local-skip keyword list recognizes the rule names, so a GRIPES finding mentioning `todo` / `lorem ipsum` / `duplicate paragraph` / etc. is locally skipped. CRITICAL/HIGH and any non-empty `codeReference` still force `web_required`.

### Stale-cycle suppression window
`preprocessor._should_suppress_stale_cycle` scans up to 80 chars on each side for whole-word negation/historical terms (`previously`, `formerly`, `superseded`, `withdrawn`, `obsolete`, `no longer`, `prior`, `historical`, plus auxiliary-verb negations). The window narrows at the nearest sentence terminator. Bare `not` is intentionally not a suppressor. Active stale requirements ("Comply with 2019 CBC") still flag.

### Auto-edit eligibility
`report_status.classify_edit_action` is the single source of truth. `AUTO_EDIT_CANDIDATE` requires:
- supportive status (`VERIFIED_SUPPORTED` / `VERIFIED_CONTRADICTED` / `LOCALLY_CLASSIFIED`), AND
- `edit_confidence >= AUTO_EDIT_CONFIDENCE_FLOOR` (0.7), AND
- not suppressed by cross-check dependency tracking.

`LOCALLY_CLASSIFIED` is supportive because the router decided the finding is self-evident from the spec. Locator/spec_editor preconditions still gate the actual mutation.

### Code cycle: California 2025 only
`DEFAULT_CYCLE = CALIFORNIA_2025`. The 2022-cycle mapping was removed — **do not reintroduce it**. Cycle label is in the verification cache key, so a cycle bump naturally invalidates prior entries.

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

| Mode | When | Model | Thinking | Search budget | Escalates? |
|---|---|---|---|---|---|
| `local_skip` | keyword classifier or Haiku triage said `local_skip` | (none) | n/a | 0 | no |
| `strict_structured` | GRIPES OR non-GRIPES `internal_coordination` profile | Sonnet | off | severity-based | no |
| `standard_reasoning` | default for substantive technical claims | Sonnet | on | severity-based | yes |
| `deep_reasoning` | escalated, OR initial pass for CRITICAL `california_ahj` | Opus | on | severity-based | no (terminal) |

`verification_routing.select_routing` is the unified pure-function selector that returns the full policy bundle; `build_verification_request` builds the kwargs dict used by every verification path (real-time, batch initial, batch retry, batch continuation).

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

| `EditActionLabel` | When |
|---|---|
| `AUTO_EDIT_CANDIDATE` | proposal + supportive status + confidence ≥ 0.7 |
| `MANUAL_EDIT_CANDIDATE` | proposal but status/confidence does not clear the bar |
| `REPORT_ONLY` | no edit proposal |
| `SUPPRESSED` | `suppression_reason` set |

Both labels are *derived* from existing `Finding` fields (`verification`, `suppression_reason`, `edit_proposal`) — no new persistence column.

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
| `SPEC_CRITIC_TABLE_CELL_AUTO_EDIT` | on | Disable to refuse every table-cell auto-edit and route to manual review |
| `SPEC_CRITIC_EDIT_TRANSACTIONAL` | on | Disable to fall back to best-effort writes when any edit fails |
| `SPEC_CRITIC_NORMALIZE_REPLACEMENT_STYLE` | on | Disable to skip per-document typographic normalization of replacement text (quotes / dashes / NBSP) before edits are applied |
| `SPEC_CRITIC_PUNCTUATION_BOUNDARY_FIX` | on | Disable to skip the trailing-`.,;:` boundary repair (drop avoidance / doubling prevention) on EDIT replacements |
| `SPEC_CRITIC_ADD_INHERITS_LIST_NUMBERING` | off | Enable to revert ADD-inserted paragraphs to legacy verbatim deepcopy of the anchor's `<w:pPr>` (keeps `<w:numPr>`, `<w:outlineLvl>`, `<w:pBdr>`, `<w:ind>`) instead of stripping them |
| `SPEC_CRITIC_RESTORE_KNOWN_FORMATTING` | off | Enable to re-apply bold formatting to recognized standards/code references (`NFPA 13`, `ASCE 7-22`, `CBC 2025`, etc.) inside replacement text after a partial EDIT collapses cross-run formatting. Default-off because a wrong match could incorrectly bold content; validate the registry in `src/editing/replacement_style.py:KNOWN_BOLD_PATTERNS` before enabling. Counter: `DiagnosticsReport.known_pattern_formatting_restored_count`. |
| `SPEC_CRITIC_USE_VERIFIER_CORRECTION_AS_REPLACEMENT` | off | Enable to skip the replaceability sanity check on `verification.correction` and use it verbatim as the applied edit's replacement text (legacy behavior). When off (default), the locator falls back to the model's `replacement_text` whenever the correction looks explanatory (parenthetical citations, URLs, paragraph-length expansions, `current`/`latest`/`as of <year>` qualifiers not in the original). |
| `SPEC_CRITIC_VERIFICATION_CACHE_PERSIST` | on | Disable to keep the verification cache in-memory only |
| `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` | `0` (no expiry) | Age-based pruning on cache load; non-integer/negative values fall back to 0 |
| `SPEC_CRITIC_CACHE_PATH` | `~/.spec_critic/verification_cache.json` | Override the on-disk cache file path; `~` and `$VAR` are expanded |

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
