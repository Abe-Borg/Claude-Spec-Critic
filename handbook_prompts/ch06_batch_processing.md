# Agent Prompt — Chapter 6: Batch Processing

**Full title:** *Batch Processing: The Message Batches Backbone*

## Your mission
Explain the asynchronous backbone that every model phase runs on: the
**Message Batches API** wrapper and the bounded-polling runtime. This is where
the program trades latency for cost (≈50% savings) and gains the **300k
extended-output** path. Make the reader understand why a desktop spec-review
tool is built around "submit, walk away, collect later," and how the program
polls without hammering the API or hanging forever.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (facts: batch turnaround, custom_id scheme, extended
   output, output caps).
2. `CLAUDE.md` — "Processing Mode" (README cross-reads), "§6 Token Budgets"
   (extended batch review 300k), the extended-output beta note.
3. Source you own:
   - `src/batch/batch.py` — the Anthropic Message Batches wrapper: submit,
     retrieve, the `custom_id` construction (`review__{name}__{idx}`,
     `verify__{idx}`), per-item `params`, status mapping.
   - `src/batch/batch_runtime.py` — bounded polling with progressive backoff;
     the poll loop, status, timeouts.
4. `STRUCTURAL_AUDIT.md` "custom_id collisions can't happen" (verified-clean) and
   "Truncated/incomplete review JSON is salvaged."
5. `TRUST_AUDIT.md` P0-4 (the hardcoded `output-300k-2026-03-24` beta header as a
   stale-header risk class) and P1-2 (batch partial-failure surfacing).

## In scope (what you own)
- **Why batch.** The cost model (≈50% cheaper), the latency trade (~45 min–2 hr
  typical, 24 hr max), and why that fits a review tool the user runs and leaves.
  Contrast with synchronous calls (used only for small verification tails and
  cross-check — defer those to Ch 8/10).
- **The wrapper.** How a list of per-spec (or per-finding) requests becomes a
  batch: the **`custom_id` scheme** and why the trailing enumerate index
  guarantees uniqueness even for identically-named specs; how `params` are
  shaped per item; how results are retrieved and mapped back by `custom_id`.
- **The polling runtime.** Bounded polling with **progressive backoff**: the
  cadence, the ceiling, and why bounded (never hang the UI forever). How status
  transitions are surfaced to callers (the GUI shows progress — defer GUI to
  Ch 13).
- **The 300k extended-output path.** Batch-only by API design; gated by the
  `output-300k-2026-03-24` beta header; fires only for inputs ≥200k tokens; the
  baseline 128k cap otherwise. Explain the gating check
  (`assert_extended_output_allowed`) at a high level (full config → Ch 12).
- **Resilience at this layer.** What "missing / incomplete / errored" looks like
  per item, and that results are *reconciled against the submitted set* by the
  caller (the actual reconciliation + repair batch lives in Ch 7 — hand off, but
  explain what the batch layer returns so Ch 7 can act on it).

## Explicitly OUT of scope (owned elsewhere)
- What's *inside* the requests (review prompts → Ch 5; verification requests →
  Ch 9/10).
- Reconciliation, the repair batch, and partial-failure surfacing → **Ch 7**
  (review) and **Ch 10** (verification waves).
- Output cap constants, beta-header config, model capability gating → **Ch 12**.
- The verification "wave" loop and real-time fallback → **Ch 10**.

## Narrative beats to hit
- *The fundamental trade*: this tool chose throughput-and-cost over latency, and
  the whole UX (submit → poll → collect) follows from that single decision.
- *Why bounded polling*: a naive `while True` poll is a hang and a rate-limit
  risk; progressive backoff is the cheap, robust answer.
- *The stale-beta-header cautionary tale*: foreshadow the `web-fetch-2026-02-09`
  incident (full story in Ch 10/17) — the 300k header is the same risk class
  (Audit P0-4): a hardcoded beta value that crashes every large-input run if the
  API retires it, with only a presence check, not an acceptance check. Present
  this as a live, honest concern.

## Invariants & facts you MUST get right
- All reviews go through batch; review `custom_id` = `review__{sanitized}__{idx}`,
  verification `custom_id` = `verify__{idx}`.
- custom_id collisions can't happen (the enumerate idx disambiguates).
- 300k is batch-only, inputs ≥200k tokens, `output-300k-2026-03-24` header.
- Web fetch (a *server tool*, not a batch concept) is GA and needs **no** beta
  header — don't conflate it with the 300k header (full story → Ch 10).

## Diagrams & tables
- A sequence diagram: client → submit batch → (poll, backoff)* → retrieve →
  map-by-custom_id.
- A table: phase → uses batch? → output cap → notes (review/cross-check/
  verification), with a column for "extended 300k eligible?".

## Cross-references to make
- To **Ch 5** (review request contents), **Ch 7** (review reconciliation/repair),
  **Ch 9/10** (verification batching/waves), **Ch 12** (caps/beta/capabilities),
  **Ch 13** (poll progress in the UI), **Ch 17** (the beta-header lesson).

## Deliverable
- Write to **`handbook/06_batch_processing.md`**. H1 = the full title. Target
  **3,000–4,500 words**.

## Quality bar
- A reader understands why the tool is batch-centric, how requests round-trip by
  `custom_id`, how polling stays bounded, and what the 300k path costs in
  fragility. Facts match `batch.py` / `batch_runtime.py`.
