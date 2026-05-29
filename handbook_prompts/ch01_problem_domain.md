# Agent Prompt — Chapter 1: The Problem Domain

**Full title:** *The Problem Domain: California DSA Mechanical & Plumbing Spec Review*

## Your mission
Make the reader understand **the world this program lives in and the problem it
solves** — well enough that every later design decision feels motivated rather
than arbitrary. This is the "why does this exist" chapter. A reader with zero
construction-domain knowledge should finish it able to explain what a DSA M&P
spec review is, why it's hard, and why an AI tool that *emits but does not apply*
edits is the chosen shape.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — especially §1 (vision/throughline), §6 (shared facts),
   §7 (glossary).
2. `README.md` — the product description, "Design Emphasis," "Edit Instructions
   (Emit-Only)," and "Model Stack" sections.
3. `CLAUDE.md` §1 ("What it is"), the "Pinned standards editions" invariant, and
   "Edit instructions are emitted, not applied."
4. `src/core/code_cycles.py` — to ground the domain in real adopted codes and
   pinned standard editions (read for content, not to document the dataclass —
   that's Ch 12).
5. `src/review/prompts.py` — skim the review categories / severity language to
   see *what kinds of defects* matter (stale code editions, placeholders,
   coordination errors, etc.). Don't document the prompt mechanics (that's Ch 5);
   mine it for "what is a real defect in this domain."

## In scope (what you own)
- **The domain.** What a CSI-format `.docx` mechanical/plumbing specification is;
  what DSA / HCAI / AHJ mean; the California 2025 code cycle (CBC, CMC, CPC,
  Title 24 energy, CALGreen, ASCE 7) and the adopted NFPA/ASHRAE/IAPMO/UL
  editions. Keep it concrete and brief — enough domain to motivate the tool.
- **The pain.** Why human spec review is slow, error-prone, and high-stakes for
  K-12 projects: stale code-cycle references, unresolved placeholders/template
  markers, leftover LEED language, code-edition drift, cross-spec coordination
  conflicts, duplicated/empty sections. These map directly to the deterministic
  detectors and review categories — name them as *real-world defects*, not code.
- **Why AI, and why this shape.** The case for an LLM reviewer, and the central
  design stance: **emit structured edit instructions, never apply them.** Explain
  the downstream-applier contract (report + `<report-stem>.edits.json` sidecar)
  and why a compliance tool deliberately stops short of mutating documents.
- **The trust mandate.** Establish the book's throughline in domain terms: a
  confidently-wrong code citation is a liability; the whole architecture is bent
  toward making uncertainty visible (foreshadow grounding, the nine-label trust
  model, the audits — but do not explain their mechanics).

## Explicitly OUT of scope (owned elsewhere)
- System architecture / package map / data model → **Ch 2**.
- The end-to-end run flow → **Ch 3**.
- How detectors / prompts / verification actually work → **Ch 4, 5, 9, 10**.
- The actual `CodeCycle` dataclass and pinned-edition mechanics → **Ch 12**.
- The audit findings themselves → **Ch 16**.

## Narrative beats to hit
- Problem → stakes → why existing approaches (manual review) fall short → why an
  AI tool helps → why it must be *cautious by construction* (emit-not-apply,
  evidence-grounded). End by handing off to the architecture chapter.
- Convey *inherent difficulty*: building codes are versioned and cross-referenced;
  "correct" depends on the adopted cycle; specs are templated and inherit each
  other's errors across files. This difficulty is the seed of later chapters.

## Invariants & facts you MUST get right
- v3.0.0; California 2025 is the only cycle (2022 removed, do not reintroduce).
- Disciplines are **mechanical & plumbing** for **K-12 DSA** projects.
- Emit-but-don't-apply: the surgical write-back stack was removed in v3.0.0.
- Pinned editions exist so the model checks claims against the editions
  California *actually adopted* — cite NFPA 13/72, ASHRAE 62.1/90.1 as examples.

## Diagrams & tables
- A table of **real-world defect classes** the tool targets (e.g., stale code
  cycle, placeholder, LEED leftover, coordination conflict) with a one-line
  "why it matters on a K-12 project" each.
- Optional: a simple diagram contrasting "traditional manual review" vs. "Spec
  Critic assists, human decides" to set up emit-not-apply.

## Cross-references to make
- Forward to **Ch 2** (the shape), **Ch 4** (detectors), **Ch 10/11** (how trust
  is enforced and shown), **Ch 16** (the honest edges).

## Deliverable
- Write to **`handbook/01_problem_domain.md`**. H1 = the full title above.
  Target **3,000–4,500 words**.

## Quality bar
- A non-domain reader can explain the problem and the emit-not-apply stance after
  reading. Domain claims are accurate and grounded in `code_cycles.py` /
  `prompts.py`. Motivates the rest of the book without explaining its mechanics.
