# EX-05 — Requirements-research reuse

| | |
|---|---|
| **Chunk** | S24 (plan Part 4 §26) |
| **Date** | 2026-09-29 |
| **Base** | master `c26b89d` (the merge of #403, Spec Critic 3.10.0, Anthropic SDK 1.7.0) |
| **Live evaluation** | Not authorized. The owner chose "offline only" at session start, and no API key was in the session's environment. |
| **Spend** | $0.00. No model request, count request, or batch was sent. |
| **Decision** | **Not evaluated.** A research cache is built behind `SPEC_CRITIC_RESEARCH_CACHE` (off by default). Its key, freshness rules, validation, refresh path, and provenance are tested offline. Hit rate, saving, and inappropriate reuse have not been measured, and no default changed. |

## The question

Location research is the one phase that spends money before a review is submitted. It runs one
web-search conversation per research dimension of every location-aware module. A project reviewed
again (after an addendum, say) asks the same questions of the same places. Can a completed
requirements profile be reused on a later run of the same project without going stale, and without
being applied to a project, client, module, or set of specifications it does not fit?

## What the investigation found

- **Where research runs.** `pipeline.prepare_batch_review` → `_run_research_phase`, once per module
  per run, before spec preparation, so its rendered profile is inside the Project Context that
  preflight counts and the batch submits. Resume and recovery never re-run it: the profile rides the
  pending-batch record.
- **What research reads.** The project profile (city, state or province, country, client), the
  module's research persona and dimension briefs (formatted with the module's code basis), and the
  corpus signals scraped from this run's specifications (client document names, risk-consultant and
  insurer mentions, edition-governance sentences, standards cited with edition years). The web-search
  tool is steered by the project's location. Research does **not** read the operator's Project
  Context, the drawing digest, or the verification basis.
- **What it costs.** The budgets are module data, not measurements. `datacenter_fire` has 4
  dimensions with up to 64 searches and 22 fetches; `datacenter_architecture` has 4, up to 70 and 24;
  `datacenter_electrical` has 5, up to 90 and 31; `datacenter_electronic_safety_security` has 5, up to
  90 and 31. The Hyperscale program runs all 18 dimensions, with up to 314 searches ($3.14 in search
  fees at $10 per 1,000) plus tokens. Actual spend is below those ceilings, but by how much has not
  been measured.
- **Prerequisites hold.** Research accounting is correct since WP-15: each dimension is recorded once,
  with its usage summed over every response it read, retried attempts included. The profile's
  serialization is stable: `to_dict` / `from_dict` round-trip, `render_text` is deterministic and
  pinned by a golden, and item ids are content-addressed (`r-` + a digest of dimension, category, and
  requirement).
- **Nothing measured repetition.** No diagnostics export or trace in the repository records two runs
  of one project, so the hit rate, the thing that decides whether reuse is worth its risk, is unknown.

## What is built

### The switches

| Switch | Values | Off |
|---|---|---|
| `SPEC_CRITIC_RESEARCH_CACHE` | `reuse`: look up; on a miss, research and store a completed result. `refresh`: research again whatever is stored, then store it. There is no truthy shorthand, because the two differ in whether a stored profile may be used. | Unset, empty, `0` / `false` / `no` / `off`, or any unknown value (one warning). Off never reads or writes the cache file, and makes the same runner call as before. |
| `SPEC_CRITIC_RESEARCH_CACHE_MAX_AGE_DAYS` | Whole days, 1 to 90; default 30 | Anything else is the default, with one warning. `0` is refused: "never expire" is exactly what research must not do. |
| `SPEC_CRITIC_RESEARCH_CACHE_PATH` | The file; default `~/.spec_critic/research_cache.json` | — |

### The key is the requests

`requirements_research.research_reuse_key` builds every dimension's first request with
`build_dimension_request`, the builder the fan-out now sends from (the refactor is byte-identical, and
a test compares every sent request with the builder's output). The key is a digest over these
components:

| Component | Covers |
|---|---|
| `project` | City, state or province, country, and client, exactly as the profile holds them |
| `module_id` | The module |
| `corpus_signals` | The rendered corpus-signal block: explicit edition constraints and client documents the specifications name |
| `model` | The research model (an override changes it) |
| `cycle_label` | The module's code basis |
| `dimension_ids` | The dimensions, in order |
| `system_prompt` | The research persona and the engine protocol |
| `user_messages` | Every dimension's message: the project header, the brief formatted with the code basis, and the corpus signals |
| `tools` | The research schema (and whether it is strict), and the web-search and web-fetch tools with their budgets, blocklist (the source policy), and the project location |
| `request_settings` | Output cap, thinking, and effort |
| `date_basis` | `current_at_research` (see below) |
| `policy_version` | `rr1`, the parse, grounding, merge, and date rules this build applies |
| `app_version` | The app version, so a release that changes those rules without a policy bump still misses |
| `max_continuations` | The continuation cap, which decides whether a heavy dimension completes |

A test fails when a request field appears that the key does not cover (`KEY_FORM_FIELDS`).

**Nothing is normalized into an assumed equivalent.** "Ashburn" and "ashburn" are two keys, as are
"ExampleCo" and "Exampleco". The only equivalences are the ones `ProjectProfile` already applies: it
trims fields and folds a known country alias ("USA" → "US").

**Two request differences are left out, on purpose.** A deep trace's `thinking.display` changes what
a trace can see, and `cache_control` changes where a cache breakpoint sits. Neither changes what
research concludes, so neither may split the key.

**Project Context is not in the key, because research never reads it.** The plan requires that a hit
must not silently override newly supplied project constraints. The operator's Project Context is
where such constraints are supplied, and the cache cannot touch it. A reused profile is spliced after
the operator's text exactly as a fresh one is, and the operator's text is never replaced or
reordered (tested with a changed context between two runs). A fresh run would not see a new Project
Context constraint in research either. The constraints research does read (the project, the client,
and what the specifications say) are all in the key.

### Dates: the effective date basis

Research asks for *current* requirements, so a profile is as-of the day it was researched
(`current_at_research`). A future project-date input, such as a permit-application date, would be a
different basis and a different key. Freshness is checked at lookup, not keyed. An entry is reused
only when both of these hold:

1. **Its age is within the limit** (default 30 days). A 30-day-old entry is reused; one a second
   older is stale.
2. **No date it names has begun since it was researched.** Every item's requirement, notes, topic,
   code reference, and authority are read for full dates (`January 18, 2027`, `18 January 2027`,
   `2027-01-18`, `1/18/2027`), months (`March 2027`), and bare years (`2027`). A period counts when
   its first day is after the research day and on or before today: the research described it as
   ahead, and it no longer is. A numeric date is read both ways (month/day and day/month; the profile
   may be Canadian), and either reading can retire the entry. A later year named on its own retires
   the entry when that year begins, even when a later full date in the same text is still ahead. That
   is the conservative direction.

A calendar period is never treated as equivalence: nothing is reused "because it is the same month".
One gap is recorded rather than guessed at. A month named in the research's own month ("later in
September") starts before the research day and is not counted, so the age limit is what bounds that
case.

### What is stored, and what never is

An entry holds the completed profile (items, dimension statuses, research date, project), the key's
component digests, the project and module it is for, its creation and last-used times, a digest of
the profile, and the research usage it replaced: dimension calls, API requests, searches, fetches,
and tokens, counting the responses of attempts abandoned for a retry (they were billed too). It never holds the prompts, the corpus signals (only their digest, since they are
excerpts of the specifications), or any specification text. A test plants a marker in the corpus
signals and finds it nowhere in the file.

- **Only completed research is stored.** A partial profile (any dimension failed) or a failed fan-out
  is never stored, and a stored row that claims otherwise is invalid. Partial-profile reuse is not
  supported. If it is added later, it needs its own rule, and per-dimension reuse is the likely shape.
- **Bounds.** At most 50 entries (least recently used first out), and at most 1,000,000 bytes per
  serialized profile.
- **Every row is validated on load**, on its own, and an invalid row is ignored and counted. A row is
  invalid when its key does not match its components or its slot, its components are not this
  policy's set, its project or module does not match the key, its profile does not match its digest,
  its creation or last-used time is missing, not finite, or more than an hour in the future, its
  profile is not completed or not for its key's dimensions, its research date disagrees with its
  profile or with its creation time by more than a day, a nested item or status field has the wrong
  type (so a row that validates always deserializes), its usage record is malformed, or it is over
  the size bound. Should a validated row still fail to deserialize, the hit becomes a miss. Valid rows beside it still load, and the next write drops the invalid ones. A store
  runs the same validation first, so the writer can never write a row the reader rejects.
- **A file this build cannot use is never overwritten.** That means unparseable JSON, another schema
  version, or no entry table. The lookup reports `unreadable`, research runs, and the store is refused
  (`unreadable_file`). `python -m evals.research_reuse delete --all` removes such a file on purpose.
- **Concurrency.** One in-process lock serializes every read-modify-write, so a routed program's
  modules, which prepare concurrently, cannot lose each other's entries (tested with eight concurrent
  stores). Two processes are not coordinated: each write is atomic (a temporary file, then a replace),
  so the file is never torn, but the later writer's view wins.

### Refresh, and failures of the cache itself

`refresh` researches again, records `refresh` (not a lookup), and replaces the stored entry. The key
builder, the lookup, and the write can each fail. None of those failures fails a run: each is logged,
recorded (`key_error`, `unreadable`, `write_failed`, `invalid`), and research runs as it would without
the switch. A failed fan-out still raises, and nothing is stored.

### A reuse is never silent

- **The profile carries a `reuse` record**: source, policy version, key, when it was researched, its
  age in days, the age limit, when it was reused, and the date basis. `to_dict` writes it only when
  set, so a fresh profile's dict is byte-identical to before. It rides every copy of the profile: the
  pending-batch record (so a resumed run still says so), both reports, and `.profile.json`.
- **The run log** names the project, client, module, research date, and age, and the dimension, item,
  and grounded counts. It says that no research call was made, that requirements changed since then
  are not reflected, and how to refresh. It then lists up to five grounded governing-code facts the
  reused research establishes, which is the governing basis the operator is accepting.
- **Both reports.** The Jurisdiction & Client Requirements section adds, in amber: "This research was
  reused from the research cache, not re-run for this review: it was researched *date* (*N days*
  before this run) for the same location, client, module, and research inputs. Requirements that
  changed since then are not reflected. Set SPEC_CRITIC_RESEARCH_CACHE=refresh to research again."
  The Run Diagnostics row "Location/client research" appends "reused from the research cache
  (researched *N days* before this run)". A program report counts the reusing modules and shows the
  oldest age (an age is never summed). Both exporters share one helper, `_research_banner_row`, and
  one sentence, `research_cache.reuse_notice`.
- **The text the reviewers see does not change.** The rendered profile block keeps its original
  "researched *date*" line, so a hit gives the review the same Project Context the original run
  gave it.

### Accounting

A hit makes no request, so it adds no attempt and nothing to the cost estimate. Each decision is one
diagnostics event with `api_call: False`, and a test shows the cost summary does not move. The
additive `research_reuse` rollup, present only when the cache recorded a decision, gives lookups by
outcome and mode, the hit rate, which components differed on a miss, stale reasons (age or named
date), store outcomes, rejected rows, and what the hits saved (`saved`, and `saved_by_model` for
pricing). Saved figures are what the stored research cost when it was first run, not a measurement of
the reusing run, and the rollup says so. The EX-03 harness's arm environments now point
`SPEC_CRITIC_RESEARCH_CACHE_PATH` into each arm's directory, so an evaluation arm can never touch the
operator's file.

## The key matrix (offline, real modules)

`python -m evals.research_reuse matrix` builds the key with the real request builders for a base
project (`datacenter_fire`; Ashburn, VA, US; client ExampleCo; a corpus block citing NFPA 13 (2022)
and Owner Design Standards Rev 4) and changes one input per row. Every row behaves as designed:

| Case | Change | Same key? | Components that differ |
|---|---|---|---|
| `same_inputs` | The same inputs again | yes | — |
| `country_alias` | Country entered as "USA" instead of "US" | yes | — |
| `city_case` | City "ashburn" instead of "Ashburn" | no | project, user_messages, tools |
| `city` | Another city in the same state | no | project, user_messages, tools |
| `state` | Another state | no | project, user_messages, tools |
| `country` | Another country | no | project, user_messages, tools |
| `client` | Another client | no | project, user_messages |
| `client_case` | Client "Exampleco" instead of "ExampleCo" | no | project, user_messages |
| `module` | Another module (electrical) for the same project | no | module_id, cycle_label, dimension_ids, system_prompt, user_messages, tools, request_settings |
| `corpus_edition` | The specifications now cite NFPA 13 (2025) | no | corpus_signals, user_messages |
| `corpus_client_document` | The specifications now name Owner Design Standards Rev 5 | no | corpus_signals, user_messages |
| `corpus_none` | No corpus signals | no | corpus_signals, user_messages |
| `model` | Research model overridden to Sonnet 5 | no | model, request_settings |
| `dimension_brief` | One dimension's brief reworded | no | user_messages |
| `dimension_budget` | One dimension's search budget raised | no | tools |
| `persona` | The research persona reworded | no | system_prompt |
| `deep_trace_display` | A deep trace asks for summarized thinking | yes | — |
| `cache_ttl` | The cache breakpoints' TTL changes | yes | — |

The tests add, through the real builders: a blocklist change, strict tool use off, an effort change,
an output-cap change, the policy version, the app version, the continuation cap, and another code
basis. Each splits the key and names its component.

## What is not built

- **Partial-profile or per-dimension reuse.** Only a completed profile is ever reused. Reusing the
  completed dimensions of a partial run, and re-running only the failed ones, would need its own
  applicability rule. It is recorded here as a later candidate.
- **A GUI control.** The switch and its refresh are environment settings, like every other
  experiment. A per-run "Refresh research" control belongs to promotion, if it comes.
- **Cross-process locking.** It is described above. Writes are atomic, and the later writer's view
  wins.
- **A project date.** The only date basis is "current at research". A permit-application or
  code-effective date input would be a new key component and a new basis.

## Configuration

| Item | Value |
|---|---|
| `research_cache.POLICY_VERSION` | `rr1` |
| `research_cache.FILE_SCHEMA_VERSION` | `1` |
| Base matrix key (3.10.0, Sonnet 5.5 research) | `9ce8ad37c3f094619498c1b9d370c69d3a228d355f533b6408675197b0729d1f` |
| `src/research/research_cache.py` | `b644cf18126f398c935206adbd83f96a1415523f2211349d157cff94d3053c48` |
| `evals/research_reuse.py` | `e64c488726fe48db306c78c0dae024e9864dd97a5645479aaefef9acb3e80f83` |

The base key includes the app version, so it changes with every release. Reproduce it with
`python -m evals.research_reuse matrix`.

## Protocol (not run)

`evals.research_reuse.EVALUATION_PROTOCOL`, fixed before any run:

- **Stage 1: hit rate and saving on ordinary repeated runs. No extra spend:** a miss researches
  exactly as a run without the switch would, and a hit makes no request. Run representative repeated
  projects with `SPEC_CRITIC_RESEARCH_CACHE=reuse` and a cache file of the experiment's own. Keep
  every diagnostics export, then run `python -m evals.research_reuse measure EXPORT.json ...`. It
  reports the hit rate with a Wilson interval, the components behind each miss, the stale reasons, and
  the saved calls, searches, tokens, and cost. The cost is a lower bound: cache writes are priced at
  the five-minute rate, the cheaper one, because the stored usage keeps no TTL split and a saving must
  not be overstated.
  **Gate:** at least 20 lookups across at least 5 projects, and a hit rate whose Wilson lower bound is
  at least 0.20. Otherwise, record "rejected — too few hits".
- **Stage 2: inappropriate reuse. Needs a budget.** For each sampled hit, research again on the same
  day with `refresh` and a separate cache file. For each project, also run fresh research twice on one
  day, as a control that shows how much two runs of the same inputs differ anyway. Then run
  `python -m evals.research_reuse diff REUSED FRESH`. It lists the controlling items (grounded, not
  process advisories) and the governing references only one side has. A person adjudicates each
  controlling difference as one of: changed since the reused research (an inappropriate reuse),
  run-to-run variation (the control shows the same kind of difference), or cannot tell. Minimum: 30
  adjudicated hits across 5 projects and 2 modules. Stop at the minimum or the spending cap.

## Promotion criteria (fixed now)

Default on only if all of these hold:

- stage 1 passes its gate;
- stage 2 finds **zero** inappropriate reuses among controlling requirements, with the Wilson upper
  bound of the inappropriate-reuse rate across all adjudicated hits at most 0.10;
- the saving is reported in calls, searches, and lower-bound cost, not only as a hit rate;
- the age limit and date rules stay as measured (a longer limit is a new evaluation).

Never, whatever the result:

- reuse a partial or failed profile;
- treat a calendar period as proof that requirements are unchanged;
- hide a reuse.

`SPEC_CRITIC_GOVERNING_BASIS_CONTEXT` stays off unless it is evaluated separately.

## Measurements

| Measure | Value |
|---|---|
| Hit rate on repeated projects | not measured |
| Saved dimension calls, searches, tokens, cost | not measured |
| Inappropriate reuses (adjudicated) | not measured |
| Miss reasons on real repeated runs | not measured |

Offline results establish behavior only. The key matrix is above. The offline tests are below.

## Offline tests

`tests/test_research_reuse_experiment.py` (see its docstring for the sections) covers:

- the switches and the age limit;
- off is byte-identical: the same runner call, no file, the same profile dict, no diagnostics or
  report change, and every sent request equal to the builder's;
- the key's components are digests of the requests actually sent, and every request field is keyed;
- the matrix, plus eight settings split through the real builders; a deep trace's display is really
  sent and still not keyed; the key takes no Project Context;
- reuse without a research call, with its provenance and log;
- every changed input misses and names the reason;
- partial and failed research are never stored;
- the age boundary, and 14 named-date cases;
- refresh;
- every corrupted-row kind rejected and counted, with a valid row beside it still used;
- an unusable file never overwritten; write, lookup, and key failures never failing a run;
- the LRU bound and eight concurrent stores; nothing but the profile and digests on disk;
- the pipeline: no call on a hit, the operator's context first, a patched runner still intercepting
  the miss, the record surviving pending state, and two modules sharing one file;
- both reports, the banner, program aggregation, `.profile.json`, and diagnostics (never priced);
- the harness: measure, the lower-bound pricing, diff, the CLI, and the EX-03 arm paths.

**Mutation check:** 57 deliberate breakages were each applied to a copy of the repository, and the
suite had to fail. Examples: a key component dropped, casefolded, or left unstripped; the age limit
ignored or off by a day; named dates ignored, compared wrongly, or read one way; every load check
disabled in turn; an unreadable file overwritten; no lock; the LRU inverted; provenance, the report
notice, the banner, or the switch broken; a reuse event priced; refresh doing a lookup; the harness
pricing writes at 2x or treating every item as controlling. The first pass left three survivors,
each a gap in the tests: the unkeyed-field tripwire checked only one direction, a future-time rule
was masked by the research-date check, and the diff test had no non-controlling items. Each got a
test, and all 48 were caught. The Codex review on #406 then found two real gaps, both fixed with
tests and nine more breakages (all caught): the stored usage counted only the attempt that produced
the result, not attempts abandoned for a retry; and row validation checked only the profile's outer
shape, so a self-consistent row with a malformed nested field could raise while deserializing and
abort the run.

## Rollback

Unset `SPEC_CRITIC_RESEARCH_CACHE`. Off never reads or writes the file. Delete
`~/.spec_critic/research_cache.json` (or `python -m evals.research_reuse delete --all`) to forget
every stored profile. A pending-batch record or `.profile.json` written while it was on keeps its
`reuse` record, which older and newer builds read as an ordinary additive key. Nothing else is
persisted: no verification-cache, pending-batch, or sidecar schema moved.
