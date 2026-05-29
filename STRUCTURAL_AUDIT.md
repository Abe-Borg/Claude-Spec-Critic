# Spec Critic — End-to-End Structural Correctness Audit (investigation plan)

## Context

This is the **second** audit. The first (`TRUST_AUDIT.md`) asked "are the trust-critical *leaf*
functions correct?" (grounding, edit proposals, detectors, status classification). This one asks a
different question the user raised: is the program's **spine** — orchestration, data joins, state,
error handling, and the honesty of the final artifact — sound enough that even perfectly-correct leaf
output can't be silently dropped, misattributed, or presented as something it isn't? "Start to finish,
I need to trust this program."

Method mirrors the first audit: three parallel code sweeps, then I **personally re-read** the spine
(batch collect/repair, the finding↔verification join, the GUI completion path, the report/sidecar
inputs) to separate real issues from noise. As before, that mattered — **three sub-agent "CRITICAL"/
"MEDIUM" claims were false alarms** (see §Verified-clean).

**Overall posture (reassuring):** the data plane is honest and the joins are sound. Dedup runs *before*
verification, verdicts attach by a stable index into a non-reordered list (a verdict cannot bind to the
wrong finding), batch results are reconciled against the *submitted* set (nothing silently dropped),
caches are correctly keyed and the on-disk one is written atomically, and GUI threading uses a clean
epoch guard. **The single systemic weakness is at the edges: the program does not make a *partially
failed* run obviously distinguishable from a *fully clean* one in its final deliverable.** That is the
one place the program can mislead, and it's exactly what the user must be able to trust.

---

## P0 — A partially-failed review can look like a complete, clean one

### P0-1 — Review-stage failures are not surfaced in the exported report or the UI terminal state
> **RESOLVED** (branch `claude/kind-volta-FE6gt`). `finalize_batch_result` now carries
> `truncated_specs` onto the new `PipelineResult.failed_review_specs`. The exported report surfaces
> it three ways (a red "Specs that failed review (not reviewed)" banner row + naming recovery hint,
> a corrected "Files Reviewed: {reviewed} of {submitted}" title line, and a red bullet annotation),
> and `on_review_complete` routes a partial failure to the amber `set_complete_with_errors()` button
> state + a `warning`-level diagnostics finalize (never bare `success`). This also resolves **P1-3**
> (the review-repair miss path lands in `truncated_specs`, which now flows to the banner row).
> Covered by `tests/test_failed_review_surfacing.py` (data plane + report) and
> `tests/test_review_complete_terminal_state.py` (GUI terminal state, tkinter-gated). See the
> "Review-stage failure surfacing" invariant in `CLAUDE.md`.
- **Where:**
  - `src/orchestration/pipeline.py:907-958` (`collect_review_batch_results`) — failures are *correctly*
    recorded: a missing/incomplete/parse-error/errored result becomes an entry in `errors` **and**
    `truncated_specs`, and `combined.error = "N spec(s) had errors: …"` (data layer is honest).
  - `src/output/report_exporter.py:316` — the report header prints `Files Reviewed: {len(files_reviewed)}`,
    and `files_reviewed` = **all submitted specs** (`pipeline.py:778`), including the ones that failed.
    So it reads "Files Reviewed: 5" even when 2 produced no review at all.
  - `truncated_specs` is plumbed through `CollectedBatchState`/diagnostics (`pipeline.py:966,1107`) and
    logged per-spec in the GUI (`batch_controller.py:193-197`) — **but `report_exporter.py` has zero
    references to it** (confirmed by grep). The Run Diagnostics banner has a `verification_failed` row
    but **no "specs that failed review" row**.
  - `src/gui/review_run_controller.py:120-158` (`on_review_complete`) — logs a warning when `rv.error`
    is set, but then calls `set_complete()` (green ✓) and finalizes diagnostics as **"success"**
    regardless of review errors (the only branch is on *export* status, not review status).
- **Why it matters:** A spec that *failed* review (0 findings because it truncated / parse-errored /
  returned nothing) is **indistinguishable from a clean spec** (0 findings) in the exported `.docx` and
  in the UI's terminal state. The only signal is a transient log line a reviewer can easily miss. For a
  compliance-review tool, "we reviewed all 5 specs and they're clean" vs "2 of 5 silently failed" is the
  difference between trustworthy and dangerous. This is the headline finding of this audit.
- **Dig into:** Add a "Specs that failed review (not reviewed)" row to the Run Diagnostics banner fed by
  `truncated_specs` (highlight red when > 0), and either correct the "Files Reviewed" count to
  "{reviewed}/{submitted}" or list the failed specs explicitly. Make the UI terminal state reflect
  partial failure (e.g., a distinct "Completed with errors" state) instead of a green checkmark + "success".
  The data already exists — this is purely a surfacing fix, but a high-value one.

---

## P1 — Real correctness / traceability gaps

### P1-1 — Cross-check (coordination) findings have no `finding_id` and are never deduplicated
> **RESOLVED** (branch `claude/gracious-franklin-027AS`). `compute_finding_id` now takes a
> `prefix=` (default `"rf"`), and `pipeline.assign_cross_check_finding_ids` stamps every
> coordination finding with a stable, content-derived `cf-` id inside `run_cross_check_for_batch`
> — *before* the findings enter cross-check verification and the edit sidecar. This closes the
> empty-`finding_id` collision in the sidecar and gives cross-check verification spans a real
> per-finding handle in the trace viewer (they previously correlated as `unknown`). The `cf-`/`rf-`
> prefix split guarantees a coordination finding and a review finding that share a dedup key never
> collapse into one sidecar entry. On the **dedup** half: true cross-division collapse is a no-op in
> practice because `_label_finding_with_chunk` prefixes each finding's `section` with its CSI-division
> label (distinct sections → distinct keys), and content-addressed ids already give a downstream
> applier the dedup signal for any genuine same-content pair — so collapse was intentionally left out
> to avoid changing report finding counts. Covered by `tests/test_cross_check_finding_ids.py` (unit
> stamping + end-to-end through `run_cross_check_for_batch` into the sidecar). See the "Finding-id
> namespacing" invariant in `CLAUDE.md`.
- **Where:** `compute_finding_id` is called **only** inside `_deduplicate_findings`
  (`pipeline.py:355,372,387`). Review findings are deduped + id-stamped at review-collect
  (`pipeline.py:953`). Cross-check findings are appended later in `finalize_batch_result`
  (`pipeline.py:1091-1092`) **without** dedup or id assignment, so each carries `finding_id=""`. They
  flow into the sidecar (`edit_sidecar.py:74-75`), which emits `"finding_id": … or ""` (`:53`).
- **Why it matters:** Every coordination edit in the machine-readable sidecar has an **empty id**. A
  downstream applier that keys, dedupes, or cross-references edits by `finding_id` sees all coordination
  edits sharing the empty key (collision/overwrite), and there's no stable handle to track a coordination
  edit across re-runs. Separately, cross-check duplicates across CSI-division chunks are never collapsed
  (ties to `TRUST_AUDIT.md` P1-3). The report itself doesn't key by id, so there's **no crash** — this is
  a sidecar/traceability quality gap, not a render failure (correcting the sub-agent's "MEDIUM-HIGH crash"
  framing).
- **Dig into:** Stamp `compute_finding_id` on cross-check findings (and consider running them through the
  same dedup) so the sidecar emits stable, unique ids. Small, contained change.

### P1-2 — Batch→real-time fallback handoff: confirm no finding is double-processed or dropped
- **Where:** `src/verification/verifier.py` last-wave fallback (~:2902-2944) and the wave-submit just
  before it. On the final wave the unresolved tail flips to real-time (`verify_finding` per finding,
  results assigned in-place).
- **Why it matters:** I did **not** read this path end-to-end, and the batch agent was explicitly unsure
  whether a tail finding could be both submitted to an in-flight wave *and* run real-time (last-writer-wins
  on `f.verification`) or, conversely, dropped by both. It's the tail of the **default** verification path,
  so it deserves a definitive read. Likely benign (an abandoned, never-retrieved batch wave doesn't write
  back), but "likely" isn't good enough for the trust bar here.
- **Dig into:** Trace the exact submit-vs-fallback ordering; add a hermetic test that a tail finding
  receives exactly one terminal `VerificationResult`.

### P1-3 — Confirm the review *repair* batch's misses feed the P0-1 surfacing
> **RESOLVED via P0-1.** A spec that fails both the original and the repair batch stays in
> `truncated_specs`, which now flows through `PipelineResult.failed_review_specs` into the banner
> row / title-line / bullet annotation. No separate change was needed.
- **Where:** `_recover_retryable_review_batch_results` (`pipeline.py:808-892`). On repair-poll detach it
  logs "{N} item(s) will appear as failed in the report" and returns the originals (which stay in
  `truncated_specs`).
- **Why it matters:** The repair layer is good resilience, but its failure path depends entirely on P0-1's
  surfacing actually existing. Verify a spec that fails both the original and repair batch is visibly
  flagged in the final artifact (today it lands in `truncated_specs`, which the report ignores — see P0-1).
- **Dig into:** Covered by fixing P0-1; just confirm the repair-miss path routes through the same banner row.

---

## P2 — Minor / hardening / doc drift

- **P2-1 — Continuation cap off-by-one.** `verifier.py:2844` uses `if continuation_counts[key] > cap`
  (not `>=`), so a finding can consume `cap + 1` waves before termination. Bounded by `max_waves`, no data
  loss. Low.
- **P2-2 — `finding_id` truncated to 12 hex (48 bits).** `pipeline.py:346`. Collision negligible at the
  typical <100 findings/run scale. Noted, not actionable.
- **P2-3 — Stale doc: "parallel" cross-check vs verification.** `CLAUDE.md` (high-level flow) says
  cross-check runs "parallel with verification by default," but the batch flow read as **sequential**
  (review → verify → cross-check → verify-cross-check, `batch_controller.py`). Sequential is *safer* (no
  shared-`Finding` race), so this is reassuring — but confirm and fix the doc. **If** a parallel path does
  exist somewhere, re-check that the two passes don't mutate shared `Finding` objects concurrently.
- **P2-4 — TraceRecorder global singleton reset is delayed.** `recorder.py:548` global, reset in `reset_ui`
  ~2.5s after completion (`review_run_controller.py:179`). A user starting a second run inside that window
  could have run-1's late worker threads enqueue trace events into run-2's recorder. **Tracing/diagnostics
  only — never findings or the report.** Low; fix by nulling/stopping the recorder synchronously at
  completion rather than on the delayed UI reset.

---

## Verified-clean — DO NOT spend time here (checked against real source)

- **Review batch results are NOT silently dropped.** A sub-agent's "CRITICAL" claim was based on reading
  only `retrieve_review_results` and *assuming* the caller. The caller (`collect_review_batch_results`,
  `pipeline.py:916-946`) iterates the **submitted** ids and turns any missing/incomplete/errored result
  into a visible error + `truncated_specs`, and a **repair batch** retries failures first. (The honesty
  gap is *surfacing* them in the final artifact — P0-1 — not losing them.)
- **custom_id collisions can't happen.** Review ids are `review__{sanitized}__{idx}` (the trailing
  enumerate `idx` disambiguates identical sanitized names); verification ids are `verify__{idx}`. A
  sub-agent's "MEDIUM collision" was wrong. (`batch.py:86,333`)
- **A verdict cannot attach to the wrong finding.** Dedup runs *before* verification (`pipeline.py:953`);
  verification writes back by stable `enumerate` index into a non-reordered list
  (`findings[outcome.finding_idx].verification = …`) with `original_custom_id` preserved across retry/
  continuation waves (`verifier.py:2532,2639,2649,2763`). Currently sound (note: it relies on the list not
  being reordered between submit and collect — a robustness constraint, not a present bug).
- **Verification-stage failures ARE honestly surfaced** — per-finding `verification_failed=True` →
  `VERIFICATION_FAILED` status → dedicated red banner row (`report_exporter.py:380,535`). (Contrast P0-1,
  which is specifically about *review*-stage failures.)
- **Extraction cache key is robust.** `(resolved_path, st_size, st_mtime_ns, head+tail SHA-256
  fingerprint)`; thread-safe (locked), LRU-bounded, returns deep copies. A changed file is not served a
  stale extraction in any realistic case. (`extraction_cache.py:53-133`)
- **Verification cache disk I/O is safe.** Atomic temp-file + `os.replace` under a lock; load tolerates
  corrupt JSON / schema mismatch by returning 0 entries. (`verification_cache.py:294-405`)
- **GUI threading is sound.** Worker runs off-thread; all widget updates marshal through `app.after(0,…)`
  gated by an epoch staleness check; `is_processing` blocks concurrent runs. No off-thread Tk access, no
  cross-run result bleed. (`review_run_controller.py:71-117`, `batch_controller.py`)
- **Truncated/incomplete review JSON is salvaged**, not discarded, via the fallback parser's backward
  bracket search. (`reviewer.py:_extract_json_array`)

---

## Verification approach (for the team executing this)

1. **P0-1 (the headline):** Hermetic test with 3 fixture specs where 1 is forced to truncate (use
   `tests/fixtures/fake_anthropic.py` `max_tokens` incomplete builder). Assert: the exported report
   distinguishes the failed spec (a banner row / corrected "Files Reviewed: 2/3"), and the diagnostics
   final status is not a bare "success". Today both will wrongly read as a clean complete run — that
   failing assertion *is* the bug.
2. **P1-1:** Build a cross-check result with an edit-bearing coordination finding; assert the sidecar entry
   has a non-empty, unique `finding_id`. (Will fail today.)
3. **P1-2:** Drive a batch verification whose tail flips to real-time; assert each tail finding ends with
   exactly one `VerificationResult` (no double-write, no `NOT_CHECKED` leftover).
4. **P2-3:** Read the batch dispatch path to settle parallel-vs-sequential; fix `CLAUDE.md` or, if truly
   parallel, add a shared-mutation test.
5. Run the full hermetic suite (`pytest`, no key/network per CLAUDE.md §9) after each change.

**Suggested sequencing:** P0-1 first (highest trust payoff; touches `report_exporter._summarize_run_diagnostics`
+ the GUI completion handler — both isolated). P1-1 is a small contained fix. P1-2 and P2-3 are
verify-first reads. The workstreams touch disjoint modules and can be dispatched in parallel.
