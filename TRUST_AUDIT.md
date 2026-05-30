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

- **P1-1 — `validate_edit_shape` allows a no-op EDIT.** `src/review/structured_schemas.py` (~:34-80):
  no check that `existingText != replacementText`. A model EDIT with identical text passes validation
  and reaches the sidecar as a no-op instruction. Confirm frequency; consider rejecting/demoting.
- **P1-2 — Batch partial-failure surfacing.** `src/verification/retry_policy.py`, `src/batch/batch_runtime.py`,
  `pipeline.finalize_batch_result`. Verify that when a batch partially fails or is canceled, the
  affected findings are clearly marked (`VERIFICATION_FAILED` / `NOT_CHECKED`) and **never silently
  dropped** from the report/sidecar. Losing findings on a batch hiccup would be a trust failure.
- **P1-3 — Cross-checker chunking.** `src/cross_check/cross_checker.py` (chunked by CSI division).
  Verify findings can't be dropped or mis-attributed across chunk boundaries, and that a coordination
  issue spanning two divisions in different chunks is still detected (or that the limitation is known).
- **P1-4 — Review/verifier system-prompt content (domain review, separate workstream).** `src/review/prompts.py`
  and the verifier prompt. The *correctness of findings* depends heavily on prompt instructions
  (code categories, pinned editions, severity rubric). This needs a **mechanical/plumbing code
  domain expert**, not just code-logic review — flag as its own track. Also re-confirm the pinned
  edition strings in `core/code_cycles.py` against the published California 2025 adoption matrix
  (CLAUDE.md warns these are hand-maintained).

---

## P2 — Minor / completeness / hardening (note, low urgency)

- **P2-1 — ASCE 7 pre-2005 stale editions missed.** `preprocessor.py:386-396`,
  `_ASCE7_PLAUSIBLE_EDITIONS = {"05","10","16","22"}`. Genuinely old editions (7-93/95/98/99/02) are
  `not in` the plausible set → skipped → **not flagged**. Deterministic-layer completeness gap only
  (the LLM review likely still catches it); no wrong findings produced. Low.
- **P2-2 — `safe_local_estimate` not clamped ≥ 1.0.** `tokenizer.py:126-128`. Defaults are all ≥1.10,
  so fine as configured; a future sub-1.0 misconfig would turn the safety pad into a danger pad. Pure
  hardening; also note exact API counts (`pipeline.py:531`) are the authoritative gate, so impact is small.
- **P2-3 — `assert_extended_output_allowed` compares to `MAX_OUTPUT_TOKENS_OPUS` regardless of model**
  (`api_config.py:183`). Now that Sonnet 4.6 also has the 300k beta, glance at whether the threshold
  constant should be model-derived. Likely benign.

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
