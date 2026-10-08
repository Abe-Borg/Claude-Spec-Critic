# EX-03 — Model selection, effort, and confidence

| | |
|---|---|
| **Chunk** | S22 (plan Part 4 §24) |
| **Date** | 2026-09-29 |
| **Base** | master `44b7d70` (the merge of #400, Spec Critic 3.10.0, Anthropic SDK 1.7.0) |
| **Live evaluation** | Not authorized. The owner chose "offline only" at session start, and no API key was in the session's environment. |
| **Spend** | $0.00. No model request, count request, or batch was sent. |
| **Decision** | **Not evaluated.** Three single-change arms are built and runnable. Two new switches default off (`SPEC_CRITIC_REVIEW_EFFORT`, `SPEC_CRITIC_REVIEW_SCOPE_WORDING`); the third arm uses the existing `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL`. No default changed. |

> **Baseline changed after this record (2026-09-29, owner decision, no measurement).** Opus 5.5 and
> Sonnet 5.5 were added to the whitelist and made the defaults, and every Opus request is now held to
> effort `medium` (`api_config.OPUS_EFFORT_CEILING`). So the baseline these arms compare against is
> Opus 5.5 for review and escalation, Sonnet 5.5 for the initial verifier, and a review at `medium`,
> not Opus 5 / Sonnet 5 / `high` as written below. What that does to each arm:
>
> - **Escalation model.** Opus 4.8 is still one change at the request (model, and web fetch, which
>   stays gated off on Opus 5.5), and it runs at the same `medium` as the baseline. It is no longer
>   the same price: $5 / $25 against Opus 5.5's $4 / $20. The second reason a Sonnet escalation was
>   not one change (the Opus-only `high` bump) is gone; the first (the gate never escalates to the
>   initial verifier's model) remains.
> - **Review effort.** The arm (`xhigh`) is now two levels above the default instead of one.
> - **Review wording.** Unchanged.
>
> The harness (`evals/model_effort.py`) and its tests were updated to the new baseline; the text
> and tables below are left as written on the date above.

> **Baseline changed again (2026-10-08, owner decision, no measurement).** The per-spec review
> default moved from Opus 5.5 (held to `medium`) to **Sonnet 5.5 at `high`**, and cross-check and
> compliance moved from `high` to `medium` (both still Sonnet 5.5). The cost reasoning is in
> `plans/cost-optimization-audit.md`. `SPEC_CRITIC_REVIEW_MODEL=claude-opus-5-5` restores the old
> review default. What that does to each arm:
>
> - **Escalation model.** Unchanged: escalation is still Opus 5.5, the initial verifier still
>   Sonnet 5.5.
> - **Review effort (`xhigh`).** Now one level above the default again, on a different model. The
>   rule's "otherwise" now reads "retain the default (high)".
> - **Prompt-audit effort comparison.** It was `review_effort_high` (`medium` → `high`). With `high`
>   the default, that arm changed nothing, so it is now `review_effort_medium` (`high` → `medium`):
>   still one setting, now asking what the cheaper step down loses.
> - **Review wording.** Unchanged.
>
> No arm measures the model change itself (Opus 5.5 at `medium` against Sonnet 5.5 at `high`). That
> needs its own arm (`SPEC_CRITIC_REVIEW_MODEL=claude-opus-5-5`, which moves model and effort
> together) and its own rule fixed before a run.

## The questions

The plan asks for one change at a time on three fronts:

1. **Escalation model.** Would a different escalation model be better or cheaper than Opus 5?
2. **Review effort.** Would a different review effort be better, or cheaper at the same quality, than `high`?
3. **Prompt contradictions and confidence.** Does any contradiction remain in the review prompt
   around confidence, and would removing it help?

This record answers what can be answered offline:

- what the code allows each question to mean;
- which arm tests each question as one change;
- the adjudicated dataset the arms are judged on;
- how the arms are kept apart;
- the decision rules, fixed before any result exists.

The measurements are for a live run to make.

## Provider facts, rechecked 2026-09-29

From the Claude API reference bundled with Claude Code (cached 2026-09-25) and the app's capability
whitelist. These are documented facts, not measurements.

| Model | Input / output, $ per million tokens | Effort levels | Web fetch | In the app's whitelist |
|---|---|---|---|---|
| Opus 5 | 5 / 25 | low–max | **not supported** (Opus 5 migration guide) | yes (review, escalation) |
| Opus 4.8 | 5 / 25 | low–max | supported | yes |
| Sonnet 5 | 2 / 10 | low–max | supported | yes (initial verifier) |
| Sonnet 4.6 | 3 / 15 | low, medium, high, max (no xhigh) | supported | yes |
| Opus 5.5 | 4 / 20 | low–max, default `medium` | not checked | **no** |
| Sonnet 5.5 | 2 / 10 | low–max, recalibrated | not checked | **no** |

A few more facts shaped the arms:

- **Batch pricing.** The Message Batches API halves every token line. Web searches are billed per
  request and are never discounted.
- **Newer models.** Opus 5.5 and Sonnet 5.5 are newer than anything the app lists. Neither is an arm
  here:
  - an unlisted id degrades to the conservative defaults (every capability flag off: no thinking
    config, no web fetch, no strict tools, a 64k output cap), so a comparison against it would
    measure the fallback, not the model;
  - both reject forced `tool_choice`, and Opus 5.5 cannot turn thinking off;
  - adding either to the whitelist is a change of its own, with its own tests, before it can be an
    arm.
- **Effort is a spend lever, not a cap.** It changes how much the model thinks and writes. It is not
  a hard token budget, so no arm assumes an effort level bounds cost by a fixed ratio.

## What the investigation found

### 1. A cheaper escalation is not one change today

The review behind this plan (API-3) asked whether the escalation tier needs Opus when Sonnet 5 costs
40% as much. The obvious arm, `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL=claude-sonnet-5`, would not
test that question. It would change two things.

- **The gate turns off escalation.** `verification_prescreen.should_escalate_verification` returns
  `False` when the escalation model equals the initial verifier's model (`VERIFICATION_MODEL_DEFAULT
  == VERIFICATION_ESCALATION_MODEL`). `verify_finding` makes the same check. So with that setting, no
  finding would ever escalate. The only findings reaching the "escalation" model would be CRITICAL
  jurisdictional ones, which are routed to `DEEP_REASONING` for their first pass.
- **The escalation tier's effort follows the model family, not the tier.**
  `api_config.effort_config_for` lifts a verification request to `high` only when the model is in
  `OPUS_MODELS`. A Sonnet escalation would therefore run at the initial pass's `medium`.

Measured as it stands, a Sonnet escalation arm would compare Opus-at-high escalation against *no
escalation*, plus a Sonnet-at-medium deep pass. That is a real question (is escalation worth having
at all?), but it is a different one.

To test a cheaper escalation as one change, the app first needs two things:

- an escalation that may use the initial model at a higher effort;
- an effort taken from the tier rather than from the model's family.

Both are behavior changes and belong to their own chunk. This session changed neither: the arm below
doesn't need them.

**The arm, `escalation_opus_4_8`.** Opus 4.8 is the one whitelisted alternative that changes only the
escalation model:

- it has the same token price as Opus 5;
- its effort stays `high`, because it is in `OPUS_MODELS`;
- escalation still fires, because it differs from the initial verifier.

It differs from Opus 5 in one capability the deepest pass could use: **web fetch**, which Opus 5
does not support. So the arm asks: *does an escalation tier that can read full pages reach
better-grounded verdicts on hard findings?* It does not ask whether escalation can be cheaper.

The web-fetch tool is not a second change. It is the model's capability gate
(`build_verification_tools_from_decision`) doing exactly what it does in production when this model is
chosen. The probe records it as an expected change (below).

Opus 4.8's knowledge cutoff (January 2026) is earlier than Opus 5's (May 2026). Adoption facts from
the months between would favor the baseline. That is part of what the arm measures, not a flaw in it.

### 2. Review effort: why `xhigh`

- **History.** The review ran at `xhigh` until it was lowered to `high` as a token-spend measure
  (CLAUDE.md, "Model capability whitelist"). Nothing recorded measured what the change did to recall.
- **The question.** `high` is the documented balance point between quality and token use. The open
  question is whether `xhigh`'s extra spend buys severe defects back.
- **Why not lower.** A lower level (`medium`) would test the other direction. The provider's guidance
  is to use at least `high` for intelligence-sensitive work, and reviewing specifications is that kind
  of work. So the lower arm is the second question, not the first.
- **The switch.** `SPEC_CRITIC_REVIEW_EFFORT` takes `low`, `medium`, `high`, or `xhigh`. It moves the
  per-spec review only, on both transports and on repairs. The capability clamp still applies, so
  `xhigh` under a pinned Sonnet 4.6 review model is sent as `high`.

### 3. Prompt contradictions: one left in the review system prompt, four in examples

A read-only sweep of every model-facing prompt checked the confidence thresholds and the coverage-first
rubric first:

- **Thresholds.** They are defined once (`structured_schemas.CONFIDENCE_HIGH_MIN` /
  `CONFIDENCE_MODERATE_MIN`). The rubric, the field description, and the report all read them. No
  surface disagrees.
- **The rubric.** The coverage-first rubric S18 wrote is intact: "Confidence ... is not a gate on
  whether to report. Report every finding you can ground in quoted spec text, including the ones you
  are uncertain about."

Against that rubric, the sweep found the following.

- **R1, the review system prompt (the arm).** `<review_scope>`, the last block of the system prompt,
  still says: "Only report a finding if you have concrete evidence from the spec text that a genuine
  problem exists."
  - "That a genuine problem exists" is a certainty bar, not a grounding rule. It excludes exactly the
    low band the rubric says to report.
  - It is the last thing the model reads before the documents. Anthropic's current prompting guidance
    names this pattern (a soft, judgment-based filter at generation time) as the one that depresses
    recall on current models.
  - The arm `review_scope_coverage_first` replaces that one sentence with: "Report a finding whenever
    you can quote the spec text it concerns; how sure you are that it is a genuine problem belongs in
    its confidence, not in whether you report it."
  - Nothing else in the prompt changes. The test replaces the sentence in every module's prompt and
    compares the whole prompt.
- **R2–R5, few-shot examples (recorded, not changed).** Examples are the strongest calibration signal
  in the prompt, so each of these is a candidate for a later arm. Each is its own change.
  - **R2.** `datacenter_electrical` example 3 is a cross-section coordination inference, which the
    rubric puts in the *moderate* band. It quotes no text, yet carries confidence 0.93 (high band).
  - **R3.** `datacenter_fire` example 1, and that module's high-band rubric example, rate "2015 IBC" as
    unambiguously stale at 0.90 with no adoption evidence. The same module's category #2 says an older
    adopted edition may govern and to defer to the adoption. The architecture, electrical, and ESS
    modules already tie their examples to the Project Requirements Profile; fire does not.
  - **R4.** `datacenter_fire` rates the wrong adopted edition MEDIUM in review and HIGH in compliance.
    Its severity then depends on which pass finds it first.
  - **R5 (probable).** `datacenter_architecture` example 3 is a multi-section conflict at 0.92, with no
    quote.
- **Minor inconsistencies (recorded).** None of these is a contradiction the model is likely to act
  on:
  - "Return exactly as many findings as genuinely supported" in `<task>`;
  - "as many findings as genuinely exist" in cross-check;
  - two REPORT_ONLY examples that assert what another specification says, while `<final_task>` says to
    review only the document above.
- **Wording that overstates the code (recorded).** The rubric and the field description say
  confidence is "for the downstream filter". No stage filters by confidence: the verifier filters on
  its verdict, and confidence only sorts and colors findings. The applier's `--min-edit-confidence`
  defaults to 0.

Confidence calibration itself is measured, not changed. The scorer builds a reliability table and a
Brier score over the review findings it can label:

- a finding matched to an expected defect counts as supported (1);
- a finding that hits a trap, or that an adjudicator marks unsupported, counts as unsupported (0).

Every arm reports it, so a live run shows whether the bands mean anything before anyone tunes them.

## Dataset

`evals/model_effort_dataset.py`. Version 1, 60 cases.

| | Verification (findings) | Review (specs) |
|---|---|---|
| **Held-out** | 19 new cases: 9 CONFIRMED, 8 DISPUTED, 2 UNVERIFIED expected; 11 severe | 8 new specs: 11 defects (9 severe), 17 traps, 1 clean control |
| **Tuning** | 13: 9 oracle-ledger captures, 1 data-center scenario, 3 new | 20: 14 labeled specs, 5 data-center scenarios, 1 new |

**Held-out cases are new.** Any case already used to tune a prompt or a label carries `prior_exposure`
and must be in the tuning split; validation enforces it. That covers:

- the labeled specs and live captures the 2026-09-09 baseline retuned;
- the data-center scenarios that shaped the edition-authority correction and S18.

Every held-out case was written for this set. The decision rules score the held-out split only.

**Coverage.** Every dimension the plan names has at least one tuning and one held-out case:

| Dimension | Tuning | Held-out |
|---|---|---|
| Correct citations and supported deficiencies | 7 | 9 |
| Wrong editions and adoption-date ambiguity | 16 | 3 |
| Numbers and units | 4 | 12 |
| Exceptions and negation | 2 | 4 |
| Ambiguous or inaccessible evidence (should stay UNVERIFIED) | 1 | 2 |
| Plausible but false claims | 3 | 8 |
| Low-severity factual issues | 2 | 3 |
| Severe omissions | 3 | 4 |
| Project overrides and mixed module authority | 5 | 5 |

**Adjudication rules** (the calibration ledger's rules):

- **The verdict is about the finding as written.** CORRECTED means the finding needed correcting,
  not merely that the spec needs an edit.
- **Facts come from outside this repository.** Sources are:
  - NFPA 13, 14, 20, and 72;
  - the California Building Standards Commission;
  - NIST SP 811;
  - ASTM titles;
  - FM Global;
  - Virginia's code agency.

  A source citing the repository is rejected. Justifying a case with the app's own pins would judge
  them against themselves.
- **Constructed cases.** Two cases rest on something no source can establish (an invented
  manufacturer, a private owner standard). They say so and expect UNVERIFIED.
- **Acceptable verdicts.** A case may list verdicts a careful adjudicator would not call wrong. The
  scorer counts those as partial, never as a false CONFIRMED or a false DISPUTED.
- **Section numbers are omitted.** NFPA section numbers move between editions, so sources name the
  requirement and the editions it was checked in.

**Traps** are correct text a review must not propose to change. Examples:

- an electrical room that meets NFPA 13's four conditions for omitting sprinklers;
- a "shall not use sodium silicate" negation;
- Virginia's older adopted editions;
- an owner's FM Global requirement;
- NFPA 72 governing a Division 21 waterflow switch.

A trap counts an edit (EDIT / ADD / DELETE, or narrower where noted) that matches it. A REPORT_ONLY
note asking the owner to confirm something is not an unsupported edit.

**Reused material.** The oracle ledger's resolved captures are reused with their adjudicated verdict,
and the loader re-checks each capture's evidence digest. The ledger's citations of this repository's
classifier code explain which status the classifier assigns, not the verdict, so they do not carry
over. Three captures whose verdict rests on the quoted spec text (a contradiction, a placeholder, a
TODO) are marked `spec_text`.

**Excluded, with reasons:**

| Item | Reason |
|---|---|
| Two ledger captures | Unresolved in the ledger, so there is no label to score against. |
| The duplicate-paragraph capture | Classified locally, so no model call is made. |
| The partial-research scenario | Judged on report surfaces, not on findings. |
| The no-research scenario | A finding is optional there either way. |

**Hashes.**

- `dataset_sha256`: `488bfcbcd6e0e08bd08f9d5cceeb0635f00fbf0283a53b4e5c2dd175c90d8883`
- held-out `split_sha256`: `7103637dcd45de313cc3571412ad29e12162ceb5170308974a9428292fe6f5f3`
- tuning `split_sha256`: `74b2e382aea237c22e6b85d5a0e14abea10c28367a4de82c0ed6eda5182e0a45`

Every case also has its own digest over its whole content, and every run record carries it.

**Sample size.** Nineteen held-out verification cases can reveal a large difference, not establish
equivalence. A 95% Wilson interval at 10 of 19 spans about ±20 points. The decision rules therefore
ask for two repetitions and a net paired gain, and they defer when the sample is too small.

## Arms

| Arm | Setting | Request fields that change (probe) | `request_probe` SHA-256 (first 16) |
|---|---|---|---|
| `baseline` | none (every experimental switch off) | none | `c372848754e3ff91` |
| `escalation_opus_4_8` | `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL=claude-opus-4-8` | `escalation.model`, `escalation.tools` (+`web_fetch`), `deep_initial.model`, `deep_initial.tools` | `c5228925d89a3a08` |
| `review_effort_xhigh` | `SPEC_CRITIC_REVIEW_EFFORT=xhigh` | `review.effort` | `8bf68ac768cb7581` |
| `review_scope_coverage_first` | `SPEC_CRITIC_REVIEW_SCOPE_WORDING=coverage_first` | `review.system_prompt_sha256` | `3a7194f02c52bda7` |

The probe (`evals.model_effort.request_probe`) builds real requests with the production builders and
flattens the fields any arm could move:

- the review;
- an initial verification;
- its escalation;
- a CRITICAL jurisdictional first pass;
- the effort of the phases the review switch must not touch;
- whether the escalation gate still fires.

The test builds every arm's probe in a **fresh process with that arm's environment** and checks the
difference from the baseline is exactly the listed fields. The parent process carries
`SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT=json_schema` and `SPEC_CRITIC_GOVERNING_BASIS_CONTEXT=1` while
it does, and neither reaches any arm.

## Keeping the arms apart

- **One process per arm.** The model defaults are read when `src.core.api_config` is imported, so an
  arm cannot switch inside a running process. The runner refuses to start when the resolved escalation
  model differs from the environment's.
- **An environment built from scratch.**
  - `arm_environment` drops every `SPEC_CRITIC_*` variable the operator's shell carries, so an
    unrelated experiment or model override cannot ride along.
  - It adds the arm's one setting.
  - It points every path the app writes into the arm's own state directory: the verification cache,
    the pending-batch record, traces, the log, and the updater's state.

  That directory must start empty, and it may not be inside `~/.spec_critic`.
- **Caches in memory and on disk.**
  - The verification cache key does not include the model (`make_cache_key`), so one cache shared by
    two arms would replay the baseline's verdicts in the candidate. A test demonstrates it.
  - The runner gives every arm, and every verification view within it, a fresh in-memory cache that
    never loads from or saves to disk (`SPEC_CRITIC_VERIFICATION_CACHE_PERSIST=0`).
  - Every case has its own cache key (validated), so a hit inside an arm means the isolation broke.
    The run summary records each view's hits and flags the arm `cache_contaminated`.
- **Two verification views.**
  - `path` is production's `verify_finding`: the initial pass, then the escalation tier when the gate
    fires. It measures the production effect and the cost.
  - `tier` forces the escalation tier (`escalated=True`) on every case. The gate sends only some
    findings to the escalation model, so without this view the escalation model would be measured on
    a handful of cases.
  - The views have separate caches. The key ignores the escalation flag, so one cache would replay
    the path verdict in the tier view.

## Metrics and decision rules (fixed before any run)

`evals/model_effort.py` computes every metric with its sample size and 95% Wilson interval. It also
gives paired discordance by (case, repetition) with an exact two-sided binomial p.

**Verification** (per view):

- exact-verdict accuracy;
- acceptable rate;
- false CONFIRMED and false DISPUTED, **reported separately**;
- severe true findings discarded;
- legitimate uncertainty kept, and unwarranted uncertainty;
- operational failures;
- evidence: conclusive verdicts that are grounded, carry a quote, and how many used web fetch;
- escalations and contested verdicts;
- estimated cost, priced per attempt on its own model;
- latency p50 and p90.

**Review:**

- recall and severe-defect recall;
- severity match;
- trap hits;
- unsupported-finding rate: trap hits plus adjudicated extras;
- unclassified extras, counted and not guessed;
- failed and repaired reviews;
- confidence calibration by band, with a Brier score;
- cost and latency.

**Rules** (`DECISION_RULES`, applied by `decide`):

| Experiment | Defer if | Reject if | Promote only if |
|---|---|---|---|
| Escalation model | fewer than 30 tier-view pairs | any more false DISPUTED than the baseline; a severe true finding discarded that the baseline kept; more than one more false CONFIRMED; more than one more operational failure | a net gain of at least 3 correct pairs in the tier view, path-view cost per record at most 1.25× the baseline's, and path-view p90 latency at most 1.5× |
| Review effort | fewer than 16 severe defects scored | more severe defects lost than gained; more than one more trap hit; more failed reviews | at least 2 severe defects gained and none lost, trap hits not higher, and cost per review at most 1.5× |
| Review wording | fewer than 20 defect pairs | as for effort | a net gain of at least 2 defects with at most 1 lost, trap hits not higher, and cost per review at most 1.25× |

Every other outcome retains the current default. The dataset meets each minimum only with two
repetitions, so a single-repetition run defers by construction.

## Measurements

| Measure | Every arm |
|---|---|
| Verdict accuracy, false CONFIRMED, false DISPUTED, uncertainty | not measured |
| Severe-defect recall, unsupported findings, trap hits | not measured |
| Repair and failure rate | not measured |
| Confidence calibration | not measured |
| Cost, latency | not measured |

Offline results establish behavior only (`tests/test_model_effort_experiment.py`, 99 tests):

- **The switches.** Off values leave every request unchanged:
  - `SPEC_CRITIC_REVIEW_EFFORT` changes `output_config` alone, is clamped per model, and leaves every
    other phase alone. Unknown values fail closed, with one warning.
  - `SPEC_CRITIC_REVIEW_SCOPE_WORDING` replaces exactly one sentence in every module's prompt.
- **The dataset.** It is valid, and every dimension is in both splits. Held-out cases are new, and
  both failure directions are measurable. Digests move with content. A changed capture refuses to
  load. The validator catches each rule broken on purpose.
- **The arms.**
  - Each arm is one setting.
  - Its environment drops inherited switches, and its state starts empty.
  - Built in its own process, it changes exactly its listed request fields.
- **The runner, against scripted clients.**
  - It records both verification views with zero cache hits and nothing written to disk.
  - It stops at the spending cap between cases.
  - It reads a review from a real `.docx` through the production extraction, pre-screen, and
    real-time review path, with the case's Project Context.
  - It refuses to run without `--live`, without a cap, with the test key, in another arm's
    environment, in a process that imported another escalation model, or onto existing records.
- **The scorer and the rules.** Each outcome class is tested. Each rule's promote, retain, reject, and
  defer branch is driven with synthetic records. Scoring works end to end from record files.
- **Mutation check.** 29 deliberate breakages, all caught:
  - **The switches:**
    - the switch ignored;
    - the switch on by default;
    - unknown values accepted;
    - the builder dropping the override;
    - the override bypassing the clamp;
    - the wording always on, or changing two sentences.
  - **Isolation:**
    - inherited switches kept;
    - no state paths;
    - a non-empty state directory accepted;
    - one cache for both views;
    - no spending cap;
    - no import-time model check;
    - the sentinel key accepted.
  - **The rules:**
    - no false-DISPUTED rule;
    - no severe-discard rule;
    - either cost limit ignored;
    - REPORT_ONLY notes counted as trap hits;
    - acceptable verdicts counted as errors;
    - the minimum-sample rule ignored.
  - **The validator and hashing:**
    - no prior-exposure rule;
    - repository sources accepted;
    - duplicate cache keys accepted;
    - split coverage unchecked;
    - the ledger digest unchecked;
    - a digest blind to content;
    - a wrong Wilson interval;
    - a run order that does not alternate.

## What a live evaluation must do

`evals.model_effort.EVALUATION_PROTOCOL` (status NOT RUN) holds it. In short:

1. **Authorization first.** A spending cap, an API key, the dataset digest above, and the stopping
   rule, agreed in advance. A key alone is not authorization.
2. **Recheck.** Prices and capabilities, and that Opus 4.8 still accepts `web_fetch_20260209`.
3. **Harness check on the tuning split.** Run each arm once with a small cap. Confirm every record is
   `ok` and no cache hit is recorded. Adjust matchers only on the tuning split.
4. **Runs.** One experiment at a time, with two repetitions:
   ```text
   python -m evals.model_effort run-experiment --experiment <id> --state-root <dir> --out <dir> --max-spend-usd <cap> --repetitions 2 --live
   ```
   - Each arm runs in its own process.
   - The run order alternates.
   - The cap is split across the four runs, and each run stops between cases once its share is spent.
5. **Adjudication.** Before scoring a review experiment, a person reads every unclassified extra
   finding and records the unsupported ones, without knowing which arm produced them.
6. **Score.**
   ```text
   python -m evals.model_effort score --experiment <id> --out <dir> [--adjudication file.json]
   ```
   Report every metric with its sample size and interval, and the rules' decision.

**Request volume, for setting a cap.** Counts only; no cost is estimated here.

- **Escalation model.** 19 held-out cases × 2 views × 2 arms × 2 repetitions = 152 verification
  calls, plus the path view's escalations and any `pause_turn` continuations.
- **Each review experiment.** 8 specs × 2 arms × 2 repetitions = 32 review requests, plus any
  repairs.

## Decision

**Not evaluated. Keep every default:**

- Opus 5 escalation;
- review effort `high`;
- the current `<review_scope>` wording.

No live request was authorized, and the plan's rule is to retain the default when the result is
inconclusive.

What the chunk leaves behind:

- **An adjudicated, hashed dataset** with a held-out split no prompt was tuned on.
- **Three arms that each change one request field** (proved in separate processes), with isolation
  that the runner checks rather than assumes.
- **Pre-registered rules.** They decide whether a later run promotes, rejects, or retains, so the bar
  cannot move to fit a result.
- **Two findings about the code.** They matter to anyone who wants a cheaper escalation tier: the
  equal-model gate, and effort keyed on the model family.

## Rollback

- Unset `SPEC_CRITIC_REVIEW_EFFORT` and `SPEC_CRITIC_REVIEW_SCOPE_WORDING`. Both are off by default,
  and off is byte-identical: no golden moved.
- Nothing is persisted by either switch: no verification cache, pending batch, sidecar, or report
  shape depends on them.
- A saved review batch stays collectable after either switch changes. Its repair request is built at
  collection time, so the repair follows the setting in effect then; the reader never consults either
  switch.
- The escalation arm is an existing variable. Unsetting it restores Opus 5.
- `evals/model_effort*.py` is evaluation code; nothing under `src/` imports it.

## Reproduce

```text
python -m evals.model_effort describe
python -m evals.model_effort validate
python -m evals.model_effort probe
python -m pytest tests/test_model_effort_experiment.py
```
