# Agent Prompt — Chapter 17: Evolution & Lessons

**Full title:** *Evolution & Lessons: The v3.0.0 Pivot and the Road Ahead*

## Your mission
Close the book with the **arc of the project**: where it came from, the big
decisions that shaped it (above all the v3.0.0 "emit-but-don't-apply" pivot), the
incidents that taught hard lessons, the distilled design philosophy, and the road
ahead. This is the chapter that turns "here is the system" into "here is *why* the
system is the way it is, and here is what comes next." It should feel like the
reflective final chapter of a good engineering book.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §1 (vision/throughline), §6 (facts).
2. `README.md` — the **Changelog (recent)** section (v3.0.0, v2.11.0) and the
   "Edit Instructions (Emit-Only)" section.
3. `CLAUDE.md` — the v3.0.0 framing throughout, the "Working agreements" (the PR
   workflow standing instruction), and the "Web-fetch for follow-up reads"
   incident write-up (the retired beta header).
4. `RELEASE_NOTES.md` and the **git history** (use `git log --oneline` to see the
   recent arc: the M-series refactors — M1 resume-subsystem removal, M2b cross-
   check dependency-suppression deletion, M3 auto-apply/locator purge, M7/M9
   serialization unification & cosmetic cleanup — the test-suite trim 601→448,
   the two audit-plan commits).
5. Skim Ch 16 (the audits) so the "road ahead" aligns with the open items.

## In scope (what you own)
- **The version arc.** A concise history culminating in v3.0.0. Earlier cycles
  (the chunks A–P refactor, v2.8.x, v2.10.0, v2.11.0's Opus-4.7 upgrade, the
  persistent cache, Haiku triage, severity-tiered budgets, the contested and
  budget-exhausted "trust upgrade chunks"). Don't enumerate exhaustively — draw
  the *shape* of the evolution.
- **The v3.0.0 pivot — the centerpiece.** Spec Critic used to *apply* edits: there
  was a surgical write-back stack (`src/editing/`: locator, spec_editor,
  apply_edits, replacement_style, edit_candidates), GUI apply dialogs, and
  auto-edit confidence gating (composite confidence, numeric/standards demotion,
  an auto-edit floor). v3.0.0 **removed all of it** in favor of emitting structured
  edit *instructions* (the report + `edits.json` sidecar) for a separate future
  applier. Tell *why*: applying edits to a legal/compliance document is a
  high-stakes, locator-fragile operation; emitting instructions and letting a
  dedicated, auditable applier (and a human) own application is the safer
  factoring. Note the cascade of simplifications this unlocked — `EditActionLabel`
  collapsed to two values; `classify_edit_action` became "is there a proposal?";
  a raft of edit-application env vars was deleted; the resume/durable-state
  subsystem and the cross-check dependency-suppression feature were removed; the
  test suite was trimmed 601→448. The recurring theme: **a lot of complexity
  existed only to support auto-apply, and removing auto-apply let it all go.**
- **Incidents & hard lessons.** Tell the **retired beta-header incident** as the
  marquee war story: the code shipped attaching `anthropic-beta: web-fetch-2026-02-09`
  on the assumption a beta header is "harmless when GA, required when gated." Both
  halves were wrong — web_fetch is GA (the tool dict alone enables it) and an
  *unrecognized* beta value is rejected with HTTP 400, so every STANDARD/DEEP
  verification (the common path) crashed at submit. The fix: attach no beta header
  for web_fetch. The lesson: **don't attach speculative beta headers; a beta value
  is a hard contract with the API, not a hint.** Connect this to the *still-live*
  same-risk-class item: the hardcoded 300k header (cross-ref Ch 6/12/16).
- **The design philosophy, distilled.** Pull the recurring principles into a
  coherent creed: determinism before the model; evidence-grounded verdicts;
  emit-but-don't-apply; degrade to safe defaults; make uncertainty visible;
  observe without mutating; pin invariants with tests; keep documentation honest
  about the edges. Show how each principle is visible across the chapters.
- **The road ahead.** A credible forward agenda, grounded in the audits (Ch 16)
  and the code's own seams: the downstream **applier** the sidecar is built for;
  surfacing partial-failure in the artifact (the headline gap); per-file sidecar
  fan-out; model-whitelist maintenance / loud-warn on unknown ids; the
  beta-header acceptance-vs-presence fragility; extraction completeness; and
  keeping the pinned-edition matrix current as cycles advance.

## Explicitly OUT of scope (owned elsewhere)
- The mechanics of the *current* system → the owning chapters (you reflect on
  them; you don't re-explain them).
- The full audit detail → **Ch 16** (you align the road-ahead with it and
  cross-reference; don't duplicate the open-items table).

## Narrative beats to hit
- *Subtraction as progress.* The most important recent work was *removing* things.
  Make the case that the v3.0.0 pivot improved trustworthiness by *reducing*
  surface area — a satisfying counter-narrative to "more features = better."
- *Lessons paid for in incidents.* The beta-header crash is concrete and
  memorable; use it to teach the general lesson about external contracts and
  fail-safe defaults.
- *An honest ending.* Echo the book's throughline: the program is trustworthy in
  its core and candid about its edges; the road ahead is about closing the
  honesty/completeness gaps at the edges, and about handing edits to a dedicated
  applier. End the book on the trust note it began on.

## Invariants & facts you MUST get right
- v3.0.0 removed the editing/ write-back stack, the apply dialogs, auto-edit
  gating, the resume subsystem, and the cross-check dependency-suppression
  feature; deleted the edit-application env vars; trimmed tests 601→448.
- `EditActionLabel` is now two values; `classify_edit_action` is proposal-or-not.
- web_fetch is GA and takes **no** beta header (the incident); the 300k header is
  the same risk class and still live.
- California 2025 is the only cycle; 2022 was removed and must not return.
- Emit-but-don't-apply is the defining stance.

## Diagrams & tables
- A **timeline / arc** of the major versions and the M-series removals.
- A **"removed in v3.0.0" table** (what was removed → why → what it simplified).
- A **design-principles table** (principle → where it shows up across chapters).
- A **road-ahead table** (item → motivation → cross-ref to Ch 16 / owning chapter).

## Cross-references to make
- To **Ch 16** (the open items / audit agenda), **Ch 10/6/12** (the beta-header
  story and risk class), **Ch 11** (emit-not-apply realized), **Ch 1** (close the
  loop on the problem framing).

## Deliverable
- Write to **`handbook/17_evolution_and_lessons.md`**. H1 = the full title. Target
  **3,500–5,000 words**.

## Quality bar
- A reader finishes understanding not just *what* Spec Critic is but *why* it
  became this, what it learned, and where it's going. The v3.0.0 pivot and the
  beta-header lesson are told vividly and accurately. The book lands on its trust
  throughline.
