# EX-04 — Evidence validation and source reuse

| | |
|---|---|
| **Chunk** | S23 (plan Part 4 §25) |
| **Date** | 2026-09-29 |
| **Base** | master `3234a6e` (the merge of #401, Spec Critic 3.10.0, Anthropic SDK 1.7.0) |
| **Live evaluation** | Not authorized. The owner chose "offline only" at session start, and no API key was in the session's environment. |
| **Spend** | $0.00. No model request, count request, or batch was sent. |
| **Decision — A, evidence validation** | **Not evaluated.** An observation-mode validator is built behind `SPEC_CRITIC_EVIDENCE_VALIDATION=observe` (off by default). It was scored offline on a constructed evidence set: on its held-out half, 12 of 17 applicable cases agreed with the adjudicated label, with no false concern on the 7 supported verdicts. It enforces nothing, and no default changed. |
| **Decision — B, source reuse** | **Not evaluated.** A within-run prototype is built behind `SPEC_CRITIC_SOURCE_REUSE` (off by default): `shadow` records matches and changes no request, and `supply` supplies passages on the real-time transport. No default changed. |

**Note (2026-09-29, same session).** Master moved while this work was in review: #402 made Opus 5.5
and Sonnet 5.5 the defaults and holds Opus to `medium` effort. The branch was merged with it. Nothing in
EX-04 reads the verifier's model or effort (the validator reads text; the reuse key uses the
verification *profile*, not the model), and the full offline suite passed on the merge (6,700 passed,
19 skipped, 18 network tests deselected; 6,706 after the review fixes below).

The plan asks for two changes that are "related but independently gated". This record keeps them apart
throughout. Each has its own switch, its own protocol, its own promotion criteria, and its own decision.

## The questions

- **A. Evidence validation.** Grounding proves a cited page was *retrieved*, and native citations show
  *which passage* the verifier's words came from. Neither shows that the passage *supports* the
  verdict. Can a check tell the difference, in observation mode first, without ever using lexical
  similarity as an acceptance threshold?
- **B. Source reuse.** Findings about the same material send the verifier after the same passages. Can
  a later verification reuse what an earlier one retrieved, keyed by the full claim context, without
  reusing a verdict and without pretending the passages were fetched fresh?

## Provider facts, rechecked 2026-09-29

These are from Anthropic's search-results page (fetched this session) and the Claude API reference
bundled with Claude Code (cached 2026-09-25). They are documented facts, not measurements.

- **`search_result` content blocks are the documented way to hand Claude retrieved passages.**
  - They are supported on every active model except Haiku 3, with no beta header.
  - Each block has a `source`, a `title`, `content` (text blocks only), and `citations`.
  - Claude cites them as `search_result_location`, which carries the `source`, the `title`, the cited
    text (whole blocks, not substrings), and the block's index.
- **Constraints that shaped the design:**
  - Search results may appear only in user messages (or inside tool results).
  - Citations are all-or-nothing across a request's search results.
  - **When web search is in the same request, citations must be enabled on every `search_result`
    block.** The verifier always carries web search, so reuse always enables them.
- **Citations are incompatible with `output_config.format`.** The verifier sends no format (EX-02
  excluded it for this reason), so supplying search results needs no guard.
- **Web fetch opens URLs already present in the conversation.** A supplied `source` URL is one of
  those, so the verifier could fetch the full page behind a reused passage. Nothing else about the
  fetch gate changes.

## A. Evidence validation (observation mode)

### What is built

`src/verification/evidence_validation.py` (stdlib plus the two citation modules and the new
`reference_parsing.py`). One function, `assess_evidence(finding, result)`, reads a conclusive verdict
and returns an assessment. It does not change the result.

| Check | Reads | A concern means |
|---|---|---|
| Source identity | The verifier's quote against the API's native citations | The quoted passage is attributed to a source the verdict does not cite, or a supporting verdict carries no quote |
| Edition | The claim's editions (loose reading: every year after a designator) against the evidence's (strict: a year written into the reference, from the quote and the accepted sources' cited passages and titles) | The source names a different edition of the same standard |
| Authority | The accepted sources' hosts (code, standards, and government publishers; forums, wikis, social, blogs; everything else unclassified) | Every accepted source is a low-authority host |
| Quoted support | Whether the quote carries the claim's section number, reference, or quantity | Never a concern. This check says whether a passage is on topic, which is not agreement |
| Numbers and units | Quantities with units, converted to a base unit per dimension (length, pressure, flow, density, area, temperature, time, percent, electrical, mass); equal within 2% or 1 °F | A dimension both sides state, with no value in common |
| Negation | Requirement clauses and their polarity. "Shall not be less than" and "shall not exceed" are bounds, not negations. Imperatives ("Provide …") are requirements. A clause is read only up to its exception | A clause on the same subject with the opposite polarity |
| Exception | Exception markers (except, unless, other than, shall not apply, not required where, permitted to be omitted, need not, exempt) | The source states an exception the finding does not mention |

**Direction.**

- CONFIRMED and CORRECTED assert that the source supports a statement: the finding's claim, or, for
  CORRECTED, the verifier's correction. A mismatch is a concern.
- DISPUTED asserts that the source contradicts the claim. There a mismatch is consistent with the
  verdict, and an exact agreement is only "unknown", because a dispute can rest on applicability or
  authority, which no text comparison sees.
- UNVERIFIED, failures, and local classifications are "not applicable".

**The overall reading.**

- **Concerns:** any check raises one.
- **Consistent:** no check raises a concern, and a *content* check agrees (edition, numbers, negation,
  or exception).
- **Insufficient:** otherwise.

Source identity, authority, and quoted support can raise a concern, but they cannot make a verdict
consistent on their own. Where a passage came from, and what it is about, is not what it says. This
rule came out of the tuning cases (below).

**Lexical overlap decides nothing.** It is reported as `features.lexical_overlap` (the share of the
claim's content words the quote uses) and read by no status. The tests force it to 0 and to 1 and
assert that every status stays the same. A faithful paraphrase shares few words with its source; a
threshold on overlap would mark exactly the careful verdicts as suspect.

### Where it runs, and what it cannot touch

With the switch on, `pipeline.verify_findings_for_run` assesses every verified finding after each
verification round. That covers both transports, both rounds, and cache replays (the assessment
records their provenance as `cache_replay`, which is how a verdict cached before any stronger rule
would still be observed). The assessment is written to:

- `VerificationResult.evidence_assessment` (runtime only: in the cache's `_SKIPPED_FIELDS`, never
  persisted);
- the verification diagnostics event (`evidence_assessment`, compact, with the reason for every
  concern);
- a diagnostics rollup (`evidence_validation`: counts by assessment, concern, verdict, and provenance,
  plus up to 50 disagreeing findings);
- one line of the text export;
- the trace's per-finding snapshot, which serializes the whole result.

It is **not** written to either report. The report's "Semantic support: not checked by this app"
stays true, because nothing acts on the assessment, and a heuristic nobody has calibrated has no place
in the deliverable a reviewer acts on. The person the plan wants reading the disagreements reads them
from the diagnostics export (`python -m evals.evidence_validation disagreements EXPORT --markdown`).

Observation leaves the verdict, grounding, sources, cache eligibility, cache key, and report status
unchanged. A test snapshots every other field, and the status, eligibility reason, and cache key,
before and after annotation. A validator exception on one finding is recorded on that finding as
`assessment: "error"` and logged, never raised.

**Policy version.** Every assessment carries `policy_version: "ev1"`. If a check is ever promoted to
enforcement, the enforcing rule must bump the version and give the verdicts it governs their own
verification-cache namespace, the way `BASIS_POLICY_NAMESPACE` did for the edition-authority wording. A
verdict cached before the rule then cannot replay around it, and unrelated entries stay warm.
Observation changes no cache key (tested).

### The evidence set

`evals/evidence_validation_dataset.py`, version 1: 43 cases.

- **Tuning, 25 cases:** used while the rules were written.
- **Held-out, 18 cases:** written after the rules were frozen and scored once.

Each case is one verified finding as the verifier would leave it: the claim, the verdict, the quote,
the accepted sources, and the native citations. Each case carries two labels:

- **`support`:** does the recorded evidence support the verdict as written? The values are `supports`,
  `does_not_support`, `cannot_tell`, or `not_applicable`.
- **`expected_checks`:** the status each targeted check should report.

**The passages are constructed.** Every quote was written for this set in the register of a code or
standard. None is a quotation of NFPA 13, NFPA 72, the IBC, or anything else. Every URL carries a
`/constructed/` path so no one mistakes a case for a citation, and validation enforces both. The set
judges how a validator reads the relationship between a claim and a passage; it says nothing about what
any standard requires. That differs from EX-03, whose facts cite external sources: this set has no
facts to cite.

**Adjudication rules:**

- A passage that qualifies the requirement with an exception the finding omits does not support the
  finding as written.
- A passage from a different edition does not establish what the cited edition requires.
- A passage on a forum or wiki cannot establish a code requirement.
- A passage attributed by the API to a source the verdict does not cite is not the verdict's evidence.
- A general passage that neither states nor contradicts the claim's specifics is `cannot_tell`.
- For DISPUTED, `supports` means the passage shows the claim wrong.

| Category | Tuning | Held-out |
|---|---|---|
| Correct support / attributed quote | 2 | 2 |
| Paraphrase | 2 | 1 |
| Unit equivalent | 2 | 3 |
| Omitted exception / exception accounted for | 1 / 1 | 2 / 0 |
| Reversed negation / comparative bound | 1 / 1 | 1 / 0 |
| Changed number / changed unit | 1 / 1 | 2 / 0 |
| Wrong edition / consistent edition | 1 / 1 | 2 / 0 |
| Wrong authority | 1 | 0 |
| Misattributed quote / missing quote / vague quote | 1 / 1 / 1 | 1 / 0 / 1 |
| DISPUTED | 3 | 1 |
| CORRECTED | 2 | 1 |
| Not applicable (UNVERIFIED, failure, local) | 2 | 1 |

Digest of the whole set: `f4aaa311bca9ce74228200aa5ef6a1093e860b5a13f61cec71c649f672af6d6c`.

### Tuning, and what it changed

The first pass over the tuning split disagreed with the labels on three cases. Each disagreement
changed a rule, a case, or an expectation, as recorded here:

1. **ev-t15.** The replacement text "Inspect waterflow alarm devices quarterly" is imperative spec
   language, which the negation reader did not count as a requirement.
   - Fix: imperative spec verbs now count, and the claim's normative text falls back from the
     replacement to the issue when the replacement states no requirement.
2. **ev-t19.** A DISPUTED verdict whose quote says exactly what the claim says read as consistent,
   because the quote carried the claim's quantity.
   - Fix: quoted support became topical only, never enough for a consistent reading. ev-t19 is now
     `cannot_tell` against a label of `does_not_support`. That is the one tuning miss, left as a
     documented limitation: a text comparison cannot see why a dispute holds.
3. **ev-t25.** Written as a clean negation paraphrase, it accidentally carried an "unless"
   qualification, which made it an omitted-exception case like ev-t05.
   - Fix: the qualification was removed so the case tests what it was written to test.

A second pass raised two more:

4. **ev-t02.** Its negation expectation (`not_applicable`) predated the imperative fix. The check now
   reads "Space hangers at 12 ft maximum" against "shall not exceed 12 ft" as the same requirement,
   which is right.
   - Fix: the expectation was updated to `consistent`.
5. **ev-t06.** "Sprinklers shall be installed throughout, except that sprinklers shall not be
   required in electrical rooms" read as a reversal of "Provide sprinklers in the pump room".
   - Fix: the negation check now reads a clause only up to its exception, which the exception check
     owns.

Also during authoring, **ev-t17** drove the content-check rule. A vague passage on nfpa.org, attributed
to it, came out consistent on provenance alone.

Final tuning result:

- 22 of 23 applicable cases agree with the label; the one miss is ev-t19.
- 9 of 9 flags are correct.
- 9 of 10 unsupported verdicts are flagged.
- No false concern on the 12 supported verdicts.
- Every targeted check expectation is met.

### Held-out result (scored once, rules frozen at `ev1`)

The held-out cases were written after the last tuning change and scored once. One label was wrong as
first written: ev-h15 was labelled `supports` while drafting. It was corrected to `does_not_support`
*before* the scoring run, because a passage stating 140% does not support a claim of 150%. No rule was
changed after the score. `evals.evidence_validation.RECORDED_HELD_OUT_RESULT` holds the result, and a
test reproduces it exactly, so any rule change that moves it is a new policy version needing a new
held-out set.

| Measure | Held-out | 95% Wilson interval |
|---|---|---|
| Agreement with the label (applicable cases) | 12 / 17 (0.71) | 0.47–0.87 |
| Flag precision (flagged verdicts that truly lack support) | 5 / 7 (0.71) | 0.36–0.92 |
| Flag recall (unsupported verdicts flagged) | 5 / 7 (0.71) | 0.36–0.92 |
| False-concern rate on supported verdicts | 0 / 7 (0.00) | 0.00–0.35 |
| Supported verdicts read as consistent | 7 / 7 | 0.65–1.00 |

The five misses, each a candidate rule change for `ev2` (to be judged on new held-out cases, never on
these):

| Case | What happened | Cause | Direction |
|---|---|---|---|
| ev-h08 | A 2016 edition named only in the cited page's title ("…, 2016 Edition") was not read, so a 2022 claim passed | The strict edition reader needs the year next to the designator; titles put it at the end | Missed concern |
| ev-h10 | A quote matching a citation whose source could not be resolved was flagged as misattributed | Unresolved is treated as "another source"; it should be unknown | False concern |
| ev-h11 | DISPUTED with a source from another edition read as supported | "The source names a different edition" is taken as support for a dispute, which is too generous when the passage agrees on its face | Wrong direction |
| ev-h13 | A general passage about sprinkler placement "reversed" a negative-imperative claim about switchgear | The negation topic guard fires on one shared generic word ("sprinklers") | False concern |
| ev-h15 | 150% against 140% passed because 65% matched | Values of one dimension are compared as sets, so one shared value hides a changed one | Missed concern |

ev-h10 and ev-h15 were written as deliberate probes of weaknesses suspected while writing the rules;
ev-h08, ev-h11, and ev-h13 were not anticipated.

### What this does and does not show

It shows that the rules behave as written on constructed cases, including cases written after they were
frozen, and it names five specific ways they fall short. It does **not** show agreement with a person
on real verifier output. Both splits were written by the rules' author, and the passages are
constructed. That comparison is the live observation protocol below, and it costs nothing but a
person's time.

The existing live captures (`evals/calibration/fixtures_live/`, 12 files) cannot stand in for it. They
predate the source quote and native citations, so none carries the evidence the validator reads.

## B. Shared source resolution (within a run)

### What is built

`src/verification/source_reuse.py` (stdlib plus the citation modules and `reference_parsing.py`).

**The key: `SourceContext`.** Every field separates keys; a test changes each field in turn.

- **Reference:** the designators and section numbers from `codeReference`, editions dropped. A finding
  without a recognizable reference is not keyable and never reuses anything, so a bare standard name
  and a month are never a key.
- **Editions:** as the claim names them, else the module's pin, else `unpinned`. An ASCE 7 pin
  (`7-22`) is written as the claim reader writes it (`2022`).
- **Cycle:** the label plus the pinned-editions fingerprint the verification cache uses.
- **Jurisdiction:** the project profile's fingerprint.
- **Basis:** the rendered governing basis's fingerprint.
- **Authority:** the claim's verification profile (jurisdictional, code standard, manufacturer,
  constructability, internal coordination).
- **Module** and **resolver policy version** (`sr1`).

Two fields the plan names are handled outside the key:

- **Client.** It is constant within a run (one project profile per run), so a run-scoped store needs
  no client field. A store that outlived a run must add one before anything is reused across runs.
- **Freshness.** It is checked at lookup, not keyed: a source retrieved more than six hours ago is
  stale.

**What is harvested.** Only text the API itself extracted from a page this conversation retrieved: the
cited text of a native citation whose tool is web search or web fetch, whose source resolved, and
which is marked retrieved. Only fresh results are harvested; a replay's passages came from another
conversation at another time.

- The verifier's own `source_quote` is never harvested. It is model-written, and harvesting it would
  turn the model's words into "retrieved content".
- A passage this module supplied (a `search_result` citation) is never harvested again, so a reused
  passage cannot come back labelled as fresh.

Bounds: at most 5 sources per context, 4 passages per source, 8 passages per request, 500 characters
per passage, and 500 contexts per run.

**Lookup outcomes.**

- `hit`
- `absent`
- `stale`
- `incompatible`: the same reference was resolved under a different context, and the differing fields
  are named.
- `insufficient`
- `not_keyable`

Every outcome but a supplying hit sends exactly the request the switch-off path sends (tested against
the recorded request).

**The two modes.**

- **`shadow`** records, on every fresh verification, what the store matched, and supplies nothing.
  Requests are byte-identical on either transport. The rollup's match rate bounds what supplying could
  ever save, at zero extra spend.
- **`supply`** puts the matched passages into the verification's user message as `search_result`
  blocks, followed by a note saying where they came from:
  - "not retrieved for this finding";
  - "follow your usual procedure";
  - "check anything they do not settle with web_search".

  It runs on the real-time transport only. A batch run falls back to `shadow`, with one warning.
  The batch wave loop builds requests in five places (initial, retry, continuation, escalation,
  real-time fallback), and half-wiring them would break the rule that both transports classify alike.

**Determinism.** Each round's lookups are taken before the round starts, and the store only grows
after it ends. The second round (cross-check and compliance findings) is matched against the first
(review findings), and no finding's supply depends on which finding of its own round finished first.
Shadow records follow the same rule (tested).

### Reuse is retrieval, never a verdict

- Every finding still gets its own verification call and its own verdict.
- The **escalation always resolves fresh**: it fires exactly when the first pass could not settle the
  claim, which is the plan's "insufficient" fallback.
- The verification cache (verdict reuse between equivalent claims) is untouched.
- A supplied passage is recorded as `VerificationResult.reused_sources`, never among `searched_sources`
  / `fetched_sources`.
- The API's citation of it carries the `search_result` tool and `retrieved: false`.
- `source_reuse` records the lookup, the supplied sources' origins and ages, and which accepted
  citations only a supplied passage could have validated (`accepted_via_reuse`).
- Both reports add one clause to the retrieval sentence when passages were supplied: "it was also
  given passages … which this verification did not retrieve itself". Otherwise the sentence is
  byte-identical.

**The grounding rule, under the experiment.** Two gates change:

- The evidence gate (a finished turn needs a successful search or fetch) also accepts supplied
  passages.
- The accepted-citation pool includes their sources.

That is a deliberate exception to "retrieved in *this* conversation", confined to `supply` mode. It is
why such a verdict is **never cached**: the fields that carry its provenance are runtime-only, so a
replay would present it as ordinary grounding. `cache_ineligibility_reason` refuses any result with
reused sources.

**No double billing.** Reuse adds no attempt and no usage. The originating verification is billed
once, on its own finding, and the reusing verification is billed for its own call. The pipeline test
prices two findings (origin and reuser) as exactly two calls.

### What the investigation found

- **Harvest yield is unmeasured, and may be low.** The verifier submits its verdict through a tool
  call, so native citations exist only where it also writes cited text. How many passages a
  verification yields is the first thing the shadow stage measures. If it is near zero, reuse has
  nothing to supply, whatever the match rate.
- **Supplying cannot skip the first search without a prompt change.** The verifier's system prompt
  says "Call web_search first, then call submit_verification_verdict". The supply note does not
  contradict it; it only adds passages the verifier may cite. So any saving comes from fewer
  *follow-up* searches.
  - A second arm that relaxes "search first" when passages were supplied would change the system
    prompt for those requests, and with it the prompt cache and every verifier golden for them.
  - It is a candidate for a later experiment, recorded rather than built.
- **Batch is the default transport and is not wired for supplying.** A promotion needs it (see the
  criteria). Shadow mode works on batch today, so the measurement that decides whether it is worth
  wiring needs nothing more.

## Configuration

| Item | SHA-256 |
|---|---|
| `src/verification/evidence_validation.py` | `1b057f0023a9acc0916d982b0421c0ac6a3605048f176900d8b81f7f2ef66256` |
| `src/verification/source_reuse.py` | `8a07df7453c87af2672ea304e0f2003114921181d335b68c07ea94d78aa53a1a` |
| `src/verification/reference_parsing.py` | `0f811de3cb043e42125d62f2b6ced1ff4afdb9e62b4c5c1b3d120b1761917f71` |
| `source_reuse.REUSE_NOTE` (the supply note text) | `0c1ac932aad2b9872c8d75e5b3dfa702b6453a38d39cfcbc256f3201b5a3708c` |
| Evidence set (`dataset_digest()`) | `f4aaa311bca9ce74228200aa5ef6a1093e860b5a13f61cec71c649f672af6d6c` |

| Switch | Values | Off |
|---|---|---|
| `SPEC_CRITIC_EVIDENCE_VALIDATION` | `observe` (also `1` / `true` / `yes` / `on`); there is no enforcing value, and `enforce` is refused with a warning | Unset, empty, `0` / `false` / `no` / `off`, or any unknown value (one warning) |
| `SPEC_CRITIC_SOURCE_REUSE` | `shadow` or `supply`; no truthy shorthand, since the two differ in whether requests change | The same |

## Protocols (not run)

`evals/evidence_validation.py` holds both protocols and the promotion criteria as data
(`VALIDATION_PROTOCOL`, `REUSE_PROTOCOL`, `PROMOTION_CRITERIA`). They were fixed before any live result
exists.

**A, validation.** This protocol costs no API spend; it rides ordinary runs.

1. Run ordinary reviews with `SPEC_CRITIC_EVIDENCE_VALIDATION=observe` and keep each diagnostics
   export.
2. A person adjudicates every listed disagreement: validator right, verifier right, both wrong, or
   cannot tell.
3. The same person adjudicates an equal-size random sample of the verdicts the validator did not flag,
   without seeing the validator's reading. Without that sample, recall and the false-concern rate
   cannot be estimated.
4. Report per check and per verdict, with DISPUTED separate.

The minimum sample is 100 of each, across at least two modules.

**B, reuse.** Two stages:

- **Stage 1 (shadow, no extra spend).** Measure the **second-round** match rate and the passages per
  match. First-round lookups are excluded, because the store is empty until the first round ends; the
  rollup keeps the rounds apart (`by_phase`, from the diagnostics phase each round is logged under).
  It gates stage 2: at least 15% of second-round verifications must match, over at least 200
  second-round lookups (pooled across runs if needed), with a passage yield above zero.
- **Stage 2 (paired, needs a budget).** Run a baseline and `supply` over the same specs, module,
  models, and profile, on the real-time transport, each arm with its own empty cache file.
  - Compare, for matched second-round findings: searches, cost, latency, and verdicts.
  - A person adjudicates every verdict that differs between the arms.
  - The minimum sample is 50 matched findings per arm, and the run stops at that or the cap.

## Promotion criteria (fixed now)

- **Validation.**
  - Observation never becomes a default.
  - A check may enforce only when all of the following hold:
    - its concern precision is at least 0.90, with a Wilson lower bound of at least 0.80;
    - its false-concern rate on supported verdicts is at most 0.02;
    - it ships under a new policy version with its own cache namespace.
  - Lexical overlap never becomes an acceptance threshold.
- **Reuse.** `supply` may be promoted only when all of the following hold:
  - stage 1 passes its gate;
  - stage 2 saves at least one search per matched finding (median);
  - **no** support judgment is degraded on adjudication (zero, not a rate);
  - a batch wiring exists.
- **Separately.** Either change may be promoted without the other.

## Offline tests

- **`tests/test_evidence_validation_experiment.py` (91 tests).** They cover:
  - the switch (no enforcing value; one warning per unknown value);
  - the reference reader;
  - quantities and conversions;
  - every check in both directions;
  - the overall reading (provenance alone never makes a verdict consistent);
  - lexical overlap decides nothing;
  - observation changes no other field, status, eligibility, or key;
  - the assessment is never cached;
  - errors are recorded, not raised;
  - the pipeline entry point with the switch off (byte-identical events and summary) and on;
  - the trace snapshot;
  - the dataset's soundness and digest;
  - the recorded held-out result, reproduced exactly;
  - the harness and CLI.
- **`tests/test_source_reuse_experiment.py` (80 tests).** They cover:
  - the switch;
  - key separation on every field;
  - harvest (API text only, retrieved only, fresh only, never re-harvested, bounded);
  - every lookup outcome, including staleness at the boundary;
  - request content and byte-identity;
  - the real-time verifier driven with a scripted client: a supplied passage grounding a verdict
    without a search, a non-supplying lookup sending the identical request, the escalation resolving
    fresh, and no extra attempt;
  - the pipeline over two rounds in both modes;
  - lookups taken before the round;
  - the batch fallback to shadow;
  - a single-flight follower not repeating its leader's lookup;
  - the report clause;
  - the cache refusal;
  - no double billing;
  - the stage-1 measurement: the rounds kept apart, the gate read from the second round only, and
    the passage yield reported in shadow mode;
  - the two switches' independence.

**Mutation check.** 36 deliberate breakages, each run alone against both modules; all are caught. Of
the first 32, 30 turned the suite red on the first run. The two survivors each exposed a missing test, and both were then caught after
adding one:

- V7: the claim's editions read strictly. This added the stale-edition-claim test.
- R17: shadow records re-derived after the round's own harvest. This added the
  shadow-records-before-the-round test.

The breakages, by part:

- **Validation:** provenance making a verdict consistent; overlap deciding quoted support; numbers
  ignoring direction; exact numeric equality; a comparative bound read as negation; exceptions not
  split from negation; the claim's editions read strictly; misattribution read as consistent; annotate
  raising; annotate with the switch off; the assessment persisted; the event always carrying the
  field; a supporting verdict without a quote not flagged; low authority not flagged.
- **Reuse:** the key ignoring edition, jurisdiction, or authority; harvesting supplied passages,
  replays, or the model's quote; no staleness; incompatible read as absent; shadow supplying; the
  escalation receiving passages; supplied passages not counted as evidence, or kept out of the
  grounding pool; reuse results cached; batch keeping `supply`; the report hiding reuse; supplied
  sources shown as searched; the user message always a list; shadow records re-derived after the
  harvest.
- **Found in review, then added:** a single-flight follower's clone carrying its leader's
  `source_reuse` record, which would count one lookup twice in the rollup. `pipeline._shared_clone`
  now drops it (and any evidence assessment, which is computed per finding), and a test and a 33rd
  breakage cover it.
- **Found by the Codex review on #403, then fixed:** the reuse measurement divided matches by every
  lookup, first round included, which can never match and would understate the gated second-round
  rate by about half; and it dropped the shadow-mode passage yield, reporting only the (always zero)
  supplied count. The rollup now keeps the rounds apart and the harness reads the gate from the
  second round and reports matched passages. Tests and three more breakages cover them (rounds
  pooled, the gate over all rounds, the shadow yield dropped).

## Rollback

Both switches are off by default, and neither writes anything persistent:

- no cache, pending-batch, or sidecar shape moved;
- a reuse-arm verdict is never cached;
- the assessment is never persisted.

Unsetting a switch restores byte-identical requests, results, events, and summaries.
