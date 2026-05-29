# Agent Prompt — Chapter 14: Observability — Tracing & Diagnostics

**Full title:** *Observability: Tracing & Diagnostics*

## Your mission
Explain how the program lets an engineer **reconstruct what actually happened** in
a run: the forensic JSONL **trace** (what the model saw, what it produced, how the
pipeline interpreted it), the single-file HTML **viewer** and the **CLI** that
read it, and the in-memory **`DiagnosticsReport`**. The defining property of this
subsystem is that it's a *silo*: it observes everything and changes nothing —
tracing can be turned off and the findings, report, and diagnostics summary are
byte-identical. Explain why that guarantee matters and how it's enforced.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (trace files), §7 (trace/span/event, diagnostics
   report).
2. `README.md` — the "Agent Tracing" section in full (files, env vars, GUI,
   viewer, CLI, silo guarantees) and `CLAUDE.md` §8 trace env vars + the tracing
   block of the source layout.
3. Source you own:
   - `src/tracing/` — `config.py` (env parsing + capture-level enum),
     `session.py` (`TraceSession`, the per-run dir + `run.json` writer),
     `recorder.py` (`TraceRecorder` global singleton, start/stop),
     `spans.py` (`SpanHandle` + span-kind constants), `capture_hooks.py`
     (defensive integration hooks that never escape to the pipeline),
     `redaction.py` (API-key / bearer-token redaction), `cli.py` (list/show/
     prune), `__main__.py`, and `viewer/trace_viewer.html` (the zero-build viewer
     — describe its four views; don't dissect the HTML).
   - `src/orchestration/diagnostics.py` — `DiagnosticsReport`, `DiagnosticEvent`,
     the scrubbing/bounding helpers (`_scrub_value`, `_scrub_and_bound`,
     `_bound_event_data`, `bound_structured_payload`), `summary()`.
4. `STRUCTURAL_AUDIT.md` P2-4 (the delayed TraceRecorder reset / cross-run bleed
   window — tracing only).

## In scope (what you own)
- **Why a forensic trace.** The motivating problem: when a verdict looks wrong or
  a finding lands in an unexpected status, you need to see *what the model saw and
  did*, after the fact, without re-running. Tie to the trust throughline:
  observability is how you audit the auditor.
- **The trace files.** `run.json` (run metadata; `run_id` matches
  `DiagnosticsReport.run_id`), `spans.jsonl` (nested spans:
  `pipeline → review/cross_check/verification_initial → api_call → web_search`),
  `events.jsonl` (typed events: `thinking_block`, `tool_use`, `web_search_query`/
  `result`, `pause_turn`, `parse_attempt`, `grounding_outcome`,
  `escalation_decision`, `budget_exhausted_marker`, …), `prompts.jsonl`
  (content-deduped by SHA-256 at default level), `findings.jsonl` (terminal
  finding snapshots with all verification telemetry). Explain default vs. **deep
  mode** (deep inlines prompts and full snippet bodies).
- **The recorder, sessions & hooks.** The global `TraceRecorder` singleton;
  `TraceSession` per-run directory; the **capture hooks** that integrate with the
  pipeline *defensively* — a hook failure never escapes into pipeline code (a
  first-of-kind warning logged once per exception-type/frame, then suppressed).
- **Redaction.** API keys and bearer tokens scrubbed before serialization (shared
  regex with diagnostics).
- **The viewer & CLI.** The single-file HTML viewer's four views (By Finding,
  By Span, Timeline, Search/Grounding) with report-matching colors/glyphs; the
  `python -m src.tracing` CLI (`list` / `show` / `prune`, `--trace-dir`).
- **The diagnostics report.** `DiagnosticsReport` as the *in-memory* operational
  report (distinct from the Word report's Run Diagnostics banner in Ch 11): event
  recording, the **scrub-and-bound** discipline (string truncation + byte bounds
  so a runaway payload can't blow up memory), and `summary()`.
- **The silo guarantees.** Tracing never alters `Finding`/`ReviewResult`/
  `VerificationResult`/`DiagnosticsReport` shape; `summary()` is byte-identical
  with tracing on or off; hook failures are contained. Explain *how* this is
  achieved (read existing state, never mutate it).

## Explicitly OUT of scope (owned elsewhere)
- The GUI tracing toggles / "Show folder" / "Open viewer" buttons → **Ch 13**
  (you own what they observe; Ch 13 owns the widgets).
- The Word report's Run Diagnostics banner → **Ch 11** (clarify the difference:
  that banner is in the deliverable; `DiagnosticsReport` is the in-memory ops
  log).
- Pipeline mechanics being traced → their owning chapters (you describe the
  *observation*, not the observed).

## Narrative beats to hit
- *Observability as a trust instrument.* For a tool whose findings drive
  compliance decisions, "why did it say that?" must be answerable later. The
  trace is that answer.
- *The discipline of non-invasiveness.* The hard engineering here is making a
  pervasive capture layer that is *guaranteed* not to change behavior or crash the
  run. Tell the story of the defensive hooks, the byte-identical-summary
  guarantee, and the scrub-and-bound caps.
- *Honest edge.* The delayed recorder reset (Audit P2-4): the global singleton is
  reset on the UI reset ~2.5s after completion, so a very fast second run could
  enqueue late worker-thread events into the prior run's recorder — *trace and
  diagnostics only, never findings or the report.* Present it scoped and low-risk,
  with the obvious fix (stop the recorder synchronously at completion).

## Invariants & facts you MUST get right
- Five trace files (names + contents per §6 / README).
- Default-on; `SPEC_CRITIC_TRACE` / `SPEC_CRITIC_TRACE_DEEP` / `SPEC_CRITIC_TRACE_DIR`.
- `run_id` matches `DiagnosticsReport.run_id`.
- Capture-hook failures never escape; first-of-kind warning then suppressed.
- `summary()` byte-identical with/without tracing.
- Redaction happens before serialization.

## Diagrams & tables
- A **span nesting tree** diagram (pipeline → … → web_search).
- A table: trace file → contents → default vs. deep behavior.
- A table of event types → what each captures.

## Cross-references to make
- To **Ch 13** (GUI toggles), **Ch 11** (the distinct Run Diagnostics banner),
  **Ch 9/10** (the routing/grounding/escalation events being traced), **Ch 16**
  (P2-4).

## Deliverable
- Write to **`handbook/14_observability.md`**. H1 = the full title. Target
  **3,000–4,500 words**.

## Quality bar
- A reader can use a trace to debug a verdict and understands the silo guarantees
  and *why* they hold. File/event facts match README §"Agent Tracing" and the
  source. The trace-vs-report-banner distinction is clear.
