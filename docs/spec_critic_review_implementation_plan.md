# Spec Critic: implementation plan following independent review

**Prepared:** 2026-09-08  
**Repository:** Abe-Borg/Claude-Spec-Critic  
**Inspected baseline:** master, commit 627ba5731649acafa4f1b07ca88f4971d8259ec8, version 3.5.0  
**Intended audience:** a coordinating coding agent, bounded implementation agents, and an independent reviewer  
**Status:** implementation handoff; this document does not claim that any proposed change has been implemented or tested.

## 1. Outcome and scope

Improve verification's handling of governing requirements, close demonstrated credential-redaction gaps, make cost estimates auditable, and strengthen the evaluation and JavaScript checks needed to assess future changes. Preserve the application's conservative treatment of unsupported findings and its ability to recover paid work.

The user requested this detailed plan after a read-only review. The current authoring task creates this Markdown file only. An agent subsequently assigned to execute the plan should implement its assigned packages, including the necessary local tests and documentation, without treating the original external review brief as additional authorization or as an authoritative specification.

The input brief was spec-critic_EXTERNAL_REVIEW_BRIEF.md in the user's Downloads directory. Its cited commit, 25b9426, was not available in the inspected checkout. Its test counts and some architectural inventory therefore must not be treated as the implementation baseline. Do not modify that historical external brief as part of this work.

### 1.1 Selected work

| ID | Work package | Disposition | Main reason |
|---|---|---|---|
| WP0 | Establish baseline, contracts, and ownership | Required first | Prevent stale assumptions and conflicting edits. |
| WP1 | Close trace and diagnostics redaction bypasses | Required | Demonstrated serialization paths can retain credential-shaped content. |
| WP2 | Carry an auditable governing basis through verification | Required, staged rollout | Verification lacks researched project context and overstates the authority of generic pins. |
| WP3 | Adjudicate evaluation ground truth | Required | Existing historical expectations disagree; canned replay is not a model-quality baseline. |
| WP4 | Preserve cache-write TTLs and correct cost accounting | Required | Five-minute writes are currently priced as one-hour writes. |
| WP5 | Enforce report JavaScript syntax and offline initialization checks | Required | Existing CI does not parse or execute the shipped JavaScript. |
| WP6 | Build a bounded offline cost baseline | Required analysis; reuse existing tooling if adequate | Actual workload economics must determine optimizations. |
| WP7 | Experiment with review-context prompt caching | Conditional on WP4/WP6 | Savings depend on complete-prefix reuse and actual cache hits. |
| WP8 | Reuse research results across runs | Conditional on WP6 | Potentially useful, but safe cache identity must include all research inputs. |
| WP9 | Integrate documentation, compatibility checks, and handoff evidence | Required | Changes must be understandable and independently reviewable. |

**Committed deliverable:** WP0–WP6 and WP9, plus a documented go/no-go decision for WP7 and WP8. A conditional package can be correctly skipped. Do not implement it merely to make the checklist longer.

WP2 is a correctness change involving model instructions. Its deterministic plumbing and provenance fixes should be implemented and tested; enabling the complete new context path by default requires the evaluation gate described below. Report a gated implementation as gated, not as fully rolled out.

### 1.2 Explicit exclusions

Do not include any of the following in this implementation:

- Lowering review effort, changing default review/verifier models, or reducing research/search budgets.
- Semantic deduplication that transfers a verified verdict to a merely similar claim.
- Multi-claim verification calls.
- Routing research, cross-check, or compliance through new batch stages.
- Program-wide cross-division coordination.
- An edit applier, automatic changes to source specifications, or changes to the emit-only design.
- A broad exporter, verifier, orchestration, or typing refactor.
- Blanket replacement of adopted standard editions with newest published editions.
- Retrospective rewriting or deletion of existing traces, caches, reports, or pending runs.
- An automatic release, version bump, merge, deployment, or billed API experiment.

These exclusions can be reconsidered in later work when the evidence supports them. They are not hidden requirements for completing this plan.

## 2. Evidence and corrections to carry into implementation

The review was a targeted static inspection, not an exhaustive proof of all application paths. No baseline tests or live runs were executed during review or plan authoring. Implementing agents must verify findings against their actual starting commit.

| Observation | Evidence anchor in the inspected source | Consequence |
|---|---|---|
| Verification receives finding fields and module code basis, without the structured research profile. | src/verification/verifier.py, _build_verification_prompt; src/orchestration/pipeline.py, location_inputs_for_submission | Add a bounded, explicit governing basis rather than the entire Project Context. |
| The verifier tells the model that pinned editions are authoritative even when search results differ. | src/verification/verifier.py, _pinned_standards_lines | Replace unconditional authority on the affected DC path; appending competing instructions is insufficient. |
| Some DC pins have UNVERIFIED provenance, omitted by edition-summary rendering. | src/modules/datacenter_fire.py; src/core/code_cycles.py, edition_summary_lines | Preserve provenance in the new basis and operator-visible explanation. |
| Prompt text bypasses normal trace scrubbing. | src/tracing/recorder.py, prompt_ref | Sanitize stored prompt content, including default and deep modes. |
| Deeply nested values become raw representations. | src/tracing/redaction.py, scrub_data; src/orchestration/diagnostics.py, recursive scrubber | Replace unsafe fallback behavior without losing unrelated content. |
| Aggregate cache creation is always priced at the one-hour multiplier. | src/core/pricing.py; src/core/api_config.py, cache usage extraction | Retain provider TTL breakdown and explicitly account for unknown legacy data. |
| Historical live oracle labels disagree with current labeled cases. | evals/calibration/fixtures_live/live_stale_nfpa72_0.json; evals/labeled_specs.py | Adjudicate the claim and evidence, not just the expected string. |
| Existing HTML tests check more than string presence, but no durable JS CI gate exists. | tests/test_html_report_exporter.py; .github/workflows/tests.yml | Add a small enforced gate; preserve existing useful tests. |
| Research depends on extracted corpus signals. | src/research/requirements_research.py, build_dimension_user_message; pipeline._run_research_phase | A jurisdiction-only research cache is unsafe. |

### 2.1 Important qualifications

1. Grounding checks establish that a cited URL matches a source retrieved by the verification tools. The retrieved pool includes both web_search and web_fetch. URL membership is not independent proof that the document supports the claim.
2. Historical research citations must not become fresh verification evidence merely by being included in a prompt.
3. Research budgets have a documented field-measurement rationale in docs/hyperscale_datacenter_module_plan.md. Their present optimality is unknown; they are not simply unexplained arbitrary numbers.
4. The inspected research inventory is eighteen dimensions across four modules: four, four, five, and five. Only routed active modules participate. Configured limits are not observed usage.
5. The canned eval baseline has nine fixtures and five seeded findings in its recall denominator. It cannot measure how a new prompt or model performs.
6. docs/html_report_baseline_evidence.md records earlier browser and syntax checks. The gap is recurring enforcement in CI, not an absence of all historical testing.
7. A scripted replay with invented token counts validates arithmetic and propagation, not real expenditure.
8. A maximum output allowance is not a bill for that many output tokens.

## 3. Non-negotiable invariants and compatibility policy

### 3.1 Trust and paid-work preservation

- CONFIRMED, CORRECTED, and DISPUTED continue to require accepted citations under the existing grounding policy.
- No research flag, provenance label, or nonempty quote bypasses current-conversation retrieval checks.
- Every finding obtains one terminal result. Successful escalation may replace an earlier result; do not confuse this with a prohibition on all intermediate assignments.
- Preserve fallback/follow-up-wave mutual exclusion, continuation caps, escalation guards, and terminal failure surfacing.
- Cache hits, shared results, missing research, failed reviews, and incomplete coverage remain visible.
- A post-spend validation or telemetry problem must degrade visibly, not discard a paid run.
- Keep registry validation at least as strict as it is now.
- Preserve rf-, cf-, and lc- identities, action semantics, per-file edit fan-out, and the emit-only sidecar contract.

### 3.2 California and intentional differences

California's unaffected prompts, tool dictionaries, routing, finding identities, cache keys, report wording, and golden fixtures must remain unchanged. None of these packages justifies mass regeneration of goldens.

There are narrowly defined intentional differences that must not be concealed behind a blanket byte-identity claim:

- Scrubbed traces can differ when their original content contained secrets or unsafe serialization branches.
- Cost diagnostics can differ when actual five-minute writes were previously priced incorrectly.
- The affected DC verifier basis and provenance displays can differ, including a profile-less DC run that formerly presented unverified generic pins as authoritative.
- An adjudicated oracle can change independently of unchanged historical model output.

For each intentional difference, record the affected surface, trigger, before/after behavior, and test. All other surfaces remain pinned. Do not apply the DC governing-policy rewrite to California as a side effect.

### 3.3 Platform, persistence, and tests

- Windows remains the deployment target; keep paths, Unicode, newline handling, frozen runtime behavior, and OS credential storage intact.
- Preserve existing pending-run compatibility. New fields are optional and versioned as needed; old paid batches must remain recoverable.
- Do not globally invalidate or delete the verification cache to simplify a migration.
- Pure helpers must not import the GUI, start a recorder, create an API client, or access the filesystem.
- Tests use synthetic data and scripted clients. Explicitly exclude network tests even if a real API key exists in the environment.
- No private specifications, real keys, or unsanitized trace excerpts belong in committed fixtures or CI artifacts.

## 4. Agent coordination and delivery sequence

### 4.1 Roles

The coordinating agent owns the dependency graph, contract decisions, shared documentation, CI integration, and final validation. Implementation agents own bounded packages. An independent reviewer examines the assembled changes, particularly persistence, reuse identity, and evidence attribution.

Use isolated worktrees where available. If agents share a checkout, do not allow concurrent edits to the same files. A shared filesystem is not isolation.

### 4.2 Suggested execution waves

| Wave | Coordinator | Independent agents |
|---|---|---|
| 0 | WP0 baseline and contract register | Read-only package reconnaissance if helpful. |
| 1 | Finalize WP2/WP4 contracts and documentation corrections | WP1 redaction; WP3 adjudication; WP5 JS tests. |
| 2 | Integrate Wave 1 and review new contracts | WP2 implementation; WP4 implementation in an isolated worktree; WP6 reader foundation. |
| 3 | Merge WP2 before resolving WP4's shared verifier/result fields; review combined telemetry | Complete WP6 against integrated schemas; independent regression review. |
| 4 | Decide WP7/WP8 from evidence | Implement only packages that meet their gates. |
| 5 | WP9, integrated tests, release-readiness report | Independent adversarial review of the final diff. |

WP1 and WP4 both touch diagnostics.py; WP2 and WP4 both touch verifier.py and pipeline.py. Coordinate the exact hunks or use isolated branches with a deliberate merge order. Never resolve such conflicts by blindly choosing one whole file.

README.md, CLAUDE.md, shared workflow edits, and dependency manifests have one integration owner. Workers submit precise documentation deltas to that owner.

### 4.3 Completion record required from every agent

Each package must return:

1. Starting and ending commit identifiers, or a clear uncommitted diff description.
2. Exact files changed and why.
3. Observable behavior before and after the change.
4. New/modified public interfaces and persistence fields.
5. Tests actually run, their outcomes, and explicit skips.
6. Remaining uncertainty, failure cases, and any plan deviation.
7. A package-specific rollback procedure.
8. A short reviewer checklist emphasizing the highest-risk path.

Use independent commits for independently revertible changes. Follow the repository's current branch/PR workflow when execution is assigned; do not merge or publish merely because this plan exists.

## 5. WP0 — Establish baseline and approve internal contracts

**Owner:** coordinator.  
**Dependencies:** none.  
**Why now:** every later package depends on the actual branch, schemas, and test behavior.

### Steps

1. Read current repository instructions, CLAUDE.md working agreements, README.md, TRUST_AUDIT.md, and the source relevant to the assigned package. Historical feature-specific instructions are design history unless they apply to the present task; do not invent new approval pauses from old work orders.
2. Record git status, current branch/commit, Python version, installed dependency consistency, and whether Node/browser tooling is available. Preserve existing user changes.
3. Run the hermetic baseline described in Section 15. Record actual counts, elapsed times, and skipped categories. Do not repeat the brief's 3,158-test claim without executing it.
4. Run the canned eval and calibration runners. Run the live-fixture replay diagnostically; distinguish historical model failures from infrastructure errors.
5. Inventory existing coverage and actual source signatures before adding new test files or helpers.
6. Create a short implementation decision register, either in the PR description or a dedicated implementation record, covering:
   - VerificationBasis schema and governing-policy version.
   - Basis placement, rendering, size limits, and cache fingerprint.
   - Legacy and bare-ID recovery behavior.
   - Cache usage counter names and unknown-data semantics.
   - JS tooling requirement and isolated CI job.
   - Feature gates and default-enablement criteria.
7. Identify available historical usage sources without modifying them. Do not recursively sweep unrelated personal directories.

### Acceptance and scope

A baseline report identifies pre-existing failures separately from regressions. WP2 and WP4 contracts are agreed before workers independently modify shared signatures. No production behavior changes in WP0.

**Risk:** mistaking missing local tooling for an application failure.  
**Rollback:** remove only newly created baseline artifacts if appropriate; never reset user changes.  
**Skip this if:** an equivalent verified baseline exists for exactly the same starting commit and environment; cite it and still inspect git status.

## 6. WP1 — Close credential-redaction bypasses

**Owner:** redaction agent.  
**Dependencies:** WP0.  
**Priority:** high; demonstrated serialization gaps, not evidence of an actual historical leak.

### Primary files

- src/tracing/redaction.py
- src/tracing/recorder.py
- Redaction/bounding and message handling in src/orchestration/diagnostics.py
- src/tracing/capture_hooks.py only if a proven boundary requires adjustment
- tests/test_tracing.py; tests/test_diagnostics_concurrency.py
- Proposed tests/test_secret_redaction.py
- Optional small leaf helper src/core/secret_redaction.py, only if it removes unsafe duplication without import cycles

### Required design

1. Define a single safe-serialization contract. Secret-named mapping values are replaced. Ordinary strings receive substring replacement for supported credential patterns, preserving surrounding nonsecret text and line breaks.
2. Preserve the existing supported patterns initially. Add new pattern families only with concrete positive and false-positive examples. Construction identifiers must not disappear merely because they are long.
3. Scrub prompt content before default-mode storage and before deep-mode inline return. Compute the prompt reference from the content actually stored. Secret-free prompts retain their old hashes.
4. Remove raw repr fallbacks at depth limits. Use an explicit safe marker for the affected branch; retain siblings and numeric telemetry. Scalar strings must still be scrubbed.
5. Detect cycles using the current traversal path. Repeated references in independent branches are not cycles.
6. Normalize supported dataclasses, mappings, sequences, sets, paths, enums, and primitives before final JSON serialization. Do not use an uncontrolled dataclasses.asdict traversal on cyclic input.
7. Make unknown-object serialization safe. Prefer a bounded type marker to executing arbitrary or recursive repr methods. No serialization exception may escape into the paid pipeline.
8. Protect all actual write boundaries: prompt JSONL, span/event/finding JSONL, synchronous run metadata, and the final serializer fallback. Inspect bound_structured_payload so truncation cannot expose a partly transformed secret.
9. Scrub DiagnosticsReport.log message text, not just its structured data.
10. Preserve byte caps, locks, queue behavior, recorder teardown, and numeric counters. Avoid repeatedly scanning large safe prompts at multiple layers without a clear reason.
11. If redaction counts are changed, count actual replacements rather than existing literal occurrences of the redaction marker.
12. Do not rewrite existing traces or claim historical files are now safe.

### Required behavioral cases

| Case | Expected result |
|---|---|
| Secret embedded between two paragraphs | Secret removed; both surrounding paragraphs survive. |
| Secret-named key at multiple nesting levels | Its value never reaches serialization. |
| Deep nested branch beyond limit | Safe marker, no raw representation, siblings retained. |
| Cyclic mapping/list | Finishes safely and emits a bounded marker. |
| Shared noncyclic object | Both branches retained normally. |
| Dataclass, set, enum, path, opaque object | Safe serializable output without new bypasses. |
| Failing or recursive repr | No secret output and no pipeline exception. |
| Default/deep capture levels | No synthetic secret in any written trace file. |
| Repeated safe prompt | Existing deterministic deduplication remains intact. |
| Secret changes but sanitized text is identical | Stored reference consistently describes sanitized content. |
| Diagnostics message containing a key | Message scrubbed; other diagnostics retained. |
| Existing literal redaction marker | Does not create a fictitious replacement count. |
| Concurrent recording and summary reads | Valid JSONL and unchanged accounting/locking guarantees. |

Tests must use temporary directories and fabricated credentials. Inspect run.json, prompts.jsonl, spans.jsonl, events.jsonl, and findings.jsonl produced by the real recorder lifecycle.

**Acceptance:** all listed serialization paths are protected; no surrounding-specification loss; existing recorder lifecycle and concurrency tests pass; no new dependency.

**Blast radius:** trace artifacts and diagnostics serialization.  
**Main risks:** excessive text removal, broken content references, lost telemetry, unnecessary copies.  
**Rollback:** one independent redaction commit; do not roll back unrelated cost work. A rollback reopens the gap, so document it rather than silently accepting it.  
**Skip this if:** implementation-time code already protects every cited path and tests demonstrate that protection.

## 7. WP2 — Give verification a consistent governing basis

**Owner:** verification agent; independent review required.  
**Dependencies:** WP0 contracts; coordinate overlapping files with WP4. WP3 supplies the evaluation discipline.

### 7.1 Problem and intended behavior

A DC verification request should receive a bounded snapshot of project adoption/AHJ research, source provenance, limitations, and module-reference assumptions. The snapshot must remain the same through both verification rounds, retries, continuations, escalation, shared-work grouping, and resume.

Do not send the entire free-text Project Context, attached specifications, or drawing digest to every finding. Do not use another model to summarize research in this package.

Correct the unconditional treatment of generic/unverified DC pins as authoritative. A module reference, a local adoption claim, an owner requirement, and the newest published standard are different things.

### 7.2 Immutable contract

Proposed new pure module: src/verification/governing_context.py. It must not import the research runner, which already depends on verification helpers. Accept serialized schema inputs or a dependency-free contract.

The existing ResearchItem fields are item_id, dimension_id, topic, category, requirement, authority, code_reference, source_urls, accepted_sources, grounded, confidence, actionability, and notes. RequirementsProfile adds research_date, project, and dimension_statuses. Reuse these facts; do not invent typed adoption/effective dates by guessing from prose.

Proposed VerificationBasis:

| Field | Meaning |
|---|---|
| schema_version | Stored representation version. |
| policy_version | Authority, selection, and rendering semantics. |
| mode | Recorded DC provenance-only or researched-context policy; California keeps its legacy path. |
| module_id / cycle_label | Owning module and cycle. |
| project | Snapshotted location and client, if available. |
| research_date | Original research date, never replaced during resume. |
| research_state | Available, partial, unavailable, or recovered without original snapshot. |
| items | Immutable selected facts with qualifications and recorded provenance. |
| dimension_statuses | Completion/failure information relevant to applicability. |
| module_basis | BaseCode entries, standard editions/amendments/provenance, seismic anchors such as ASCE, and the versioned code-basis rendering inputs. |
| omissions | Scope/size omissions and explicit incompleteness information. |
| fingerprint | Derived canonical identity, never independently supplied by a caller. |

Field names are proposed; equivalent names are acceptable if one documented contract is used consistently.

### 7.3 Construction, selection, and trust rules

1. Build once after research and before review submission/worker startup. A profile-less DC run builds its provenance-only basis from module data during preparation, without adding a research call.
2. Preserve claims and qualifications verbatim. Normalize structure, not legal meaning.
3. Include governing_code, local_amendment, referenced_standard, and ahj_requirement items first.
4. Include relevant client/owner/insurer specification requirements under explicitly separate authority labels because compliance findings can concern them. Do not silently omit all client requirements or relabel them as statutes.
5. Preserve process-advisory classification and exclusions. A permit/schedule advisory must not become a mandatory specification edit.
6. Revalidate row consistency. grounded=true with no accepted citations is inconsistent and must not render as grounded. Historical accepted_sources remain historical provenance.
7. Preserve contradictory claims rather than choosing the one with maximum model confidence.
8. Use deterministic selection and ordering. Target a compact block; propose a 4,000-token limit initially and validate it against representative saved profiles. Omit whole items, preserve qualifications, and show omissions explicitly. Never truncate a legal qualification mid-sentence.
9. Include all selection/omission choices in fingerprint semantics. If the size limit makes the basis unusable for a finding's applicability, preserve uncertainty instead of claiming complete research.
10. Use the existing prompt-serialization helpers to escape every project/item field. Research text remains untrusted data.
11. Keep research URLs out of the current verification conversation's accepted-retrieval pool. The verifier must retrieve supporting material itself before a verified verdict.
12. Do not add new report-status categories in this package. Use the existing incomplete/unverified/failure vocabulary and clear basis diagnostics.

### 7.4 Prompt authority and placement

For the affected DC path, replace the unconditional pinned-authority clause. The prompt should:

- Treat local adoption and AHJ research as claims to investigate, with their as-of date and limitations.
- Treat module editions as reference assumptions unless applicability is established.
- Mark UNVERIFIED provenance explicitly.
- Keep contractual requirements distinct from legal adoption.
- Require evidence for applicability when sources conflict.
- Avoid substituting the newest publication for an adopted edition automatically.
- Preserve UNVERIFIED when authority, effective dates, exceptions, or applicability cannot be established.

Use one central prompt-composition boundary. A wrapped, clearly labeled basis section may use the existing cached system-prompt structure if research text is explicitly treated as data, not instructions. A dedicated user-data prefix is also acceptable if it preserves the same contract and is justified in the decision register. Do not add message-caching changes here merely because WP7 exists.

California's current prompt assembly must use its existing branch unchanged. The profile-less DC path must disclose generic/unverified provenance rather than silently inheriting California's authoritative-cycle assumptions. Record that deliberate DC compatibility change.

### 7.5 Freeze and persist before spending

Carry the optional snapshot through:

- PreparedBatchReview and prepare_batch_review in src/orchestration/pipeline.py.
- BatchSubmission and _build_batch_submission.
- PendingBatch.from_submission, _pending_batch_from_mapping, and to_submission in src/orchestration/batch_resume.py.
- PendingProgramRun child persistence/reconstruction.
- reconstruct_batch_submission and normal GUI/headless resume paths.
- Any repair metadata paths that reconstruct the review submission.

Persist the entire snapshotted code basis: BaseCode rows, standard editions/provenance, seismic anchors, and the code-basis template inputs or a supported versioned rendering contract. A cycle label alone is insufficient. Validate module/project consistency.

For DC requests, resolve code-basis headers and both the cycle/standards portion of the cache key and its new fingerprint from the same saved basis. Do not mix current code_basis_format_kwargs or _standards_fingerprint inputs with an older snapshot. If the saved rendering policy is unsupported, use the explicit incompatibility path rather than silently rendering today's assumptions.

Use an additive optional field and its own version where possible. Do not repurpose unrelated batch/program schema versions or rewrite all pending files.

### 7.6 Recovery policy

| Situation | Required behavior |
|---|---|
| New run with complete research | Build and save one basis, then use it everywhere. |
| Partial research | Preserve successful facts plus failed-dimension and completeness warnings. |
| Ordinary resume | Use saved basis, date, policy, and project; ignore changed GUI fields. |
| Legacy saved run with structured research | Recover saved research facts, but do not infer historical module pins from a cycle label. Use a visibly reconstructed/new recovery basis for unknown assumptions, or explicit incompatibility; persist through the safe lifecycle. No new research call. |
| Saved snapshot from unsupported policy | Preserve paid results and surface incompatibility; do not reinterpret silently. |
| Bare batch-ID recovery | Mark historical context unavailable. Do not pretend current GUI location was the original location. |
| User supplies recovery context | Treat it as explicit recovery input with a fresh identity and visible provenance. |
| Invalid/mismatched snapshot | Fail closed for the affected applicability claim or degrade visibly; retain paid review results. |

Do not make snapshot errors a new reason to discard an otherwise recoverable paid review. Do not rerun research automatically during recovery.

### 7.7 All-path propagation checklist

Use the same immutable basis across:

- GUI round one and round two in src/gui/batch_controller.py.
- Headless round one and round two in pipeline.run_batch_collection_headless.
- verify_findings_for_run and _execute_verification_attempts.
- start_batch_verification and collect_batch_verification_results.
- verify_finding and _run_verification_call.
- Initial verification batch construction in src/batch/batch.py.
- _build_retry_request and _build_continuation_request.
- Batch wave context reconstruction.
- _run_batch_escalation_wave.
- Real-time fallback inside collect_verification_batch_results.
- Central build_verification_request in verification_routing.py.
- Program child collection, with each module retaining its own basis.

Keep run context separate from VerificationRoutingDecision unless inspection establishes a compelling reason otherwise. Do not change modes, models, budgets, permits, backoff, continuation caps, or fallback thresholds.

### 7.8 Cache and single-flight isolation

Extend make_cache_key with an optional derived governing-context fingerprint. Preserve the exact old California key when the new basis is absent. Use a versioned namespace/suffix for affected DC questions; do not purge the cache.

Fingerprint the interpretation-relevant snapshot actually rendered: module/cycle assumptions, provenance, selected facts and qualifications, location/client, research date, missing-dimension state, omissions, and policy version. Deterministically equivalent inputs should agree. Materially different questions must not.

Keep the existing model-independent verified-claim cache policy unless a separate evidence-based change is justified. A prompt policy/version change affecting the question is different from merely changing verifier model provenance.

Propagate identity to every get/put and shared-work path, including preparation, initial grouping, cache-fill race checks, follower rechecks, takeover, real-time fallback, initial stores, and escalation stores. Inspect _stamp_grounded_cache_hits and _verify_findings_singleflight explicitly.

Two identical findings under different governing bases must not share a flight, including the in-process sharing of clean ungrounded results. The same basis must still allow existing sharing.

### 7.9 Acceptance tests

Add proposed tests/test_verification_governing_context.py and extend the existing location, persistence, cache, and transport tests.

Required cases:

1. An older project-adopted edition reaches all paths without an instruction to automatically override it with the newest/module edition.
2. Verified research provenance, unverified research, conflicting claims, generic pins, and failed dimensions remain distinct.
3. Historical research URLs alone cannot pass the current verification grounding gate.
4. Context strings containing closing tags, instructions, quotes, Unicode, and control-like text remain data.
5. Changing a fact, qualification, provenance, client, policy, or relevant omission changes reuse identity.
6. Equivalent canonical inputs produce the same fingerprint.
7. Different bases isolate concurrent findings; equal bases retain sharing.
8. Resume after changing GUI location, current module data, or current date retains the saved basis or emits an explicit policy incompatibility.
9. Legacy/bare-ID recovery is honest and never silently re-researches; today's module pins cannot masquerade as missing historical assumptions.
10. Both rounds, repair/retry, continuation, escalation, and fallback carry the same basis.
11. Oversized context is bounded with visible omissions.
12. California prompts, tools, cache keys, and unaffected reports remain byte-identical.
13. Existing unsupported-verdict, disputed-grounding, models_disagreed, budget-exhaustion, and exactly-once regressions remain green.
14. DOCX, HTML, and sidecar consumers give a consistent, concise account of the basis used; no exporter rewrite.

### 7.10 Delivery and rollout

Deliver as contract/helper tests, then persistence/reuse plumbing, then prompt/provenance integration, then evaluation and visibility. These may be separate commits but must not be activated in an unsafe partial combination.

Define two DC modes explicitly. The required provenance-only correction removes unconditional authority from generic/unverified pins, including in profile-less DC runs, and receives its own saved policy and new cache namespace. The researched-context expansion remains narrowly gated during evaluation. Turning that context gate off must not restore the old authoritative wording or reuse verdicts under the superseded DC policy. California retains its existing path.

Snapshots record mode and policy; a flag change must not silently change a resumed run. Documentation must state which correction is active and which expansion is gated.

Before default enablement, evaluate domain-reviewed cases covering different local adoptions, partial/misleading research, uncertain pins, conflicting authorities, and contractual requirements. Measure incorrect confirmations and disputes, as well as unresolved results. Hermetic tests establish transport and safety behavior, not improved model reasoning. A billed comparison requires an authorized budget; otherwise mark the rollout pending and retain truthful limitations.

**Blast radius:** verification prompts, DC provenance, cache identity, saved runs, and basis summaries.  
**Main risks:** missed propagation, stale verdict reuse, fabricated authority, silent recovery drift, extra input cost.  
**Rollback:** stop new-run activation while preserving readers for saved snapshots; maintain namespace isolation. Never silently downgrade an in-flight basis to the old policy.  
**Skip this if:** every affected feature is already correctly implemented and behaviorally verified. Individual optional client-category expansions can be deferred only with visible scope limitations; cache/persistence isolation cannot be skipped when context changes the question.

## 8. WP3 — Adjudicate evaluation expectations

**Owner:** evaluation agent.  
**Dependencies:** WP0; coordinate WP2 scenarios.  
**Purpose:** establish trustworthy evaluation labels before using them to justify prompt or cost-policy changes.

### Files

evals/labeled_specs.py, evals/live_capture.py, evals/calibration/fixtures_live/, evals/calibration/README.md, and narrowly necessary loader/harness/scorer changes. Extend tests/test_labeled_specs.py, tests/test_live_capture.py, tests/test_eval_judge.py; add proposed tests/test_calibration_oracles.py.

### Steps

1. Inventory all twelve current live captures and map each to its labeled case explicitly. Avoid fragile name splitting.
2. For each case, compare the specification defect, finding assertion, proposed remedy, captured response, retrieved/cited evidence, old oracle, and current expected label.
3. Distinguish whether the spec is defective, whether the finding is correct, and whether the captured evidence permits a verified status. CORRECTED means the finding needed correction; it does not merely mean the spec needs an edit.
4. Adjudicate conflicting cases individually. A stale citation can be a real defect while the proposed replacement edition is itself wrong.
5. When determining code applicability, consult primary jurisdiction/standard sources for the fixture's actual cycle. Record support and date. Never use current application output as the oracle.
6. Preserve captured responses and original findings. Do not rewrite historical model evidence to match new expectations.
7. Put the review ledger outside fixture-discovery directories, for example evals/calibration/oracle_reviews.json. Record fixture ID, labeled-case ID, hash of the immutable evidence portion, review state, verdict/status, rationale, source references, and adjudication date.
8. Mark insufficiently supported cases unresolved. Show exclusions and denominators explicitly; do not silently drop difficult cases.
9. Add an integrity check for dispositions and changed evidence identities. Hash immutable captured evidence, excluding the mutable oracle/ledger fields. This check must not require historical model responses to agree with corrected ground truth.
10. Keep changes to labels, metadata, and scorer behavior independently reviewable.
11. Explain canned replay, historical replay, and new live evaluation separately in docs and output.
12. Define the representative WP2 evaluation set before reviewing the new model outcomes. Record model/prompt/effort/judge settings for any future comparison.

Add a narrowly scoped reviewed-only path to the existing calibration runner, proposed --oracle-reviews PATH --reviewed-only. It validates ledger dispositions/evidence hashes, scores reviewed expectations, excludes unresolved cases with their IDs/reasons and denominator shown, and preserves genuine model failures. Existing invocation without these options remains the historical diagnostic replay. Missing or inconsistent adjudication metadata must fail validation, not silently exclude more cases. Add tests proving the runner actually consumes the ledger; a standalone ledger file ignored by scoring does not complete this package.

### Acceptance

Every existing live fixture has a documented disposition. Every changed label has an independent rationale. Captured model output is unchanged. Genuine historical model mistakes remain visible. No blanket baseline rewrite is used to force a green score.

The normal canned runners remain hermetic and pass their intended regression expectations. A live-fixture replay may correctly report historical model errors; its nonzero quality result is not automatically an implementation failure. Malformed fixtures or inconsistent adjudication metadata are implementation failures.

**Blast radius:** evaluation labels and quality claims, not production verdict logic.  
**Risks:** replacing one questionable oracle with agreement-to-code; erasing model errors; moving denominators invisibly.  
**Rollback:** revert a label and its adjudication record together; preserve captured evidence.  
**Skip this if:** an individual label cannot be justified; record unresolved status. Skip the package only if all captures already have current, evidence-backed dispositions.

## 9. WP4 — Preserve TTL usage and correct cost accounting

**Owner:** accounting agent.  
**Dependencies:** WP0 counter contract; coordinate WP1/WP2 shared files.  
**Scope:** accounting changes only, with no request-policy change.

### 9.1 Counter contract

Retain aggregate cache_creation_input_tokens and cache_read_input_tokens. Add consistent counters such as:

~~~text
cache_creation_5m_input_tokens
cache_creation_1h_input_tokens
cache_creation_unknown_input_tokens
cache_creation_breakdown_status
~~~

Missing detail is not reported zero. Ten thousand aggregate writes without provider TTL detail means ten thousand unknown-TTL writes, not known-one-hour writes.

For trustworthy normalized data:

~~~text
known_5m + known_1h + unknown = aggregate_creation_tokens
~~~

Keep the aggregate and components from being added together as separate spend.

Handle absent, partial, negative, malformed, and contradictory fields defensively. Preserve trustworthy aggregate usage; classify unreliable detail as unknown and expose an accounting warning. Telemetry normalization must not discard paid results.

### 9.2 Pricing policy

For the currently supported model rates, known five-minute writes use 1.25 times input price; known one-hour writes use 2 times; reads use 0.1 times. Token costs retain the batch factor; web-search charges do not receive that factor. Revalidate the provider contract before changing pricing.

Keep the old conservative 2-times estimate for unknown-TTL writes, with an explicit assumption/unknown-token count. This preserves legacy numerical behavior while making its uncertainty visible. Optionally show an interval for the unknown component; do not expand the entire GUI into a billing dashboard.

Keep existing estimated_cost_usd keys. Add detail alongside them. Unknown models remain unpriced. Missing usage is unavailable, not evidence of a free call.

### 9.3 Propagation inventory

Inspect and update every boundary already carrying aggregate cache usage:

- api_config cache-usage extraction, including SDK objects and dictionary-shaped synthetic inputs.
- ReviewResult and review response parsing, including refusals and incomplete outcomes.
- Realtime review diagnostics and repair outcomes.
- Verifier _cache_token_usage, _USAGE_COUNTER_KEYS, _usage_counters, _merge_usage_counters, _conversation_view, and _ConversationEvidence.
- VerificationResult fields, terminal unverified/error construction, and call_usage entries.
- Batch prior_usage, follow-up waves, escalation success/failure, and synchronous fallback.
- Research _DimensionOutcome, response aggregation, and dimension diagnostics.
- Cross-check/compliance result handling.
- Drawing digest/impact result handling.
- Chunked-pass and program aggregation.
- GUI-to-diagnostics handoff.
- Diagnostics _CALL_USAGE_COUNTERS, _billable_calls, phase totals, summaries, and report presentation.
- Shared-result clones and verification-cache runtime-field exclusions.

Audit model/transport tagging at the same granularity as billed data. Do not invent a mixed-transport defect if current code handles it correctly; add a representative test. An aggregate covering batch and realtime usage must not acquire one blanket discount.

call_usage remains authoritative when present. Do not price its entries and the kept-result flat counters again. Durable verdict cache hits and shared followers contribute zero current-run spend; their historical provenance may remain separately visible.

No verification cache schema invalidation is needed merely to retain runtime accounting fields. Exclude new spend counters from durable verdict projection.

### 9.4 Numerical acceptance examples

Using Sonnet 5's inspected standard input price of USD 2 per million tokens:

| Input | Cache-write estimate |
|---|---|
| 1,000 known 5-minute tokens | USD 0.0025 |
| 1,000 known 1-hour tokens | USD 0.004 |
| 1,000 unknown legacy tokens | USD 0.004, explicitly conservative |
| 1,000 5-minute + 2,000 1-hour + 3,000 unknown | USD 0.0225; aggregate is 6,000 |
| Same mixed example, all batch transport | USD 0.01125 for these token writes |

These isolate cache-write cost; no input/output/read/search charge is included. Use exact test expectations or appropriate numerical tolerances. Do not count 6,000 additional aggregate tokens.

### 9.5 Tests and acceptance

Extend tests/test_pricing.py, test_diagnostics_cost_pricing.py, test_verification_token_telemetry.py, test_batch_escalation.py, test_chunked_pass_engine.py, test_requirements_research.py, drawing tests, test_verification_cache_serialization.py, and test_verification_singleflight.py.

Cover known/mixed/unknown TTLs, explicit zero versus missing, malformed detail, batch discount, searches, unknown model, continued conversations, failed escalations, replay zeroing, and concurrent aggregation. Preserve locks and existing output keys.

**Acceptance:** real provider TTL information survives to totals; legacy uncertainty is visible; no double counting; no API request, prompt, routing, or verdict behavior changes. Corrected diagnostic amounts are the only intended report-data differences.

**Blast radius:** result telemetry and cost summaries across phases.  
**Risks:** dropped fields, double counting, wrong transport discount, treating missing data as zero.  
**Rollback:** independent accounting commit; no prompt-cache policy or durable verdict changes to reverse.  
**Skip this if:** equivalent complete accounting already exists or a verified provider contract establishes a single write rate. Current evidence does not support skipping.

## 10. WP5 — Enforce JavaScript syntax and offline browser checks

**Owner:** report-test agent.  
**Dependencies:** WP0; coordinator owns final CI/dependency integration.  
**Scope:** test the bytes shipped by the existing exporter; do not rewrite its embedded JavaScript.

### 10.1 Syntax gate

Add proposed tests/test_html_report_javascript.py.

1. Generate representative reports through production write_html_report: single-module, program, hostile-content, and chat-disabled variants.
2. Read the generated bytes directly and decode UTF-8 without universal-newline conversion.
3. Extract every executable inline script while excluding application/json data blocks. Validate expected script counts explicitly; do not accidentally test only the first match.
4. Run node --check on exactly those bodies, using stdin or temporary files that preserve the bytes.
5. Check return code, stderr, and timeout. A missing Node executable must fail in the required CI job.
6. Recompute the CSP hash over the same script bytes. Preserve existing exporter security tests.
7. Include a deliberately malformed synthetic script to prove that the test's rejection path works.
8. Keep file-generation fixtures compact and deterministic. Do not commit full generated reports just to test syntax.

### 10.2 Bounded offline browser smoke

Add proposed tests/test_html_report_browser.py.

Use a fresh headless Chromium context and the actual written report file. Keep CSP enabled. Install request interception before navigation; reject external traffic. No real API key, API call, source document, or existing browser profile is used.

Required behaviors:

- Initial report loads without JavaScript errors or CSP violations.
- Search/filter hides and restores the expected findings.
- Expand/collapse operates on visible report content.
- Chat opens to its key-entry state; model options/starter UI initialize where applicable.
- Chat can close; the no-chat report has no chat controls and still filters normally.
- Hostile report-derived text stays inert.
- Opening and interacting with these controls causes no external request.

Observe errors before navigation, not after initialization has already failed. Use bounded condition waits, not arbitrary sleeps. Cap individual operations and total job duration.

This smoke does not claim to test SSE framing, tool loops, or live API compatibility. Do not expand it into a comprehensive chat simulator unless a concrete defect warrants the extra maintenance.

### 10.3 CI and dependencies

Retain the fast Python job. Add an independent job with:

- An explicit supported Python and Node version.
- Existing application/test dependencies.
- A small pinned browser-testing dependency file, proposed requirements-browser-tests.txt.
- Chromium installation during setup.
- A proposed SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1 setting that converts missing Node, Playwright, or Chromium into failure in both new test modules.
- Explicit -m "not network" test selection and no production credentials.

Ordinary local pytest may skip unavailable optional browser tooling. The required CI job must not pass through those skips. Keep browser packages out of runtime requirements and the frozen Windows application.

Dependency installation can use network access during CI setup. Test execution must remain offline. Document these as separate phases.

**Acceptance:** malformed shipped JavaScript, broken initialization, hostile-content execution, or missing required tools fail CI; production report bytes remain unchanged.

**Blast radius:** test/development dependencies and CI only unless a demonstrated report bug is uncovered.  
**Risks:** permissive skips, testing reconstructed rather than actual script bytes, flaky browser waits.  
**Rollback:** remove the independent job/test additions without touching production rendering.  
**Skip this if:** an equivalent enforced syntax/runtime gate already covers the actual exported reports. Historical manual checks are insufficient.

## 11. WP6 — Build an offline cost and workload baseline

**Owner:** measurement agent.  
**Dependencies:** WP0 inventory; WP4 for complete new accounting. Reader work can start earlier.  
**Scope:** use existing local records without starting the application, re-running research, or making API calls.

### 11.1 Inputs and precedence

Inspect existing tracing/reporting tooling before writing another reader. Prefer extending a suitable small tool over creating a second reporting framework.

Possible sources:

- Saved diagnostics events or summaries.
- HTML report-embedded run_diagnostics.
- Existing run.json, spans.jsonl, events.jsonl, and findings.jsonl.
- Explicitly supplied exported usage records.

Historical traces do not necessarily contain complete token usage or call_usage. Current capture hooks emphasize response content, and some verification span outputs omit billing counters. Inventory coverage before presenting totals.

Establish a source-precedence/deduplication rule. The same run represented by diagnostics, a trace directory, and an HTML report is one run, not three bills. If exact identity is unavailable, flag possible overlap rather than asserting a clean total.

Use explicit paths or the narrowly configured trace root. Do not traverse unrelated home directories. Default output can be stdout; write a report only to an explicit destination.

The reader must not initialize TraceRecorder, trigger retention/pruning, extract original specifications, resolve live URLs, or construct an API client.

### 11.2 Report schema

Report, where actually available:

| Group | Fields |
|---|---|
| Provenance | Input path/type, run ID, run date, extraction method, schema/accounting version. |
| Workload | Program, active modules, transport, number of specs and routed pairs, completion state. |
| Usage | Input/output/read/write tokens, known TTL components, searches, missing/unknown counters. |
| Attribution | Phase, model, module, transport, retry/continuation/escalation classification where recorded. |
| Money | Rate-table identity/date, priced amount, conservative unknown component, unpriced usage. |
| Timing | Observed phase/run duration, including incomplete work where available. |
| Completeness | Data that is missing, inferred, aggregated, duplicated, or not comparable. |
| Opportunity | Candidate change, addressable spend, likely net saving, uncertainty, quality/latency risk. |

Keep incomplete/failed paid runs visible as a separate cohort. Do not remove them to make the normal-run cost look smaller.

Separate HTTP-response counts from aggregated conversation counts. A diagnostic event may include several continuation responses. Do not invent call counts from event counts.

Do not infer billable searches from the number of URLs. Do not equate cache-token ratios with whole-run dollar savings. Historical usage repriced with current rates is a current-rate comparison, not a reconstruction of the historical invoice.

### 11.3 Prefix-reuse analysis

Analyze repeated review context only where sufficient request material is recorded. A reusable prefix depends on the entire preceding request configuration: model, tools/parameters, system, thinking/effort, module/cycle, introductory text, element-ID hints, and effective context.

Group by a privacy-preserving local digest of those effective inputs. Do not copy raw private project content into a committed report. If inputs are missing, mark reuse unknown rather than equating identical file names or client names with identical prefixes.

The original illustrative math should be corrected in any report:

~~~text
One-hour cache, one write and r reads:
cached units = 2 + 0.1r
uncached units = 1 + r
saving requires r > 1.111..., hence at least two reads.

Fourteen requests, one prefix, one write + thirteen reads:
14 / 3.3 = approximately 4.24 times cheaper on that portion.

Fourteen requests across four distinct equal-sized prefixes,
one write per prefix + ten reads:
14 ordinary units versus 9 cached units,
approximately 35.7 percent saving on that portion.
~~~

These are idealized calculations, not observed cache behavior. Concurrent batch execution can produce additional writes/misses.

### 11.4 Tests and decision output

Add a focused offline-reader test module if new tooling is needed. Cover:

- Two files describing the same run.
- Aggregate-only history and unknown TTLs.
- Truncated/malformed JSONL and missing files.
- Interrupted/failed runs.
- Unknown models and mixed transports.
- Incomplete request capture.
- No diagnostics at all.
- Deterministic output.
- No network, recorder startup, pruning, extraction, or implicit writes.

Use sanitized synthetic fixtures. No new model call is necessary.

Deliver a ranked decision table for WP7/WP8: observed spend, recoverable fraction, expected absolute saving, assumptions, sample size, and confidence. State the workload range. Do not extrapolate one large hyperscale package to small California projects.

If historical data cannot support a decision, record that result and arrange passive telemetry on the user's next otherwise-planned run. Do not manufacture a measured baseline from scripted token counts.

**Acceptance:** every dollar figure has a coverage statement and rate basis; duplicate sources are not double-counted; missing information remains explicit; WP7/WP8 each receive a defensible go/no-go/deferred decision.

**Blast radius:** offline tooling and analysis artifacts.  
**Risks:** incomplete history presented as a complete bill, duplicate counting, accidental trace mutation.  
**Rollback:** remove the reader/report independently.  
**Skip this if:** existing reporting already supplies equivalent actual-usage attribution and uncertainty; cite the evidence and reuse it.

## 12. WP7 — Conditional review Project Context caching

**Owner:** request-builder agent.  
**Dependencies:** WP4/WP6 and stable WP2 submission schemas.  
**Gate:** WP6 identifies repeated, sizable context within genuinely identical complete prefixes and plausible positive net savings. No new default behavior on the basis of request counts alone.

### 12.1 Smallest useful implementation

Add a narrowly scoped, initially default-off option for eligible profile-enabled DC review requests. Store the selected policy on the prepared/submitted run so repair and resume cannot silently change it. Proposed option name: SPEC_CRITIC_REVIEW_CONTEXT_CACHE; final naming follows repository conventions.

The default and profile-less California paths retain their current string-shaped user content and identical token-count cache keys. Eligibility must come from actual module/profile/run state, not a global environment flag alone.

Use src/review/prompts.py to expose a deliberate prefix/suffix boundary while preserving the public string builder. Joining the new blocks must exactly reproduce the old message text.

Do not locate the boundary by searching arbitrary text for the first closing project_context tag. Derive it from the same structured assembly that creates the wrapper, introductory instructions, context, spec, pre-detected alerts, final task, and repair suffix.

Place an explicit breakpoint on the text block ending with the reusable context. Keep the spec and repair instruction after it. Preserve system/tool cache markers and total marker limits.

Choose TTL using observed reuse intervals and the provider's valid ordering rules. Do not shorten earlier one-hour markers merely to add a later five-minute marker; do not put a longer-lived entry after a shorter-lived one if the API forbids that ordering. Verify current rules before coding.

### 12.2 Request and counting parity

Update ReviewRequestSpec with an explicit request-shape/policy input. Route initial batch, realtime, repair, and restored submissions through the same builder.

build_token_count_request must count the actual content-block shape. Remove cache-pricing fields with a nonmutating projection only as required by the counting API. Do not count a concatenated surrogate string and submit a different block structure.

Version the token-count cache identity for the opted-in shape. Legacy/off keys remain identical. Include relevant options in GUI/preflight invalidation so a cached exact count cannot survive a material shape change.

Respect the model's cache minimum and the four-breakpoint limit. On the reviewed documentation, the minimums differ by model; resolve current values during implementation rather than assuming one global 1,024-token rule.

Do not add global automatic caching, continuation caching, speculative warm-up calls, or changes to effort/output caps in this package.

### 12.3 Acceptance and measurement

Required hermetic cases:

- Block concatenation equals the old message exactly.
- Empty context, whitespace, CRLF, Unicode, escaped closing tags, element-ID hints, alerts, and repair suffixes.
- Default/off/profile-less CA payload and count-key byte identity.
- Actual count projection and submitted block structure agree.
- Prefix grouping separates different modules, tools, hints, effort, and context.
- Saved policy survives restart/repair.
- No extra messages, API calls, or cache markers appear accidentally.

Extend test_token_budgets.py, test_realtime_review.py, test_review_repair_hardening.py, test_batch_resume.py, prompt-serialization and golden tests; add a focused request-builder test if needed.

A live success claim requires actual read/write usage and positive dollar savings after writes, without new failures. Report sample size and batch hit variability. An unchanged string is necessary for behavioral preservation but is not a measurement of live model quality or API acceptance.

**Blast radius:** opt-in DC review request shape, preflight identity, and saved policy.  
**Risks:** cache misses that increase cost, incorrect prefix partitioning, preflight drift, repair/resume divergence.  
**Rollback:** disable for new runs; preserve recorded policy/readers for existing runs or explicitly perform a safe versioned transition.  
**Skip this if:** prefixes mostly occur once/twice, context is small, batch hits are poor, or absolute savings do not justify complexity.

## 13. WP8 — Conditional cross-run research cache

**Owner:** research-cache agent.  
**Dependencies:** WP4/WP6 and finalized WP2 provenance semantics.  
**Gate:** actual repeat research inputs justify reuse after conservative invalidation.

### 13.1 Key and entry design

Prefer per-dimension entries so a new dimension does not invalidate unrelated work. Do not copy the entire verification-cache implementation merely because it exists.

Fingerprint the effective research inputs:

- Module/dimension identity.
- Project location and client.
- Code basis and standard/provenance assumptions.
- Rendered corpus signals from the current documents.
- Research system/user prompt or explicit stable protocol revision.
- Output schema.
- Model and material tool/policy configuration.

A changed client, package signal, source-quality policy, dimension prompt, or standards basis must not hit an incompatible entry. TTL is a freshness policy, not a replacement for a complete key.

Store original research date, items, accepted/cited provenance, dimension status, key/schema versions, and historical usage separately from new-run accounting. Avoid storing entire source specifications merely to establish identity.

### 13.2 Reuse policy

Reuse only completed, valid entries under an explicit policy. Do not turn a failed, structurally invalid, or budget-exhausted result into a successful hit. A completed dimension can contain unverified items only if their uncertainty is retained; never promote them on replay.

Validate entries on load. Corruption, unknown schema, or mismatched input becomes a miss. Use atomic writes and bounded storage. Do not introduce single-flight machinery beyond demonstrated concurrent demand.

Choose a configurable TTL based on actual revision cadence. The external brief's fourteen-to-thirty-day suggestion is not a validated requirement.

Expose original age/provenance and a refresh/bypass control. A refresh failure must not silently substitute stale results. If stale fallback is ever allowed, it requires explicit policy and visible stale/failed-refresh status.

Reused research records zero new API tokens/searches. Preserve historical usage only as provenance. Mixed fresh/cached dimensions retain separate original dates; a profile assembled today must not pretend all facts were researched today.

### 13.3 Persistence and visibility

Resume continues to use the saved run profile/basis; it must not consult a mutable cross-run cache to reinterpret paid work.

Keep profile/sidecar readers backward compatible. If a new envelope/version is needed, document it separately from verification-cache versions. DOCX and HTML must agree on reused/partial research status.

Test default/disabled behavior for exact compatibility. Avoid silently enabling the cache for California or other modules without research capability.

### 13.4 Acceptance

Test invalidation for all listed key inputs; expiry; corrupt/unknown entries; concurrent access; atomic-write failure; partial dimensions; refresh failure; mixed source dates; replayed-cost isolation; saved-run isolation; and visible report/profile provenance.

Extend requirements research, research concurrency, batch resume, cache visibility, DC end-to-end and report tests. Synthetic profiles are sufficient for the cache mechanism.

**Blast radius:** research execution, provenance, local storage, and reports of reuse.  
**Risks:** stale/inappropriate applicability claims, concealed refresh failure, false accounting, excessive invalidation eliminating value.  
**Rollback:** disable cross-run lookup while preserving existing run snapshots and readable exported profiles. Leave cache files untouched.  
**Skip this if:** genuine identical-input repetition is rare, reliable invalidation removes most hits, or measured saving is immaterial.

## 14. WP9 — Documentation, integration, and independent review

**Owner:** coordinator and a reviewer who did not author the high-risk changes.  
**Dependencies:** all selected packages and conditional decisions.

### 14.1 Documentation edits

Update README.md for operator-visible behavior and CLAUDE.md for engineering contracts. Keep additions precise; do not shorten either document as a cleanup exercise.

Correct or qualify:

- Full Project Context versus the actual compact governing basis verification receives.
- Generic pins, adopted editions, newest publications, and UNVERIFIED provenance.
- URL retrieval grounding versus substantive support.
- Existing implicit server-tool caching versus explicit application breakpoints.
- One-hour break-even math and model-specific minimums.
- Tools-to-system-to-messages invalidation; changing a tool can invalidate later prefixes.
- Research dimension counts and configured limits versus observed usage.
- Known TTL billing versus conservative unknown estimates.
- Canned fixture counts and what each evaluation mode establishes.
- Existing HTML/security tests versus the new enforced browser/syntax gate.
- Recovery provenance and cache fingerprint semantics.
- Feature gates, defaults, current activation status, and rollback behavior.

Update requirements only when dependencies actually change. Browser tooling belongs in its test-only dependency file, not the packaged runtime.

Update docs/standards_provenance.md only with verified factual changes or clarified provenance treatment. Do not replace UNVERIFIED labels without authoritative evidence. Do not imply a newer NFPA edition must govern a particular project solely because it is newer.

Do not change version literals just to deliver these fixes. If release work is separately requested, synchronize all five existing version literals and run the release-version check.

### 14.2 Integration checks

1. Review the final diff for unexpected files, test-only dependencies in production, generated artifacts, and private data.
2. Recheck optional defaults and legacy serialized inputs.
3. Confirm no newly enabled path can retrieve a verdict under a different basis key.
4. Confirm current retrieval evidence stays separate from historical research citations.
5. Confirm both GUI and headless drivers carry the same basis and accounting.
6. Confirm every newly added usage field survives aggregation and is zeroed on replay.
7. Confirm browser CI cannot succeed by skipping absent tooling.
8. Confirm oracle changes preserve historical responses.
9. Review intentional golden/report changes one by one.
10. Run focused checks after each package, then one complete integrated suite. Repeat only after a new change/failure/unresolved concern.
11. Record conditional packages as implemented, skipped with evidence, or deferred with missing evidence.
12. Record WP2 rollout status honestly; tests of request assembly do not establish domain reasoning accuracy.

### 14.3 Independent review scenarios

The reviewer should try to demonstrate:

- A researched-but-not-retrieved URL producing a verified verdict.
- Two equal claims under different contexts sharing a verdict or in-flight result.
- A resumed paid batch picking up today's location, date, standards, or feature flag.
- Unverified generic standards becoming authoritative through another prompt surface.
- A redacted prompt leaking its original secret through a hash-associated alternate record or repr fallback.
- One paid continuation/escalation appearing twice in totals.
- A durable cache hit acquiring new-run usage.
- A historical model error being erased through an oracle edit.
- Invalid exported JavaScript passing the new CI check.
- A conditional optimization becoming default despite a failed/absent economics gate.

A failed attempt is useful evidence, not proof that all possible paths are safe. Record exactly what was tested.

**Blast radius:** integration and documentation only, unless review identifies a concrete defect.  
**Rollback:** each package retains its own rollback; preserve new snapshot readers before disabling behavior.  
**Skip this if:** never skip final integration. A package-specific check may be inapplicable only with an explicit reason.

## 15. Verification commands and acceptance matrix

All commands below are for later implementation, not a claim that they were run while writing this plan. Paths in commands are repository-relative for portability. The inspected Windows environment has venv/Scripts/python.exe; another agent should resolve its own interpreter rather than assuming that path exists.

### 15.1 Baseline and final hermetic checks

Windows PowerShell examples, from the repository root:

~~~powershell
& .\venv\Scripts\python.exe -m pip check
& .\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not network"
& .\venv\Scripts\python.exe -m evals.runner
& .\venv\Scripts\python.exe -m evals.calibration.runner
& .\venv\Scripts\python.exe packaging/windows/check_release_version.py --tag v3.5.0
~~~

The release-check tag above matches the inspected baseline. Resolve the intended version at execution time; do not change application versions just to satisfy this example. The explicit marker exclusion is important: tests/conftest.py can allow network-marked tests when a real key exists. Do not print that key or rely on its absence. The canned eval runners are scripted; review their implementation if it changes.

Do not use --write-baseline merely to clear a failure. Explain the intended semantic change first.

Live-capture fixture replay, which makes no model call:

~~~powershell
& .\venv\Scripts\python.exe -m evals.calibration.runner --fixtures-dir evals/calibration/fixtures_live
~~~

Interpret this command's result under WP3: historical model errors can correctly remain failures. A future live-capture command with --live is a different, billed operation and is not part of the default verification set.

After implementing WP3's proposed reviewed-only options, run the same hermetic replay with --oracle-reviews evals/calibration/oracle_reviews.json --reviewed-only. Its output must identify excluded/unresolved cases and the scored denominator. These options are proposed additions and do not exist at the inspected baseline.

### 15.2 Focused existing test groups

Use the environment's Python with -m pytest -q -p no:cacheprovider -m "not network", followed by relevant files. Proposed new files must be added after implementation.

| Area | Existing tests to retain |
|---|---|
| Trust | test_source_grounding_invariant.py, test_batch_wave_grounding.py, test_source_quote_schema.py, test_verified_contested.py, test_report_status.py, test_budget_exhaustion.py, test_verification_failed_status.py |
| Transport lifecycle | test_batch_fallback_handoff.py, test_batch_continuation_cap.py, test_batch_escalation.py, test_verification_stop_parity.py, test_verification_semaphore.py |
| Basis and reuse | test_location_aware_verification.py, test_cache_standards_fingerprint.py, test_verification_cache_serialization.py, test_verification_singleflight.py |
| Recovery | test_batch_resume.py, test_recover_in_progress.py, test_review_repair_hardening.py, test_program_pipeline.py, test_program_result_integrity.py |
| Redaction/traces | test_tracing.py, test_trace_recorder_teardown.py, test_diagnostics_concurrency.py, test_trace_retention.py |
| Accounting | test_pricing.py, test_diagnostics_cost_pricing.py, test_verification_token_telemetry.py, test_chunked_pass_engine.py |
| Research/compliance | test_requirements_research.py, test_research_concurrency.py, test_compliance_pass.py, test_datacenter_e2e.py |
| Request construction | test_token_budgets.py, test_prompt_serialization.py, test_realtime_review.py, test_capability_policy.py, test_strict_tool_use.py |
| Evaluation | test_labeled_specs.py, test_live_capture.py, test_eval_judge.py |
| Reports | test_html_report_exporter.py, test_html_gui_hook.py, test_edit_sidecar.py, test_evidence_panel.py, test_pinned_standards_editions.py |
| Compatibility | test_golden_domain_surfaces.py, test_golden_datacenter_surfaces.py, test_domain_routing_pins.py, test_gui_import_hermeticity.py, test_release_metadata.py |

All table filenames are under tests/. Do not run every group repeatedly after each small edit. Run relevant groups, then the full integrated suite at the defined milestones.

### 15.3 Browser setup and gate

After creating the test dependency file and required-tooling mechanism:

~~~powershell
python -m pip install -r requirements-browser-tests.txt
python -m playwright install --with-deps chromium
$env:SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS = '1'
python -m pytest -q -p no:cacheprovider -m "not network" tests/test_html_report_javascript.py tests/test_html_report_browser.py
~~~

The CI job must separately provide the chosen Node version and require the tooling. The Chromium dependency command is intended for the CI environment; adapt platform setup for Windows without machine-wide changes unrelated to this project.

### 15.4 Scenario acceptance matrix

| Scenario | Required evidence |
|---|---|
| Small CA run, no profile | Unchanged request/key/domain golden surfaces. |
| DC run with complete research | Basis present, correct policy, current-retrieval gates retained. |
| DC run with partial research | Missing dimensions and uncertain facts visible. |
| DC without historical profile | Generic assumptions/provenance disclosed, no invented adoption. |
| Concurrent modules with same claim | Isolation when basis differs; reuse when compatible. |
| Resume after environment/module changes | Saved basis preserved or explicit incompatibility, paid review retained. |
| Initial verification plus escalation | Correct per-model/per-transport spend exactly once. |
| Cache hit/shared follower | Zero current spend and honest reuse labeling. |
| Provider mixed TTL usage | Correct component pricing and aggregate conservation. |
| Legacy aggregate usage | Conservative estimate labeled unknown, not fabricated precision. |
| Secret in trace payload | No secret bytes in written artifacts, useful context preserved. |
| Hostile exported report | Inert content, valid CSP, no external request on open. |
| Live-fixture oracle conflict | Evidence-backed disposition; raw response unchanged. |
| Opt-in context caching | Identical text, actual shape counted, measured net saving before default activation. |
| Research replay | Complete input identity, original dates, visible refresh/replay behavior. |

## 16. Handoff prompts for implementation agents

These prompts assign bounded work. The coordinator should attach this plan, identify the current checkout/worktree, and supply the package's agreed contract and current baseline. Do not send every worker an instruction to implement the entire plan.

### Agent A: redaction

Implement WP1 only. Reconfirm the serialization gaps, create behavioral tests using synthetic secrets, and close the prompt/depth/object/message bypasses while preserving useful content and numeric telemetry. Coordinate diagnostics.py with the accounting owner. Do not modify existing private traces, API policy, or unrelated recorder lifecycle behavior. Return the package completion record from Section 4.3.

### Agent B: verification basis

Implement WP2 in its defined stages. First deliver the immutable basis/authority/persistence/cache contract and tests; then propagate it across both drivers, both rounds, retries, continuations, escalation, fallback, cache, and single-flight. Preserve California defaults and never promote historical research citations into fresh verification evidence. Record DC provenance differences and rollout status. Coordinate overlapping result fields with WP4 and domain-evaluation cases with WP3.

### Agent C: evaluation

Implement WP3. Inventory every live capture, adjudicate conflicting expectations against the actual finding and evidence, preserve raw captured outputs, and add a small review ledger/integrity check. Do not force historical model responses to pass. Keep unresolved cases and denominators visible. No billed recapture. Return the evidence for each changed oracle.

### Agent D: accounting and measurement

Implement WP4, then WP6 or coordinate the reader with its assigned owner. Preserve known-five-minute, known-one-hour, and unknown write usage through all current aggregation paths; keep legacy conservative estimates explicit. Prove no double counting and replay zeroing. Analyze actual available local records with coverage labels, and issue go/no-go decisions for WP7/WP8. Do not change cache policy while fixing accounting.

### Agent E: JavaScript gate

Implement WP5 only. Validate actual exported executable script bytes and CSP, then add a small offline Chromium initialization/interaction smoke. Keep browser tooling in an independent required CI job and out of the runtime. Missing CI tools must fail rather than skip. Do not rewrite the exporter to simplify testing.

### Agent F: independent integration review

Review the assembled diff against Sections 3, 14.3, and 15.4. Focus on context identity, resume semantics, current versus historical evidence, accounting conservation, secret serialization, and CI false greens. Give concrete reproduction cases for findings. Do not broaden the task into a general rewrite or reopen deferred cost policies without evidence.

## 17. Deferred decisions and stop conditions

A coding agent should make routine implementation decisions within the assigned package and record them. Missing evidence is not a reason to stop unrelated work.

| Condition | Continue with | Hold/defer |
|---|---|---|
| No complete historical usage records | Accounting fix, offline reader, coverage report | Claims of measured savings and default cache activation. |
| No authorized billed evaluation | Hermetic WP2 plumbing/provenance work and reviewed scenarios | New live run and unqualified claims of improved model reasoning. |
| Ambiguous oracle | Record unresolved status, complete other cases | Forced label or fabricated primary-source support. |
| Legacy pending run lacks context | Honest degraded/recovery path preserving paid work | Automatic re-research or invented historical location. |
| Unrelated dirty files | Isolated worktree or bounded changes preserving them | Reset, overwrite, or cleanup. |
| Relevant production bug found during new tests | Minimal scoped fix with evidence | Unrelated redesign. |
| Conditional cache saves too little | Document skip and complete required packages | Building it to satisfy a nominal checklist. |
| Required browser tooling absent | Set up declared CI/test dependencies within authorized environment | Treating the required CI job's skip as success. |

Billed API experiments require a concrete proposed dataset, models/settings, maximum cost, stopping rule, and comparison output before authorization is requested. The presence of an API key is not authorization to spend.

## 18. Final definition of done

The final handoff must include:

- [ ] WP0 baseline and decision register.
- [ ] WP1 serialization protection with synthetic-secret tests.
- [ ] WP2 immutable basis, authority policy, persistence, all-path reuse identity, and explicit rollout status.
- [ ] WP3 adjudication dispositions for every current live fixture, with immutable historical evidence.
- [ ] WP4 complete known/unknown TTL propagation and correct cost accounting.
- [ ] WP5 required exported-JS syntax and offline browser CI gate.
- [ ] WP6 actual-data cost report or an explicit evidence-coverage report showing why totals cannot yet be measured.
- [ ] WP7 go/no-go decision; implementation and measured activation evidence only if selected.
- [ ] WP8 go/no-go decision; complete invalidation/provenance policy only if selected.
- [ ] README/CLAUDE documentation matches the shipped behavior and defaults.
- [ ] Every intended California/DC/report/trace difference is classified and justified.
- [ ] Focused and integrated checks reported with actual results and skips.
- [ ] Independent review resolved or remaining findings explicitly listed.
- [ ] Package-level rollback instructions and saved-run compatibility notes.
- [ ] No unrequested source-document mutation, historical-artifact cleanup, live spending, release, or merge.

Do not describe a gated feature as enabled, an unrun test as passed, an estimate as a measured bill, or a retrieved URL as proof of legal applicability.

## 19. Primary references and maintenance notes

The source paths and symbol names above were inspected at the stated baseline. Line numbers change; use symbols and tests to relocate them. All proposed filenames are explicitly labeled proposed.

External API facts were checked against these official sources on 2026-09-08:

- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing): model prices, token modifiers, and tool charges.
- [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching): prefix rules, limits, TTLs, and cache minimums.
- [Tool use with prompt caching](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching): server-inserted five-minute writes and invalidation.
- [Batch processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing): transport semantics and best-effort cache reuse.

Refresh mutable API facts at implementation time. Do not add new supported models, change defaults, or retune safety factors simply because the documentation lists newer options.

Local design references: CLAUDE.md, TRUST_AUDIT.md, docs/standards_provenance.md, docs/hyperscale_datacenter_module_plan.md, docs/html_report_baseline_evidence.md, and evals/calibration/README.md. Treat their historical claims as evidence to verify, not as proof that every described behavior currently exists.
