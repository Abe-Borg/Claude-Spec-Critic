# Architecture at a Glance: Subsystems, Dependencies & the Core Data Model

The previous chapter argued that Spec Critic's whole reason for existing is
*trust* — that a compliance tool which is confidently wrong about a building
code is worse than no tool at all. This chapter is where that argument becomes
**structure**. If you open the repository cold, you see ten directories and
roughly four dozen Python files, and it is not obvious why the code is carved up
the way it is. The short answer is that almost every seam in this codebase
exists to keep uncertainty *visible* and *contained*: deterministic checks live
apart from model calls so the cheap, certain answers never get tangled with the
expensive, probabilistic ones; verification lives apart from review so a verdict
can be grounded independently of the claim that prompted it; output lives apart
from everything so the report can render a finding's trust status without being
able to *change* that status.

This is the **static map** of the system. After this chapter you should be able
to point at any file in `src/` and say which subsystem it belongs to and roughly
what it does, and you should be able to sketch — from memory — the handful of
data objects that flow from a `.docx` file to a Word report. We deliberately
stay at altitude here: we draw the boxes and name the data shapes, but we leave
the *moving picture* (what happens, in what order, when you click "Review") to
[**Ch 3 — A Run, End to End**](03_end_to_end_flow.md), and we leave field-by-field semantics of each data
object to the chapter that owns the file it lives in.

Two ideas carry the chapter. First, the system is **layered**, not monolithic: a
foundation of configuration, a set of independently testable worker subsystems,
a spine that sequences them, a consumer that reads the finished state, a thin
GUI on top, and an observability silo off to the side. Second — and this is the
thread to hold onto — **the data model *is* the contract between the layers.**
The unit of currency is the `Finding`. A `Finding` is born in review carrying a
problem and (sometimes) a proposed fix; it picks up a stable identity and its
per-file origins at deduplication; and only later does it accumulate a
*verdict*. By the time it reaches the report it has gathered all the context a
human (or a downstream applier) needs to decide whether to trust it. Follow the
`Finding` and you have followed the system.

---

## 1. The shape of the system: ten packages

`src/` is organized into **ten packages** — eight *functional* packages plus the
`gui` and `tracing` siblings. The tree holds **58 Python modules** in total: 48
application modules and 10 package `__init__.py` files (the `output` package
ships without one), alongside a root `main.py` launcher and a single
self-contained HTML trace viewer under `tracing/viewer/`.[^count]

[^count]: `HANDBOOK_PLAN.md` §6 cites "56 source files"; the tree today holds 58
`.py` files under `src/`. The figure drifted by two as the codebase grew —
exactly the kind of small fact-vs-source gap the audits (see [**Ch 16 — Trust
Under the Microscope**](16_trust_under_the_microscope.md)) exist to catch. The per-package counts below are
authoritative as of this writing; verify against the tree if precision matters.

Here is the whole system on one page: each package, the problem it owns, its
files, and the chapter that takes it apart in depth.

| Package | Responsibility | Key files (app modules) | Deep dive |
|---|---|---|---|
| **`core`** (5) | Foundation: model ids & capability whitelist, output caps, code-cycle definitions, token counting, API-key storage, platform paths. Everything sits on this. | `api_config.py`, `code_cycles.py`, `tokenizer.py`, `api_key_store.py`, `app_paths.py` | [**Ch 12 — Configuration, Models & Token Economics**](12_configuration_and_models.md) |
| **`input`** (3) | Turn `.docx` files into reviewable text + a stable element-id map, with caching; run the deterministic local detectors. | `extractor.py`, `extraction_cache.py`, `preprocessor.py` | [**Ch 4 — Input**](04_input.md) |
| **`review`** (5) | The per-spec Claude pass: build the request, define the tool-use schemas, render prompts, parse findings. Defines the `Finding`/`EditProposal`/`ReviewResult` data model. | `reviewer.py`, `review_request_builder.py`, `structured_schemas.py`, `prompts.py`, `prompt_serialization.py` | [**Ch 5 — The Review Engine**](05_review_engine.md) |
| **`batch`** (2) | The Message Batches API backbone: submit/retrieve wrapper and bounded polling with progressive backoff. | `batch.py`, `batch_runtime.py` | [**Ch 6 — Batch Processing**](06_batch_processing.md) |
| **`orchestration`** (2) | The spine. Sequences every stage, owns aggregate run state, deduplicates findings, and keeps the in-memory operational diagnostics. | `pipeline.py`, `diagnostics.py` | [**Ch 7 — Orchestration & State**](07_orchestration.md); diagnostics → [**Ch 14 — Observability**](14_observability.md) |
| **`cross_check`** (1) | The cross-spec coordination pass: find defects that span multiple specs, chunked by CSI division. | `cross_checker.py` | [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md) |
| **`verification`** (9) | The largest functional package. Decide *whether* to check a finding (routing, modes, profiles, triage, prescreen) and *how to check and judge* it (the verifier, source grounding, the claim cache, retry policy). | `verifier.py`, `verification_routing.py`, `verification_modes.py`, `verification_profiles.py`, `verification_prescreen.py`, `triage.py`, `source_grounding.py`, `verification_cache.py`, `retry_policy.py` | [**Ch 9 — Verification I**](09_verification_routing.md) (routing) & [**Ch 10 — Verification II**](10_verification_grounding.md) (checking) |
| **`output`** (3) | Consume the finished state: classify each finding's trust status & edit label, render the Word report, write the JSON edit sidecar. | `report_status.py`, `report_exporter.py`, `edit_sidecar.py` | [**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md) |
| **`gui`** (10) | The CustomTkinter desktop app: a shell, reusable widgets, dialogs, and seven thin controllers bridging widgets to the pipeline. | `gui.py`, `widgets.py`, `about_usage_dialogs.py`, + 7 `*_controller.py` | [**Ch 13 — The Desktop GUI**](13_gui.md) |
| **`tracing`** (8) | A forensic observability silo: per-run JSONL trace (spans/events/prompts/findings), defensive capture hooks, redaction, a CLI, and a zero-build HTML replay viewer. | `recorder.py`, `session.py`, `spans.py`, `capture_hooks.py`, `redaction.py`, `config.py`, `cli.py`, `__main__.py` | [**Ch 14 — Observability**](14_observability.md) |

A few orienting notes on the packages that surprise people:

**`verification` is nearly half the worker code for a reason.** It is split into
*two questions* that the rest of the book treats as separate chapters. The
"should we even spend a web search on this?" machinery — prescreen, profiles,
modes, routing, optional Haiku triage — is one cluster ([**Ch 9**](09_verification_routing.md)). The "go check
it, and decide what counts as proof" machinery — the verifier itself, source
grounding, the persistent claim cache, the retry/continuation taxonomy — is the
other ([**Ch 10**](10_verification_grounding.md)). Nine files sounds heavy until you realize that *grounding a
verdict in real evidence* is the single hardest thing this program does.

**`core` is genuinely foundational, with one honest exception.** Every other
package imports from `core`; `core` imports from no other package at module-load
time. The one exception is a deliberate sleight of hand: `tokenizer.py` reaches
*into* `review` for the Anthropic client (`from ..review.reviewer import
_get_client`) — but only inside a function body, lazily, so the import graph has
no load-time cycle. The token counter needs the API client to get exact counts;
rather than invert that dependency, the code defers it to call time. We will see
this pattern — *break a cycle by importing inside a function* — twice more
below.

**`output` is more decoupled than it looks.** It imports only `core` (for the
severity→budget helper and code-cycle metadata) and `verification` (for the
cache path and verdict vocabulary). Notably it does **not** import `review`,
even though its whole job is to render `Finding` objects: `report_status.py`
treats a `Finding` *structurally* — it reads `.verification` and
`.edit_proposal` off whatever object it is handed — rather than importing the
class. That duck-typing is what lets the trust-model classifier ([**Ch 11**](11_trust_model_and_output.md)) be
unit-tested against hand-built stand-ins with no review machinery in sight.

---

## 2. The dependency / layering view

The packages stack into five tiers. Read the diagram top-to-bottom as
"who drives whom," and note the two boxes pulled out to the side — `output` and
`tracing` — which sit *across* the tiers rather than *in* them.

```
   ┌───────────────────────────────────────────────────────────────┐
   │  gui          shell + widgets + dialogs + 7 thin controllers   │   ← DRIVER (thin)
   └───────────────────────────────────────────────────────────────┘
                                │ drives
                                ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  orchestration    pipeline (the spine) + diagnostics           │   ← SPINE
   └───────────────────────────────────────────────────────────────┘
        │            │            │            │            │
        ▼            ▼            ▼            ▼            ▼
   ┌────────┐  ┌─────────┐  ┌────────┐  ┌────────────┐  ┌──────────────┐
   │ input  │  │ review  │  │ batch  │  │ cross_check│  │ verification │   ← WORKERS
   └────────┘  └─────────┘  └────────┘  └────────────┘  └──────────────┘
        │            │            │            │            │
        └────────────┴─────┬──────┴────────────┴────────────┘
                           ▼ all sit on
   ┌───────────────────────────────────────────────────────────────┐
   │  core    api_config · code_cycles · tokenizer · keys · paths   │   ← FOUNDATION
   └───────────────────────────────────────────────────────────────┘

   ┌─────────────┐   reads the finished PipelineResult / Finding[]
   │   output    │ ◄──── consumes state; imports only core + verification
   └─────────────┘        (Finding is duck-typed, never imported)

   ┌─────────────┐   worker code calls capture_hooks.*(...) one-way
   │   tracing   │ ◄──── observes; never mutates Finding/ReviewResult/
   └─────────────┘        VerificationResult; hook failures are swallowed
```

The arrows that matter at the package level — the actual `from ..X import …`
edges, with the deferred ones marked — look like this:

```
   core          →  (nothing, except a lazy function-local borrow of
                     review.reviewer._get_client from tokenizer)
   input         →  core
   review        →  core, input          [+ verification, TYPE_CHECKING only]
   batch         →  core, review, verification, tracing   [verification deferred]
   verification  →  core, review, batch, tracing
   cross_check   →  core, input, review, verification, tracing
   orchestration →  core, input, review, batch, cross_check, verification, tracing
   output        →  core, verification
   gui           →  core, input, review, batch, orchestration, output, tracing
   tracing       →  orchestration (only redaction.py, reusing diagnostics' regexes)
```

Three things in that list are worth pausing on, because they are exactly the
kind of structural honesty the handbook is meant to teach.

**The `review`↔`verification` "cycle" is not a runtime cycle.** A `Finding`
carries an optional `VerificationResult` (the verdict that gets attached later),
so `reviewer.py` needs to *name* that type. But it imports it only under
`if TYPE_CHECKING:` — a type-checker-only hint that costs nothing at runtime.
At runtime the dependency flows one way: `verification` imports `review` (it
operates on real `Finding` objects), and `review` does not import
`verification` at all. The data model gets to reference a type that lives
"downstream" without creating a load-order problem.

**The `batch`↔`verification` cycle is broken by deferred imports.** Verification
runs *as batches* (the verifier submits its checks through the Message Batches
API), so `verification` imports `batch` at module top level. But `batch` also
needs a couple of verification helpers — the severity→budget function and the
routing/tool builders — to assemble a verification request. It pulls those in
*inside functions* (`from ..verification.verification_routing import …` at call
time) rather than at the top of the file. Same trick as `core`/`review`: when
two peers genuinely need each other, the less-central one borrows the other
lazily so the module graph stays acyclic.

**`tracing` is a silo — but not by being undepended-upon.** It is tempting to
describe tracing as "a module nobody imports," and that is *wrong*: nine files
across `batch`, `cross_check`, `gui`, `orchestration`, and `verification` import
`capture_hooks` and sprinkle one-line calls through the worker code. What makes
tracing a *silo* is the **direction of the coupling and the shape of the
guarantee**, not the absence of imports:

- It is a **one-way observer of the data model.** No tracing field ever appears
  on a `Finding`, a `ReviewResult`, or a `VerificationResult`. Tracing *reads*
  those objects to record them; it never alters their shape. You can delete the
  entire `tracing` package and the data contracts between stages are unchanged.
- Its hooks are **defensive.** `capture_hooks` is designed so that a failure
  inside tracing — a serialization bug, a full disk — is caught and swallowed,
  *never escaping into the pipeline*. Observability can break without breaking
  the run it is observing.
- Its only *inbound* dependency on the rest of the code is one import in
  `redaction.py`, reusing the secret-scrubbing regexes that already live in
  `diagnostics.py` — sharing a pattern, not borrowing pipeline logic.

So tracing sits beside the system, watching it, structurally unable to corrupt
it. The full mechanics — spans, events, the JSONL files, the redaction layer —
are [**Ch 14 — Observability**](14_observability.md)'s to explain.

**Why this layering and not a monolith?** Because each worker subsystem can be
*tested and reasoned about in isolation*. The extractor can be exercised against
in-memory `.docx` fixtures with no API key. The trust-model classifier can be
fed hand-built finding-shaped objects. The verifier's grounding logic can be
checked against canned tool-use responses. The *spine* — `pipeline.py` — is the
only place that has to know the order of operations, and the GUI is deliberately
thin so that the same pipeline runs identically whether a human clicked a button
or a test called the function. The cost of this arrangement is the handful of
deferred imports above; the benefit is that the genuinely hard, probabilistic
parts of the system are each boxed where they can be scrutinized.

---

## 3. The core data model — the chapter's centerpiece

Everything above is scaffolding for this section. The packages matter because of
what flows *between* them, and what flows between them is a small set of
dataclasses. There are about ten that carry the run. We introduce them here as a
*connected map* — what each one carries (the load-bearing fields only) and how
one becomes the next — and we defer the field-by-field semantics to each
object's owning chapter. The pointers are explicit; follow them when you need
the detail.

### 3.1 The transformation chain

Here is the whole data model in motion, from file to report. Read it as "what
shape the data is in at each seam":

```
  .docx file
     │  extractor.py
     ▼
  ExtractedSpec ───────────────► ParagraphMapping[]   (element_id: "p7", "t0r2", "s1h0")
   (content, word_count,         extraction_warnings[]
    document_id)
     │  preprocessor.py  (no API call)
     ▼
  PreprocessResult ─────────────────────────────────────────────────┐
   (9 deterministic alert lists: leed / placeholder / template_marker│
    / stale_code_cycle / invalid_code_cycle / empty_section / …)     │  alerts bypass the
     │                                                               │  model and flow
     │  review.py  (Claude, via the Message Batches API)             │  straight to the report
     ▼                                                               │
  ReviewResult ───────────► Finding[]                                │
   (raw_response, thinking,   severity · fileName · section · issue  │
    tokens, parse_status)     · actionType · existingText /          │
                              replacementText · codeReference         │
                              · confidence                            │
                              · edit_proposal: EditProposal | None    │
                              · verification: None  ◄── empty for now │
     │  pipeline._deduplicate_findings  (SHA-256 dedup keys)         │
     ▼                                                               │
  Finding[]  (now with finding_id; merged reps carry                 │
              occurrence_originals[] = per-file members)             │
     ├───────────────────────────────┐                              │
     │  cross_checker (parallel)      │  verifier (parallel)         │
     ▼                                ▼                              │
  ReviewResult                   per Finding:                        │
  (coordination findings,        VerificationRoutingDecision         │
   → cross_check_result)          (mode · profile · model ·          │
                                   budget · tools)                   │
                                        │                            │
                                        ▼                            │
                                 VerificationResult ────────────────►│
                                  (verdict · grounded · sources ·    │
                                   models_disagreed · …)             │
                                  attached to Finding.verification    │
     │  pipeline.finalize_batch_result                               │
     ▼                                                               ▼
  PipelineResult ◄─────────────────────────────────────────────────┘
   (review_result · cross_check_result · all alert lists ·
    cycle_label · extracted_specs[])
     │
     ├──────────────────────────────┬───────────────────────────────┐
     │  report_exporter (.docx)      │  edit_sidecar (.edits.json)   │
     ▼                               ▼                               │
  Word report                   JSON edit feed          group_findings():
  (per-finding trust status)    (one proposal/finding)   FindingGroup → FindingOccurrence[]
```

The intermediate carrier `CollectedBatchState` does not appear above because it
is a *transport* object, not a transformation: in batch mode the pipeline splits
into separate `submit → poll → collect → finalize` calls, and
`CollectedBatchState` is the bundle that survives the gaps between them. More on
that in [**Ch 7 — Orchestration & State**](07_orchestration.md).

### 3.2 The objects, in flow order

**`ExtractedSpec` + `ParagraphMapping`** *(defined in `input/extractor.py`;
detail → [**Ch 4 — Input**](04_input.md)).* One `ExtractedSpec` per document: the flattened
`content` string, a `word_count`, a `document_id`, and — the load-bearing part —
an optional `paragraph_map` of `ParagraphMapping` rows. Each row records one
extracted element (a body paragraph, a table-cell row, a header/footer
paragraph) and stamps it with a **stable, human-readable `element_id`** —
`p7` for body paragraph 7, `t0r2` for table 0 row 2, `s1h0` for a section-1
header. Those ids are how a finding can later point at "the exact paragraph I
mean" without anyone re-walking the document. `ExtractedSpec` also carries
`extraction_warnings` — the breadcrumb a drawing-heavy spec leaves so the report
can warn that some content may not have been captured as text.

**`PreprocessResult`** *(defined in `input/preprocessor.py`; detail → [**Ch 4**](04_input.md)).*
This is the output of the *deterministic* pre-screen, and it is structurally
important precisely because it is **not** model-derived. It is a bag of alert
lists — LEED references, placeholders, template markers, stale and invalid code
cycles, empty sections, duplicate headings and paragraphs — each alert a small
dict carrying a stable `deterministic_rule` id. These alerts never go to Claude
and never become `Finding`s in the review path; they flow *around* the model,
straight into the report. That separation is the first concrete expression of
the trust thesis: the cheap, certain detections are kept apart from the
expensive, probabilistic ones so the two can be rendered (and trusted)
differently.

**`Finding` + `EditProposal`** *(defined in `review/reviewer.py`; detail →
[**Ch 5 — The Review Engine**](05_review_engine.md)).* The `Finding` is the unit of currency. At birth
it carries a `severity` (`CRITICAL` / `HIGH` / `MEDIUM` / `GRIPES`), the
`fileName` and `section` it came from, the `issue` prose, an `actionType`, the
verbatim `existingText` / `replacementText` it proposes to change, an optional
`codeReference`, and a `confidence`. Crucially, the *edit* half is optional and
explicit: a finding either carries an `EditProposal` (a structured
action / existing → replacement / anchor / `target_element_id` / `edit_confidence`)
or it does not — coordination notes and interpretation questions legitimately
have no clean textual fix and say so via `REPORT_ONLY`. And the `verification`
slot starts **empty**:

```python
@dataclass
class Finding:
    severity: str
    fileName: str
    issue: str
    actionType: str
    existingText: str | None
    replacementText: str | None
    confidence: float = 0.5
    verification: VerificationResult | None = None   # ← filled in much later
    edit_proposal: EditProposal | None = None
    finding_id: str = ""                              # ← stamped at dedup
    occurrence_originals: list["Finding"] = field(default_factory=list)
    # … plus anchorText, evidenceElementId, demotion_reason, affected_files
```

That `verification: None` default is the whole story in one line: a `Finding`
arrives un-adjudicated and *accumulates* its verdict downstream. Two other
fields earn their place in this overview. `finding_id` is the stable
SHA-256-derived identity stamped at deduplication, so the report and the JSON
sidecar can refer to the same finding by name. And `occurrence_originals` is how
a *multi-file* finding remembers its parts: when dedup collapses the same defect
across several specs into one representative, the per-file member findings are
preserved here so each file's own exact `existingText` survives the merge — the
detail belongs to [**Ch 7**](07_orchestration.md), but the field is part of the data model, so it is
named here. The `as_edit_proposal()` accessor is the single way anyone asks
"does this finding have a usable edit?", reconstructing one from legacy fields
when needed and returning `None` for `REPORT_ONLY` or malformed shapes.

**`ReviewResult`** *(defined in `review/reviewer.py`; detail → [**Ch 5**](05_review_engine.md)).* The
envelope around one review (or cross-check) call: the `findings` list plus
metadata — the model used, token counts, prompt-cache telemetry,
`parse_status`, `stop_reason`, and the raw `structured_payload` the model sent
through the tool schema. The pipeline produces one `ReviewResult` for the
per-spec review and, when coordination runs, a *second* one for the cross-check
pass. Its convenience properties (`critical_count`, `high_count`, …) are how the
GUI and report get their severity tallies without re-counting.

**`VerificationRoutingDecision`** *(defined in
`verification/verification_routing.py`; detail → [**Ch 9 — Verification I**](09_verification_routing.md)).*
Before a finding is checked, a *pure function* produces this frozen policy
bundle for it: the chosen `mode` and `profile`, the `model` id, whether
`thinking` is enabled, the `web_search_max_uses` budget, which tools to attach,
the cache phase, and whether the finding short-circuited to a `local_skip`. The
point of bundling every knob into one immutable object is that *every*
verification path — real-time, batch initial, batch retry, batch continuation —
reads from the same decision and cannot quietly pick a different policy than the
selector intended. It is a contract object, not a result.

**`VerificationResult`** *(defined in `verification/verifier.py`; detail →
[**Ch 10 — Verification II**](10_verification_grounding.md)).* This is the richest object in the system, and
deliberately so — adjudicating trust requires a lot of evidence. The headline
fields are the `verdict` (`CONFIRMED` / `CORRECTED` / `DISPUTED` / `UNVERIFIED`),
a `grounded` boolean, and the `sources` list — which, by invariant, contains
only **accepted** citations (model-cited URLs that actually matched something a
search tool retrieved). Around that core sit layers of telemetry the report and
cache lean on: the searched / cited / accepted / rejected source partition, the
search and fetch counts, escalation history, and three sentinels worth knowing
by name even at this altitude — `models_disagreed` (both verifiers grounded a
verdict and *disagreed* → the `VERIFIED_CONTESTED` status), `verification_failed`
(a transient operational error, not a clean "couldn't ground it"), and
`budget_exhausted` (spent the whole search budget without grounding). The full
field tour is [**Ch 10**](10_verification_grounding.md)'s; what matters *here* is simply that this object gets
attached to `Finding.verification`, completing the finding.

**`PipelineResult`** *(defined in `orchestration/pipeline.py`; detail →
[**Ch 7**](07_orchestration.md)).* The aggregate run state the report consumes: the per-spec
`review_result`, the optional `cross_check_result`, every deterministic alert
list carried through from preprocessing, the `cycle_label`, and the list of
`extracted_specs` (so the report's diagnostics banner can count specs whose
extraction raised warnings). It is the single object handed to the exporter.

**`FindingGroup` + `FindingOccurrence`** *(defined in
`orchestration/pipeline.py`; detail → [**Ch 7**](07_orchestration.md)).* These formalize the difference
between a *display* concept and an *executable* one. A `FindingGroup` is "the
same issue, with a representative finding"; its `occurrences` expand to one
`FindingOccurrence` per affected file, each binding the representative to that
file's own pre-merge `original_finding`. The report renders groups; a downstream
applier would walk occurrences. They are produced by `group_findings()` from the
deduplicated list — a clean split so multi-file edits never fan one file's exact
text across files whose text differed.

**`DiagnosticsReport`** *(defined in `orchestration/diagnostics.py`; detail →
[**Ch 14 — Observability**](14_observability.md)).* The in-memory operational health record for a run:
a `run_id`, timestamped `events`, the list of `failed_specs`, and a set of byte
caps and counters (`secrets_redacted`, `events_dropped`) that keep it bounded on
a long batch poll. It is *operational* metadata — "did the machinery work?" —
distinct from the *trust* metadata that lives on each `Finding`. The pipeline
writes to it throughout; nothing downstream depends on it to render results.

### 3.3 The throughline: a `Finding` accumulates context

Step back from the individual objects and the shape of the contract is clear.
The same `Finding` instance travels most of the pipeline, growing as it goes:

```
   born in review        deduped              verified              rendered
   ─────────────         ───────              ────────              ────────
   severity, issue,      + finding_id         + verification:       → trust status
   existingText/         + affected_files       VerificationResult     (Ch 11)
   replacementText,      + occurrence_           (verdict, grounded,  → edit label
   edit_proposal?          originals[]           sources, sentinels)    EDIT_SUGGESTED /
   verification = ∅                                                     REPORT_ONLY
```

Nothing *replaces* the finding; later stages *annotate* it. This is why the data
model is the real architecture: the packages are just the machines that bolt new
context onto a finding as it passes through, and the report at the end is simply
a finding with everything attached. A reader who internalizes this can predict
where any given piece of information lives — "the verdict? that's on
`.verification`; the per-file text? that's in `occurrence_originals`" — without
opening the source.

---

## 4. Cross-cutting principles you can see in the structure

Five design principles are visible *in the layout itself* — you can read them
off the package map and the data model without running anything.

1. **Determinism before any API call.** The `input` package runs the
   deterministic detectors (`PreprocessResult`) and the local token preflight
   *before* a single model request. Certain, free answers are computed first and
   kept in their own data channel; they bypass the model entirely. The model is
   only spent on questions that actually need judgement.

2. **Emit, don't apply.** The data model has an `EditProposal` and an edit
   sidecar, but there is no writer-back anywhere in `src/`. A finding *describes*
   a change (action / existing → replacement / `target_element_id`); it never
   *makes* one. The surgical write-back stack was removed in v3.0.0, and the
   absence is structural: nothing in the dependency graph can mutate a `.docx`.
   Applying edits is a future, separate program's job.

3. **Trust-model output.** `output/report_status.py` exists solely to *classify*
   a finding into one of nine `ReportStatus` labels and one of two
   `EditActionLabel` values — both *derived* from fields already on the
   `Finding`, no new persistence. Trust is a rendering decision made at the end,
   from evidence gathered along the way, by a module that can read a finding's
   status but cannot change it.

4. **Observability as a non-invasive silo.** As §2 detailed, `tracing` watches
   the run through one-way, defensive hooks and never touches the data shapes.
   The structural guarantee — *delete tracing and the contracts are unchanged* —
   is what lets the forensic layer be rich without being risky.

5. **Degrade to safe defaults.** `core/api_config.py` is the single source of
   truth for model capabilities, and an *unknown* model id degrades to defaults
   that disable every capability flag — producing a smaller, safe request rather
   than an API rejection. The configuration layer is built to fail quiet and
   small. The mechanics are [**Ch 12 — Configuration, Models & Token
   Economics**](12_configuration_and_models.md)'s to detail.

---

## 5. How it connects

This chapter is the index for the rest of the book. Each box on the package map
has a chapter that opens it up:

- The **dynamic** counterpart to this static map is [**Ch 3 — A Run, End to
  End**](03_end_to_end_flow.md), which takes the same objects and shows them *moving* — the actual
  sequence of calls from a click to a finished report. We drew the boxes; Ch 3
  animates them.
- **Part II** opens the ingestion and review machines: [**Ch 4 — Input**](04_input.md)
  (`ExtractedSpec`, the element-id scheme, the deterministic pre-screen),
  [**Ch 5 — The Review Engine**](05_review_engine.md) (`Finding`, `EditProposal`, `ReviewResult`,
  prompts and schemas), and [**Ch 6 — Batch Processing**](06_batch_processing.md) (the Message Batches
  backbone every model call rides on).
- **Part III** is coordination and verification: [**Ch 7 — Orchestration &
  State**](07_orchestration.md) (the spine, `PipelineResult`, dedup, the grouping pair), [**Ch 8 —
  Cross-Spec Coordination**](08_cross_spec_coordination.md), [**Ch 9 — Verification I**](09_verification_routing.md) (routing, modes,
  profiles, triage, `VerificationRoutingDecision`), and [**Ch 10 — Verification
  II**](10_verification_grounding.md) (grounding, verdicts, escalation, the cache, `VerificationResult`).
- **Part IV** is where trust becomes visible: [**Ch 11 — The Trust Model & Report
  Output**](11_trust_model_and_output.md) (the nine statuses, the Word report, the edit sidecar).
- **Part V** covers the cross-cutting systems: [**Ch 12 — Configuration, Models &
  Token Economics**](12_configuration_and_models.md) (`core`), [**Ch 13 — The Desktop GUI**](13_gui.md) (`gui` and `main.py`),
  [**Ch 14 — Observability**](14_observability.md) (`tracing` and `diagnostics`), and [**Ch 15 — Quality
  Engineering**](15_quality_engineering.md) (tests and calibration).

---

## 6. Key takeaways

- **Ten packages, five tiers.** A `core` foundation; `input` / `review` /
  `batch` / `cross_check` / `verification` workers; an `orchestration` spine; an
  `output` consumer; a thin `gui` driver; a `tracing` silo. 58 Python modules
  (48 application + 10 initializers) in `src/`.
- **The data model is the contract.** A `Finding` is the unit of currency. It is
  born in review (problem + optional `EditProposal`, empty verdict), stamped with
  a `finding_id` and `occurrence_originals` at dedup, and annotated with a
  `VerificationResult` at verification. It accumulates context; it is never
  replaced.
- **The seams encode the trust thesis.** Deterministic alerts
  (`PreprocessResult`) flow *around* the model; verdicts are gathered by a
  separate `verification` package; the trust label is a *derived* rendering
  decision in `output`; and nothing in the graph can mutate a spec — edits are
  emitted, never applied.
- **Honest structural edges.** The `review`↔`verification` reference is
  `TYPE_CHECKING`-only; `core`/`review` and `batch`/`verification` cycles are
  broken by deliberate function-local imports; `output` duck-types `Finding`;
  and `tracing` is a silo by virtue of one-way, failure-isolated hooks that never
  reshape the data — not by being unimported.
- **Defer for detail.** This chapter is the map. Field-level semantics live in
  each object's owning chapter; the moving picture lives in [**Ch 3**](03_end_to_end_flow.md).
