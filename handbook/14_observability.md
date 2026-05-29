# Observability: Tracing & Diagnostics

A reviewer opens the Word report, scrolls to a CRITICAL finding about a Title 24
duct-insulation requirement, and sees the verdict: **Verified — supported**, green
check, one cited URL. They click through to the source. It does not say what the
finding claims. Now what?

This is the question the observability subsystem exists to answer. Spec Critic is
a compliance tool, and the throughline of this entire handbook is that a tool
which is *confidently wrong* about a building code is worse than no tool at all.
Every other subsystem fights that failure mode at the moment of judgment —
deterministic pre-screening, evidence grounding, the nine-label trust model.
Observability fights it *afterward*. When a verdict looks wrong, when a finding
lands in an unexpected status, when an operator simply does not believe the
machine, someone needs to reconstruct **what the model actually saw, what it
produced, and how the pipeline interpreted that output** — without re-running the
job. Observability is how you audit the auditor.

There are two artifacts. The first is a **forensic trace**: five JSONL files
written to disk per run, capturing the agent's invocations in enough detail to
replay the whole lifecycle of any finding offline. The second is the in-memory
**`DiagnosticsReport`**: an operational health log the app builds as it runs and
can dump as text. They share one identifier — the `run_id` — and almost nothing
else, and that separation is deliberate.

The defining property of this subsystem is that it is a **silo**. It observes
everything and changes nothing. Tracing can be switched off entirely and the
findings, the Word report, and the diagnostics summary are byte-for-byte
identical. The interesting engineering here is not the capture — capture is easy.
The hard part is building a pervasive observation layer that is *guaranteed* not
to alter behavior and *guaranteed* not to crash the run it is watching. This
chapter is mostly about how that guarantee is earned.

---

## Why a forensic trace, and not just logs

The naive answer to "why did it say that?" is: turn up the log level and run it
again. That does not work here, for two reasons that are specific to this system.

First, **re-running is expensive and slow.** Reviews go through the Message
Batches API; a verification pass with web search can take from forty-five minutes
to a couple of hours. Asking an operator to reproduce a questionable verdict by
re-submitting the batch is asking them to wait out a coffee-break-length feedback
loop for every doubt they have.

Second, **re-running is not reproducible.** The verifier calls a live model with
live web search. The web moves; the model is non-deterministic; the search tool
may retrieve different URLs on the second pass. The run you re-execute is not the
run you are explaining. The only faithful record of *that* run is the one captured
*during* it.

So Spec Critic captures once and inspects forever. The trace is written as the
run happens, lands on disk under `~/.spec_critic/traces/<run_id>/`, and survives
the process. Weeks later, with no API key and no network, an engineer can open
the trace and watch a single finding move from review through routing, search,
grounding, escalation, and verdict — and see precisely where reality and the
report diverged.

Tracing is **default-on**. The switches (`src/tracing/config.py`) follow the same
disable-token convention as the rest of the codebase, so an operator who knows how
to turn off any other Spec Critic flag already knows how to turn off tracing:

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_TRACE` | on | Disable with `0` / `false` / `no` / `off`. |
| `SPEC_CRITIC_TRACE_DEEP` | off | Any truthy value opts into deep mode (per-chunk streaming, full snippet bodies, inline prompts). Implies trace enabled. |
| `SPEC_CRITIC_TRACE_DIR` | `~/.spec_critic/traces/` | Override the trace root. `~` and `$VAR` are expanded. |

The capture level is read at *run start*, not import time, so a GUI checkbox that
just flipped the env var takes effect on the next run without a process restart
(see **Ch 13 — The Desktop GUI & Its Controller Architecture** for the widgets).
Deep mode is treated as a stronger signal of operator intent than the main flag:
if `SPEC_CRITIC_TRACE_DEEP` is truthy, tracing is enabled even if
`SPEC_CRITIC_TRACE` says disable.

---

## The five files

A trace directory is named for the run and contains five files at the default
capture level. Critically, the directory name **matches `DiagnosticsReport.run_id`**
— the GUI sources the recorder's `run_id` from the diagnostics report — so a trace
folder and its in-memory diagnostics counterpart are the same run by construction.

| File | Contents | Default vs. deep |
|---|---|---|
| `run.json` | Run metadata: `run_id`, mode, model, cycle label, files reviewed, capture level, started/ended timestamps, and a `resumed_at` list for batch runs that survived a restart. | Same. Written synchronously (it is small and read on every resume). |
| `spans.jsonl` | One line per *closed* span — one logical agent invocation. Spans nest via `parent_span_id`. | Same shape; deep spans carry inline prompts in their `inputs`. |
| `events.jsonl` | One line per point-in-time event, each tagged with its `span_id`. | Deep adds `stream_chunk` events and snippet bodies on search results. |
| `prompts.jsonl` | Content-deduped prompt bodies, referenced from spans by a 24-hex SHA-256 digest. | **Deep mode does not write this file at all** — prompts are inlined on each span instead, so the span replays self-contained. |
| `findings.jsonl` | One line per finding at its *terminal* state, snapshotted at run end, carrying every verification telemetry field. | Same. |

A few of these choices repay a closer look.

**Prompts are deduplicated by content.** The review system prompt, the verifier
system prompt, the pinned-editions block — these are large and byte-identical
across dozens of calls in a run. Writing them once and referencing them by hash
keeps the trace small. `prompt_ref(kind, text)` hashes the body to 24 hex
characters, writes it to `prompts.jsonl` the first time it sees that digest, and
returns `{"ref": <hash>}` for the span to store. In deep mode it skips the sidecar
and returns `{"inline": text}` so the span is fully self-contained for replay —
the deliberate trade is trace size for replay convenience.

**The on-disk span omits its events.** An `AgentSpan` carries an in-memory
`events` list for debugging, but the serialized span record intentionally leaves
it out: events stream to `events.jsonl` *as they fire*, carrying their own
`span_id`, and the viewer re-joins them by that key. This keeps a span line small
and lets events be written in real time rather than buffered until the span
closes.

**`ended_at` marks the end of the automated pipeline, not the report.** The
recorder is torn down when the pipeline (review → verification → cross-check →
finalize) completes. Report export and the file-save dialog happen afterward, on
the UI thread, behind open-ended user think-time — deliberately *outside* the
trace window, so `ended_at` does not absorb the minutes a user spends deciding
where to save. A `run.json` whose `ended_at` predates the report file's
modification time is therefore expected, not a sign of a truncated trace.

---

## Spans, events, and the nesting tree

The trace's data model has two primitives, defined in `src/tracing/spans.py`.

A **span** is one logical agent invocation with a duration: a review of one spec,
a verification call, a batch wave, an API round-trip. Spans nest through
`parent_span_id` to form a tree that mirrors the pipeline's call structure. An
**event** is a point-in-time marker *inside* a span: a thinking block, a tool
call, a parse decision, a grounding outcome. Spans answer "what ran, and how
long?"; events answer "what happened along the way?"

The canonical nesting, with point-in-time events hanging off whichever span is
active:

```
pipeline                              ← opened once at run entry (KIND_PIPELINE)
├── extraction
├── review  (per spec)
│   └── api_call
│        • thinking_block             (event)
│        • tool_use                   (event)
│        • parse_attempt              (event)
├── triage  (Haiku pre-classification, when it runs)
├── cross_check
│   └── cross_check_chunk  (per CSI division: 21 / 22 / 23 / Controls / 25 + 01)
│        └── api_call
└── verification_initial  (per finding)
    • cache_hit / cache_miss          (event)
    └── api_call → web_search
         • web_search_query           (event)
         • web_search_result          (event)
         • web_fetch_request/result   (event)
         • pause_turn                 (event)
    • grounding_outcome               (event)
    • escalation_decision             (event)
    └── verification_escalation       (Sonnet → Opus, when it fires)
         └── api_call
```

Span *kinds* are stable string constants (`KIND_PIPELINE`, `KIND_REVIEW`,
`KIND_VERIFICATION_INITIAL`, `KIND_VERIFICATION_ESCALATION`, `KIND_API_CALL`,
`KIND_WEB_SEARCH`, and a dozen more). The HTML viewer keys its layout and colors
on these, so adding a kind is a coordinated change with the viewer. The same file
documents a per-kind I/O contract — the expected shape of each kind's `inputs` and
`outputs` — but the recorder does **not** validate against it: the contract is a
shared understanding between the capture sites and the viewer, not an enforced
schema. That keeps a malformed capture from ever raising.

Events are an open, tagged vocabulary. The ones worth knowing:

| Event type | Captures |
|---|---|
| `thinking_block` | The model's extended-thinking text for a call. |
| `tool_use` | A custom-tool invocation (name + input). |
| `web_search_query` / `web_search_result` | A search query and the URL + title pairs it returned (snippet bodies only in deep mode). |
| `web_fetch_request` / `web_fetch_result` | A full-page fetch and its URL/title (content preview only in deep mode). |
| `parse_attempt` | A parse decision: structured / text-fallback / parse-error / incomplete. |
| `pause_turn` / `continuation_resume` | Long-call continuation boundaries. |
| `retry` | A retry attempt with its failure class and backoff. |
| `cache_hit` / `cache_miss` | A verification-cache lookup outcome. |
| `escalation_decision` | Whether escalation fired, the reason, and the initial → final verdict transition. |
| `grounding_outcome` | Accepted vs. rejected source URLs, and whether the verdict was downgraded for being ungrounded. |
| `budget_exhausted_marker` | The verifier spent its full search budget without grounding. |
| `stream_chunk` | Per-chunk streamed text — **deep mode only**, a no-op otherwise. |
| `note` | A free-form annotation (also used to record `local_skip` resolutions). |

The last several rows are the load-bearing ones for trust debugging. A
`grounding_outcome` event is the trace's record of the **grounding invariant** in
action — the rule that a `CONFIRMED` / `CORRECTED` verdict requires at least one
cited URL the search or fetch tool actually retrieved (see **Ch 10 — Verification
II: How We Check & Judge**). An `escalation_decision` paired with a
`verification_escalation` span is how a `VERIFIED_CONTESTED` finding — two capable
models grounding *different* verdicts — becomes visible after the fact (the
routing that produced it is **Ch 9 — Verification I: How We Decide to Check**).
When the report shows a verdict you distrust, these events are where you go to see
the evidence the model stood on.

---

## The recorder, the lifecycle, and the hooks

Three layers sit between the pipeline and the disk.

**The recorder** (`recorder.py`) is a global singleton, the `TraceRecorder`. It
owns the trace directory and a single background **writer thread** — the only
thread that ever touches a file handle. Public methods (`open_span`, `close_span`,
`add_event`, `prompt_ref`, `record_finding_snapshot`) are safe to call from any
thread; they serialize the payload and enqueue it, and the writer drains the queue
one JSONL line at a time, `fsync`-ing on `stop()`. Because batch verification runs
on a `ThreadPoolExecutor`, span parenting is tracked two ways at once: a
`contextvars.ContextVar` (which the `span()` context manager sets and which
propagates across a copied context) and a thread-local stack (pushed and popped by
`open_span` / `close_span` for callers that hold a raw handle). A worker submitted
to a thread pool sees neither automatically, so the recorder exposes
`bind_to_current_context(fn)` to snapshot the current context around a submitted
callable. `stop()` is idempotent, and a second `start()` against the same
directory opens the files in **append** mode — which is exactly how a batch run
that survived an app restart continues its original trace rather than starting a
new one.

**The lifecycle helpers** (`session.py`) are the thin wrappers the GUI controllers
call: `start_run_recorder` (gated on the trace flag; keys a fresh recorder to the
`run_id` sourced from diagnostics), `reattach_run_recorder` (reopens an existing
trace dir on a batch resume), and `stop_run_recorder` (drains, closes, and clears
the global). They live in the tracing package rather than the GUI so they import
without `customtkinter` and stay unit-testable headless.[^session-naming]

**The capture hooks** (`capture_hooks.py`) are the integration surface — the only
tracing functions the rest of the codebase calls. This layer is where the
non-invasiveness guarantee is built, and the discipline is uniform. Every hook:

1. Calls `get_recorder()` and **returns immediately** if no recorder is installed
   (tracing off → the hook is a cheap no-op).
2. Wraps the recorder call in `try/except Exception`, so a tracing failure can
   never escape into pipeline code.
3. Logs the *first* failure of each `(exception-type, originating-frame)` pair
   once, then suppresses every repeat.

That third rule is the difference between a useful warning and a log flood. A
pathological capture site that fails on every one of two hundred findings should
tell you once that it is broken, not two hundred times. A small `_safe` decorator
enforces the pattern so no hook has to remember it:

```python
def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            _log_once(f"tracing capture {fn.__name__} failed", exc=exc)
            return None   # the pipeline never sees the failure
    return wrapper
```

Hooks that open a span return a `SpanHandle | None`; `None` means tracing is off,
and the matching close hooks tolerate `None` for symmetry, so a capture site never
has to branch on whether tracing is enabled. The hooks also degrade gracefully on
*shape*: `capture_response_content_blocks` walks an Anthropic message's content
blocks to emit thinking / tool-use / search / fetch events, and every field read
goes through a tolerant lookup that handles both SDK objects and the plain dicts
the batch-retrieval path hands back — a block whose shape it does not recognize is
skipped silently rather than crashing the trace. The design stance is consistent
throughout: **better to drop one event than to break the run.**

Batch verification gets special handling. It executes on Anthropic's servers, so
there is no live span lifecycle to wrap. After a wave assigns a finding its final
`VerificationResult`, `capture_batch_verification_span` opens-and-immediately-
closes a `verification_initial` span carrying that result, correlated by
`metadata.finding_id`, so the viewer's By-Finding view shows a verification node
for batch findings too. In deep mode it also walks the wave's raw message onto that
span for real-time parity — but only in deep mode, because batch is the common path
and capturing every finding's thinking at the default level would bloat the trace.

---

## Redaction: scrub before you serialize

A trace is a verbatim record, which makes leaking a credential into one a real
risk. The rule is simple and enforced at the serialization boundary: every span,
event, finding snapshot, and `run.json` payload passes through `scrub_data()`
(`redaction.py`) on its way to the queue. `scrub_data` walks the structure (bounded
to six levels of nesting, so a cyclic dict cannot loop forever), redacts any value
whose *key* looks secret-shaped (`api_key`, `password`, `bearer`, …), and replaces
any *value* matching a known credential prefix — `sk-ant-`, `Bearer `, `AKIA` —
with `<redacted>`.

The patterns are not redefined here. `redaction.py` imports them directly from
`src/orchestration/diagnostics.py`, so the two observability surfaces share **one
source of truth** for what a secret looks like; adding a new credential pattern is
a one-line change in diagnostics that both inherit. (That import direction — the
tracer importing from the diagnostics module, never the reverse — turns out to be
the cleanest statement of the silo, as the next-but-one section explains.)

One deliberate non-redaction is worth stating plainly: **spec content is not
scrubbed.** Per design, the trace is allowed to capture the full extracted spec
text — that is exactly the "what the model saw" the trace exists to preserve. Only
credential-shaped values are removed. The matchers look for full key prefixes
rather than any long hex run, so false positives are rare; the failure mode they
guard against — a leaked key — is far worse than the cost of occasionally
redacting a string that merely looked like one.

---

## Reading a trace: the viewer and the CLI

Two readers ship with the codebase, neither requiring a build step or a network.

The **HTML viewer** (`src/tracing/viewer/trace_viewer.html`) is a single,
zero-build file: open it in any browser, point it at a trace folder, and it
renders four views whose colors and glyphs mirror the Word report so the same
finding reads the same way in both places.

- **By Finding** — the headline view. Each finding's full lifecycle: review →
  verification → grounding → verdict, with the trust-model status badge.
- **By Span** — the raw span tree plus prompt resolution (following a span's
  `prompt_ref` hash into `prompts.jsonl`).
- **Timeline** — every event in time order, filterable by type.
- **Search / Grounding** — the search queries a run issued and the accepted vs.
  rejected URLs, which is where you confirm the grounding invariant held.

The **CLI** (`python -m src.tracing`) is for the terminal:

```
python -m src.tracing list                          # enumerate runs
python -m src.tracing show <run_id>                 # finding-by-finding summary
python -m src.tracing prune --keep-last 20          # keep the 20 newest
python -m src.tracing prune --older-than 30d --yes  # delete runs older than 30 days
```

All subcommands take `--trace-dir` to read a non-default root. `show` reproduces
the report's status glyphs from the raw `findings.jsonl` — re-classifying each
finding with the same logic as `report_status` rather than importing the heavier
report module — and prints a one-line-per-finding triage summary with verdict,
search count, and any disagreement / budget / failure flags. It resolves a
`<run_id>` by directory name first, then by the `run_id` embedded in `run.json`, so
a copied or renamed trace folder still resolves. `prune` is the housekeeping path,
because a default-on trace accumulates: it sorts runs newest-first and deletes by
count or by age, prompting for confirmation unless `--yes` is passed.

---

## The diagnostics report: the in-memory ops log

The second observability artifact is the `DiagnosticsReport` (`diagnostics.py`),
and the first thing to be clear about is what it is **not**. It is not the Word
report's "Run Diagnostics banner." That banner lives in the exported `.docx`,
faces the reviewer, and is derived from `Finding` / `VerificationResult` fields by
`report_exporter` (see **Ch 11 — The Trust Model & Report Output**). The
`DiagnosticsReport` is an *in-memory operational log* the pipeline builds as it
runs and can dump as text — an engineer's instrument, not a deliverable. They share
the spirit of "how did this run go?" but neither their data path nor their
audience.

The report accumulates `DiagnosticEvent`s through `log()` and `record_api_call()`,
the latter normalizing every Anthropic call into one consistent key set (phase,
model, token and cache usage, search count, batch-vs-realtime, retry status) so
that `summary()` can roll up "which phases cost the most?" and "which get cache
hits?" without each call site reinventing the shape. `summary()` is a rich
aggregation — per-phase token and cache-hit-ratio telemetry, the verdict
breakdown, escalation change-rate, retry stats, search-budget percentiles, and
severity counts — and `to_text()` renders it as a human-readable timeline.

What makes the report safe to keep in memory through a multi-hour batch poll is the
**scrub-and-bound discipline**. Diagnostics has the same job as the trace — observe
without harm — but a different hazard: an unbounded in-memory structure. A single
event can carry a multi-megabyte field (a sprawling raw response, a huge source
list), and a long poll can produce a great many events. Four caps contain this:

| Cap | Value | Guards against |
|---|---|---|
| Max retained events | 5,000 | Event-count blowup on a long batch poll (oldest dropped, drop count tracked). |
| Per-event data | 16 KiB | One runaway event bloating memory. |
| Total event data | 8 MiB | Cumulative footprint across all events. |
| Per-string field | 4 KiB | A single giant string inside an otherwise small event. |

The same `_scrub_and_bound` helper that truncates oversized strings also redacts
secrets using the shared patterns — and it counts redactions and truncations into
the summary, so an operator can see at a glance that scrubbing fired or that a cap
was hit, rather than being silently lied to about completeness. When an event is
*still* over the per-event cap after scrubbing, the bounding logic evicts the
largest string-shaped fields first and **never touches numeric telemetry**, because
the summary rollup parses those token counts with `int(...)` and a truncation
marker where a number should be would crash it. Bounding that stays correct under
its own caps is the small, real engineering in this file.

---

## The silo, and how it actually holds

The chapter opened with the claim that tracing observes everything and changes
nothing — that `DiagnosticsReport.summary()` is byte-identical with tracing on or
off, that no finding, report, or summary shifts when you flip the switch. A claim
like that is only worth as much as its enforcement. Here is the enforcement.

```
                 ┌───────────────────────────────────────┐
   pipeline ─────┤  builds DiagnosticsReport (in-memory)  │──► to_text() / summary()
        │        └───────────────────────────────────────┘
        │  (capture hooks: read-only)
        ▼
   ┌─────────────────┐     scrub + enqueue     ┌──────────────────────┐
   │  TraceRecorder  │ ──────────────────────► │  JSONL on disk        │
   └─────────────────┘                         └──────────────────────┘

   correlated only by:  run_id (12 hex, shared at run start)
   dependency direction: tracing ──imports──► diagnostics   (never the reverse)
```

Three structural facts do the work.

**The two sinks are independent.** The pipeline populates the `DiagnosticsReport`
directly, through `log()` and `record_api_call()`. Tracing is a *separate* sink
that the capture hooks feed. Nothing in `capture_hooks.py` or `recorder.py` ever
calls a `DiagnosticsReport` method. So the diagnostics event stream — and therefore
`summary()`'s every byte — is identical whether or not a recorder is installed.
They are joined by a shared `run_id` (12 hex characters, the same width as a
span id) and by nothing else.

**The dependency graph is one-directional.** `diagnostics.py` does not import
anything from `src/tracing/`. The only coupling runs the other way:
`tracing/redaction.py` imports the secret patterns *from* diagnostics. The observed
module has no knowledge of the observer. This is not a convention you have to
remember to honor — it is enforced by the import graph, and a future edit that made
diagnostics depend on tracing would be the thing to flag in review.

**The hooks only ever read.** The capture layer reads existing `Finding` and
`VerificationResult` state through defensive `getattr` and serializes a copy. It
adds no field to any pipeline object and mutates none. Combined with the
`try/except` containment in every hook, this closes the loop: tracing cannot change
the data, and tracing cannot crash the run. The trace is allowed to be incomplete
on a bad day; it is never allowed to be *invasive*.

That is the real lesson of this subsystem. A capture layer that is merely "usually
harmless" is a latent bug in a tool whose whole value proposition is trust. A
capture layer that is *structurally* incapable of altering the thing it watches can
be left on by default — which is exactly what Spec Critic does.

---

## Design tensions and an honest edge

The silo guarantee is strong, but it is not flawless, and the audit
(**Ch 16 — Trust Under the Microscope: The Audits**) names the gap precisely.

**P2-4 — the delayed recorder reset.** The `TraceRecorder` is a process-global
singleton. On a normal run it is installed at start and cleared at completion. But
the GUI clears it as part of `reset_ui`, which the controller schedules roughly 2.5
seconds *after* the run completes (`app.after(2500, …)`). That leaves a short
window in which the global still points at the just-finished run's recorder. If a
user starts a *second* run inside that window, and a straggler worker thread from
run-1 fires a late capture, that event can be enqueued into run-2's recorder — a
small cross-run bleed.

Two things keep this scoped and low-risk. First, the blast radius is the
observability layer only: **trace files and diagnostics, never findings and never
the report.** The silo holds even here — the worst case is a mislabeled line in a
JSONL file, not a wrong verdict in a deliverable. Second, it requires a
human-fast second launch inside a ~2.5-second window after a multi-minute job, so
it is rare in practice. The fix is equally clean and is the obvious one: stop the
recorder *synchronously* at pipeline completion (where `_stopped` is set and the
old recorder begins dropping enqueues immediately) rather than deferring teardown
to the cosmetic UI reset. It is filed as a P2 — worth doing, not urgent — precisely
because the silo confines the consequences.

A second, smaller honesty: redaction is applied at the serialization boundary for
spans, events, finding snapshots, and `run.json`, which is every surface that
carries pipeline-shaped data. The deduplicated `prompts.jsonl` sidecar stores the
prompt bodies — system prompts and spec text — which by construction do not contain
the API key (that rides in the SDK transport header, never in the message body).
The practical exposure is therefore nil, but it is worth knowing exactly where the
scrub runs rather than assuming it blankets every byte on disk.

---

## How it connects

Observability is the one subsystem that touches every other without being touched
back. It draws lines outward in four directions:

- To **Ch 13 — The Desktop GUI & Its Controller Architecture**: the tracing
  checkboxes, the "Show folder" and "Open viewer" buttons. Ch 13 owns the widgets;
  this chapter owns what they observe.
- To **Ch 11 — The Trust Model & Report Output**: the *distinct* Run Diagnostics
  banner in the Word report. The banner is a deliverable derived from findings; the
  `DiagnosticsReport` is the in-memory ops log.
- To **Ch 9 — Verification I** and **Ch 10 — Verification II**: the routing,
  grounding, and escalation decisions whose `grounding_outcome`,
  `escalation_decision`, and `budget_exhausted_marker` events the trace records.
  This chapter describes the *observation*; those chapters describe the *observed*.
- To **Ch 16 — Trust Under the Microscope**: audit finding P2-4, the delayed
  recorder reset.

---

## Key takeaways

- **Observability is a trust instrument.** For a tool whose findings drive
  compliance decisions, "why did it say that?" must be answerable *later*, without
  re-running — because re-running a batch is slow and, against a live model and a
  moving web, not even reproducible. The trace is that answer.
- **Five files, default-on.** `run.json` (metadata, `run_id` matches
  `DiagnosticsReport.run_id`), `spans.jsonl` (nested invocations), `events.jsonl`
  (point-in-time markers), `prompts.jsonl` (content-deduped by 24-hex SHA-256), and
  `findings.jsonl` (terminal snapshots). Deep mode trades size for replay
  convenience — inlining prompts (so `prompts.jsonl` is not written) and adding
  snippet bodies and stream chunks.
- **The recorder is non-invasive by construction.** A background writer thread is
  the only thing that touches files; every capture hook no-ops when tracing is off,
  swallows its own exceptions, and warns once per failure site. Better to drop an
  event than break a run.
- **The silo holds because of structure, not vigilance.** Tracing and diagnostics
  are independent sinks correlated only by `run_id`; the dependency graph runs one
  way (tracing imports from diagnostics, never the reverse); the hooks only read.
  `summary()` is byte-identical with tracing on or off.
- **Redaction is shared and runs before serialization.** Credential patterns live
  once in `diagnostics.py`; tracing imports them. Spec content is deliberately *not*
  scrubbed — it is the "what the model saw" the trace exists to keep.
- **Diagnostics is bounded.** Event-count, per-event, total, and per-string caps,
  plus secret scrubbing, keep a multi-hour poll from blowing up memory — and the
  caps report when they fire, so completeness is never silently overstated.
- **One honest edge.** The global recorder is reset ~2.5s late on the UI thread
  (audit P2-4), opening a narrow cross-run bleed window — confined to trace and
  diagnostics, never findings or the report. The fix is to stop the recorder
  synchronously at completion.

[^session-naming]: The chapter prompt and `HANDBOOK_PLAN.md` describe `session.py`
as housing a `TraceSession` class that owns the per-run directory and writes
`run.json`. The source has no `TraceSession` class: `session.py` provides the
recorder *lifecycle* helpers (`start_run_recorder` / `reattach_run_recorder` /
`stop_run_recorder`), and the per-run directory creation and `run.json` writing
live on `TraceRecorder` in `recorder.py`. Per the handbook's source-wins rule, the
mechanics above describe the code as it is; the naming in the plan appears to be
drift.
