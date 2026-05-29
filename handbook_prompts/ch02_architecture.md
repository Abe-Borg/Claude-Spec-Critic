# Agent Prompt — Chapter 2: Architecture at a Glance

**Full title:** *Architecture at a Glance: Subsystems, Dependencies & the Core Data Model*

## Your mission
Give the reader the **static map** of the whole system: the packages, what each
is responsible for, how they depend on one another, and — crucially — the
**handful of data objects that flow through the pipeline**. After this chapter a
reader should be able to point at any source file and know which subsystem it
belongs to and roughly what it does, and should understand the shape of the data
that moves between stages. This is the structural backbone the rest of the book
hangs on.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §3 (TOC), §4 (ownership map), §6 (facts), §7 (glossary).
2. `CLAUDE.md` — the "Source layout" tree and "High-level flow" block.
3. The repo tree (`src/`), and the **dataclass definitions** (read signatures /
   fields, not full bodies):
   - `src/input/extractor.py` → `ExtractedSpec`, `ParagraphMapping`
   - `src/input/preprocessor.py` → `PreprocessResult`
   - `src/review/reviewer.py` → `Finding`, `EditProposal`, `ReviewResult`
   - `src/verification/verifier.py` → `VerificationResult`
   - `src/verification/verification_routing.py` → `VerificationRoutingDecision`
   - `src/orchestration/pipeline.py` → `PipelineResult`, `CollectedBatchState`,
     `FindingGroup`, `FindingOccurrence`
   - `src/orchestration/diagnostics.py` → `DiagnosticsReport`

## In scope (what you own)
- **The package map.** The 8 functional packages plus `gui` and `tracing`:
  `core`, `input`, `review`, `batch`, `orchestration`, `cross_check`,
  `verification`, `output`, `gui`, `tracing`. One tight paragraph each: its
  responsibility and the files inside it (name them; defer mechanics to owners).
- **The dependency / layering view.** Draw the layering: `core` (config, cycles,
  tokenizer) underpins everything; `input` / `review` / `batch` / `verification`
  / `cross_check` are the worker subsystems; `orchestration` is the spine that
  sequences them; `output` consumes the finished state; `gui` drives the
  orchestration; `tracing` is a *silo* that observes without being depended upon.
  Show who-imports-whom at the package level.
- **The core data model — the chapter's centerpiece.** Introduce the ~10
  dataclasses as a *connected map*: what each one carries (the load-bearing
  fields only), and how one becomes the next as data flows
  (`ExtractedSpec` → `Finding`[] in a `ReviewResult` → deduped `Finding`[] →
  each gets a `VerificationResult` → aggregated into `PipelineResult`). Include
  the multi-file grouping pair (`FindingGroup`/`FindingOccurrence`) and the
  routing/diagnostics objects. **For full field-level semantics, defer to the
  owning chapter** (Finding/EditProposal → Ch 5; VerificationResult → Ch 10;
  RoutingDecision → Ch 9; ExtractedSpec/PreprocessResult → Ch 4;
  PipelineResult/groups → Ch 7; DiagnosticsReport → Ch 14). Say so explicitly.
- **Cross-cutting design principles** visible in the structure: determinism
  before any API call; emit-not-apply; trust-model output; tracing as a
  non-invasive silo; degrade-to-safe-defaults configuration.

## Explicitly OUT of scope (owned elsewhere)
- The *dynamic* run-time flow and handoffs → **Ch 3** (you own the static
  structure; Ch 3 owns the moving picture — coordinate so you don't both narrate
  the pipeline sequence in depth; you show the boxes, Ch 3 shows the data moving
  through them).
- Field-level detail of any dataclass → its owning chapter (defer with a pointer).
- Any subsystem's internal mechanics.

## Narrative beats to hit
- Why this layering and not a monolith: each worker subsystem is independently
  testable and swappable; the spine owns sequencing and state; the GUI is thin.
- The data model *is* the contract between stages — emphasize that a `Finding`
  is the unit of currency and accumulates context (edit proposal, then verdict)
  as it travels.

## Invariants & facts you MUST get right
- 56 source files, 8 functional packages + `gui` + `tracing` (verify the tree).
- `Finding.occurrence_originals` holds per-file members after a multi-file merge
  (full detail → Ch 7, but mention it exists as part of the data model).
- Tracing never alters `Finding`/`ReviewResult`/`VerificationResult` shape (the
  silo guarantee) — mention as a structural principle; detail → Ch 14.

## Diagrams & tables (this chapter should be diagram-rich)
- A **package dependency diagram** (boxes + arrows; ASCII or Mermaid).
- A **data-flow diagram** of the core objects transforming stage to stage.
- A **table** mapping each package → responsibility → key files → owning chapter.

## Cross-references to make
- Forward pointers to every Part II–V chapter as the "deep dive" for each box.
- To **Ch 3** for the dynamic flow.

## Deliverable
- Write to **`handbook/02_architecture.md`**. H1 = the full title. Target
  **3,500–5,000 words** (diagram-heavy; prose can be tighter).

## Quality bar
- A reader can place any file in its subsystem and sketch the data model from
  memory. Diagrams are legible in plain text. Defers field detail cleanly so it
  doesn't collide with owning chapters.
