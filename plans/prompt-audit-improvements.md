# Prompt audit improvements

This work follows the attached *Spec Critic Prompt Audit* and the subsequent
repository review. The owner requested one pull request per step, with a pause
for their review and merge before the next step starts. Begin each subsequent
step from the merged `master`; do not merge a PR on the owner's behalf.

## Sequence

| Step | Scope | State |
|---|---|---|
| 1 | Extend finding-coverage instructions to cross-check and compliance. | Implemented and offline validated in this change. Live quality measurement pending. |
| 2 | Extend the existing experiment infrastructure with package-level cross-check/compliance cases and comparisons. | Pending step 1 review and merge. |
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
for ADD, and use REPORT_ONLY when no reliable anchor exists. Unverified items
and process advisories remain non-controlling, with the existing coverage and
hedging rules still applicable.

The intended golden changes are the California and data-center cross-check
system prompts and the data-center compliance system prompt. User messages,
review prompts, schemas, parsing, chunking, model selection, and effort policy
are unchanged. Existing golden and compliance-completeness tests establish
the offline contract; they cannot establish an improvement in model recall.

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
