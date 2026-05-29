# The Spec Critic Engineer's Handbook

*A blended engineering handbook and narrative for the Spec Critic codebase,
captured at **version 3.0.0**.*

Spec Critic is a Python desktop application (CustomTkinter) that reviews
California K-12 **DSA mechanical & plumbing** `.docx` specifications. It extracts
text, runs deterministic local pre-screens, sends per-spec reviews through
Claude's Message Batches API, optionally runs cross-spec coordination, verifies
findings against web search, and exports a Word report plus a machine-readable
JSON sidecar of suggested edits. Crucially, **it emits edit instructions but
never applies them** — the surgical write-back stack was removed in v3.0.0.

This handbook teaches the system deeply enough that a new engineer can navigate,
modify, and trust the code; explains every subsystem and how the pieces fit;
traces the flow from `.docx` input to report; and tells the story of *why* the
code is shaped the way it is — what was hard, what bit the team, and where the
program is still being perfected.

> **The throughline is trust.** A compliance tool that is *confidently wrong*
> about a building code is worse than no tool at all. Almost every design decision
> in this codebase — deterministic pre-screening, evidence-grounded verification,
> the emit-but-don't-apply stance, the nine-label trust model, the forensic trace
> — exists to make uncertainty *visible* rather than hidden. Read every chapter
> with the question *"how does this part make uncertainty visible?"* in mind.

---

## Table of contents

The handbook is **front matter plus seventeen chapters, grouped into six Parts**.
Parts I–IV follow the data; Parts V–VI step back to the cross-cutting systems and
the meta-story.

### Front Matter

- [**Ch 0 — Preface & How to Read This Handbook**](00_preface.md) — the trust
  throughline, who the book is for, reading paths, and the reader-facing glossary.

### Part I — The Problem & The Shape

- [**Ch 1 — The Problem Domain: California DSA Mechanical & Plumbing Spec Review**](01_problem_domain.md)
  — what a spec is, the DSA/HCAI gate, the cycle-relative meaning of "correct,"
  and why a confident error is a hazard.
- [**Ch 2 — Architecture at a Glance: Subsystems, Dependencies & the Core Data Model**](02_architecture.md)
  — the ten packages, the dependency layering, and the `Finding`-centered data
  model that is the contract between layers.
- [**Ch 3 — A Run, End to End: Following the Data from `.docx` to Report**](03_end_to_end_flow.md)
  — the nine pipeline stages in motion, following one finding from paragraph to
  report and sidecar.

### Part II — Ingestion & Review

- [**Ch 4 — Input: Extraction, Element IDs & the Deterministic Pre-Screen**](04_input.md)
  — turning `.docx` into addressable text and catching the cheap, certain defects
  locally before any model call.
- [**Ch 5 — The Review Engine: Prompts, Schemas & the Anthropic Client**](05_review_engine.md)
  — making an unreliable narrator produce reliable structure: the tool schema,
  the prompts, the salvage parser, and prompt-cache discipline.
- [**Ch 6 — Batch Processing: The Message Batches Backbone**](06_batch_processing.md)
  — trading latency for cost and output headroom, the `custom_id` round-trip, the
  bounded poller, and the 300k extended-output path.

### Part III — Coordination & Verification

- [**Ch 7 — Orchestration & State: The Pipeline Spine**](07_orchestration.md)
  — the spine that sequences stages, carries state across batch gaps, reconciles
  results, and deduplicates before verification.
- [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md)
  — finding defects that live *between* specs, the CSI-division chunking
  compromise, and its honest blind spots.
- [**Ch 9 — Verification I: How We Decide to Check (Routing, Modes, Profiles, Triage)**](09_verification_routing.md)
  — the deterministic decision layer: whether to check, what kind of claim, how
  hard to reason, and how much search budget.
- [**Ch 10 — Verification II: How We Check & Judge (Grounding, Verdicts, Escalation, Cache)**](10_verification_grounding.md)
  — the grounding invariant (the program's immune system), escalation, the
  contested verdict, and the claim cache.

### Part IV — Output & Trust

- [**Ch 11 — The Trust Model & Report Output: Status Labels, the Word Report & the Edit Sidecar**](11_trust_model_and_output.md)
  — the nine `ReportStatus` labels and two `EditActionLabel` values, the Word
  report's layout, and the emit-but-don't-apply edit sidecar.

### Part V — Cross-Cutting Systems

- [**Ch 12 — Configuration, Models & Token Economics**](12_configuration_and_models.md)
  — the control plane: the model stack, the capability whitelist, output caps,
  token counting, prompt-cache policy, and the pinned standard editions.
- [**Ch 13 — The Desktop GUI & Its Controller Architecture**](13_gui.md)
  — the thin shell, seven controllers, and the threading / run-epoch discipline
  that keeps a multi-hour run responsive and race-free.
- [**Ch 14 — Observability: Tracing & Diagnostics**](14_observability.md)
  — the forensic JSONL trace and the in-memory diagnostics report, and the silo
  guarantee that observation never alters the run.
- [**Ch 15 — Quality Engineering: Testing & Calibration**](15_quality_engineering.md)
  — testing the deterministic seams around the model, the hermetic suite, and the
  two evals (golden-set regression and judgment calibration).

### Part VI — The Meta-Story

- [**Ch 16 — Trust Under the Microscope: The Audits**](16_trust_under_the_microscope.md)
  — what two formal audits found: the core is the strong part; the risk lives at
  the edges (surfacing partial failure, multi-file edit emission).
- [**Ch 17 — Evolution & Lessons: The v3.0.0 Pivot and the Road Ahead**](17_evolution_and_lessons.md)
  — why the program got more trustworthy by getting *smaller*, the beta-header
  incident, the design creed, and the road ahead.

---

## Suggested reading paths

You do not have to read straight through. Three paths cover the common reasons
you might be here (the full reading guide lives in
[Ch 0 — Preface](00_preface.md)):

| If you are… | Read in this order |
|---|---|
| **A new engineer onboarding** | [Ch 1](01_problem_domain.md) → [Ch 2](02_architecture.md) → [Ch 3](03_end_to_end_flow.md), then dive into whichever subsystem you'll touch (Parts II–V). |
| **A domain reviewer or non-coder** | [Ch 1](01_problem_domain.md) → [Ch 11](11_trust_model_and_output.md) → [Ch 16](16_trust_under_the_microscope.md). What the tool reviews, how to read its trust labels, and where its honest limits are. |
| **Debugging a strange verdict** | [Ch 3](03_end_to_end_flow.md) → [Ch 9](09_verification_routing.md) → [Ch 10](10_verification_grounding.md) → [Ch 14](14_observability.md). The flow, the routing, the grounding, then how to replay the run from its trace. |

---

## Conventions

- **Why over how.** Chapters explain *mechanisms and rationale*, not line-by-line
  code; code appears only in short illustrative fragments.
- **The source code is the final authority.** Where this handbook, the project's
  `CLAUDE.md`, and a chapter disagree, **the code wins.** Chapters cite the file
  (and often the symbol) for a value, and flag any contradiction they find — that
  drift is what the audits in [Ch 16](16_trust_under_the_microscope.md) care about.
- **Cross-references are links.** A reference such as
  [**Ch 10 — Verification II**](10_verification_grounding.md) is a relative link to
  that chapter's file; this README is the master table of contents.
- **"Emit, don't apply" is everywhere.** An "edit proposal," an "edit sidecar," or
  an `EDIT_SUGGESTED` label is a *suggestion* for a human or a downstream tool —
  never a change the program made.

Assembly provenance, the consistency edits applied during integration, and the
known source-vs-doc drifts the chapters flag are recorded in
[ASSEMBLY_NOTES.md](ASSEMBLY_NOTES.md).
