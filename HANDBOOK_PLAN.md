# The Spec Critic Engineer's Handbook — Master Plan

> This document is the **production plan and shared contract** for writing
> *The Spec Critic Engineer's Handbook*: an extremely detailed, book-length
> technical narrative of the Spec Critic program — what it is, why it exists,
> how every part works, how the pieces fit, the flow from start to finish, the
> problems it solves, the challenges that were overcome, and the work that is
> still being perfected.
>
> The handbook is too large for a single authoring session. It is decomposed
> into **17 chapters plus front matter**, each written by a separate agent
> working **in parallel** from a dedicated prompt file in `handbook_prompts/`.
> **Every agent reads this plan first.** It is the single source of truth for
> scope boundaries, terminology, shared facts, voice, structure, and the
> assembly contract. If an agent's prompt and this plan ever disagree, this
> plan wins — except that the *source code itself always wins over both.*

---

## 1. Vision & reader

**What the handbook is.** A "story so far" of the Spec Critic codebase, written
as a *blended engineering handbook and narrative*. It must do four things at
once:

1. **Teach the system** deeply enough that a new engineer can navigate, modify,
   and trust the code.
2. **Explain the structure** — every script, every subsystem, and how each fits
   into the whole.
3. **Trace the flow** from `.docx` input to Word report + JSON sidecar output.
4. **Tell the story** — the problems being solved, the inherent difficulty of
   solving them, the challenges the team hit and how they were overcome, and the
   honest edges where the program is still being perfected.

**Primary reader.** A competent software engineer who is *new to this codebase*
and to the *California DSA mechanical/plumbing spec-review domain*. They have
general engineering skill but no prior context on either. The book should leave
them able to reason about the system and its trade-offs — not just recite its
parts.

**Voice.** Blended handbook + narrative. Authoritative and precise like good
internal engineering docs, but with a through-line and a point of view: *why*
the code is shaped the way it is, what was hard, what bit the team, and what is
still imperfect. Avoid marketing tone. Avoid breathless hype. Write like a
senior engineer explaining the system to the engineer who will inherit it.

**The book's throughline: trust.** Spec Critic is a compliance-review tool. A
tool that is *confidently wrong* about a building code is worse than no tool.
Almost every design decision in this codebase — deterministic pre-screening,
evidence-grounded verification, the emit-but-don't-apply stance, the nine-label
trust model, the forensic trace — exists to make uncertainty *visible* rather
than hidden. Keep returning to this thread.

---

## 2. Global writing parameters (decided — do not relitigate)

| Parameter | Decision |
|---|---|
| **Audience & voice** | Blended handbook + narrative (teach the system *and* tell the story). |
| **Depth** | Deep: target **3,000–5,000 words per chapter**. Front matter (Ch 0) may be shorter (~1,500–2,500). |
| **Illustration** | **Prose + diagrams, minimal code.** Use ASCII or Mermaid diagrams, tables, and the *occasional* short illustrative snippet (a function signature, a data shape, a representative tool-schema fragment). **No line-by-line code walkthroughs. No long verbatim source dumps.** When you must show code, keep it under ~15 lines and use it to illustrate a *concept*, not to reproduce the file. |
| **Structure** | 17 numbered chapters + front matter, grouped into six Parts (below). |
| **Length of book** | ~60–90 pages equivalent. |

---

## 3. Table of contents (canonical)

Chapter numbers and titles below are **canonical**. Use these exact titles and
numbers in cross-references throughout the book.

**Front Matter**
- **Ch 0 — Preface & How to Read This Handbook** *(owns the reader-facing glossary)*

**Part I — The Problem & The Shape**
- **Ch 1 — The Problem Domain: California DSA Mechanical & Plumbing Spec Review**
- **Ch 2 — Architecture at a Glance: Subsystems, Dependencies & the Core Data Model**
- **Ch 3 — A Run, End to End: Following the Data from `.docx` to Report**

**Part II — Ingestion & Review**
- **Ch 4 — Input: Extraction, Element IDs & the Deterministic Pre-Screen**
- **Ch 5 — The Review Engine: Prompts, Schemas & the Anthropic Client**
- **Ch 6 — Batch Processing: The Message Batches Backbone**

**Part III — Coordination & Verification**
- **Ch 7 — Orchestration & State: The Pipeline Spine**
- **Ch 8 — Cross-Spec Coordination**
- **Ch 9 — Verification I: How We Decide to Check (Routing, Modes, Profiles, Triage)**
- **Ch 10 — Verification II: How We Check & Judge (Grounding, Verdicts, Escalation, Cache)**

**Part IV — Output & Trust**
- **Ch 11 — The Trust Model & Report Output: Status Labels, the Word Report & the Edit Sidecar**

**Part V — Cross-Cutting Systems**
- **Ch 12 — Configuration, Models & Token Economics**
- **Ch 13 — The Desktop GUI & Its Controller Architecture**
- **Ch 14 — Observability: Tracing & Diagnostics**
- **Ch 15 — Quality Engineering: Testing & Calibration**

**Part VI — The Meta-Story**
- **Ch 16 — Trust Under the Microscope: The Audits**
- **Ch 17 — Evolution & Lessons: The v3.0.0 Pivot and the Road Ahead**

---

## 4. Source-file ownership map (prevents overlap)

Each source file has **exactly one owning chapter** — the chapter responsible
for explaining it in depth. Other chapters may *mention* a file but must defer
its mechanics to the owner via a cross-reference.

| Source | Owning chapter |
|---|---|
| `README.md`, `src/__init__.py`, overall tree | Ch 1 (domain), Ch 2 (structure) |
| `main.py` | Ch 13 (entry point), mentioned in Ch 3 |
| `src/input/extractor.py`, `extraction_cache.py`, `preprocessor.py` | Ch 4 |
| `src/review/reviewer.py`, `review_request_builder.py`, `structured_schemas.py`, `prompts.py`, `prompt_serialization.py` | Ch 5 |
| `src/batch/batch.py`, `batch_runtime.py` | Ch 6 |
| `src/orchestration/pipeline.py` | Ch 7 |
| `src/cross_check/cross_checker.py` | Ch 8 |
| `src/verification/verification_prescreen.py`, `verification_profiles.py`, `verification_modes.py`, `verification_routing.py`, `triage.py` | Ch 9 |
| `src/verification/verifier.py`, `source_grounding.py`, `verification_cache.py`, `retry_policy.py` | Ch 10 |
| `src/output/report_status.py`, `report_exporter.py`, `edit_sidecar.py` | Ch 11 |
| `src/core/api_config.py`, `code_cycles.py`, `tokenizer.py`, `api_key_store.py`, `app_paths.py` | Ch 12 |
| `src/gui/*` (10 files) | Ch 13 |
| `src/tracing/*` (10 files), `src/orchestration/diagnostics.py` | Ch 14 |
| `tests/*`, `evals/*` | Ch 15 |
| `TRUST_AUDIT.md`, `STRUCTURAL_AUDIT.md` | Ch 16 |
| `RELEASE_NOTES.md`, changelog, git history, working agreements | Ch 17 |

**The core data model is shared.** The dataclasses (`Finding`, `EditProposal`,
`ReviewResult`, `VerificationResult`, `PipelineResult`, `FindingGroup`,
`FindingOccurrence`, `ExtractedSpec`, `VerificationRoutingDecision`,
`PreprocessResult`, `DiagnosticsReport`) are *introduced as a map* in **Ch 2**
(what each carries, how they relate, one data-flow diagram). **Full field-level
semantics** belong to the owning chapter of the file where the dataclass is
defined (e.g. `Finding`/`EditProposal` detail → Ch 5; `VerificationResult`
detail → Ch 10). Ch 2 must explicitly say "see Ch N for full detail."

---

## 5. Parallel workflow & assembly

1. **Phase A — parallel authoring.** Each chapter agent reads this plan + its
   prompt in `handbook_prompts/`, reads its owned source files (and the listed
   `CLAUDE.md` / audit sections), and writes its chapter to
   **`handbook/<NN>_<slug>.md`** (e.g. `handbook/04_input.md`). Agents do **not**
   edit each other's files and do **not** edit this plan.
2. **Phase B — assembly.** A final integration pass concatenates chapters in
   order into `handbook/THE_SPEC_CRITIC_HANDBOOK.md` (or a linked set), inserts
   the TOC, normalizes cross-reference links, and resolves any terminology or
   fact drift against §6–§7 below.
3. **Conflict rule.** Source code > this plan > individual prompts. If an agent
   finds the code contradicts a "fact" stated here or in `CLAUDE.md`, the agent
   writes what the **code** says and adds a short footnote noting the
   discrepancy (this is exactly the kind of drift the audits care about).

**Output path convention:** `handbook/<two-digit-number>_<short_slug>.md`.
Front matter is `handbook/00_preface.md`. Use H1 for the chapter title, H2/H3
for sections.

---

## 6. Shared facts sheet (keep these consistent across all chapters)

These are load-bearing facts. Use them verbatim. **Where a value is marked
"(verify in source)", cite the actual value from the named file rather than
trusting memory** — versions and model ids drift.

- **Product:** Spec Critic, **v3.0.0**. A Python 3.11+ desktop app
  (CustomTkinter) for reviewing California K-12 DSA **mechanical & plumbing**
  CSI-format `.docx` specifications.
- **Codebase size:** ~22k lines of `src/`, ~10k lines of tests, ~2.3k lines of
  evals; 56 source files across 8 packages (`core`, `input`, `review`, `batch`,
  `orchestration`, `cross_check`, `verification`, `output`, plus `gui` and
  `tracing`).
- **Default code cycle:** `CALIFORNIA_2025` (`DEFAULT_CYCLE`). The 2022 cycle
  was removed and must not be reintroduced.
- **Codes reviewed against:** CBC, CMC, CPC, California Energy Code (Title 24),
  CALGreen, ASCE 7, plus adopted editions of NFPA 13/14/20/24/25/72,
  ASHRAE 62.1/90.1/15, IAPMO Uniform Plumbing Code, and UL listings
  (UL 300/555/555S/268/1479).
- **Model stack (defaults, all overridable by env var; exact id strings live in
  `api_config.py` — verify in source):** Review = Opus 4.7; Cross-check =
  Sonnet 4.6; Verification initial = Sonnet 4.6; Escalation / deep-reasoning =
  Opus 4.7; Triage = Haiku 4.5.
- **Model capability whitelist** covers Opus 4.7, Sonnet 4.6, Haiku 4.5.
  **Unknown model ids degrade to safe defaults** that disable every capability
  flag (smaller request, never an API rejection).
- **Processing:** all reviews go through the **Message Batches API** (~50% cost
  savings; typical turnaround ~45 min – 2 hr; 24 hr max). The **300k extended
  output** path is batch-only (`output-300k-2026-03-24` beta header) and fires
  only for inputs ≥200k tokens.
- **Web fetch is generally available and takes NO `anthropic-beta` header.** A
  retired `web-fetch-2026-02-09` header once caused HTTP 400 crashes on the
  common path; this is a recurring cautionary tale (Ch 10, Ch 17).
- **Output caps** (`api_config._PHASE_OUTPUT_BUDGET`, clamped per model):
  Review / batch review = 128k; Extended batch review = 300k; Cross-check = 96k;
  Verification (+retry/continuation) = 16k; Triage = 8k.
- **Context limits** (`tokenizer.py`): `MAX_CONTEXT_TOKENS = 1,000,000`;
  `RECOMMENDED_MAX = 500,000` (per-spec input — preflight *raises*);
  `CROSS_CHECK_RECOMMENDED_MAX = 822,000`.
- **Verification search budget (`api_config._SEVERITY_MAX_USES`):** CRITICAL=8,
  HIGH=7, MEDIUM=5, GRIPES=3 (flat across profiles).
- **Four verification modes:** `local_skip`, `strict_structured`,
  `standard_reasoning`, `deep_reasoning`.
- **Five verification profiles:** `california_ahj`, `code_standard`,
  `manufacturer`, `constructability`, `internal_coordination`.
- **Nine `ReportStatus` labels:** `VERIFIED_SUPPORTED`, `VERIFIED_CONTRADICTED`,
  `DISPUTED`, `INSUFFICIENT_EVIDENCE`, `LOCALLY_CLASSIFIED`, `NOT_CHECKED`,
  `MANUAL_REVIEW_REQUIRED`, `VERIFICATION_FAILED`, `VERIFIED_CONTESTED`.
- **Two `EditActionLabel` values:** `EDIT_SUGGESTED`, `REPORT_ONLY`.
- **Nine deterministic detector rules** (`deterministic_rule` ids): `leed_reference`,
  `placeholder`, `template_marker`, `stale_code_cycle` / `stale_asce7`,
  `invalid_code_cycle`, `empty_section`, `duplicate_heading`,
  `duplicate_paragraph`, `inconsistent_filename`.
- **The grounding invariant:** `CONFIRMED` / `CORRECTED` verdicts require at
  least one **accepted external citation** (a model-cited URL whose normalized
  form matched a URL the `web_search` *or* `web_fetch` tool actually retrieved).
  Enforced in three places (verifier `_apply_source_grounding`,
  `_enforce_grounding_invariant`, and `VerificationCache.put`).
- **Verification cache key:** `cycle_label | actionType | codeReference |
  sha256(claim_summary)[:24 hex]`. Omits the verifier model id. Default TTL = 60
  days. Refuses to persist ungrounded / `verification_failed` / `budget_exhausted`
  results.
- **Element id scheme:** every extracted element gets a stable id like `p7`
  (paragraph), `t0r2` (table 0, row 2), `s1h0` (section header).
- **Cross-check chunking by CSI division:** Div 21 / 22 / 23 / Controls / 25 + 01.
- **Emit-but-don't-apply:** Spec Critic emits structured edit *instructions*
  (rendered in the report and written to a `<report-stem>.edits.json` sidecar)
  but **never mutates spec documents**. The surgical write-back stack was removed
  in v3.0.0. Applying edits is a future, separate program's job.
- **Tracing** is default-on; writes `run.json`, `spans.jsonl`, `events.jsonl`,
  `prompts.jsonl`, `findings.jsonl` to `~/.spec_critic/traces/<run_id>/`.

**Do not confuse the model the *authoring agent* runs on with the models the
*app* configures.** The app's configured model stack (Opus 4.7 / Sonnet 4.6 /
Haiku 4.5) is legitimate handbook content. Never insert the authoring agent's
own model identity into any chapter.

---

## 7. Canonical glossary (use these terms consistently)

Domain:
- **DSA** — Division of the State Architect; the California authority that
  reviews/approves K-12 (and community college) construction documents.
- **HCAI** — Department of Health Care Access and Information (formerly OSHPD);
  the analogous AHJ for healthcare facilities.
- **AHJ** — Authority Having Jurisdiction.
- **M&P** — Mechanical & Plumbing (the spec disciplines this tool reviews).
- **CSI / CSI division** — Construction Specifications Institute MasterFormat;
  specs are numbered by division (21 fire suppression, 22 plumbing, 23 HVAC,
  25 integrated automation, 01 general).
- **Spec** — a `.docx` specification section (CSI-format).
- **Code cycle** — the dated set of adopted codes (here, California 2025) and
  the pinned editions of referenced standards.
- **Pinned editions** — the specific NFPA/ASHRAE/IAPMO/UL editions California
  adopted for the cycle; the model flags drift from these.

Data objects (introduced in Ch 2; detailed in owning chapters):
- **`ExtractedSpec`** — extracted text + element-id map + extraction warnings for
  one document.
- **`Finding`** — one issue the reviewer (or a detector) raised; carries severity,
  text, optional `EditProposal`, and (after verification) a `VerificationResult`.
- **`EditProposal`** — a structured edit (action / existing → replacement /
  anchor / target element id / confidence). *Emitted, never applied.*
- **`ReviewResult`** — the output of one review (or cross-check) call: findings +
  metadata + errors.
- **`VerificationResult`** — the verdict + grounding + telemetry for one finding.
- **`VerificationRoutingDecision`** — the policy bundle (mode/model/budget/tools)
  for verifying one finding.
- **`PipelineResult` / `CollectedBatchState`** — aggregate run state.
- **`FindingGroup` / `FindingOccurrence`** — multi-file grouping of the same
  defect across specs.

Process terms:
- **Pre-screen / deterministic detector** — local, no-API checks run before any
  model call; each carries a stable `deterministic_rule` id.
- **Review** — the per-spec Claude pass that produces findings.
- **Cross-check / coordination** — the cross-spec pass that finds defects
  spanning multiple specs.
- **Verification** — the web-search-backed pass that adjudicates a finding into a
  grounded verdict.
- **Grounding** — proving a verdict's cited URL was actually retrieved by a search
  tool. *Grounding proves the source is real, not that the source proves the
  claim* (an important trust caveat).
- **Verdict** — `CONFIRMED` / `CORRECTED` / `DISPUTED` / `UNVERIFIED` from the
  verifier.
- **Escalation** — re-running an uncertain finding on a stronger model (Sonnet→Opus).
- **Contested** — both verifiers grounded their verdicts but *disagreed*
  (`models_disagreed` → `VERIFIED_CONTESTED`).
- **Budget exhausted** — the verifier spent its full search budget without
  grounding (a sentinel, not a separate status).
- **Triage** — optional Haiku pre-classification of whether a finding needs web
  search.
- **Mode / profile** — verification routing dimensions (see §6).
- **Batch / wave / `custom_id`** — Message Batches API concepts; a wave is one
  submit→poll→collect cycle within verification.
- **Prompt cache / cache breakpoint** — Anthropic prompt caching; breakpoints
  must land in byte-stable positions.
- **Edit sidecar** — the `<report-stem>.edits.json` machine-readable feed for a
  downstream applier.
- **Trace / span / event** — the forensic JSONL observability layer.
- **Diagnostics report** — the in-memory operational health report
  (`DiagnosticsReport`).
- **Calibration eval** — the fixture-driven scoring harness in `evals/calibration/`.

---

## 8. Chapter template (every chapter follows this skeleton)

Adapt freely, but every chapter should contain these beats (not necessarily as
literal headings — weave them):

1. **Opening / thesis** — one or two paragraphs: what this part of the system is
   responsible for, and the *problem* it exists to solve. Hook the reader.
2. **How it works** — the mechanics, prose-first, with diagrams/tables and
   minimal illustrative code. This is the bulk.
3. **Design tensions & decisions** — the genuinely hard parts: the trade-offs,
   the things that bit the team, *why* it's built this way and not the obvious
   alternative, and how challenges were overcome.
4. **Edges & what's still being perfected** — known gaps, audit findings
   relevant to this subsystem, open questions. Be honest. (Pull from
   `TRUST_AUDIT.md` / `STRUCTURAL_AUDIT.md` where the prompt points you.)
5. **How it connects** — explicit cross-references to the adjacent chapters
   (upstream input, downstream consumers).
6. **Key takeaways** — a short bulleted recap.

Cross-reference style: write "see **Ch 10 — Verification II**" (use the
canonical title). The assembly pass converts these to links.

Diagram style: prefer simple ASCII boxes/arrows or Mermaid (` ```mermaid `).
Keep diagrams legible in plain text.

---

## 9. Quality bar (every chapter must clear this)

- **Accurate to the source.** Where you state a mechanism, it must match the
  code. When unsure, read the file; do not invent. Cite `file.py` (and a symbol
  name) so a reader can find it.
- **Right altitude.** Explain mechanisms and *why*, not line-by-line *how*. A
  reader should understand the subsystem without the source open, and know where
  to look when they open it.
- **In its lane.** Cover what you own; defer what you don't (per §4) with a
  cross-reference. No duplicated deep-dives.
- **Narrative present.** Include the problem, the difficulty, the decision, and
  the honest edge — not just a description of the current state.
- **Consistent terms & facts** per §6–§7.
- **Self-contained file** at `handbook/<NN>_<slug>.md`, H1 title, ~3–5k words.

---

## 10. Deliverables produced by this planning step

- `HANDBOOK_PLAN.md` (this file) — at repo root.
- `handbook_prompts/ch00_preface.md` … `handbook_prompts/ch17_evolution_and_lessons.md`
  — one prompt per chapter agent (18 files), at repo root in `handbook_prompts/`.

The chapter agents, when run, produce `handbook/<NN>_<slug>.md` (18 chapter
files), which the assembly pass stitches into the final handbook.
