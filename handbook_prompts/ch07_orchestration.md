# Agent Prompt — Chapter 7: Orchestration & State

**Full title:** *Orchestration & State: The Pipeline Spine*

## Your mission
Explain the **spine** of the program: the orchestration module that sequences
every stage, holds the run's state, and performs the trust-critical joins —
deduplication, finding-id assignment, multi-file grouping, the token preflight,
and the reconciliation/repair of batch results. This is the chapter the
structural audit cared most about, because the spine is where correct leaf
output can still be dropped, misattributed, or made to look cleaner than it is.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (facts), §7 (glossary), §8 (template).
2. `CLAUDE.md` — "FindingGroup vs FindingOccurrence," "Token preflight raises
   (not warns)," and the high-level flow.
3. Source you own — `src/orchestration/pipeline.py`:
   - State objects: `PipelineResult`, `CollectedBatchState`, `BatchSubmission`,
     `_PreparedSpecs`, `FindingGroup`, `FindingOccurrence`.
   - Flow: `_prepare_specs`, `_run_exact_token_preflight`, `start_batch_review`,
     `collect_review_batch_results`, `_recover_retryable_review_batch_results`
     (the repair batch), `run_cross_check_for_batch`, `start_batch_verification`,
     `collect_batch_verification_results`, `finalize_batch_result`.
   - Joins: `_deduplicate_findings`, `_dedup_key`, `compute_finding_id`,
     `group_findings`, `_originals_by_filename`, `_normalized_text_digest`.
4. **Both audits — this is the spine chapter:** `STRUCTURAL_AUDIT.md` P0-1
   (partial failure looks clean), P1-1 (cross-check findings lack ids/dedup),
   P1-2 (batch→real-time fallback handoff), P1-3 (repair-miss surfacing), P2-1/2,
   and its whole "Verified-clean" section. `TRUST_AUDIT.md` P0-1 (sidecar
   under-emits multi-file), P0-2 (per-file anchor collapse), and "Dedup will not
   falsely merge distinct edits" (verified-clean).

## In scope (what you own)
- **The run-state objects** and how the spine threads them stage to stage
  (`BatchSubmission` → `CollectedBatchState` → `PipelineResult`).
- **Deduplication.** Why dedup runs **before** verification (so a verdict binds
  to a stable index in a non-reordered list). The `_dedup_key` — including the
  full-text SHA-256 digests of `existingText`/`replacementText` that prevent
  *wrong-text* merges — and `compute_finding_id` (12-hex). The merge: how
  `affected_files` and `occurrence_originals` are populated so per-file edit text
  survives the collapse.
- **Multi-file grouping.** `FindingGroup` / `FindingOccurrence` /
  `executable_finding()` — what they're *for* (per-file fan-out of the same
  defect). **State candidly that `group_findings()` is currently called only
  from tests, not production** (Audit TRUST P0-1): the data is preserved on the
  `Finding` but the sidecar emits one entry keyed to the representative file.
- **Token preflight.** `_run_exact_token_preflight` and why `_prepare_specs`
  **raises** `ValueError` over `RECOMMENDED_MAX` rather than logging — refusing a
  doomed run beats silently truncating a spec.
- **Reconciliation & the repair batch.** How `collect_review_batch_results`
  iterates the *submitted* ids and turns any missing/incomplete/errored result
  into a visible `error` + an entry in `truncated_specs`; how
  `_recover_retryable_review_batch_results` retries failures in a second batch.
  The honesty property: nothing is silently dropped at the data layer.
- **`finalize_batch_result`.** How findings, cross-check output, verdicts, and
  extracted-spec warnings are assembled into the final `PipelineResult`.

## Explicitly OUT of scope (owned elsewhere)
- The batch API wrapper itself → **Ch 6**.
- Verification internals (waves, grounding, the real-time fallback's *mechanics*)
  → **Ch 10** (you own the *handoff* question: that the spine hands findings to
  verification and gets them back; the audit item P1-2 about double-write/drop is
  shared — cover the spine side, point to Ch 10 for the verifier side).
- Cross-check chunking → **Ch 8**.
- Report/sidecar rendering and the diagnostics banner → **Ch 11**.
- GUI threading/epoch model → **Ch 13**.
- `DiagnosticsReport` internals → **Ch 14**.

## Narrative beats to hit
- *The spine's job is honesty.* Frame the chapter around the structural audit's
  thesis: the data plane is sound (joins are correct, verdicts can't bind to the
  wrong finding, batch results are reconciled against the submitted set), but the
  **edges** are where a *partially failed* run can still look like a clean one.
- *The headline open issue (Audit STRUCTURAL P0-1):* `files_reviewed` counts all
  *submitted* specs including ones that failed review; `truncated_specs` is
  plumbed through diagnostics but the report has no "specs that failed review"
  row, and the GUI shows a green checkmark regardless. A failed spec (0 findings
  because it truncated) is indistinguishable from a clean spec (0 findings).
  Present this as the most important thing the program is still perfecting.
- *The sidecar under-emission (Audit TRUST P0-1/P0-2):* multi-file defects emit
  one edit instruction for the representative file; the per-file machinery exists
  but isn't wired into the sidecar. Honest, specific, and tied to the
  emit-not-apply contract.
- *Why dedup-before-verify is load-bearing* and why the dedup key's text digests
  make a dangerous false-merge impossible.

## Invariants & facts you MUST get right
- Dedup runs **before** verification; verdicts attach by stable enumerate index
  into a non-reordered list with `original_custom_id` preserved across waves.
- `_dedup_key` includes SHA-256 digests of existing/replacement text (no
  wrong-text merge).
- `compute_finding_id` is 12 hex (48 bits); cross-check findings currently get
  `finding_id=""` (P1-1).
- Preflight **raises** over `RECOMMENDED_MAX` (500k).
- `group_findings()` is test-only today; `occurrence_originals` is populated but
  the sidecar emits one representative entry.

## Diagrams & tables
- A spine diagram: prepare → submit → collect(+repair) → dedup → [cross-check] →
  verify → finalize, with the state object on each arrow.
- A "dedup key" table: which fields are in the key (→ safe merges) vs. which are
  not (anchor/insertPosition/evidenceElementId → collapse to representative).
- A small "what the data layer records vs. what the artifact shows" table that
  sets up the P0-1 honesty gap (rendering owned by Ch 11).

## Cross-references to make
- To **Ch 6** (batch), **Ch 8** (cross-check), **Ch 10** (verification + the
  shared P1-2 fallback question), **Ch 11** (where the honesty gap would be
  fixed — banner row + sidecar fan-out), **Ch 13** (UI terminal state), **Ch 16**
  (the audit's framing).

## Deliverable
- Write to **`handbook/07_orchestration.md`**. H1 = the full title. Target
  **3,500–5,000 words**.

## Quality bar
- A reader understands the spine as the trust-critical join layer: what it does
  correctly (the data plane) and where it can still mislead (the edges). Audit
  items are represented accurately, not overstated. Defers verifier/report/GUI
  mechanics cleanly.
