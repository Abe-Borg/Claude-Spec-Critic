# Package-level prompt coverage evaluation

`evals.package_review` compares the cross-check and compliance coverage wording
added in PR #412. It reuses `evals.model_effort` for environment/state isolation,
finding matching, pricing and latency summaries. Requests, response parsing,
chunk synthesis and compliance coverage reconciliation use production code.
The GUI, production defaults and production prompts are unchanged by this harness.

## What is compared

- `baseline`: the merged prompts with only the added confidence/severity
  coverage instructions removed.
- `coverage`: the shipped prompts, including those instructions.

Both keep the review fix that prohibits REPORT_ONLY findings for subset-local
absence. This is a controlled wording comparison against a safe baseline, not
a replay of the unsafe pre-review fallback. User messages, profile inputs,
existing findings, model, effort, schemas and cache policy are identical.
Frozen coverage fragments and request probes refuse a comparison if this
contract changes. Probes build requests without sending them.

The 20 cases cover severe equipment conflicts, minor naming drift, clean
coordination, already-identified findings, missing/contradicted owner
requirements, unverified/process controls, and chunk reconciliation. The
chunk-represented case covers the PR review scenario: a requirement exists in
one subset and is absent without an insertion anchor in the other. The
chunk-missing case requires a genuine package omission to survive the merge.

All cases are **constructed, closed-world owner requirements**, not researched
laws or representative project packages. `example.invalid` denotes fictional
fixture evidence. Tuning and held-out splits have different equipment values
and requirement ids but share templates. These splits can detect regressions;
they cannot establish real-world generalization. Real project cases and human
adjudication are needed before a production adoption decision.

## Offline checks

From the repository root:

```sh
python -m evals.package_review validate
python -m evals.package_review probe --split all
python -m pytest -q tests/test_package_review_eval.py
```

`validate`, `probe`, `adjudication-template` and `score` make no paid requests.

## Collecting paired responses

With `ANTHROPIC_API_KEY` configured, use a fresh output directory and an explicit
USD cap. This example is a paid run, not an instruction to run it automatically:

```sh
python -m evals.package_review run \
  --out /tmp/spec-critic-package-tuning \
  --split tuning --repetitions 3 --cap-usd 10 --live
```

Run held-out measurements into a different fresh directory after tuning:

```sh
python -m evals.package_review run \
  --out /tmp/spec-critic-package-held-out \
  --split held_out --repetitions 3 --cap-usd 10 --live
```

Each arm/repetition starts a fresh Python process with operator
`SPEC_CRITIC_*` overrides removed and state paths under its own directory.
Arm order alternates between repetitions. Both arms use one attempt per
request, with SDK retries disabled, to keep collection bounded; this is an
evaluation control and differs from production's retry allowance. Fixed
fixture partitions exercise the production chunk engine without letting the
extra prompt tokens change which specs are compared. The production input
budget check still applies. This does not evaluate automatic chunk planning
or cross-module coordination.

The cap is divided across arms/repetitions, with the remaining overall budget
checked before starting each arm. It is checked again before every streamed
request. **One in-flight request can exceed the cap by its own cost.** Unknown
usage or an unpriced model stops further sends, including subsequent arms.
Partial records remain on disk; there is no automatic retry or resume. A cap
that is too small yields incomplete comparisons, not cheaper valid results.

`experiment.json` records source/dataset fingerprints and request probes.
JSONL files contain every case outcome, final findings and coverage, response
tool content, actual request fingerprints, per-request usage and latency.
Usage is recorded before response parsing, including failed streams as unknown
usage. Interrupted, failed and skipped cases remain visible. Records flush
after each case. An interruption before an outcome is written leaves an
incomplete file that the scorer rejects.

## Adjudication and scoring

```sh
python -m evals.package_review adjudication-template \
  --out /tmp/spec-critic-package-held-out > /tmp/package-adjudication.json
python -m evals.package_review score \
  --out /tmp/spec-critic-package-held-out \
  --adjudication /tmp/package-adjudication.json
```

The template is keyed by **arm/repetition/case**, with a digest of that record's
findings. Inspect the JSONL response and the dataset before marking a record
`reviewed: true`. Check every expected defect's index (or `null` for a miss),
then classify every remaining finding:

- `supported`: a correct additional finding or eligible confirmation advisory;
- `unsupported`: a false claim or a forbidden action;
- `duplicate`: a redundant finding of an already recovered issue.

Example for a successful record with one expected finding and one duplicate:

```json
{
  "coverage/1/held_out.compliance.missing": {
    "findings_sha256": "copy the digest from the generated template",
    "reviewed": true,
    "matches": {"missing_test_duration": 0},
    "dispositions": {"1": "duplicate"}
  }
}
```

Invalid/reused indexes, stale digests, unmatched record keys, incomplete
dispositions and attempts to override fixed eligibility traps are rejected.
Leaving `reviewed: false` keeps matching provisional. Automatic token matches
are suggestions, not adjudication. Eligible REPORT_ONLY confirmation advice
is not automatically treated as a false positive.

Scoring reports both combined and separate cross-check/compliance results:
expected and recovered defects, severe recall, supported/unsupported/duplicate
counts, unclassified extras, clean packages with known false positives,
coverage correctness, failures, unrun cases, package cost and median latency.
Precision and adjudicated recall are withheld until all records for the arm
and surface succeed and receive complete adjudication. Precision counts
distinct correct findings and supported extras as successes; duplicates remain
in its denominator. Failed/unrun cases remain misses in provisional recall.
Do not infer clean-package precision from the known-false-positive count while
extras remain unclassified. Repetitions of a template are not independent
projects; this harness makes no statistical significance claim.

The scorer requires every case/repetition from both arms and matching
source/dataset/request fingerprints. It flags unsuccessful pairs as
incomparable. Partial runs cannot support a promotion decision. To score an
older dataset, check out the source used for that run; do not silently relabel
captured responses with a newer dataset.

**Downstream verification is not run.** Records explicitly leave total
pipeline cost unset. These measurements exclude requirements research,
per-spec review and verification; package cost must not be presented as total
cost or as proof that coverage wording improves verified quality. Acceptance
gates and review-plus-verification measurements belong to the later audit
steps. No paid runs or quality improvement claims accompany this harness PR.
