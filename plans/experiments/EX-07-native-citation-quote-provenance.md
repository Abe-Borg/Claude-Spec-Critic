# EX-07 — Native citations and verdict quote provenance

| | |
|---|---|
| **Origin** | Step 5 of the [prompt audit improvements](../prompt-audit-improvements.md); the report recommends an API-behavior spike before implementation. |
| **Inspected baseline** | Spec Critic 3.10.0, merged master `1cdab632c39bfd025a8043955b1df6eb754d36ad` (PR #415), Anthropic SDK 1.11.0. |
| **Decision** | Record the candidate; do not implement an enforcing quote check or change grounding. |
| **Live API spike** | NOT RUN. No API credential or authorized spending cap is available in this session. |
| **Spend** | $0.00. No model request, token-count request, or batch was sent. |

## What remains to establish

Can the API attribute the exact value of
`submit_verification_verdict.input.source_quote` to a retrieved source? If its
citations attach only to assistant text, can a cited prose quote immediately
before the verdict tool supply an unambiguous, complete provenance link to
that field while preserving the existing verdict schema and both transports?

An attributed passage still needs separate assessment of whether it supports
the claim, the correction, or the dispute under the controlling edition and
jurisdiction. This investigation concerns quote provenance, not that semantic
or applicability judgment.

## Findings from source and installed SDK inspection

These are offline observations, not confirmation of live provider behavior.

| Existing behavior | Evidence and limit |
|---|---|
| The verdict is a client-tool input with a nullable `source_quote`. | [VERIFICATION_VERDICT_SCHEMA](../../src/review/structured_schemas.py) requires the field. [The parser](../../src/verification/verifier.py) demotes CONFIRMED/CORRECTED with an empty quote. DISPUTED is asked for a quote, but both parser and cache tolerate absence: [cache eligibility](../../src/verification/verification_cache.py) is citation-gated for DISPUTED, not quote-gated. A grounded dispute with a substantive accepted source can be persisted and replayed without a quote. None of these checks establishes exact quote provenance. |
| URL grounding and native attribution already exist independently. | [Source grounding](../../src/verification/source_grounding.py) validates verdict sources against retrieved URLs; native citations never replace a missing or rejected verdict source. EX-04's default-off `supply` experiment separately discloses passages retrieved earlier in the same run. |
| The installed SDK declares citations on text, not tool inputs. | `anthropic.types.TextBlock.model_fields` includes `citations`; `ToolUseBlock.model_fields` includes `input` and has no declared citation field. This gives no documented tool-input citation carrier in the installed types. It does not prove the server cannot return an extension or a future shape. |
| Citation collection reads assistant text blocks. | [collect_native_citations](../../src/verification/native_citations.py) skips `tool_use` blocks. It collects search and document citations across the real-time conversation and batch waves, then records retrieval, accepted-source association, attempt, model and role. Mocked transport tests establish parity of this processing, not live API parity. |
| Existing records cannot certify the entire verdict quote. | Search citations describe up to 150 characters; stored cited text is bounded to 500 characters and records to 20. Truncation and omission are recorded. Document resolution uses whitespace-collapsed containment to disambiguate a fetched document, not exact equality or validation of the citation's character range. PDFs may resolve by index only. The collector discards the opaque `encrypted_index` and does not retain an explicit association between a cited text block and the tool's `source_quote` field. |
| EX-04 already observes a relationship between quotes and citations. | [Evidence validation](../../src/verification/evidence_validation.py) compares whitespace-collapsed fragments/containment for `source_identity`. No matching citation means `unknown`, since the quote may occur elsewhere on a cited page. The check enforces nothing and cannot be treated as exact full-quote validation. See [EX-04](EX-04-evidence-validation-source-reuse.md). |

The report's proposed strengthening is therefore not a request to add citation
capture again. The unresolved work is a reliable association from one verdict
field to complete source text, including the cases where the API cites only a
shorter fragment or returns no usable citation.

## Future API spike: controls and stopping rules

This is a protocol for future work, not an implemented runner. Before any paid
call, record an authorized positive USD cap, current prices, exact model IDs,
SDK version, request fingerprints, public fixture URLs and expected passages.
Use fresh conversations and empty caches. Disable SDK retries and the EX-04
validation/reuse switches; record every actual request, including failed
attempts, continuations, search charges and unknown usage. Check the cap before
each send; one in-flight request may overshoot by its own cost. Unknown usage,
unpriced models, an exhausted cap or an unexpected request stops further sends.
Do not run the application's full verification/escalation pipeline for a
carrier probe or permit a real-time fallback to hide a batch failure.

Build requests with the production verification request builder and preserve
its thinking/effort, strict-tool capability policy, `tool_choice: auto`, verdict
schema, domain restrictions and tool limits. Do not force the verdict tool
before retrieval or add `output_config.format`. The repository records JSON
outputs as incompatible with citations in EX-02/EX-04; recheck current provider
documentation and save the relevant URL/date/statement before the live spike.

Start with one diagnostic prose-only positive control per model/transport:
ask for a short cited passage, with no verdict tool. This deliberately differs
from the production request and tests only whether citation-bearing text can
be obtained. If a control fails to retrieve the source or returns no usable
citation, stop that cell and record it as inconclusive; do not silently replace
it with a different model or source.

For cells with successful controls, compare these two carriers independently:

| Carrier | Request change | Question |
|---|---|---|
| A: current verdict tool | None. | Where, if anywhere, does the raw response carry a citation associated specifically with `input.source_quote`? Citations on unrelated prose do not answer this. |
| B: cited prose followed by the same verdict tool | Add only an instruction to emit the exact quote as cited prose immediately before submitting the unchanged verdict input. | Are both values equal, is the entire quote attributed, and can its source and producing attempt be linked without ambiguity? |

Use the shipped Sonnet 5.5 initial-verifier and Opus 5.5 escalation model IDs,
each in streaming real time and actual Messages Batches. The initial matrix is
three public, fixed-source cases per carrier, twice each: a short passage, a
passage longer than a search citation's 150-character excerpt, and two sources
sharing a passage to expose ambiguous attribution. Alternate carrier order.
This bounds the initial spike to 48 comparison conversations plus four
positive controls; only cells with successful controls proceed. Predeclare
at most two continuation/reminder sends per conversation (three Messages
requests total), with no escalation, automatic retry or fallback. A batch
request/wave counts as a send; stop and retain incomplete conversations when
the bound is reached. The spending cap can stop the matrix earlier.

Use search on both models. Include fetched-text evidence only where the
production capability policy already permits fetch: Sonnet's fetch-capable
routing, with citations enabled. Opus 5.5 fetch remains disabled pending its
separate [capability probe](../../tests/test_network_smoke.py); this spike must
not change that flag. Do not add synthetic `search_result` blocks to the live
matrix: they would test EX-04's supplied-passage carrier instead of actual
web-search/fetch provenance. PDF and supplied-result behavior remain separate
follow-ups if this first matrix establishes a useful carrier.

## Evidence to retain and evaluate

Save request bodies and transport headers, raw streamed events/final messages
or raw batch results, request/message/batch IDs, ordered blocks across all
continuations, stop reasons, request timings, actual usage and cost. Use public
fixtures and exclude credentials from artifacts. Observe unknown fields in raw responses
as well as SDK dumps: a missing declared SDK field or a collector's empty
list is insufficient evidence that the provider returned nothing. Keep raw
unbounded citations and source text in the spike artifacts so the app's caps
cannot conceal partial coverage; record the source snapshot and its digest.
No such artifacts are generated by this documentation change.

For each verdict, record these separate results:

1. Was retrieval successful and the unchanged verdict tool parsed?
2. Was there a citation carrier on the tool field, on prose, or neither?
3. Did cited prose equal the tool quote as decoded strings? Also record UTF-8
   equality against the retrieved text, without collapsing whitespace, case,
   punctuation or units. “Exact” concerns the text the API returned, not the
   source website's HTML bytes; normalization is a separate diagnostic.
4. Did citation-associated source text cover the entire quote in order, without
   an omitted middle or an unsupported ellipsis? Equal prose alone is not proof
   of this. Overlap with one short citation cannot certify the rest of a quote.
5. Could the source, document/range and producing attempt be resolved, and did
   that source match an accepted verdict source? Keep ambiguities explicit.
6. What extra tokens, cost, latency, parse failures and incomplete conversations
   did the prose carrier introduce? Report streaming and batch separately.

Replay captured results offline with deliberate negative controls: change a
number or a negation only in the tool quote; change a middle phrase in a long
quote while preserving its cited prefix; use the same passage from a different
source; remove citations; truncate or omit a citation; provide an unresolved
document index; and borrow a quote from another attempt. These are mutations
for testing a proposed scorer, not API findings. A scorer must not mark them as
proven exact quotes. Missing, partial and ambiguous evidence is `unknown`, not
proof that a quote was fabricated. Semantic support receives an independent
human assessment, never an exact-match or lexical-similarity verdict.

## Decision after the spike

Publish every cell, including failures, negatives and unrun cells, with the
recorded API shapes and the scope of the conclusion. If no tool-field carrier
is observed, report that fact for the tested versions and requests; do not
claim the API can never support one. If prose cannot supply complete,
unambiguous attribution while keeping the verdict contract, retain the current
behavior and record the limitation. If it can, propose a separate default-off,
observation-only prototype and its evaluation before any enforcing change.

Neither successful transport nor exact quote provenance establishes controlling
authority, edition or semantic support. Any later enforcement needs its own
adjudicated quality/cost comparison, cache/policy versioning, behavior for
legacy/missing evidence, and tests across continuations and escalation. Until
that review, URL grounding, quote-presence rules, cache eligibility and report
statuses stay as they are.

Include cached DISPUTED results without quotes in any later observation-only
evaluation: their quote provenance is `unknown`, and they retain their existing
cache eligibility and verdict. A new observer must not discard these results
or treat an absent quote as evidence that a dispute is invalid. The existing
[cache source-quote tests](../../tests/test_verification_cache_source_quote.py)
cover both in-memory replay of such a dispute and loading a quote-less disputed
row from disk; these contracts must remain in scope.

## Offline validation

- Existing targeted tests: **367 passed** across native citations, source
  grounding, batch-wave grounding, evidence validation, and verification-cache
  serialization/eligibility. They cover capture/resolution/bounds, transport
  processing, continuation/escalation provenance and unchanged acceptance.
- Installed SDK inspection: Anthropic **1.11.0**; text/tool fields recorded above.
- All **30 relative links** in the investigation, tracker and linked handbook
  chapter resolve; the CLAUDE.md open-item link also resolves.
- `git diff --check`: passed. Only four Markdown documents change.
- Review follow-up: **155 cache tests passed**, including the source-quote
  tests for quote-less DISPUTED replay and disk loading, plus cache eligibility
  and serialization. The documentation now states that DISPUTED is
  citation-gated, not quote-gated, and keeps such cached results in the future
  observation-only evaluation.

These checks validate the documented baseline contracts, not the live spike or
a measured quality improvement. The full suite was not rerun for documentation
changes; step 4 recorded **7,615 passed, 21 expected skips** on the same baseline.
