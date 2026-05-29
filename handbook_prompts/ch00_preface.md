# Agent Prompt — Chapter 0: Preface & How to Read This Handbook

## Your mission
Write the **front matter** for *The Spec Critic Engineer's Handbook*: a preface
that frames the whole book, a reading guide, the conventions used throughout,
and the **reader-facing glossary**. You are setting the tone and the contract
for every chapter that follows. This is the first thing a new engineer reads.

## Read first (in order)
1. `HANDBOOK_PLAN.md` (repo root) — the master plan. **Internalize §1 (vision),
   §6 (shared facts), §7 (glossary), §8 (chapter template).**
2. `README.md` (repo root) — for the product's self-description and the
   design-emphasis bullets.
3. The top of `CLAUDE.md` (repo root) — the one-paragraph "What it is."

You do **not** need to read source code for this chapter. Your job is framing,
not mechanics.

## In scope (what you own)
- A **preface**: what this handbook is, why it was written ("the story so far"),
  who it's for, and the promise it makes to the reader. Establish the book's
  throughline — **trust** (a compliance tool that is confidently wrong is worse
  than no tool; nearly every design choice exists to make uncertainty visible).
- A **reading guide**: the six-Part structure (lift the TOC from
  `HANDBOOK_PLAN.md` §3), and **2–3 suggested reading paths**, e.g.:
  - *New engineer onboarding* → Ch 1 → 2 → 3, then dive by subsystem.
  - *Domain reviewer / non-coder* → Ch 1, Ch 11, Ch 16.
  - *Debugging a strange verdict* → Ch 3 → Ch 9 → Ch 10 → Ch 14.
- **Conventions**: explain the cross-reference style, the diagram style, the
  "emit-but-don't-apply" stance the reader will see everywhere, and the fact
  that the book favors *why* over line-by-line *how*.
- The **reader-facing glossary**: render `HANDBOOK_PLAN.md` §7 as a clean,
  alphabetized glossary the reader can flip back to. Expand each entry to a
  full, friendly sentence or two (the plan's version is terse shorthand).

## Explicitly OUT of scope (owned elsewhere)
- Any subsystem mechanics — every "how it works" belongs to a later chapter.
- The architecture map and data model → **Ch 2**.
- The end-to-end flow narrative → **Ch 3**.
- Do not write chapter summaries that pre-empt the chapters; the reading guide
  should orient, not spoil.

## Narrative beats to hit
- Open with the *stakes*: a single missed stale code reference or an
  over-confident "this is fine" can ship a non-compliant spec to a school
  construction project. That stake is why the book exists.
- State plainly that this is a *living* system with honest, documented edges —
  the handbook does not pretend the program is finished or perfect (foreshadow
  Ch 16 and Ch 17 without detailing them).

## Diagrams & tables
- A clean rendering of the **6-Part / 17-chapter TOC** as a table or nested list.
- A small "reading paths" table (persona → chapter sequence).

## Deliverable
- Write to **`handbook/00_preface.md`**. H1 = "Preface & How to Read This
  Handbook." Target **1,500–2,500 words** (front matter may be shorter than the
  body chapters).

## Quality bar
- Sets a precise, senior-engineer voice the rest of the book will match.
- The glossary is accurate to `HANDBOOK_PLAN.md` §6–§7 (no invented terms).
- Orients without duplicating any chapter's content.
