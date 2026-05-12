# Spec Critic Repair Wave — Release Notes

This wave is a correctness/safety pass over the existing review pipeline. No
new product features were added. Every change either tightens a contract,
removes a class of silent failure, or makes a previously implicit behavior
inspectable.

## Validation summary (Chunk 14)

| Check                              | Result |
|------------------------------------|--------|
| `python -m compileall -q src`      | pass (exit 0) |
| Unit test suite (hermetic, no API) | 1302 passed, 0 failed, 0 errors |
| GUI-dependent tests                | auto-skipped (no display server in CI); covered locally on operator workstations |
| Golden-set eval harness            | 10/10 fixtures pass, no baseline drift |
| Stubbed-pipeline dry-run           | smoke + request-shape + fixture round-trip suites pass |
| End-to-end report export           | cost summary, downgraded source-less verdicts, manual-refusal display, and dedup-vs-edit-identity all render as designed |
| DOCX edit safety                   | normal paragraph edits succeed; hyperlink / field-code / drawing / tracked-change / comment / bookmark paragraphs refuse; transactional mode suppresses partial output on failure |
| Live API smoke test                | not run — no real `ANTHROPIC_API_KEY` available in this environment; suite is hermetic by design (`tests/conftest.py` autoskips `@pytest.mark.network`) |

The live smoke test is the only Chunk 14 task that did not run. The plan
explicitly marked it as "if safe and available"; operators with a valid key
can run one review, one verification, and one batch-shape validation
manually to close the loop.

## Fixes shipped in this wave

### Chunk 1 — API model capability and batch retention
- `MODEL_SONNET_46.supports_extended_output_beta` is `True`; the
  `output-300k-2026-03-24` beta header is now selected via the central
  capability registry instead of an Opus-only family check.
- Local batch-state retention dropped from 30 days to 28 days with a
  warning threshold at day 25 so we can no longer outlive the API's
  result-download window.
- `claude-opus-4-7` deliberately left as the default review model (it is a
  valid current Anthropic model ID — the plan explicitly forbade
  "fixing" it to an older dated model).

### Chunk 2 — Structured tool-output terminology + diagnostics payload preservation
- Renamed env var `SPEC_CRITIC_STRUCTURED_OUTPUTS` →
  `SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT`. The old name still works for one
  release (deprecation alias).
- Renamed `structured_outputs_enabled()` →
  `structured_tool_output_enabled()` and updated comments / docstrings to
  state the actual contract: `tool_choice=auto` means the model is
  *expected*, not *required*, to call the custom tool.
- Empty-array tool responses no longer write `"[]"` into the "thinking"
  text channel.
- Added `ReviewResult.structured_payload` and
  `VerificationResult.structured_payload` so diagnostics retain the parsed
  tool input dict (text-only `raw_response` is empty for tool-use
  responses); byte-bounded via `diagnostics.bound_structured_payload`.

### Chunk 3 — Central review request builder + exact token preflight
- One `review_request_builder.build_review_request` produces the kwargs
  used by real-time review, batch review, and token preflight. No
  production path constructs a review request outside this builder.
- Token preflight counts the exact shape that will be sent (system
  prompt, user message, project context, pre-detected alerts, paragraph
  map, tools, cache controls, max tokens, model, thinking config).
- Exact counts cached by a hash of the full request shape so a change in
  any contributor invalidates the cache.
- Reordering input files no longer lets an alert-heavy small-body spec
  bypass the exact-count gate.

### Chunk 4 — Unified verification routing and request construction
- `VerificationRoutingDecision` is the single source of truth for the
  finding-level routing (profile, mode, model, thinking, effort, search
  budget, max continuations, escalation eligibility, cache phase, tool
  set, trace reasons). It serializes to JSON for diagnostics.
- One selector `verification_router.select_routing_decision` produces the
  decision for review-time and batch-time alike.
- One builder `verifier.build_verification_request_from_decision`
  consumes the decision and produces request kwargs.
- Real-time, batch, retry, and continuation requests all flow through
  the same selector+builder. Stamped on every `VerificationResult` so
  every verdict can report the decision that produced it.

### Chunk 5 — Source-grounding invariant for CONFIRMED / CORRECTED
- `_enforce_grounding_invariant` downgrades any externally-verified
  `CONFIRMED` / `CORRECTED` whose `accepted_sources` is empty. The
  explanation gets `(downgraded: no accepted external citation was
  provided)` and `grounded` is reset to `False`.
- `report_status.classify_status` has a belt-and-suspenders accepted-
  citation check on `VERIFIED_SUPPORTED` / `VERIFIED_CONTRADICTED` so a
  future code path that bypasses the verifier wrapper still can't promote
  a source-less verdict.
- Verification cache schema bumped to v2; v1 entries are dropped on first
  load, and `VerificationCache.put` + `load_from_disk` re-validate the
  invariant so stale cached source-less verdicts cannot reappear.
- Local-skip findings are explicitly exempt (no external evidence
  expected).

### Chunk 6 — Centralized retry, continuation, and batch-failure policy
- App-level retries consolidated; typed Anthropic SDK exceptions replace
  string-matching on exception text.
- Default verification continuation cap is 2; deep/critical routing can
  raise the cap via the routing decision.
- Batch verification waves track per-finding failure class (invalid
  request / server error / expired / malformed / parse failure) and
  convert repeated same-class failures to terminal `UNVERIFIED` earlier
  than the global wave cap.
- Invalid-request batch items no longer blindly retry without changing
  the request shape.
- Diagnostics record retry reason, retry count, terminal failure reason,
  and continuation count.

### Chunk 7 — Parse-time edit proposal validation
- `reviewer.validate_edit_shape` is the single validator for `EDIT` /
  `DELETE` / `ADD` shape requirements.
- Invalid proposals are demoted to `REPORT_ONLY` at parse time:
  executable fields cleared, `demotion_reason` stamped, finding
  preserved so the report still surfaces the underlying issue.
- `Finding.as_edit_proposal()` defensively re-runs the validator so
  legacy resume payloads and directly-constructed Findings with invalid
  shapes also fall to `None`.
- `pipeline._deduplicate_findings` carries `demotion_reason` onto merged
  findings so grouped findings can't rehydrate cleared edit fields.
- Report exporter and `edit_candidates.classify_edit_candidates` surface
  the demotion reason so a reviewer sees *why* an apparently-actionable
  finding routed to manual review.

### Chunk 8 — Separate report deduplication from executable edit identity
- `Finding.occurrence_originals` preserves the per-file pre-merge member
  findings whenever `_deduplicate_findings` collapses findings across
  files.
- `FindingOccurrence.original_finding` plus `executable_finding()` /
  `has_original()` helpers bind each occurrence to its per-file original.
- `apply_edits.execute_edit_plan` uses the per-file original's
  `existingText` / `replacementText` / `anchorText` /
  `evidenceElementId` / `edit_proposal` for each affected file;
  legacy / cross-check populated `affected_files` without recorded
  originals route to manual review with an explicit `EditReport.warning`
  rather than guessing with the representative's text.
- Resume state round-trips `occurrence_originals` (recursion bounded at
  one level).

### Chunk 9 — DOCX unsafe-markup refusal + transactional edit safety
- `spec_editor.detect_unsafe_markup` walks paragraph / cell subtrees for
  WordprocessingML constructs that make run-level surgery risky
  (hyperlinks, field characters / instructions / `w:fldSimple`,
  drawings / `w:pict` / `w:object`, comment ranges, tracked
  insertions / deletions / moves, bookmark ranges, `w:sdt` content
  controls, footnote / endnote references, smart tags, custom XML).
- Every mutation site (paragraph replacement, table-cell replacement /
  delete, ADD anchor, whole-paragraph DELETE) refuses *before* mutating
  and surfaces the reason in `EditReport.warnings`.
- `SPEC_CRITIC_TABLE_CELL_AUTO_EDIT=0` refuses every table-cell auto-edit
  regardless of markup.
- Default all-or-none transactional mode validates the mutated document
  reopens as a real `Document`, then suppresses the disk write entirely
  when any individual outcome failed; previously-`applied` outcomes are
  demoted to `skipped` with an explicit "Output suppressed under
  all-or-none policy" detail.
- `SPEC_CRITIC_EDIT_TRANSACTIONAL=0` falls back to legacy best-effort
  writes for operators who need it.

### Chunk 10 — Bounded diagnostics + cost visibility
- `DiagnosticsReport` carries `max_event_data_bytes` (16 KiB per event)
  and `max_total_data_bytes` (8 MiB total). Oversized strings truncate
  with a visible marker; oldest events evict when the global cap is
  breached. Counters: `events_truncated_by_size`, `bytes_dropped`,
  `total_data_bytes`.
- Secret scrubbing: key names matching `api_key` / `password` / `bearer`
  / `client_secret` and values matching `sk-ant-...` / `AKIA...` /
  `Bearer ...` patterns are replaced with `<redacted>` *before* byte-cap
  eviction. `summary()["secrets_redacted"]` counts hits.
- `src/cost_estimator.py` walks diagnostics events through a pricing
  table (Opus 4.x, Sonnet 4.6, Haiku 4.5; rates as of
  `PRICING_AS_OF`), applies the batch-API 50% discount on input/output
  (cache writes/reads and web-search unaffected) and the documented
  Anthropic cache multipliers. Unknown models surface as
  `missing_pricing_calls`.
- Cost surfaces: Word report (between severity table and trust-model
  summary), diagnostics window (per-phase card), and the post-run log
  line all use "Estimated API cost" wording, call out staleness when
  pricing is missing, and reuse the same number.

### Chunk 11 — Cache-aware prompt tightening
- System prompt gained a static `<examples>` block with four reference
  findings (valid `EDIT`, valid `ADD`, `REPORT_ONLY` coordination, and a
  `DO NOT REPORT` negative example). Stored on `_REVIEW_EXAMPLES` so the
  prompt cache prefix stays byte-stable.
- User message gained a short `<final_task>` block after the spec body
  (and after `<pre_detected>` when alerts fire): review only the document
  above, submit findings once, drop findings without concrete evidence,
  ensure edit fields match `actionType`, don't duplicate pre-detected
  alerts. The `evidenceElementId` bullet only emits on the id-rendering
  path so the legacy path stays clean.
- Every prompt-cache breakpoint pinned by prior chunks is preserved
  (Chunk G `TestPromptCacheBreakpointSafety`, Chunk K2 cache-prefix
  test, Chunk D4.1 alert-on/off invariant).

### Chunk 12 — Golden-set eval harness
- `evals/` package with ten fixtures: clean spec, stale code cycle,
  placeholder marker, internal contradiction, coordination, valid edit,
  invalid edit, unsafe DOCX, verification with accepted source,
  verification with source-less CONFIRMED that must downgrade.
- Ten metrics: review recall, false-positive count, duplicate rate,
  parse-failure rate, edit-proposal validity, locator success,
  unsafe-edit refusal, citation acceptance, source-less-confirmed
  survivors, cost-estimate availability.
- `python -m evals.runner` runs hermetically (no API key); compares
  against the checked-in baseline at `evals/baseline.json` and exits
  non-zero on drift.

### Chunk 13 — Small hardening and maintainability cleanup
- Verification cache claim digest widened from 16 hex chars (64 bits)
  to 24 hex chars (96 bits). On-disk JSON format unchanged; legacy keys
  read fine and simply miss against new-form lookups.
- Extraction-cache fingerprint now includes a SHA-256 of the file's
  first + last 64 KiB plus size so a same-size in-place rewrite
  preserving `mtime_ns` invalidates the cache.
- `api_key_store` gained optional OS-keyring support via the `keyring`
  package (degrades to file fallback when no backend); fallback file is
  written with `0o600` permissions on POSIX and existing files have
  their permissions tightened on read.
- `pyproject.toml` ignores `uv.lock`.

## Deferred recommendations

These are promising but explicitly out of scope for the repair wave. The
golden-set eval harness in Chunk 12 makes any of them measurable when
revisited:

- True Anthropic Structured Outputs migration for the review and
  cross-check final responses (design note already lives at
  `docs/design-notes/d9-1-structured-outputs-verification.md`).
- Verification taxonomy simplification driven by eval data — once the
  golden-set baseline is run across real prompts, the four
  `VerificationMode` and five `VerificationProfile` dimensions can be
  pruned for redundancy.
- Project-context persistence (project profiles).
- Two-pass review workflow.
- Deterministic spec graph feeding targeted LLM review on suspicious
  spans only.
- More advanced cross-check dependency-id evaluation (per `REVIEW_FOLLOWUPS.md`).
- Richer interactive accept/reject review UI.
- Corpus-level cross-check caching.
- Category-based web-source reputation system (today's
  `_WEB_SEARCH_BLOCKED_DOMAINS` is a simple blocklist).

## Regression risks to watch

- Source-grounding invariant may downgrade more findings than before.
  The downgraded findings still appear in the report under
  `INSUFFICIENT_EVIDENCE` — they are not hidden.
- Parse-time edit demotion will reduce auto-edit count. Pre/post counts
  are visible in `diagnostics.edit_skip_reasons`.
- DOCX unsafe-markup refusal will surface manual-review items where
  before an auto-edit might have silently corrupted formatting. The
  refusal reason is visible in `EditReport.warnings`.
- Dedup refactor keeps display grouping intact while routing
  cross-file edits to manual review when the per-file original is
  unavailable. Legacy resume payloads (pre-Chunk-8) load with empty
  `occurrence_originals` and fall back to the conservative
  "only edit the representative's own file" path.
- Retry caps will reduce verification completion in pathological cases.
  Terminal-failure reason is recorded so operators can see *why* a
  finding was abandoned.
