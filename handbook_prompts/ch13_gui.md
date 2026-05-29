# Agent Prompt — Chapter 13: The Desktop GUI & Its Controller Architecture

**Full title:** *The Desktop GUI & Its Controller Architecture*

## Your mission
Explain the only part of the program a user actually touches: the CustomTkinter
desktop app. Cover the app shell, the **seven thin controllers** that bridge
widgets to the pipeline, and — most importantly — the **threading and epoch
model** that keeps a long, batch-driven run from freezing the UI or letting a
stale run's results bleed into a new one. The audit found this layer's threading
*sound*; explain why.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (facts), §7 (glossary).
2. `CLAUDE.md` — the `gui/` source-layout block (the app shell + 7 controllers +
   widgets + dialogs) and the README "GUI" / "Tracing → GUI" sections.
3. Source you own:
   - `main.py` — the PyInstaller-aware entry point.
   - `src/gui/gui.py` — `SpecReviewApp` shell, UI construction, the run lifecycle
     methods, the epoch helpers (`_next_run_epoch`, `_dispatch_if_current`), the
     tracing row toggles, drag-and-drop.
   - The seven controllers: `batch_controller.py`, `context_controller.py`,
     `diagnostics_controller.py`, `file_selection_controller.py`,
     `report_controller.py`, `review_run_controller.py`,
     `token_analysis_controller.py`.
   - `src/gui/widgets.py` — reusable components.
   - `src/gui/about_usage_dialogs.py` — About / API-usage dialogs.
4. `STRUCTURAL_AUDIT.md` — "GUI threading is sound" (verified-clean), P0-1 (the UI
   shows a green checkmark + "success" even when specs failed review), P2-4
   (TraceRecorder global reset is delayed ~2.5s, a cross-run trace-bleed window —
   tracing only, never findings).

## In scope (what you own)
- **The app shell.** `SpecReviewApp`: how the window is built (inputs card, the
  run controls, progress, the tracing row, font scaling), drag-and-drop spec
  selection, and project-context attachment. The PyInstaller entry (`main.py`,
  the `sys._MEIPASS` branch) — how the app ships as a desktop binary.
- **The controller pattern.** Why the GUI is split into seven *thin* controllers
  that bridge widgets to the orchestration spine, keeping `gui.py` a shell. One
  paragraph each on what each controller does:
  - `file_selection_controller` — choosing/validating spec files.
  - `context_controller` — the project-context box + attachments.
  - `token_analysis_controller` — preflight token counts shown in the UI.
  - `review_run_controller` — the run lifecycle (validate → start → complete /
    error → reset), recorder start.
  - `batch_controller` — submit thread, poll, progress, collect.
  - `report_controller` — exporting the report to a file.
  - `diagnostics_controller` — wiring diagnostics logging/progress.
- **Threading & the epoch model — the centerpiece.** The worker runs off-thread;
  *all* widget updates marshal back through `app.after(0, …)`; an **epoch
  staleness guard** (`_dispatch_if_current`) drops callbacks from a superseded
  run; `is_processing` blocks concurrent runs. Explain why this is the correct
  pattern for Tk (no off-thread widget access) and how it prevents cross-run
  result bleed.
- **The run from the UI's side.** Button press → validate → submit batch thread →
  poll with progress → collect → export → reset — the same sequence Ch 3 narrates
  at the data level, here at the *interaction* level (focus on threading and
  state, not pipeline mechanics).
- **The tracing row.** The two checkboxes (record / deep) that set env vars at run
  start, "Show folder," "Open viewer" (defer trace internals to Ch 14).

## Explicitly OUT of scope (owned elsewhere)
- Pipeline mechanics (extract/review/verify/finalize) → **Ch 3/7/etc.** (you
  drive them; you don't re-explain them).
- Tracing internals, the viewer, the CLI → **Ch 14**.
- The report/sidecar contents → **Ch 11** (you trigger the export).
- Token preflight math → **Ch 12**.

## Narrative beats to hit
- *Thin GUI, fat spine.* The controllers exist so the UI carries almost no logic —
  it's a presenter over the orchestration layer. This is what makes the pipeline
  testable headlessly (the test suite never needs Tk for the core).
- *The hard part is time, not pixels.* A run takes 45 min–2 hr on a background
  service; the UI must stay responsive, must not touch widgets off-thread, and
  must not let a user's second run get contaminated by the first. Tell the epoch
  guard as the elegant answer, and note the audit *confirmed* it sound.
- *Honest edges.* The terminal-state honesty gap (Audit P0-1): the UI finalizes
  as "success" with a green check even when some specs failed review — a partial
  failure should arguably surface as "Completed with errors" (the data exists in
  `truncated_specs`; cross-ref Ch 7/11). And the delayed recorder reset (P2-4): a
  ~2.5s window where a fast second run could enqueue trace events into the prior
  run's recorder (trace/diagnostics only — never findings or the report).

## Invariants & facts you MUST get right
- All widget updates marshal via `app.after(0, …)`; epoch guard drops stale
  callbacks; `is_processing` blocks concurrent runs (threading is sound).
- Seven controllers (name them correctly).
- Tracing toggles set env vars at run start (take effect without restart);
  default record-on, deep-off.
- The UI currently shows success/green regardless of review-stage errors (P0-1).

## Diagrams & tables
- A diagram: `gui.py` shell ↔ seven controllers ↔ orchestration spine.
- A **threading swimlane**: Tk main thread vs. worker thread vs. batch service,
  with `app.after` marshaling and the epoch check.
- A table: controller → responsibility → key entry points.

## Cross-references to make
- To **Ch 3** (the run flow it drives), **Ch 7** (the spine + failed-spec data),
  **Ch 11** (export + the terminal-state honesty gap), **Ch 12** (preflight/token
  display), **Ch 14** (tracing internals + the recorder-reset edge), **Ch 16**
  (P0-1/P2-4).

## Deliverable
- Write to **`handbook/13_gui.md`**. H1 = the full title. Target
  **3,000–4,500 words**.

## Quality bar
- A reader understands the thin-controller design and, especially, the
  threading/epoch model and *why* it's correct. Controller list and threading
  facts match the source. Honest edges noted, scoped to GUI.
