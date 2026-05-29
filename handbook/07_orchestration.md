# Orchestration & State: The Pipeline Spine

Every chapter so far has described a *worker*: the extractor that turns a `.docx`
into reviewable text, the review engine that turns text into findings, the batch
backbone that ferries requests to Anthropic and back. This chapter describes the
thing that holds them in order. `src/orchestration/pipeline.py` is the **spine**
— roughly eleven hundred lines that sequence every stage, carry the run's state
across the long gaps between batch calls, and perform the trust-critical *joins*
that the workers can't do for themselves: assigning each finding a stable
identity, collapsing the same defect found in five specs into one, refusing a
doomed run before it spends a dollar, and reconciling what the batch sent back
against what was actually submitted.

It is also the chapter the structural audit cared about most, and for a precise
reason. The leaf functions can each be perfectly correct — a verdict correctly
grounded, an edit proposal correctly shaped, a detector correctly firing — and a
run can *still* mislead, if the spine drops a result quietly, binds a verdict to
the wrong finding, merges two distinct edits into one, or lets a partially-failed
run wear the same green checkmark as a clean one. The spine is where correct leaf
output goes to be either honestly assembled or quietly corrupted.

The headline, stated up front so the rest of the chapter can earn it: **the data
plane is sound, and the edges are where the work remains.** The joins are
correct — dedup runs before verification, a verdict cannot attach to the wrong
finding, batch results are reconciled against the submitted set, and nothing is
silently dropped at the data layer. But the program does not yet make a
*partially failed* run obviously distinguishable from a *fully clean* one in its
final deliverable. For a compliance tool, that gap between "we reviewed all five
specs and they're clean" and "two of five silently failed" is the single most
important thing still being perfected, and this chapter is where it lives.

## The state objects: what survives the gaps between calls

In a synchronous program, "run state" is just local variables on a call stack.
Spec Critic doesn't get that luxury. Because the review goes through the Message
Batches API ([**Ch 6 — Batch Processing**](06_batch_processing.md)), the run is not one function call; it
is a sequence of *separate* calls — submit, poll, collect, finalize — with
forty-five minutes to two hours of wall-clock between the first and the third.
The GUI spawns a fresh worker thread for each phase ([**Ch 13 — The Desktop
GUI**](13_gui.md)). So the spine cannot keep its state on a stack; it has to keep it in
*objects* that are handed forward across the gaps. There are three, and they form
a chain:

```
  _prepare_specs ──► BatchSubmission ──► CollectedBatchState ──► PipelineResult
     (extract,         (the job +          (review results +       (the finished
      pre-screen,       everything          dedup'd findings +       run, ready
      preflight)        needed to           cross-check +            for the
                        collect later)      truncated_specs)         exporter)
```

**`BatchSubmission`** is what `start_batch_review` returns. It carries the live
`BatchJob`, the ordered `review_request_ids` (the submitted set — this list is
load-bearing later), the `model`, `project_context`, every deterministic alert
list from the pre-screen, and — critically — `prepared_specs`, the actual
extracted spec objects. Holding the specs here is what lets the *repair batch*
re-submit a failed item without re-extracting, and what lets cross-check and the
diagnostics banner read spec content after the original extraction call is long
gone.

**`CollectedBatchState`** is what `collect_review_batch_results` produces once
the batch comes home. It wraps the `BatchSubmission`, adds the combined
`ReviewResult` (findings, thinking, token totals), the `cross_check_result` slot,
and — the field the audit keeps returning to — `truncated_specs`, the list of
specs whose review failed. It threads every alert list forward and carries a
`trace_span_id` so the batch-mode root span can be closed at the very end.

**`PipelineResult`** is the terminal object the report and sidecar consume. It is
deliberately a *consumer-facing* shape: `review_result`, `cross_check_result`,
the `cycle_label`, total elapsed time, every deterministic alert list, and
`extracted_specs` (so the report's diagnostics banner can count specs whose
extraction raised warnings). Notably, it does **not** carry `truncated_specs` —
and that omission, harmless-looking here, is the seam where the P0-1 honesty gap
opens. We will come back to it.

A fourth object, `_PreparedSpecs`, lives only inside `_prepare_specs` and never
escapes; it is the bundle of extracted specs plus per-filename alert maps that
the submission is built from. The whole spine, then, is a controlled hand-off of
these objects across thread and time boundaries:

```
 list[Path]
   │  _prepare_specs  (extract → pre-screen → token preflight; RAISES on overage)
   ▼
 _PreparedSpecs ──────────────────────────────────────────────┐
   │  start_batch_review → submit_review_batch                 │
   ▼                                                            │ alert lists
 BatchSubmission {job, review_request_ids, prepared_specs}     │ ride through
   │  (… 45 min – 2 hr in Anthropic's queue …)                 │ every object
   │  collect_review_batch_results                             │
   │    ├─ retrieve_review_results        (Ch 6)               │
   │    ├─ _recover_retryable_…  ← REPAIR BATCH                │
   │    └─ _deduplicate_findings  ← finding_id + merge         │
   ▼                                                            │
 CollectedBatchState {review_result, truncated_specs}          │
   │  run_cross_check_for_batch  (optional; excludes failed)   │  → Ch 8
   │  start_batch_verification → collect_batch_verification…   │  → Ch 10
   │    (writes Finding.verification by stable index)          │
   │  finalize_batch_result                                    │
   ▼                                                            ▼
 PipelineResult {review_result, cross_check_result, …} ◄───────┘
```

The order on that diagram is not incidental — three of its steps (preflight,
dedup-before-verify, reconcile-against-submitted) are the trust-critical
decisions this chapter exists to explain. Take them in turn.

## Before the spend: the preflight that refuses

The first trust decision happens before any model is called at all. A single
CSI-format spec can be enormous; a request that exceeds the model's usable
context window will either be silently truncated by the API or rejected outright,
and a *silently truncated* spec is the worst outcome a compliance tool can
produce — it looks reviewed but half of it was never seen.

`_prepare_specs` extracts every spec, runs the deterministic pre-screen on each
(collecting LEED, placeholder, template-marker, stale-cycle, structural, and
naming alerts), and then runs `_run_exact_token_preflight` over the *exact request
shape the batch will submit* — system prompt, the per-spec user message
*including* its `<pre_detected>` alert block, the tool schema, and cache controls.
This matters: a spec with a small body but a large alert block must not slip past
a check that only weighed the body. The builder that owns the request shape
(`ReviewRequestSpec`) is the same one the batch path uses, so the count is honest.

The non-obvious invariant — and one `CLAUDE.md` calls out explicitly — is that
**preflight raises; it does not warn.** When the exact Anthropic token count
exceeds `RECOMMENDED_MAX` (500,000), `_run_exact_token_preflight` throws
`ValueError` and the run stops before submission:

```python
if exact_tokens > RECOMMENDED_MAX:
    raise ValueError(
        f"Spec '{rs.filename}' is too large for a single API call: "
        f"exact API token count {exact_tokens:,} exceeds recommended "
        f"maximum {RECOMMENDED_MAX:,} for model {model}."
    )
```

An earlier version of this code logged a warning and carried on with only a
cl100k-based gate. The decision to *refuse* is the trust thesis in miniature:
**a run that cannot be done correctly should not be done at all.** A loud failure
the reviewer must act on beats a quiet success that reviewed two-thirds of a
document and reported zero findings on the third nobody saw.

Two defenses sit behind that single raise, and they cover each other. The exact
Anthropic `count_tokens` call is the *authoritative* gate, but it is a real API
call with real latency, so for projects above eight specs the spine exact-counts
only the top four candidates — ranked by the **full local request shape**, not
raw body length, so reordering files can't sneak a large request past. Every
spec, counted exactly or not, then passes a second *local* gate: a cl100k_base
estimate padded by a model-aware safety factor (`exceeds_per_call_limit_for_model`
→ `safe_local_estimate`). The exact count catches the truth; the padded local
count catches the spec the exact pass didn't reach. Neither alone is trusted to
be the whole answer. (The token-economics machinery — the safety multipliers, the
`RECOMMENDED_MAX` rationale — is [**Ch 12 — Configuration, Models & Token
Economics**](12_configuration_and_models.md)'s; here it is enough that the spine *enforces* it by refusing.)

## Reconciliation: nothing is silently dropped at the data layer

When the batch comes home, `collect_review_batch_results` faces the question the
batch layer deliberately left to it ([**Ch 6**](06_batch_processing.md)): the wrapper returns a dictionary
keyed by `custom_id`, and a submitted spec that *never came back* is simply absent
from that dictionary. Detecting absence — and every other species of failure — is
the spine's job, and the way it does it is the foundation of the audit's
"data plane is honest" verdict.

The function does not iterate the *results*; it iterates the **submitted set**,
`submission.review_request_ids`, looking each one up:

```python
for rid in submission.review_request_ids:
    rr = results_by_request.get(rid)
    if rr is None:                       # never came back
        errors.append(f"{filename}: No result returned from batch")
        truncated_specs.append(filename)
        continue
    if rr.parse_status == "incomplete":  # truncated mid-output
        ...; truncated_specs.append(filename); continue
    if rr.parse_status == "parse_error": # unparseable even after salvage
        ...; truncated_specs.append(filename); continue
    if rr.error:                         # errored / expired / canceled
        ...; truncated_specs.append(filename); continue
    all_findings.extend(rr.findings)     # the only success path
```

Because the loop is driven by *what was submitted* rather than *what returned*,
there is no way for a spec to fall through silently. Every one of the five
outcomes the batch layer can hand back — ok, incomplete, parse_error, errored, or
absent — lands in exactly one branch, and four of the five append the filename to
`truncated_specs` and a human-readable line to `errors`. The combined
`ReviewResult.error` is then set to `"N spec(s) had errors: …"`. The audit
re-read this path personally and filed it under *verified-clean*: "Review batch
results are NOT silently dropped … the caller iterates the submitted ids and
turns any missing/incomplete/errored result into a visible error + `truncated_specs`."

### The repair batch

Before that reconciliation even runs, the spine gives failures a second chance.
`_recover_retryable_review_batch_results` scans the returned results for the
*retryable* species — a missing result, an `incomplete` or `parse_error` status,
or an error string naming a batch-level errored/expired/canceled item — and if it
finds any, it rebuilds the original `ExtractedSpec` objects from
`submission.prepared_specs` (by the `index` recorded in the job's request map),
recomputes their deterministic alerts, and submits a **second batch** with a
pointed retry instruction:

> *"This is a retry of a previously truncated review … Spend the entire output
> budget on the findings array."*

It polls that repair batch with the same bounded poller ([**Ch 6**](06_batch_processing.md)), retrieves the
results, and overwrites the failed entries in place. The logging is deliberately
honest about partial recovery: `"Review repair batch recovered {recovered}/{N}
item(s)"` at `success` level only when *all* recovered, `warning` otherwise. And
if the repair batch itself detaches or fails to poll, it logs — verbatim — that
*"{N} item(s) will appear as failed in the report"* and returns the originals
untouched, so they stay in `truncated_specs`.

That last sentence is the hinge of audit item **P1-3**. The repair layer is good
resilience, but its failure path depends *entirely* on the report actually
surfacing `truncated_specs`. The data is recorded faithfully; whether the final
artifact shows it is a separate question — the one the next-to-last section
confronts.

## Deduplication: the load-bearing join

DSA master specs are templated. The same boilerplate paragraph — and therefore
the same defect — routinely appears verbatim across a Division 23 HVAC spec, a
Division 22 plumbing spec, and three others. If the reviewer flags it five times,
the report should say "this issue, in five specs," once — not print the same
finding five times, and certainly not verify it five times. `_deduplicate_findings`
is that collapse, and *where* it runs in the sequence is as important as *how*.

**Dedup runs before verification, and that ordering is trust-critical.** Once the
findings are deduplicated, the list is fixed and not reordered, and verification
writes each verdict back by a stable index into that frozen list
(`findings[finding_idx].verification = …`, with the `original_custom_id`
preserved across retry and continuation waves — the verifier mechanics are
[**Ch 10 — Verification II**](10_verification_grounding.md)'s). If dedup ran *after* verification, or if the list
were reordered between submit and collect, a verdict could bind to the wrong
finding — the one trust failure that would silently corrupt every report. By
freezing identity first, the spine guarantees a verdict can only ever land on the
finding it was computed for. The structural audit confirms this is "currently
sound," with the explicit note that it *relies on the list not being reordered* —
a robustness constraint the spine honors by treating the deduped list as
immutable from that point on.

### The dedup key, and why a false merge is impossible

Two findings merge only if their `_dedup_key` is identical, and the composition of
that key is a careful, defensive choice. The dangerous failure mode is the
*opposite* of leaving duplicates: it is falsely merging two findings whose edit
text actually differs, because then one file's correct replacement would be
applied to another file whose original differed. The key forecloses that by
hashing the **full** edit text:

| Field in the key (→ controls whether two findings merge) | Field *not* in the key (→ collapses to the representative on merge) |
|---|---|
| normalized `issue` text (filenames stripped, whitespace folded, lower-cased) | `fileName` → becomes `files[0]`, the representative's file |
| `section` (trimmed, lower-cased) | `confidence` → becomes the group `max` |
| `codeReference` (trimmed, lower-cased) | `severity` → the representative's |
| `actionType` | `anchorText` |
| **SHA-256 of `existingText`** (full text, not truncated) | `insertPosition` |
| **SHA-256 of `replacementText`** (full text, not truncated) | `evidenceElementId` |

The two text digests are the guard. An earlier implementation hashed only the
first ~200 characters; the inline comment that replaced it is blunt about why that
was wrong — *"Truncating before hashing silently merged distinct findings."* Two
long passages that happen to share an opening paragraph would have collided and
merged into one edit. Hashing the whole string makes that collision impossible:
two findings merge only when their edit text is byte-identical. The TRUST_AUDIT
filed this under *verified-clean* — "Dedup will not falsely merge distinct edits …
the key includes full-text SHA-256 digests … so two findings only merge when their
edit text is byte-identical."

The right-hand column carries its own honest edge, and the TRUST_AUDIT names it as
**P0-2**: `anchorText`, `insertPosition`, and `evidenceElementId` are *not* in the
key, so members of a merged group can differ on them, and the merge keeps only the
representative's values. For an `ADD` action, `anchorText` is the locator — so a
multi-file `ADD` could carry the representative file's anchor and mislocate the
insertion in the others. The per-file values aren't lost — they're preserved in
`occurrence_originals`, below — but whether anything downstream *reads* them is the
question P0-1/P0-2 turn on.

### Stable identity, and the merge

Each finding gets a deterministic id at dedup time. `compute_finding_id` hashes
the same `_dedup_key` and truncates to twelve hex characters: `rf-{12 hex}`. Two
findings with the same dedup identity therefore share an id, and a merged
representative carries the id of its group. Twelve hex is 48 bits — the
structural audit notes this as **P2-2**, "collision negligible at the typical
<100 findings/run scale," and leaves it. The id is what lets the report and the
JSON sidecar refer to the same finding by name, and what lets the cross-check pass
label the prior findings it was shown.

When a group has more than one member, the merge builds a fresh representative
`Finding` that:

- takes the highest-severity / highest-confidence member's fields (the group is
  sorted by `(severity_rank, -confidence)` and the first is the representative);
- rewrites `issue` to append `"(found in N specs: a.docx, b.docx, …)"`;
- sets `affected_files` to the de-duplicated list of every member's filename;
- carries the representative's `edit_proposal`, `finding_id`, and `demotion_reason`
  forward (so a `REPORT_ONLY` finding stays `REPORT_ONLY`, and a demoted edit
  can't be silently rehydrated by the merge); and
- stores **`occurrence_originals` = the full list of member findings**, including
  the representative itself.

That last field is the one to remember. `occurrence_originals` is how a merged
finding *remembers its parts*: each member is a per-file singleton carrying that
file's own exact `existingText` / `replacementText` / `anchorText`, so the
information needed to emit a correct, file-specific edit instruction for *every*
affected file survives the collapse. The merge does not recurse — members keep
their own `occurrence_originals` empty — so this terminates after one level.

## Multi-file grouping: a contract built, not yet wired

The spine goes one step further than preserving per-file originals: it defines a
formal pair of types to *consume* them. `FindingGroup` is the display concept —
"the same issue, with a representative" — and its `occurrences` list expands
`affected_files` into one `FindingOccurrence` per file. Each occurrence binds the
representative finding to that file's pre-merge `original_finding`, and exposes
`executable_finding()`, which returns the per-file original when one exists and
falls back to the representative otherwise. The intent, as `CLAUDE.md` states it,
is that "per-file `existingText` / `replacementText` differences survive the merge
for the report and the edit-instruction sidecar."

Here is the honest part, and it must be stated plainly because the TRUST_AUDIT
makes it the spine's **P0-1**: **`group_findings()` is currently called only from
tests, never from production.** A grep confirms its only caller is
`tests/test_dedup_edit_identity.py`. The data is faithfully preserved on every
merged `Finding` — `occurrence_originals` is populated correctly — but nothing in
the live pipeline walks it. The downstream consumer that *would* fan a multi-file
defect out into one edit instruction per file exists as a tested, working helper,
and is simply not wired in. The result, traced through to the artifact: when the
same defect spans five specs, the edit sidecar emits **one** instruction, keyed to
the representative file (`fileName = files[0]`), and does not even include
`affected_files` in the entry. A downstream applier reading the sidecar would fix
the one file and leave the identical defect in the other four with no
machine-readable instruction at all — only the human-readable `issue` string says
"found in 5 specs."

This is squarely the *emit-but-don't-apply* contract showing its current seam. The
representative-only emission is honest (it's not *wrong* about the file it names),
but it is *incomplete* for a tool whose whole point is machine-readable edits. The
fix is small and the machinery is already built and tested — wiring
`group_findings()` + `executable_finding()` into the sidecar, or at minimum
including `affected_files` per entry — and it belongs to the chapter that owns the
sidecar's shape: [**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md). The spine's
responsibility, met today, is to *preserve* the per-file data; realizing it in the
artifact is the open work.

## Handing findings to verification, and getting them back

After dedup (and the optional cross-check pass, whose chunking is [**Ch 8 —
Cross-Spec Coordination**](08_cross_spec_coordination.md)'s), the spine hands the frozen finding list to
verification. Its half of that contract is narrow and worth isolating from the
verifier's, because the audit (P1-2) raised a shared concern that splits cleanly
along this seam.

`start_batch_verification` runs the local pre-pass (`prepare_findings_for_verification`)
which resolves local-skip and cache-hit findings *in place* — writing
`f.verification` directly — and returns only the `remaining` findings that need a
web-backed check. If none remain, it returns `None`, and the spine treats that as
"verification complete" without polling. Otherwise it submits the remaining
findings as a batch and stamps `job.submitted_findings = remaining`.
`collect_batch_verification_results` then drives the wave loop and, crucially,
verification writes each verdict back by **stable index** into that submitted list:
`findings[finding_idx].verification = parsed`.

The spine's contribution to correctness here is exactly the dedup-before-verify
ordering established above: it hands verification a list that is *already
deduplicated and will not be reordered*, so the index a verdict carries can only
bind to the finding it was computed for. The audit's open question — **P1-2** —
is about the *verifier* side: on the final wave, a shrunken unresolved tail flips
from batch to real-time, and the audit flagged that it had not personally traced
whether a tail finding could be both submitted to an in-flight wave *and* run
real-time (last-writer-wins) or dropped by both. That is a question about the
verifier's wave mechanics, not the spine's hand-off, and it belongs to [**Ch 10 —
Verification II**](10_verification_grounding.md). The spine side is sound: a stable list out, the same objects
annotated in place, the same objects read back.

## `finalize_batch_result`: assembling the artifact

The terminal join is almost anticlimactic, which is the point — by the time
`finalize_batch_result` runs, every hard decision has been made. It concatenates
the review findings and the cross-check findings into one list, snapshots each
finding's terminal state into the trace, closes the batch-mode pipeline span, and
packs everything into a `PipelineResult`: the review and cross-check results, the
`cycle_label`, total elapsed time, every deterministic alert list, and the
`extracted_specs` (so the report can read each spec's `extraction_warnings`).

Two details in this otherwise-quiet function carry weight. First, the cross-check
findings are appended here **without** going through `_deduplicate_findings` — and
therefore without `compute_finding_id`. This is audit item **P1-1**: every
coordination finding reaches the sidecar with `finding_id = ""`. The report
doesn't key by id, so nothing crashes, but a downstream applier that dedupes or
cross-references edits by id sees every coordination edit sharing the empty key.
The fix is contained — stamp `compute_finding_id` on cross-check findings (and
consider running them through dedup too) — and the surfacing belongs to [**Ch 11**](11_trust_model_and_output.md).

Second, `finalize_batch_result` is where `truncated_specs` *stops*. The
`CollectedBatchState` carried it faithfully through collect and cross-check; the
`PipelineResult` does not have a field for it. That dropped thread is the
mechanical cause of the headline gap.

## The edges: where a partial failure can still look clean

Set the data plane's correctness against the artifact's honesty in one table, and
the gap is stark:

| What the data layer records (correctly) | What the final artifact shows |
|---|---|
| `truncated_specs` — every spec whose review failed | *(nothing — `PipelineResult` has no such field; the report has no "specs that failed review" row)* |
| `combined.error = "N spec(s) had errors: …"` | the report header prints `Files Reviewed: {len(files_reviewed)}` — and `files_reviewed` counts **all submitted specs**, including the failed ones |
| per-spec `errors` lines + per-spec GUI log warnings | a transient log line a reviewer can miss |
| repair-batch miss logged as "will appear as failed" | the GUI shows a green ✓ and finalizes diagnostics as "success" regardless |

This is the structural audit's **P0-1**, and the audit calls it "the headline
finding." A spec that *failed* review — zero findings because it truncated,
parse-errored, or never returned — is **indistinguishable in the exported `.docx`
and the UI's terminal state from a spec that was reviewed cleanly and genuinely
had zero findings.** For a compliance-review tool, that is the difference between
trustworthy and dangerous. The audit is careful to scope it precisely: this is
*not* a data-loss bug. The failures are recorded honestly at every layer up to
`CollectedBatchState`. It is a *surfacing* gap — the last two hops (the
`PipelineResult` shape and the report/GUI rendering) don't carry the signal
through. The data already exists; the fix is to plumb `truncated_specs` into a
"Specs that failed review" diagnostics-banner row (red when > 0), correct
"Files Reviewed: N" to "{reviewed}/{submitted}", and give the GUI a distinct
"Completed with errors" terminal state instead of an unconditional green
checkmark. Those renderings are owned by [**Ch 11 — The Trust Model & Report
Output**](11_trust_model_and_output.md) (the banner and report) and [**Ch 13 — The Desktop GUI**](13_gui.md) (the terminal
state); the spine's part of the fix is the one missing field on `PipelineResult`.

It is worth noting, for contrast, what the program *does* get right here, because
it shows the gap is a localized omission rather than a systemic blind spot.
*Verification*-stage failures **are** surfaced honestly: a per-finding
`verification_failed=True` becomes the `VERIFICATION_FAILED` status with a
dedicated red banner row. The machinery to surface a failed stage exists and is
wired — for verification. The same wiring simply hasn't been extended to
*review*-stage failures. The asymmetry is the whole bug.

The lower-priority edges round out the honest picture:

- **P1-1 — cross-check findings carry `finding_id = ""`** (above): a sidecar
  traceability gap, not a crash.
- **P1-2 — the batch→real-time verification handoff** (above): the spine side is
  sound; the tail-fallback question is [**Ch 10**](10_verification_grounding.md)'s to settle definitively.
- **P2-2 — `finding_id` is 48 bits**: negligible collision risk at this scale.
- **P2-3 — a stale doc claim** that cross-check runs "parallel with verification";
  the batch flow actually runs sequentially (review → verify → cross-check →
  verify cross-check), which is *safer* — no shared-`Finding` race. The honest
  note belongs in the docs.

None of these is a present data-loss bug. The spine loses nothing; the work that
remains is making the artifact say everything the data already knows.

## How it connects

- **Upstream — the batch backbone.** The wrapper that submits, polls, and maps
  results back by `custom_id`, and the honest courier contract the spine relies on
  (every result classified, nothing dropped), is [**Ch 6 — Batch Processing**](06_batch_processing.md). The
  spine is the "caller" Ch 6 hands its reconciliation responsibility to.
- **The review request shape** the preflight counts and the repair batch rebuilds
  is [**Ch 5 — The Review Engine**](05_review_engine.md)'s; the extraction and pre-screen that feed
  `_prepare_specs` are [**Ch 4 — Input**](04_input.md)'s.
- **Cross-spec coordination** — the chunked-by-CSI-division pass the spine invokes
  via `run_cross_check_for_batch`, and which excludes failed specs from its input
  — is [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md).
- **Verification** — the wave loop, grounding, escalation, and the real-time
  fallback whose tail-handoff is the verifier half of P1-2 — is [**Ch 10 —
  Verification II**](10_verification_grounding.md). The spine owns only the hand-off (a stable list out, verdicts
  read back by index).
- **The honesty gap's fix** — the "specs that failed review" banner row, the
  corrected "Files Reviewed" count, the per-file sidecar fan-out, and stamping ids
  on cross-check findings — is rendered in [**Ch 11 — The Trust Model & Report
  Output**](11_trust_model_and_output.md); the GUI terminal state is [**Ch 13 — The Desktop GUI**](13_gui.md).
- **The audit framing** that organizes this chapter — data-plane-sound,
  edges-still-perfecting — is the subject of [**Ch 16 — Trust Under the
  Microscope**](16_trust_under_the_microscope.md), and `DiagnosticsReport`, the operational record the spine writes
  to, is [**Ch 14 — Observability**](14_observability.md)'s.

## Key takeaways

- **The spine carries state across time and threads** in three hand-off objects —
  `BatchSubmission` → `CollectedBatchState` → `PipelineResult` — because a
  batch-mode run is a sequence of separate calls, not one stack frame.
- **The preflight raises, it does not warn.** A spec over `RECOMMENDED_MAX`
  (500k tokens) stops the run before submission, behind two mutually-covering
  gates (exact Anthropic count + padded local estimate). A loud refusal beats a
  silent truncation.
- **Reconciliation is driven by the submitted set.** `collect_review_batch_results`
  iterates the submitted `custom_id`s, not the returned ones, so no failure falls
  through; a **repair batch** retries failures before the run is declared done.
  Nothing is dropped at the data layer.
- **Dedup runs before verification, and that ordering is load-bearing.** It
  freezes finding identity so a verdict (written back by stable index) can only
  bind to the finding it was computed for. The dedup key's **full-text SHA-256
  digests** of `existingText`/`replacementText` make a wrong-text merge impossible.
- **The per-file machinery is built but not wired.** `occurrence_originals` is
  preserved on every merged finding, but `group_findings()` is test-only today, so
  the sidecar emits one instruction per multi-file defect (TRUST_AUDIT P0-1/P0-2).
- **The one place the program can still mislead is its edges.** A partially-failed
  run is not yet distinguishable from a clean one in the report or the UI
  (STRUCTURAL_AUDIT P0-1): `truncated_specs` is recorded faithfully but dropped at
  `finalize_batch_result` and never rendered. The data is honest; the artifact
  isn't yet. That is the spine's most important unfinished work.
