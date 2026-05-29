# Trust Under the Microscope: The Audits

Every chapter before this one has argued, in its own way, that Spec Critic is
built to be trustworthy. The deterministic pre-screen catches the cheap defects
before a model is ever consulted. The review prompts are shaped to force
structured, checkable output. Verification refuses to call a claim *confirmed*
unless it can point at a URL a search tool actually retrieved. The trust model
hands the reader nine distinct ways to believe a finding, and the report renders
every one of them in a different color. The whole machine is engineered around a
single conviction, repeated throughout this book: *a compliance tool that is
confidently wrong is worse than no tool at all.*

But conviction is not evidence. A system can be designed for trust and still leak
it — at a join nobody re-read, in a banner row nobody added, in a beta header
that quietly expired. The only way to know whether the design held is to send
someone in to break it on purpose. This chapter is the story of that attempt.

Two formal audits live in the repository — `TRUST_AUDIT.md` and
`STRUCTURAL_AUDIT.md` — and they are kept there, in full, including the parts
where the auditor's own automated helpers were wrong. That is itself part of the
trust story. A tool that asks its users to trust its judgment about building
codes has no business hiding its own self-assessment. The audits are not
marketing; they are a prioritized list of *things that could betray the user*,
ranked by how badly and how silently each one could do it. This chapter
consolidates them into one honest answer to the question a new engineer (or a
nervous reviewer) will inevitably ask: **start to finish, how much can I trust
this program, and where are its edges?**

## Two lenses on the same question

The two audits ask the same question — "can I trust this?" — from opposite ends
of the system. The first looks at the **leaves**: the small, trust-critical
functions that decide things in isolation. Does grounding actually reject a
fabricated URL? Does the dedup key actually prevent two different edits from
being merged? Does `classify_status` actually pick the right label? The second
looks at the **spine**: the orchestration, the data joins, the state machine, the
error handling, and — most importantly — the *honesty of the final artifact*.
Even if every leaf function is perfect, can the spine silently drop, misattribute,
or misrepresent that perfect output before it reaches the user?

| | **Trust Audit** (`TRUST_AUDIT.md`) | **Structural Audit** (`STRUCTURAL_AUDIT.md`) |
|---|---|---|
| **Lens** | Leaf correctness | Spine correctness |
| **Question** | Are the trust-critical functions correct *in isolation*? (grounding, edit proposals, detectors, status classification) | Can correct leaf output be silently dropped, misattributed, or presented as something it isn't? |
| **Headline finding** | Multi-file findings emit only **one** edit instruction — a correct verdict produces an *under-applied* external instruction (sidecar under-emission). | A **partially failed** run is not made distinguishable from a **fully clean** one in the final report or UI. |
| **Overall posture** | The verification/grounding *core* is genuinely well-built; risk lives in edit-emission and input completeness. | The data plane is honest and the joins are sound; the one systemic weakness is at the *edges* — surfacing partial failure. |

The two lenses converge on the same surprising conclusion, and it is worth
stating up front because it inverts most people's intuition: **the large language
model is not the weak link.** The verification engine — the part that reasons
about building codes, the part you would expect to be the most fragile — is the
strongest, most defended part of the system. The risk lives where it always
lives in real software: at the seams. In whether a merged finding fans back out
to every file it affects. In whether a failed spec looks different from a clean
one. In whether a configuration default has quietly gone stale. The audits' job
was to find those seams, and they did.

## The method — and why the method is part of the story

Both audits used the same approach, and it is deliberately the same approach the
*product itself* uses to decide what to believe:

1. **Three parallel code sweeps.** Independent automated passes over the
   trust-critical paths, each free to flag anything suspicious.
2. **A careful personal re-read.** The auditor then read the trust-critical paths
   directly — the grounding gate, the batch collect/repair logic, the
   finding↔verification join, the GUI completion path — to separate real issues
   from noise.

That second step was not a formality. In both audits, **sub-agent sweeps produced
confident "CRITICAL" claims that turned out to be false alarms.** The trust audit
filtered out two; the structural audit filtered out three. These were not subtle
near-misses — they were assertions of severe bugs that simply weren't there:

- A sweep flagged the DOCX content-loss threshold as an inverted-polarity
  CRITICAL bug; the re-read showed `extractor.py` implements the documented "warn
  when proportion `>` 0.20" exactly correctly (see **Ch 4 — Input**).
- A sweep claimed review batch results were "silently dropped" — built on reading
  one function and *assuming* its caller. The actual caller,
  `collect_review_batch_results`, iterates the *submitted* ids and turns every
  missing or errored result into a visible error (see **Ch 7 — Orchestration &
  State**).
- A sweep reported a `custom_id` collision the id-construction scheme makes
  impossible.

The lesson is exact and a little recursive: **an automated sweep is a lead, not a
verdict.** You do not trust it until you have grounded it in the source — which is
precisely what Spec Critic demands of its own verifier. The audit of the trust
tool was conducted by the trust tool's own philosophy. That an automated reviewer
can be confidently, specifically wrong is not a footnote in this book; it is the
thesis, applied to the audit itself.

This is the right place to anchor the rest of the chapter: nothing below is a raw
sweep result. Each finding survived a human re-read, and the false alarms were
discarded by name.

## The good news: the defenses that held

Before the open items, the reassurance — because it is the larger part of the
picture and it is *earned*. The auditor's own word for the core was "reassuring":
the data plane is honest and the joins are sound. The table below collects the
defenses that were read directly and confirmed correct — the load-bearing
guarantees that, if broken, would make the whole edifice untrustworthy. They are
not broken.

| Defense that held | What it guarantees | Source (audit · file) |
|---|---|---|
| **Grounding gate / URL matching** | A fabricated URL cannot match a searched one — normalization is conservative and the match is exact, so `CONFIRMED`/`CORRECTED` can't be grounded on an invented citation. | Trust · `source_grounding.py`, `verifier._apply_source_grounding`, `_enforce_grounding_invariant` |
| **Independent status re-check** | `classify_status` re-derives the verdict's trust label from the finding's own fields, independent of the verifier — defense-in-depth, not a single gate. | Trust · `report_status.py` |
| **Dedup won't merge distinct edits** | The dedup key includes full-text **SHA-256 digests** of `existingText`/`replacementText`; two findings merge only when their edit text is byte-identical. The dangerous direction (wrong-text merge) is closed. | Trust · `pipeline._dedup_key` |
| **Verdict can't bind to the wrong finding** | Dedup runs *before* verification; verdicts write back by a stable index into a non-reordered list, with `original_custom_id` preserved across retry/continuation waves. | Structural · `pipeline.py`, `verifier.py` |
| **Batch results reconciled, not dropped** | `collect_review_batch_results` iterates the *submitted* ids; any missing/incomplete/errored result becomes a visible error plus a `truncated_specs` entry, and a repair batch retries failures first. | Structural · `pipeline.py` |
| **`custom_id` collisions impossible** | Review ids carry a disambiguating trailing index; verification ids are independently namespaced. | Structural · `batch.py` |
| **Verification-stage failures *are* surfaced** | A per-finding `verification_failed=True` becomes a `VERIFICATION_FAILED` status with a dedicated red banner row. (Contrast the review-stage gap below.) | Structural · `report_exporter.py` |
| **Caches are safe** | The extraction cache key is a robust `(path, size, mtime, head+tail SHA-256)` tuple returning deep copies; the on-disk verification cache writes atomically (temp file + `os.replace`) and tolerates corrupt JSON by returning zero entries. | Structural · `extraction_cache.py`, `verification_cache.py` |
| **GUI threading is sound** | Workers run off-thread; every widget update marshals through `app.after(0, …)` behind an epoch staleness guard; concurrent runs are blocked. No cross-run result bleed. | Structural · `review_run_controller.py` |
| **Truncated review JSON is salvaged** | The fallback parser's backward bracket search recovers findings from incomplete JSON rather than discarding the whole response. | Structural · `reviewer.py` |

Read that table as a whole and the shape of the system's trustworthiness comes
into focus. The places where a compliance tool would most catastrophically betray
a user — inventing a source, applying the wrong edit, attaching a verdict to the
wrong finding, losing findings on a batch hiccup, serving a stale extraction —
are exactly the places that were read most carefully and found most solid. The
grounding gate alone is checked in three independent places (see **Ch 10 —
Verification II** and **Ch 11 — The Trust Model & Report Output**). This is what
defense-in-depth looks like when it works.

## The headline gap: a failed run can look like a clean one

Now the candor. The single most important thing still being perfected is the
**Structural Audit's P0-1**, and it is the finding that matters most for a
compliance tool specifically.

When a review batch comes back, some specs can fail — truncate, hit a parse
error, error out, or simply return nothing. The data layer handles this
*honestly*: every such spec is recorded in `errors`, added to a `truncated_specs`
list, and the combined result's `error` field is set to "N spec(s) had errors."
Nothing is lost — the audit confirmed it directly, and so did the re-read for this
chapter; the information is all there in `CollectedBatchState`.

The problem is what happens *after* the data layer. The exported report's header
prints `Files Reviewed: {len(files_reviewed)}`, and `files_reviewed` is **all
submitted specs**, including the failed ones. So the report reads "Files Reviewed:
5" even when two of those five produced no review at all. The Run Diagnostics
banner has a row for verification failures, and a row for extraction warnings —
but **no row for specs that failed review.** And the GUI's completion handler,
seeing the export succeed, sets a green checkmark and finalizes diagnostics as
"success" regardless of whether two specs silently failed.

```
   review batch returns
        │
        ▼
  ┌───────────────────────────────┐
  │ collect_review_batch_results  │   data layer: HONEST
  │   failed spec → errors[]       │   ✓ recorded
  │             → truncated_specs[]│   ✓ not lost
  │   combined.error = "N had …"   │   ✓ flagged
  └───────────────┬───────────────┘
                  │   truncated_specs carried through
                  │   CollectedBatchState …
                  ▼
  ┌───────────────────────────────┐
  │ report_exporter / GUI          │   surface: SILENT
  │   "Files Reviewed: 5"          │   ✗ counts failed specs as reviewed
  │   no "failed review" banner row│   ✗ zero refs to truncated_specs
  │   green ✓ + "success"          │   ✗ terminal state hides partial failure
  └───────────────────────────────┘
```

The consequence is precise and serious. A spec that *failed* review shows **zero
findings** — and a spec that was reviewed cleanly also shows zero findings. In the
exported `.docx` and in the UI's final state, **the two are indistinguishable.**
The only signal that two of five specs were never actually reviewed is a transient
log line a reviewer can easily miss. For a tool whose entire value proposition is
"we reviewed your specs," the difference between *"we reviewed all five and
they're clean"* and *"two of five silently failed"* is the difference between
trustworthy and dangerous.

Three things must be said clearly about this finding. First, **it is a surfacing
gap, not a data-loss bug.** The honest data already exists in `truncated_specs`;
nothing is corrupted or dropped. The fix is to *render* what the pipeline already
knows. Second, the proposed remedy is small and isolated — add a "Specs that
failed review" row to the Run Diagnostics banner (fed by `truncated_specs`, red
when greater than zero), correct the header to read "{reviewed}/{submitted}," and
make the UI's terminal state reflect partial failure instead of a bare green
checkmark. The owning chapters are **Ch 11 — The Trust Model & Report Output**
(the report and banner) and **Ch 13 — The Desktop GUI** (the completion handler),
with the source data owned by **Ch 7 — Orchestration & State**. Third — and this
is the uncomfortable part — **as of this writing the gap is still open.**
`report_exporter.py` contains zero references to `truncated_specs`; the banner's
"Spec content extraction warnings" row is a *different* mechanism entirely (it
counts drawing/picture/OLE-heavy specs, not failed reviews). The audit named the
single highest-payoff fix in the program, and the fix has not yet landed. That is
exactly why it sits at the top of the backlog below.

## The rest of the backlog

The remaining open items are real but lower-impact, and they group cleanly. The
table below is the consolidated, prioritized agenda; the prose after it adds the
"why it matters" that a one-liner can't carry. Every item is attributed to its
audit and priority and pointed at the chapter that owns the eventual fix.

| Open item | Audit · priority | Owning chapter | Status (one line) |
|---|---|---|---|
| Partial-failure not surfaced in report/UI | Structural P0-1 | Ch 11, Ch 13 | **Headline.** Data is honest; surfacing missing. Still open. |
| Multi-file finding emits only one edit instruction | Trust P0-1 | Ch 7, Ch 11 | Sidecar under-emits; `group_findings()` is wired only into tests. |
| Per-file anchor/`evidenceElementId` collapse on merge | Trust P0-2 | Ch 7, Ch 11 | Subset of above; anchored/`ADD` edits may mislocate in non-representative files. |
| Cross-check findings lack `finding_id`, never deduped | Structural P1-1 | Ch 7, Ch 8 | Coordination edits reach the sidecar with empty ids. |
| Batch→real-time fallback: prove "exactly one terminal result" | Structural P1-2 | Ch 10 | Likely benign; unread end-to-end. Verify-first. |
| Model-capability whitelist goes stale | Trust P0-3 | Ch 12 | A newer model silently degrades to safe defaults. |
| 300k beta header: presence checked, not acceptance | Trust P0-4 | Ch 12 | Same risk class as the web-fetch-header incident. |
| Extraction completeness (headers/footers/text-boxes/footnotes) | Trust P0-6 | Ch 4 | May leave requirement text unreviewed. Verify-first. |
| Batch-wave grounding parity with real-time | Trust P0-5 | Ch 10 | Default path; "likely fine, must be proven." |
| `validate_edit_shape` permits a no-op `EDIT` | Trust P1-1 | Ch 5 | `existingText == replacementText` passes validation. |
| Minor / hardening (continuation off-by-one, doc-drift, recorder reset, …) | Both · P2 | Ch 4, 10, 14 | Bounded, low-urgency. |

### Sidecar under-emission for multi-file defects (Trust P0-1 / P0-2)

This is the trust audit's strongest finding, and it is the mirror image of the
structural headline: where the structural audit worried about *failed* output
looking clean, this worries about *correct* output reaching the world incomplete.

DSA master specs are templated, so the same defect frequently appears verbatim
across several spec sections. When dedup collapses those into one merged
`Finding`, the merged finding carries `affected_files = [a, b, c]` and a list of
per-file `occurrence_originals`. But the edit sidecar emits **one** entry, with
`fileName = a` only, and does not include `affected_files` in the entry at all. A
downstream applier reading that sidecar fixes file `a` and never learns that `b`
and `c` carry the identical defect. The only record that the issue spans three
files is the human-readable `issue` string ("found in 3 specs: …") — useless to a
machine.

The painful detail is that the machinery to do this correctly **already exists**.
`group_findings()` and `FindingOccurrence.executable_finding()` in `pipeline.py`
were built precisely to fan a merged finding back out into per-file executable
instructions, and `CLAUDE.md` explicitly states the `occurrence_originals` field
exists "so per-file differences survive the merge **for the report and the
edit-instruction sidecar**." But the intent is not realized: a grep confirms
`group_findings()` is called **only from tests**, never in production — a fact
that still holds against the current source. The fan-out helper is dead code
outside the test suite. P0-2 is the sharp edge of the same problem: because
`anchorText` and `evidenceElementId` are deliberately *not* part of the dedup key
(only the edit text is), a merged `ADD` action keeps the representative file's
anchor, which can mislocate the insertion in the other files. The correct
per-file anchors are sitting in `occurrence_originals`, unread. The fix — wire
`group_findings()` into the sidecar, or at minimum emit `affected_files` per
entry — lives in **Ch 7 — Orchestration & State** and **Ch 11 — The Trust Model
& Report Output**.

### Cross-check findings carry no stable id (Structural P1-1)

Review findings are deduplicated and stamped with a `compute_finding_id` at
review-collect time. Cross-check (coordination) findings are appended later, in
`finalize_batch_result`, **without** dedup or id assignment — so each one carries
`finding_id = ""`. They flow into the sidecar unchanged, and the sidecar faithfully
emits `"finding_id": ""`. A downstream applier that keys or dedupes edits by id
sees every coordination edit collide on the empty key, with no stable handle to
track a coordination edit across re-runs. The structural audit is careful to
*downgrade* a sub-agent's framing here: this is **not** a crash (the report
doesn't key by id), it is a sidecar/traceability quality gap. The fix is small and
contained — stamp `compute_finding_id` on cross-check findings, and consider
running them through the same dedup — and it lives across **Ch 7 — Orchestration &
State** and **Ch 8 — Cross-Spec Coordination**. (It also ties to the trust audit's
P1-3 concern that coordination duplicates spanning two CSI-division chunks are
never collapsed.) The re-read for this chapter confirms `compute_finding_id` is
still invoked only inside `_deduplicate_findings`; the item remains open.

### The fallback handoff that wasn't read end-to-end (Structural P1-2)

When a batch verification's unresolved tail shrinks below the real-time fallback
threshold, those last few findings flip from batch to synchronous verification.
The structural auditor was explicit and honest: *they did not read this path
end-to-end.* The worry is a tail finding being both submitted to an in-flight
wave *and* run real-time (last-writer-wins on `f.verification`), or conversely
being dropped by both. The auditor's judgment is "likely benign — an abandoned,
never-retrieved batch wave doesn't write back — but 'likely' isn't good enough for
the trust bar here." The remedy is a verify-first read plus a hermetic test
asserting every tail finding ends with **exactly one** terminal
`VerificationResult`. This is owned by **Ch 10 — Verification II**. Note the
discipline in how this is reported: an unverified path is named as unverified
rather than waved through — which is, again, the product's own philosophy turned
on its author.

### Configuration drift: a newer model, silently worse (Trust P0-3 / P0-4)

Two items here share a root cause — a hardcoded assumption about the outside world
that can quietly go stale.

The **model-capability whitelist** (`api_config._MODEL_CAPABILITIES`) enumerates
exactly three model ids: Opus 4.7, Sonnet 4.6, Haiku 4.5. Any id outside that set
falls through to `_DEFAULT_CAPABILITIES`, which disables every capability flag —
no adaptive thinking, no extended-output beta, a 200k context window, output
capped at 128k instead of 300k. This "safe default" is genuinely smart in one
direction: a misconfigured model id produces a *smaller* request, never an API
rejection (see **Ch 12 — Configuration, Models & Token Economics**). But it has a
perverse failure mode. An operator who deliberately points
`SPEC_CRITIC_REVIEW_MODEL` at a **newer, more capable** model — precisely to get
*better* reviews — instead gets a silently *degraded* one: no extended thinking,
a smaller output cap, no effort tuning, no error. The protection against API
rejection is bought with the currency of quiet quality loss. The whitelist still
covers only those three ids, so the gap is open; the audit's suggestion is to keep
the whitelist current and, at minimum, *warn loudly* on an unknown id rather than
degrade in silence.

The **300k extended-output beta header** (`BATCH_OUTPUT_BETA =
"output-300k-2026-03-24"`) is the same risk class, and the audit names the
precedent directly. This codebase was once bitten by a retired
`web-fetch-2026-02-09` beta header that caused HTTP 400 and crashed every run on
the common path — a story told in full in **Ch 10** and **Ch 17 — Evolution &
Lessons**. The 300k header is hardcoded the same way, and `assert_extended_output_allowed`
checks only that the header is *present*, not that the API still *accepts* it. If
that beta value is ever retired or renamed, every large-input (≥200k-token) batch
review crashes at submit — the exact failure mode of the prior incident, on a
less-common path. There is a genuinely encouraging coda here, though: the team
*did* internalize the web-fetch lesson. The current `api_config.py` attaches no
beta header for `web_fetch` at all and documents, at length, *why* an unrecognized
beta value is rejected rather than ignored. The lesson was learned for one header;
the structurally-identical check on the 300k header is simply the next place to
apply it. Both items are owned by **Ch 12**.

### Extraction completeness: is any spec text never reviewed? (Trust P0-6)

This is a trust gap in the "don't miss real problems" direction, and it is
verify-first. `extract_text_from_docx` walks the document body for paragraphs and
tables. python-docx body iteration typically misses **headers and footers, text
boxes** (`w:txbxContent` inside a drawing), **footnotes/endnotes**, and
grouped-shape text — and DSA specs sometimes park requirements or revision notes
in exactly those places. The content-loss warning (see **Ch 4 — Input**) covers
drawing/picture/OLE-heavy specs but not text-bearing parts outside the body. If a
requirement lives in an unextracted part, the model never sees it, and a real
defect there is silently un-flagged. The proposed first step is empirical: build
a fixture `.docx` with text in a header, a text box, and a footnote, and confirm
whether each is captured — then decide whether to extract them or at least extend
the content-loss warning to flag their presence. Owned by **Ch 4**.

### Batch grounding parity — likely fine, must be proven (Trust P0-5)

This is the most important item to *verify* rather than to *fix*, because it sits
on the default, highest-volume path. The real-time grounding gate was read and
confirmed solid. A sub-agent asserted the batch wave "mirrors" it — but the
auditor did not read the batch path personally, and **batch is the default route**
for both review and verification. If `_classify_wave_results` ever stamped
`grounded`/verdicts without running the identical `_apply_source_grounding` +
`_enforce_grounding_invariant` partition, a `CONFIRMED` could reach the report
ungrounded on the common path — which would undermine the single most important
guarantee in the table of defenses above. The auditor's own assessment is "likely
fine, but must be proven given trust requirements," and the recommended proof is a
hermetic test that feeds a batch `CONFIRMED` with an *ungrounded* citation and
asserts it is downgraded to `UNVERIFIED`, identical to the existing real-time
test. Owned by **Ch 10**.

### Minor and hardening items

The long tail is genuinely minor and is recorded for completeness, not alarm: a
`validate_edit_shape` that permits a no-op `EDIT` where `existingText ==
replacementText` (Trust P1-1, **Ch 5**); ASCE 7 editions older than 2005 falling
outside the deterministic detector's "plausible" set, which the LLM review likely
still catches (Trust P2-1, **Ch 4**); a continuation-cap off-by-one that is bounded
and lossless (Structural P2-1, **Ch 10**); a `finding_id` truncated to 48 bits,
with negligible collision risk at the typical scale (Structural P2-2); a
documentation drift where `CLAUDE.md` calls cross-check "parallel with
verification" while the batch flow runs sequentially — and sequential is the
*safer* arrangement, so the doc is what needs correcting (Structural P2-3); and a
`TraceRecorder` singleton reset that lags completion just long enough for a second
run to catch a first run's late trace events — *tracing only, never findings or the
report* (Structural P2-4, **Ch 14 — Observability**). One item is a separate
workstream, not a code fix: the *content* of the system prompts and the pinned
edition strings in `code_cycles.py` need a mechanical/plumbing **domain expert**,
not a code-logic reviewer (Trust P1-4, **Ch 5** and **Ch 12**).

## Where the risk really lives

Step back from the individual items and the audits tell one coherent story, and
it is not the one most people expect. The instinct, walking into a tool built on a
large language model, is that the model is the soft spot — that the thing to fear
is the AI "hallucinating" a building code. The audits found the opposite. The
verification and grounding engine — the AI-facing core — is the most defended,
most carefully re-read, most demonstrably correct part of the system. The core
does what it claims.

The real risk lives at the **edges**: the honesty of the artifact (does a failed
run look failed?) and the completeness of the emitted edits (does a multi-file fix
reach every file?). These are not glamorous AI problems — they are classic
software-engineering seam problems, the kind that hide in the join between a
correct producer and a correct consumer. That inversion is the most useful single
takeaway a new engineer can carry out of this chapter: **trust the verifier, audit
the plumbing.** Spend your review attention on the surfacing layer and the
edit-emission pipeline, not on second-guessing whether the grounding gate works —
it does.

## The caveat that outranks every bug

There is one statement in the trust audit that matters more than any P0, P1, or
P2 — more, arguably, than anything else in this entire handbook. It is not a bug.
It is a permanent property of what automated verification can and cannot prove,
and every user of this tool must understand it:

> A `VERIFIED_SUPPORTED` / `CONFIRMED` verdict only guarantees that the cited URL
> was **actually retrieved by the search tool** — *not* that the page's content
> demonstrably supports the specific code claim.

The grounding invariant proves the source is **real**. It does **not** prove the
source **proves the claim**. The verifier can cite a genuine, retrieved page that
does not, in fact, contain the provision it was cited for — and the grounding gate
will pass it, because the gate's job is to reject *invented* URLs, not to
re-adjudicate whether a real page substantiates a specific sentence. A green
checkmark in the report means "the evidence behind this finding is real and
checkable," not "this finding is proven true." Those are different claims, and the
gap between them is exactly where a careful human reviewer earns their keep.

The practical instruction follows directly and should be repeated wherever this
tool is handed to a new user: **human spot-checking of `VERIFIED_*` findings
remains warranted.** The tool's value is that it does the expensive work of
finding candidate defects, gathering real sources, and rendering its own
uncertainty honestly across nine trust levels. Its value is *not* that it removes
the human from the loop. The nine-label trust model (see **Ch 11**) exists
precisely so that a reviewer can spend their scarce attention where it matters —
on the contested, the insufficient, and yes, the spot-check of the confirmed —
rather than reading every finding from scratch. Grounding makes uncertainty
*visible*; it does not make it *vanish*.

## An agenda, not a panic

It would be easy to read a list of P0s and conclude the program is fragile. That
would be the wrong conclusion, and the audits' own framing guards against it. What
the audits produced is a **prioritized backlog with a clear payoff ordering**, not
a catalog of crises. The sequencing the auditors recommend is itself a lesson in
trust-tool engineering:

1. **Surface partial failure first** (Structural P0-1). Highest trust payoff,
   smallest blast radius — the data already exists, only the rendering is missing.
   A user who can see that two of five specs failed can compensate; a user who
   can't is the one truly at risk.
2. **Fix the edit-emission completeness** (Trust P0-1 / P0-2). The next most
   likely to need real code, and the place where correct verdicts are currently
   under-delivered to the applier.
3. **Run the cheap configuration checks** (Trust P0-3 / P0-4). Quick policy
   changes with outsized payoff — keep the whitelist current, make the beta-header
   check fail gracefully.
4. **Prove the verify-first items** (Trust P0-5 / P0-6, Structural P1-2). Confirm
   whether a gap even exists — batch grounding parity, extraction completeness, the
   fallback handoff — before writing any code.

Crucially, these workstreams touch disjoint modules and can be dispatched in
parallel. The road from here to a closed backlog — and the larger story of how the
program arrived at this shape, the v3.0.0 pivot that removed the surgical-edit
machinery and made Spec Critic a tool that *emits but never applies* — is the
subject of **Ch 17 — Evolution & Lessons**.

The honest summary: the core you would expect to be fragile is the part you can
lean on, and the edges you would expect to be trivial are where the real work
remains. The audits exist, they are kept in the repo with their own false alarms
intact, and they point at fixable, well-scoped problems. A tool that can say all
of that about itself — out loud, in writing, to the engineer who inherits it — has
already done the hardest part of earning trust.

## Key takeaways

- **Two complementary audits.** The Trust Audit checked the **leaves** (grounding,
  edit proposals, detectors, status). The Structural Audit checked the **spine**
  (orchestration, joins, state, error handling, artifact honesty). Both used three
  parallel sweeps plus a careful personal re-read.
- **The method is part of the message.** Several sub-agent "CRITICAL" claims were
  false alarms, filtered by re-reading the source — the product's own grounding
  philosophy, applied to its own audit. An automated sweep is a lead, not a verdict.
- **The core is the strong part.** Grounding can't be faked, dedup won't merge
  distinct edits, verdicts can't bind to the wrong finding, batch results are
  reconciled against the submitted set. The defenses that matter most held.
- **The risk lives at the edges.** The headline gap (Structural P0-1) is that a
  *partially failed* run isn't made distinguishable from a *clean* one — a
  **surfacing** gap, not data loss; the data exists in `truncated_specs`, unread by
  the report. The runner-up (Trust P0-1) is that multi-file findings under-emit
  edit instructions because `group_findings()` is wired only into tests.
- **The caveat that outranks every bug.** A `VERIFIED_SUPPORTED` verdict proves the
  cited source is **real**, not that it **proves the claim**. Human spot-checking of
  `VERIFIED_*` findings remains warranted — grounding makes uncertainty visible, not
  absent.
- **A backlog, not a panic.** Surface partial failure first; then close
  edit-emission completeness; then the cheap config checks; then prove the
  verify-first items. The fixes are small, scoped, and owned by identified chapters.
  See **Ch 17 — Evolution & Lessons** for where the road leads.
