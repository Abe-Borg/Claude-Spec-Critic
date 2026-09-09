# Spec Critic: implementation plan following independent review

**Revision:** 2 (2026-09-08). Revision 1 was authored the same day; see §0 for what changed and why.
**Repository:** Abe-Borg/Claude-Spec-Critic
**Inspected baseline:** `34df26b` (master merge), version 3.5.0. Revision 1 cited `627ba57`, which is an ancestor.
**Intended audience:** an implementing agent, and a reviewer checking the result.
**Status:** implementation handoff. Nothing in this document claims any change has been implemented or tested.

---

## 0. What changed in revision 2, and why

Revision 1 proposed ten work packages (WP0–WP9), seven of them "Required," plus a six-agent
coordination process. A review of that plan against the source found the *findings* accurate but the
*shape* wrong: two packages carry nearly all the real-world value, one was speculative infrastructure
built to decide packages that would probably be skipped, and one had a scope gap that undercut its own
goal. The review also produced three source-level corrections that change the analysis.

### 0.1 Corrections to revision 1

These are errors in revision 1 or in the review of it. An agent that read revision 1 must carry these
forward.

| # | Revision 1 said | Correct position | Anchor |
|---|---|---|---|
| C1 | The edition-authority fix is a verification-prompt change. | It spans three surfaces: the review prompt, the verifier prompt, **and** the deterministic stale-cycle detector. Fixing verification alone leaves conflicting guidance upstream and an unverified report surface. | `src/review/prompts.py:176`; `src/verification/verifier.py:646`; `src/input/preprocessor.py:405` |
| C2 | (Implied, and asserted in review) The DC reviewer is uniformly anchored on the module's generic pins. | **Wrong.** DC review category #2 already instructs deference to project adoption where the project context names it, and project context *does* reach review. The reviewer has the facts and a rule; verification has neither. The asymmetry is the defect, not reviewer anchoring. | `src/modules/datacenter_fire.py:212` |
| C3 | (Asserted in review) Adjudicating the twelve live fixtures is a prerequisite for trusting the WP2 change. | **Wrong.** The live-capture harness hardcodes `CALIFORNIA_2025`, so those fixtures cannot evaluate a data-center edition-authority change at all. Fixture adjudication is independent evaluation hygiene. A separate, small DC applicability set is required. | `evals/live_capture.py:403` |
| C4 | Redaction must preserve nonsecret text surrounding a matched credential in ordinary strings. | **Withdrawn as a requirement.** Substring replacement is strictly less fail-safe than the current whole-value replacement when a pattern is imperfect. Close the demonstrated bypasses using the existing conservative behavior. Preservation may be proposed later with false-positive evidence. | `src/tracing/redaction.py:33` |
| C5 | Single-flight isolation is a design task in the basis package. | It is a **consequence plus a regression test**. Single-flight is keyed on the verification cache key, so once the basis fingerprint enters `make_cache_key`, isolation follows. Do not build separate machinery. | `src/orchestration/pipeline.py:2650` |
| C6 | (Asserted in review) A wrong-edition verification failure has occurred in real runs. | Not established. Static inspection establishes a **mechanism**, not an occurrence. One targeted run settles it cheaply; do not describe it as observed until then. | — |

### 0.2 Structural changes

- **Reorganized around a five-step sequence** rather than ten packages. WP identifiers are retained in
  parentheses so revision 1's discussion remains traceable.
- **WP6 (offline cost reader) is no longer required.** Correct the accounting, read diagnostics from two
  or three ordinary runs, and build a reader only if those records prove genuinely hard to analyze.
  Deciding whether an optimization is worthwhile must not automatically become a software project.
- **WP5 is split.** The exported-JavaScript syntax check ships now; the headless-browser job is deferred
  until a concrete defect motivates its dependency and CI machinery.
- **WP1 is bounded** to three demonstrated bypasses, using existing conservative behavior.
- **WP0 and WP9 became short checklists** (§9) instead of packages.
- **The six-agent process is optional** (Appendix A), not a requirement of delivery.

### 0.3 Retained from revision 1, unchanged

These are load-bearing parts of the correctness fix and removing them makes the smaller implementation
unsafe: context-aware verification cache identity, saved-basis consistency through resume and recovery,
and the separation of historical research citations from fresh verification evidence. They are carried
into §5 intact.

---

## 1. Outcome and scope

Make the app's treatment of governing code editions consistent and honest across every surface that
asserts one; repair the evaluation labels those judgments will eventually be measured against; close
three demonstrated credential-serialization bypasses; and correct cache-write cost accounting. Preserve
the application's conservative treatment of unsupported findings and its ability to recover paid work.

### 1.1 Committed work, in sequence

| Step | Work | Was | Why in this position |
|---|---|---|---|
| 1 | Documentation corrections; adjudicate the twelve live fixtures; add a focused DC applicability set | WP9 (part), WP3 | Cheap, independent, and one of the corrections is a factual error in `CLAUDE.md` that is actively misleading about this area. |
| 2 | Edition authority across review, verification, and the deterministic detector, with cache and resume safeguards | WP2 | The correctness fix. Highest real-world consequence. |
| 3 | Exported-JavaScript syntax check in CI | WP5.1 | Near-zero cost; the extraction helper already exists. |
| 4 | Bounded redaction fixes and cache-accounting correction, as small independent changes | WP1, WP4 | Real but bounded; independently revertible. |
| 5 | Cost optimization, only if diagnostics from the user's ordinary runs justify it | WP6/7/8 | Gated on evidence that does not yet exist, and that this work may not initiate billed runs to create. |

Steps 3 and 4 are independent of steps 1–2 and may be done in any order or in parallel. Step 2 depends
on step 1 only for the documentation correction (so the implementing agent is not working from a
`CLAUDE.md` statement that is false).

### 1.2 Deferred, with gates

| Item | Gate to reconsider |
|---|---|
| Headless-browser smoke test (WP5.2) | A report-initialization defect that the syntax check would not have caught. |
| Offline cost/workload reader (WP6) | Diagnostics from two or three ordinary runs prove genuinely hard to analyze by inspection. |
| Review Project-Context prompt caching (WP7) | Measured repeated, sizable context within genuinely identical complete prefixes, and plausible positive net savings after write cost. |
| Cross-run research cache (WP8) | Measured repeat research inputs that survive conservative invalidation. |

A deferred item that is correctly skipped is a successful outcome. Do not implement one to lengthen a
checklist.

### 1.3 Explicit exclusions

Unchanged from revision 1. Do not include any of the following:

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

---

## 2. Verified evidence

Every row below was confirmed by reading the source at `34df26b`. Line numbers drift; relocate by symbol.

| Observation | Anchor |
|---|---|
| `src/verification/` contains **zero** references to `project_context`. Verification receives finding fields, a `user_location` dict, and a `jurisdiction_fingerprint` — never the researched adoption facts. | `src/orchestration/pipeline.py:2283` `location_inputs_for_submission` |
| The verifier prompt instructs the model to *"treat the pinned edition as authoritative for the cycle"* — unconditionally, for every module. | `src/verification/verifier.py:646` |
| The DC fire module pins eleven NFPA standards against a `dc-ibc-2024` national model-code basis; **every one is marked `UNVERIFIED`**. `edition_summary_lines()` renders editions only and drops provenance. | `src/modules/datacenter_fire.py:96–180`; `src/core/code_cycles.py:178` |
| The DC review prompt names those editions in prose with no provenance marker — but review category #2 *does* instruct deference to project adoption where the project context names it, and project context reaches review. | `src/review/prompts.py:176`; `src/modules/datacenter_fire.py:212` |
| The deterministic stale-cycle detector compares citations against `cycle.primary_code_year`, which is **2024** for the DC module. | `src/input/preprocessor.py:405` |
| `stale_code_cycle` is **not** in `_LOCAL_SKIP_KEYWORDS` (only `"invalid code cycle"` is), so a resulting finding does reach web verification — where it meets the authoritative-pin instruction above. | `src/verification/verification_prescreen.py:65` |
| Single-flight verification is keyed on the verification cache key. | `src/orchestration/pipeline.py:2650` |
| All twelve live fixtures carry auto-generated, explicitly unreviewed ground truth; **eleven of twelve** disagree with the captured verdict. Every `stale_*` fixture is labeled `CORRECTED` against a captured `CONFIRMED`. | `evals/calibration/fixtures_live/*.json` |
| The live-capture harness hardcodes `CALIFORNIA_2025`. | `evals/live_capture.py:403` |
| The canned eval baseline has nine fixtures and five seeded findings in its recall denominator. | `evals/baseline.json` |
| Research inventory is eighteen dimensions across four modules (4/4/5/5). | `src/modules/datacenter_*.py` |
| `src/research/requirements_research.py` imports from `src/verification/`, so a new module inside `src/verification/` must not import the research runner. | `src/research/requirements_research.py:69–76` |
| `prompt_ref` writes `prompts.jsonl` with no scrubbing; deep mode returns inline text. | `src/tracing/recorder.py:402` |
| Both recursive scrubbers return a raw `repr()` past depth six. | `src/tracing/redaction.py:47`; `src/orchestration/diagnostics.py:241` |
| `DiagnosticsReport.log` stores `message` unscrubbed; only `data` is scrubbed. | `src/orchestration/diagnostics.py:470` |
| `redaction_count` counts literal `<redacted>` occurrences in serialized output, so a pre-existing marker inflates it. | `src/orchestration/diagnostics.py:292` |
| Cache creation is always priced at the 1-hour multiplier; the code documents this as a deliberate conservative approximation. | `src/core/pricing.py:41` |
| Only the aggregate cache counters are extracted; the provider's TTL breakdown is discarded. | `src/core/api_config.py:1374` |
| CI is Python-only: no Node, no JavaScript parse, no browser. | `.github/workflows/tests.yml` |
| The HTML exporter test module already extracts the executable script body for CSP hashing. | `tests/test_html_report_exporter.py:843` `_EXEC_SCRIPT_RE` |

### 2.1 The edition-authority defect, precisely

State the defect this way; the revision-1 framing was imprecise and the imprecision matters for the fix.

Three surfaces assert a governing edition. They do not agree:

| Surface | Has adoption facts? | Rule applied |
|---|---|---|
| Deterministic pre-screen | no | compares against `cycle.primary_code_year` (2024) |
| Review prompt | **yes** (project context carries the research profile) | defer to project adoption where named |
| Verification prompt | **no** | *treat the pinned edition as authoritative* |

The stage holding the facts is overruled by the stage without them. The resulting failure **inverts**
relative to the naive expectation, and the inverted form is worse:

1. Research establishes that the project's jurisdiction is on an older I-code base.
2. The reviewer correctly defers and flags a spec citing the module's newer pinned edition as misaligned
   with local adoption. This is a *correct* finding.
3. Verification receives that finding with no adoption facts and an instruction that the pinned edition
   is authoritative. It can ground a **DISPUTED** verdict against the correct finding.
4. `DISPUTED` is the verdict that tells a reviewer to discard the finding.

A false positive is visible and gets human review. A silently discarded true positive does not. Any
acceptance test for step 2 must cover this direction, not only the "confidently confirms a wrong
edition" direction.

Separately, the deterministic detector fires with no model judgment at all: a spec correctly citing an
older I-code base gets a `stale_code_cycle` alert that renders in the report under the
`(deterministic check)` heading with no verification, and that also primes the review model through the
`<pre_detected>` block before category #2's deference can apply.

### 2.2 Qualifications to carry into implementation

1. Grounding establishes that a cited URL matches a source the verification tools retrieved. The pool
   includes both `web_search` and `web_fetch`. URL membership is not proof that the document supports
   the claim.
2. Historical research citations must not become fresh verification evidence merely by appearing in a
   prompt.
3. Research budgets have a documented field-measurement rationale in
   `docs/hyperscale_datacenter_module_plan.md`. Their present optimality is unknown; they are not
   unexplained arbitrary numbers.
4. Configured limits are not observed usage. Only routed active modules participate.
5. The canned eval cannot measure how a new prompt or model performs. It is a regression harness.
6. `docs/html_report_baseline_evidence.md` records earlier browser and syntax checks. The gap is
   recurring enforcement, not an absence of all historical testing.
7. A maximum output allowance is not a bill for that many output tokens.
8. A scripted replay with invented token counts validates arithmetic and propagation, not expenditure.

---

## 3. Non-negotiable invariants

### 3.1 Trust and paid-work preservation

- `CONFIRMED`, `CORRECTED`, and `DISPUTED` continue to require accepted citations under the existing
  grounding policy.
- No research flag, provenance label, or nonempty quote bypasses current-conversation retrieval checks.
- Every finding obtains exactly one terminal result. Successful escalation may replace an earlier
  result; that is not a prohibition on intermediate assignment.
- Preserve fallback/follow-up-wave mutual exclusion, continuation caps, escalation guards, and terminal
  failure surfacing.
- Cache hits, shared results, missing research, failed reviews, and incomplete coverage remain visible.
- A post-spend validation or telemetry problem must degrade visibly, never discard a paid run.
- Keep registry validation at least as strict as it is now.
- Preserve `rf-`, `cf-`, and `lc-` identities, action semantics, per-file edit fan-out, and the
  emit-only sidecar contract.

### 3.2 California and intentional differences

California's unaffected prompts, tool dictionaries, routing, finding identities, cache keys, report
wording, and golden fixtures must remain unchanged. None of this work justifies mass golden
regeneration.

Narrowly defined intentional differences that must not hide behind a blanket byte-identity claim:

- Scrubbed traces differ when their original content contained secrets or hit an unsafe serialization
  branch.
- Cost diagnostics differ when actual five-minute writes were previously priced as one-hour writes.
- The DC edition-authority surfaces differ — verifier basis, review-prompt provenance, deterministic
  stale-cycle behavior — including a profile-less DC run that formerly presented unverified generic
  pins as authoritative.
- An adjudicated evaluation oracle changes independently of unchanged historical model output.

Record affected surface, trigger, before/after behavior, and test for each. Do not apply the DC
edition-authority rewrite to California as a side effect.

### 3.3 Platform, persistence, and tests

- Windows remains the deployment target; keep paths, Unicode, newline handling, frozen runtime
  behavior, and OS credential storage intact.
- Preserve pending-run compatibility. New fields are optional and versioned as needed; old paid batches
  must remain recoverable.
- Do not globally invalidate or delete the verification cache to simplify a migration.
- Pure helpers must not import the GUI, start a recorder, create an API client, or touch the filesystem.
- Tests use synthetic data and scripted clients. Exclude network tests explicitly even when a real key
  is present in the environment.
- No private specifications, real keys, or unsanitized trace excerpts in committed fixtures or CI
  artifacts.

---

## 4. Step 1 — Documentation corrections and evaluation ground truth

**Scope:** documentation and evaluation labels. No production behavior changes.

### 4.1 Documentation corrections (was part of WP9)

`CLAUDE.md` states that Project Context "ships on **every** review, cross-check, AND verification call."
**This is false.** Project context reaches review, cross-check, compliance, and drawing impact. It does
not reach verification — `src/verification/` never references it. This statement is load-bearing for
anyone reasoning about the area step 2 changes, and it is probably why the gap survived. Correct it
first.

Correct or qualify, in `CLAUDE.md` and `README.md` as each applies:

- What verification actually receives (finding fields, `user_location`, `jurisdiction_fingerprint`)
  versus the full Project Context.
- The distinction between generic module pins, jurisdiction-adopted editions, newest published
  editions, and `UNVERIFIED` provenance.
- URL-retrieval grounding versus substantive support for a claim.
- Known-TTL billing versus conservative unknown-TTL estimates (after step 4).
- Live-fixture counts, what each evaluation mode establishes, and that live capture runs on the
  California cycle only.
- Feature gates, defaults, current activation status, and rollback behavior.

Update `requirements.txt` only if dependencies actually change; update `docs/standards_provenance.md`
only with verified factual changes or clarified provenance treatment. Do not remove an `UNVERIFIED`
label without authoritative evidence, and do not imply that a newer NFPA edition governs a project
merely because it is newer. Do not change version literals to deliver these fixes.

### 4.2 Adjudicate the twelve live fixtures (was WP3)

**This is evaluation hygiene, not a gate on step 2** (correction C3). The live-capture harness hardcodes
`CALIFORNIA_2025`, so these fixtures cannot evaluate a data-center edition-authority change.

Files: `evals/labeled_specs.py`, `evals/live_capture.py`, `evals/calibration/fixtures_live/`,
`evals/calibration/README.md`, and narrowly necessary loader/harness/scorer changes. Extend
`tests/test_labeled_specs.py`, `tests/test_live_capture.py`, `tests/test_eval_judge.py`; add proposed
`tests/test_calibration_oracles.py`.

1. Map each of the twelve captures to its labeled case explicitly. Avoid fragile name splitting.
2. For each case compare: the specification defect, the finding's assertion, the proposed remedy, the
   captured response, the retrieved and cited evidence, and the current expected label.
3. Separate three questions that the auto-labeler conflated: is the spec defective; is the finding
   correct; does the captured evidence permit a verified status. **`CORRECTED` means the finding needed
   correcting — not that the spec needs an edit.** The systematic `stale_*` mislabeling appears to be
   exactly this conflation, but **disagreement alone does not establish which side is right**;
   adjudicate each case on its evidence.
4. The three deterministic-detector captures (`placeholder_selection`, `template_todo_marker`,
   `duplicate_paragraph`) are labeled with a verdict where a `local_skip` classification is the
   expected outcome. Treat the category confusion as its own disposition, not as a verdict flip.
5. For code applicability, consult primary jurisdiction and standard sources for the fixture's actual
   cycle. Record support and date. Never use current application output as the oracle.
6. Preserve captured responses and original findings verbatim. Do not rewrite historical model evidence
   to match new expectations.
7. Put the review ledger outside fixture-discovery directories — proposed
   `evals/calibration/oracle_reviews.json` — recording fixture id, labeled-case id, a hash of the
   immutable evidence portion, review state, verdict/status, rationale, source references, and
   adjudication date.
8. Mark insufficiently supported cases unresolved. Show exclusions and denominators explicitly.
9. Add an integrity check over dispositions and evidence identity, hashing the immutable captured
   evidence and excluding the mutable oracle fields. It must not require historical model responses to
   agree with corrected ground truth.

Add a narrowly scoped reviewed-only path to the calibration runner — proposed
`--oracle-reviews PATH --reviewed-only`. It validates ledger dispositions and evidence hashes, scores
reviewed expectations, excludes unresolved cases with their ids/reasons and the denominator shown, and
preserves genuine model failures. Invocation without these options remains the historical diagnostic
replay. Missing or inconsistent adjudication metadata must fail validation, not silently exclude more
cases. Add tests proving the runner consumes the ledger; a ledger that scoring ignores does not
complete this work.

### 4.3 Add a focused DC applicability set

Because §4.2 cannot evaluate step 2, build a small reviewed set that can. It must cover:

- Differing local adoptions against the same module pins.
- Generic/`UNVERIFIED` pins with no research available.
- Partial research (some dimensions failed).
- Misleading or conflicting adoption claims.
- Contractual/owner requirements distinct from legal adoption.
- **The inversion case in §2.1**: a correct finding that defers to local adoption, checked by a verifier
  whose instruction would discard it.

Define this set and its judging criteria *before* reviewing any new model outcomes, and record
model/prompt/effort/judge settings for any future comparison. Hermetic tests establish transport and
safety behavior; they do not establish improved model reasoning. A billed comparison requires an
authorized budget with a dataset, maximum cost, and stopping rule agreed in advance.

**Acceptance:** the false `CLAUDE.md` statement is corrected; every existing live fixture has a
documented disposition with an independent rationale; captured model output is unchanged; genuine
historical model mistakes remain visible; no blanket baseline rewrite forces a green score; the DC
applicability set exists with stated judging criteria.

**Rollback:** revert a label together with its adjudication record; preserve captured evidence.

---

## 5. Step 2 — Edition authority across all three surfaces

**Independent review required.** This is the correctness fix.

### 5.1 Intended behavior

A DC run should treat local adoption, AHJ requirements, owner/insurer requirements, module reference
editions, and newest published editions as **different things with different authority**, consistently
across the deterministic pre-screen, the review prompt, and the verification prompt. Verification
should receive a bounded, immutable snapshot of the run's adoption/AHJ research plus source provenance
and limitations — not the entire free-text Project Context, not attached specifications, not the
drawing digest, and not a model-generated summary.

The snapshot must stay identical across both verification rounds, retries, continuations, escalation,
shared-work grouping, and resume.

### 5.2 The three surfaces

| Surface | Change |
|---|---|
| Deterministic pre-screen (`preprocessor.py:405`) | **Suppress the stale-cycle detector for a profile-enabled module unless an unambiguous structured adoption target exists** (defined in §5.2.1). A profile-less run always suppresses. California is untouched. |
| Review prompt (`prompts.py:176`) | Mark `UNVERIFIED` provenance explicitly where pins are rendered. Category #2's deference rule stays; the pins it may be weighed against must not present themselves as verified. Do not remove the deference instruction. |
| Verifier prompt (`verifier.py:646`) | Replace the unconditional authority clause on the affected DC path with the rules in §5.5, and supply the basis in §5.3. |

Because a `<pre_detected>` alert primes the review model, the pre-screen change must land with or before
the prompt changes; a suppressed-detector run and an authority-corrected prompt are consistent, but a
firing detector plus a corrected prompt sends contradictory signals into the same request.

#### 5.2.1 What qualifies as a structured adoption target

Revision 2 initially said "with a profile, compare against the researched adoption." That is not
implementable as written, and the gap is worth stating so nobody re-derives it:

- `ResearchItem` carries adoption only as free-form `requirement` and `code_reference` **strings**.
  §5.3 forbids inventing typed adoption or effective dates by guessing from prose. There is therefore
  no typed year to compare against unless one is constructed under a validated contract.
- The detector's contract is deliberately **single code family with one primary target year**. The
  data-center modules are I-code-only on purpose — `src/modules/datacenter_architecture.py:15` records
  that adding NBC/NFC/NECB to the abbreviation set "would create false stale/invalid alerts," because
  one shared year vocabulary cannot represent both families. The hyperscale program covers **USA and
  Canada**, so a Canadian project's governing adoption can never be expressed as a target for this
  detector.

A target therefore qualifies **only** when all of the following hold; otherwise suppress:

1. It names the same code family as the module's `code_abbreviations`.
2. It resolves to a year already in that module's `plausible_cycle_years`.
3. It comes from a grounded research item, not an ungrounded or process-advisory one.
4. No other item in the basis contradicts it.

Most runs will not qualify, and **suppression is the intended default**, not a degraded path. A
suppressed detector loses nothing that matters: the review prompt still receives the research text and
category #2 still instructs deference, so the edition question reaches a model that can weigh it —
which a regex comparing two integers cannot.

Constructing typed targets more broadly requires a validated code-family-to-edition contract with
Canadian coverage. That is **out of scope here**. Propose it separately, with its own detector-contract
change, if suppression proves too coarse in practice.

### 5.3 Contract

Proposed new pure module: `src/verification/governing_context.py`. **It must not import the research
runner** — `src/research/requirements_research.py` already imports from `src/verification/`, so the
reverse direction is a cycle. Accept serialized schema inputs or a dependency-free contract.

Existing `ResearchItem` fields are `item_id`, `dimension_id`, `topic`, `category`, `requirement`,
`authority`, `code_reference`, `source_urls`, `accepted_sources`, `grounded`, `confidence`,
`actionability`, `notes`. `RequirementsProfile` adds `research_date`, `project`, `dimension_statuses`.
Reuse these facts; do not invent typed adoption/effective dates by guessing from prose.

Proposed `VerificationBasis`:

| Field | Meaning |
|---|---|
| `schema_version` | Stored representation version. |
| `policy_version` | Authority, selection, and rendering semantics. |
| `mode` | Provenance-only or researched-context. California keeps its legacy path. |
| `module_id` / `cycle_label` | Owning module and cycle. |
| `project` | Snapshotted location and client, if available. |
| `research_date` | Original research date; never replaced during resume. |
| `research_state` | Available, partial, unavailable, or recovered-without-original-snapshot. |
| `items` | Immutable selected facts with qualifications and recorded provenance. |
| `dimension_statuses` | Completion/failure information relevant to applicability. |
| `module_basis` | `BaseCode` entries, standard editions/amendments/**provenance**, seismic anchors, and the versioned code-basis rendering inputs. |
| `omissions` | Scope/size omissions and explicit incompleteness information. |
| `fingerprint` | Derived canonical identity; never supplied by a caller. |

Field names are proposed; equivalent names are fine under one documented contract.

#### 5.3.0 Sub-chunk sequencing (revised during implementation)

Step 2 is delivered in three PRs, and the seam between the second and third moved once the code was
in front of us. The original plan put cache identity (§5.9) with propagation; it belongs with the
prompt change instead. **The cache key must change exactly when the question changes** — while nothing
renders the basis, two runs under different bases ask the *same* question, so splitting their identity
would invalidate data-center verdicts for no benefit and charge for the re-verification before the
benefit lands. So:

- **2a — contract** (§5.3/§5.4). Landed.
- **2b — propagation and persistence** (§5.6/§5.7, and the carry half of §5.8). Behaviorally inert:
  the basis is built, carried, saved and restored, and nothing reads it.
- **2c — the three surfaces** (§5.2/§5.5) **together with cache identity and single-flight** (§5.9),
  because that is the moment the question changes. This also satisfies §5.11's rule that the
  pre-screen and prompt changes must not activate in a state where they disagree.

#### 5.3.1 Implementation status — the contract layer has landed

`src/verification/governing_context.py` and `tests/test_verification_governing_context.py` implement
this section and the trust rules in §5.4 that belong to construction. **Nothing threads the basis
yet**: no prompt renders it, no cache key folds it in, and no pending-batch record persists it. Those
are §§5.5–5.9 and remain open. Verification therefore still cannot see the run's adoption research, so
the §2.1 inversion is not yet closed and the DC applicability scenarios still describe current
behavior.

Two deliberate deviations from the proposed field table, both allowed by its "equivalent names are
fine under one documented contract" clause:

- `module_id` and `cycle_label` are **nested inside `module_basis`** rather than sitting at the top
  level. They describe the module's assumptions and are consumed with the pins and their provenance;
  splitting them across two levels invited a snapshot whose identity fields and edition fields could
  be updated independently.
- `fingerprint` is a **method**, not a stored field. §5.3 requires it never be supplied by a caller,
  and a stored field is exactly what a caller can supply. `to_dict()` emits it for readers;
  `basis_from_dict()` ignores any stored value and recomputes.

Three additions beyond the proposal, each closing a way the snapshot could quietly stop meaning what
it claims:

- `BasisItem.consistency_note` records rule 6's re-derivation rather than performing it silently. A
  row that claimed grounding without a citation is carried as ungrounded **and says so**, so the
  correction is visible instead of looking like the research never claimed it.
- An `authority_class` (`adopted_law_or_ahj` / `contractual_or_owner` / `other`) is derived per item,
  making rule 4's separation a structural property rather than a rendering convention.
- An over-budget disclosure. Rule 8 drops whole items, which means the single highest-priority item is
  admitted even when it alone exceeds the budget. That is the right trade — truncating a qualification
  changes what it requires — but it can put the block over its bound, and an over-budget block the
  caller was told is bounded is the kind of quiet breach the budget exists to prevent, so it is stated
  in `omissions` and folded into the fingerprint.

The 4,000-token default remains **unvalidated against real saved profiles**, as §5.4 rule 8 requires.
It is a parameter (`token_budget`), not a baked-in constant, so validation can move it without a code
change.

**Two review findings, both accepted.** An automated review of the contract PR raised two defects that the module's own stated rules already forbade, and both were real:

- **`StandardEdition.note` / `ca_amended` were dropped.** `note` is an applicability condition, not a descriptor — `datacenter_electrical.py` pins NFPA 110 "where an EPSS or owner criterion invokes it" — so the snapshot rendered a bare `NFPA 110: 2022`, stating a requirement the module never declared, and a qualifier change could not reach the fingerprint. `StandardPin` now carries `edition_phrase` / `note` / `ca_amended`, renders the module's own phrase, and fingerprints all three. This is the same failure §5.4 rule 2 forbids for research items, committed against module pins instead.
- **`project` was a mutable dict inside a frozen dataclass.** `frozen=True` seals the attribute, not the mapping, and `project` feeds both the render and the fingerprint — so a post-construction write would let a request carry different context under an identity earned by the old context, defeating §5.6 before propagation even starts. `__post_init__` now copies and seals it behind a `MappingProxyType`.

**Test standard.** The contract tests were mutation-checked: twelve deliberate breakages of the
load-bearing behaviors (grounding re-derivation, contradiction preservation, authority separation,
whole-item dropping, policy-version refusal, fingerprint completeness including omissions, the
provenance-only disclosure, advisory exclusion, size accounting, historical-URL isolation, and
priority-over-confidence ordering) were each confirmed to fail the suite. A green suite that no
mutation can turn red is the failure mode of §4.3's original detector criteria and is not accepted
here. Six further mutations cover the two review findings above (qualifier dropped from the
snapshot / the render / the fingerprint, `ca_amended` dropped, the mapping left mutable, the mapping
aliased rather than copied). The last of those caught a test proving the wrong thing for the second
time in this chunk: the aliasing test ran through `build_verification_basis`, which copies before
constructing, so it would have passed even if `__post_init__` merely wrapped what it was handed —
the direct-construction and `replace()` paths are what actually exercise the copy.

**One test was loosened, with proof.** `tests/test_dc_applicability.py::test_verification_still_cannot_see_project_context`
matched on file text, so a module *explaining* why verification cannot see the project context tripped
it. It now matches on the AST — a reference, attribute, parameter, keyword argument, or the exact
string as a key — which prose cannot produce and every real access shape does. All five access shapes
were confirmed to still fire it, and a prose-only mention was confirmed not to.

#### 5.3.2 Implementation status — the provenance-only correction has landed

Step 2c delivers §5.11's **provenance-only correction** only. The **researched-context expansion**
landed separately in step 2d and is wired end-to-end but gated OFF by default — see §5.3.3.

Active now, on `project_profile_enabled` modules only:

- **Verifier prompt** — `verifier._reference_assumption_standards_lines` replaces "treat the pinned
  edition as authoritative for the cycle" with the §5.5 rules, including the explicit
  don't-invert guard (a newer publication is not automatically the governing edition).
- **Pre-screen** — stale-cycle detection suppressed (§5.2 / §5.2.1's intended default). It lands in
  the same change as the prompt, per §5.2's coupling requirement, and `TestSurfacesAgree` asserts the
  two are true for exactly the same modules so they cannot drift apart.
- **Base codes and seismic anchor** — `_base_code_assumption_lines` qualifies them on the same
  footing as the standards. `edition_summary_lines` covers `cycle.standards` only, so the first pass
  disclaimed the NFPA list while the line above still declared a current I-code/ASCE basis — which is
  what every core scenario turns on. Engine-owned so module wording cannot reopen it;
  `datacenter_fire`'s slots were corrected to match its siblings.
- **Report methodology note** (§5.10 item 15) — `_render_pinned_editions_note` framed the pins as
  "the current cycle" and told the reader that findings citing other editions were suspect. That
  inverts the truth for a project on an older adopted edition, and does so in the artifact a
  reviewer acts on rather than a prompt a model reads. Corrected for location-aware modules; the
  HTML exporter imports the same function, so both exporters agree by construction.
- **Review prompt** — the engine's standards clause marks provenance
  ("Module reference editions (assumptions, not confirmed adoptions for this project)"). The module's
  category #2 deference rule is untouched, as §5.2 requires; the DC templates already framed
  `{pinned_standards}` as fallbacks, so what was missing was only the provenance half.
- **Cache namespace** — `BASIS_POLICY_NAMESPACE = "bp1"`, appended for the affected modules. Verdicts
  under the superseded wording answer a different question and must not replay. Derived from the
  cycle inside `make_cache_key`, not threaded as a parameter, because a parameter would have to reach
  three call sites plus every `get`/`put` caller and one missed site replays a stale verdict silently.

**Known scoped-out gap.** §3.2 forbids applying the rewrite to California as a side effect, so
California keeps the authoritative wording even though **8 of its 15 pins are `UNVERIFIED`**. That is
the same defect class in smaller form. It is narrower there — Title 24 is one statewide jurisdiction
and the confirmed pins were checked against a real adoption table — but it is not zero, and it is
recorded here rather than left implicit.

**Golden blast radius**, as §3.2 requires it be recorded: **nine** DC goldens, **zero** California
ones — the verifier system and user prompts (both verdict-tool variants), the reviewer user messages
(plain and full), the cross-check and compliance system prompts, and the preprocessor alerts.

The cross-check and compliance prompts are **wider than the three surfaces §5.2 names**, and that is
deliberate rather than drift. `datacenter_fire` declared "Current code basis: IBC …, IFC …, ASCE …"
in four module slots, and those slots feed the review, cross-check, compliance and verifier prompts
alike. Correcting only the verifier's would leave cross-check still asserting a current basis while
the verifier called the same editions assumptions — a new inconsistency between stages, which is the
failure §5.2's coupling rule exists to prevent. The wording is corrected once, in the module, and
every surface that reads it follows.

**The applicability scenarios gained a second measurement.** `observed_detector_alerts` records the
*raw* detector and `observed_pipeline_alerts` records what a run actually surfaces. They now differ —
the detector fires, the pipeline emits nothing — and both are executed by the tests. Recording only
the raw output would have let a scenario describe an alert no run ever shows; recording only the
pipeline output would have made the pre-screen criteria vacuous the moment suppression landed.

Nine mutations across the three surfaces and the namespace, all confirmed to fail the suite.

#### 5.3.3 Implementation status — the researched-context expansion is wired and gated

Step 2d implements §5.5's remaining half: the basis's own facts now render into the verifier prompt,
and its fingerprint is in the verification cache key. Both are behind
`SPEC_CRITIC_GOVERNING_BASIS_CONTEXT`, **default OFF**, because §5.11 requires measuring the change
against the §4.3 applicability set — incorrect confirmations and incorrect disputes reported
separately — before it becomes the default. `evals/dc_applicability.py` still reports
`EVALUATION_PROTOCOL["status"] = NOT RUN`; a billed comparison needs its own authorization.

**Report a gated implementation as gated** (§5.11). With the flag unset, every prompt is byte-identical
to what step 2c shipped and every cache key is byte-identical to the pre-2d shape, so the feature is
inert rather than merely quiet. The gate does not touch the provenance-only correction: turning it off
cannot restore the superseded authoritative wording, which is asserted directly rather than assumed.

What landed:

- **Prompt** — `verifier.resolve_governing_basis` renders a `<governing_basis>` section at the end of
  `<code_basis>`. Placement is the argument: the module's own pins and their "these are assumptions"
  qualification are read *first*, so the researched facts extend the declared basis rather than
  displacing it. Rendering them above or outside that qualification would present research as the
  primary authority — the inverted-bias failure §5.5 names explicitly.
- **Cache identity** — `make_cache_key` gains a trailing `gb:<fingerprint>` segment, appended only
  when a basis was actually rendered. A profile-less or gated-off run produces the exact pre-existing
  key, so rollback does not orphan the cache.
- **One resolution, two consumers.** The prompt lines and the cache fingerprint come from a single
  `resolve_governing_basis` call returning both. §5.9 asks for a fingerprint of the snapshot *actually
  rendered*, and separate decisions in a prompt builder and a cache-key caller could disagree — the
  dangerous direction being a rendered block with no fingerprint, which lets a research-informed
  verdict replay for runs that never saw it. Deriving both from one return value makes that
  unrepresentable rather than merely tested for.
- **Single-flight isolation** falls out of the key, as §5.9's correction C5 predicted — no separate
  machinery. The regression test drives the real `verify_findings_for_run` grouping rather than
  `make_cache_key` directly, because a test of the key function alone would still pass if the flight
  grouping stopped consulting it.

**Propagation is enforced structurally, not by review.** §5.11 names missed propagation as the main
risk, and its realistic shape is mundane: someone adds a wave, a retry path, or a driver, threads the
two run-context parameters that were already there, and never learns about the third — leaving one
verification silently running without context. Two mechanisms address it. `governing_basis` travels
with `user_location` / `jurisdiction_fingerprint` through all thirteen verification signatures, and an
AST tripwire (`tests/test_governing_basis_context_gate.py::TestPropagationIsStructurallyEnforced`)
fails if any signature or call site carries one without the other, if a cache `get`/`put` is
jurisdiction-scoped but not basis-scoped, or if the basis leaks into a request/tool builder (it is a
system-prompt concern; `user_location` reaches those for the web_search tool). Separately,
`location_inputs_for_submission` became `verification_inputs_for_submission` returning a 3-tuple, so a
driver that is not updated fails to unpack loudly instead of verifying blind.

**Grounding is not weakened.** The block states that the research pass's own citations do not count as
sources retrieved in this conversation, and `historical_source_urls` makes that assertable rather than
merely instructed — a researched URL must never become a citation the verifier can lean on.

**The block body is escaped** (review finding, P2). `render_basis_text` returns content and delegates
prompt-boundary escaping to its caller; the first cut wrapped it in the delimiters raw. That is the one
place untrusted external text reaches a *system* prompt: the basis carries researched claims verbatim
by design, and research summarizes pages fetched from the open web, so a researched requirement
containing `</governing_basis>` closed the block and let what followed read as a sibling instruction
section — demonstrated as a forged `<verdict_rules>` instructing the verifier to confirm without
searching. Now wrapped through `prompt_serialization.wrap_document_block` under a canonical
`TAG_GOVERNING_BASIS`. Escaping rather than stripping: the hostile text still renders, inert, so it
stays visible in a trace. The assertions compare structural tag counts against a baseline prompt —
the real verifier prompt has its own `<verdict_rules>` section, so a membership check would read as a
leak when nothing leaked. Four further mutations (escaping removed, `escape_text` neutered, escaping
turned into stripping, wrong tag constant), all caught.

**Degradation is silent-safe, never silent-lossy.** An unparseable or policy-incompatible snapshot
yields no block *and* no fingerprint, so a malformed record degrades to today's behavior instead of
partitioning the cache under an identity nothing can reproduce. A verification prompt must never be
the thing that breaks a paid run.

Twelve mutations across the gate, renderer, prompt splice, cache identity, propagation, single-flight
grouping, and the driver accessor — all confirmed to fail the suite. Two of them initially passed and
are worth recording: nothing pinned that the block reached the *system prompt* (only that the renderer
produced lines), and the single-flight test asserted on `make_cache_key` rather than on the real
grouping. Both are the same mistake — testing a helper instead of the link that uses it.

**Golden blast radius: zero.** The gate is off by default, so no golden moved.

**Still open before default enablement:** the §4.3 evaluation (§5.11), and `DEFAULT_BASIS_TOKEN_BUDGET`
= 4,000 remains a proposed parameter, unvalidated against representative saved profiles (§5.4 rule 8).

### 5.4 Construction, selection, and trust rules

1. Build once after research and before review submission or worker startup. A profile-less DC run
   builds its provenance-only basis from module data during preparation, with no research call.
2. Preserve claims and qualifications verbatim. Normalize structure, not legal meaning.
3. Include `governing_code`, `local_amendment`, `referenced_standard`, and `ahj_requirement` items first.
4. Include relevant client/owner/insurer requirements under explicitly separate authority labels —
   compliance findings can concern them. Do not silently omit them and do not relabel them as statutes.
5. Preserve process-advisory classification and exclusions. A permit or schedule advisory must never
   become a mandatory specification edit.
6. Revalidate row consistency. `grounded=true` with no accepted citations is inconsistent and must not
   render as grounded. Historical `accepted_sources` remain historical provenance.
7. Preserve contradictory claims. Do not resolve them by maximum model confidence.
8. Use deterministic selection and ordering. Target a compact block; propose a 4,000-token limit and
   validate it against representative saved profiles. Omit whole items, preserve qualifications, and
   show omissions explicitly. Never truncate a legal qualification mid-sentence.
9. Fold every selection and omission choice into fingerprint semantics. If the size limit leaves the
   basis unusable for a finding's applicability, preserve uncertainty rather than claim complete
   research.
10. Escape every project/item field through the existing `prompt_serialization` helpers. Research text
    remains untrusted data.
11. **Keep research URLs out of the current verification conversation's accepted-retrieval pool.** The
    verifier must retrieve supporting material itself before a verified verdict.
12. Add no new report-status categories here. Use the existing incomplete/unverified/failure vocabulary
    plus clear basis diagnostics.

### 5.5 Prompt authority and placement

On the affected DC path, replace the unconditional pinned-authority clause. The prompt should:

- Treat local adoption and AHJ research as claims to investigate, with an as-of date and limitations.
- Treat module editions as **reference assumptions** unless applicability is established.
- Mark `UNVERIFIED` provenance explicitly.
- Keep contractual requirements distinct from legal adoption.
- Require evidence for applicability when sources conflict.
- Never substitute the newest publication for an adopted edition automatically.
- Preserve `UNVERIFIED` when authority, effective dates, exceptions, or applicability cannot be
  established.
- **Not invert into the opposite bias.** A verifier that reflexively favors a researched adoption claim
  over a pinned edition has the same defect facing the other way. Both are claims; evidence decides.

Use one central prompt-composition boundary. A wrapped, clearly labeled basis section may use the
existing cached system-prompt structure if research text is explicitly framed as data; a dedicated
user-data prefix is acceptable under the same contract, justified in the decision record. Do not add
message-caching changes here.

California's prompt assembly uses its existing branch unchanged. The profile-less DC path must disclose
generic/`UNVERIFIED` provenance rather than silently inheriting California's authoritative-cycle
assumptions.

### 5.6 Freeze and persist before spending

Carry the optional snapshot through:

- `PreparedBatchReview` and `prepare_batch_review` (`src/orchestration/pipeline.py`).
- `BatchSubmission` and `_build_batch_submission`.
- `PendingBatch.from_submission`, `_pending_batch_from_mapping`, `to_submission`
  (`src/orchestration/batch_resume.py`).
- `PendingProgramRun` child persistence and reconstruction.
- `reconstruct_batch_submission` and the GUI/headless resume paths.
- Any repair-metadata path that reconstructs the review submission.

Persist the **entire** snapshotted code basis: `BaseCode` rows, standard editions **and provenance**,
seismic anchors, and the code-basis template inputs or a supported versioned rendering contract. A cycle
label alone is insufficient. Validate module/project consistency.

For DC requests, resolve code-basis headers, the cycle/standards portion of the cache key, and the new
fingerprint from the **same** saved basis. Never mix current `code_basis_format_kwargs` or
`_standards_fingerprint` inputs with an older snapshot. An unsupported saved rendering policy takes the
explicit incompatibility path rather than silently rendering today's assumptions.

Use an additive optional field with its own version. Do not repurpose unrelated batch/program schema
versions or rewrite existing pending files.

### 5.7 Recovery policy

| Situation | Required behavior |
|---|---|
| New run, complete research | Build and save one basis; use it everywhere. |
| Partial research | Preserve successful facts plus failed-dimension and completeness warnings. |
| Ordinary resume | Use the saved basis, date, policy, and project. Ignore changed GUI fields. |
| Legacy saved run with structured research | Recover saved research facts, but do not infer historical module pins from a cycle label. Use a visibly reconstructed recovery basis for unknown assumptions, or explicit incompatibility. No new research call. |
| Saved snapshot from unsupported policy | Preserve paid results and surface incompatibility. Do not reinterpret silently. |
| Bare batch-ID recovery | Mark historical context unavailable. Never pretend the current GUI location was the original. |
| User supplies recovery context | Treat as explicit recovery input with a fresh identity and visible provenance. |
| Invalid/mismatched snapshot | Fail closed for the affected applicability claim or degrade visibly; retain paid review results. |

A snapshot error must never become a new reason to discard an otherwise recoverable paid review. Never
re-run research automatically during recovery.

### 5.8 Propagation checklist

Use the same immutable basis across:

- GUI rounds one and two (`src/gui/batch_controller.py`).
- Headless rounds one and two (`pipeline.run_batch_collection_headless`).
- `verify_findings_for_run` and `_execute_verification_attempts`.
- `start_batch_verification` and `collect_batch_verification_results`.
- `verify_finding` and `_run_verification_call`.
- Initial verification batch construction (`src/batch/batch.py`).
- `_build_retry_request` and `_build_continuation_request`.
- Batch wave context reconstruction.
- `_run_batch_escalation_wave`.
- The real-time fallback inside `collect_verification_batch_results`.
- Central `build_verification_request` (`verification_routing.py`).
- Program child collection, each module retaining its own basis.

Keep run context separate from `VerificationRoutingDecision` unless inspection establishes a compelling
reason otherwise. Do not change modes, models, budgets, permits, backoff, continuation caps, or
fallback thresholds.

### 5.9 Cache identity and single-flight

Extend `make_cache_key` with an optional derived governing-context fingerprint. Preserve the exact
existing California key when the new basis is absent. Use a versioned namespace or suffix for affected
DC questions; do not purge the cache.

Fingerprint the interpretation-relevant snapshot **actually rendered**: module/cycle assumptions,
provenance, selected facts and qualifications, location/client, research date, missing-dimension state,
omissions, and policy version. Deterministically equivalent inputs must agree; materially different
questions must not.

Keep the existing model-independent verified-claim cache policy. A prompt-policy change that alters the
question is different from a change of verifier-model provenance.

Propagate identity to every get/put and shared-work path: preparation, initial grouping, cache-fill
race checks, follower rechecks, takeover, real-time fallback, initial stores, and escalation stores.
Inspect `_stamp_grounded_cache_hits` explicitly.

**Single-flight isolation is a consequence, not a design task** (correction C5). `_verify_findings_singleflight`
is keyed on the verification cache key, so once the fingerprint is in that key, two findings under
different bases cannot share a flight — including the in-process sharing of clean ungrounded results.
Add the regression test; do not build separate isolation machinery.

### 5.10 Acceptance tests

Add proposed `tests/test_verification_governing_context.py`; extend the existing location, persistence,
cache, transport, and preprocessor tests.

1. An older project-adopted edition reaches all paths with no instruction to override it with the
   newest or module edition.
2. **The inversion case (§2.1):** a correct adoption-deferring finding is not discarded by a verifier
   applying module pins as authority.
3. Verified research provenance, unverified research, conflicting claims, generic pins, and failed
   dimensions remain distinct.
4. Historical research URLs alone cannot pass the current verification grounding gate.
5. Basis strings containing closing tags, instructions, quotes, Unicode, and control-like text remain
   data.
6. Changing a fact, qualification, provenance, client, policy, or relevant omission changes reuse
   identity; equivalent canonical inputs produce the same fingerprint.
7. Different bases isolate concurrent findings; equal bases retain sharing.
8. Resume after changing GUI location, current module data, or the current date retains the saved basis
   or emits an explicit policy incompatibility.
9. Legacy and bare-ID recovery are honest and never silently re-research; today's module pins cannot
   masquerade as missing historical assumptions.
10. Both rounds, repair/retry, continuation, escalation, and fallback carry the same basis.
11. Oversized context is bounded with visible omissions.
12. **Pre-screen:** a profile-less DC run emits no stale-cycle alert; a profile-present run whose
    research yields no qualifying target (§5.2.1) also emits none; a run with a qualifying same-family
    target compares against it; a Canadian profile never produces an I-code stale alert; California
    pre-screen output is byte-identical.
13. California prompts, tools, cache keys, and unaffected reports remain byte-identical.
14. Existing unsupported-verdict, disputed-grounding, `models_disagreed`, budget-exhaustion, and
    exactly-once regressions remain green.
15. DOCX, HTML, and sidecar consumers give a consistent, concise account of the basis used. No exporter
    rewrite.

### 5.11 Rollout

Deliver as contract/helper tests, then persistence and reuse plumbing, then the three prompt/detector
surfaces, then evaluation and visibility. Separate commits are fine; they must not activate in an
unsafe partial combination — in particular, the pre-screen and prompt changes must not disagree (§5.2).

Define two DC modes explicitly. The **provenance-only correction** — removing unconditional authority
from generic/`UNVERIFIED` pins, including in profile-less runs, and suppressing the stale-cycle detector
there — gets its own saved policy and cache namespace. The **researched-context expansion** stays
narrowly gated during evaluation. Turning the expansion gate off must not restore the old authoritative
wording or reuse verdicts under the superseded policy.

Snapshots record mode and policy; a flag change must never silently alter a resumed run. Documentation
must state which correction is active and which expansion is gated. Report a gated implementation as
gated.

Before default enablement, evaluate against the §4.3 DC applicability set. Measure incorrect
confirmations, incorrect disputes (§2.1), and unresolved results.

**Blast radius:** verification prompts, review prompt provenance, deterministic pre-screen output, DC
cache identity, saved runs, basis summaries.
**Main risks:** missed propagation, stale verdict reuse, fabricated authority, inverted bias toward
research claims, silent recovery drift, extra input cost.
**Rollback:** stop new-run activation while preserving readers for saved snapshots; maintain namespace
isolation. Never silently downgrade an in-flight basis to the old policy.

---

## 6. Step 3 — Exported-JavaScript syntax check

**Scope:** test the bytes the existing exporter ships. Do not rewrite its embedded JavaScript.

Add proposed `tests/test_html_report_javascript.py`.

1. Generate representative reports through production `write_html_report`: single-module, program,
   hostile-content, and chat-disabled variants.
2. Read the generated bytes directly; decode UTF-8 without universal-newline conversion.
3. Extract every executable inline script, excluding `application/json` data blocks. The existing
   `_EXEC_SCRIPT_RE` in `tests/test_html_report_exporter.py:843` already does this — reuse it rather
   than writing a second extractor. Assert expected script counts explicitly so the test cannot silently
   check only the first match.
4. Run `node --check` on exactly those bodies, via stdin or byte-preserving temporary files.
5. Check return code, stderr, and timeout.
6. Recompute the CSP hash over the same script bytes. Preserve the existing exporter security tests.
7. Include a deliberately malformed synthetic script proving the rejection path works.
8. Keep fixtures compact and deterministic. Do not commit generated reports to test syntax.

**CI:** add Node to the existing hermetic job (`ubuntu-latest` provides it) and a
`SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1` setting that turns a missing Node into a failure there. Local
`pytest` may skip when Node is absent; CI must not. Keep test tooling out of `requirements.txt` and the
frozen Windows application.

**Deferred (WP5.2):** the headless-Chromium initialization smoke, its `requirements-browser-tests.txt`,
and a separate required CI job. Revisit on a report-initialization defect the syntax check would miss.

**Acceptance:** malformed shipped JavaScript or a missing required tool fails CI; production report
bytes are unchanged.

---

## 7. Step 4 — Bounded redaction and cache-accounting corrections

Two independent commits. Neither changes request policy, prompts, routing, or verdicts.

### 7.1 Redaction (was WP1, bounded)

Close exactly the three demonstrated bypasses, **using the existing conservative whole-value
replacement** (correction C4):

| Bypass | Anchor | Fix |
|---|---|---|
| Prompt content is written to `prompts.jsonl` with no scrubbing; deep mode returns it inline. | `src/tracing/recorder.py:402` | Scrub before default-mode storage and before deep-mode inline return. Compute the reference from the content actually stored, so a secret-free prompt keeps its existing hash. |
| Both recursive scrubbers emit a raw `repr()` past depth six. | `src/tracing/redaction.py:47`; `src/orchestration/diagnostics.py:241` | Replace with an explicit safe marker. Retain siblings and numeric telemetry. Scalar strings must still be scrubbed. |
| `DiagnosticsReport.log` stores `message` unscrubbed. | `src/orchestration/diagnostics.py:470` | Scrub the message text, not only structured `data`. |

Out of scope: substring preservation in ordinary strings (C4); a general safe-serialization rewrite;
new pattern families; cycle-detection redesign; `capture_hooks` changes. If `redaction_count` is
touched at all, count actual replacements rather than literal `<redacted>` occurrences
(`src/orchestration/diagnostics.py:292`) — otherwise leave it.

Preserve byte caps, locks, queue behavior, recorder teardown, and numeric counters. Do not rewrite
existing traces or claim historical files are now safe.

**Tests** (proposed `tests/test_secret_redaction.py`, plus `tests/test_tracing.py`,
`tests/test_diagnostics_concurrency.py`): a synthetic secret at multiple nesting levels never reaches
serialization; a deep branch past the limit yields a safe marker with siblings retained; both capture
levels write no synthetic secret to `run.json`, `prompts.jsonl`, `spans.jsonl`, `events.jsonl`, or
`findings.jsonl` under the real recorder lifecycle; a repeated safe prompt keeps its deduplication; a
diagnostics message containing a key is scrubbed while other diagnostics survive; concurrent recording
and summary reads still produce valid JSONL. Temporary directories and fabricated credentials only.

**Rollback:** one independent commit. A rollback reopens the gap; document it rather than accepting it
silently.

### 7.2 Cache-write accounting (was WP4)

Accounting only. The provider contract was reconfirmed against current documentation: **five-minute
writes are 1.25× the base input rate, one-hour writes 2×, reads 0.1×**, and
`usage.cache_creation` exposes `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`. The
application declares a 1-hour TTL on every breakpoint it sets, so five-minute writes arise from
**server-inserted breakpoints after server-tool results** — which concentrates them in verification,
the highest-volume phase. That is why the aggregate-only extraction at
`src/core/api_config.py:1374` matters.

**Counters.** Retain aggregate `cache_creation_input_tokens` / `cache_read_input_tokens`. Add:

```text
cache_creation_5m_input_tokens
cache_creation_1h_input_tokens
cache_creation_unknown_input_tokens
cache_creation_breakdown_status
```

Missing detail is **unknown**, never zero. Maintain `known_5m + known_1h + unknown = aggregate`. Never
add the aggregate and its components together as separate spend. Handle absent, partial, negative,
malformed, and contradictory fields defensively: preserve trustworthy aggregate usage, classify
unreliable detail as unknown, expose an accounting warning. Telemetry normalization must never discard
paid results.

**Pricing.** Known 5m at 1.25×, known 1h at 2×, reads at 0.1×. Token costs keep the batch factor;
web-search charges do not. Keep the existing conservative 2× estimate for unknown-TTL writes with an
explicit assumption and unknown-token count, so legacy numbers stay stable while their uncertainty
becomes visible. Keep existing `estimated_cost_usd` keys and add detail alongside. Unknown models stay
unpriced. Missing usage is unavailable, not free.

**Propagation.** Inspect and update every boundary already carrying aggregate cache usage:
`api_config` extraction (SDK objects and dict-shaped synthetic inputs); `ReviewResult` and review
response parsing including refusals and incomplete outcomes; realtime review diagnostics and repair
outcomes; `_cache_token_usage`, `_USAGE_COUNTER_KEYS`, `_usage_counters`, `_merge_usage_counters`,
`_conversation_view`, `_ConversationEvidence`; `VerificationResult` fields, terminal
unverified/error construction, and `call_usage` entries; batch `prior_usage`, follow-up waves,
escalation success and failure, synchronous fallback; research `_DimensionOutcome` and dimension
diagnostics; cross-check and compliance result handling; drawing digest and impact; chunked-pass and
program aggregation; the GUI-to-diagnostics handoff; `_CALL_USAGE_COUNTERS`, `_billable_calls`, phase
totals, summaries, and report presentation; shared-result clones and verification-cache runtime-field
exclusions.

`call_usage` remains authoritative when present — do not price its entries *and* the kept-result flat
counters. Durable cache hits and shared followers contribute zero current-run spend; historical
provenance may remain separately visible. No cache schema invalidation is required to retain runtime
accounting fields; exclude new spend counters from durable verdict projection.

**Numerical acceptance** (Sonnet 5 input at USD 2 per million tokens; cache-write cost only):

| Input | Expected |
|---|---|
| 1,000 known 5-minute tokens | USD 0.0025 |
| 1,000 known 1-hour tokens | USD 0.004 |
| 1,000 unknown legacy tokens | USD 0.004, explicitly conservative |
| 1,000 5m + 2,000 1h + 3,000 unknown | USD 0.0225; aggregate 6,000 |
| Same mix, all batch transport | USD 0.01125 |

Do not count 6,000 additional aggregate tokens.

**Tests:** extend `test_pricing.py`, `test_diagnostics_cost_pricing.py`,
`test_verification_token_telemetry.py`, `test_batch_escalation.py`, `test_chunked_pass_engine.py`,
`test_requirements_research.py`, the drawing tests, `test_verification_cache_serialization.py`, and
`test_verification_singleflight.py`. Cover known/mixed/unknown TTLs, explicit zero versus missing,
malformed detail, batch discount, searches, unknown model, continued conversations, failed escalations,
replay zeroing, and concurrent aggregation.

**Acceptance:** real provider TTL information survives to totals; legacy uncertainty is visible; no
double counting; no request, prompt, routing, or verdict behavior changes. Corrected diagnostic amounts
are the only intended report-data differences.

---

## 8. Step 5 — Cost optimization, gated

Do not build a measurement apparatus to decide whether to build an optimization.

After step 4, read the diagnostics from ordinary runs **the user performs in the course of their normal
work**. Do not initiate application runs to generate measurement data: that is a billed API experiment
under §1.3 and §11, and the presence of an API key is not authorization to spend. If no suitable
diagnostics have accumulated yet, wait for them or request an authorized budget with a dataset, cost
cap, and stopping rule — do not manufacture a baseline. If the attribution in those records is legible,
decide WP7/WP8 from it and record the decision. Build a dedicated offline reader (WP6) **only** if the
records prove genuinely hard to analyze — and then extend existing tracing/reporting tooling rather than
creating a second reporting framework.

If a reader is built, it must not initialize `TraceRecorder`, trigger retention or pruning, extract
specifications, resolve live URLs, or construct an API client; it must take explicit paths and never
traverse unrelated home directories. Establish source precedence so one run represented by diagnostics,
a trace directory, and an HTML report is counted once. Separate HTTP-response counts from aggregated
conversation counts. Do not infer billable searches from URL counts, equate cache-token ratios with
whole-run savings, or present historical usage repriced at current rates as a reconstructed invoice.
Keep incomplete and failed paid runs visible as a separate cohort. A handful of ordinary runs is an
initial signal, not a characterization of every workload.

**Reference break-even arithmetic** (idealized; concurrent batch execution produces additional writes):

```text
One-hour cache, one write and r reads:
  cached units   = 2 + 0.1r
  uncached units = 1 + r
  saving requires r > 1.111..., i.e. at least two reads (three requests).

Fourteen requests, one prefix, one write + thirteen reads:
  14 / 3.3 = approximately 4.24x cheaper on that portion.

Fourteen requests across four equal-sized prefixes, one write each + ten reads:
  14 ordinary units versus 9 cached units, approximately 35.7 percent saving on that portion.
```

**Gates.** WP7 requires measured repeated, sizable context inside genuinely identical *complete*
prefixes — model, tools, parameters, system, thinking/effort, module/cycle, introductory text,
element-ID hints, and effective context all matched — plus plausible positive net savings after write
cost. WP8 requires genuine identical-input repetition that survives conservative invalidation
(module/dimension identity, location, client, code basis and provenance assumptions, rendered corpus
signals, prompt or protocol revision, output schema, model, and material tool/policy configuration).

Two API constraints to verify at implementation time if WP7 proceeds: minimum cacheable prefixes are
**model-dependent and not monotonic across generations**, and where per-block TTLs differ, longer-lived
entries must appear before shorter-lived ones. A maximum of four breakpoints applies per request.

If the evidence cannot support a decision, record that result and arrange passive telemetry on the next
otherwise-planned run. Do not manufacture a measured baseline from scripted token counts.

---

## 9. Baseline and completion checklists

These replace WP0 and WP9. They are checklists, not packages.

### 9.1 Before starting (was WP0)

- [ ] Record git status, branch, commit, Python version, dependency consistency (`pip check`), and
      whether Node is available. Preserve existing user changes.
- [ ] Run the hermetic suite and record actual counts, elapsed time, and skipped categories. Do not
      repeat an inherited test-count claim without executing it.
- [ ] Run the canned eval and calibration runners; run the live-fixture replay diagnostically and
      distinguish historical model failures from infrastructure errors.
- [ ] Inventory existing coverage and actual source signatures before adding test files or helpers.
- [ ] Record decisions for: the `VerificationBasis` schema and policy version; basis placement,
      rendering, size limit, and cache fingerprint; legacy and bare-ID recovery behavior; cache usage
      counter names and unknown-data semantics; feature gates and default-enablement criteria.

A baseline report separates pre-existing failures from regressions. No production behavior changes here.

### 9.2 Before declaring done (was WP9)

- [ ] Review the final diff for unexpected files, test-only dependencies in production, generated
      artifacts, and private data.
- [ ] Confirm no newly enabled path can retrieve a verdict under a different basis key.
- [ ] Confirm current retrieval evidence stays separate from historical research citations.
- [ ] Confirm both GUI and headless drivers carry the same basis and accounting.
- [ ] Confirm every new usage field survives aggregation and is zeroed on replay.
- [ ] Confirm CI cannot pass by skipping an absent required tool.
- [ ] Confirm oracle changes preserved historical responses.
- [ ] Review each intentional golden/report/trace difference individually against §3.2.
- [ ] Record deferred items as skipped-with-evidence or deferred-with-missing-evidence.
- [ ] Record step 2 rollout status honestly: tests of request assembly do not establish domain
      reasoning accuracy.

**Adversarial scenarios a reviewer should attempt:** a researched-but-not-retrieved URL producing a
verified verdict; two equal claims under different bases sharing a verdict or in-flight result; a
resumed paid batch picking up today's location, date, standards, or feature flag; unverified generic
pins becoming authoritative through another prompt surface; a correct adoption-deferring finding
discarded as `DISPUTED` (§2.1); a stale-cycle alert firing on a jurisdiction-correct citation; a
redacted prompt leaking through a hash-associated alternate record or a depth-limit fallback; one paid
continuation or escalation counted twice; a durable cache hit acquiring new-run usage; a historical
model error erased through an oracle edit; invalid exported JavaScript passing CI. A failed attempt is
evidence about that path, not proof that all paths are safe. Record what was tested.

---

## 10. Verification commands

Commands are for later execution and are repository-relative. Resolve the interpreter for the actual
environment; the inspected Windows environment has `venv/Scripts/python.exe`, which another agent must
not assume exists.

```powershell
& .\venv\Scripts\python.exe -m pip check
& .\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not network"
& .\venv\Scripts\python.exe -m evals.runner
& .\venv\Scripts\python.exe -m evals.calibration.runner
& .\venv\Scripts\python.exe packaging/windows/check_release_version.py --tag v3.5.0
```

The explicit marker exclusion matters: `tests/conftest.py` can allow network-marked tests when a real
key exists. Do not print that key or rely on its absence. Do not use `--write-baseline` to clear a
failure; explain the intended semantic change first. Do not change version literals to satisfy the
release-check example.

Live-fixture replay (no model call):

```powershell
& .\venv\Scripts\python.exe -m evals.calibration.runner --fixtures-dir evals/calibration/fixtures_live
```

Interpret its result under §4.2: historical model errors can correctly remain failures. Malformed
fixtures or inconsistent adjudication metadata are implementation failures. After §4.2's reviewed-only
options exist, the same replay with `--oracle-reviews evals/calibration/oracle_reviews.json
--reviewed-only` must identify excluded/unresolved cases and the scored denominator.

### 10.1 Focused test groups

Run with `-m pytest -q -p no:cacheprovider -m "not network"` plus the relevant files under `tests/`.
Run relevant groups after each change, and the full suite at step boundaries — not every group after
every edit.

| Area | Tests |
|---|---|
| Trust | `test_source_grounding_invariant.py`, `test_batch_wave_grounding.py`, `test_source_quote_schema.py`, `test_verified_contested.py`, `test_report_status.py`, `test_budget_exhaustion.py`, `test_verification_failed_status.py` |
| Transport lifecycle | `test_batch_fallback_handoff.py`, `test_batch_continuation_cap.py`, `test_batch_escalation.py`, `test_verification_stop_parity.py`, `test_verification_semaphore.py` |
| Basis and reuse | `test_location_aware_verification.py`, `test_cache_standards_fingerprint.py`, `test_verification_cache_serialization.py`, `test_verification_singleflight.py` |
| Recovery | `test_batch_resume.py`, `test_recover_in_progress.py`, `test_review_repair_hardening.py`, `test_program_pipeline.py`, `test_program_result_integrity.py` |
| Pre-screen | `test_preprocessor_policy.py`, `test_preprocessor_synthetic_paragraphs.py`, `test_asce7_stale_editions.py`, `test_anchor_and_polity.py` |
| Redaction/traces | `test_tracing.py`, `test_trace_recorder_teardown.py`, `test_diagnostics_concurrency.py`, `test_trace_retention.py` |
| Accounting | `test_pricing.py`, `test_diagnostics_cost_pricing.py`, `test_verification_token_telemetry.py`, `test_chunked_pass_engine.py` |
| Research/compliance | `test_requirements_research.py`, `test_research_concurrency.py`, `test_compliance_pass.py`, `test_datacenter_e2e.py` |
| Request construction | `test_token_budgets.py`, `test_prompt_serialization.py`, `test_realtime_review.py`, `test_capability_policy.py`, `test_strict_tool_use.py` |
| Evaluation | `test_labeled_specs.py`, `test_live_capture.py`, `test_eval_judge.py` |
| Reports | `test_html_report_exporter.py`, `test_html_gui_hook.py`, `test_edit_sidecar.py`, `test_evidence_panel.py`, `test_pinned_standards_editions.py` |
| Compatibility | `test_golden_domain_surfaces.py`, `test_golden_datacenter_surfaces.py`, `test_domain_routing_pins.py`, `test_gui_import_hermeticity.py`, `test_release_metadata.py` |

### 10.2 Scenario acceptance matrix

| Scenario | Required evidence |
|---|---|
| Small CA run, no profile | Unchanged request, key, pre-screen, and golden surfaces. |
| DC run with complete research | Basis present, correct policy, current-retrieval gates retained. |
| DC run with partial research | Missing dimensions and uncertain facts visible. |
| DC without historical profile | Generic provenance disclosed, no stale-cycle alert, no invented adoption. |
| Correct adoption-deferring finding | Not discarded by the verifier (§2.1 inversion). |
| Concurrent modules, same claim | Isolation when basis differs; reuse when compatible. |
| Resume after environment change | Saved basis preserved or explicit incompatibility; paid review retained. |
| Initial verification plus escalation | Correct per-model, per-transport spend counted exactly once. |
| Cache hit / shared follower | Zero current spend, honest reuse labeling. |
| Provider mixed TTL usage | Correct component pricing, aggregate conserved. |
| Legacy aggregate usage | Conservative estimate labeled unknown, not fabricated precision. |
| Secret in trace payload | No secret bytes in written artifacts; useful context preserved. |
| Hostile exported report | Valid JavaScript, valid CSP, inert content. |
| Live-fixture oracle conflict | Evidence-backed disposition; raw response unchanged. |

---

## 11. Deferred decisions and stop conditions

Make routine implementation decisions within the assigned step and record them. Missing evidence is not
a reason to stop unrelated work.

| Condition | Continue with | Hold |
|---|---|---|
| No complete historical usage records | Accounting fix; inspect ordinary-run diagnostics | Claims of measured savings; default cache activation |
| No authorized billed evaluation | Hermetic step-2 plumbing, provenance work, reviewed scenarios | New live runs; unqualified claims of improved reasoning |
| Ambiguous oracle | Record unresolved status; complete other cases | Forced label; fabricated primary-source support |
| Legacy pending run lacks context | Honest degraded recovery preserving paid work | Automatic re-research; invented historical location |
| Unrelated dirty files | Isolated worktree or bounded changes preserving them | Reset, overwrite, cleanup |
| Production bug found during new tests | Minimal scoped fix with evidence | Unrelated redesign |
| Deferred optimization saves too little | Document the skip; complete required steps | Building it to satisfy a checklist |

Billed API experiments require a proposed dataset, models and settings, maximum cost, stopping rule,
and comparison output before authorization is requested. **The presence of an API key is not
authorization to spend.**

## 12. Definition of done

- [ ] `CLAUDE.md`'s Project-Context/verification statement corrected; other documentation matches
      shipped behavior and defaults.
- [ ] Every current live fixture has an evidence-backed disposition; historical evidence immutable.
- [ ] A DC applicability set exists with stated judging criteria, including the §2.1 inversion case.
- [ ] Edition authority is consistent across pre-screen, review prompt, and verifier prompt, with basis
      persistence, recovery, and cache identity; rollout status stated honestly.
- [ ] Exported-JavaScript syntax check enforced in CI.
- [ ] Three redaction bypasses closed with synthetic-secret tests; conservative behavior retained.
- [ ] Known/unknown TTL propagation complete; cache-write accounting corrected; no double counting.
- [ ] Cost-optimization decision recorded from ordinary-run diagnostics, or explicitly deferred.
- [ ] Every intentional California/DC/report/trace difference classified and justified.
- [ ] Focused and integrated checks reported with actual results and skips.
- [ ] Step-level rollback instructions and saved-run compatibility notes.
- [ ] No unrequested source-document mutation, historical-artifact cleanup, live spending, release, or
      merge.

Do not describe a gated feature as enabled, an unrun test as passed, an estimate as a measured bill, a
retrieved URL as proof of legal applicability, or a statically inferred mechanism as an observed
failure.

---

## 13. References and maintenance notes

Symbol names and line anchors were inspected at `34df26b`. Line numbers drift; relocate by symbol and
test.

External API facts were reconfirmed on 2026-09-08 against official documentation: model pricing and
token modifiers; prompt-cache prefix rules, breakpoint limits, TTL multipliers (5-minute 1.25×,
1-hour 2×, reads 0.1×), and model-dependent cache minimums; server-tool-inserted five-minute cache
writes; and batch transport semantics. Refresh mutable API facts at implementation time. Do not add
supported models, change defaults, or retune safety factors merely because documentation lists newer
options.

Local design references: `CLAUDE.md`, `TRUST_AUDIT.md`, `docs/standards_provenance.md`,
`docs/hyperscale_datacenter_module_plan.md`, `docs/html_report_baseline_evidence.md`, and
`evals/calibration/README.md`. Treat their historical claims as evidence to verify — this revision
exists partly because one such claim was wrong.

---

## Appendix A: optional agent handoff prompts

Delivery does not require a multi-agent process. These prompts are available if the work is split;
attach this plan, identify the checkout, and supply the current baseline. Each returns: start/end
commits or a clear uncommitted diff description; files changed and why; observable behavior before and
after; new public interfaces and persistence fields; tests actually run with outcomes and explicit
skips; remaining uncertainty and plan deviations; a rollback procedure; and a short reviewer checklist
for the highest-risk path.

**Documentation and evaluation (step 1).** Correct the misleading documentation. Inventory every live
capture, adjudicate conflicting expectations against the actual finding and evidence, preserve raw
captured outputs, and add the review ledger and integrity check. Do not force historical model
responses to pass. Keep unresolved cases and denominators visible. Build the DC applicability set with
its judging criteria. No billed recapture.

**Edition authority (step 2).** Deliver the immutable basis, authority policy, persistence, and cache
contract with tests; then the three surfaces — pre-screen, review prompt, verifier prompt — and
propagation across both drivers, both rounds, retries, continuations, escalation, fallback, cache, and
single-flight. Preserve California defaults byte-for-byte. Never promote historical research citations
into fresh verification evidence, and do not invert into reflexive deference to research claims. Record
DC provenance differences and rollout status.

**JavaScript gate (step 3).** Validate the actual exported executable script bytes and CSP using the
existing extraction helper. Wire Node into the hermetic CI job; a missing required tool must fail, not
skip. Do not rewrite the exporter to simplify testing, and do not add browser tooling.

**Redaction and accounting (step 4).** Close the three demonstrated serialization bypasses using
existing conservative behavior; do not add substring preservation. Separately, preserve known-5m,
known-1h, and unknown write usage through every aggregation path, keeping legacy conservative estimates
explicit. Prove no double counting and replay zeroing. Change no cache policy while fixing accounting.

**Independent review.** Review the assembled diff against §3, §9.2, and §10.2. Focus on basis identity,
resume semantics, current versus historical evidence, accounting conservation, secret serialization,
and CI false greens. Give concrete reproduction cases. Do not broaden into a general rewrite or reopen
deferred cost policies without evidence.
