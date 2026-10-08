# EX-06 — Cross-chunk and cross-module coordination

| | |
|---|---|
| **Chunk** | S25 (plan Part 4 §27) |
| **Date** | 2026-09-29 |
| **Base** | master `9fdba29` (the merge of #406, Spec Critic 3.10.0, Anthropic SDK 1.7.0) |
| **Live evaluation** | Not authorized. The owner chose "offline only" at session start, and no API key was in the session's environment. |
| **Spend** | $0.00. No model request, count request, or batch was sent. |
| **Decision** | **Not evaluated.** An observation-only coordination pass is built behind `SPEC_CRITIC_CROSS_COORDINATION` (off by default): `candidates` records, at no API cost, the pairs of passages it would send; `observe` also asks the cross-check model about each pair. Its deterministic candidate stage was scored offline on constructed cases: on the held-out half it selected 8 of 10 conflicts and falsely joined 2 of 8 controls. The model stage has not met the live API. Nothing it finds reaches a report, and no default changed. |

## The question

Cross-check compares specifications only within the request it sends. When a module's package is too
large for one request, the chunk planner splits it by CSI division (and splits a division into parts
when it must), and nothing compares a spec in one chunk with a spec in another (CLAUDE.md, "Cross-check
chunking"). In a routed program each module's cross-check sees only its own partition, so a Division
21 sprinkler spec and a Division 28 fire-alarm spec are never compared, however small the program.

Can a bounded pass find conflicts across those boundaries — a fire-alarm spec against a sprinkler spec,
a feeder against the motor it serves — without drowning a reviewer in look-alikes, and without
claiming to have checked coordination it did not check?

## What the investigation found

- **Where the gap is, exactly.** Two kinds of pair are never compared today. A *cross-chunk* pair: two
  specs of one module that the chunk planner put in different requests (different CSI groups, or
  different parts of a split group). A *cross-module* pair: two specs routed to different modules. The
  cross-check result did not record which specs shared a request, so the gap could not be computed
  after the fact. It is recorded now (`ReviewResult.chunk_plan`, below).
- **What corpus evidence exists.** The repository holds no set of real cross-spec conflicts. The only
  evidence of what coordination problems look like is what the module authors wrote down as anchors:
  each module's cross-check severity examples and review categories. Counted by subject:

  | Subject | Cross-check severity examples (5 modules) | Review categories (mentions, all modules) | Deterministically readable? |
  |---|---|---|---|
  | Responsibility / division of work | 5 of 5 (both CRITICAL examples that name a specific conflict) | 16 | Yes: "furnished by Division 28", "Division 26 shall wire …" |
  | Ratings / capacity | 2 of 5 | 13 | Yes, when a quantity with a unit follows a named item |
  | Voltage / supply characteristics | 0 of 5 | 12 (CA equipment-schedule mismatches, electrical, fire alarm) | Yes: V, kV, VDC, phase, Hz |
  | Model numbers | 2 of 5 | — | No: free text with no reliable shape |
  | Zoning | 2 of 5 | 6 | No: zone correspondence is a mapping, not a value |
  | Sequences / interfaces | 3 of 5 | 34 | No: a sequence is prose, not a fact |
  | Dimensions / materials | 1 of 5 | 7 | Partly; deferred |

  The first three are chosen: they are named by the corpus, and they can be read without a model. The
  plan's other candidate categories (equipment identity, location, interface requirements, material)
  are recorded as not read, with the reason in the last column.
- **No precedent in the code for a program-level coordination pass.** Drawing impact is the one pass
  that runs once per program after every module's collection; the new pass follows it (same place,
  same deferral rule, same diagnostics recording).

## What is built

### The switches

| Switch | Values | Off |
|---|---|---|
| `SPEC_CRITIC_CROSS_COORDINATION` | `candidates`: read and select, send nothing. `observe`: also send the selected pairs to the model. No truthy shorthand, since the two differ in whether the pass spends; no value reports anything. | Unset, empty, `0` / `false` / `no` / `off`, or any unknown value (one warning). Off, no stage runs, no event is written, no stage is deferred. |
| `SPEC_CRITIC_CROSS_COORDINATION_SCOPE` | `module` (default; the plan's first stage): cross-chunk pairs within each module. `program`: also cross-module pairs. | Any other value is `module`, with one warning. |

### What the pass reads (`src/coordination/facts.py`, stdlib only)

Each specification's paragraph map, element by element, clause by clause. A **fact** is one statement,
in one clause, of one attribute of one subject: the text as written, the values normalized, each
value's qualifier (exact, minimum, maximum), the element it came from, the scope its clause states,
and every assumption the reader made (`uncertainty`).

- **Subjects** are identified two ways only: a hyphenated equipment tag (`FP-1`, `ATS-2A`, `UPS-A`;
  standards, codes, and document parts stoplisted; `FP-01` is not `FP-1`), or a closed vocabulary of
  23 item names whose only synonyms are documented equivalences (an abbreviation of the same term, a
  spelling variant, a term NFPA 72 renamed). "Fire pump" is never "jockey pump"; "emergency generator"
  is never "standby generator". A name written beside its tag ("AC-1 air compressor", "the fire pump,
  FP-1,") is that tagged item; a name standing alone stays a name. A value with no subject in its
  clause takes the subject the previous clause of the same element ended on, else the element's
  heading, and says so; a value that finds no subject is counted, not guessed.
- **Values.** Voltages (pairs such as 480Y/277 V are one statement), phase count, frequency; flow,
  pressure, horsepower, kilowatts, kVA, amperes. Conversions only where exact (gpm / L/s / L/min, psi /
  kPa / bar, kV / V). Horsepower and kilowatts are different attributes. Voltages compare only within
  one class (below 50 V, to 1 kV, above), and a motor nameplate voltage is compatible with the nominal
  system voltage it serves (NEMA MG 1 / ANSI C84.1: 460↔480 and so on). A role word keeps two values
  apart (churn vs rated pressure, 150% overload vs rated flow, frame vs trip current, standby vs prime
  kW).
- **Responsibility.** Passive ("furnished by Division 28 and installed by Division 23"), active
  ("Division 28 shall program …"), and a verbless form only when a work noun says what is assigned
  ("Wiring of tamper switches under this Section"). "Provide" is furnish and install; a work noun
  before the verb ("power wiring … shall be provided by Division 26") assigns the wiring. A section
  number is its division; "this Section" is the file's own division; "others" is no party.
- **Scope.** Construction phase ("Phase 2", never "3-phase" or "three phase 60 Hz"), building, data
  hall, and status (existing, future, temporary, relocated, alternate) — status only when it stands
  before the subject, since "connect the new panel to the existing system" says nothing about the new
  panel.
- **Bounds.** At most 400 facts per specification (the rest counted); passages windowed to 1,200
  characters around the value.

### Which pairs become candidates (`src/coordination/candidates.py`, stdlib only)

A pair of specifications is *co-analyzed* when a planned cross-check request contained both, whatever
became of that request (a failed request is the cross-check's own reported gap). Every other pair in
scope is new. A module whose cross-check result did not record its plan contributes no pair and is
listed as unassessed.

Two facts join only when they share a category, attribute (roles included), and subject; their scopes
can be the same (no stated phase, building, or hall differs; the status matches); and their values
cannot both hold (after the voltage mapping; bounds compared as bounds; a 1% allowance only across a
unit conversion; two divisions or two named contractors, never a division against a contractor).
Counted but not joined: pairs co-analyzed, out of scope, scope-separated, and agreeing.

Candidates are ranked (responsibility, then electrical, then rating; tagged before named; fewer
assumptions first) and bounded: at most 40 per pass and 4 per pair of specifications. Everything past a
bound is recorded with its reason. Candidate ids are content-derived and order-independent (`co-` + 12
hex).

### The model pass (`src/coordination/adjudication.py`)

Up to 10 candidates per request, each with both passages (escaped), the file, element, module, and
heading of each side, and what the reader thought each side states. One engine-owned system prompt
(identical for every module and run, so it caches), with an example of each assessment; a strict tool
(`submit_coordination_observations`) on whitelisted models; no web tools; the cross-check model
(Sonnet 5.5) at `high`, the cross-check's effort when this was written (cross-check moved to `medium`
on 2026-10-08; this experiment kept `high`). The phase is registered (`PHASE_COORDINATION`: 16k
output cap, system and tools cached).

An observation names its candidate id (ids not sent are dropped and counted) and one of `conflict`,
`not_conflict`, `cannot_tell`. A `conflict` must quote each side — words found in that side's passage,
whitespace-tolerant, never case-tolerant — and say why both passages refer to the same item in the
same scope; one that fails is recorded as `cannot_tell` with the reason. A candidate a response left
out is recorded as not assessed.

The request follows the package-pass contracts: sized before it is sent (`RequestBudget`, never
truncated); the shared retry schedule on the no-retry client; one re-request for an unparseable
payload; the per-call permit around each stream only; one attempt record per paid request (unknown
usage for one that raised before its response was read).

### Where it runs (observation only)

Last in the collection, after drawing impact: the headless driver and the GUI's single-module
collection call `pipeline.run_coordination_for_batch`; a routed program runs the pass once at program
level (`program_pipeline._run_program_coordination`) and its children skip it. While a review repair is
outstanding the pass is deferred with the other paid stages (`STAGE_COORDINATION`, named only when the
switch is on). With cross-check disabled it records a skip and reads nothing. In a routed program, a
module the run assigned specifications to but has no collected result for (its collection failed, or
it was never submitted) is listed as not assessed, so the pass never reads as complete over part of a
program.

What it records goes to diagnostics only: one summary event (status, mode, scope, counts, what was not
assessed, each module's cycle label and governing-basis fingerprint) and one event per candidate or
observation (both sides, bounded to 100 per pass), rolled up as `coordination` in the summary with a
`to_text` line. A pass that spent is recorded through `record_pass_api_call` and priced once from its
attempts (category "coordination (experiment)"); a pass that did not is an ordinary event.

**What it does not do**, each tested: add a finding, an edit, a report line, or a sidecar entry (the
sidecar payload and every occurrence id are byte-identical with it on and off); verify an observation
(each side carries its module and governing-basis provenance so a later stage could verify it under its
own module, and nothing uses a default module); replace or shorten any per-specification review.

### The one change outside the experiment

`ReviewResult.chunk_plan`: the chunked cross-check records which specifications each planned request
held — one entry for the whole package on the single-call path, `[]` when nothing was planned, and
`None` (not recorded) on every other result. Runtime only; nothing but the experiment reads it.

## The candidate stage, measured offline

`evals/coordination_dataset.py`: small sets of specifications, each document individually plausible,
with a declared cross-check plan per module and labeled pairs:

- **conflicts**: two elements that state one requirement for one item in one scope two incompatible
  ways; the stage should select them. A miss is a *missed conflict*.
- **controls**: two elements that look alike and are not a conflict — different equipment, a
  different phase or building, an existing and a new item, compatible values, attributes that are not
  the same attribute, a pair cross-check already compared, a pair outside the scope. A selection is a
  *false join*.

The passages are constructed (written for this set; illustrative tags, divisions, and values; no
published document quoted).

**Tuning (29 cases, 14 conflicts, 16 controls)**, written with the rules. The first pass selected 10
of 14 conflicts; three misses shared one cause (a name written beside its tag was read as two
subjects, and a schedule row was read value by value), which became two rules. Two cases were added
for weaknesses expected in advance. Final: 13 of 14 conflicts (0.93, 95% 0.69–0.99), 1 of 16 false
joins (0.06, 0.01–0.28), no unlabeled candidates.

- Missed **t22a**: one pump named by tag in one spec ("the fire pump, FP-1") and by name in the other
  ("fire pump feeder"). Equating a tag and a bare name needs a mapping the documents do not state; the
  rule does not guess.
- False join **t28c**: two different compressors that share the name "air compressor" and state no tag
  or scope. The stage is loose by design here; this is the pair the model is asked to reject.

**Rules frozen at `cx1`** (2026-09-29T22:21:26Z): `facts.py`
`47381ee9a29f9a23aebe873c0a6b6aa0dd1e7449c67d21fdd98f2566138c0e9c`, `candidates.py`
`c54e511973c28036aea2b72c154c824d677f7f602b2f77ef501311eb662510f8`. That state is commit `e149b37`,
whose dataset has no held-out cases; the held-out cases were added in the next commit, `251485b`. The
only later change to `facts.py` corrects a docstring sentence about the corpus evidence.

**Held-out (18 cases, 10 conflicts, 8 controls)**, written after the freeze with new equipment, tags,
phrasings, and file names, and scored once. `evals.coordination.RECORDED_HELD_OUT_RESULT` pins it and a
test reproduces it exactly.

| Measure | Held-out | 95% Wilson interval |
|---|---|---|
| Conflicts selected | 8 / 10 (0.80) | 0.49–0.94 |
| Controls falsely joined | 2 / 8 (0.25) | 0.07–0.59 |
| Candidates matching no label | 0 | — |

The four misses, each a candidate rule change for `cx2` (to be judged on new held-out cases, never on
these):

| Case | What happened | Cause | Direction |
|---|---|---|---|
| h02a | Releasing solenoid valves wired by Division 28 in one spec and under Section 21 13 19 in the other: not selected | "Solenoid valve" is not in the subject vocabulary | Missed conflict |
| h17a | Door holders furnished under Section 08 71 00 and by Division 28: not selected | "Door holder" is not in the vocabulary | Missed conflict |
| h08c | "The fire protection contractor" and "the sprinkler contractor" installing heat tracing: joined | Two contractor names are compared as two parties; they are usually one firm | False join |
| h10c | A UPS's output voltage (208Y/120 V) against another spec's input feeder (480 V): joined | Voltage roles (input, output, primary, secondary) are not read | False join |

The vocabulary misses are the expected shape of the rule's weakness: a closed list recalls only what it
names. Growing it should follow the corpus (stage 0 below), not the cases that exposed it.

### What this does and does not show

It shows that the candidate rules behave as written on constructed cases, including cases written after
they were frozen, and it names four specific failures. It does **not** show how often real
specifications state coordination facts in a shape the rules read, how many candidates a real package
yields, or whether the model judges a pair correctly — both splits were written by the rules' author,
and no model call was made. Stage 0 of the protocol answers the first two at no cost.

## Cost, bounded rather than measured

`candidates` makes no request. `observe` makes at most 4 requests per pass (40 candidates, 10 per
request), each of at most 3 attempts (transient retries and the one parse re-request included), each
attempt capped at 16,000 output tokens, with an input of two passages of at most 1,200 characters per
candidate plus a 3,428-character system prompt. At Sonnet 5.5 list prices ($2 / $10 per million), the
worst case — every request at its output cap, every attempt used — is about $2 per pass; a pass that
finds no candidate costs nothing. Typical cost and latency are **not measured**, and the plan's warning
applies: no "few percent" claim without a measurement.

## Configuration

| Item | SHA-256 |
|---|---|
| `src/coordination/facts.py` (final; differs from the freeze by one docstring sentence) | `17dede29dbf44f42c7b7b8a2096a28bf6883b2457ddd23bd3c7a39494d7b6776` |
| `src/coordination/candidates.py` (unchanged since the freeze) | `c54e511973c28036aea2b72c154c824d677f7f602b2f77ef501311eb662510f8` |
| `src/coordination/adjudication.py` | `cc938d50a0883c99a29fb8586516ae30d857670f30ae5a7917ab18b75fcdcfb6` |
| `src/coordination/runner.py` | `3c89f15e09f17dfa96c29e88302ffc0d866cb80d4874eecb09fd6b04d2d77aeb` |
| The adjudication system prompt (`build_system_prompt()`) | `4e5f7836aac3478a24a147f136b5b6edbbc79ed3ec508deabb4beaeaafbd8ed2` |
| `COORDINATION_SCHEMA` (sorted JSON) | `a8350b831f1bd629e32d4edaa72e6602563ede68b05a66f19f536bb7d6347ba4` |
| Tuning split (`dataset_digest("tuning")`) | `0485f4ee460e3da717b4d0ca14aa283083dab0adc1b2eaa5e5af1a8b5ee6c503` |
| Held-out split (`dataset_digest("held_out")`) | `be35341f8d16da06c74c873f20e7438d178d51fc72b2c3254c8313eec6e26e49` |
| Whole set (`dataset_digest()`) | `955ee55747c065364c5f4fada72a2a778aaf66eaefb99e3d9ea68fc659fa5981` |

## Protocol (not run)

`evals/coordination.py` holds it as data (`EVALUATION_PROTOCOL`), fixed before any live result exists.

- **Stage 0 — candidates on ordinary runs (no spend).** Run reviews with
  `SPEC_CRITIC_CROSS_COORDINATION=candidates`, first with the module scope, then the program scope, and
  keep each diagnostics export. Measure candidates joined, selected, and deferred per run by kind and
  category, and facts and unattributed values per specification. A person adjudicates every selected
  candidate from `python -m evals.coordination items EXPORT --markdown`: same item and scope (yes / no /
  cannot tell) and conflict (yes / no). At least 30 adjudicated candidates per category before stage 1
  spends on that category.
- **Stage 1 — observe within one module (budget).** `observe`, module scope, over the constructed cases
  (`python -m evals.coordination run --live --max-spend-usd N --out FILE`) and over real packages whose
  cross-check chunked. Measure missed conflicts (a labeled conflict judged anything but conflict), false
  joins (a control or a person-adjudicated non-conflict judged conflict), the `cannot_tell` rate, the
  validation demotions, and the incremental cost and latency from the attempt records.
- **Stage 2 — observe for a small program (budget).** `observe`, program scope, a hyperscale package of
  5 to 15 specifications across at least two modules; the same measures, cross-module candidates
  reported apart.
- Minimum sample: 50 adjudicated observations per category and stage. Stop at the spending cap or the
  sample minimum, whichever comes first.

## Promotion criteria (fixed now)

- Observation never becomes a default. A category (responsibility, electrical, rating) may be reported
  only when all of these hold, category by category:
  - stages 1 and 2 have both run for it;
  - its false-join rate on conflict judgments is at most 0.10, with a 95% Wilson upper bound at most
    0.20, and no controlled false join of a different phase, building, or existing and new item;
  - its missed-conflict rate is reported (no threshold: the candidate stage bounds recall, and the
    report must say what was not compared);
  - each reported conflict's two sides are verified under their own modules' governing bases — built
    and tested first; nothing verifies an observation today;
  - the incremental cost per run is measured and stated here.
- Never: promote the whole pass at once; claim complete coordination (the report must keep naming what
  was not compared); let a digest replace the per-specification review.

## Offline tests

`tests/test_cross_coordination_experiment.py` (165 tests):

- the switches (no truthy shorthand, no reporting value, one warning per unknown value) and the phase,
  operation, and stage registrations;
- off is inert: the stage returns the state untouched, no event, no deferred stage, no summary key, no
  cost category; the old deferred-stage list is unchanged;
- the chunk plan on every cross-check path, and "not recorded" elsewhere;
- the fact reader: tags and the stoplist, leading zeros, longest match, synonyms (each justified),
  case-sensitive abbreviations, name-beside-tag, scope (phase vs supply characteristics, case-sensitive
  building ids, status before its subject only), voltages and classes, kVA not volts, phase and
  frequency, conversions, hp vs kW, case-sensitive amperes, bounds, roles, merging a clause's values,
  every responsibility form, carry-over and heading subjects, schedule rows, counted values and limits,
  content-derived ids, windowed passages;
- value comparison (the voltage mapping, bounds, the conversion allowance, comparable parties);
- candidate selection: pair kinds, co-analysis in any module, unrecorded plans, joins and non-joins,
  one-sided scope, order-independent ids, priority, both bounds with their reasons, provenance of a
  file in two modules;
- the model pass with scripted clients: request shape (strict tool, cache breakpoints, no web tools),
  the strict-subset schema, escaping of hostile passages, every validation rule, the text fallback, an
  incomplete stop, a transient retry (unknown then known attempt), the one parse re-request, a refused
  request, an oversize request never sent, the permit around each call only;
- the runner: candidates mode sends nothing; module scope; failed-review files; no text; batching with
  a failed request and an omitted candidate; every request failing; an internal error never escaping;
  items with both sides and provenance; bounded summaries; deferred candidates listed as not assessed;
- diagnostics: candidates mode is not an API call; observe is priced once (twice recorded, once
  counted); items capped and every event under the byte cap;
- the drivers: the stage on and with cross-check disabled; finalize carries the record; the headless
  driver off, on, as a program child, and provisional; the program driver off and on; the program
  sidecar and occurrence ids identical on and off; program deferral; a module whose collection failed,
  or that was assigned specs but never submitted, listed as not assessed;
- the dataset (sound; validation catches each problem) and the harness (the held-out score reproduces
  exactly; the tuning score; category-aware matching; Wilson intervals; reading an export both ways;
  every refusal of a paid run; stopping at the cap and never overwriting; the CLI).

**Mutation check.** 55 deliberate breakages, each run alone against the test module; all are caught.
Four survived the first run, each exposing a missing test, and each was caught after adding one:

- the supply-characteristic guard on construction phases ("single phase 2 pole");
- status read for the whole clause during extraction (tested only on the helper);
- an order-dependent candidate id (only ever exercised through sorted inputs);
- deferred candidates missing from the runner's not-assessed list.

The breakages, by part: the stoplist, leading zeros, longest match, name-beside-tag, abbreviation case,
the phase guard, status scope, the voltage mapping, voltage classes, roles, bounds, the conversion
allowance, party comparability, the verbless form, "this Section", merging, heading subjects,
carry-over, schedule rows, work nouns; co-analysis, module scope, scope conflicts, unrecorded plans,
the per-pair bound, id order, agreement, deferred candidates; quote validation, unknown ids, the parse
re-request, unknown attempts, the budget check, escaping, retrying refused requests; candidates mode
sending, failed files, the not-assessed list, exception containment, failed requests' candidates,
pricing candidates mode, the item cap; the stage off, deferral off, program children, the headless
call, cross-check disabled; both chunk-plan paths; the rollup; category-aware scoring.

- **Found by the Codex review on #410, then fixed:** when one module of a routed program failed
  collection, the program-level pass read only the modules that succeeded and listed nothing as
  unassessed, so it could report itself complete over part of the program. The program stage now
  passes each assigned module without a collected result (its error, or "not submitted") to the pass
  as a not-assessed note. Four tests and four breakages cover it (the notes dropped, the call site not
  passing the errors, the runner ignoring them, an unsubmitted module ignored).

## Rollback

Both switches are off by default and neither writes anything persistent: no cache, pending-batch, or
sidecar shape moved; the chunk plan and the pass's record are runtime only. Unsetting the switch
restores byte-identical requests, results, events, and summaries.
