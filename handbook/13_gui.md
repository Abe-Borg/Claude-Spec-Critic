# The Desktop GUI & Its Controller Architecture

Everything else in this book is invisible to the person who actually runs Spec
Critic. They never see the routing decision, the grounding gate, the cache key,
or the eleven-hundred-line spine. They see a dark window with a few input fields
and a blue button that says **Submit Batch**. The desktop GUI is the entire
surface of the program — and for most of its life that surface has to do
something genuinely hard while *looking* like it is doing nothing at all: wait.

A review is not a function call that returns in a second. It goes through the
Message Batches API ([**Ch 6 — Batch Processing**](06_batch_processing.md)), which means a single run is a
sequence of *separate* operations — submit, poll, collect, verify, finalize —
with forty-five minutes to two hours of wall-clock time in Anthropic's queue
between the first and the last. During all of that the window must stay alive:
the user must be able to scroll the log, collapse a panel, read the "How It
Works" dialog, or simply move the window, without the whole thing freezing into
the operating system's spinning-beachball state. And there is a sharper hazard
underneath responsiveness. If the user starts a *second* run — or if a slow
background thread from a finished run wakes up late — the UI must never let one
run's results paint over another's. For a compliance tool, a stale verdict
silently overwriting a fresh one is the same category of failure the rest of the
codebase works so hard to prevent: confident, invisible wrongness.

This chapter is about how a deliberately *thin* GUI solves a time problem. The
structural audit looked hard at this layer and reached an unusually clean
verdict — **"GUI threading is sound"** ([**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md)).
The bulk of this chapter explains *why* it is sound, and the last part is honest
about the one place the surface can still mislead.

## Thin shell, fat spine: the controller pattern

The first design decision is visible the moment you open `src/gui/gui.py`: there
is almost no logic in it. The file builds a window and then *delegates*. Its
central class, `SpecReviewApp`, is a long list of one-line methods that each hand
off to a function in a controller module:

```python
def start_review(self):            _start_review(self)
def _poll_batch(self):             poll_batch(self)
def _on_review_complete(self, r):  on_review_complete(self, r)
def _export_report_to_file(self, r): return export_report_to_file(self, r)
```

The controllers are not classes and hold no state of their own. They are plain
functions whose first argument is `app` — the live `SpecReviewApp`. *All* run
state lives on the app object (`is_processing`, `_run_epoch`, `_batch_submission`,
`_selected_files`, `_diagnostics_report`, and so on); the controllers read and
mutate that state and the widgets hanging off it. This inverts the usual
expectation that a GUI class is where the behavior lives. Here the class is a
**presenter** — layout plus wiring — and the behavior is spread across seven
focused modules, each owning one slice of the workflow.

```
                         ┌───────────────────────────┐
                         │   SpecReviewApp (gui.py)   │
                         │  window, widgets, state,   │
                         │  thin delegating methods   │
                         └───────────────────────────┘
                                      │  app
            ┌──────────────┬──────────┼───────────┬──────────────┐
            ▼              ▼          ▼           ▼              ▼
   file_selection    context   token_analysis  review_run    batch
     _controller   _controller   _controller   _controller  _controller
            │              │          │           │              │
            │              │          │       report_controller  │
            │              │          │     diagnostics_controller│
            └──────────────┴──────────┴─────┬─────┴──────────────┘
                                            ▼
                          orchestration spine (pipeline.py)
                       extract → review → verify → cross-check → finalize
                                  (Ch 3 / Ch 4–10)
```

Why split it this way? Two reasons, both about trust by way of testability. First,
keeping `gui.py` a shell means the *business logic of a run* is reachable without
ever constructing a Tk window. The controllers call straight into the
orchestration spine (`src/orchestration/pipeline.py`, [**Ch 7 — Orchestration &
State**](07_orchestration.md)), which is pure Python; the hermetic test suite exercises that spine
directly and **skips the GUI tests entirely when `tkinter` is unavailable** (the
project's test harness does exactly this — see [**Ch 15 — Quality Engineering**](15_quality_engineering.md)).
A program whose core can only be tested through its UI is a program whose core is
hard to trust. Second, the controllers carve the workflow along its natural
seams, so a change to (say) how project context is attached touches one file
(`context_controller.py`) and not the threading code, and vice versa.

The seven controllers, each in one paragraph:

- **`file_selection_controller`** turns a user's file choice — from the Browse
  dialog or a drag-and-drop event — into the app's `_selected_files` list. It
  parses the platform-specific drop payload (`parse_dropped_paths` handles
  brace-quoted paths with spaces via `Tk.splitlist`, falling back to `shlex`),
  filters to supported extensions (`.docx`), records the parent directory, paints
  the entry field, and kicks off token analysis. Selections **accumulate** rather
  than replace: each Browse / drop unions onto the existing list (`merge_selected_specs`,
  de-duped by resolved path), so a user can load specs from more than one folder
  — the native file dialog only multi-selects within a single folder, so this is
  the only way to span folders. Re-selecting already-loaded files is a no-op.
  The **Clear** button (`clear_selection`) is the explicit reset; it bumps the
  analysis epoch and cancels the pending exact-token debounce so an in-flight
  background analysis can't repopulate the just-cleared panel. It deliberately
  does *not* own the `FileListPanel` widget — that stays on the app; the controller
  just normalizes paths and notifies.

- **`context_controller`** owns the Project Context box — the free-text paragraph
  that ships with every API call. It manages the placeholder/focus dance
  (the grey "Describe your project (optional)" prompt that clears on focus), a
  300 ms debounced token recount as you type, the colour-coded token label (amber
  at 90 % of the limit, red over it), the modal "Expand" editor, and `.docx`/`.pdf`
  *attachment* extraction — rejecting unsupported types and surfacing per-file
  read errors through a message box rather than failing silently.

- **`token_analysis_controller`** produces the preflight token counts the user
  sees before committing to a run. It runs the fast local `cl100k_base` estimate
  for every selected file off-thread, then fires a debounced (400 ms) Anthropic
  `count_tokens` call for the *largest* spec and swaps the gauge to the exact
  figure when it returns. The token math itself belongs to [**Ch 12 —
  Configuration, Models & Token Economics**](12_configuration_and_models.md); this controller's job is to display
  it without blocking or letting a stale pass overwrite a fresh one.

- **`review_run_controller`** is the run's spine on the UI side. It owns input
  validation, the **run-epoch staleness guard**, `start_review`, the
  completion/error handlers (`on_review_complete` / `on_review_error`), and
  `reset_ui`. This is where the threading discipline that the rest of the chapter
  is about actually lives.

- **`batch_controller`** owns every batch-specific step: the worker thread that
  calls `start_batch_review`, the bounded polling loop, the progress updates, and
  the long `collect_batch_results` worker that drives result collection,
  verification, cross-check, cross-check verification, and finalize. It also owns
  the *trace recorder's* lifecycle within a run — start at submit, stop after
  collect.

- **`report_controller`** is the smallest: it opens the save dialog, calls
  `export_report`, writes the machine-readable edit sidecar beside it, and returns
  a status string (`"canceled"` / `"success"` / `"error"`) so the caller can
  decide what to log. The report's *contents* are [**Ch 11 — The Trust Model &
  Report Output**](11_trust_model_and_output.md); this controller only triggers the export.

- **`diagnostics_controller`** builds the log/progress callbacks the pipeline
  calls into during a run and owns the pop-out Diagnostics window. Its callbacks
  fan out to *two* places at once — the on-screen activity log and the in-memory
  `DiagnosticsReport` ([**Ch 14 — Observability**](14_observability.md)) — so the UI and the forensic
  record never drift apart.

| Controller | Responsibility | Key entry points |
|---|---|---|
| `file_selection_controller` | Choose / validate / normalize / accumulate spec files | `browse_for_specs`, `parse_dropped_paths`, `merge_selected_specs`, `apply_selected_specs`, `clear_selection`, `clear_file_state` |
| `context_controller` | Project-context text + attachments | `get_project_context`, `on_context_change`, `attach_context_files`, `open_context_modal` |
| `token_analysis_controller` | Preflight token counts in the gauge | `analyze_tokens`, `refresh_exact_token_count`, `on_file_selection_change` |
| `review_run_controller` | Run lifecycle + epoch guard | `validate_inputs`, `next_run_epoch`, `dispatch_if_current`, `start_review`, `on_review_complete`, `on_review_error`, `reset_ui` |
| `batch_controller` | Submit / poll / collect / verify / finalize | `submit_batch_thread`, `on_batch_submitted`, `poll_batch`, `poll_and_collect_thread`, `collect_batch_results` |
| `report_controller` | Export report + edit sidecar | `export_report_to_file` |
| `diagnostics_controller` | Diagnostics callbacks + window | `make_diag_log`, `make_diag_progress`, `finalize_diagnostics`, `open_diagnostics_window` |

## The app shell: building the one window

`SpecReviewApp.__init__` sets the window to 900×950 (minimum 750×700), reads any
saved API key from disk or the environment, initializes the run-state fields, and
calls `_create_ui` to build the layout top to bottom: a header with the title and
the two help buttons; an **accessibility row** with a font-scaling segmented
control (100 % / +10 % / +20 %, applied live via `ctk.set_widget_scaling`); a
collapsible **inputs card**; the file list panel; the token gauge; the **Submit
Batch** button; a thin progress bar; the activity log; and a Diagnostics button
that stays disabled until a run produces a report.

The inputs card is five labelled rows: **API Key** (masked entry), **Specs**
(entry + Browse, also registered as a drag-and-drop target), **Project Context**
(an inline textbox plus *Expand* and *Attach Files…* buttons), **Options** (the
cross-spec coordination checkbox), and **Tracing** (covered below). The widgets
themselves come from `src/gui/widgets.py`, a small library of reusable
components built once and reused: the `TokenGauge` (an animated bar showing the
largest single spec's estimated call size against the per-call limit, labelled
"(approx)" until the exact API count lands); the `FileListPanel` (a checkbox list
with All/None controls and a red pulsing glow when a file is over the per-call
limit); the `EnhancedLog` (a paced, colour-coded, collapsible activity log that
queues lines so status updates appear at a readable cadence); the
`AnimatedButton` (the run button, with `ready` / `processing` / `complete` visual
states); and the `DiagnosticsWindow` (a pop-out that renders a `DiagnosticsReport`
into configuration, summary, and event-timeline cards). Two static informational
modals — "How It Works" and "How to Use" — live in `about_usage_dialogs.py`,
deliberately kept out of `gui.py` so the shell stays a layout file.

Drag-and-drop degrades gracefully. The root window is `_CTkDnDRoot`, which is
`customtkinter.CTk` mixed with `tkinterdnd2`'s `DnDWrapper` *when that package is
importable*, and plain `CTk` otherwise. If `tkinterdnd2` is missing, the import
guard at the top of `gui.py` quietly disables the drop target and prints a
one-line hint; Browse still works. The program never hard-depends on a feature it
can run without.

### Shipping as a desktop binary: `main.py` and PyInstaller

`main.py` is a sixteen-line entry point whose only real job is to make the
package importable whether the program is running from source or as a frozen
PyInstaller binary:

```python
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS          # PyInstaller's unpacked temp dir
    sys.path.insert(0, base_path)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, base_path)
from src.gui.gui import main
```

When PyInstaller packages the app into a single executable, it unpacks the bundle
into a temporary directory at launch and exposes that path as `sys._MEIPASS`. The
`frozen` branch puts that directory on the import path so `from src.gui.gui import
main` resolves; the non-frozen branch does the equivalent for a source checkout.
`main()` itself just sets the dark CustomTkinter appearance and blue theme and
enters `SpecReviewApp().mainloop()`. The takeaway: the program ships as a
self-contained desktop binary an engineer can hand to a non-technical reviewer,
with no Python install required — and the entry point is the only place that has
to know which of those two worlds it is in.

## The hard part is time, not pixels: threading & the epoch model

This is the centerpiece. Tk — the toolkit under CustomTkinter — has one
unbreakable rule: **all widget access must happen on the thread that created the
window** (the "main" thread running `mainloop`). Touch a widget from another
thread and behavior ranges from corrupted rendering to a hard crash. But a Spec
Critic run *cannot* happen on the main thread, because it blocks for hours. So the
design splits cleanly:

1. **Work runs off-thread.** Each blocking phase — submit, poll, collect — runs in
   its own `threading.Thread(..., daemon=True)`. The threads are *daemon* threads
   on purpose: they do not keep the process alive, so closing the window never
   hangs waiting for a two-hour poll to finish.

2. **Every widget update marshals back through `app.after(0, …)`.** Tk's `after`
   schedules a callback to run on the main thread at the next event-loop tick.
   This is the *only* sanctioned bridge from a worker thread to a widget, and the
   codebase uses it without exception: a worker never calls `app.log.log(...)` or
   `progress_bar.set(...)` directly — it hands the call to `after`.

3. **A staleness guard drops superseded callbacks.** This is the elegant part. The
   app keeps an integer `_run_epoch`, and every phase that launches a worker first
   bumps it. The worker captures the epoch value it was born under and threads it
   through every UI update via `dispatch_if_current`:

```python
def next_run_epoch(app) -> int:
    app._run_epoch += 1
    return app._run_epoch

def dispatch_if_current(app, epoch, fn) -> None:
    app.after(0, lambda: fn() if app._run_epoch == epoch else None)
```

The whole anti-bleed mechanism is those two functions. A callback scheduled by a
worker only *runs* if the app's current epoch still equals the epoch that worker
captured. The instant a newer phase or a newer run bumps `_run_epoch`, every
in-flight callback from the older epoch becomes a no-op — it is still delivered to
the main thread, but it does nothing. A zombie thread from an abandoned run can
fire as many `after` callbacks as it likes; none of them touch a widget.

4. **`is_processing` blocks concurrent *starts*.** `start_review` returns
   immediately if `app.is_processing` is already `True`. So the user cannot launch
   two overlapping runs from the button. The epoch guard and the `is_processing`
   flag cover different halves of the same problem: `is_processing` prevents a
   second run from *starting* while one is live; the epoch guard makes any
   *late-arriving* callback from a previous run harmless even after a new one
   begins.

Here is the full lifecycle as a swimlane, with the epoch checks marked. Note that
`_run_epoch` is bumped three times in a single run — at `start_review`, at
`poll_batch`, and at `collect_batch_results` — so that each phase's callbacks are
gated independently:

```
 Tk main thread (mainloop)        worker thread(s) [daemon]        Anthropic batch service
 ─────────────────────────        ─────────────────────────        ───────────────────────
 start_review
   validate_inputs
   is_processing = True
   set_processing() (button)
   epoch = next_run_epoch ───►  submit_batch_thread(epoch)
   (window stays responsive)      start recorder
                                  start_batch_review ───────────────►  enqueue review batch
                                  dispatch_if_current(epoch, …) ─┐
 on_batch_submitted  ◄─── after(0): run iff epoch current ◄──────┘
   progress = 0.4
   epoch = next_run_epoch ───►  poll_and_collect_thread(epoch)
   (responsive)                   poll_batch_bounded  ◄────────────── poll … (45 min – 2 hr)
                                    progress_cb → dispatch_if_current ─┐
 _update_poll_progress ◄── after(0): run iff epoch current ◄──────────┘
                                  dispatch_if_current(epoch, _collect_batch_results)
 collect_batch_results
   epoch = next_run_epoch ───►  _do_collect(epoch)
                                  collect review → verify → cross-check → finalize
                                  dispatch_if_current(epoch, _on_review_complete) ─┐
 on_review_complete  ◄─── after(0): run iff epoch current ◄────────────────────────┘
   progress = 1.0; export report (save dialog runs HERE, on main thread)
   set_complete() (green ✓)
   after(2500, reset_ui) ─────────────────────────┐  (recorder stopped in _do_collect's finally)
 reset_ui  (≈2.5 s later)  ◄───────────────────────┘
   is_processing = False; progress hidden; recorder reference cleared
```

Reading that swimlane top to bottom is the run *from the UI's side* — the same
sequence [**Ch 3 — A Run, End to End**](03_end_to_end_flow.md) narrates at the data level, here at the
interaction level. A few details earn their place:

- **The save dialog runs on the main thread.** `on_review_complete` (and the
  `export_report_to_file` it calls) is itself a callback delivered through
  `after`, so the native file-picker — which *must* be on the UI thread — opens
  safely. The worker never opens a dialog.

- **The recorder is stopped in the worker's `finally`.** `collect_batch_results`
  wraps its work in `try/finally`, and the `finally` stops the trace recorder and
  clears `app._trace_recorder` as soon as collection ends — success or exception.
  `reset_ui` *also* stops it, idempotently, which matters for error paths (more
  below).

- **`reset_ui` is intentionally delayed 2.5 seconds.** After a clean run the button
  turns green and reads "✓ Complete," and the UI lingers in that state for 2.5 s
  (`app.after(2500, app._reset_ui)`) so the user actually registers that it
  finished before the window resets to its ready state. During that window
  `is_processing` is still `True`, so the button is inert.

Why is this the *correct* pattern, and not over-engineering? Because the
alternatives are worse. Running work on the main thread freezes the window for
hours. Running work off-thread *without* `after` marshaling is undefined behavior
the moment a worker touches a widget. And marshaling *without* the epoch guard
leaves a real bug on the table: a slow or hung worker from run A can wake up after
run B has started and repaint run A's stale progress, log lines, or even a stale
"Complete" — exactly the cross-run contamination a compliance tool must not have.
The epoch integer turns that whole class of race into a single equality check. The
audit traced this end-to-end and signed off: no off-thread Tk access, no cross-run
result bleed.

There is a *second* epoch counter doing the same job in a quieter place.
Background **token analysis** (`token_analysis_controller`) carries its own
`_analysis_epoch`: each time the file selection changes, the epoch bumps, and an
older analysis thread — which may still be mid-extraction or waiting on a
`count_tokens` call — sees that its captured epoch no longer matches and silently
drops its results. Without it, toggling files quickly could let a slow estimate
for an old selection overwrite the gauge for the new one. Same pattern, same
reasoning, applied to the preflight display instead of the run.

## The tracing row

The inputs card's Tracing row exposes the observability layer's controls without
pulling its internals into the GUI. Two checkboxes — **Record agent trace**
(default on) and **Deep mode** (default off) — plus a **Show folder** button and
an **Open viewer** button. The checkboxes do something deliberately simple:
`_on_trace_toggle` translates their state into the `SPEC_CRITIC_TRACE` and
`SPEC_CRITIC_TRACE_DEEP` environment variables. Because the trace recorder reads
those variables at *construction* time — which happens at the next run's submit —
toggling between runs takes effect **without a process restart**, and the toggle
is applied once at startup so the very first run honours the defaults. "Show
folder" opens `~/.spec_critic/traces/` in the OS file explorer; "Open viewer"
opens the bundled single-file HTML replay tool via a `file://` URL so it works
offline. What those traces *contain*, how the recorder is structured, and how the
viewer reconstructs a run all belong to [**Ch 14 — Observability: Tracing &
Diagnostics**](14_observability.md).

## Edges & what's still being perfected

The threading is sound; the *honesty of the terminal state* is where the audit
found work remaining. Two findings touch this chapter directly, and both are about
the gap between what the program knows and what its surface shows.

**P0-1 — a partially-failed run can look like a clean one (the headline audit
finding).** When the batch comes back, the spine *correctly* records every spec
whose review failed — a missing, truncated, parse-errored, or errored result lands
in `truncated_specs` and sets `review_result.error` ([**Ch 7 — Orchestration &
State**](07_orchestration.md)). The GUI even surfaces it transiently: `collect_batch_results` logs a
per-spec warning, and `on_review_complete` logs *"Review completed with errors —
some specs failed."* But then the same handler calls `set_complete()` — the green
"✓ Complete" — and finalizes the diagnostics status. Crucially, the *only* thing
that decides whether diagnostics finalize as "success" versus a softer status is
the **export** outcome, not the **review** outcome. A run where two of five specs
silently failed review still ends on a green checkmark and a "Run completed
successfully" diagnostics line. The single warning scrolls past in the activity
log. For a tool whose entire purpose is to be trusted about compliance, *"we
reviewed all five and they're clean"* and *"two of five never got reviewed"* must
not share a terminal state. The data to fix it already exists end-to-end
(`truncated_specs`); the gap is purely one of *surfacing* it — a distinct
"Completed with errors" state in the UI and a corresponding row in the report's
Run Diagnostics banner ([**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md); the full
finding and its remedy are in [**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md)). It is worth
naming plainly because it sits on the most-trusted surface of the program.

**P2-4 — the trace recorder's reset rides on the delayed `reset_ui`.** The trace
recorder is a process-global singleton (its internals are [**Ch 14**](14_observability.md)'s to explain).
In the normal path it is stopped and flushed inside `collect_batch_results`'s
`finally` the instant collection ends; `reset_ui` then performs a second,
idempotent stop and clears the reference — but `reset_ui` runs ~2.5 s *after*
completion, riding the same delay that keeps the green checkmark on screen. The
audit flagged that this delayed global reset opens a narrow window in which a
late-firing worker thread from the finishing run could enqueue trace events
against the wrong run's recorder. The blast radius is deliberately small: it
touches **trace and diagnostics capture only — never findings, verdicts, or the
report**, all of which are already finalized by the time the window opens. It is a
low-severity hardening item (the suggested fix is to null the recorder
synchronously at completion rather than on the delayed UI reset), and it is
genuinely cross-run *tracing* hygiene, not a data-plane risk.

**A smaller drift worth noting in its lane.** The static "How to Use" dialog tells
the user *"You can close the app and reopen it later — the pending batch state is
saved and you will be prompted to resume."* The batch controller this chapter
owns, however, documents its flow as **forward-only — "a batch runs
start-to-report in a single process"** — and there is no resume entry point among
the run-lifecycle controllers (a vestigial recorder-reattach guard in `reset_ui`
is the only remaining hint of a once-planned resume path). This is exactly the
kind of doc-versus-code drift the audits care about: harmless to a run, but a
promise the current UI does not keep. It belongs on the list of things still being
reconciled.

## How it connects

The GUI is the program's mouth and hands, so it touches nearly every other
chapter — always as a *driver*, never as a re-implementer:

- It triggers the run that [**Ch 3 — A Run, End to End**](03_end_to_end_flow.md) follows at the data level
  and that the spine in [**Ch 7 — Orchestration & State**](07_orchestration.md) actually sequences; the
  failed-spec data behind P0-1 lives there.
- It displays the preflight token math owned by [**Ch 12 — Configuration, Models &
  Token Economics**](12_configuration_and_models.md), and it submits through the batch backbone of [**Ch 6 — Batch
  Processing**](06_batch_processing.md).
- It fires the export whose contents — the Word report, the trust labels, the edit
  sidecar — belong to [**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md), and where the
  P0-1 banner fix would land.
- Its tracing row and diagnostics window are thin handles onto [**Ch 14 —
  Observability**](14_observability.md), which owns the recorder internals and the P2-4 mechanism.
- Both honest edges (P0-1, P2-4) are catalogued in full in [**Ch 16 — Trust Under
  the Microscope**](16_trust_under_the_microscope.md).

## Key takeaways

- **The GUI is a thin presenter.** `gui.py` builds the window and delegates; seven
  stateless controller modules carry the workflow, with all run state on the
  `SpecReviewApp` object. This keeps the core testable headlessly — the pipeline
  never needs Tk.
- **The hard problem is time, not pixels.** A run blocks for 45 min–2 hr, so work
  runs on daemon worker threads and *all* widget updates marshal back to the main
  thread through `app.after(0, …)` — the only safe bridge under Tk.
- **The epoch guard is the anti-bleed mechanism.** A monotonically bumped
  `_run_epoch`, captured per worker and checked in `dispatch_if_current`, makes
  every callback from a superseded phase or run a no-op. `is_processing` blocks
  concurrent starts. A parallel `_analysis_epoch` protects the token gauge. The
  audit confirmed this layer sound — no off-thread access, no cross-run bleed.
- **Tracing toggles are env-var writes** applied at the next run's start, so
  record/deep mode change without a restart (default record-on, deep-off).
- **The honest edge is the terminal state, not the threading.** P0-1: a
  partially-failed run still ends on a green checkmark and a "success" diagnostics
  line — the failure data exists but is not surfaced in the UI's final state or the
  report. P2-4: the global trace recorder's reset rides the delayed `reset_ui`,
  a narrow tracing-only window that never touches findings or the report.
