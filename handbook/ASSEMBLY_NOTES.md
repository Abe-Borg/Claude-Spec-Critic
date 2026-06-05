# Assembly Notes — Wave D (Editor Pass)

This file records the integration pass that turned the 18 independently-authored
chapter files into one cohesive handbook: the inventory, the consistency edits
made, the source-vs-doc conflicts found, the items left for a human, and the
final stats. The editor made **light-touch consistency edits only** — no chapter's
substance was rewritten, and no chapter file was deleted.

**Output form chosen:** **Linked set + index** (the recommended, lowest-risk
form). The 18 per-chapter files are kept as-is; [`README.md`](README.md) is the
new master table of contents (front cover, blurb, six-Part TOC with relative
links, reading paths). Cross-references in the chapters were converted to relative
file links. No heading-level demotion or concatenation was performed.

---

## 1. Inventory & structural validation

**All required files present; none missing, empty, or a stub.** The prerequisite
is met.

> **Note on the file count.** The Wave D prompt's prerequisite says "(19 files:
> front matter + Ch 1–17)." That is a miscount: front matter (Ch 0) + Ch 1–17 =
> **18 files**, which is exactly what `HANDBOOK_PLAN.md` §10 specifies ("18 chapter
> files") and exactly what exists. There is no missing 19th file.

**H1 titles:** all 18 match their canonical §3 title verbatim. No mismatches.

**Word counts vs. the §2 target** (3,000–5,000 per chapter; front matter
~1,500–2,500):

| File | Words | Note |
|---|---:|---|
| 00_preface.md | 2,697 | Front matter; slightly over the 2.5k soft cap, fine. |
| 01_problem_domain.md | 4,504 | On target. |
| 02_architecture.md | 4,298 | On target. |
| 03_end_to_end_flow.md | 4,117 | On target. |
| 04_input.md | 4,607 | On target. |
| 05_review_engine.md | 5,770 | Modestly over 5k (richest of the set); not trimmed. |
| 06_batch_processing.md | 4,405 | On target. |
| 07_orchestration.md | 4,470 | On target. |
| 08_cross_spec_coordination.md | 3,795 | On target. |
| 09_verification_routing.md | 5,398 | Modestly over 5k; not trimmed. |
| 10_verification_grounding.md | 5,052 | Just over 5k. |
| 11_trust_model_and_output.md | 4,769 | On target. |
| 12_configuration_and_models.md | 5,160 | Just over 5k. |
| 13_gui.md | 3,804 | On target. |
| 14_observability.md | 4,605 | On target. |
| 15_quality_engineering.md | 4,334 | On target. |
| 16_trust_under_the_microscope.md | 5,030 | Just over 5k. |
| 17_evolution_and_lessons.md | 4,195 | On target. |

No chapter is far under target (no stubs) or wildly over. The five chapters a
little over 5k (5, 9, 10, 12, 16) are the densest subsystems and were left intact.

---

## 2. Consistency edits made

### Fact fixes

1. **Ch 2 — source-file count split.** The prose read "58 Python modules in total:
   **47** application modules and **11** package `__init__.py` files," but the
   chapter's own per-package table sums to **48** app modules, and the tree has
   only **10** `__init__.py` files. Verified against the tree: 58 total `.py`, 48
   application modules, 10 `__init__.py` — the `output/` package ships *without*
   one. Corrected both occurrences (§1 prose and the Key-takeaways line) to "48
   application modules and 10 `__init__.py` files (the `output` package ships
   without one)." The total (58) and the §6-drift footnote were already correct.

2. **Ch 17 — test-suite count reconciled with Ch 15.** Ch 17 stated the suite
   "went from **601 to 448 tests**," while Ch 15 reports "~**396** test functions"
   in the current tree. Verified: the current tree has **396** `def test_`
   definitions (Ch 15 is correct); the "601 → 448" figure comes from a git commit
   message (`Trim test suite to essentials (601 -> 448 tests, -2.2k lines)`) and is
   the **v3.0.0-trim milestone**, not the current count (`RELEASE_NOTES.md` is
   empty; the numbers appear nowhere in the docs). Reframed Ch 17 to attribute
   "601 → 448" to the v3.0.0 trim and added that the suite has since been pared to
   ~396 (cross-referencing Ch 15), so the two chapters no longer appear to
   contradict each other.

### Terminology

3. **Ch 6 — "opt-in" triage wording.** Ch 6's phase table called triage an
   "opt-in Haiku pre-pass" and its connections section called it "the *optional*
   Haiku triage pre-pass." Ch 9 (the owning chapter) establishes — and the source
   confirms (`verifier.py` `prepare_findings_for_verification` calls
   `classify_findings_with_haiku` **unconditionally**, gated only on the presence
   of `ANTHROPIC_API_KEY`, with no feature flag) — that triage runs *automatically*.
   Removed "opt-in"/"optional" from both Ch 6 mentions so it no longer contradicts
   the owner and the code. (`CLAUDE.md`'s source-map and `HANDBOOK_PLAN.md` §6/§7
   still call triage "opt-in"/"optional" — see §3 below.)

### Cross-reference fixes & normalization

4. **Ch 11 — sidecar example `finding_id`.** The illustrative sidecar JSON used
   `"finding_id": "a1b2c3d4"`, which does not match the real format. Ch 7 documents
   `rf-{12 hex}`, and the source (`pipeline.compute_finding_id`) returns
   `f"rf-{digest[:12]}"`. Changed the example to `"rf-3f9a2b7c4d1e"` for internal
   consistency.

5. **Cross-references converted to working relative links (all 18 chapters).**
   Converted **411** bold chapter references into relative file links — **316**
   titled refs (`**Ch N — Title**` → `[**Ch N — Title**](NN_slug.md)`) and **95**
   bare refs (`**Ch N**` → `[**Ch N**](NN_slug.md)`). The conversion was keyed by
   chapter *number* (unambiguous; chapters use both short and full title forms),
   tolerated line wraps anywhere in the reference (including at the em-dash),
   skipped fenced code blocks and self-references, and preserved the title text
   verbatim. Verified afterward with a whitespace-tolerant scan: every titled
   reference is linked (**0** missed), **0** dangling targets, **0** double-wraps,
   **0** paragraph over-matches; all targets resolve to existing files.
   Reading-path arrows (e.g. `**Ch 1 → Ch 2**`) and Ch 0's descriptive "How the
   book is organized" table were intentionally left unlinked.

   **Cross-reference accuracy:** every `Ch N — Title` reference was checked against
   §3. All chapter numbers and titles are correct (chapters use faithful
   short/full forms of the canonical titles). **No wrong numbers, no wrong titles,
   and no dangling references were found.**

### Scope / overlap de-duplication

**No scope trims were needed.** The chapters are already well-disciplined against
the §4 ownership map and the cross-cutting-thread guidance. Each thread has one
owner doing the deep-dive and the others deferring with a cross-reference:

- **Grounding invariant** → fully treated only in **Ch 10**; Ch 3/9/11/16 touch it
  and defer (Ch 11's `has_accepted` re-check is its own classifier concern, not a
  re-explanation). No double deep-dive — the classic risk the prompt named did not
  materialize.
- **Partial-failure-looks-clean gap** → owned by **Ch 7** (spine); surfaced from
  its own angle in **Ch 11** (report/banner) and **Ch 13** (GUI terminal state),
  touched in Ch 3, consolidated in Ch 16. Each covers a distinct angle, not a
  repeated deep-dive.
- **Sidecar multi-file under-emission** → **Ch 7** (spine data) + **Ch 11**
  (sidecar emission); referenced in Ch 8/16.
- **Beta-header incident / 300k risk class** → the incident *story* lives in
  **Ch 10**; the *lesson* in **Ch 17**; the 300k risk class is owned by **Ch 6**;
  Ch 12 and Ch 16 touch it and defer. This matches the planned split.

---

## 3. Source-vs-doc conflicts found (per the "source code wins" rule)

These are places where the **code contradicts** `HANDBOOK_PLAN.md` §6/§7 and/or
`CLAUDE.md`. In every case the chapters resolved the conflict in favor of the
source (mostly via footnotes), which is exactly the behavior §5 prescribes. The
editor **verified the load-bearing ones against the source** and they hold. They
are collected here so a human can decide whether to update the *plan/`CLAUDE.md`*
(the editor does not edit those files).

| # | Conflict | Source truth (verified) | Flagged in |
|---|---|---|---|
| a | §6 says "**56** source files" | **58** `.py` (48 app + 10 `__init__`; `output/` has none) | Ch 2 footnote (+ edit #1) |
| b | `CLAUDE.md` high-level flow: cross-check runs "**parallel** with verification" | Strictly **sequential**: review → verify → cross-check → verify-cross-check (`batch_controller.py`; `run_cross_check_for_batch` reads `f.verification.verdict`, which requires verification first). Sequential is the *safer* design. | Ch 3, 7, 8, 16 (Structural P2-3) |
| c | `CLAUDE.md`/§6/§7 call triage "**opt-in**"/"optional" | Triage runs **automatically** in the verification pre-pass (`verifier.py`), gated only on `ANTHROPIC_API_KEY`; no feature flag | Ch 9 footnote (+ edit #3) |
| d | §6 model stack is "**all overridable** by env var" | **Cross-check** model is *not* overridable: `CROSS_CHECK_MODEL_DEFAULT` is bound directly, no `SPEC_CRITIC_CROSS_CHECK_MODEL` exists | Ch 12 drift note |
| e | `CLAUDE.md`/Ch 2 call `reviewer.py` the "**streaming** + tool-use" client | Review rides the **batch** API (no streaming); `reviewer.py` is the shared parsing library | Ch 5 footnote |
| f | Plan/`CLAUDE.md` source-map describe a **`TraceSession`** class in `session.py` | No such class; `session.py` holds lifecycle helpers, the per-run dir + `run.json` live on `TraceRecorder` in `recorder.py` | Ch 14 footnote |
| g | `CLAUDE.md` §9 lists `tests/fixtures/docx_fixtures.py` | **Does not exist** (verified: `tests/fixtures/` has only `fake_anthropic.py`); tests build DOCX inline | Ch 15 footnote |
| h | `conftest.py` guards `test_core_regressions.py` / `test_gui_refactor_modules.py` | Neither exists (verified: 26 `test_*.py` files); guard is a harmless fossil | Ch 15 footnote |
| i | `ParagraphMapping` docstring: HF delimiter id `meta<n>` | Code emits the literal `meta:hf` | Ch 4 footnote |
| j | `verification_profiles.py` module comment: "California first" | `classify_finding_profile` checks **internal-coordination first** | Ch 9 footnote |
| k | git milestone "601 → 448 tests" vs. current suite | Current tree has **396** `def test_` functions | Ch 17 (reconciled, edit #2); Ch 15 footnote |
| l | `submit_verification_batch` inline comment says "32k" | Operative cap `VERIFICATION_OUTPUT_CAP = 16_000` | Ch 6 |
| m | "How to Use" GUI dialog promises run **resume** | Batch flow is forward-only; no resume entry point | Ch 13 |
| n | calibration `README.md`: "both [evals] should be green" | `fp_overconfident_numeric_swap` is a deliberate miss → calibration runner exits non-zero by design | Ch 15 footnote |

### Reconciliation update (2026-06)

Several of the `CLAUDE.md` drifts catalogued above have since been fixed directly
in `CLAUDE.md`, and one (m) was closed in the *code*; the chapter footnotes that
flagged them were updated to match. Current status:

- **b, c, e, f, g — resolved in `CLAUDE.md`.** The high-level-flow line now reads
  "sequential after verification"; `triage.py` is labeled "(automatic; needs API
  key)"; `reviewer.py` is described as the client factory + `Finding` model +
  tool-use/JSON parsing (not "streaming"); `session.py` is described as the recorder
  lifecycle helpers (the `TraceSession` naming is gone); and the `docx_fixtures.py`
  line was corrected to "tests build DOCX inline."
- **m — closed in code.** A review-batch resume / recovery subsystem now exists
  (`orchestration/batch_resume.py`, the GUI **Recover batch…** action, and
  `scripts/recover_batch.py`), so the "How to Use" dialog's resume promise is now
  kept. Ch 6 and Ch 13 were updated.
- **a, h, k — counts moved.** The tree now holds **59** `.py` files (49 app + 10
  `__init__`) after `batch_resume.py`; the suite is **49 test files / ~645 `def
  test_` functions** (was 26 / ~396 at assembly). Ch 2 and Ch 15 were updated.
- **d — still accurate, now stated in the docs.** The cross-check model has no
  `SPEC_CRITIC_*` override (`CROSS_CHECK_MODEL_DEFAULT` is bound directly); `README.md`
  and `CLAUDE.md` now say so explicitly instead of "all overridable."
- **i, j, l, n and the `conftest.py` guard (h) — unchanged source-side residue**,
  still as described above.

The historical record in §1–§2 and §5 is left as-authored; only this status note
and the affected chapter footnotes were updated.

---

## 4. Unresolved issues for a human

- **No content stubs, no off-scope chapters, no unresolvable contradictions, and
  no dangling cross-references** were found. The handbook is internally consistent.

- **The stale statements in `HANDBOOK_PLAN.md` §6/§7 and `CLAUDE.md` themselves are
  not edited by this pass.** The plan explicitly says agents do not edit the plan,
  and `CLAUDE.md` is the project's living instruction file. A maintainer who wants
  the *docs* to match the *code* should update, in particular: the "parallel with
  verification" high-level-flow line, the triage "(opt-in)" labels, the "56 source
  files" figure and the "all overridable" model-stack claim, the
  `docx_fixtures.py` reference, and the `session.py`/`TraceSession` description
  (items b, c, a, d, g, f above). The chapters already document the correct
  behavior, so the handbook is safe to read today regardless. **Update (2026-06):**
  items b, c, e, f, g have since been applied to `CLAUDE.md`, and the resume gap (m)
  was closed in code — see the *Reconciliation update* at the end of §3.

- **Open *codebase* work (not handbook issues).** The audits' verify-first /
  fix-first items — Structural P0-1 (surface partial failure in the report/UI),
  Trust P0-1/P0-2 (multi-file sidecar fan-out), Trust P0-3/P0-4 (model-whitelist
  staleness and the hardcoded 300k beta header), P0-5/P0-6 and Structural P1-2
  (prove batch grounding parity, extraction completeness, the fallback handoff) —
  are real open work in the program, fully documented in Ch 16 and Ch 17. They are
  listed here only so they are not mistaken for assembly gaps; they require code
  changes, not editorial ones.

- **The permanent caveat that outranks every bug** (stated in Ch 10/16/17): a
  `VERIFIED_SUPPORTED` verdict proves the cited source is *real*, not that it
  *proves the claim*. Human spot-checking of `VERIFIED_*` findings remains
  warranted. This is a property of the domain, not a defect to close.

---

## 5. Final stats

- **Chapters:** 18 files — front matter (Ch 0) + Ch 1–17 — all present, none stub,
  all H1 titles matching §3.
- **Total length:** **81,010 words** across the 18 chapters (**82,105** including
  `README.md`) — roughly 80–90 pages equivalent, within the plan's §2 target.
- **Output form:** **Linked set + index.** New `README.md` master TOC with the six
  Part dividers (Part I–VI as headers) and relative links to all 19 files, plus the
  reading paths. Exactly one canonical navigation TOC (the README); Ch 0 retains
  its non-clickable in-preface orientation table and its reading guide.
- **Cross-reference links created:** **411** (316 titled + 95 bare), all validated
  to resolve, no double-wraps, no over-matches, no dangling targets.
- **Consistency edits:** 4 content fixes (Ch 2 ×2 spots, Ch 6 ×2 spots, Ch 11,
  Ch 17) + the repo-wide cross-reference linking. No scope trims required. No
  chapter substance rewritten; no chapter file deleted.
