# Prompt audit improvements

This work follows the attached *Spec Critic Prompt Audit* and the subsequent
repository review. The owner requested one pull request per step, with a pause
for their review and merge before the next step starts. Begin each subsequent
step from the merged `master`; do not merge a PR on the owner's behalf.

## Sequence

| Step | Scope | State |
|---|---|---|
| 1 | Extend finding-coverage instructions to cross-check and compliance. | Merged in PR #412; offline validated. Live quality measurement pending. |
| 2 | Extend the existing experiment infrastructure with package-level cross-check/compliance cases and comparisons. | Merged in PR #413, including request-history review fix; offline validated. Live measurements pending. |
| 3 | Compare review effort, open-ended procedure, and the existing `coverage_first` wording as separate variants. | Merged in PR #414; offline validated. Live measurements pending. |
| 4 | Apply predeclared quality and total-cost gates; adopt, retain, or reject each variant on measured evidence. | Merged in PR #415; offline validated. Live collection, adjudication and representative real-spec evidence remain pending. |
| 5 | Investigate native-citation behavior with structured tool outputs and quote provenance before proposing a grounding change. | Offline investigation and future API-spike protocol documented in this change; awaiting owner review and merge. Live API behavior remains unmeasured. |

## Current baseline

The report described v3.9.0 with Opus/Sonnet 5 and review effort `high`.
The checked-out v3.10.0 instead defaults to Opus/Sonnet 5.5, with Opus review
at `medium` through `api_config.OPUS_EFFORT_CEILING`. An explicit
`SPEC_CRITIC_REVIEW_EFFORT` override can select `high` for a controlled comparison.

`evals/calibration/harness.py` replays captured verifier responses; it does not
measure defects the review model failed to emit. Extend `evals/model_effort.py`
and its adjudicated dataset for review comparisons. Its existing process/cache
isolation, repetitions, tuning/held-out split, usage accounting, and separate
`coverage_first` wording arm provide the starting point. Package-level
cross-check/compliance evaluations belong in step 2.

## Step 1: grounded findings without confidence or severity suppression

Both package-level prompts now repeat the review prompt's rule that confidence
labels evidence rather than deciding whether to emit a finding. They explicitly
include uncertain and low-severity issues and explain that downstream
verification filters and ranks the findings. Zero findings remains valid.

Cross-check retains its cross-spec scope, literal-text grounding, and prohibition
on duplicating already-identified findings. Compliance ties gaps to controlling
profile requirements and the supplied corpus. Missing text cannot be quoted:
cite the requirement id and relevant context, use a verbatim insertion anchor
for ADD, and use REPORT_ONLY when no reliable anchor exists in a whole-package
request. In a chunk subset, record absence in coverage and emit an ADD only
with a reliable anchor; never use REPORT_ONLY for subset-local absence.
Unverified items and process advisories remain non-controlling, with the
existing coverage and hedging rules still applicable.

The intended golden changes are the California and data-center cross-check
system prompts and the data-center compliance system prompt. The chunk-subset
note reinforces the restriction on REPORT_ONLY absence findings; whole-package
user messages, review prompts, schemas, parsing, chunk planning, model selection,
and effort policy are unchanged. Existing golden and compliance-completeness
tests establish the offline contract; they cannot establish an improvement in
model recall.

Validation on 2026-09-30:

- Regenerated only the three affected system-prompt goldens and inspected their
  diffs; all three targeted golden tests passed.
- `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1 .venv/bin/python -m pytest -q`:
  **7,402 passed, 21 skipped**. Skips covered live API smoke tests and unavailable
  optional tokenizer/packaging/browser tooling.
- `.venv/bin/python -m evals.runner`: **9 of 9 offline regression fixtures passed**.
- `UV_CACHE_DIR=/tmp/spec-critic-uv-cache uv pip check --python .venv/bin/python`:
  **all 45 installed packages compatible**.
- `git diff --check`: passed.

No paid model run was performed. Finding recall and verification cost remain
unmeasured; step 2 supplies the package-level evaluation cases and comparisons.

Review follow-up: [PR #412 comment](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/412#discussion_r4149959294)
identified that the package merge reconciles ADD findings but preserves other
actions. An unanchored REPORT_ONLY absence finding from one chunk could therefore
survive even when another chunk represents the requirement. The no-anchor
fallback is now reserved for whole-package requests, and the chunk note explicitly
requires coverage without a finding when an insertion cannot be anchored.
Genuine contradiction and advisory REPORT_ONLY findings retain their existing
eligibility and hedging rules.

After this follow-up, the compliance system golden was regenerated, the
whole-package/subset request distinction was checked, and the full offline
suite was rerun: **7,402 passed, 21 skipped**. `git diff --check` passed.

## Step 2: package-level measurements

`evals.package_review` extends the experiment infrastructure with 20 labeled,
constructed owner-basis packages, split between tuning and held-out fixtures.
It reuses the existing model-effort isolation, pricing and matching helpers and
the production package request builders, parsers, chunk engine and compliance
finalizer. A controlled baseline removes only the added coverage instructions;
both arms retain the merged chunk-absence safeguards. Offline probes require
every other request field to match. Production defaults are unchanged.

Fresh subprocesses and state directories isolate each arm/repetition, and arm
order alternates. Explicit fixture partitions hold chunk membership constant.
One request attempt per chunk bounds the experiment; SDK retries are disabled.
Every streamed attempt is priced, including usage from unparseable responses;
unknown usage stops further calls. Runs require a live flag, API key, empty
output directory and finite positive USD cap. One in-flight request may exceed
the cap by its own cost.

Scoring retains severe misses, duplicates, clean-package false positives,
unclassified extras, coverage errors and unsuccessful cases. Human judgments
bind to each arm/repetition/case's finding digest. Precision and adjudicated
recall are withheld until successful records are fully adjudicated. Source,
dataset and request fingerprints guard paired comparisons. Separate summaries
for cross-check and compliance prevent one surface hiding the other's result.

The fixtures share templates across splits and are not real-project evidence.
Downstream verification is not run, and total pipeline cost stays explicitly
unmeasured. Those measurements and predeclared acceptance gates remain for the
later steps. See [the evaluation guide](../docs/package_review_evaluation.md)
for commands, adjudication format and limits. No paid run was performed.

Validation on 2026-10-01 UTC:

- Full hermetic suite with required HTML tooling: **7,435 passed, 21 skipped**,
  including 33 new evaluation-harness tests. Skips cover the same unavailable
  live API, tokenizer, packaging and browser tooling as step 1.
- Offline dataset validation: **20 cases, no validation errors**. Request
  probes checked **24 request shapes**, with coverage wording as the only
  difference between arms.
- Existing offline evaluation runner: **9/9 fixtures passed**.
- Dependency check: **45 installed packages compatible**.
- Staged diff whitespace check: passed.

An earlier full run encountered an intermittent failure in the unchanged
`test_concurrent_follower_inherits_clean_unverified_in_process` concurrency
test. Its file passed all 30 tests independently, and the final full-suite
rerun passed. No verifier or concurrency implementation was changed here.

Review follow-up: [PR #413 comment](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/413#discussion_r4150904327)
identified that checking request membership alone accepted successful records
with missing, duplicated or reordered chunk requests. Successful records now
require the entire probe sequence, and unrun records must have no requests.
Failed records permit only requests in probe order, with skipped chunks omitted:
the production chunk engine can skip an oversized chunk and continue to later
chunks, so those histories need not be prefixes. Failed pairs remain
incomparable. Regression tests reproduced six malformed-success cases before
the fix, and a collector integration test verifies the skip-then-send history.
Follow-up validation: **52 harness tests passed**, **7,454 full-suite tests
passed with 21 expected skips**, and dataset validation and the staged diff
whitespace check passed. No paid run was performed.

## Step 3: independent review variants

The existing review dataset and runner now support `review_effort_high`
(shipped `medium` versus `high`) and `review_procedure` (the numbered
procedure versus open-ended reasoning). The existing `review_scope_wording`
experiment supplies the separate `coverage_first` comparison. Each candidate
sets exactly one control. Distinct values of the effort control are allowed;
duplicate variable/value pairs remain invalid. The original `xhigh` arm and
its historical decision rules keep their meaning.

`SPEC_CRITIC_REVIEW_PROCEDURE=open_ended` changes only the procedure block.
Section traversal, literal quotes, confidence assignment, boilerplate
exclusion, output schema and every other prompt block stay the same. The
switch defaults off and leaves existing prompt goldens byte-identical.
Both transports and repairs use the production review request builder.

`probe-experiment` checks isolated subprocesses without API calls. Preflight
now runs before paid experiment arms as well, requiring exactly the declared
request differences and recording the probes plus dataset/split fingerprints.
Full fixed-review and package/research request hashes protect the controls
beyond the selected fields. Arm order alternates, states stay separate, and
a failed subprocess stops later arms.

The two new experiment ids always defer adoption decisions. Audit scoring uses
`--measurement-only` for all three comparisons, including the existing scope
arm. Step 4 still owns predeclared quality gates, record-bound adjudication
and review-plus-verification measurements. The existing runner collects
real-time reviews and excludes downstream verification; its cost cannot be
presented as total pipeline cost or measured batch savings. The eight held-out
review cases are constructed variants, not real-project generalization
evidence. See [the review evaluation guide](../docs/review_prompt_evaluation.md)
for commands and controls. No paid model run was performed.

Validation: **7,519 full-suite tests passed with 21 expected skips**, including
164 review experiment and variant tests. All three isolated request probes
passed with exactly their declared changes; dataset validation, all nine
offline evaluation fixtures and the diff whitespace check passed. Existing
default prompt goldens remained unchanged. After master merged the SDK upgrade,
the full suite and probes passed again with Anthropic 1.11.0, and the installed
dependency graph was compatible. No paid run was performed.

## Step 4: predeclared quality and total-cost decisions

`evals.prompt_audit` adds a separate, predeclared workflow over the existing
review and package fixtures and production executors. Each declaration freezes
source/runtime/dataset fingerprints and isolated request controls before any
paid output. The collector includes production local/Haiku triage, verification
and escalation, with fresh subprocess/state per arm/repetition and caches per
case. A shared ledger measures every actual stream/create request, preserves
failed/unknown usage, and checks the budget before every send.

Adjudication binds to each complete record, including verifier results and
usage. Raw and retained findings receive independent matches/dispositions;
model verdicts alone do not establish truth. Severe paired losses cannot hide
inside aggregate recall. Quality, cost and serial latency gates are frozen
before collection. Cross-check and compliance receive separate decisions.
Incomplete pairs, unknown prices/usage and unfinished judgments defer decisions.

The workflow can recommend adopt/retain/reject on its evaluated fixtures, while
production decisions remain deferred pending representative real-spec evidence.
No production defaults changed and no paid run occurred. Measured scope is
real-time review/package plus triage/verification, excluding research and other
stages; this is not batch savings or production concurrent latency. Existing
step 2/3 captures lack those downstream measurements and cannot be relabeled.
See [the decision guide](../docs/prompt_audit_decisions.md) for gates and commands.

Validation: **7,615 full-suite tests passed with 21 expected skips**, including
96 new audit tests. Integration checks exercised production review/package
parsers, Haiku triage, both verifier tiers and pause-turn continuations. SDK
content models are normalized only for fingerprints; sent objects stay intact.
All four isolated held-out declarations/probe comparisons passed, and their
missing-evidence scores deferred. Both dataset validators, all nine existing
offline evaluation fixtures, the 45-package dependency check and the diff
whitespace check passed. No paid run or production adoption occurred.

## Step 5: native citations and quote provenance

The source/installed-SDK investigation distinguishes existing native-attribution
capture from the unresolved association to the verdict's `source_quote` field.
SDK 1.11.0 declares citations on `TextBlock`, with none declared on
`ToolUseBlock`; that observation does not establish live provider behavior.
The collector already handles both transports and attributes citations to
attempts. EX-04's observation-only source-identity comparison uses normalized
fragments, while citation caps and document resolution cannot certify an
entire quote's exact text.

[EX-07](experiments/EX-07-native-citation-quote-provenance.md) records the candidate
and a bounded future API spike comparing the unchanged tool verdict with cited
prose followed by the same verdict tool. It specifies positive controls, both
transports/models, raw evidence, exactness/coverage/source checks, negative
replays and inconclusive outcomes. CLAUDE.md's open-items table links the work.
No prompt, parser, grounding, cache or report behavior changes. No live run
occurred; an authorized spending cap and API access are still needed.

Validation: **367 existing targeted tests passed** across native citations,
source and batch-wave grounding, evidence validation, and cache serialization/
eligibility. Installed SDK field inspection, all 28 relative documentation
links and the diff whitespace check passed. Only four Markdown documents
change; the full suite was not rerun. Live API behavior and quality remain
unmeasured.

## Measurement rules for later steps

Measure correct findings recovered rather than raw finding counts. Adjudicate
unmatched findings before estimating precision; retain explicit counts for
unclassified extras and duplicates. Report recall separately for CRITICAL/HIGH
defects, false positives on clean packages, downstream verification outcomes,
and total review-plus-verification cost and latency. Set severity-aware
acceptance rules before examining candidate results; an aggregate allowance of
two findings must not hide severe misses.

Evaluate each change separately before combining successful variants. Keep
research, cross-check, and compliance effort fixed during review-effort tests.
Record negative and inconclusive results as well as wins. A live run needs
configured API access and a spending cap; implemented experiment wiring is
not a measured quality result.
