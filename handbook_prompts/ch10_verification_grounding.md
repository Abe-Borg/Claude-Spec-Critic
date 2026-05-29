# Agent Prompt — Chapter 10: Verification II — How We Check & Judge

**Full title:** *Verification II: How We Check & Judge (Grounding, Verdicts, Escalation, Cache)*

## Your mission
This is the trust core of the entire program. Ch 9 decided *how* to verify a
finding; you explain how the verifier **executes** that decision and **judges**
the result: the web-search-backed call (real-time and batch waves), the
**grounding invariant** that refuses to call anything CONFIRMED/CORRECTED without
a real retrieved citation, the Sonnet→Opus **escalation** and the **contested**
verdict when two grounded verifiers disagree, the **budget-exhausted** sentinel,
the retry/continuation taxonomy, the real-time fallback, and the persistent
**claim cache**. Write this as the chapter where the book's trust throughline
pays off — and where you are most scrupulously honest about its limits.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (grounding invariant, cache key, modes/budgets),
   §7 (glossary: grounding/verdict/escalation/contested/budget-exhausted).
2. `CLAUDE.md` — these invariants in full: **Grounding invariant**;
   **Verification cache key**; **Cache-replay visibility**; **Web-fetch for
   follow-up reads** (the GA / no-beta-header story — read carefully); **Escalation
   disagreement surfacing**; **Budget-exhaustion sentinel**; **Real-time
   fallback**; and the `VERIFIED_CONTESTED` / `VERIFICATION_FAILED` /
   `INSUFFICIENT_EVIDENCE` rows of §4.
3. Source you own:
   - `src/verification/verifier.py` (~3,175 lines — the core). Key surfaces:
     `VerificationResult`, `_apply_source_grounding`, `_enforce_grounding_invariant`,
     `verify_finding`, `_run_verification_call`, `_classify_wave_results`,
     `_run_batch_escalation_wave`, `collect_verification_batch_results`,
     `_collect_search_evidence_detailed`, `_collect_fetch_evidence_detailed`,
     `_web_search_count` / `_web_fetch_count`, the verdict/stop-reason parsers,
     `_local_skip_result`, the verifier system-prompt builder + pinned-standards
     lines, the escalation outcome helpers.
   - `src/verification/source_grounding.py` — URL normalization, the
     searched/cited/accepted/rejected partition, `searched ∪ fetched` pool.
   - `src/verification/verification_cache.py` — claim-keyed verdict cache,
     `put` (grounding guard, refusal to persist failed/exhausted), TTL pruning,
     `_clone_for_hit`/`_clone_for_store`, schema version, the on-disk atomic
     write.
   - `src/verification/retry_policy.py` — retry / continuation / batch-failure
     taxonomy.
4. `TRUST_AUDIT.md` — the **trust-model caveat** (grounding proves the source is
   *real*, not that it *proves the claim*), P0-5 (batch grounding parity — the
   default path), and the "Grounding gate / URL matching is sound" verified-clean
   note. `STRUCTURAL_AUDIT.md` P1-2 (fallback double-write/drop) and P2-1
   (continuation cap off-by-one).

## In scope (what you own)
- **The verification call.** What the verifier sends (system prompt with cycle
  context + **pinned standards editions** block; the finding; the web_search /
  web_fetch tools) and how it runs in two worlds: **real-time**
  (`_run_verification_call`) and **batch waves** (`_classify_wave_results` +
  continuation/escalation waves). Explain a "wave" as one submit→poll→collect
  cycle, with retries and continuations for incomplete outputs.
- **The grounding invariant — the heart of the chapter.** CONFIRMED/CORRECTED
  require ≥1 **accepted** citation: a model-cited URL whose normalized form
  matched a URL the `web_search` *or* `web_fetch` tool actually retrieved.
  Explain the three enforcement points (`_apply_source_grounding` partitions
  searched/cited/accepted/rejected and downgrades when every cited URL is
  ungrounded; `_enforce_grounding_invariant` is the defensive downgrade;
  `VerificationCache.put` refuses to persist a CONFIRMED/CORRECTED without an
  accepted citation). Stress that `VerificationResult.sources` is the *accepted*
  list — fabricated URLs can't match, so they never reach the report or cache.
  Confirm **batch parity**: the batch wave applies the identical grounding gate
  (Audit P0-5 — this is the default path).
- **web_fetch.** Generally available, **no `anthropic-beta` header** — and the
  cautionary tale: the retired `web-fetch-2026-02-09` header once crashed every
  STANDARD/DEEP verification at submit with HTTP 400. Explain how fetched sources
  join the grounding pool (`searched ∪ fetched`) and are rendered as a distinct
  "full-text sources consulted" set, and the telemetry (`web_fetch_requests`,
  `fetched_sources`).
- **Escalation & contested.** CRITICAL/HIGH UNVERIFIED escalates Sonnet→Opus; the
  snapshot logic that preserves the initial verdict/sources; `models_disagreed`
  (both grounded, different verdicts) → `VERIFIED_CONTESTED`, distinct from a
  mere `escalation_changed_verdict`. Why disagreement is itself the signal and
  the right default is human review (the edit is withheld downstream).
- **Budget-exhausted sentinel.** UNVERIFIED where searches ≥ the mode-scaled
  budget; it's runtime telemetry, **not** a new status (still
  `INSUFFICIENT_EVIDENCE`); the over-budget paths that set it directly; why the
  cache refuses to persist it.
- **Retry / continuation / real-time fallback.** The failure taxonomy
  (`retry_policy.py`): transient operational errors → `VERIFICATION_FAILED`;
  continuation for incomplete outputs; the real-time fallback when the unresolved
  tail drops below the threshold (5).
- **The claim cache.** The key (`cycle_label | actionType | codeReference |
  sha256(claim_summary)[:24]`), why it omits the verifier model, the 60-day TTL,
  the cache-replay age badge data (`cache_entry_created_ts`), atomic disk writes,
  and the guard that drops ungrounded/failed/exhausted results. Telemetry
  round-trips without a schema bump.

## Explicitly OUT of scope (owned elsewhere)
- The *decision* of mode/profile/budget/triage → **Ch 9** (reference it).
- How verdicts map to the nine `ReportStatus` labels and how the evidence panel
  renders → **Ch 11** (you produce the `VerificationResult`; Ch 11 classifies and
  draws it). State the resulting status names, but the classifier + rendering are
  Ch 11.
- Output-cap / cache-policy / model-capability config → **Ch 12**.
- Pinned-edition *data* (the `CodeCycle`) → **Ch 12**; you own the verifier
  prompt's *use* of it (`_pinned_standards_lines`).

## Narrative beats to hit
- *The payoff of the throughline*: this is where "make uncertainty visible"
  becomes machinery. Grounding is the program's immune system against confident
  hallucination.
- *The crucial, honest caveat (Audit TRUST):* grounding proves the cited source
  was **really retrieved**, **not** that the page actually supports the specific
  code claim. A real, retrieved page might not contain the cited provision.
  State this plainly — human spot-checking of VERIFIED_* findings is still
  warranted. This honesty is the chapter's most important sentence.
- *The contested design*: two capable models reading real sources reaching
  different conclusions is a *feature* — it surfaces exactly the findings that
  need human eyes.
- *Cautionary tales*: the stale beta header (web_fetch) and the still-live 300k
  header risk class (cross-ref Ch 6); the fallback handoff question (Audit P1-2 —
  cover the verifier side: an abandoned, never-retrieved batch wave doesn't write
  back, so a tail finding should get exactly one terminal result; present the
  audit's "must be proven" stance honestly).

## Invariants & facts you MUST get right
- Grounding pool is `searched ∪ fetched`; `sources` = accepted, not cited.
- web_fetch is GA and attaches **no** beta header (the empty `extra_headers` seam
  is kept only because the batch API rejects unknown per-item `params` keys).
- `VERIFIED_CONTESTED` requires *both* verifiers grounded *and* different verdicts
  (strictly tighter than escalation-changed-verdict).
- Budget-exhausted is still `INSUFFICIENT_EVIDENCE` (no new status); cache refuses
  to persist it.
- Cache key omits the verifier model; claim digest is 24 hex; default TTL 60 days.
- Cache refuses to persist ungrounded / failed / exhausted results.
- Batch wave applies the *same* grounding gate as real-time.

## Diagrams & tables
- A **grounding decision diagram**: model verdict + cited URLs → partition vs.
  searched∪fetched → accept/reject → downgrade-if-ungrounded → final verdict.
- A **verdict→status sketch** (hand off classification to Ch 11, but show the
  mapping: CONFIRMED+grounded→VERIFIED_SUPPORTED, etc.).
- A **state diagram** of a finding through waves: initial → (retry|continuation)
  → (escalation) → terminal, with the real-time fallback branch.
- A table of the cache's persist-refusal conditions.

## Cross-references to make
- To **Ch 9** (the decision being executed), **Ch 6** (batch/waves + the 300k
  header risk class), **Ch 11** (status classification + evidence panel +
  cache-replay badge rendering), **Ch 12** (pinned-edition data, caps), **Ch 16**
  (the trust caveat, P0-5, P1-2).

## Deliverable
- Write to **`handbook/10_verification_grounding.md`**. H1 = the full title.
  This is a flagship chapter — target **4,500–5,500 words** (the upper end is
  fine here given its centrality).

## Quality bar
- A reader trusts (and knows the limits of) a VERIFIED_* verdict, understands
  grounding as defense-in-depth, and can explain contested and budget-exhausted.
  Every grounding/cache fact matches the source and `CLAUDE.md`. The honest
  caveat is stated unmistakably.
