# The Real-Time Review Transport

[**Ch 6 — Batch Processing**](06_batch_processing.md) makes a specific trade:
latency for cost and output headroom. The Message Batches API halves the price
and, for large inputs, lifts the output ceiling to 300k — and in exchange a run
takes as long as the batch takes, which can be hours.

That is the right default and remains the default. But it is not right for
everyone all the time. A reviewer checking three sections before a deadline does
not want to submit a batch and wait; they want to watch findings arrive. This
chapter is about the alternative transport that serves them, and about the
engineering rule that made it safe to add: **anti-drift by construction.**

The risk in a second transport is not that it fails. It is that it slowly
diverges — that batch and real-time build subtly different requests, parse
responses through different code, and produce different findings for the same
spec. A tool whose answer depends on which button you pressed has lost the trust
argument of [**Ch 1 — The Problem Domain**](01_problem_domain.md) before it
starts. So the design goal was not "add a streaming path." It was "add a
streaming path that *cannot* drift."

## 1. The operator's view

The GUI Options row carries a persisted "Real-time review (streaming)" toggle
(`core/ui_state.load_review_transport` / `save_review_transport`; an unknown
stored value degrades to `"batch"`). Alongside it sits a worker-count selector.

The copy has to be cost-honest, because the trade is real and runs the other way
from most "faster" toggles: **real-time bills at standard API pricing — there is
no 50% batch discount.** The Options hint, the About/usage dialog, and the run log
all say so. Faster and more expensive, stated plainly.

## 2. Where it plugs in

`start_batch_review(review_transport="realtime")` runs the per-spec reviews **to
completion inside the submit step**, via `review/realtime_review.run_realtime_review`
— a `ThreadPoolExecutor` of streaming Messages calls, one per spec.

That is the surprising part of the design and the source of most of its economy:
real-time does not add a parallel pipeline. It collapses submit-and-collect into
the submit step, then hands the *finished* results to the same collection tail
the batch path uses.

The submission carries a `{custom_id: ReviewResult}` map in
`BatchSubmission.realtime_results` (in-memory only), plus a local `BatchJob` stub
whose `batch_id` is the `REALTIME_JOB_SENTINEL` (`"realtime"`).

**Branch on `BatchSubmission.review_transport`, never on the sentinel id.** The
sentinel exists so a `BatchJob` can be constructed at all; it is not the
transport signal. Code that sniffs the id will break the moment a second local
transport appears, and — worse — will silently misclassify if the id ever changes.

## 3. The anti-drift contract

Three shared seams do the work:

| Concern | Shared mechanism |
|---|---|
| Request construction | The **same** `build_review_request` |
| Response classification | The **same** `reviewer.review_result_from_message` |
| Id minting | The **same** `_review_custom_id` |

The prompt-cache prefix is therefore byte-identical across transports. The only
parameter deltas are that `service_tier` is omitted and extended output is pinned
off (see §5).

The second row is the one that required actual refactoring rather than
discipline. `review_result_from_message` was **extracted from the batch retrieval
path**, which now delegates to it. That is the difference between "both paths call
the same function" and "both paths were written to behave the same" — only the
first is a guarantee.

`collect_review_batch_results` then short-circuits retrieval and the second-batch
repair, and **reuses the entire shared tail**: bucketing into `truncated_specs`,
anchor validation, dedup and `rf-` stamping. So the failed-review surfacing that
[**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md)
identified as a trust gap works identically on both transports, with no
transport-specific code to keep in sync.

Truncation parity comes from one inline instructed repair per spec, using
`RETRY_TRUNCATED_REVIEW_INSTRUCTION` from `review_request_builder` — **shared
verbatim** with the batch repair pass.

## 4. Worker discipline

Workers obey two rules that come from the research fan-out's threading model
(see [**Ch 19 — Location-Aware Review**](19_location_aware_review.md)):

- **Workers never raise.** A terminal failure becomes an error `ReviewResult`
  that lands in `truncated_specs` — the same bucket a truncated batch result
  lands in, so it reaches the same surfacing.
- **Workers never touch `log` or `diagnostics`.** Telemetry rides back to the
  coordinator on the outcome object. A `ThreadPoolExecutor` worker writing to
  shared mutable state is a race waiting to be debugged at 2am, and the rule
  removes the category.

Concurrency defaults to 4 (`SPEC_CRITIC_REALTIME_REVIEW_WORKERS`, clamped 1–8).
The GUI persists its own choice of exactly 2/4/6/8 in
`ui_state.realtime_review_workers`, defaulting to 4. For backward compatibility a
missing GUI key is seeded from the environment variable when that value happens
to be one of the four GUI choices; once saved, the explicit GUI preference wins.
`review_run_controller.start_review` snapshots the value *before* the background
thread starts and passes it explicitly, so a mid-run settings change cannot alter
a running review. Headless callers that omit it keep the environment path and the
wider 1–8 range.

Actual concurrency is `min(configured_workers, review_job_count)` — four workers
for two specs is two workers.

The UI copy here also has to be honest, and in a more subtle way than the pricing
line: more workers usually finish sooner and spend budget faster, and higher
rate-limit and retry pressure can *add* cost. What does not change is the
successful planned request set. More workers is a pacing choice, not a scope
choice.

## 5. The oversize gate

Any spec whose local token estimate is at or above `LARGE_REVIEW_INPUT_THRESHOLD`
(200,000 — the point at which the batch path lifts output to 300k) is **refused
before any spend**, with an actionable "run in batch mode" `ValueError`.

The reasoning chain is worth following because it is a capability fact, not a
policy preference. The `output-300k-2026-03-24` beta is **batch-only by API
design**. Real-time therefore hard-caps at the 128k phase baseline. A spec large
enough to need the extended output would review on the real-time path and produce
a truncated result — having been paid for at full price.

So the gate mirrors `_resolve_extended_output`'s condition exactly, including its
model whitelist: it only refuses on beta-whitelisted models. A model that could
not lift its output on the batch path either is **not** blocked, because for that
model real-time is not giving anything up.

Refusing before spend rather than truncating after it is the same instinct as the
digest cost-confirm in [**Ch 20 — Drawings**](20_drawings.md): when the program
can know in advance that an operation will disappoint, it should say so before
taking the money.

## 6. Verification transport is coupled to review transport

A real-time review that then waits on batch verification would deliver nothing
the operator wanted. So verification follows the run's review transport, through
`pipeline.verify_findings_for_run` — the single entry point both drivers (the GUI
collect step and `run_batch_collection_headless`) call, for round 1 **and** round
2.

```python
def verify_findings_for_run(
    findings, *, module=DEFAULT_MODULE, transport="batch",
    log=..., progress=..., cache=None,
    user_location=None, jurisdiction_fingerprint=None,
    api_call_semaphore=None,
) -> None:
```

- The **batch** arm delegates to the existing wave pair of [**Ch 10 —
  Verification II**](10_verification_grounding.md).
- The **realtime** arm is `prepare_findings_for_verification` plus a ≤5-worker
  pool over `verify_finding`.

The realtime arm is not new machinery. It is the batch collector's *real-time
fallback tail* — the path that already existed for handling a small unresolved
remainder — promoted to a full pass. Reusing a proven path rather than writing a
second verifier is the same anti-drift instinct as §3.

Worker exceptions stamp `VERIFICATION_FAILED`, and the exactly-once terminal
result invariant is preserved. The net effect: **a real-time run performs zero
batch polling end to end.**

## 7. No resume story, and four places that enforce it

Real-time has no resume, and this is a deliberate scope decision rather than an
omission. A batch is a remote object with an id that survives a client crash; a
set of in-flight streaming calls is not.

What matters is that the absence is enforced rather than merely documented, in
four places:

1. Realtime runs never write pending-batch state.
2. `PendingBatch.from_submission` **refuses** a realtime submission — the backstop
   if some future call site forgets rule 1.
3. The GUI collect step gates `clear_pending_batch()` on the batch transport.
4. The startup resume prompt, the manual **Recover batch…** action, and
   `scripts/recover_batch.py` all stay batch-only.

Rule 3 is the non-obvious one and it protects against a genuinely nasty bug: if a
realtime run cleared pending state unconditionally, a *successful realtime run*
would delete the resumable state of an **earlier detached batch** — destroying
work the operator had already paid for and could otherwise have recovered.

## 8. Diagnostics

The runner records one `record_api_call(mode="realtime", phase="review")` row per
API call it makes. The GUI's aggregate `batch_collect` row stays batch-only, as a
double-count guard. `DiagnosticsReport.mode` and the trace's `run.json` mode both
carry the transport, so a run's transport is recoverable from its forensic record
— see [**Ch 14 — Observability**](14_observability.md).

## 9. Routed programs share one pool

A multi-module program does not multiply the worker choice per module. Every
`(module, spec)` pair becomes a heterogeneous `RealtimeReviewJob`, and **all jobs
share one global pool** governed by `SPEC_CRITIC_REALTIME_REVIEW_WORKERS`.

Each job retains its own cycle, context, alerts, and trace parent, and uses
`(module_id, custom_id)` as its opaque program key — necessary because child
`custom_id` values are only module-local and would collide across modules.

All initial requests, all repair requests, and every oversize gate are built
**before** `_get_client`, which preserves the no-spend preflight barrier: the run
either passes every gate or fails having billed nothing.

The surrounding concurrency model for routed programs — concurrent module
preparation, the shared research permit budget, concurrent collection with
per-module dependency chains preserved, and the global synchronous-call semaphore
— is described in [**Ch 18 — Modules & Programs**](18_modules_and_programs.md)
and pinned by `tests/test_program_pipeline.py`.

## 10. Pins

`tests/test_realtime_review.py` covers the runner's parse paths and request pins,
truncation and repair parity through collect→finalize, the retry taxonomy, the
concurrency cap, the oversize gate, transport plumbing,
`verify_findings_for_run`'s exactly-once behavior, a headless end-to-end run, the
no-pending-state guarantee, the `ui_state` round-trip, and diagnostics telemetry.

The parity assertions are the important ones. A test that only checked "real-time
produces findings" would pass indefinitely while the two transports drifted apart.
