# Agent Prompt — Chapter 4: Input

**Full title:** *Input: Extraction, Element IDs & the Deterministic Pre-Screen*

## Your mission
Explain how raw `.docx` files become structured, reviewable text — and how a
layer of **deterministic, no-API detectors** catches a whole class of defects
*before a single token is sent to Claude*. This is the front door of the
pipeline and the first expression of a core philosophy: **do the cheap,
certain checks locally; save the model for the judgment calls.**

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (facts), §7 (glossary), §8 (template).
2. `CLAUDE.md` — "§5 Deterministic Pre-Screen" table, "DOCX content-loss
   warning," "Deterministic-rule ids are public," "Stale-cycle suppression
   window."
3. Source you own:
   - `src/input/extractor.py` — `extract_text_from_docx`, the element-id scheme,
     `_detect_content_loss_warning`, `ExtractedSpec`, `ParagraphMapping`, PDF/
     context extraction.
   - `src/input/extraction_cache.py` — the LRU cache keyed by mtime + content
     fingerprint; thread-safety; deep-copy returns.
   - `src/input/preprocessor.py` — all the detectors and `preprocess_spec`,
     `PreprocessResult`, the `deterministic_rule` ids, stale-vs-invalid logic,
     `_should_suppress_stale_cycle`.
4. `TRUST_AUDIT.md` P0-6 (extraction completeness) and P2-1 (ASCE 7 pre-2005);
   `STRUCTURAL_AUDIT.md` "Extraction cache key is robust" (verified-clean).

## In scope (what you own)
- **Extraction.** How paragraphs, tables, and headers/footers are pulled from
  `.docx`; the stable **element-id scheme** (`p7`, `t0r2`, `s1h0`, …) and why
  stable ids matter downstream (edit targeting, evidence panels). PDF and
  project-context extraction. The parallelized extraction of multiple specs.
- **The content-loss warning.** The 20% non-text-element threshold (strict `>`),
  what it counts (drawings/pictures/OLE objects), the warning string, and *why*
  it exists (a drawing-heavy spec may have un-extracted requirements). Note it's
  surfaced later in the Run Diagnostics banner (defer rendering to Ch 11).
- **The extraction cache.** The cache key (`resolved_path, st_size, st_mtime_ns,
  head+tail SHA-256`), LRU bounding, locking, deep-copy returns — and why a
  changed file is never served stale.
- **The deterministic pre-screen.** Walk the **nine detectors** (the
  `CLAUDE.md` §5 table): LEED, placeholders, template markers, stale vs. invalid
  code cycles, empty sections, duplicate headings, duplicate paragraphs,
  inconsistent filenames. Explain the **stale vs. invalid** distinction (real
  historical cycle vs. fabricated year — disjoint by construction). Explain the
  **stale-cycle suppression window** (the ±80-char negation/historical-term scan,
  the sentence-terminator narrowing, why bare "not" is intentionally *not* a
  suppressor). Explain that each alert carries a stable `deterministic_rule` id
  and that these ids are *public* — the verification router recognizes them.

## Explicitly OUT of scope (owned elsewhere)
- How detector alerts are *rendered* in the report (the "(deterministic check)"
  section) → **Ch 11**.
- How `deterministic_rule` ids drive verification local-skip routing → **Ch 9**.
- The `CodeCycle` dataclass / pinned-edition data → **Ch 12** (you may reference
  which cycle is current).
- The review prompt and findings → **Ch 5**.

## Narrative beats to hit
- *Why local-first*: cost, determinism, and the principle that you should never
  pay a model to find a literal `TODO:`. The pre-screen is fast, free, and
  perfectly reliable on the things it can see.
- *Design tension*: completeness vs. noise. The strict `>` threshold and the
  suppression window are both tuned to avoid false alarms on every run. Tell the
  story of why bare "not" is excluded and why borderline 20% specs stay quiet.
- *The honest edge*: extraction completeness (Audit P0-6) — python-docx body
  iteration can miss text in headers/footers, text boxes (`w:txbxContent`),
  footnotes, and grouped shapes; the content-loss warning covers
  drawing-heavy specs but not all text-bearing parts. And ASCE 7 pre-2005
  editions fall outside the "plausible" set (P2-1). Present these as known,
  bounded gaps, and note the LLM review is a backstop.

## Invariants & facts you MUST get right
- Content-loss threshold is `> 0.20` (strict) — *not* an off-by-one (the audit
  confirmed `<= threshold: return None` is correct as designed).
- Stale = real historical cycle; invalid = fabricated year; disjoint.
- The extraction cache returns **deep copies** and is thread-safe.
- Element-id format exactly as in §6.

## Diagrams & tables
- The **nine-detector table** (`deterministic_rule` → what it catches → example).
- A small diagram: `.docx` → extractor → `ExtractedSpec` (+ element-id map +
  warnings) → preprocessor → `PreprocessResult` (alerts).
- A snippet of the element-id scheme on a tiny example document.

## Cross-references to make
- To **Ch 9** (rule ids → local-skip), **Ch 11** (alert rendering + content-loss
  banner row), **Ch 12** (cycle data), **Ch 16** (the extraction-completeness
  audit item).

## Deliverable
- Write to **`handbook/04_input.md`**. H1 = the full title. Target
  **3,000–4,500 words**.

## Quality bar
- A reader understands exactly what the model does and does not "see," why the
  detectors are deterministic, and where the extraction edges are. Detector
  table matches `CLAUDE.md` §5 and the source.
