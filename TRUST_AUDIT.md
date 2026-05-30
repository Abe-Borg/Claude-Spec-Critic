# Spec Critic — Trust & Correctness Audit (investigation plan)

## Context

The user needs to **trust** that Spec Critic's findings and suggested edits/additions/deletions
are correct, and wants latent logic errors surfaced. This is an *audit / triage* deliverable, not
an implementation: the job is to hand a team of agents a prioritized list of **things to dig into**,
with enough location + reasoning that each can be confirmed and (where real) fixed. No fixes here.

I ran three parallel code sweeps and then **personally re-read the trust-critical paths** to
separate real issues from noise (two sub-agent "CRITICAL"s were false alarms — see §Verified-clean).

**Overall posture:** the verification/grounding core is genuinely well-built (defense-in-depth, see
§Verified-clean). The higher-risk surface is the *edit-emission pipeline* (what reaches the
machine-readable sidecar) and *input completeness / model configuration* — i.e., the places where a
correct internal verdict can still produce an incomplete or under-applied external instruction.

> **Trust-model caveat to communicate to the user (not a bug):** a `VERIFIED_SUPPORTED` /
> `CONFIRMED` verdict only guarantees the cited URL was *actually retrieved by the search tool*
> (`source_grounding.py`), **not** that the page's content demonstrably supports the specific code
> claim. The model could cite a real, retrieved page that doesn't actually contain the cited
> provision. Automated grounding proves *the source is real*, not *the source proves the claim*.
> Human spot-checking of `VERIFIED_*` findings is still warranted.

---

## P0 — Highest priority (directly affects correctness/completeness of emitted edits, or trust in input)

### P0-1 — Multi-file findings emit only ONE edit instruction (sidecar under-emission)  ⟵ strongest finding
- **✅ RESOLVED** (commit `144d13a`): `edit_sidecar.build_edit_instructions` now wires `group_findings()` + `executable_finding()` to emit one sidecar entry **per affected file** (schema v3), each with its own `fileName` / `evidenceElementId` / `edit_proposal`; `group_findings()` is no longer test-only. See CLAUDE.md "FindingGroup vs FindingOccurrence" / "Edit instructions are emitted, not applied".
- **Where:** `src/output/edit_sidecar.py:43-63` (`_finding_entry`), `src/orchestration/pipeline.py:349-422` (`_deduplicate_findings`, merge sets `fileName=files[0]` at :400), `group_findings()`/`FindingOccurrence` at `pipeline.py:260-301`.
- **Observed (confirmed):** When dedup collapses the same defect across N specs (common for templated
  DSA master specs), the merged Finding carries `affected_files=[a,b,c]` and `occurrence_originals`.
  But the sidecar emits **one entry** with `fileName=a` only, and **does not include `affected_files` at
  all** in the entry. The helper built to expand this — `group_findings()` / `occurrence_originals` /
  `FindingOccurrence.executable_finding()` — is **called only from tests, never in production**
  (`grep` confirms: only `tests/test_dedup_edit_identity.py`). CLAUDE.md explicitly says these fields
  exist "so per-file ... differences survive the merge **for the report and the edit-instruction sidecar**"
  — that intent is **not realized in code**.
- **Why it matters:** A downstream applier reading the sidecar applies the fix to file `a` only;
  the identical defect in `b` and `c` gets **no machine-readable edit instruction** (only the
  human-readable `issue` string says "found in 3 specs: …"). For a tool whose whole purpose is
  automated edit application, this is silent under-application.
- **Dig into:** Should the sidecar emit one entry **per affected file** (via `group_findings()` +
  `executable_finding()`), or at minimum include `affected_files` in each entry? Confirm whether the
  downstream applier contract expects per-file fan-out. Decide whether `group_findings()` should be
  wired into `edit_sidecar.py`/`report_exporter.py` or deleted as dead code.

### P0-2 — Per-file `anchorText` / `evidenceElementId` collapse to representative on merge (subset of P0-1, but distinct correctness risk)
- **✅ RESOLVED** (commit `144d13a`, same per-occurrence fan-out as P0-1): per-file `anchorText` / `insertPosition` / `evidenceElementId` now survive the merge via `occurrence_originals` and reach the sidecar per affected file (`has_per_file_original` flags borrowed locators).
- **Where:** `pipeline.py:_dedup_key` (:319-327) vs the merge block (:398-421).
- **Observed:** The dedup key includes `existingText`/`replacementText` digests, so merged members
  share **identical edit text** (good — no wrong-text risk). BUT `anchorText`, `insertPosition`, and
  `evidenceElementId` are **not** in the key, so members can differ on them; the merge keeps only the
  **representative's** values (`:409-411`). For `ADD` actions, `anchorText` is the locator.
- **Why it matters:** A multi-file `ADD`/anchored edit may carry the representative file's anchor,
  which can mislocate the insertion in the other files. `occurrence_originals` preserves the correct
  per-file values but (per P0-1) nothing downstream reads them.
- **Dig into:** Confirm whether anchored/ADD edits actually diverge per file in practice; if so this
  is fixed by the same per-occurrence fan-out as P0-1.

### P0-3 — Model-capability whitelist goes stale → silently degrades a *newer/better* model
- **✅ RESOLVED** (branch `claude/nice-gauss-TtDyR`): of the three "dig into" questions — (2) `claude-opus-4-8` added to both `_MODEL_CAPABILITIES` and `OPUS_MODELS` with docs-confirmed flags (adaptive thinking, 128k output, `output-300k-2026-03-24` batch beta, 1M context, `effort`), so selecting it no longer degrades; (3) unknown ids now emit one deduped `WARNING` from `model_capabilities` naming the conservative fallback caps, so the whitelist going stale is no longer silent. (1) Default models left at Opus 4.7 — confirmed still valid/available against Anthropic's current model list, so no availability problem; the 4.7→4.8 *default* bump is a deployment/cost decision deferred to the user (env var `SPEC_CRITIC_*_MODEL` now selects 4.8 cleanly). Regression coverage: `tests/test_capability_policy.py` (`TestOpus48Whitelisted`, `TestUnknownModelWarnsLoudly`).
- **Where:** `src/core/api_config.py:225-274` (`_MODEL_CAPABILITIES`, `_DEFAULT_CAPABILITIES`, `model_capabilities`). Whitelist = `opus-4-7`, `sonnet-4-6`, `haiku-4-5` only.
- **Observed (confirmed):** Any unknown id (including `claude-opus-4-8`, the current model — this very
  session runs on it) falls to `_DEFAULT_CAPABILITIES`, which sets `supports_adaptive_thinking=False`,
  `supports_extended_output_beta=False`, `supports_effort=False`, `context_window=200_000`.
- **Why it matters:** An operator who sets `SPEC_CRITIC_REVIEW_MODEL=claude-opus-4-8` to get *better*
  reviews instead gets **no extended thinking, output capped at 128k instead of 300k, no effort tuning**
  — silently worse reviews, with no error. The "safe default" protects against API rejection but
  trades it for quiet quality loss. The default model itself (`opus-4-7`) should be checked for
  current availability.
- **Dig into:** Is `opus-4-7` still the right default? Add `opus-4-8` (and successors) to the whitelist
  with correct flags? Should an unknown id at least **warn loudly** rather than degrade silently?

### P0-4 — Hardcoded `output-300k-2026-03-24` beta header is the same stale-header risk class as the bug that already bit this codebase
- **✅ RESOLVED** (branch `claude/graceful-300k-fallback`): the review batch is the **only** 300k-beta call site (verification batches carry no beta), and it now **degrades gracefully instead of crashing**. `submit_review_batch` routes the create through a new `batch._create_review_batch`, which attempts the beta path and, on a beta-header rejection, clamps the extended requests back to the model ceiling (`output_cap_for_model` → 128k for Opus) and re-submits on the **non-beta** path — output may truncate on very large specs (already surfaced by the review-stage failure reporting), strictly better than a hard HTTP 400 at submit. Detection is precise: `_is_beta_header_rejection` matches only an HTTP 400 whose message names the `anthropic-beta` header, so unrelated 400s/5xx **propagate unmasked** (no silent swallowing). `assert_extended_output_allowed` is retained — it still fail-fasts the *local* programmer error of requesting 300k without attaching the header, a complementary guard to the new *API-rejection* fallback. On the "dig into" questions: extended output **is** exercised (any spec whose exact Anthropic input count ≥ `LARGE_REVIEW_INPUT_THRESHOLD`=200k); I could not confirm `output-300k-2026-03-24` against the live API from here, which is exactly why the fallback (not a re-pinned literal) is the right fix — a retired/renamed header now self-heals at runtime and logs a `WARNING` naming `BATCH_OUTPUT_BETA` so the operator can update the pin. Regression coverage: `tests/test_batch_beta_fallback.py` (detection precision, clamp, fallback-vs-propagate, and an end-to-end `submit_review_batch` survives-rejection wiring test).
- **Where:** `api_config.py:63` (`BATCH_OUTPUT_BETA`), `:176-190` (`assert_extended_output_allowed`).
- **Observed:** CLAUDE.md documents a prior incident where a retired `web-fetch-2026-02-09` beta header
  caused HTTP 400 and crashed every run on the common path. The 300k header is hardcoded the same way.
  `assert_extended_output_allowed` only checks the header is **present**, not that the API still
  **accepts** it.
- **Why it matters:** If this beta value is retired/renamed, every large-input (≥200k-token) batch
  review crashes at submit — the exact failure mode from the prior incident, just on a less-common path.
- **Dig into:** Confirm `output-300k-2026-03-24` is still valid against the current API. Decide whether
  extended output is even being exercised, and whether there should be a graceful fallback (drop to
  128k) rather than a hard failure if the header is rejected.

### P0-5 — Batch-wave verification: confirm grounding parity with real-time (this is the DEFAULT path)
- **✅ RESOLVED** (branch `claude/gifted-bardeen-kpA61`): read `_classify_wave_results` end-to-end — **parity holds**. The batch wave parser stamps `grounded`/sources from the same `_collect_search_evidence_detailed` + `_collect_fetch_evidence_detailed` evidence the real-time path uses, then runs the **identical** chain `_apply_source_grounding(parsed, searched=deduped_searched, fetched=deduped_fetched)` → `_enforce_grounding_invariant(parsed)` (`verifier.py:2470-2475`), so a CONFIRMED/CORRECTED citing a URL absent from the searched∪fetched pool is downgraded to UNVERIFIED on the common path exactly as on real-time. The real gap was *test coverage*: the pre-existing `TestBatchAndRealtimePathParity` exercised the two grounding helpers **in isolation**, so a refactor that dropped the calls from `_classify_wave_results` would not have failed it. New regression coverage in `tests/test_batch_wave_grounding.py` drives fake batch verdicts through the **real** `_classify_wave_results` (searched URL ≠ cited URL) and asserts: grounded verified verdict survives, ungrounded/invented citation downgrades, no-citation-with-search downgrades via the invariant, and a mixed list keeps only the grounded URL. A mutation check (deleting the two grounding lines) fails all 7 tests, confirming the guard has teeth. Continuation/retry sub-paths re-enter `_classify_wave_results` per wave and inherit the same gate.
- **Where:** `src/verification/verifier.py` batch wave `_classify_wave_results` (~:2270-2490) vs the
  real-time gate I confirmed (`_apply_source_grounding` :380-467, `_enforce_grounding_invariant` :312-377).
- **Observed:** The real-time grounding gate is solid (read and confirmed). A sub-agent asserts the
  batch path "mirrors" it, but I did **not** read the batch path myself, and **batch is the default,
  highest-volume route** for both review and verification.
- **Why it matters:** If the batch wave stamps `grounded`/verdicts without running the same
  `_apply_source_grounding` + `_enforce_grounding_invariant`, a `CONFIRMED` could reach the report
  ungrounded on the common path. This is the single most important thing to *verify* (likely fine, but
  must be proven given trust requirements).
- **Dig into:** Read `_classify_wave_results` end-to-end; confirm every CONFIRMED/CORRECTED there passes
  the identical grounding partition + invariant as real-time, including the continuation/retry sub-paths.

### P0-6 — Extraction completeness: is any spec text silently NOT reviewed?
- **✅ RESOLVED (text boxes + footnotes/endnotes)** (branch `claude/bold-faraday-itPv3`): empirically confirmed the gap (an in-memory `.docx` with a DrawingML text box, a VML text box, a footnote, and an endnote round-tripped through `extract_text_from_docx` captured **none** of them — only the body). Fixed by extracting all three as labeled blocks after the body: `_collect_textbox_mappings` pulls every `<w:txbxContent>` (DrawingML `wps:txbx` **and** legacy VML `v:textbox`) from the body via descendant search; `_collect_note_mappings` locates the `word/footnotes.xml` / `word/endnotes.xml` package parts **by content type** (relationship ids aren't stable), parses them defensively, and skips the structural `separator`/`continuationSeparator` notes by `w:type`. Each kind renders as its own block (`===== TEXT BOX CONTENT =====` / `FOOTNOTE` / `ENDNOTE`) through the shared `_append_supplemental_block`, which the existing header/footer block was refactored onto so all four supplemental sources share one lockstep `paragraphs`/`paragraph_map` append. The reconstruction invariant is preserved (verified by `extract_text_from_docx` itself, which raises on mismatch) and new stable element ids are minted (`tb<box>p<para>`, `fn<id>p<para>`, `en<id>p<para>`, with `meta:tb`/`meta:fn`/`meta:en` delimiters). A spec with none of these is byte-identical to before (every collector no-ops on absence). **Headers/footers were already extracted** (lines 238-280 pre-change), so that sub-worry was already closed. Regression coverage: `tests/test_extraction_supplemental_content.py` (17 tests: DrawingML/VML capture, text box in a table cell, mixed text+box no-dup, multi-box ordering/ids, footnote/endnote capture + separator exclusion + ids, absent/empty/malformed parts graceful, reconstruction invariant, unique ids, block order, word-count). A mutation stubbing both collectors fails 11/17. **Remaining (deliberately out of scope, lower frequency):** SmartArt / grouped-shape text (`a:t` runs in `wpg:grpSp` / diagram parts), text boxes anchored *inside* headers/footers (the header/footer walk reads `container.paragraphs.text`, which doesn't descend into a `txbxContent`), and tables nested inside a text box or note (only direct-child `<w:p>` are read). The content-loss warning is intentionally unchanged: a now-extracted text box still counts toward the drawing proportion, which over-warns slightly in the safe ("verify visually") direction.
- **Where:** `src/input/extractor.py:197+` (`extract_text_from_docx` iterates `doc.element.body` children
  for `}p` and tables only). Content-loss warning (`_detect_content_loss_warning` :119-173) covers
  drawing/picture/OLE-heavy specs but **not** text-bearing parts outside the body.
- **Observed/worry:** python-docx body iteration typically misses **headers/footers**, **text boxes**
  (`w:txbxContent` inside `w:drawing`), **footnotes/endnotes**, and SmartArt/grouped-shape text. DSA
  specs sometimes put requirements or revision notes in headers/footers or text boxes.
- **Why it matters:** If requirement text lives in an unextracted part, the model never sees it →
  findings are based on incomplete input, and a real defect there is silently un-flagged. Trust gap in
  the "don't miss real problems" direction.
- **Dig into:** Build a fixture .docx with text in a header, a text box, and a footnote; confirm whether
  each is captured. If not, decide whether to extract them or at least extend the content-loss warning
  to flag their presence.

---

## P1 — Correctness, lower frequency or impact (confirm, fix if real)

- **P1-1 — `validate_edit_shape` allows a no-op EDIT.** ✅ **RESOLVED** (branch `claude/reject-noop-edit-shape`).
  Note: the helper actually lives in `src/review/reviewer.py` (`validate_edit_shape`), not
  `structured_schemas.py` — the audit's path was stale. It now returns a demotion reason for an EDIT whose
  `existingText` is byte-for-byte identical to `replacementText`, so the finding demotes to REPORT_ONLY and
  no no-op instruction reaches the report or the sidecar. Both the parse-time path (`_parse_findings`) and
  the defensive `Finding.as_edit_proposal()` accessor route through the helper, so parser output and
  directly-constructed/legacy findings are both covered. Exact-equality only (case/whitespace-only deltas are
  legitimate fixes and pass). Coverage in `tests/test_parse_time_edit_validation.py` (unit + parse-time +
  `as_edit_proposal` paths).
- **P1-2 — Batch partial-failure surfacing.** ✅ **RESOLVED (verified — NO drop gap; locked in with tests)** (branch `claude/bold-faraday-itPv3`). Traced the verification-batch path end-to-end: (1) `collect_verification_batch_results` ends **every** finding with exactly one `VerificationResult` — the post-loop safety net (`verifier.py:3174-3176`) stamps a terminal UNVERIFIED on any finding still at `verification is None` after the wave loop + escalation wave (this catches detached/timed-out batches via the `break`), and the exactly-once handoff is already locked in by `tests/test_batch_fallback_handoff.py`; (2) a *canceled* batch item is non-retryable — `_classify_wave_results` (`verifier.py:2298-2341`) routes `result.type=="canceled"` → `classify_batch_failure` → `BATCH_CANCELED` (in `_BATCH_NEVER_RETRY`) → `terminal_unverified`, which the wave loop turns into `verification_failed=True` → `VERIFICATION_FAILED`; a *missing* item is a transient `SERVER_ERROR` retry (re-submitted, never dropped); (3) `finalize_batch_result` (`pipeline.py:1142-1146`) concatenates review + cross-check findings with **no** verification-status filter, so a failed/unchecked finding rides into `PipelineResult` and the edit sidecar (`build_edit_instructions` gates emission on the edit *proposal*, not the verdict). The coverage gap (not a code gap) was the *end-to-end* report/sidecar guarantee and the canceled-item classification; both are now regression-tested in `tests/test_batch_partial_failure_surfacing.py` (canceled→terminal, missing→retry, and a mixed verified/failed/unchecked batch surviving `finalize` + sidecar with correct `report_status` each). Mutations (finalize drops failed findings; canceled mis-routed) fail the tests.
- **P1-3 — Cross-checker chunking.** ✅ **RESOLVED (no drop/mis-attribution; cross-division limitation now documented)** (branch `claude/bold-faraday-itPv3`). Read `cross_checker.py` end-to-end. **No finding is dropped or mis-attributed:** `_assign_chunk` always returns a chunk (an unparseable/unmatched CSI prefix routes to `"general"`, never dropped), `_group_specs_by_chunk` pools singleton-division chunks into `"general"` so the union of all chunk specs equals the input (no drop, no dup), and `_label_finding_with_chunk` keeps each finding's own division label in `section`. Partial chunk failure preserves the other chunks' findings (`_synthesize_chunk_findings` keeps every completed chunk; combined status is `completed` when ≥1 chunk completed, `failed`/`skipped` only when zero completed; the completed/failed/skipped tally rides in the summary). **The cross-division limitation is real and was undocumented:** each chunk is cross-checked in isolation, so a coordination conflict spanning two *different* divisions in *different* chunks is **not detectable once chunking is active** (a chunked run is a within-discipline pass). This is an intentional tractability trade-off (alternative was the prior all-or-nothing `skipped`); it is now made *known* at three levels — the `run_chunked_cross_check` docstring, an operator-facing note appended to the chunking log line, and a CLAUDE.md invariant ("Cross-check chunking: within-discipline only when chunked"). Note: chunking only triggers above `CROSS_CHECK_RECOMMENDED_MAX`; small projects take the single un-chunked path and have no blind spot. Regression coverage: `tests/test_cross_check_chunking.py` (completeness/no-drop, singleton→general, div-22/23 separate chunks, the "no single call sees two divisions" limitation lock-in, partial-failure preservation + no mis-attribution, status matrix). Mutations (failed chunk wipes findings; chunk isolation broken) fail the tests. **Follow-up landed (same branch):** a *partial* chunk failure (some chunks complete, one fails/skips) previously reported overall status `completed` and showed a clean green "Cross-spec coordination" banner row, hiding that a division's coordination did not run. `ReviewResult` now carries `chunk_failures` / `chunk_skips` (set in `run_chunked_cross_check`); `_summarize_run_diagnostics` surfaces them and the Run Diagnostics banner red-flags the row with "— N chunk(s) not analyzed" when > 0. Coverage in `tests/test_diagnostic_banner.py` (data plane + red-shading render) and `tests/test_cross_check_chunking.py` (counts on the combined result); mutation (never flag partial failure) fails the render test.
- **P1-4 — Review/verifier system-prompt content (domain review, separate workstream).** `src/review/prompts.py`
  and the verifier prompt. The *correctness of findings* depends heavily on prompt instructions
  (code categories, pinned editions, severity rubric). This needs a **mechanical/plumbing code
  domain expert**, not just code-logic review — flag as its own track. Also re-confirm the pinned
  edition strings in `core/code_cycles.py` against the published California 2025 adoption matrix
  (CLAUDE.md warns these are hand-maintained).

---

## P2 — Minor / completeness / hardening (note, low urgency)

- **P2-1 — ASCE 7 pre-2005 stale editions missed.** ✅ **RESOLVED** (commit `115b9ea`, PR #240, branch
  `claude/asce7-stale-editions`). `_ASCE7_PLAUSIBLE_EDITIONS` now spans `{"88","93","95","98","02","05",
  "10","16","22"}`, so the genuinely-old editions that were previously `not in` the set (and thus skipped)
  now flag via `DETERMINISTIC_RULE_STALE_ASCE7`. Coverage in `tests/test_asce7_stale_editions.py` (22 tests).
  (The ✅ marker was missing from this doc even though the fix had merged — corrected here.)
- **P2-2 — `safe_local_estimate` not clamped ≥ 1.0.** ✅ **RESOLVED** (branch `claude/clamp-safety-factor`).
  `local_estimate_safety_factor` now returns `max(1.0, factor)`, so the docstring's "≥ 1.0" contract is
  *enforced* rather than merely assumed — a sub-1.0 entry slipping into `_LOCAL_SAFETY_FACTORS` (or a
  sub-1.0 `_DEFAULT_LOCAL_SAFETY_FACTOR`) can no longer shrink the estimate below the raw local count and
  turn the safety pad into a danger pad. Clamping at the source covers every caller, not just
  `safe_local_estimate`. Regression coverage in `tests/test_token_budgets.py` (`TestLocalEstimateSafetyFactor`):
  an injected 0.5 registry factor and a 0.9 default both clamp to 1.0 and never undercount; a mutation
  removing the clamp fails both. (Exact API counts remain the authoritative gate, so this was always
  low-impact — pure hardening of the fallback path.)
- **P2-3 — `assert_extended_output_allowed` compares to `MAX_OUTPUT_TOKENS_OPUS` regardless of model.**
  ✅ **RESOLVED** (branch `claude/bold-faraday-itPv3`). The guard's threshold is now the *selected
  model's* baseline output ceiling, derived from the single `output_cap_for_model` source of truth
  (Opus 128k, Sonnet/Haiku 64k) via a new optional `model=` arg, which `submit_review_batch` now
  passes. This makes the fail-fast guard correct for Sonnet — a 64k–128k Sonnet request without the
  beta (which the API would reject) is now caught at the call site instead of slipping past the old
  hardcoded 128k threshold. When `model` is omitted the guard falls back to the 128k Opus ceiling so
  it never *over*-fires on a legitimate sub-ceiling request (the API stays the backstop). Confirmed
  benign in practice (current call sites request either ≤ model-ceiling or exactly 300k), so this is
  hardening, not a live-bug fix. Coverage: `tests/test_batch_beta_fallback.py::TestAssertExtendedOutputAllowed`
  (Opus unchanged, the Sonnet 64k–128k gap now raises, omitted-model fallback, 300k always caught).

---

## Verified-clean — DO NOT spend time here (already checked by me)

- **Content-loss threshold is NOT an off-by-one.** `extractor.py:165` `if proportion <= threshold: return None`
  correctly implements the documented "warn when proportion > 0.20 (strict `>`)". A sub-agent flagged
  this as a CRITICAL bug by inverting the polarity — it is correct as designed.
- **Stale-cycle suppression "missing break" is fine.** `preprocessor.py:303-306` (pre-window loop) is
  functionally equivalent to "trim to after the rightmost terminator" because it reassigns the window
  per term; the post-window loop correctly uses first-occurrence + break. No bug.
- **Dedup will not falsely merge distinct edits.** The dangerous direction is well-guarded: the key
  includes full-text SHA-256 digests of `existingText` and `replacementText` (`pipeline.py:316,325-326`),
  so two findings only merge when their edit text is byte-identical. (The real issue is *under-emission*
  on merge — P0-1 — not wrong-text merging.)
- **Grounding gate / URL matching is sound.** `source_grounding.py` (conservative normalization, exact
  match, fabricated URLs can't match) + `_apply_source_grounding` + `_enforce_grounding_invariant`
  (`verifier.py:312-467`) + the independent re-check in `classify_status` (`report_status.py:206-213`)
  form real defense-in-depth.
- **`classify_status` branch order is correct.** `models_disagreed → VERIFIED_CONTESTED` is evaluated
  before the verdict branches (`report_status.py:200-201`), as documented.

---

## Verification approach (for the team executing this)

1. **P0-1 / P0-2 (sidecar fan-out):** Write a test with two in-memory fixture specs sharing one
   identical defect (and one with a per-file divergent `anchorText`). Run the pipeline; assert the
   sidecar contains an actionable instruction for **every** affected file with the correct per-file
   locator. Use `tests/fixtures/docx_fixtures.py`; mirror `tests/test_dedup_edit_identity.py`.
2. **P0-3 / P0-4 (model + header):** Grep all model-id and `anthropic-beta` literals; cross-check each
   against the current Anthropic model list / beta list. Add a regression test that an unknown model id
   either warns or is explicitly handled, and that the 300k path degrades gracefully if the header is
   rejected.
3. **P0-5 (batch grounding parity):** Read `_classify_wave_results`; add a hermetic test using
   `tests/fixtures/fake_anthropic.py` that feeds a batch CONFIRMED with an **ungrounded** citation and
   asserts it is downgraded to UNVERIFIED — identical to the existing real-time test.
4. **P0-6 (extraction):** Fixture .docx with header/footer/text-box/footnote text; assert presence (or
   a content-loss-style warning) in `ExtractedSpec`.
5. **P1-2 / P1-3:** Hermetic tests simulating a partial batch failure and a cross-division coordination
   issue split across chunks; assert no finding is silently lost.
6. Run the full hermetic suite (`pytest`, no key/network needed per CLAUDE.md §9) after each change.

**Suggested sequencing:** P0-5 and P0-6 are *verification-first* (confirm whether a gap exists before
writing code). P0-1/P0-2 are the most likely to need real code changes. P0-3/P0-4 are quick config/policy
checks with outsized payoff. Dispatch as independent workstreams; they touch disjoint modules.
