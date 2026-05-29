# Agent Prompt — Chapter 3: A Run, End to End

**Full title:** *A Run, End to End: Following the Data from `.docx` to Report*

## Your mission
Narrate **one complete run of the program from start to finish** — the moving
picture. Where Ch 2 drew the static boxes, you show the data flowing through
them: a user picks `.docx` files, presses the button, and (eventually) a Word
report and a JSON sidecar appear. You are the connective tissue of the whole
book: every stage gets *introduced here at moderate altitude* and then handed
off to its deep-dive chapter. The signature device of this chapter: **follow one
finding through the entire machine.**

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §3 (TOC), §4 (ownership), §6 (facts).
2. `CLAUDE.md` — "High-level flow" block and "What it is."
3. `README.md` — "Pipeline at a Glance."
4. `src/orchestration/pipeline.py` — the public flow functions (read their
   names, signatures, and docstrings; do **not** document internals — that's
   Ch 7): `_prepare_specs`, `start_batch_review`, `collect_review_batch_results`,
   `run_cross_check_for_batch`, `start_batch_verification`,
   `collect_batch_verification_results`, `finalize_batch_result`.
5. `src/gui/review_run_controller.py` and `src/gui/batch_controller.py` — to see
   how the run is *driven* (submit thread → poll → collect → export). Don't
   document the GUI internals (Ch 13); just trace the order of operations.
6. `main.py` — the entry point.

## In scope (what you own)
- **The full sequence**, narrated as a story with clear stage boundaries:
  1. Selection & extraction (`.docx` → `ExtractedSpec`, cached).
  2. Deterministic pre-screen (local detectors fire before any API call).
  3. Token preflight (exact Anthropic count; *raises* if over `RECOMMENDED_MAX`).
  4. Submit the **review batch** (Message Batches API).
  5. Poll with bounded backoff; collect results; reconcile against the
     *submitted* set; run the repair batch for retryable failures.
  6. Deduplicate findings (before verification).
  7. Optional cross-spec coordination.
  8. Verification (route → batch waves → grounding → verdict; real-time tail
     fallback).
  9. Finalize, then export the Word report + write the `edits.json` sidecar.
- **The handoffs** between stages — what object is produced and consumed at each
  boundary (tie back to Ch 2's data model).
- **"Follow one finding."** Pick a representative finding (e.g., a HIGH-severity
  stale-edition claim) and trace it: detected/raised → deduped → routed →
  verified+grounded → classified into a `ReportStatus` → rendered in the report
  and serialized to the sidecar. This makes the whole pipeline concrete.
- **The async, batch-centric character** of the run: most of the wall-clock time
  is spent waiting on batches; the GUI polls; the user can walk away.

## Explicitly OUT of scope (owned elsewhere)
- *Deep mechanics of any single stage.* You introduce and hand off. Extraction
  internals → **Ch 4**; review prompts/parsing → **Ch 5**; batch wrapper → **Ch 6**;
  dedup/state internals → **Ch 7**; cross-check chunking → **Ch 8**; routing →
  **Ch 9**; grounding/verdicts/cache → **Ch 10**; report/sidecar rendering →
  **Ch 11**; GUI threading → **Ch 13**; tracing → **Ch 14**.
- The static architecture/data-model map → **Ch 2** (reference it; don't redraw
  the package diagram).

## Narrative beats to hit
- Emphasize the *ordering invariants* that make the run trustworthy: pre-screen
  before API; preflight raises rather than silently truncating; dedup *before*
  verification (so a verdict can't bind to the wrong finding); reconciliation
  against the submitted set so nothing is silently dropped.
- Foreshadow the honest edge (detail → Ch 16): a *partially failed* run does not
  yet look obviously different from a clean one in the final artifact. Mention it
  here as a flow-level caveat and point to Ch 16/Ch 11.

## Invariants & facts you MUST get right
- All reviews go through the **batch** API (no synchronous review path); typical
  ~45 min–2 hr turnaround.
- Dedup runs **before** verification.
- Token preflight **raises** (`ValueError`) when the exact count exceeds
  `RECOMMENDED_MAX` (500k).
- Verification has a **real-time fallback** when the unresolved tail drops below
  the threshold (5).
- Output is **report + `edits.json` sidecar**; nothing is applied.

## Diagrams & tables (this chapter should be flow-diagram-centric)
- A top-to-bottom **pipeline flow diagram** (the `CLAUDE.md` high-level-flow as a
  proper diagram), annotated with the object handed off at each arrow.
- A **swimlane or sequence sketch** showing GUI thread vs. worker thread vs.
  Anthropic batch service across the run (high level; defer threading detail to
  Ch 13).
- A small "lifecycle of one finding" strip.

## Cross-references to make
- A pointer at *every* stage to its owning deep-dive chapter (this chapter is the
  book's index to the pipeline).

## Deliverable
- Write to **`handbook/03_end_to_end_flow.md`**. H1 = the full title. Target
  **3,500–5,000 words**.

## Quality bar
- A reader finishes able to recount the run start-to-finish and name which
  chapter explains each stage. The "follow one finding" thread is concrete and
  correct. No stage is explained so deeply that it steps on an owning chapter.
