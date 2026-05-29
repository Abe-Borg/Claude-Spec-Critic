# Agent Prompt — Chapter 5: The Review Engine

**Full title:** *The Review Engine: Prompts, Schemas & the Anthropic Client*

## Your mission
Explain how a spec's text becomes a structured list of `Finding`s: the prompts
that tell Claude what a defect is, the **tool schema** that forces structured
output, the client that calls the API and parses the result (with a resilient
fallback), and the **prompt-cache discipline** that keeps the expensive prefix
reusable. You also own the definition of the pipeline's unit of currency — the
`Finding` and its `EditProposal`.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (facts), §7 (glossary), §8 (template).
2. `CLAUDE.md` — "REPORT_ONLY action," "Edit instructions are emitted, not
   applied" (the `as_edit_proposal()` accessor), "Prompt-cache breakpoint
   stability."
3. Source you own:
   - `src/review/reviewer.py` — `Finding`, `EditProposal`, `ReviewResult`,
     `validate_edit_shape`, `_extract_json_array` (the salvage parser),
     `_parse_findings`, the client (streaming + tool-use parsing), API-key access.
   - `src/review/structured_schemas.py` — the `submit_review_findings` tool
     schema, the action types, `REPORT_ONLY`, `validate_edit_shape` demotion.
   - `src/review/prompts.py` — the system + user prompt builders (review
     categories, severity rubric, pinned-edition references).
   - `src/review/review_request_builder.py` — the central request-shape builder.
   - `src/review/prompt_serialization.py` — escaping/wrapping at prompt
     boundaries (the single source of truth for wrapper attrs/bodies).
4. `TRUST_AUDIT.md` P1-1 (no-op EDIT allowed) and P1-4 (prompt content needs a
   domain expert).

## In scope (what you own)
- **The `Finding` / `EditProposal` / `ReviewResult` data model** — full
  field-level semantics (Ch 2 only mapped them). What a finding carries:
  severity, issue text, code reference, optional structured edit, and the slots
  later filled by verification. `as_edit_proposal()` as the single accessor that
  reconstructs a proposal from legacy fields and returns `None` for REPORT_ONLY
  / invalid shapes.
- **The prompts.** What the system prompt tells Claude (the defect categories,
  including "code edition misalignment" naming NFPA 13/72 and ASHRAE 62.1/90.1;
  severity rubric; the role). How the spec body and any pre-screen alerts are
  framed. Keep this at the level of *what the model is asked to do and why* — you
  may quote short representative fragments, not the whole prompt.
- **The structured tool schema.** Why structured tool-use over free text; the
  action types (EDIT/DELETE/ADD/**REPORT_ONLY**); `validate_edit_shape` and the
  **demotion-to-REPORT_ONLY** mechanism (EDIT/DELETE/ADD lacking required fields
  get demoted with a `demotion_reason`). Why REPORT_ONLY exists (coordination /
  interpretation findings shouldn't fabricate existing/replacement text).
- **The client & parsing.** Streaming, tool-use block parsing, and the
  **tagged-JSON text fallback** with `_extract_json_array`'s backward-bracket
  salvage of truncated/incomplete JSON. The resilience story: a partially
  returned response is salvaged, not discarded.
- **Prompt-cache breakpoint stability.** Why the instruction prefix before
  `<spec ` must be byte-identical across calls, why `<final_task>` sits *after*
  the spec body (and after `<pre_detected>` when alerts fire), and how
  `prompt_serialization.py` centralizes escaping so boundaries never drift.

## Explicitly OUT of scope (owned elsewhere)
- How the request is actually submitted to the batch service → **Ch 6**.
- Dedup, finding-id assignment, multi-file grouping → **Ch 7**.
- Verification of findings → **Ch 9/10**.
- Output caps / cache policy / `thinking` config mechanics → **Ch 12** (you may
  state the review cap is 128k / 300k-extended and that the prefix is cached, but
  defer the config machinery).
- Rendering edit proposals / the sidecar → **Ch 11**.

## Narrative beats to hit
- *Why structured output*: a compliance tool needs machine-readable findings
  with stable fields, not prose. The tool schema is the contract.
- *Design tension*: models truncate, wander, or omit required fields. The
  responses: salvage parsing, shape validation with graceful demotion, and the
  REPORT_ONLY escape hatch. Tell this as "how we made an unreliable narrator
  produce reliable structure."
- *The cache discipline*: the prefix is large and reused across every spec;
  byte-stability is real money saved. A careless edit to the prefix silently
  blows the cache — hence the single-source-of-truth serialization module.
- *Honest edges*: `validate_edit_shape` doesn't reject a no-op EDIT where
  existing == replacement (Audit P1-1); and the *quality* of findings depends on
  prompt content that really needs a mechanical/plumbing code expert to audit
  (P1-4). Present both candidly.

## Invariants & facts you MUST get right
- REPORT_ONLY is part of the schema; demotion stamps `demotion_reason`.
- `as_edit_proposal()` returns `None` for REPORT_ONLY / invalid shapes.
- The prefix before `<spec ` is byte-identical across calls;
  `prompt_serialization.py` owns escaping.
- Edits are *emitted, never applied* (reinforce the throughline).

## Diagrams & tables
- A table of **action types** (EDIT/DELETE/ADD/REPORT_ONLY) → required fields →
  what happens if missing (demotion).
- A diagram of the prompt layout showing the cached prefix boundary, `<spec>`,
  optional `<pre_detected>`, and the trailing `<final_task>`.
- A short illustrative fragment of the `submit_review_findings` schema (trimmed).

## Cross-references to make
- To **Ch 4** (pre-screen alerts feed `<pre_detected>`), **Ch 6** (batch submit),
  **Ch 7** (dedup/ids), **Ch 11** (edit rendering + sidecar), **Ch 12** (caps,
  cache policy, thinking config), **Ch 16** (P1-1/P1-4 edges).

## Deliverable
- Write to **`handbook/05_review_engine.md`**. H1 = the full title. Target
  **3,500–5,000 words**.

## Quality bar
- A reader understands how unstructured spec text becomes validated structured
  findings, why the cache prefix is sacred, and where the schema/prompt edges
  are. Data-model detail matches `reviewer.py`.
