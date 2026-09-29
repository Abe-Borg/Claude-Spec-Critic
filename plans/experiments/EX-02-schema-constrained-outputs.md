# EX-02 — Schema-constrained outputs

| | |
|---|---|
| **Chunk** | S21 (plan Part 4 §23) |
| **Date** | 2026-09-29 |
| **Base** | master `d57288f` (the merge of #396, Spec Critic 3.10.0, Anthropic SDK 1.7.0) |
| **Live evaluation** | Not authorized. The owner chose "offline only" at session start, and no API key was in the session's environment. |
| **Spend** | $0.00. No model request, count request, or batch was sent. |
| **Decision** | **Not evaluated.** A candidate with two arms is implemented behind a default-off switch (`SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT`). No default changed. |

## The question

Every step that parses model output asks for it through a custom "submit" tool under
`tool_choice: auto`, and keeps a tagged-JSON text reader for the case where the model answers in
prose instead. Would a schema-constrained output reduce the responses the app cannot read, without
losing findings, evidence, or money?

This record answers what can be answered offline:

- which mechanisms exist and which each consumer can use;
- which consumer goes first;
- what the candidate changes;
- how it stays safe for saved batches.

The size of the problem it would fix is the part no one had measured, and this chunk makes it
measurable instead of assuming it.

## Three mechanisms, kept apart

| Mechanism | What it guarantees | What it does not |
|---|---|---|
| **Strict tool arguments** (`strict: true` on a tool) | A tool call's input matches the tool's schema. | That the model calls the tool at all. |
| **Forced tool invocation** (`tool_choice: {"type": "tool", "name": …}`) | The model calls that tool. | Anything about a refusal or a truncation, which still end the turn. |
| **Constrained final response** (`output_config.format`, `json_schema`) | The response's text is JSON matching the schema. | A tool call (there is none), or completeness when the response is refused or cut off. |

## What the provider documents (rechecked 2026-09-29)

These come from Anthropic's structured-outputs, thinking, and tool-use pages and the pinned SDK
(`anthropic` 1.7.0). They are documented facts, not measurements. Where the pages disagree, the
disagreement is recorded rather than resolved.

- **Supported models.** Both strict tool use and JSON outputs are listed for Opus 5, Opus 4.8,
  Sonnet 5, Sonnet 4.6, and Haiku 4.5, which are all five models in the app's capability table.
- **Forced tool use with thinking.** The thinking page says forced tool use "is incompatible with
  manual extended thinking but works with adaptive thinking", except on Opus 5.5, Sonnet 5.5,
  Fable 5.1, and Mythos 5.1, which reject it on every request.
  - The app sends adaptive thinking on every phase except triage.
  - The code was written on the understanding that forcing is rejected whenever thinking is on.
    For the models the app uses, that understanding is now documented as wrong.
  - No forced request with adaptive thinking has been sent from this repository.
- **JSON outputs with thinking: the pages disagree.**
  - The structured-outputs page's compatibility table says "Extended thinking: **Not
    compatible.**"
  - The thinking page tells users of Opus 5.5, whose thinking cannot be turned off, to use
    structured outputs instead of forced tool use.
  - The same table calls prefill "compatible", which contradicts the thinking page (no prefill
    while thinking is on). That suggests the table is stale, but a suggestion is not a
    measurement.
  - Strict tool use, which the same page groups with JSON outputs, has run with adaptive thinking
    in production. A strict-schema 400 on every review and cross-check request, fixed long ago
    (`structured_schemas.py`, the `insertPosition` note), is the evidence those requests reached
    the API.
- **JSON outputs with citations:** "Not compatible" (a 400).
- **JSON outputs with other features:**
  - Batches API, streaming, and token counting: compatible.
  - `count_tokens` accepts `output_config` so that it counts the system prompt the format adds.
  - Changing the format invalidates prompt caching for the conversation.
  - A new schema compiles on first use; the compiled form is cached for 24 hours.
- **Refusal and truncation still happen.**
  - A refusal "does not return a JSON object".
  - A `max_tokens` stop "returns an incomplete JSON object (potentially invalid)".
  - Schema guarantees do not remove either failure.
- **SDK.**
  - `OutputConfigParam` has `format` (a `JSONOutputFormatParam`: `type: "json_schema"`, `schema`).
  - `messages.stream`, `messages.create`, batch requests, and `count_tokens` all accept
    `output_config`.
  - No SDK upgrade is needed.

## Consumers

`python -m evals.structured_outputs` builds this table from the app's real request builders.

| Consumer | Model | Server tools | Strict args | Forced call | Constrained response |
|---|---|---|---|---|---|
| Review (per spec) | Opus 5 | none | on | **experiment arm** | **experiment arm** |
| Cross-check | Sonnet 5 | none | on | eligible, not tried | eligible, not tried |
| Compliance | Sonnet 5 | none | on | eligible, not tried | eligible, not tried |
| Drawing impact | Sonnet 5 | none | on | eligible, not tried | eligible, not tried |
| Location research | Sonnet 5 | web search + web fetch (citations on) | on | excluded | excluded |
| Verification | Sonnet 5 | web search + web fetch (citations on) | on | excluded | excluded |
| Haiku triage | Haiku 4.5 | none | on | **on** (no thinking) | not needed |

Why the two web-tool consumers are excluded:

- JSON outputs cannot be combined with citations, and `web_fetch` is sent with citations enabled.
- Web search results carry citations anyway.
- The verifier's native citations (plan WP-16) would be lost.
- Forcing the submit tool would skip the search the verdict depends on.
- The web tools' dynamic filtering rejects any `tool_choice` beyond `auto` (a 400 on
  `disable_parallel_tool_use`).

Triage already forces its tool, which is the detour fix that phase needed; the plan says to keep it,
and it is unchanged.

## Choosing the first consumer: there was no measured failure rate

The plan asks for the first consumer to be "chosen from measured parse failures". A read-only sweep
of the repository found **no measured parse-failure rate for any consumer**:

- Source comments and the handbook say "rare", "occasional", and "reliably but not
  contractually", with no numbers.
- The 12 captured live verifier responses (`evals/calibration/fixtures_live/`) show no parse
  failure, but they store parsed fields only, so they could not show one.
- `evals/baseline.json`'s `parse_failure 0/5` counts items dropped from synthetic payloads, not
  model detours.
- No diagnostics export or trace is committed.

The app could not have measured it either:

- Review attempt records carried an outcome (`ok`, `parse_error`, `incomplete`, `refusal`), but
  no diagnostics JSON ever held them. The only JSON export, `summary()`, rolled them into totals.
- Nothing recorded whether a parsed review came from the tool or from the text fallback.

The per-spec review is chosen anyway, on **cost of failure and exposure**, and the choice is marked
provisional:

- It is the highest-volume consumer: one request per spec per run.
- An unreadable review is a zero-finding spec that pays for a full repair request at the 128k
  cap. On the batch transport that is a second batch cycle, and while the repair is pending every
  dependent stage waits (plan WP-14).
- It has no server tools, so every mechanism is available to it.
- It is the one consumer with the saved-batch compatibility problem the plan names, since review
  batches persist and resume.

The protocol's first live step measures the review's baseline rate from ordinary runs. If that rate
is zero, the experiment stops there and the decision becomes **reject**.

## What changed

**Measurement (on by default, additive, telemetry only):**

- `ReviewResult.parse_source` records where a parsed review came from: `tool`, `json`, or `text`
  (the fallback). It is runtime only.
- Each review attempt record carries that value as `AttemptUsage.output_channel`, on both
  transports and for primary and repair attempts. The field is descriptive like `outcome`, and
  pricing never reads it.
- `DiagnosticsReport.summary()` gains `review_parse_outcomes`:
  - `attempts`, deduplicated like billing;
  - `by_outcome`;
  - `by_output_channel`, over the attempts that parsed. A record written before the field exists
    counts as `unrecorded`, never as a tool call.
- `scripts/recover_batch.py --diagnostics-json` saves that summary.
- `python -m evals.structured_outputs --diagnostics summary.json` turns it into rates: unparsed,
  parse error, truncation, refusal, and text-fallback share.
- The same reader counts the verifier's unreadable verdicts (`no_verdict`, `malformed_verdict`)
  from `retry_stats`.

**Reading (on by default; no response that parsed before reads differently):**

`reviewer.review_result_from_message` decides by what the response contains, never by the switch:

1. a `submit_review_findings` call;
2. otherwise, a text body that is exactly one JSON object whose `findings` is a list;
3. otherwise, the tagged-JSON fallback, as before.

Two properties follow:

- Every source goes through the same field validation (`_parse_findings`: severities, edit shape,
  demotion to REPORT_ONLY), since a schema-valid edit can still be an unsafe one.
- A batch submitted under any arm, or before the arm existed, is collected correctly whatever the
  switch says at collection time.

**The candidate (off by default): `SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT`**

| Value | Request |
|---|---|
| unset, empty, `0` / `false` / `no` / `off` | The default: the review tool under `tool_choice: auto`. Byte-identical to a build without the switch; no golden moved. |
| `forced_tool` | The same tool and prompts, with `tool_choice: {"type": "tool", "name": "submit_review_findings", "disable_parallel_tool_use": true}`. **One field changes.** |
| `json_schema` | No tool, and `output_config.format` set to the review schema, merged with the phase's effort. Four prompt lines that named the tool are reworded, and the shared retry instruction is reworded for a repair built under this arm. |
| anything else | The default, with one warning. The switch fails closed. |

The switch is gated by two new capability flags. Each records what is documented, and only the
experiment reads them:

- `supports_forced_tool_with_thinking`: Opus 5, Opus 4.8, Sonnet 5, Sonnet 4.6.
- `supports_json_output_format`: those four plus Haiku 4.5.

A model that doesn't have the flag keeps the default shape, with one warning. Opus 5.5 and any
other unlisted id is refused, so an override never turns the experiment into a 400.
`supports_forced_tool_choice`, the flag triage uses, is unchanged.

**Counting.**

- When a request constrains its response, the counting form carries `output_config` reduced to
  `{"format": …}`.
- The count endpoint receives it.
- The padded local estimate adds the format's JSON plus a 600-token allowance for the injected
  prompt. That allowance is an assumption: the provider does not document the size.
- Every other request's counting form, and its count-cache key, is unchanged.

**Live probe (written, not run).** `tests/test_network_smoke.py -k review_output_constraint`
sends three small requests, each built by the production builder:

- the `forced_tool` review;
- the `json_schema` review;
- the `json_schema` count request.

It is skipped without an API key. The older structured-outputs gate in the same file had its
docstring corrected.

**Doc corrections.** The claim that forcing is rejected whenever thinking is on was corrected in:

- `structured_schemas.py`
- `api_config.py`
- CLAUDE.md
- handbook ch. 5
- a triage test comment

The README's v3.7.0 changelog line is history and is left as written.

## Request layout (offline)

From `python -m evals.structured_outputs` for the default module:

| Arm | Fields that differ from the default | System prompt SHA-256 (first 16) | User message SHA-256 (first 16) |
|---|---|---|---|
| `tool_auto` | none | `9fe5e0b55ca4d477` | `35d0c8b392391e6d` |
| `forced_tool` | `tool_choice` | `9fe5e0b55ca4d477` | `35d0c8b392391e6d` |
| `json_schema` | `messages`, `output_config`, `system`, `tool_choice`, `tools` | `02ec192cf87a1d52` | `dd37a8b45e0e3af2` |

The review schema (`REVIEW_FINDINGS_SCHEMA`, as sent in `output_config.format`) has SHA-256
`d648f8e5707de99d8a87513cde0c9a8bc32dce59f24bfe11081705af84d0dfe5`.

## Dataset and configuration

- **Dataset:** none. No model output was produced or scored.
- **Layout capture spec:** a three-line synthetic spec (`PART 1 GENERAL` / `1.01 SUMMARY` /
  `A. Provide the specified piping system.`, file name `230500.docx`), built by
  `evals.structured_outputs.review_arm_requests`.

`configuration_sha256`, one per arm, covers:

- the shape (model, thinking, effort, tools, strict, `tool_choice`, server tools, citations,
  output format);
- the prompt digests;
- the changed fields.

California K-12 (default module):

| Arm | `configuration_sha256` |
|---|---|
| `tool_auto` | `4debccac7e3f3c34ceefba6e3e249af4473c953cbcf098dd5134da951c3df351` |
| `forced_tool` | `ad037acbb06b369214763a0ff67f5c91241ce617d95e3184f1f01275ab671d4d` |
| `json_schema` | `060c098541c08777e1633cf2b4649d7442228eeb690339d1b99e3b92302ea98a` |

`datacenter_fire`:

| Arm | `configuration_sha256` |
|---|---|
| `tool_auto` | `2cbdd2f17a8ae0f65462d81682c25ef5af7b81a75bd127e1fc3d1241edc54583` |
| `forced_tool` | `9493e4d0a1cfe04d5f760ad588fe8246fb7d3d6b4a6955ece2a80a8d95519b90` |
| `json_schema` | `538328a05f4c6be335050e50bd078af9e5d2f4eb1ea5c189a3895d30eda23b21` |

## Measurements

| Measure | `tool_auto` | `forced_tool` | `json_schema` |
|---|---|---|---|
| Live API accepts the shape | yes (production) | not measured | not measured |
| Unparsed rate (parse error, truncation, refusal) | not measured | not measured | not measured |
| Text-fallback rate | not measured | not measured | not measured |
| Repair rate, total attempts | not measured | not measured | not measured |
| Latency, cost | not measured | not measured | not measured |
| Findings (count, severity mix, demotions, unsupported) | not measured | not measured | not measured |

Offline results, which establish behavior against fake responses only
(`tests/test_structured_output_experiment.py`, 106 tests):

- **The reader.** Each case the plan names classifies as intended:
  - valid constrained output;
  - legacy tool output;
  - the text fallback;
  - a zero-finding response;
  - a refusal (not repaired);
  - a truncation (repaired);
  - missing content;
  - an object without a findings list;
  - prose around JSON (not read as a constrained response);
  - schema-valid findings with an unsafe edit or an unknown severity (validated exactly as on the
    tool path);
  - a response whose blocks, or the whole message, arrive as plain dictionaries, as the batch
    results stream may deliver them (found by the Codex review of the PR).
- **Both transports end to end.** A real-time `json_schema` review is truncated, then repaired
  under the JSON retry wording, and its two attempt records say `incomplete` then `json`. Batch
  submission carries each arm's shape.
- **Saved batches.** Tool, JSON, and tagged-text batch results collect correctly when the switch
  names a different arm.
- **Switch and gate.** Unsupported model selections, the off values, and unknown values all keep
  the default shape.
- **Mutation check.** 14 deliberate breakages, all caught:
  - the capability gate ignored;
  - default on;
  - an unknown value enabling JSON;
  - the schema aliased;
  - a lenient JSON scan;
  - the reader consulting the switch;
  - JSON bypassing field validation;
  - the format dropping effort;
  - the forced arm left on `auto`;
  - the retry wording not changed;
  - the counting form dropping the format;
  - no allowance for the injected prompt;
  - the batch attempt channel lost;
  - the rollup counting failed attempts as channels.

## What a live evaluation must do

`evals/structured_outputs.py` holds the protocol (`EVALUATION_PROTOCOL`, status NOT RUN). In short:

1. **Authorization first:** a spending cap, an API key, a fixed corpus, and a stopping rule agreed
   in advance.
2. **Capability probe:** run `pytest -m network tests/test_network_smoke.py -k
   review_output_constraint`. A 400 ends that arm, and the error goes in this record.
3. **Baseline rate, at no extra spend:** read `review_parse_outcomes` from ordinary runs' summaries.
   If the unparsed and text-fallback rates are both zero over a corpus that matters, stop and
   reject.
4. **Arms:** `tool_auto`, `forced_tool`, and `json_schema`, changing one thing at a time on the same
   corpus, module, model, effort, transport, workers, and Project Context. Run both transports, and
   submit under one arm and collect under another once.
5. **Metrics:**
   - unparsed rate and text-fallback rate;
   - repair rate and total attempts;
   - latency and cost by category;
   - quality: finding count, severity mix, REPORT_ONLY and anchor demotions, unsupported findings
     (adjudicated on `evals/labeled_specs.py`), and a sample read side by side.

   A constrained response that parses but omits findings is a regression.
6. **Promotion:** only with fewer unparseable outputs and no more omission, unsupported findings,
   lost evidence, retries, repairs, latency, or cost. Promotion is per consumer.

## Decision

**Not evaluated. Keep the switch off.**

- No live request was authorized.
- The provider's pages contradict each other on the `json_schema` arm's central combination (JSON
  outputs with adaptive thinking).
- Most importantly, there was no evidence that the review has a parse-failure problem big enough to
  fix.

The chunk's lasting contribution is the measurement: every run's diagnostics summary now says how
many review attempts parsed, through which channel, and how many did not. The candidate stays in the
code for the same reason EX-01's does: it is off by default and byte-identical when off, and it
turns a live comparison into an environment change instead of a code change.

## Rollback

- Unset `SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT`.
- Nothing is persisted by the switch: no verification cache, pending batch, sidecar, or report
  shape depends on it.
- A batch submitted under an arm stays collectable after the switch is unset, because reading never
  consults it.
- The measurement fields (`parse_source`, `output_channel`, `review_parse_outcomes`) are runtime
  telemetry, additive in the diagnostics summary.

## Reproduce

```text
python -m evals.structured_outputs [--module datacenter_fire] [--diagnostics summary.json]
python -m pytest tests/test_structured_output_experiment.py
# Live probe (billable; needs authorization and a key):
ANTHROPIC_API_KEY=... python -m pytest -m network tests/test_network_smoke.py -k review_output_constraint
```
