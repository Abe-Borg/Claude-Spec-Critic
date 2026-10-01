# Independent review comparisons for the prompt audit

Audit step 3 extends `evals.model_effort`; it does not change production
defaults. The baseline is Opus 5.5 review at `medium`, the four-step review
procedure, current scope wording and all experiment switches off. The report
described an older `high` baseline; current code makes `medium` the correct
control. Research, cross-check, compliance and verification stay fixed.

| Experiment id | Candidate arm | Its only setting | Expected request change |
|---|---|---|---|
| `review_effort_high` | `review_effort_high` | `SPEC_CRITIC_REVIEW_EFFORT=high` | Review effort `medium` → `high` |
| `review_procedure` | `review_procedure_open_ended` | `SPEC_CRITIC_REVIEW_PROCEDURE=open_ended` | Review system prompt's procedure block |
| `review_scope_wording` | `review_scope_coverage_first` | `SPEC_CRITIC_REVIEW_SCOPE_WORDING=coverage_first` | Review system prompt's scope emission sentence |

Each candidate compares separately with the same baseline. Do not set all
three switches at once for the initial comparison. The existing EX-03
`review_effort` experiment remains a separate `medium` versus `xhigh` test;
its meaning and arm id are preserved.

## The open-ended procedure

The candidate replaces the numbered identify/check/check reasoning steps with
an instruction to reason about code/edition consistency and sibling sections,
schedules and defined terms together. It keeps section traversal, exact-quote
grounding, confidence assignment and the boilerplate exclusion. All other
prompt blocks, examples, output schemas and user messages remain the same.

`SPEC_CRITIC_REVIEW_PROCEDURE` is read when a request is built. Only
`open_ended` enables it. Unset, empty, `current`, `0`, `false`, `no` and `off`
keep the shipped prompt byte for byte. Unknown values warn once and use the
shipped procedure. Both batch and real-time requests and their repairs use the
same production builder. No prompt goldens should be regenerated for this
default-off experiment.

## Offline checks

From the repository root, validate the dataset and registry:

```sh
python -m evals.model_effort validate
python -m pytest -q tests/test_model_effort_experiment.py tests/test_review_prompt_evaluation.py
```

Probe each experiment without an API key or paid request, using a fresh state
root for each comparison:

```sh
python -m evals.model_effort probe-experiment \
  --experiment review_effort_high --state-root /tmp/review-high-probe
python -m evals.model_effort probe-experiment \
  --experiment review_procedure --state-root /tmp/review-procedure-probe
python -m evals.model_effort probe-experiment \
  --experiment review_scope_wording --state-root /tmp/review-scope-probe
```

Each probe launches isolated Python processes with inherited
`SPEC_CRITIC_*` overrides removed and separate state paths. It builds production
requests, then requires the changed fields to equal the candidate's declaration.
A no-op candidate or unrelated request change is refused. The probe includes
all fixed review fields and system cache metadata, initial/escalated verifier
shapes, and full cross-check, compliance and requirements-research request
fingerprints. The bare `probe` command instead shows the current process's
settings; use `probe-experiment` for a controlled comparison.

## Collecting responses after live access and a budget are authorized

No paid collection accompanies this PR. API credentials and an agreed spending
cap are required for a live comparison. With those configured, run **one**
experiment first on tuning cases, then in a different directory on held-out
cases. This example shows the `high` comparison; substitute one of the other
experiment ids to collect it independently.

```sh
python -m evals.model_effort run-experiment \
  --experiment review_effort_high --split tuning --repetitions 2 \
  --state-root /tmp/review-high-tuning-state --out /tmp/review-high-tuning \
  --max-spend-usd 10 --live
python -m evals.model_effort run-experiment \
  --experiment review_effort_high --split held_out --repetitions 2 \
  --state-root /tmp/review-high-held-out-state --out /tmp/review-high-held-out \
  --max-spend-usd 10 --live
```

Use fresh output/state directories for each experiment and split. Baseline
record filenames are shared across experiments; mixing output directories
would collide. Every arm/repetition runs in its own process with fresh cache
and state paths. Order alternates between repetitions. The preflight runs
before paid arms and saves `<experiment>.probes.json`, including request
shapes, changed fields and dataset/split digests. Reusing that probe file is
refused, and a subprocess failure stops subsequent arms.

The existing review runner uses **real-time transport**, not production's
default batch transport. Its cost and latency are real-time measurements;
they must not be presented as measured batch savings. Both transports use
the same request builder, covered by the offline request tests.

The cap is allocated across arm/repetition processes and checked between cases
by the existing runner. **A case, including repairs, can exceed its allocation
by its own cost.** This is a stopping allowance, not a hard billing limit.
Check attempt usage, unknown/unpriced usage, failures, repairs, unrun cases and
the JSONL findings before interpreting results. A partial run is incomplete
measurement. It must not support an adoption decision.

## Scoring without an adoption decision

```sh
python -m evals.model_effort score \
  --experiment review_effort_high --out /tmp/review-high-held-out \
  --split held_out --measurement-only
```

Use `--measurement-only` for **all three audit comparisons**, including the
existing scope-wording experiment. The two new experiment ids always defer
adoption even without that flag. The original EX-03 experiments keep their
historical decision rules for their own workflow; those rules are not the
audit's step 4 acceptance gates.

The existing scorer reports recall, severe recall, trap hits, unclassified
extras, operational failures, confidence bands and review-only cost/latency.
Token matches are provisional labels. Unsupported counts do not establish
precision while extra findings remain unclassified. Inspect the actual
captured findings for every arm and repetition; do not reuse finding indexes
across different outputs. The legacy case-level adjudication format is not a
substitute for the record-bound adjudication needed for adoption in step 4.
Pooled repetitions and template fixtures are not independent real projects,
so their statistical summaries do not establish real-world generalization.

The unchanged review dataset has 20 tuning cases (14 expected defects and
6 clean cases) and 8 held-out cases (11 expected defects and 1 clean case).
Held-out cases are constructed variants, not a representative project sample.
Any tuning work must stay in the tuning split. The package-level cases from
step 2 remain a separate `evals.package_review` comparison.

**No downstream verification is run by these review comparisons.** Any
increase in findings may increase verification spend or be rejected later.
Review-only recall and cost do not establish verified quality or total
pipeline savings. Step 4 must declare severity-aware quality gates before
looking at live results, bind adjudication to each captured record, and measure
review plus verification cost/latency before proposing adoption. Step 3 only
makes the separate comparisons runnable and checks their offline contracts.
