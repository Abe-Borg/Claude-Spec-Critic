# Prompt-audit quality and total-cost gates

Step 4 adds `evals.prompt_audit` to collect and score the complete measured
path: a per-spec review or package pass, its Haiku triage, and its production
verification/escalation calls. It reuses the step 2 package executor, step 3
review builders and fixtures, production report statuses, and the existing
matching, state-isolation and pricing helpers. Existing EX-03 runners and
historical rules remain available. Production defaults do not change.

**Status: no paid run or adoption evidence.** Offline tests establish the
measurement contracts, not improved quality, savings or faster responses.
Each comparison has its own declaration, output directory and paired arms.
Supported comparisons: `review_effort_medium`, `review_procedure`,
`review_scope_wording`, and `package_coverage`. The last reports cross-check
and compliance separately. `review_effort_medium` was `review_effort_high`
while the review default was Opus 5.5 at `medium`; since 2026-10-08 it compares
the Sonnet 5.5 `high` default with `medium`.

## Declare before collecting

Run from the repository root. This command builds isolated request probes
without an API key or remote requests, then writes `audit.json`:

```sh
python -m evals.prompt_audit declare \
  --experiment review_effort_medium --split held_out --repetitions 3 \
  --out /tmp/audit-review-medium-held-out
```

The declaration freezes versioned gates, arm settings, source/runtime/dataset
fingerprints, primary/repair or chunk request shapes, verification context,
and declaration time. Every actual arm rechecks those fingerprints before
sending anything. Scoring requires the original checkout and dependencies.
A changed source, SDK, dataset or protocol requires a fresh declaration.
Existing step 2/3 review-only captures cannot become step 4 evidence. Use a
separate tuning declaration for harness development; tuning always defers a
fixture decision. Do not tune against held-out responses.

## Predeclared screening gates

These fixed operational thresholds in version 1 were not selected from
candidate outputs and are not statistical confidence claims.

| Gate | Required candidate behavior |
|---|---|
| Completeness | All cases in both arms and every repetition succeed, with known priced usage and complete human judgments. |
| Repetitions | At least three; distinct cases count once for sample minimums. |
| Raw and final recall | Neither drops below baseline. |
| Severe recall | At least 90% in both views, with **zero paired losses** of a CRITICAL/HIGH defect baseline recovered in the same case/repetition. Gains elsewhere cannot compensate. |
| Precision | At least 95% in each defined view, with at most a two-percentage-point decrease from baseline. Duplicates remain in the denominator. |
| Clean fixtures | No increase in cases with unsupported findings. |
| Duplicates | No increase in duplicate rate. |
| Retained severe false positives | Zero unsupported findings reported as CRITICAL/HIGH in the final view. |
| Requirement coverage | No missing, incorrect, duplicated or unexpected coverage rows in the candidate. |
| Total measured cost | At most 125% of baseline, including review/package, triage and verification. |
| Serial p95 latency | At most 125% of baseline. |
| Benefit | Higher final recovered-defect count, or at least 10% lower total cost, while every quality/resource gate holds. Otherwise retain baseline. |

Held-out minimums reflect available fixtures, not representativeness: review
needs eight unique cases, one clean case and eight severe defects; cross-check
needs four cases, two clean and one severe defect; compliance needs six cases,
three clean and three severe defects. Pooling repetitions does not satisfy
unique-case minimums. Severe defect labels come from fixture truth independently
of the model's severity.

The `final` view includes `VERIFIED_SUPPORTED`, `VERIFIED_CONTRADICTED` and
`LOCALLY_CLASSIFIED` findings. All other report statuses are counted and
remain visible in the capture. This evaluates settled claims; it changes
neither production reports nor downstream edit policy. A grounded model
verdict is not a truth label. A human must judge each finding and, for
CORRECTED, the actual correction. Local classifications also need adjudication.

## Collect after API access and a budget are authorized

No live collection was performed for this PR. With credentials and an agreed
spending cap configured, this example runs the declaration above:

```sh
python -m evals.prompt_audit run \
  --out /tmp/audit-review-medium-held-out --cap-usd 10 --live
```

Each arm/repetition runs in a fresh subprocess. Inherited experimental
overrides are removed, candidate controls stay independent, and order
alternates. Cases use fresh in-memory verifier caches and serial calls;
legitimate cache hits within a case are recorded. Provider prompt caching
is not isolated across processes; actual cache usage is priced as reported.
The production pre-pass determines local skips and Haiku eligibility, and
`verify_finding` controls initial routing and escalation. SDK retries remain
disabled under the existing production retry policy.

One ledger intercepts all actual stream/create requests, retaining usage from
malformed responses and unknown usage from exceptions. It checks the budget
**before every SDK request**, including repairs, continuations, triage and
escalation. Unknown/unpriced usage prevents another send. The parent allocates
the cap across processes and stops after a failed process or unknown usage.
An in-flight request can exceed its allowance by its own cost; this is not a
hard billing limit. Records are flushed per case. A crashed arm can leave
partial output, which cannot pass the gates. Collection cannot restart in
the same declaration directory.

Measured cost is **review/package plus verification**, not the entire
application. It excludes requirements research, unrelated package passes,
drawing impact and report generation. Prices are estimates from captured token,
cache and server-tool usage, not an invoice. Per-operation breakdowns expose
downstream spend that a cheaper review could otherwise hide.

Latency runs from generation through final verification in this serial
real-time harness, including preparation, repairs, triage, continuations and
retry waits within the case. It is neither production batch turnaround nor
production concurrent wall time. p50/p95 values are descriptive fixture
statistics; repetitions of templates are not independent projects.

## Adjudicate each complete record

```sh
python -m evals.prompt_audit adjudication-template \
  --out /tmp/audit-review-medium-held-out > /tmp/audit-review-medium-judgments.json
```

The template starts with `reviewed: false`; it is not an oracle. Inspect JSONL
findings, raw response blocks, verifier explanations/corrections and sources
before marking a record reviewed. Keys are `<arm>/<repetition>/<case_id>`.
Each judgment binds to the findings digest and **the entire record digest**,
including verification and usage. Changed outcomes require new judgments.

- `matches`: address every expected defect with an original finding index or
  `null`. Token matches are provisional suggestions.
- `dispositions`: classify every unmatched original index as `supported`,
  `unsupported` or `duplicate`.
- `final_matches`: independently address every defect with a retained index
  or `null`. An original match may be lost after verification; a correction
  must actually establish the expected defect to count.
- `final_dispositions`: classify every unmatched retained index. Remove an
  index from this map when assigning it to `final_matches`.

Matched indexes must be distinct and valid. Fixed unsafe-edit traps remain
unsupported regardless of model verdicts or attempted judge overrides.
Unknown or unsuccessful record keys are refused. Precision and fixture
decisions stay withheld until every pair has complete judgments and usage.
Missing files, unrun cases and failures are explicit; their known spend does
not become a successful zero-cost case. No inferential intervals are reported
for pooled repeats.

## Decide within the evidence's scope

```sh
python -m evals.prompt_audit score \
  --out /tmp/audit-review-medium-held-out \
  --adjudication /tmp/audit-review-medium-judgments.json
```

`fixture_decision` reports `adopt`, `retain`, `reject` or `defer` against the
frozen gates. Each package stage receives its own decision; cross-check gains
cannot mask compliance regressions. `production_decision` always defers for
these constructed datasets. A fixture win is a candidate for representative
real-spec review, not authority to switch the shipped prompt or effort.
The review corpus has only one held-out clean case; package splits share
templates and use fictional owner requirements.

Record negative and inconclusive results alongside wins. Production adoption
requires independently adjudicated representative specs and real-world
operating costs/latency, followed by a separate reviewed change. Neither
`score` nor `run` modifies production defaults. Native-citation and
quote-provenance investigation remains step 5.
