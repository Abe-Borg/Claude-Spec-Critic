# Agent Prompt — Wave D: Assembly & Consistency Pass

## Your mission
You run **after all 18 chapter files exist** in `handbook/`. You are the
**editor, not an author.** Your job is to turn 18 independently-written chapters
into one cohesive handbook: validate completeness, enforce consistency (facts,
terminology, scope), normalize cross-references into working links, stitch the
chapters into a final artifact with a generated table of contents and Part
dividers, and produce an assembly report. Make **light-touch** edits for
consistency — do **not** rewrite chapters or change their substance.

## Prerequisite (check first — do not proceed if unmet)
All chapter files must be present and non-trivial:
`handbook/00_preface.md`, `handbook/01_problem_domain.md`, …,
`handbook/17_evolution_and_lessons.md` (19 files: front matter + Ch 1–17).
If any is **missing, empty, or an obvious stub**, STOP and report which in
`handbook/ASSEMBLY_NOTES.md` — do not fabricate or rewrite a missing chapter.

## Read first
1. `HANDBOOK_PLAN.md` — the whole file, especially:
   - **§3** canonical TOC (exact chapter numbers + titles)
   - **§4** source-file → owning-chapter map (the authority for overlap)
   - **§6** shared facts sheet
   - **§7** glossary
   - **§8** chapter template + cross-reference style
   - **§5** conflict rule: **source code > this plan > chapter prompts**
2. All chapter files in `handbook/`.
3. The source tree / `CLAUDE.md` / the audit docs — **only as needed** to
   adjudicate a specific factual discrepancy (source wins).

## What to do, in order

### 1. Inventory & structural validation
- Confirm all 19 files exist; each H1 matches its canonical title from §3
  (flag mismatches).
- Note each chapter's approximate word count against its target; flag anything
  far under (possible stub) or wildly over.

### 2. Facts & terminology consistency
- Cross-check load-bearing facts against §6 across all chapters: version
  (3.0.0); model stack; the **4 modes**, **5 profiles**, **9 `ReportStatus`
  names** (exact spelling/casing), **2 `EditActionLabel`** names; search budgets
  (8/7/5/3); output caps; context limits; cache TTL (60d); CSI chunks;
  element-id format; "**web_fetch is GA / takes no beta header**"; the 300k
  header. Where a chapter has drifted, fix it to match §6.
  **If §6 itself conflicts with the source, the source wins** — fix both and
  record it in the notes.
- Enforce glossary terms (§7): consistent naming (e.g., don't let one chapter
  silently rename "cross-check / coordination"); exact status-label spelling.

### 3. Scope / overlap de-duplication
- Using the §4 ownership map, find places where a **non-owner** chapter
  *deep-dives* a topic owned elsewhere (the classic risk: two chapters both
  fully explaining the grounding invariant). Trim the non-owner to a brief
  mention + a cross-reference to the owner; keep the owner's full treatment.
- Verify these **cross-cutting threads** are framed consistently with one owner,
  others deferring:
  - **Grounding invariant** → owner **Ch 10** (touched by 3, 9, 11, 16).
  - **Partial-failure-looks-clean honesty gap** → owner **Ch 7**, surfacing in
    **Ch 11** (touched by 13, 16).
  - **Sidecar multi-file under-emission** → **Ch 7 / Ch 11** (touched by 16).
  - **Beta-header incident / 300k risk class** → story in **Ch 10**, lesson in
    **Ch 17** (touched by 6, 12).

### 4. Cross-reference normalization
- Find every "see Ch N — Title" reference. Verify the number **and** title match
  §3; fix wrong ones. Flag any **dangling** reference (to a section that doesn't
  exist).
- Convert references to working links per the output form chosen in step 5.

### 5. Stitch the final artifact — pick ONE output form
- **(Recommended) Linked set + index.** Keep the 19 per-chapter files; create
  `handbook/README.md` as the book's front cover: title, one-paragraph blurb,
  the 6-Part / 17-chapter TOC with **relative links** to each chapter file, and
  the reading paths from Ch 0. Cross-references become relative file links, e.g.
  `[Ch 10 — Verification II](10_verification_grounding.md)`. Lowest risk — no
  heading-level churn.
- **(Alternative) Single concatenated book.** Produce
  `handbook/THE_SPEC_CRITIC_HANDBOOK.md`: title page, a generated TOC with
  in-document anchors, **Part dividers** (Part I–VI), and each chapter demoted so
  its title becomes H2 and its internal headings shift down one level
  consistently. Cross-references become in-document anchor links. More polished
  as a single read, but you must normalize heading levels carefully.

Insert the six **Part dividers** in their canonical positions (§3) in either form.

### 6. Light polish (do NOT rewrite)
- Spot-check voice/tense uniformity (senior-engineer, present tense); fix only
  jarring outliers.
- Diagrams may mix ASCII and Mermaid — that's fine; only flag broken/garbled
  ones.
- Ensure exactly **one** canonical navigation TOC (Ch 0 has a *reading guide*;
  the assembled index is the master TOC — de-duplicate if both try to be master).
- Optional: add a one-to-two-sentence bridge at a Part boundary only if the
  transition reads abruptly.

### 7. Report — write `handbook/ASSEMBLY_NOTES.md`
- Inventory result (present / missing / stub; word counts vs. targets).
- Every consistency edit you made, grouped (fact fixes, terminology, scope
  trims, cross-reference fixes).
- **Unresolved issues for a human**: chapters that read as stubs or off-scope;
  contradictions you couldn't safely resolve; any **source-vs-doc conflicts**
  you found (per §5); dangling cross-references you couldn't fix.
- Final stats: chapter count, total word count, output form chosen.

## Hard constraints
- You are an **editor**. Light-touch consistency edits only. Do **not** rewrite a
  chapter's substance, alter its arguments, or "improve" prose wholesale. If a
  chapter is substantively wrong or a stub, **flag it** in `ASSEMBLY_NOTES.md` —
  don't silently rewrite it.
- **Source code > `HANDBOOK_PLAN.md` > chapter prompts.** Verify any contested
  fact against the source before changing it.
- Preserve each author's voice and diagrams.
- **Never delete a chapter file.**

## Deliverable
- The stitched artifact (linked index `handbook/README.md`, **or**
  `handbook/THE_SPEC_CRITIC_HANDBOOK.md`), with the six Part dividers in place and
  cross-references normalized, plus `handbook/ASSEMBLY_NOTES.md`.

## Quality bar
- A reader can navigate the whole book from one entry point; cross-references
  resolve; facts, terminology, and status names are consistent across chapters;
  no topic is deep-dived twice. Everything the editor could not safely fix is
  clearly listed for a human in `ASSEMBLY_NOTES.md`.
