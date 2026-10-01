# Prompt audit improvements

This work follows the attached *Spec Critic Prompt Audit* and the subsequent
repository review. The owner requested one pull request per step, with a pause
for their review and merge before the next step starts. Begin each subsequent
step from the merged `master`; do not merge a PR on the owner's behalf.

## Sequence

| Step | Scope | State |
|---|---|---|
| 1 | Extend finding-coverage instructions to cross-check and compliance. | Merged in PR #412; offline validated. Live quality measurement pending. |
| 2 | Extend the existing experiment infrastructure with package-level cross-check/compliance cases and comparisons. | Implemented in this change; offline validation below. Awaiting owner review and merge. |
| 3 | Compare review effort, open-ended procedure, and the existing `coverage_first` wording as separate variants. | Pending step 2 review and merge. |
| 4 | Apply predeclared quality and total-cost gates; adopt, retain, or reject each variant on measured evidence. | Pending step 3 review and merge and sufficient measurements. |
| 5 | Investigate native-citation behavior with structured tool outputs and quote provenance before proposing a grounding change. | Pending step 4 review and merge. |

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
