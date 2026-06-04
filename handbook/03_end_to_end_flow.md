# A Run, End to End: Following the Data from `.docx` to Report

Chapter 2 drew the boxes. This chapter sets them in motion. A reviewer drops a
folder of `.docx` specification sections onto the window, presses one button,
and — somewhere between forty-five minutes and two hours later — a Word report
and a JSON sidecar appear on disk. Between that press and that report, the data
passes through nine recognizable stages, crosses three threads, leaves the
machine entirely to sit in Anthropic's batch queue, and comes back transformed
from raw paragraphs into grounded, severity-ranked, trust-labeled findings.

This is the connective-tissue chapter. Every stage gets *introduced* here at
moderate altitude and then handed off to the chapter that owns its mechanics. If
you want to know precisely how a verdict gets grounded against a cited URL, this
chapter will tell you *when* and *why* that happens and point you to [**Ch 10 —
Verification II**](10_verification_grounding.md); it will not reproduce the grounding logic. Read this chapter
to learn the shape of a run — the order of operations, the object handed across
each boundary, and the invariants that keep the whole thing trustworthy. Read
the owning chapters to learn how each stage works inside.

The signature device of this chapter is a single finding. We pick one
representative defect — a HIGH-severity claim that a plumbing spec cites a stale
edition of a referenced standard — and follow it the whole way: from the
paragraph it lives in, through detection, deduplication, routing, verification,
grounding, classification, and finally into the report and the sidecar. By the
end you should be able to recount the run start to finish and name the chapter
that explains each step.

## The character of a run: asynchronous, batch-centric, walk-away

The first thing to understand about a Spec Critic run is that **most of its
wall-clock time is spent waiting.** Every per-spec review goes through
Anthropic's **Message Batches API** — there is no synchronous review path in
this product. Batching buys roughly 50% on per-token cost and lifts the output
ceiling (the 300k extended-output path is batch-only; see [**Ch 6 — Batch
Processing**](06_batch_processing.md)), at the price of latency: a typical batch returns in 45 minutes to
2 hours, with a 24-hour ceiling the API almost never approaches. The design
assumes the reviewer submits a project and walks away.

That assumption shapes the entire control structure. The run is not one long
function call; it is a sequence of short bursts of local work separated by long
polling waits. Local work — extraction, the deterministic pre-screen, the token
preflight, deduplication, finalize, export — is fast and synchronous. The waits
— the review batch, the verification batch waves, the cross-spec pass — are
where the clock actually moves. Because the program is a desktop GUI, none of
that waiting is allowed to freeze the window, which is why the run is spread
across a foreground thread and a chain of background worker threads. The
threading discipline itself belongs to [**Ch 13 — The Desktop GUI**](13_gui.md); here we care
only about the *order* in which the work happens and *what object* crosses each
boundary.

```
   GUI thread (foreground)         worker threads (daemon)        Anthropic batch service
   ───────────────────────         ───────────────────────       ───────────────────────
   user presses "Submit Batch"
        │ start_review()
        │  validate, set up
        │  DiagnosticsReport,
        │  trace recorder
        ▼
   spawn submit thread ──────────► submit_batch_thread()
                                    _prepare_specs (extract,
                                      pre-screen, preflight)
                                    submit_review_batch() ───────► review batch QUEUED
        ◄───── on_batch_submitted ──┘
        │ "Polling..."
        ▼
   spawn poll+collect thread ─────► poll_and_collect_thread()
                                    poll_batch_bounded() ◄───────► (bounded backoff polling)
                                          … 45 min – 2 hr …
        ◄──── "collecting results" ─┘
        ▼
   spawn collect thread ──────────► collect_batch_results()
                                    collect_review_batch_results()
                                      (+ repair batch) ──────────► repair batch (if needed)
                                    dedup → verify → cross-check
                                      → verify cross-check ──────► verification batch waves
                                    finalize_batch_result()
        ◄──── on_review_complete ───┘
        │ export Word report
        │ + edits.json sidecar
        ▼
   report on disk
```

The foreground thread never blocks on the network. It hands each long-running
phase to a fresh daemon thread and schedules the result back onto itself with
`app.after(0, …)`, gated by a **run-epoch staleness guard** so that a result
from an abandoned or superseded run is silently dropped rather than painted into
a stale UI. Each background thread does one phase and then dispatches the next
phase's kickoff back onto the foreground thread. The entry point that bootstraps
all of this — `main.py` — is a thin PyInstaller-aware shim that imports and calls
`src.gui.gui.main`; it owns nothing but the launch and is covered in [**Ch 13**](13_gui.md).

## The pipeline, top to bottom

Here is the whole pipeline as a single flow, annotated with the object handed
across each arrow. This is the conceptual spine that the rest of the book hangs
off of; each stage names the chapter that owns it.

```
 .docx files (folder or explicit selection)
   │  list[Path]
   ▼
┌─────────────────────────────────────────────────────────┐
│ 1. EXTRACTION  (extract_multiple_specs_cached)            │  → Ch 4
│    parse paragraphs/tables/headers; assign element ids;   │
│    LRU-cached by file identity + content fingerprint      │
└─────────────────────────────────────────────────────────┘
   │  list[ExtractedSpec]   (+ extraction_warnings)
   ▼
┌─────────────────────────────────────────────────────────┐
│ 2. DETERMINISTIC PRE-SCREEN  (preprocess_spec)            │  → Ch 4
│    LEED / placeholder / template-marker / stale-cycle /   │
│    structural / naming detectors — NO API call            │
└─────────────────────────────────────────────────────────┘
   │  alert lists, keyed per filename (pre_detected_by_filename)
   ▼
┌─────────────────────────────────────────────────────────┐
│ 3. TOKEN PREFLIGHT  (_run_exact_token_preflight)          │  → Ch 12
│    exact Anthropic count of the real request shape;       │
│    RAISES ValueError if > RECOMMENDED_MAX (500k)          │
└─────────────────────────────────────────────────────────┘
   │  list[ReviewRequestSpec] (validated to fit)
   ▼
┌─────────────────────────────────────────────────────────┐
│ 4. SUBMIT REVIEW BATCH  (submit_review_batch)             │  → Ch 5 / Ch 6
│    one request per spec via the Message Batches API       │
└─────────────────────────────────────────────────────────┘
   │  BatchSubmission (job + review_request_ids + alerts + specs)
   ▼
┌─────────────────────────────────────────────────────────┐
│ 5. POLL + COLLECT + RECONCILE + REPAIR                    │  → Ch 6 / Ch 7
│    poll_batch_bounded → retrieve_review_results →         │
│    reconcile vs submitted set → repair batch for          │
│    retryable failures                                     │
└─────────────────────────────────────────────────────────┘
   │  list[Finding] (raw, possibly duplicated across specs)
   ▼
┌─────────────────────────────────────────────────────────┐
│ 6. DEDUPLICATE  (_deduplicate_findings)                   │  → Ch 7
│    collapse identical findings; keep per-file occurrences;│
│    stamp a stable finding_id   ← BEFORE verification      │
└─────────────────────────────────────────────────────────┘
   │  CollectedBatchState (deduped findings + alerts + truncated_specs)
   ▼
┌─────────────────────────────────────────────────────────┐
│ 7. VERIFICATION + 8. CROSS-SPEC COORDINATION              │  → Ch 8/9/10
│    route → batch waves → ground → verdict;                │
│    real-time fallback for the small tail                  │
└─────────────────────────────────────────────────────────┘
   │  Findings carrying VerificationResult + ReportStatus
   ▼
┌─────────────────────────────────────────────────────────┐
│ 9. FINALIZE + EXPORT  (finalize_batch_result → exporter)  │  → Ch 11
│    Word report  +  <report-stem>.edits.json sidecar       │
└─────────────────────────────────────────────────────────┘
   │  files on disk — nothing applied to the spec documents
   ▼
 done
```

The orchestration functions that implement stages 3–9 live in
`orchestration/pipeline.py` and are deliberately small and named for what they
do: `_prepare_specs`, `start_batch_review`, `collect_review_batch_results`,
`run_cross_check_for_batch`, `start_batch_verification`,
`collect_batch_verification_results`, and `finalize_batch_result`. Each is a
checkpoint where one named dataclass is produced and the next is consumed. The
internals of those functions — how state threads through `BatchSubmission` →
`CollectedBatchState` → `PipelineResult`, how the dedup key is built — are [**Ch 7
— Orchestration & State**](07_orchestration.md). What follows is the moving picture.

### Stage 1 — Selection and extraction

The reviewer either points the app at a folder or hand-picks specific `.docx`
files. `_prepare_specs` resolves that into a list of paths (skipping Word's
`~$` lock files), then calls `extract_multiple_specs_cached`, which extracts all
specs in parallel and returns one `ExtractedSpec` per document. Each
`ExtractedSpec` carries the flattened text, a paragraph map whose every element
has a stable id (`p7` for a paragraph, `t0r2` for table 0 row 2, `s1h0` for a
section header), and a list of `extraction_warnings` — the latter raised when a
document is so drawing- or object-heavy that meaningful content may not have
survived extraction. Extraction is LRU-cached by file identity and content
fingerprint, so re-running a project after toggling an option skips the DOCX
parse entirely. The mechanics — the XML walk, the element-id scheme, the
content-loss heuristic — are [**Ch 4 — Input**](04_input.md).

A spec that extracts to zero words is logged and dropped. If *every* file fails
extraction, `_prepare_specs` raises and the run ends before any money is spent.

### Stage 2 — The deterministic pre-screen

Before a single token leaves the machine, every spec runs through
`preprocess_spec`: a battery of local, no-API detectors that catch the cheap,
unambiguous defects the LLM should never be paid to find. Each detector carries
a stable `deterministic_rule` id and fires into a typed alert list — LEED
references, placeholders like `[SELECT]`/`TBD`, template markers
(`TODO`/`FIXME`/`XXX`), stale or invalid code-cycle citations, empty sections,
duplicate headings or paragraphs, and project-level filename-consistency
problems. These alerts do two jobs. They become findings in their own right
(rendered under a "(deterministic check)" heading), and they are folded back
into each spec's prompt as a `<pre_detected>` block so the model is *told* what
local rules already caught and does not waste output re-reporting it. The full
detector catalog and the subtle suppression windows (e.g. not flagging "the
*previously* adopted 2019 cycle") are [**Ch 4**](04_input.md).

The ordering here is itself a trust invariant: **local detectors run before any
API call.** The deterministic, explainable checks get first pass; the model is
reserved for the judgment calls.

### Stage 3 — Token preflight that raises

With specs extracted and alerts in hand, `_prepare_specs` builds the *real*
request shape for each spec — system prompt, user message including the
`<pre_detected>` block, the id-tagged paragraph rendering, and the tool schema —
and counts it. The count is authoritative: when the Anthropic `count_tokens`
endpoint is available, `_run_exact_token_preflight` uses its exact number; when
it is not, a local cl100k estimate padded by a model-aware safety multiplier
stands in. Either way, if a spec's request exceeds `RECOMMENDED_MAX` (500,000
tokens), preflight **raises `ValueError`** and the run aborts.

This is a deliberate, hard-won behavior. An earlier design merely logged a
warning and submitted anyway, trusting the API to truncate — which silently
produced reviews of half a spec. Preflight now fails loudly and early rather
than producing a confident-looking report of an incompletely-read document. The
token budgets, safety multipliers, and the small-batch-versus-top-K counting
strategy are [**Ch 12 — Configuration, Models & Token Economics**](12_configuration_and_models.md).

### Stage 4 — Submitting the review batch

`start_batch_review` ties stages 1–3 together (it calls `_prepare_specs`
internally) and then calls `submit_review_batch`, which packages one request per
spec and submits the whole set to the Message Batches API. It returns a
`BatchSubmission`: the live `BatchJob`, the ordered list of `review_request_ids`
(the *submitted set* — this list matters in the next stage), the deterministic
alert lists, the extracted specs themselves (retained so a later stage can repair
failures and surface extraction warnings), the cycle label, and the
cross-check-enabled flag. Control returns to the GUI, the progress bar jumps to
roughly 40%, and the run settles into its long poll. How the review request is
*shaped* — the `submit_review_findings` tool schema, the prompt-cache breakpoints,
the tagged-JSON text fallback — is [**Ch 5 — The Review Engine**](05_review_engine.md); the batch
wrapper itself is [**Ch 6**](06_batch_processing.md).

### Stage 5 — Poll, collect, reconcile, repair

A background thread calls `poll_batch_bounded` with `DEFAULT_REVIEW_POLL_POLICY`,
which polls the batch with progressive backoff and a bounded budget — it will not
poll forever, and if it loses the connection or the batch detaches it reports
that cleanly rather than hanging. The polling mechanics are [**Ch 6**](06_batch_processing.md).

When the batch reaches terminal status, `collect_review_batch_results` does the
careful part. It retrieves the per-request results and then **reconciles them
against the submitted set** — it iterates `submission.review_request_ids`, not
the set of results the API happened to return. A request id with no result
becomes an explicit error and its spec is recorded in `truncated_specs`; a result
that came back truncated (`incomplete`) or unparseable (`parse_error`) is likewise
flagged. **Nothing is silently dropped.** Anything that can be retried —
missing, truncated, unparseable, or errored/expired/canceled — is handed to
`_recover_retryable_review_batch_results`, which submits a *second*, smaller
**repair batch** (with a retry instruction telling the model to spend its entire
budget on findings) and polls it to completion, splicing whatever it recovers
back into the result set. Specs that still fail after the repair batch remain
marked as failed and are excluded from downstream coordination so cross-check
never reasons about a spec that was never successfully reviewed.

### Stage 6 — Deduplication, before verification

Still inside `collect_review_batch_results`, the surviving findings from all
specs are run through `_deduplicate_findings`. The same defect frequently appears
in several specs (twelve plumbing sections can all cite the same wrong edition);
dedup collapses findings that share a dedup identity — normalized issue text,
section, code reference, action type, and hashed existing/replacement text — into
one representative, while **retaining each file's pre-merge original** in
`occurrence_originals` so that per-file edit text survives the merge. Every
finding, merged or singleton, is stamped with a stable `finding_id`. The output
is a `CollectedBatchState`.

That dedup runs **before verification** is one of the load-bearing ordering
invariants of the whole system. Verification is the expensive, web-search-backed
stage; deduplicating first means we pay to verify a claim *once* and bind the
resulting verdict to the single representative finding, rather than verifying
twelve copies and risking a verdict attaching to the wrong one. The dedup key
construction and the occurrence-tracking model are [**Ch 7**](07_orchestration.md).

### Stages 7 & 8 — Verification and cross-spec coordination

Here the *conceptual* flow above (verification and cross-check shown together,
mirroring `CLAUDE.md`'s high-level-flow block) and the *implemented* flow diverge
in a way worth stating plainly, because this is a "follow the data" chapter and
the data follows the code.

In the running GUI batch path (`batch_controller.collect_batch_results`), the
order is strictly sequential and verification of the review findings comes
**first**:

1. **Verify the review findings.** `start_batch_verification` applies a local
   pre-pass — `prepare_findings_for_verification` resolves what it can without the
   network (keyword classifier, then the persistent on-disk cache, then optional
   Haiku triage) and returns only the subset that still needs a web-backed call.
   If everything resolved locally it returns `None` and no batch is submitted.
   Otherwise the remaining findings go out as a verification batch, and
   `collect_batch_verification_results` drives the wave loop.
2. **Run cross-spec coordination.** `run_cross_check_for_batch` (only if the
   reviewer enabled it) feeds the deduped, now-partly-verified review findings —
   minus any the verifier marked `DISPUTED` — to `run_chunked_cross_check`, which
   chunks the project by CSI division and looks for defects that span specs.
3. **Verify the cross-check's own findings**, through the same
   `start_batch_verification` → `collect_batch_verification_results` path, sharing
   the same verification cache.

> **Doc/code drift, flagged.** `CLAUDE.md`'s high-level-flow block lists
> cross-check *before* verification and describes it as "parallel with
> verification by default," and the README's "Pipeline at a Glance" echoes that.
> The implemented GUI batch path is sequential and verifies the review findings
> *first* — `run_cross_check_for_batch` reads each finding's `verification.verdict`
> to drop DISPUTED findings from the "already identified" context, which only
> works if verification has already run. Per the handbook's conflict rule, the
> code wins; this is exactly the species of documentation drift that [**Ch 16 —
> Trust Under the Microscope**](16_trust_under_the_microscope.md) exists to catalog.

The verification stage is itself a small pipeline. Each finding is *routed* into
one of four modes (`local_skip`, `strict_structured`, `standard_reasoning`,
`deep_reasoning`) by severity, profile, and content — the routing, the five
profiles, the severity-tiered search budgets, and the Haiku triage safety
contract are [**Ch 9 — Verification I**](09_verification_routing.md). The web-backed call then tries to
*ground* a verdict: a `CONFIRMED` or `CORRECTED` verdict is only allowed to stand
if the model cited at least one URL that the `web_search` (or `web_fetch`) tool
actually retrieved. Grounding, the verdict taxonomy, escalation (Sonnet → Opus
when an initial pass is uncertain), the "contested" disagreement signal, and the
claim cache are [**Ch 10 — Verification II**](10_verification_grounding.md).

One flow-level detail belongs here because it is about *timing*, not mechanism:
the verification batch runs in **waves** (submit → poll → collect → resubmit the
unresolved), but when the unresolved tail shrinks below
`_REALTIME_FALLBACK_THRESHOLD` (5), the remainder flips to synchronous real-time
calls rather than paying another full batch-poll cycle for a handful of stragglers.
It is a pragmatic latency optimization: batches are cheap per token but slow to
turn around, so a five-finding tail is not worth another 45-minute wait.

### Stage 9 — Finalize and export

`finalize_batch_result` combines the review findings and any cross-check findings
into a single `PipelineResult`, carries the deterministic alert lists and the
extracted specs through (the latter so the report can count extraction warnings),
snapshots every finding's terminal state for the trace, and closes the pipeline
trace span. Control returns to the foreground thread's `on_review_complete`,
which logs the severity tally and calls the report exporter.

The output is **two files and only two files**: a Word `.docx` report, and a
machine-readable `<report-stem>.edits.json` sidecar. The report renders every
finding with its trust-model status, its evidence panel, and — where one exists —
its proposed replacement text shown inline. The sidecar serializes each finding's
structured edit proposal for a downstream applier to ingest. **Spec Critic emits
edit instructions; it never applies them.** The surgical write-back stack that
once mutated documents was removed in v3.0.0. The report layout, the nine-label
trust model, the sidecar schema, and the Run Diagnostics banner are [**Ch 11 — The
Trust Model & Report Output**](11_trust_model_and_output.md).

## Follow one finding

Abstractions blur; a concrete object stays sharp. Let us follow one finding the
whole way.

A plumbing spec, `22 13 16 - Sanitary Waste and Vent Piping.docx`, contains the
sentence: *"Fire-stopping at pipe penetrations shall comply with NFPA 13, 2019
edition."* California's 2025 cycle pins a different adopted edition of NFPA 13.
That is a real defect — a stale standards edition — but not one the deterministic
pre-screen catches: the stale-cycle detector looks for stale *California code
cycles* like "2019 CBC," not for a stale edition of a referenced NFPA standard.
This is precisely the gap the LLM review exists to fill, and following this
finding shows why both layers are needed.

```
LIFECYCLE OF ONE FINDING
────────────────────────
extract    ─► paragraph "…NFPA 13, 2019 edition" lands as element p42 in ExtractedSpec   (Ch 4)
pre-screen ─► deterministic detectors do NOT fire (edition drift ≠ stale CA cycle)        (Ch 4)
review     ─► Opus 4.8 (batch) raises a Finding via submit_review_findings:               (Ch 5)
              severity=HIGH, codeReference="NFPA 13", actionType=EDIT,
              existingText="2019 edition", replacementText="<adopted edition>", target=p42
collect    ─► reconciled against the submitted set; spec parsed cleanly                   (Ch 6/7)
dedup      ─► appears once → kept as singleton, stamped finding_id "rf-…"                  (Ch 7)
route      ─► HIGH + non-empty codeReference ⇒ never local-skip, never Haiku-eligible ⇒   (Ch 9)
              web_required ⇒ standard_reasoning mode (Sonnet 4.6, web_search budget 7)
verify     ─► Sonnet searches the CA Building Standards Commission adoption matrix;        (Ch 10)
              cites a URL the web_search tool actually returned ⇒ grounded;
              verdict = CORRECTED (the 2019 edition is wrong)
classify   ─► CORRECTED + grounded ⇒ ReportStatus.VERIFIED_CONTRADICTED (✎)                (Ch 11)
render     ─► report shows the finding, the cited evidence panel, the proposed edit;       (Ch 11)
              edits.json carries the EditProposal — but nothing is applied
```

Three things in that trace are worth dwelling on, because they are the trust
model made concrete.

First, **the two detection layers are complementary, not redundant.** The
deterministic pre-screen deliberately did *not* fire — and that is correct.
Edition drift inside a referenced standard requires knowing which edition
California adopted for the cycle, which is judgment the reviewer model supplies.
Had this instead been a literal "2019 CBC" reference, the deterministic detector
*would* have caught it first and the model would have been told so in its
`<pre_detected>` block.

Second, **routing was forced, not guessed.** Because the finding is HIGH severity
*and* carries a non-empty `codeReference`, the local-skip and Haiku-triage paths
are contractually forbidden from short-circuiting it (the triage safety contract
in [**Ch 9**](09_verification_routing.md) makes CRITICAL/HIGH and any code-referencing finding ineligible for a
local skip). A claim about a building code is never allowed to slip past
verification on a cost-saving heuristic.

Third, **the verdict had to be grounded to count.** Sonnet did not get to assert
"the 2019 edition is wrong" on its own authority; the `CORRECTED` verdict only
survived because the model cited a URL that the search tool had actually
retrieved. Grounding proves the *source is real* — not, importantly, that the
source proves the claim, a caveat [**Ch 10**](10_verification_grounding.md) is careful about — but it is the line
between an evidence-backed correction and a confident hallucination. Had the
search returned nothing citable, the same finding would have landed as
`INSUFFICIENT_EVIDENCE` instead, and the report would have said so rather than
quietly presenting an ungrounded "correction."

The finding ends its life as a row in the Word report — amber ✎ glyph,
`VERIFIED_CONTRADICTED`, evidence panel naming the adoption-matrix URL, proposed
replacement shown inline — and as an entry in `edits.json` for a future applier.
Spec Critic's last act is to *describe* the edit precisely and hand it off. It
does not touch the spec.

## The ordering invariants that make a run trustworthy

Several of the boundaries above are not arbitrary sequencing; they are the
guarantees that let a reviewer trust the output. Collected in one place:

- **Pre-screen before any API call.** Cheap, deterministic, explainable checks
  run first; the model is paid only for judgment. (Stage 2.)
- **Preflight raises, never truncates.** A spec too large for one call aborts the
  run loudly rather than producing a confident review of a partially-read
  document. (Stage 3.)
- **Reconcile against the submitted set.** Collection iterates the request ids
  that were *submitted*, so a missing or failed result becomes a visible error
  and a flagged spec — never a silent omission. (Stage 5.)
- **Deduplicate before verification.** A claim is verified once and its verdict
  binds to one representative finding; we never verify duplicates and risk a
  verdict attaching to the wrong copy. (Stage 6.)
- **Failed specs are excluded from coordination.** Cross-check never makes a
  coordination claim that rests on a spec whose own review failed. (Stage 8.)
- **Grounding gates the strong verdicts.** `CONFIRMED`/`CORRECTED` require a real,
  retrieved citation, enforced in three independent places. (Stage 8 → Ch 10.)
- **Emit, never apply.** The run's terminal act is to write a report and a
  sidecar; it never mutates a `.docx`. (Stage 9 → Ch 11.)

Each of these exists because the alternative — submit-and-hope, verify-everything,
trust-the-model — produced exactly the failure mode a compliance tool cannot
afford: being confidently wrong about a building code. The throughline of this
book is *making uncertainty visible rather than hidden*, and most of these
invariants are that principle expressed as control flow.

## An honest edge: a partial failure does not shout

There is a flow-level limitation worth naming here and flagging for the chapters
that examine it closely. A run can *partially* fail — a spec truncates and is not
recovered by the repair batch, a verification wave detaches before grounding a
verdict, cross-check is skipped because extracted specs went missing — and when
it does, the resulting artifact does not look *dramatically* different from a
clean run at first glance. The information is present: truncated specs appear as
error-status entries, verification failures land in the `VERIFICATION_FAILED`
status, and the report's **Run Diagnostics banner** surfaces failed-spec and
verification-failure counts in red when they are non-zero. But the report's title
and overall structure are identical whether the run was pristine or limped across
the line; there is no single, unmissable run-level "this report is incomplete"
verdict at the very top. A reviewer skimming the first page could, in principle,
miss that a fifth of the project never got reviewed.

This is a known edge, not a bug in disguise — the signal exists, it is simply
quieter than it ideally would be. The mechanics of how partial failures are
surfaced (and where the gaps remain) are [**Ch 11 — The Trust Model & Report
Output**](11_trust_model_and_output.md); the broader question of whether the artifact's trust signaling matches
its trust *reality* is exactly what [**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md)
audits.

## How this connects

This chapter is the book's index to the pipeline. Each stage's deep dive lives
elsewhere, and you should follow the cross-references rather than expect the
mechanics here:

- **Stages 1–2 (extraction, pre-screen)** → [**Ch 4 — Input**](04_input.md).
- **Stage 4 (review request shape)** → [**Ch 5 — The Review Engine**](05_review_engine.md).
- **Stages 4–5 (batch submission, polling)** → [**Ch 6 — Batch Processing**](06_batch_processing.md).
- **Stages 5–6 (collection, reconciliation, dedup, state objects)** → [**Ch 7 —
  Orchestration & State**](07_orchestration.md).
- **Stage 8 (cross-spec coordination)** → [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md).
- **Stage 7/8 routing** → [**Ch 9 — Verification I**](09_verification_routing.md); **grounding/verdicts/cache**
  → [**Ch 10 — Verification II**](10_verification_grounding.md).
- **Stage 9 (report, sidecar, trust labels)** → [**Ch 11 — The Trust Model &
  Report Output**](11_trust_model_and_output.md).
- **Token preflight and budgets** → [**Ch 12 — Configuration, Models & Token
  Economics**](12_configuration_and_models.md).
- **The threading model, the run-epoch guard, `main.py`** → [**Ch 13 — The Desktop
  GUI**](13_gui.md).
- **The trace spans woven through every stage** → [**Ch 14 — Observability**](14_observability.md).
- **The static box-and-arrow architecture and the data-model map** → [**Ch 2 —
  Architecture at a Glance**](02_architecture.md) (which this chapter set in motion).

## Key takeaways

- A run is **asynchronous and batch-centric**: short bursts of local work
  separated by long batch waits, spread across a foreground GUI thread and a
  chain of daemon worker threads, with a run-epoch guard discarding stale results.
- The pipeline has **nine recognizable stages**, each producing one named object
  for the next to consume: `ExtractedSpec` → alerts → validated `ReviewRequestSpec`
  → `BatchSubmission` → `CollectedBatchState` → verified findings → `PipelineResult`
  → report + sidecar.
- The order is load-bearing: **pre-screen before API, preflight that raises,
  reconcile against the submitted set, dedup before verification, grounding before
  a strong verdict, emit before — never instead of — apply.**
- **Following one finding** — a HIGH-severity stale-edition claim — shows the
  layers cooperating: the deterministic screen correctly stays silent, the
  reviewer raises it, routing refuses to skip it, the verifier grounds a
  `CORRECTED` verdict, and it lands as `VERIFIED_CONTRADICTED` in the report and an
  `EditProposal` in the sidecar — with nothing applied.
- The implemented GUI path **verifies review findings before cross-check**,
  diverging from the "parallel" description in `CLAUDE.md`/README; the code wins,
  and the drift is a [**Ch 16**](16_trust_under_the_microscope.md) concern.
- A **partial failure is recorded but not loud** — surfaced in statuses and the
  Run Diagnostics banner, yet not as a top-level "incomplete run" headline. An
  honest edge, detailed in [**Ch 11**](11_trust_model_and_output.md) and audited in [**Ch 16**](16_trust_under_the_microscope.md).
