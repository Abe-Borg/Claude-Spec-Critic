# D9.3 — Server-Side Compaction for Huge Cross-Check Runs

**Type:** Research only — no production code changes.
**Status:** Recommendation below.
**Scope:** Whether Spec Critic's cross-check phase should adopt Anthropic's beta server-side compaction (`compact-2026-01-12`) for projects whose combined specs approach or exceed `CROSS_CHECK_RECOMMENDED_MAX` (~822k tokens).
**Prepared:** Phase D9 (Wave 4).

---

## 1. What we have today

Cross-check (`src/cross_checker.py`) compares all of a project's specs against each other for coordination issues that no per-spec review can see. The phase is single-turn by construction — `run_cross_check` makes one `messages.stream` call with the entire corpus rendered as `<spec>` blocks plus the review findings as `<prior>` blocks.

Size handling today:

1. `count_tokens(system_prompt) + count_tokens(user_message)` is compared against `CROSS_CHECK_RECOMMENDED_MAX` (currently 822,000 tokens, see `src/tokenizer.py`).
2. If the total fits, run cross-check on the full corpus in one call.
3. If not, `run_chunked_cross_check` falls back to per-CSI-division chunks (Div 21 / 22 / 23 / Controls + Commissioning / general) plus a Haiku 4.5 "cross-discipline synthesis" pass over the chunk-level findings (`_run_cross_discipline_synthesis`).
4. If the corpus has fewer than two viable chunks, the phase returns `cross_check_status="skipped"`.

The cross-check finding shape (`_CROSS_CHECK_FINDING_OBJECT_SCHEMA` in `src/structured_schemas.py`) requires the model to cite `upstreamFindingIds` (review-finding ids) and `independentEvidenceIds` (raw-spec element ids — `<para id="pN">` / `<row id="tNrM">` / `<heading id="pX">`). The downstream gate (`pipeline.classify_cross_check_dependencies`) suppresses cross-check findings only when every cited upstream is DISPUTED *and* there is no independent spec evidence. That gate depends on exact element ids surviving into the model's reasoning context — not on summaries.

---

## 2. What compaction does, per the latest docs

Beta feature behind the header `compact-2026-01-12`. Supported on Claude Opus 4.7, Opus 4.6, Sonnet 4.6 (per the docs fetched 2026-05-11 from `platform.claude.com/docs/en/build-with-claude/compaction.md`).

When the input context exceeds a configurable threshold (default 150k tokens, minimum 50k), the API:

1. Synthesizes a `compaction` content block summarizing earlier message blocks.
2. Drops all message blocks before the compaction block on subsequent requests.
3. Bills the summarization itself as a separate `iterations` entry in `usage` (so the cost surfaces in the response).
4. Requires the caller to *preserve* the compaction block on every subsequent turn — append `response.content`, not just the text.

Compaction works on streaming and on prompt caching. It is compatible with server tools. The docs do not explicitly call out batch API compatibility.

---

## 3. Question 1 — Is compaction compatible with the cross-check request shape?

**Mechanically yes, semantically no — for the place we'd want it most.**

Cross-check is single-turn. Compaction triggers at the start of each sampling iteration when the *current* input exceeds the threshold. So a 200k-token single-turn cross-check call *could* trigger compaction once before the model starts generating.

But that is exactly the wrong mode of operation for this phase:

- Cross-check is asking the model to compare **exact wording** across specs ("does CMC 220715 reference the same gauge as CMC 232113 in the duct schedule?"). Summarizing the input *is* what we are trying to avoid.
- Compaction summarizes everything before the compaction block. If the threshold trips in the middle of the spec corpus, half the specs become a summary and the other half stay verbatim. The asymmetry is worse than uniform chunking — the model gets a fuller view of the late-loaded specs and a thin view of the early ones.
- The `independentEvidenceIds` contract requires the model to cite raw `<para id="pN">` element ids. Summarized text by definition does not retain those ids. A cross-check finding that depends on an element id from a summarized region cannot be validated by `classify_cross_check_dependencies`.

For the chunked-cross-check fallback (each chunk is run as its own call), compaction *could* fire if a single chunk exceeded the threshold. Today's `_group_specs_by_chunk` already keeps Division 22 alone, Division 23 alone, etc. A single division-22 chunk on a 100-section mega-project could approach 150k tokens. Compaction would summarize the early specs in the chunk and keep the rest verbatim — same asymmetry as above.

---

## 4. Question 2 — Does compaction preserve enough exact evidence for spec coordination?

**No — and this is the load-bearing finding for this design note.**

Cross-check findings are valid only when the model can quote (or cite ids for) the exact text that conflicts. The Chunk K2 + Chunk M architecture *intentionally* ties cross-check findings to stable element ids so the report can show "section A says X, section B says Y." If the model writes a finding like:

> Coordination conflict: HVAC schedule references galvanized duct in `t0r3` but plumbing schedule references stainless in `p47`.

…and the post-cross-check verifier or human reviewer goes to look at `p47`, they need the original text to validate the claim. Once `p47` is inside a compacted summary, the element id is gone and the claim is unverifiable.

Empirically, the failure mode of summarization on dense technical text is well-known: it preserves narrative gist and discards numeric and identifier specifics. That is the exact information cross-check needs.

---

## 5. Question 3 — Does compaction help single-turn large-document review, or mainly multi-turn agents?

The compaction docs are written around multi-turn conversations:

> When the conversation gets too long, the API automatically summarizes early turns.

The docs do acknowledge single-turn use as well: compaction triggers whenever the *input* exceeds the threshold, regardless of conversation structure. So a 250k-token cross-check call would trigger compaction once before model generation begins.

But "single-turn very long input" is exactly the failure mode for cross-check: there is no prior context to discard, only spec content to compare. Compaction in that mode collapses early specs into a summary, which is just a worse version of chunking.

**Compaction is built for multi-turn agentic loops where early turns become irrelevant.** Cross-check has no irrelevant earlier turns — every spec is potentially relevant to every other spec.

---

## 6. Question 4 — Would it replace chunking or merely supplement it?

Neither. The chunked-cross-check fallback today exists because:

1. The full corpus exceeds `CROSS_CHECK_RECOMMENDED_MAX` (822k tokens).
2. Per-CSI-division chunks are *meaningful* — division 22 specs are more likely to conflict with each other than with division 25 specs, so the chunked pass still catches most within-discipline issues.
3. The Haiku synthesis pass recovers cross-discipline findings the per-chunk passes can't see.

Compaction does not solve (1) — even with compaction, the per-iteration billed input is the *original* corpus size, just summarized later. The cost remains.
Compaction does not solve (2) — it would compress the input but lose the structural-by-division shape that makes the chunked pass useful.
Compaction does not solve (3) — the synthesis pass still needs *some* representation of every chunk's findings.

So compaction is not a substitute for chunking. The only way it would *supplement* chunking is by extending the threshold at which chunking kicks in — e.g. raise `CROSS_CHECK_RECOMMENDED_MAX` to 1M and let compaction handle the 822k–1M band. The problem is that 822k → 1M is exactly the band where the model already struggles to do precise cross-spec comparison, and summarizing the early portion makes it worse, not better.

---

## 7. Question 5 — Costs and beta-header requirements

**Beta header:** `compact-2026-01-12`. Must be sent on every call (not auto-set by the SDK). Spec Critic does not currently rely on any beta header for cross-check (the GA path uses `client.messages.stream(...)` with no betas).

**Cost shape:** compaction's summarization is billed as a separate `iterations` entry in `usage`:

```
usage.iterations = [
    {"type": "compaction", "input_tokens": 180000, "output_tokens": 3500},
    {"type": "message",    "input_tokens": 23000,  "output_tokens": 1000},
]
```

The top-level `input_tokens` / `output_tokens` reflect only the final iteration. **To compute total cost, the caller must sum across iterations.** This breaks Spec Critic's current cost accounting (`diagnostics.DiagnosticsReport.record_api_call` reads top-level usage). Adopting compaction would require routing through `usage.iterations` to avoid silent undercounting.

Per the docs, *re-applying* a prior compaction block on a subsequent turn incurs no extra cost — the block is treated as part of the message. That helps multi-turn agents but is irrelevant to single-turn cross-check.

**Prompt caching interaction:** the docs claim compaction is compatible with prompt caching, and that `cache_control` can be set on compaction blocks. For Spec Critic, the cross-check system prompt is already cached (`PHASE_CROSS_CHECK`). Compaction does not break that, but the *post-compaction* prefix is new and would generate a fresh cache write the first time it appears — net negative for the small project case and roughly neutral for the large project case.

---

## 8. Question 6 — Tests that would prove compaction does not lose important coordination conflicts

If implementation were ever authorized:

1. **Ground-truth cross-check fixture.** Build a synthetic 250k-token corpus with three known cross-discipline conflicts (HVAC vs. plumbing, division-23 vs. division-22 schedule mismatch, division-23 vs. division-25 controls sequence). Run the current chunked pass and capture findings. Run with compaction enabled and capture findings. Assert the compaction-enabled run finds at least the same three conflicts.
2. **Element-id retention.** Assert that every cross-check finding's `independentEvidenceIds` references an id that exists in the *uncompacted* paragraph_map. Compaction-induced loss of ids should make this assertion fail.
3. **Cost accounting.** Assert that `record_api_call` sums across `usage.iterations` when present. Currently it does not.
4. **Stability under repeated runs.** Run the same compaction-enabled cross-check three times against the same corpus. The summarization step is non-deterministic; assert the union of findings across runs is stable to within a sensible threshold (e.g. 90% Jaccard similarity on finding dedup keys).

Test (1) is the load-bearing one. If compaction degrades it, the whole experiment is over.

---

## 9. Other considerations

- **The synthesis pass is the more interesting lever.** `_run_cross_discipline_synthesis` already takes a *deliberate* summary of each chunk (severity / file / section / issue, ≤300 chars each) and asks Haiku to find cross-discipline issues. That is hand-crafted compaction tuned for cross-check semantics. Replacing it with API-side compaction loses the per-finding shape that makes it useful.
- **The 1M context window on Opus 4.6/4.7 and Sonnet 4.6 already covers most projects.** `CROSS_CHECK_RECOMMENDED_MAX = 822_000` was set defensively under the 1M ceiling — there is headroom to raise it (e.g. to 950k) without touching compaction at all. That is a smaller, lower-risk change that addresses the same band of "borderline-too-big" projects, and was not in scope for this delta plan but is worth flagging.
- **Compaction's value proposition is multi-turn agentic context decay.** Spec Critic has no such loop. Even the verification phase, which makes many calls, makes each one as a fresh request (no shared prior history).

---

## 10. Recommendation

**Recommendation: compaction is not suitable for cross-checking exact spec language. Do not use.**

Specifically:

1. **Compaction is fundamentally incompatible with the cross-check task** because it summarizes the very text that cross-check must compare verbatim, and because it discards the stable `<para>`/`<row>`/`<heading>` element ids that Chunk K2/M cross-check findings depend on.
2. **Compaction is not a substitute for chunking** because it does not change the billed input cost and because it loses the by-division structure that makes the chunked pass useful.
3. **Compaction is not even a useful supplement** because the band it would target (822k–1M tokens) is exactly the band where exact-evidence cross-check is most fragile.
4. **The cost accounting in `diagnostics.record_api_call` would need to change** to handle `usage.iterations`. That is a real but small piece of work; it does not change the recommendation.

**Concrete action items:**

- **None.** Do not implement compaction in cross-check.
- **Optional / out-of-scope:** consider raising `CROSS_CHECK_RECOMMENDED_MAX` from 822k toward the 1M context ceiling for Opus 4.6/4.7 and Sonnet 4.6 specifically. This is a separate design exercise (it requires a load test and a measurement of when the model's coordination-finding quality degrades) and should *not* be folded into a D9 chunk.
- **Optional / out-of-scope:** improve the synthesis pass instead. It is the project's existing "compaction" mechanism, hand-built for cross-check semantics. Worth investing in before any server-side compaction.

**Revisit triggers:**

- Anthropic adds an "exact-quote preservation" mode to compaction that keeps numeric and identifier tokens verbatim.
- Spec Critic adds a multi-turn cross-check agent (not currently planned; cross-check is single-turn by design).
- The chunked-cross-check fallback proves inadequate on real projects.

---

## 11. References used

- Spec Critic source: `src/cross_checker.py` (`run_cross_check`, `run_chunked_cross_check`, `_group_specs_by_chunk`, `_run_cross_discipline_synthesis`, `_build_cross_check_input`), `src/structured_schemas.py` (`_CROSS_CHECK_FINDING_OBJECT_SCHEMA`), `src/pipeline.py` (`classify_cross_check_dependencies`), `src/tokenizer.py` (`CROSS_CHECK_RECOMMENDED_MAX`), `src/diagnostics.py` (`record_api_call`).
- Anthropic documentation (fetched 2026-05-11):
  - `platform.claude.com/docs/en/build-with-claude/compaction.md` (model support, beta header `compact-2026-01-12`, response shape, `usage.iterations` billing, prompt-caching interaction, server-tool compatibility).
- Delta plan: Chunk D9.3 in `1e4f5a14-spec_critic_delta_plan_from_second_agent.md`.
