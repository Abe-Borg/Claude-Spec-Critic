# Spec Critic plan — progress tracker

This file records what's done and what's next. The instructions live in
[`spec-critic-implementation-plan.md`](spec-critic-implementation-plan.md).

**Coding agents:** update this file in the same pull request as your work (plan, Part 1). Master's
copy of this file is the truth: a chunk counts as done only once the PR that marks it DONE is merged.

---

## ▶ Right now

| | |
|---|---|
| **Next chunk** | **S06 — Request budgets** (WP-08) |
| **Last finished** | S05 — Verification failures and cache (WP-10) |
| **Last merged PR** | [#378](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/378) (S04) |
| **Overall** | 5 of 25 chunks done |

**Prompt for the next session** (paste it into a new Claude Code session on this repository):

```text
Continue the Spec Critic implementation plan.

Next chunk: S06 — Request budgets (WP-08).

Start from the latest master. Read CLAUDE.md, then plans/PROGRESS.md, then Part 1
and chunk S06 in plans/spec-critic-implementation-plan.md, and its packages in Part 4.
Do only this chunk. If PROGRESS.md names a different next chunk, follow PROGRESS.md.
Open one PR. When I tell you it's merged, give me the prompt for the next session.
```

---

## Status of every chunk

Status words: **TODO** · **IN PROGRESS** · **PARTLY DONE** (the next session continues it) ·
**DONE** · **DONE (not evaluated)**, for experiments run without a live evaluation.

| Chunk | What it does | Packages | Status | PR |
|---|---|---|---|---|
| S01 | Record the starting point; build shared test fixtures | WP-01 | DONE | [#375](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/375) |
| S02 | Stop merging different findings; applier refuses ambiguous files | WP-06A, WP-07 | DONE | [#376](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/376) |
| S03 | Fix the false structure alerts and the text checks | WP-04 | DONE | [#377](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/377) |
| S04 | Make the report chat recover from errors | WP-12 | DONE | [#378](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/378) |
| S05 | Stop caching "couldn't verify" as an answer | WP-10 | DONE | |
| S06 | Size big requests for the model that runs them | WP-08 | TODO | |
| S07 | Show when compliance coverage is incomplete | WP-09 | TODO | |
| S08 | Keep paid repair batches recoverable | WP-14 | TODO | |
| S09 | Count every paid attempt exactly once | WP-15 | TODO | |
| S10 | Read Word content controls, fields, and smart tags | WP-02 | TODO | |
| S11 | Keep every edit location, part 1: occurrence model and applier reader | WP-06B | TODO | |
| S12 | Keep every edit location, part 2: sidecar writer and reports | WP-06B | TODO | |
| S13 | Route specs by their own SECTION heading | WP-05 | TODO | |
| S14 | Read Word automatic numbering | WP-03 | TODO | |
| S15 | Respect rate-limit timing | WP-11 | TODO | |
| S16 | Make tracing optional; keep keys out of the environment | WP-13 | TODO | |
| S17 | Keep the verifier's citations; fix the fetch instructions | WP-16 | TODO | |
| S18 | Make prompts, reports, and docs match the code | WP-17 | TODO | |
| S19 | Correctness release | release | TODO | |
| S20 | Experiment: shared project-context caching | EX-01 | TODO | |
| S21 | Experiment: schema-constrained outputs | EX-02 | TODO | |
| S22 | Experiment: model, effort, and confidence | EX-03 | TODO | |
| S23 | Experiment: evidence validation and source reuse | EX-04 | TODO | |
| S24 | Experiment: research reuse | EX-05 | TODO | |
| S25 | Experiment: cross-chunk and cross-module coordination | EX-06 | TODO | |

---

## Chunk checklists

A chunk is DONE when every box below it is ticked and its packages' acceptance criteria in Part 4 of
the plan pass. If a box turns out to be wrong, don't tick it silently: write what you did instead
under "Decisions and deviations".

From S01 on, each chunk's known defects are strict xfails in `tests/test_plan_open_defects.py`;
search it for `fixed by S0N` to find yours. A fix shows up as an `[XPASS(strict)]` failure naming the
chunk: remove that marker in the same pull request, and keep the control test next to it passing.

### S01 — Record the starting point; build shared test fixtures (WP-01)
- [x] Starting commit and offline test result recorded under "Starting point" below. Re-measure; the September 23 numbers are only a reference.
- [x] Shared DOCX fixture builders in `tests/fixtures/`. They cover the clean 3-PART spec (plan, Appendix A) and its four variants: table-only article body, automatic numbering, a truly empty article, and a truly duplicated heading. They also cover content controls (block, inline, dropdown), simple fields (a stored REF result), smart tags, hyperlinks, tracked insertions and deletions, merged and nested tables, and representative compact filenames. → `tests/fixtures/spec_docx.py`, pinned by `tests/test_spec_docx_fixtures.py`.
- [x] Clean fixtures and single-defect mutations are separate builders.
- [x] Contract pins for behavior that is already right: extraction reconstruction, unique element IDs, the meaning of legacy `pN` / `tN` IDs, and group-vs-occurrence identity as it stands today. → `tests/test_contract_pins.py`.
- [x] Every check in `plans/check_plan_status.py` becomes a test marked `xfail(strict=True, reason="open: fixed by S0N (WP-xx)")`. The script is then deleted, and this file names the new test module. → `tests/test_plan_open_defects.py` (markers also carry `raises=AssertionError`; see "Decisions and deviations").
- [x] Each converted check keeps its control case, so a detector that goes silent or a cache that stops caching can't pass as a fix. The "source hint" checks become behavioral tests, not string searches. (The Haiku cache-minimum check stays a documentation check; see "Decisions and deviations".)
- [x] The full offline suite passes, with the new tests reported as xfailed.

### S02 — Stop merging different findings; applier refuses ambiguous files (WP-06A, WP-07)
- [x] The generic "CSI number … .docx" stripping is gone. Only exact known corpus filenames are normalized, with literal escaping and clear boundaries. With no corpus context, the text is kept as is. → `FindingIdentityContext` in `src/orchestration/pipeline.py`.
- [x] One normalization context is used for review, cross-check, compliance, and finding IDs, with no global mutable state. The `rf-` / `cf-` / `lc-` prefixes are kept. → `finding_identity_context_for_submission`; an AST tripwire in `tests/test_finding_identity_normalization.py` requires every production call to pass it.
- [x] Copper and PVC stay distinct, and the same issue naming different known files still groups. Overlapping names, spaces, punctuation, uppercase extensions, unknown names, and reordered input don't corrupt identity.
- [x] Applier: a filename maps to every distinct resolved path. Two different same-named inputs are ambiguous in either order; repeating one path is not. → `applier/run.py::_index_specs`.
- [x] All file bindings and destinations are resolved before any write. Output collisions, and destinations that would overwrite any supplied source, are refused. Every held instruction appears in the receipt with a specific reason, and the exit status is non-success when anything is held. → `_plan_files`; new outcomes `FILE_AMBIGUOUS` / `DESTINATION_CONFLICT`; exit `3` (see "Decisions and deviations").
- [x] `--assist` never picks among ambiguous files. A dry run and a real run make the same decisions.
- [x] Release-note line added: some finding IDs change because the old key was wrong; existing reports are untouched.

### S03 — Fix the false structure alerts and the text checks (WP-04)
- [x] Heading candidates carry number, title, level, and position. Integer-led prose and quantities ("2 coats…", "12 inches…", "1.5 inches…") are not headings. → `HeadingCandidate` / `heading_candidates` in `src/input/preprocessor.py` (also `run_in` and a `provenance`, `"typed"` until S14).
- [x] A heading's content runs through its whole subtree, so a PART with articles is not empty. The clean 3-PART fixture and its table-only variant produce no empty or duplicate alerts; a truly empty article and a truly duplicated heading still alert. An empty PART is one alert, not one per empty article (new `empty_part` fixture mutation). → `tests/test_heading_structure.py`.
- [x] Stale-citation suppression uses only citation-related historical or rejection phrases. The three WP-04B examples and the "shall not deviate" / "cannot depart" forms are flagged. Genuine "previous edition" / "superseded" contexts stay suppressed, and sentences with several citations are tested. → three cue tables plus neighbor-citation bounds; `TestCitationRelatedSuppression` in `tests/test_preprocessor_policy.py`.
- [x] ASCE/SEI, the optional word "Standard", Unicode dashes, and 2- and 4-digit edition years are recognized. The California long-form references go into the California module's vocabulary only. → `_ASCE7_PATTERN` / `_asce7_edition_key`; `src/modules/california_k12_mep.py`.
- [x] Bare TBD is detected once, with no double count alongside `[TBD]`. Keyword boundaries hold (EDITION is not EDIT, SELECTED is not SELECT), `TBDF-200` stays clean, and the `TBD-200` policy is written down. → `PLACEHOLDER_PATTERNS` comment and CLAUDE.md §5: `TBD-200` is an identifier.
- [x] File naming: separated, compact, and SECTION-prefixed names are recognized. Unknown names don't hide a mixture, and when no style dominates a neutral mixture notice is issued. → `detect_inconsistent_file_naming`; "dominant" means used by more than half of the CSI-named files.
- [x] Rule IDs, alert order, and alert limits are unchanged, and so are the location-aware modules' policies. Changed goldens are reviewed one by one. → `TestAlertContractUnchanged`; two golden lines changed, both the naming alert's `context` (see "Decisions and deviations").

### S04 — Make the report chat recover from errors (WP-12)
- [x] A Node test harness runs the exact script the exporter ships (extracted the same way the CSP test extracts it) against scripted event streams. → `tests/fixtures/chat_harness.js` + `chat_harness.py`; `tests/test_html_chat_behavior.py` (92 tests). The harness checks the extracted bytes against the report's CSP hash.
- [x] API error events, transport and reader failures, malformed required data, and premature EOF all reach the chat's state. A response without a valid terminal event is not treated as complete. → a response counts only at `message_stop` with a stop reason and every block closed; the `try` guards only `JSON.parse`.
- [x] Split UTF-8 characters, arbitrary chunk boundaries, LF and CRLF separators, and multi-line data frames reconstruct correctly. → also lone CR endings, a CR at a chunk end, a fatal decoder flushed at the end, and an unterminated final event (never dispatched).
- [x] Invalid or incomplete tool arguments never become `{}`. Stop reasons, the continuation limit, and the tool-round limit are handled visibly. → every stop reason, including `model_context_window_exceeded` and unknown ones (see "Decisions and deviations").
- [x] Committed history never holds a `tool_use` without its `tool_result`, under one documented transaction model. Partial text is shown as interrupted and never replayed as a complete answer. → rollback: a turn commits only on `end_turn` / stop sequence (module docstring, CLAUDE.md "HTML report + Ask AI", handbook ch. 22).
- [x] Stop, New Chat, and a model change can't let an old response write into a newer conversation, and the controls are always restored. → also Forget key and leaving the page; tested with streams that ignore the abort.
- [x] Citation deltas stay with their text block and survive the next request.
- [x] The API key lives in page memory only. The legacy `sc_api_key` storage entry is removed and never re-imported, Forget Key clears memory, and a storage failure doesn't break chat.
- [x] Escaping and CSP-hash tests still pass, and the key policy in CLAUDE.md's "HTML report + Ask AI" section is updated.

### S05 — Stop caching "couldn't verify" as an answer (WP-10)
- [x] Real time and batch share one verdict-classification contract. A malformed or missing verdict after an ordinary end turn is an operational failure that keeps its known usage, not an UNVERIFIED. Refusal, max tokens, malformed tool input, and unexpected stops are each handled explicitly. → `verifier.classify_verification_turn` + `_stamp_verdict_result` / `_failure_result`, called by both transports; `VerificationResult.outcome`; `tests/test_verdict_classification_contract.py` (every turn outcome through both transports, compared field by field).
- [x] One cache-eligibility predicate is used at write, read, and disk load. Only grounded conclusive verdicts that meet the source and quote rules are reused. UNVERIFIED, failed, budget-exhausted, and local results never are. Invalid timestamps and non-finite numbers are rejected record by record. → `verification_cache.cache_ineligibility_reason`; `_record_timestamp` / `_persisted_payload_problem`; `tests/test_verification_cache_eligibility.py`.
- [x] Legacy UNVERIFIED rows are ignored individually and valid conclusive rows still load. There is no blanket flush. → counted in `stats()["rejected_on_load"]` and the run's load log line; no schema bump (still v4).
- [x] Within a run, a well-formed UNVERIFIED is shared once among equivalent in-flight findings; parse failures, local results, and budget failures are not shared. Followers settle correctly when the leader succeeds, is uncertain, fails, or is cancelled, and they add no billed usage. → `pipeline._shareable_verdict` requires `outcome == "verdict"`; WP-10 section of `tests/test_verification_singleflight.py`.
- [x] A later run retries an earlier UNVERIFIED under the normal escalation policy. → `TestALaterRunRetries` (real `verify_finding`, real disk round trip, HIGH finding escalates again).
- [x] Empty or whitespace-only sources are rejected on both transports and in `classify_status`. A DISPUTED backed only by `""` is not DISPUTED. → `source_grounding.is_substantive_source`, shared by the grounding invariant, the cache predicate, and `classify_status`.
- [x] The CLAUDE.md "Budget-exhaustion sentinel" sentence that says the grounded guard drops every UNVERIFIED is corrected, along with any related cache text. → also the two CLAUDE.md claims that the cache refused a quote-less DISPUTED, and handbook ch. 10 (see "Decisions and deviations").

### S06 — Size big requests for the model that runs them (WP-08)
- [ ] A small request-budget result records the count, count source, model, input ceiling, output reserve, fit decision, and any unavailability reason. It is built from the same inputs as the real request.
- [ ] Count-API results are called estimates, not "exact". Malformed or missing counts never become a trusted zero.
- [ ] When the count API is off or unavailable, model-specific padding applies to every locally counted part of the request, tool overhead included.
- [ ] The single-call vs chunked decision comes from that result, and every final chunk is checked too. Counts are cached only per model and per exact request shape.
- [ ] An oversized CSI group is subdivided deterministically. An item that can't be split is reported as unanalyzed and never truncated.
- [ ] Completed chunks are kept when another chunk fails. Reduced cross-chunk coordination and failed or skipped coverage are surfaced.
- [ ] Preflight respects the per-call concurrency gate without holding it across a whole multi-chunk pass.
- [ ] The batch extended-output threshold uses the same count source, and real time stays on its non-extended path.
- [ ] All nine WP-08 acceptance cases pass with stubbed counts.

### S07 — Show when compliance coverage is incomplete (WP-09)
- [ ] The expected coverage set is the grounded, controlling, non-process requirements; UNVERIFIED research stays advisory. The prompt and examples agree with that set.
- [ ] Returned rows are normalized against known IDs and omitted IDs are computed. An omitted row becomes a synthetic "unclear / not assessed" row with a reason and an origin marker, never "represented" or "missing".
- [ ] The result carries the expected count, the returned count, the omitted IDs and their count, and a completeness flag. Behavior with zero expected items is documented.
- [ ] Execution status stays separate from completeness; no new chunk status is added that would drop findings. Completed findings survive failed or skipped chunks.
- [ ] An ADD that depends on the requirement being absent everywhere is held as report-only, with the reason, when any relevant chunk wasn't assessed.
- [ ] A prominent partial-analysis notice appears in the DOCX and HTML reports, diagnostics, JSON/profile output, and program reports.

### S08 — Keep paid repair batches recoverable (WP-14)
- [ ] A structured collection outcome separates "there are reportable primary findings" from "the remote job and its repairs are finished". It distinguishes no repair needed, pending, temporarily unreachable, consumed, and conclusively unusable.
- [ ] The repair batch ID and request map are persisted, and legacy saved state is read conservatively.
- [ ] One cleanup decision is used by the GUI, the CLI, single-module runs, and program runs. State is kept while a repair is pending or unreachable, an all-failed run stays recoverable, and a valid zero-findings run may clear. Run and batch identity are checked before clearing.
- [ ] A reportable primary result with a pending repair is shown as provisional. Dependent paid stages (verification, cross-check, compliance, drawing impact) wait for the repair, and the report says which stages are waiting.
- [ ] Resume collects the same repair ID with no duplicate submission, and tests count actual paid calls. Program children keep their unresolved state independently.

### S09 — Count every paid attempt exactly once (WP-15)
- [ ] Each attempt gets a usage record: operation, model, transport, token and cache categories, search usage, and a stable identity (batch ID + custom ID + role).
- [ ] Primary and repair attempts are both kept even when the repair's findings replace the primary's. Failed, truncated, and malformed attempts with usage stay billable, and unknown usage stays unknown and labeled.
- [ ] Each operation has one billing input (attempts or an aggregate, never both). Real-time totals are unchanged for an equivalent scenario, and shared followers add nothing.
- [ ] The batch discount, cache TTL categories, cache reads, and search fees are each applied once, and repeated collection doesn't double count.
- [ ] Recovery reports separate earlier batch spend from spend caused by the recovery itself, and estimates are labeled as estimates.

### S10 — Read Word content controls, fields, and smart tags (WP-02)
- [ ] `w:sdt` / `w:sdtContent`, `w:smartTag`, and `w:fldSimple` are traversed structurally. Only stored field results are read; field instructions are never read as prose or executed.
- [ ] Accept-All revision rules apply at every depth, including inside controls and hyperlinks. Nothing is emitted twice and source order is preserved.
- [ ] Legacy `pN` / `tN` IDs keep their meaning: a wrapped table doesn't renumber ordinary tables. Newly readable wrapped content gets its own ID namespace.
- [ ] Applier: each new ID either resolves deterministically or is explicitly unsupported. An edit touching an unsupported wrapper is refused, even when matching text exists elsewhere.
- [ ] Text-bearing structures that are still skipped produce a warning, without invented completeness numbers.
- [ ] The supported and unsupported surfaces are listed in CLAUDE.md "DOCX supplemental content extraction", and the handbook row in "Known-wrong statements" is fixed.

### S11 — Keep every edit location, part 1: occurrence model and applier reader (WP-06B)
- [ ] Display groups are separated from executable occurrences. There is one occurrence per validated target (element identity + instruction identity), genuine duplicate emissions collapse, and indistinguishable anchors stay one uncertain occurrence.
- [ ] Occurrence IDs are stable and don't depend on input order or presentation counters. Program entries include module identity, and a missing original stays explicitly missing.
- [ ] Every consumer of `finding_id` and `EditEntry.key` is audited: receipts, filters, chat links, and conflict detection.
- [ ] The applier reads sidecar schemas 4, 5, 6, and 7, and `is_program` recognizes both 5 and 7. Legacy 4 and 5 behave exactly as before, and unknown versions are still refused.
- [ ] The applier detects incompatible edits to one region and holds them visibly. Every target is still resolved before any mutation.
- [ ] The sidecar writer still emits 4 and 5; the new writer lands in S12.

### S12 — Keep every edit location, part 2: sidecar writer and reports (WP-06B)
- [ ] The sidecar writes schema 6 (single module) and 7 (program). Each entry has an `occurrence_id` and a documented unique-entry key that includes module provenance.
- [ ] The same issue at p4 and p8 gives two entries and two correct tracked changes; a duplicate emission at p4 gives one; two files with two locations each give four. Cross-module entries don't collide, and conflicting edits are held.
- [ ] Both exporters show occurrence locations, and receipts account for every occurrence.
- [ ] An end-to-end test runs report → sidecar → applier → receipt and keeps every executable occurrence.
- [ ] The CLAUDE.md sidecar section and the handbook row in "Known-wrong statements" are updated, and a release-note line is added.

### S13 — Route specs by their own SECTION heading (WP-05)
- [ ] The SECTION heading is extracted from a bounded opening region, and only heading-shaped text counts. Section number and title are carried with provenance on the extracted spec, and assignment and routing share one extraction rule.
- [ ] A compact leading filename number is accepted only when the body heading corroborates it. The guards against dates, project numbers, NFPA references, and embedded numbers are kept.
- [ ] Contradictory strong filename and body evidence gives an explicit ambiguous result.
- [ ] Unsupported Division 27/28 scopes and legacy fire-alarm corroboration are unchanged.
- [ ] Distinct inputs with colliding basenames are rejected before submission at headless boundaries too, and routing provenance survives saved state and resume.
- [ ] `210500.docx` + SECTION 21 05 00 routes to fire suppression, `211313.docx` + its wet-pipe heading is supported, and related-section references can't override the real heading.

### S14 — Read Word automatic numbering (WP-03)
- [ ] Numbering is resolved from `numPr`, `numId`, `abstractNum`, level text, starts, overrides, restarts, and style-inherited numbering. Counters are kept per document and per list instance.
- [ ] Displayed labels reach review, section attribution, and structural detection (revisit the S03 heading candidates). Context-DOCX extraction behavior is defined.
- [ ] Literal source text and synthetic label spans are kept separately, and the reconstruction contract is updated deliberately. The structural locator ignores display labels.
- [ ] Typed numbering isn't duplicated. Unsupported or ambiguous numbering produces a warning instead of a guess.
- [ ] The applier refuses edits that touch a synthetic label, while body-text edits after a label still locate. Offsets are translated only when the mapping proves them.
- [ ] A boundary test runs extraction → prompt → finding → applier, and the handbook's extraction list mentions numbering.

### S15 — Respect rate-limit timing (WP-11)
- [ ] Retry-After (in seconds or as an HTTP date) and supported millisecond headers are parsed and validated; bad values fall back to the local policy.
- [ ] Backoff is bounded and exponential with injected jitter, and never retries before a valid server floor. Both the attempt count and the elapsed time are bounded, and retry-count settings keep their meaning, zero included.
- [ ] Authentication and invalid-request errors are not retried as transient.
- [ ] Concurrency permits are taken per outbound call, continuations and escalations included, and released before sleeping. The same gate is never acquired twice in a nested way.
- [ ] Each outbound path has a documented retry owner, including batch-result retrieval and token counting, and app-owned loops run with SDK retries off.
- [ ] Tests use an injected clock, sleep, and random source; one transient failure produces the expected number of calls.

### S16 — Make tracing optional; keep keys out of the environment (WP-13)
- [ ] Trace startup and reattachment run inside the worker's lifecycle protection on fresh and resumed runs. A trace failure logs one warning and the review continues without tracing.
- [ ] A partially started recorder is disposed, and only the run that owns the global recorder can clear it. Teardown errors never hide the real error, and widgets are restored on every exit.
- [ ] With deep trace on, core requests ask for the summarized thinking display (subject to the model's capabilities). Normal-mode requests stay byte-identical, and missing thinking is never logged as returned.
- [ ] Keys entered in the GUI are never written to `os.environ`. A per-run credential or client provider is passed through orchestration and captured when the run starts. Environment keys on the command line still work, and nothing sets and restores a global variable.
- [ ] Fake keys are absent from the process environment, saved state, report payloads, and trace metadata.
- [ ] GUI tests: install `python3-tk` if possible so the skipped GUI suites run; otherwise say so in the PR.

### S17 — Keep the verifier's citations; fix the fetch instructions (WP-16)
- [ ] Native citation data is captured where the response carries it: source URL and title, tool identity, cited text, locators, the attempt and model, and whether the result was fresh, cached, or shared.
- [ ] Document-index citations resolve only through their own response documents. Unknown citation shapes are observable without dropping an otherwise valid result.
- [ ] The optional fields round-trip through the cache, legacy entries still load and are labeled honestly, and no whole documents are persisted.
- [ ] Retrieval, native attribution, and semantic support appear as separate concepts in the trace and the report.
- [ ] The fetch instructions allow a URL supplied by the user or already present in the conversation, within the tool's constraints, and still require checks of support, edition, authority, and applicability. Capability gates are unchanged, and verifier goldens are reviewed.
- [ ] No lexical-overlap threshold changes acceptance.

### S18 — Make prompts, reports, and docs match the code (WP-17)
- [ ] The Haiku cache minimum is corrected to 4,096 in `src/core/api_config.py` and CLAUDE.md, after rechecking the provider's table.
- [ ] Count wording distinguishes provider estimates, local estimates, fallback padding, context capacity, and output capacity.
- [ ] Continuation caching is documented as existing behavior.
- [ ] No-op demotion is explainable: the reason is recorded at a stable normalization step, not inside a read-only report helper, and the banner count, severity counts, and sidecar exclusion agree.
- [ ] The cross-check scope is documented: it works within a chunk and a module, and a small program is not automatically checked across disciplines.
- [ ] Banners and summaries distinguish "analysis incomplete", "verification inconclusive", "operational failure", and "no issue found".
- [ ] Price commentary is corrected: Sonnet 5 is 40% of Opus 5 and Sonnet 4.6 is 60% of Opus 4.6. Recheck the prices first.
- [ ] Thinking display is described as visibility control, not a cost reduction.
- [ ] Every row still open in "Known-wrong statements" is fixed. CLAUDE.md, the README, and the handbook match shipped behavior, and no experiment is described as enabled.
- [ ] Changed goldens are reviewed one by one.

### S19 — Correctness release
- [ ] The release-note lines collected below move into README.md "Changelog (recent)" under the new version. They are specific and make no unmeasured quality or cost claims.
- [ ] The version is bumped in every literal the release check reads (see CLAUDE.md "Windows desktop build + self-update"), and `tests/test_release_metadata.py` passes.
- [ ] The full offline suite, `python -m pip check`, and the JavaScript check (`SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`) pass.
- [ ] The PR body has a short Windows smoke-test checklist for the owner to run before merging: start a review, recover a batch, use the HTML chat, and apply a sidecar to a copy.
- [ ] No tag is pushed by the agent; the PR states the exact tag command to run after merging.

### S20–S25 — Experiments (EX-01 … EX-06)
The same four boxes apply to each experiment, plus its own line.
- At session start, ask the owner whether a live evaluation is authorized. That needs a spending cap and an API key in the session's environment. Record the answer.
- Do the offline part: the investigation, any code behind a default-off switch, and offline tests using fake responses.
- Write a decision record at `plans/experiments/EX-0N-<name>.md` with dataset and configuration hashes, arm settings, measurements, spend, and the decision: enable, defer, reject, or not evaluated.
- Change no default unless the experiment's promotion criteria in Part 4 pass.

| Chunk | Common boxes done | Experiment-specific box | Decision |
|---|---|---|---|
| S20 (EX-01) | [ ] | [ ] Exact request layout captured (tools, system blocks, project context, per-file content, breakpoints, TTLs, model), and the breakpoint budget counted, resume caching included | |
| S21 (EX-02) | [ ] | [ ] First consumer chosen from measured parse failures. Strict tool arguments, forced tool use, and constrained final output kept distinct, and older saved batches still parse | |
| S22 (EX-03) | [ ] | [ ] Adjudicated dataset with held-out cases, caches isolated per arm (memory and disk), and one change tested at a time | |
| S23 (EX-04) | [ ] | [ ] Validation runs in observation mode only, source-reuse keys include the claim context, and validation and reuse are decided separately | |
| S24 (EX-05) | [ ] | [ ] Cache key built from every materially relevant input, failed or partial research never reused, a refresh path offered, and the profile's age shown | |
| S25 (EX-06) | [ ] | [ ] Observation mode behind a switch, starting within one module and then a small program. Every conflict cites both sides, and false joins and missed conflicts are measured | |

**Skipping the experiments:** if the owner starts a session with "Skip the Spec Critic experiments",
mark S20–S25 **DONE (not evaluated)** with the reason "skipped by owner", open one PR, and after it
merges print the final banner (plan, Part 1).

---

## Known-wrong statements

These say something the code doesn't do. Don't rely on them. The listed chunk fixes each one and
ticks it here.

| Fixed | Where | What it says | What's true | Fixed in |
|---|---|---|---|---|
| [x] | `CLAUDE.md`, "Budget-exhaustion sentinel" (~line 441) | "The `grounded` guard already drops every UNVERIFIED" | A grounded UNVERIFIED is cached and replayed | S05 |
| [ ] | `src/core/tokenizer.py` (~lines 105, 169–170, 433), `src/orchestration/pipeline.py` (~lines 671, 686) | `count_tokens` results are "exact" | They are provider estimates | S06 |
| [ ] | `handbook/04_input.md` (~line 20) | "Still not extracted" list | Content controls, field results, and smart tags are also not extracted; automatic numbering is lost too | S10 (numbering part in S14) |
| [ ] | `handbook/11_trust_model_and_output.md` (~line 28) | "The sidecar no longer under-emits" | True across files only; repeated locations in one file still collapse to one entry | S12 |
| [ ] | `CLAUDE.md`, "Prompt Caching" table (~line 633); `src/core/api_config.py` (~lines 961, 999) | Haiku's cache minimum is 2048 tokens | 4,096 for Haiku 4.5 | S18 |
| [ ] | `handbook/12_configuration_and_models.md` (~line 359), found by S01 | Haiku's cache minimum is 2,048 tokens | 4,096 for Haiku 4.5 | S18 |
| [ ] | `handbook/15_quality_engineering.md` (~line 162 and its `[^count]` footnote), found by S04 | "The suite is 49 test files holding roughly 645 test functions", offered as the order of magnitude | 150 test files and about 3,100 `def test_` functions (about 4,650 collected tests) at S04 | S18 |

---

## Decisions and deviations

Record here, with the date and chunk, whenever a session departs from the plan, finds a package
already done, or makes a judgment call the plan left open.

- **2026-09-23, plan revision:** The original WP-17 item 8 named the wrong models. It is corrected in the plan to: Sonnet 5 is 40% of Opus 5, and Sonnet 4.6 is 60% of Opus 4.6.
- **2026-09-23, plan revision:** Sessions run one at a time in the order above, so the original plan's guidance on parallel agents, worktrees, and a separate integrator (its §4) no longer applies.
- **2026-09-24, S01 — markers carry `raises=AssertionError`.** A bare `xfail(strict=True)` treats *any* exception as the expected failure, so a check broken by a renamed function would quietly read as "still open". The deleted script reported that case as ERROR. With `raises=AssertionError`, only a failed assertion counts as the defect; probe preconditions ("the flow really reached the worker hand-off") fail through `pytest.fail`, so a probe that stops reaching its target fails loudly. The reason text is unchanged, so a search for `fixed by S0N` still finds every marker.
- **2026-09-24, S01 — controls are separate tests.** The script checked a control inside the same check. In pytest a control that failed inside a strict xfail would read as "still open", so each control is an ordinary test beside its xfail and must always pass.
- **2026-09-24, S01 — WP-05 enters at the assignment seam.** The two routing checks run a real extracted DOCX through `assignments_for_specs`, not `route_spec`. S13 carries the SECTION heading on the extracted spec, so a check that called `route_spec` directly could stay red after a correct fix.
- **2026-09-24, S01 — source hints.** Three became behavioral tests. WP-11 drives the batch-results retry loop with a real `RateLimitError` carrying `retry-after`. WP-12 runs the exporter's exact script under Node (`tests/fixtures/chat_key_probe.js`). WP-13 runs the three real GUI flows against fake apps; they skip without tkinter, and here they ran and xfailed under Python 3.12 with Tk. The fourth, WP-17's Haiku cache minimum, has no behavior to exercise: the number appears only in comments and docs, and the triage no-cache decision is right either way. It stays a documentation check, made precise. It parses each "Haiku cache minimum" claim, requires the claims to exist (so deleting them can't pass as a fix), and requires 4,096. Its control pins the behavior that matters: triage is not cached.
- **2026-09-24, S01 — more reproductions than the script had.** These were added as strict xfails too. WP-02 (S10): the block control's order, inline and drop-down control text, an unresolved drop-down reaching the placeholder detector, smart-tag text, an insertion inside a hyperlink, and an edit aimed at a control landing on a plain copy elsewhere. WP-03 (S14): numbering labels in the review prompt and section attribution. WP-04 (S03): each mutation producing only its own alert, the other WP-04A quantity phrases, the "shall not deviate" / "cannot depart" forms, and ASCE with an em dash or the word "Standard". WP-07 (S02): end to end through `apply_sidecar`, in both input orders. WP-10 (S05): a whitespace-only source. The WP-06B check belongs to S12, where the sidecar writer changes.
- **2026-09-24, S01 — two WP-01 acceptance lines wait for S03.** "Clean documents produce zero alerts" and "each mutation produces no unrelated alerts" can't pass until S03, because every PART heading is falsely flagged today. S01 changes no behavior, so both are strict xfails owned by S03. The halves that already hold are ordinary tests: each mutation raises its own alert, and the clean fixture is clean for every non-structural detector.
- **2026-09-24, S01 — no per-package suites yet.** The suggested suites (`test_extraction_content_controls.py`, `test_extraction_numbering.py`, `test_heading_structure.py`) were not created. The reproductions stay in one module so a single search finds every open defect; the fixing sessions add focused suites.
- **2026-09-24, S01 — found, for S10.** An edit aimed at hyperlink text is refused with the reason "the target text sits inside an existing tracked revision by another author". `DocumentEditor._target_paragraph` looks at every nested run, so hyperlink runs trigger the revision message. The refusal is safe, but the reason is wrong.
- **2026-09-24, S01 — fixtures checked in a real word processor.** Every ready-made fixture was opened once in LibreOffice Writer 24.2 and exported to text. The auto-numbered fixture displays "PART 1 GENERAL", "1.01 SUMMARY", "A. Provide…", and every wrapped sentinel is visible text. CI has no word processor, so the XML is pinned instead.
- **2026-09-24, S02 — known names are removed, not replaced by a placeholder.** The plan allows either ("may remove the exact known filename"). Removal keeps an id unchanged wherever the old rule was already right — a finding that names only its own file — so ids move only where the old key was wrong. `TestIdStability` pins both halves against the old rule.
- **2026-09-24, S02 — the context is derived, not stored.** Each of the three stages calls `finding_identity_context_for_submission(submission)` on the same submission (files reviewed ∪ review request map ∪ re-extracted specs) instead of carrying a context field on `CollectedBatchState`. It is a pure function of one object, so the stages cannot disagree, and no saved state changes. An AST tripwire fails if stage code passes any other context.
- **2026-09-24, S02 — a space is a token boundary.** Prose separates a file name from the words before it with a space, so a known name at the end of an unrecognized multi-word name ("Old Work Results.docx" with only "Results.docx" known) is still removed from it. Documented in the class docstring and CLAUDE.md, not pinned as desired behavior.
- **2026-09-24, S02 — new outcomes and exit 3.** "Non-success when anything is held" is read as held by the new binding and destination checks. They get their own outcomes, `FILE_AMBIGUOUS` and `DESTINATION_CONFLICT`, and a new exit status `3` that doesn't need `--strict`: the inputs are wrong, not the edits. Policy holds keep their meaning (`--strict` still ignores them), and `FILE_MISSING` still needs `--strict` as before, since supplying a subset of the specs is legitimate.
- **2026-09-24, S02 — two binding rules the plan implies but doesn't list.** A sidecar that spells one name two ways (`Spec.docx` / `spec.docx`) is `FILE_AMBIGUOUS`: names match case-insensitively, so the two can't be told apart, and before this both groups bound one file and wrote one destination twice. A sidecar `fileName` containing `/` or `\` never binds (`FILE_MISSING`, "names a path"), per WP-07 item 6.
- **2026-09-24, S02 — destination equals source now refused while planning.** It used to apply the edits in memory and then demote them to `FAILED`, and a dry run skipped the check and reported `WOULD_APPLY`, which was a dry-run/real-run mismatch. It is now `DESTINATION_CONFLICT` in both, and `test_the_source_is_never_the_destination` was updated to say so. `_apply_to_file` keeps a last check right before the save, and a test proves it on its own.
- **2026-09-24, S02 — re-running over a folder with last run's output is refused.** `--specs <dir>` sweeps in the previous `*.applied.docx`, which is then a supplied specification that the new copy would overwrite. Before, it was overwritten silently, along with any review work saved in it. The message says to move it or change `--output-dir` / `--output-suffix`. This is user-visible, so it has a release-note line.
- **2026-09-24, S02 — case policy.** Two paths are one input when `Path.resolve()` + `os.path.normcase` agree (on Windows this also absorbs case). On a case-insensitive volume that `normcase` doesn't know (macOS), the same path up to case also counts when the filesystem reports one non-zero inode. A zero inode never counts, because some filesystems report 0 for every file. Destination checks compare case-folded paths everywhere, which is the refusing direction. Found in review (Codex, P2): an existing destination can also be a supplied spec under another name, as a hard link. Saving rewrites the file in place, so that spec would change. Existing destinations are now also compared by `(st_dev, st_ino)`, against every supplied spec and against each other; a zero inode proves nothing. An existing copy that is nobody else's file is still replaced, as before.
- **2026-09-24, S02 — found: CLAUDE.md's applier test count was stale.** It said 213, but master had 221. It now says 261 and lists `test_applier_bindings.py`.
- **2026-09-24, S03 — "provenance" is the number's source, and there is only one today.** The preprocessor receives text, not the paragraph map, so no style names are available to it. `HeadingCandidate.provenance` is `"typed"` for every heading now. S14 adds automatic numbering as a second provenance, and should revisit the candidates there (as its checklist says).
- **2026-09-24, S03 — a bare integer is never a heading number.** The plan asks to exclude integer-led prose. The rule excludes every integer-led line, including a heading such as "1 GENERAL" written without the word PART. SectionFormat PART headings carry the word, so this only loses detections in formats this app doesn't target, and a missed heading can't cause a false alert. A dotted number also needs a title-shaped title: a capital first letter, no `shall` / `must`, no mixed-case sentence ending in a period, not a table row, and at most 120 characters.
- **2026-09-24, S03 — ancestor/leaf policy: report the highest empty heading.** An empty heading is reported only when its parent is not empty. Every empty heading is then covered by exactly one alert, and "report an empty PART when it has no substantive descendant content" holds even when the PART has articles. The fixtures got an `empty_part` mutation (PART 2's only article loses its only body; expected alert: `PART 2 PRODUCTS`), and the fixture oracle in `tests/test_spec_docx_fixtures.py` states the same policy from declared roles.
- **2026-09-24, S03 — two additions to "a heading's content".** Run-in text after a colon (`1.03 REFERENCES: ASTM A53`) is the heading's own content. The structure also stops at an `END OF SECTION` line and at the extractor's footnote, endnote, and header/footer blocks, so a last article with nothing before `END OF SECTION` is now flagged. Before, that line counted as its body. The text-box block does not stop the structure: a text box is anchored in the body and can hold a heading's only content. Duplicate headings are still compared across the whole file, as before.
- **2026-09-24, S03 — suppression cues are bound to the citation.** The plan asks for "citation-related historical/rejection phrases." Each cue must touch the citation (only an article and punctuation before it; only copulas, relative pronouns, and punctuation after it), except `previously` / `formerly` / `no longer` / an old-edition phrase anywhere earlier in the clause. Those lose their force when `shall` / `must` / `will` / `should` stands between them and the citation. The window is also cut at neighboring citations. That is what makes sentences with several citations come out right. Two effects go beyond the plan's list. "Previously approved submittals shall comply with 2022 CBC" now flags (it was suppressed). A few clear rejections now suppress where the old code flagged them: `instead of` / `rather than` / `in lieu of`, `supersedes` / `replaces`, `, not [per] <citation>`, and `not the <citation>`. Bare `not` is still not a cue.
- **2026-09-24, S03 — found in review (Codex, P2): a cue shared by a list.** Cutting each window at the neighboring citation hid a shared cue from all but one member of a coordinated list: "Previously, the 2019 CBC and 2019 CMC applied" still flagged the CMC. Citations joined only by a comma, `and`, `or`, `and/or`, or `&` (optionally followed by `the`) are now judged as one citation, so the cue before the list or after it covers every member; any other words between two citations still keep them apart. Fixing it exposed an older bug in both year/code detectors: in "2019 CBC, 2019 CMC", the `<code> <year>` pattern also matches "CBC, 2019", which overlaps both real citations without being contained in either, so it was reported as a third citation. It was reproduced on master for the stale and the invalid-year detectors, including the data-center modules. An overlapping match is now skipped. That removes only duplicate alerts, so no module's policy changes.
- **2026-09-24, S03 — California long forms: a little more than the four demonstrated.** Each demonstrated form is added, plus close relatives: `2022 Title 24` (the mirror of `Title 24, 2022`), `2022 California Green Building Standards Code` (CALGreen's formal name), and `CPC (2019)` (the parenthesized year without the word edition). All are in the California module only. `Title 24` is matched as itself, and the alert names only the year, so it is never equated with the CBC. The invalid-year detector reads these forms too, so `Title 24, 2024` is an invalid California cycle.
- **2026-09-24, S03 — ASCE also reads `SEI/ASCE 7-02`**, the form ASCE used for the 2002 edition. One old quirk is kept: a designation with no separator at all (`ASCE 716`) still reads as 7-16, as before.
- **2026-09-24, S03 — placeholder decisions.** `[OPTIONAL …]` and `[OPTIONS …]` stay placeholders along with `[OPTION …]`: in the supported templates a bracketed OPTIONAL is a keep-or-delete choice still to be made. `TBD-200` is an identifier, like `TBDF-200` and the `XXX-12` model number the template-marker rule already skips, so a TBD joined to a hyphenated or longer token never flags; `TBD - see drawings` and `TBD—by Architect` still do. A bare `tbd` in lower case flags too. The bare-TBD pattern is last in the list, so every older alert keeps its place.
- **2026-09-24, S03 — naming: "dominant" means a majority, and names without a section number stay out.** A style is the project's when more than half of the CSI-named files use it. Otherwise every CSI-named file gets a neutral "Mixed CSI filename styles (no dominant style)" alert with `dominant_style: None`. A name without a leading section number is neither counted nor flagged. Before, it was flagged as `found_style: "other"` whenever a recognized style dominated. The plan keeps the naming notice apart from coverage and routing, and a missing section number is a routing matter.
- **2026-09-24, S03 — the naming alert's `context` now says something.** It was just the file name, repeated under the same file name in the report. It now names the file's style and the project's: "23-31-13-Metal-Ducts.docx — dash-separated; most files are space-separated". That is the only change in the two preprocessor goldens (one line each, reviewed). Both exporters' section intro changed from "…differs from the project's dominant style" to "…differs from other files in the project" (`NAMING_ALERTS_DESCRIPTION`, shared), and the pipeline's preflight log line has a mixture wording.
- **2026-09-24, S03 — found, for S14:** the extractor's section attribution (`extractor._is_heading_paragraph`) is a separate heading heuristic, and it still treats a line like "1.5 inches minimum cover" as a heading. Its docstring calls such false positives harmless (they move a section boundary by one paragraph). S14 changes section attribution anyway and could reuse `heading_candidates`.
- **2026-09-24, S04 — one transaction model: rollback.** The plan allows either rolling back the unfinished part of a turn or answering it with an error `tool_result`. A turn (the question plus its tool rounds and `pause_turn` continuations) now commits only on `end_turn` or a stop sequence, and every other ending discards it whole, the question included. Two API rules decided it: a follow-up user message may hold only `tool_result` blocks (so an error result can't share a message with the reader's next question, and an unresolved server tool call would make that request fail), and a call whose input never arrived whole has no input to send back. The reader still sees everything that arrived, marked "Interrupted", and the question goes back into the message box.
- **2026-09-24, S04 — `max_tokens` and refusals roll back too.** Both are terminal stop reasons, but the answer is incomplete, and the API's guidance for a mid-stream refusal is to discard the partial output. So asking the chat to "continue" an answer cut off at the length limit no longer works; the notice says to ask a narrower question or choose a lower effort instead. That is user-visible, so it has a release-note line. `model_context_window_exceeded` and unknown stop reasons are handled the same way (the first as a notice, the second as an error), and `stop_sequence` commits like `end_turn`.
- **2026-09-24, S04 — invalid JSON is a stream failure; a missing required field is a model mistake.** The chat's tools don't use eager input streaming, so the API validates tool input before streaming it, and input that won't parse, or parses to something other than an object, means the stream itself can't be trusted: the turn fails and nothing runs. Input that parses but lacks a required field is answered with an error `tool_result` (`"Not run: missing required input …"`), the documented pattern that lets the model retry. Either way no tool runs on `{}` or on defaults.
- **2026-09-24, S04 — found and fixed: the chat's `pause_turn` continuation lacked the `container` id.** Its web search is `web_search_20260209`, whose dynamic filtering runs in a code-execution container. A pause while filtering needs the container named on the continuation, or the API rejects it, which is the failure CLAUDE.md ("Server-tool containers must survive a `pause_turn` resume") records for the main app. The chat now carries the last container id a response reported to every later request of the same turn, and never into a new turn. It was not in the plan's list, but WP-12 asks for valid continuations.
- **2026-09-24, S04 — forward compatibility.** Unknown event types and delta types are ignored, as the API's versioning policy asks; a known event that breaks the stream's contract (out of order, wrong block type, after `message_stop`) fails the turn. An event left without its terminating blank line at end-of-stream is not dispatched, per the SSE format, so a stream cut off inside its final `message_stop` frame is incomplete. The SDKs do the same.
- **2026-09-24, S04 — two small additions the plan implies.** Forget key and leaving the page (`pagehide`) also stop an answer in flight, since the next request of that turn would need the key. A stored model preference is now checked against the offered models: an older report's `claude-sonnet-4-6` in `sessionStorage` used to select nothing, and the request went out with an empty model.
- **2026-09-24, S04 — the probe became the harness.** `tests/fixtures/chat_key_probe.js` is deleted. The WP-12 check in `tests/test_plan_open_defects.py` now runs on the new harness, its strict-xfail marker is removed, and its control still passes. Three string pins in `tests/test_html_report_exporter.py` were updated for the new expressions (`serverToolsFor(turn.model)`, `effort: turn.effort`) and the new key copy; the behavior they stood in for is now asserted under Node.
- **2026-09-24, S04 — found in the harness: Node's `vm` swallows a throwing accessor on the sandbox object.** The first "storage unavailable" mode defined a throwing `sessionStorage` getter on the sandbox; Node's interceptor turned that into `undefined`, so it simulated *missing* storage, and a mutation that removed the chat's guard survived. The accessor is now installed from inside the context, the harness logs each denied access, and the test asserts the page actually hit it.
- **2026-09-24, S05 — one contract, two loops.** Both transports now classify a finished (non-paused) conversation through `verifier.classify_verification_turn` and build the result with `_stamp_verdict_result` or `_failure_result`; only pausing, retrying, and batching stay per transport. `VerificationResult.outcome` (runtime-only, never cached) names the kind on every result the verifier builds. Reproduced first on master `577f578`: text with no JSON after a searched turn was a grounded, cacheable UNVERIFIED in real time and a failure on batch; a verdict call with no `verdict` field or `"PROBABLY"` was a grounded, cacheable UNVERIFIED on both; a verdict call whose input was not an object was a clean UNVERIFIED in real time and a failure on batch; every real-time failure reported zero tokens.
- **2026-09-24, S05 — a turn with no search evidence is a failure on both transports.** The plan doesn't say how to classify it, and the transports disagreed: real time called it clean uncertainty (and escalated CRITICAL / HIGH findings to Opus), batch called it an operational failure. Batch's reading was kept — nothing was checked, so "insufficient evidence" would say something false — as `no_search` or `search_failed`. So real-time no longer escalates these (nor malformed replies); `should_escalate_verification` never escalates a failed pass, and the plan forbids adding a repair loop. The gate now counts search **and** fetch result blocks, as real time did: the batch gate also required a search and a non-zero `web_search_requests` counter, so a fetch-only conversation (which CLAUDE.md said passed on both) failed on batch. Blocks and counters agree in production; only fakes could tell them apart.
- **2026-09-24, S05 — what "malformed" means.** A verdict call whose input is not an object, a missing, null, or unknown `verdict` (no longer coerced to UNVERIFIED), several verdict calls that disagree, or text holding no valid verdict object. Case and whitespace in the verdict are forgiven, and the other fields stay tolerant, because each already has a safe defined outcome: missing sources demote through grounding, a missing quote demotes CONFIRMED / CORRECTED, and a missing explanation is just empty. A demoted verdict is still a well-formed verdict (genuine uncertainty), not a failure.
- **2026-09-24, S05 — incomplete stops are named.** `refusal` (with `stop_details`), `max_tokens`, `model_context_window_exceeded`, and anything else (`stop_sequence`, `None`, an unknown reason) each get their own outcome and explanation, and `retry_telemetry["terminal_reason"]` carries the outcome on both transports. `test_verification_stop_parity.py` pinned the old shared explanation and `terminal_reason: "terminal_unverified"`; it was updated on purpose and now also pins the outcome and the kept usage.
- **2026-09-24, S05 — two synthesized UNVERIFIEDs became failures.** The batch safety net ("No verification result after all batch waves.", reached when polling detaches or fails before the finding's wave finishes) and "No API key available" were clean INSUFFICIENT_EVIDENCE results; nothing was checked in either, so both are now VERIFICATION_FAILED (`no_result`, `no_api_key`). A finding left unresolved after the last wave with no failure class at all becomes `no_result` too; that path can't be reached today.
- **2026-09-24, S05 — usage is kept per conversation; cross-attempt accounting is for S09.** Every exit keeps the known usage of the conversation that produced it: each classified failure on both transports, a real-time exception after some responses, and the batch loop terminals (continuation cap, unresolved, tracker-terminated), which used to drop `accumulated_usage`. Not done, and recorded here for S09 (WP-15's attempt model): an attempt abandoned for a retry is not added to the final result. That covers a real-time retry after partial responses, a batch fresh retry after a paused conversation (its `prior_usage` is dropped when the retry context is built), and the real-time fallback after paid batch waves. Adding these into `call_usage` now would pre-empt S09's attempt identity.
- **2026-09-24, S05 — sharing uses a positive marker.** `_shareable_verdict` shares an UNVERIFIED only when the verifier stamped it `outcome == "verdict"` (and it isn't failed, budget-exhausted, or local). This fails safe: anything else, a synthesized terminal included, costs at most one extra attempt and is never an inherited non-answer. The budget terminals (`continuation_cap`, `search_ceiling`) are "budget failures" under the plan's exclusion even when `budget_exhausted` is False. The test doubles of a clean UNVERIFIED in `test_verification_singleflight.py` and `test_governing_basis_context_gate.py` declare `outcome=OUTCOME_VERDICT`, since `verify_finding` now stamps it.
- **2026-09-24, S05 — the cache predicate also refuses replays.** Besides the plan's list, a `hit` or `shared` result is never re-stored, because that would reset its age and pass a stale verdict off as fresh, and a local result is recognized by `verification_mode` as well as `cache_status`. Contested verdicts stay cacheable (they are grounded conclusions, and `models_disagreed` is persisted), and DISPUTED stays citation-gated but not quote-gated (the existing rule, pinned by a test). Two CLAUDE.md passages said the cache refused a quote-less DISPUTED. It never did, and both are corrected.
- **2026-09-24, S05 — what an invalid record is.** A creation time that is missing, a string or bool, not finite, not positive, or more than a day in the future (to allow clock skew between machines). A present `last_used_ts` that is invalid (absent or zero is a legacy row). A present persisted field of the wrong type, or a count that is negative, fractional, or not finite; a whole-number float such as `3.0` is fine. Found while reproducing: a single string timestamp raised out of `load_from_disk`, and the pipeline then started with an **empty** cache, so one hand-edited row threw away every valid one. The load now reports ignored rows in the run log.
- **2026-09-24, S05 — "substantive" uses `normalize_url`.** A source counts when `source_grounding.normalize_url` can reduce it to something, so punctuation-only entries such as `").."` don't count either, the same line the grounding validator already drew. A blank citation the model made still shows as a rejected `empty` citation (it is not dropped at parse time), and blank entries are dropped from every persisted source list.
- **2026-09-24, S05 — source-string pins became behavioral tests.** `test_budget_exhaustion.py::TestVerifierSourceInspection` (a local variable name, a helper signature) and `test_verification_failed_status.py::TestVerifierExceptionPathsMarkFailed` (it counted `failed=True` literals) both said the paths "can't be driven without a real API call". The scripted clients in the new `tests/fixtures/verification_drivers.py` drive them, now with real SDK exceptions.
- **2026-09-24, S05 — small consequences worth knowing.** The batch text fallback reads dict-shaped text blocks, as real time always did (a dict-shaped batch message with a text verdict used to read as "no content"). A shared follower's diagnostics event now records `api_call=False`, since the summary already skipped it. `_search_gate_failure` and `_collect_search_evidence` had no callers left and were removed. Handbook ch. 10 also said a grounding downgrade renders as DISPUTED and quoted `_CACHE_SCHEMA_VERSION = 3`; both were wrong and are fixed, since that table was being edited anyway.

---

## Release-note lines (collected for S19)

Each session adds one plain line per user-visible change. S19 moves them into README.md.

- (S02) Findings that differ only in wording no longer merge because both mention a file name. "…requires copper pipe in 210500.docx" and "…requires PVC pipe in 210500.docx" are now two findings, while the same issue reported in several files still groups. Some finding IDs change because the old key was wrong; existing reports and sidecars are untouched.
- (S02) Edit applier: when two different supplied files share a file name, neither is edited (`FILE_AMBIGUOUS`), in either input order. An edited copy that would overwrite any supplied file, such as last run's `*.applied.docx` left in the folder, is refused (`DESTINATION_CONFLICT`). Both are decided before anything is written, the other files are still processed, and the run exits 3.
- (S03) Structure alerts: a clean spec no longer gets an "Empty section" alert for every PART heading, and lines such as "2 coats of primer shall be applied." are no longer read as headings or duplicate headings. A PART with no content anywhere is one alert, not one per empty article. An article with nothing before END OF SECTION is now flagged.
- (S03) Stale code-year alerts are no longer silenced by unrelated nearby words ("prior to fabrication", "historical society", "may not deviate from"). Citations described as old or rejected ("previously", "superseded", "shall not follow", "instead of") stay quiet, and each citation in a sentence is judged on its own. ASCE 7 written as ASCE/SEI 7-16, ASCE Standard 7-16, with an en or em dash, or as 7-2016 is recognized. For the California program, "2019 California Building Standards Code", "2022 Edition of the CBC", "CBC (2022 edition)", and "Title 24, 2022" are recognized too.
- (S03) A comma-separated list of code citations ("2019 CBC, 2019 CMC") is no longer reported with an extra, phantom citation.
- (S03) Placeholders: a bare "TBD" is now flagged, once. "[EDITION …]" and "[SELECTED …]" are no longer mistaken for EDIT and SELECT placeholders, and part numbers such as TBD-200 stay clean.
- (S03) File naming: compact (210500) and SECTION-prefixed names are recognized, names without a section number no longer hide a mixture, and when no style is used by most files the report says the styles are mixed instead of picking one.
- (S04) Ask AI chat: an answer that fails partway (an API error, a dropped connection, a cut-off or malformed response, a refusal, the length or tool limit, Stop, New chat, or a model change) is now shown as interrupted and left out of the conversation, and the question goes back into the message box. Before, some failures were silently kept as if the answer were complete, and a failure during a report-tool call could make every later message fail. An answer cut off at the length limit can no longer be continued by asking "continue"; ask a narrower question or choose a lower effort.
- (S04) Ask AI chat: the API key is now kept only in the page's memory, not in the browser tab's session storage. Reloading or closing the report forgets it, and a key an older report left in session storage is deleted when a report is opened. A browser that blocks or restricts storage no longer breaks the chat.
- (S04) Ask AI chat: web-search citations stay attached to the text they support, shown as numbered sources, and are kept when the conversation continues. A long web search that pauses while filtering its results now continues instead of failing.
- (S05) Verification: a claim the verifier could not settle ("Insufficient evidence") is no longer saved in the claim cache, so the next run checks it again instead of replaying the old answer for up to 60 days. Within one run it is still shared among identical findings. When the cache file loads, entries an earlier version saved this way are ignored, along with any row holding an invalid date or value, and the run log says how many; valid entries still load.
- (S05) Verification: a garbled or missing verifier reply (no verdict, an unknown verdict value, a malformed tool call, text with no valid verdict) and a reply that ran no web search now show as "Verification failed" instead of "Insufficient evidence", the same way in batch and real-time runs, and the tokens they used are in the cost estimate. A refusal, the output limit, and the context window are each named in the explanation. In real-time runs these replies are no longer escalated to Opus. A batch run whose polling stopped before a finding's wave finished marks that finding as failed, not as insufficient evidence.
- (S05) Verification: an empty or whitespace-only citation never counts as a source, so a "Disputed" backed only by blank sources shows as "Insufficient evidence".

---

## Starting point

**Re-measured by S01** (2026-09-23/24) at master `6b48503`, the merge of PR #374. `src/`,
`applier/`, `tests/`, `scripts/`, and `evals/` are identical to `f9da027`. Spec Critic 3.9.0,
Anthropic SDK 1.7.0. No failures existed on master.

- **Python 3.11.15** (CI's version), in a fresh venv built from `requirements-dev.txt`. `python -m pytest -m "not network"`: **3,979 passed, 14 skipped, 10 deselected** in about 20 seconds, identical to the reference below. The same 14 container skips: 11 tkinter, 1 tiktoken rank file, 1 PyInstaller, 1 Playwright.
- **Python 3.12.3 with Tk**, so the tkinter suites run as they do in CI: **4,156 passed, 3 skipped** (rank file, PyInstaller, Playwright).
- `python plans/check_plan_status.py`, its last run before S01 deleted it: **25 OPEN, 0 FIXED, 0 ERROR**.
- **After S01:** 3.11: 4,159 passed, 18 skipped, 48 xfailed. 3.12 with Tk: 4,337 passed, 3 skipped, 51 xfailed. The 4 extra skips on 3.11 are the GUI probes, which need tkinter.

Container notes for later sessions:

- The system interpreter can't run the suite. Its Debian `cryptography` package has no `_cffi_backend`, so `pypdf` fails to import and two modules error at collection. Build a venv from `requirements-dev.txt` (then `pip install -e . --no-deps`) and run pytest from it.
- `python3.11-tk` can't be installed (the deadsnakes PPA is blocked), but `apt-get install -y python3-tk` adds Tk for Python 3.12. A 3.12 venv from the same lock file runs every GUI suite.

Reference measurement from the plan revision (2026-09-23, master `f9da027`): 3,979 passed, 14 skipped,
10 deselected in about 30 seconds; `plans/check_plan_status.py` 25 OPEN, 0 FIXED, 0 ERROR.

---

## Session log

Newest first. One entry per session: date, chunk, PR, what changed, test result, and what's left.

### 2026-09-24 — S05: Verification failures and cache (WP-10)
- **PR:** (added in a follow-up commit)
- Started at master `577f578` (the merge of #378). Both baselines matched S04's final numbers exactly: 3.11 had 4,656 passed, 18 skipped, 22 xfailed; 3.12 with Tk had 4,834 passed, 3 skipped, 25 xfailed. No failures existed on master.
- **Reproduced first**, with a scratch script that drove both transports on master: a grounded UNVERIFIED was cached and replayed on the next lookup; text with no JSON after a searched turn was a grounded, cacheable UNVERIFIED in real time but a failure on batch; a verdict call with no `verdict` field, or with `"PROBABLY"`, was a grounded, cacheable UNVERIFIED on both; a verdict call whose input was not an object was a clean UNVERIFIED in real time and a failure on batch; every real-time failure reported zero tokens; a DISPUTED backed only by a whitespace source rendered as DISPUTED; and one string timestamp in the cache file raised out of `load_from_disk`, so the run started with an empty cache.
- **Classification.** `verifier.classify_verification_turn` classifies every finished conversation on both transports, and `_stamp_verdict_result` / `_failure_result` build the result; only pausing, retrying, and batching stay per transport. `VerificationResult.outcome` (runtime-only) names the kind. A malformed verdict (input that is not an object; a missing, null, or unknown `verdict`; conflicting calls; text with no valid verdict), a turn with no search or fetch result, and every incomplete stop (refusal, `max_tokens`, the context window, anything else) is a failure: UNVERIFIED, ungrounded, never escalated, never cached, with the conversation's usage and evidence kept. The batch loop's own terminals (continuation cap, non-retryable error, unresolved, crashed fallback, the safety net) use the same builder, and "no API key" and the safety net are now failures.
- **Cache.** `verification_cache.cache_ineligibility_reason` is the one predicate at `put`, `get`, and `load_from_disk`. Only a grounded CONFIRMED / CORRECTED / DISPUTED with a substantive citation (and a quote, for CONFIRMED / CORRECTED) is reused, and a replay is never re-stored. The load checks each record on its own (timestamps, field types, counts), ignores the bad ones, and counts them in `stats()["rejected_on_load"]`; the pipeline's load log line reports the count. No schema bump.
- **Sharing.** `pipeline._shareable_verdict` shares an UNVERIFIED in-process only when the verifier stamped `outcome == "verdict"` and it is not failed, budget-exhausted, or local. A shared follower's diagnostics event is not an API call.
- **Sources.** `source_grounding.is_substantive_source` (a value `normalize_url` can reduce to something) is used by the grounding invariant, the cache predicate, and `classify_status`.
- Tests: `tests/test_verdict_classification_contract.py` (138) and `tests/test_verification_cache_eligibility.py` (138) are new, with `tests/fixtures/verification_drivers.py`, which drives the real `verify_finding` and the real batch wave loop from scripted responses. `test_verification_singleflight.py` gained 12 WP-10 tests. The source-string pins in `test_budget_exhaustion.py` and `test_verification_failed_status.py` became behavioral tests, with real SDK exceptions. `test_verification_stop_parity.py` was updated on purpose (named outcomes, kept usage). The 3 S05 strict xfails (2 markers) became regression tests.
- Mutation-checked: 41 breakages, and every one turned a test red. 21 in the verifier (the old unknown-verdict coercion, a missing verdict tolerated, a malformed call falling through to the text, conflicting calls not detected, text without a verdict read as UNVERIFIED, no evidence gate, each of the three incomplete stops not named, usage dropped by each transport, by a failed continuation, and at the batch continuation cap, fetch results ignored by the batch gate, a failure not flagged, dict text blocks ignored, blank sources counted by the invariant, a failed escalation replacing the first pass, the safety net and no API key not failures, budget exhaustion not stamped), 12 in the cache (UNVERIFIED reused, blank citations counted, replays re-stored, `get` or the load not applying the predicate, non-finite, future, string, and missing timestamps accepted, field types not checked, rejected rows not counted, blank list entries kept), 6 in the pipeline (sharing ignoring the outcome, excluding a grounded UNVERIFIED, or inheriting a failure, a budget shortfall, or a local result; the load log omitting ignored rows), 1 in `classify_status`, and 1 in diagnostics. One (sharing inheriting a failure) survived at first; parametrized sharing cases now catch it.
- Docs: CLAUDE.md (a new "Verification outcomes: one classification contract, one cache predicate" section; the grounding-invariant cache bullet; the DISPUTED quote paragraph; routed-program single-flight; the budget-exhaustion sentence; incomplete-stop parity; the exactly-once safety net; two trust-table rows; the §9 fixture bullet), README (design bullets, pipeline step 6, and a new "Verification Failures, Uncertainty, and the Claim Cache" section), handbook ch. 10 (the parser, a new "One classification contract" section, the gates, the cache refusal table, the schema version, the trust rows, the takeaways), and ch. 9, 11, and 15.
- Tests: 3.11 had 4,954 passed, 18 skipped, 19 xfailed. 3.12 with Tk had 5,132 passed, 3 skipped, 22 xfailed. `pip check` was clean, and the JavaScript-required HTML suites passed with `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`.
- Not done, by design: an attempt abandoned for a retry is still not added to the final result (a real-time retry after partial responses, a batch fresh retry after a paused conversation, the real-time fallback after paid batch waves). That is S09's attempt model; see "Decisions and deviations".
- **Next:** S06.

### 2026-09-24 — S04: Chat (WP-12)
- **PR:** [#378](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/378)
- Started at master `01027c3` (the merge of #377). Both baselines matched S03's final numbers exactly: 3.11 had 4,563 passed, 18 skipped, 23 xfailed; 3.12 with Tk had 4,741 passed, 3 skipped, 26 xfailed. No failures existed on master.
- **Reproduced first.** The new harness, run against master's script, showed the P1-5 case end to end: an `error` event after a partly streamed `tool_use` produced no notice, and the next request carried that `tool_use` with `input: {}` and no `tool_result` (the API rejects that, so every later message fails). The tool-round limit left the same orphan. Also on master: CRLF or CR endings split across reads lost the whole answer, EOF counted as success, citations were never attached to their blocks, throwing storage stopped the script at load, and the `pause_turn` continuation sent no `container`. 71 of the 88 new tests failed on master; the 17 that passed are happy paths, LF streams, and preference handling.
- **Stream and errors.** An SSE line parser (CRLF / LF / CR, a CR at a chunk end waits, multi-line `data:`, comments ignored, an unterminated final event never dispatched) with a fatal UTF-8 decoder flushed at the end. Each event is checked as it arrives; a response counts only at `message_stop` with a stop reason and every block closed. Only `JSON.parse` is guarded. Tool input is parsed strictly at `content_block_stop`.
- **Transaction.** Turns build up in `turn.messages` and commit only on `end_turn` / stop sequence (`commitTurn`); every other ending discards the turn (`finishTurn`, which closes a turn exactly once, aborts its request, and restores the controls). History trimming drops whole turns. The partial answer is marked "Interrupted", and the question returns to the message box. Stop, New chat, a model change, Forget key, and `pagehide` close the turn at once, and a closed turn's later reads are dropped. Every stop reason and both loop limits are handled visibly.
- **Citations and blocks.** `citations_delta` is appended to its own text block's `citations`, replayed verbatim, and shown as numbered sources; a citation without an https URL is replayed but never shown. Thinking signatures and web search results replay byte-for-byte. The `container` id reaches later requests within the turn.
- **Key.** Page memory only; `prefRemove("sc_api_key")` on load; Forget key and `pagehide` clear it; guarded preference storage (`prefStore` / `prefGet` / `prefSet` / `prefRemove`); stale model preferences are ignored.
- Tests: `tests/test_html_chat_behavior.py` (92) is new, with `tests/fixtures/chat_harness.{js,py}`. The S04 strict xfail became a regression test on the new harness, and the old probe was deleted. Three exporter string pins were updated.
- Mutation-checked: 40 breakages of the shipped script, and every one turned a test red: swallowed handler errors, `{}` for bad tool JSON, non-object input, EOF / open blocks / missing stop reason accepted, a CR ending a line early, unknown events rejected, events before `message_start`, error events ignored, a non-fatal decoder, failed turns committed, stale reads processed, a non-idempotent close, Stop waiting for the promise, no abort on early endings, model change not stopping the turn, the key written to storage, the legacy key re-imported, Forget key and `pagehide` keeping it, unguarded storage reads and access, stale model preferences, citations not attached / not marked / non-https shown, no container, both loop limits off by one, `max_tokens` / refusal / unknown stop / empty answers / unresolved web tools / unsigned thinking committed, no required-field check, no trimming, the question not restored, and the answer not marked. One (unguarded storage access) survived at first and exposed the harness's `vm` accessor problem (see "Decisions and deviations").
- Docs: CLAUDE.md ("HTML report + Ask AI": transaction model, stream contract, key policy; §9 harness bullet; §10 live-run row), README (Ask AI and Testing), handbook ch. 22 (a new section on the transaction, the key policy, and the pins) and ch. 15 (a test-map row), and the exporter's module docstring.
- Review: the Codex bot left one P1 finding, and it was fixed. A response that ended with `end_turn` or a stop sequence but still carried a report-tool call was committed without a `tool_result`, so every later request in that conversation would be rejected (reproduced first; 3 new tests failed before the fix, and a fourth pins the valid case, a web call answered after a report-tool round). The commit check that covered only web tool calls now covers every call in the turn: a report-tool call is answered only after a `tool_use` stop, so one left in a finished or paused response is malformed and discards the turn, instead of being run late or stripped from what the API sent. 5 more mutations (report calls not checked, only the last response checked, tool results not counted as answers, user messages skipped, a web call required to be answered in its own response) were each caught, and the web-tool mutation was retargeted at the new check. CLAUDE.md and handbook ch. 22 now state the check.
- Tests: 3.11 had 4,656 passed, 18 skipped, 22 xfailed. 3.12 with Tk had 4,834 passed, 3 skipped, 25 xfailed. `pip check` was clean, and the JavaScript-required HTML suites passed with `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`.
- Not done, by design: no live run against the real API (CLAUDE.md §10 still lists it), and no headless-browser smoke.
- **Next:** S05.

### 2026-09-24 — S03: Detectors (WP-04)
- **PR:** [#377](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/377)
- Started at master `0a2aef3` (the merge of #376). Both baselines matched S02's final numbers exactly: 3.11 had 4,256 passed, 18 skipped, 44 xfailed; 3.12 with Tk had 4,434 passed, 3 skipped, 47 xfailed. No failures existed on master.
- **WP-04A.** `heading_candidates` returns qualified `HeadingCandidate`s (number, title, level, position, `run_in`, provenance). A heading needs `PART n` or a dotted number and a title-shaped title. A heading's content is its subtree. The structure stops at `END OF SECTION` and at the footnote, endnote, and header/footer blocks. An empty heading is reported only when its parent isn't empty. The duplicate check reads the same candidates.
- **WP-04B.** Stale-citation suppression uses three tables of cues bound to the citation, with a requirement-verb barrier for the in-clause ones, and each window is cut at the neighboring citations.
- **WP-04C.** The ASCE 7 pattern reads ASCE/SEI, SEI/ASCE, `Standard`, seven dash characters, and four-digit editions, normalized by `_asce7_edition_key` with a century check. The California long forms were added to the California module's vocabulary only.
- **WP-04D.** Whole-word bracket keywords, a bare-TBD pattern placed last, and the identifier rule for `TBD-200`. `[OPTIONAL]` stays a placeholder.
- **WP-04E.** Styles are space, dash, and compact, each with or without SECTION. A majority rule decides dominance, a neutral mixture notice is issued otherwise, and names without a section number stay out. The exporters' shared section intro and the preflight log line were adjusted.
- Tests: `tests/test_heading_structure.py` (63) is new. Added `TestCitationRelatedSuppression`, `TestLocationAwareModulesStillSuppressStaleCycleChecks`, and `TestPreflightNamingLog` to `test_preprocessor_policy.py`; `TestDesignationSyntax` to `test_asce7_stale_editions.py`; and the long-form, placeholder, naming, report, and `TestAlertContractUnchanged` classes to `test_deterministic_checks.py`. The 21 S03 strict xfails (8 markers) became regression tests. The fixtures gained `empty_part`.
- Goldens: `preprocessor_alerts.json` and `dc_preprocessor_alerts.json` each changed one line, the naming alert's `context`. Every structural, stale, placeholder, and invalid-year alert in both is byte-identical.
- Mutation-checked: 37 breakages, every one turned a test red. 12 for headings (bare integers back, each title rule removed, flat emptiness, redundant alerts, leaves instead of ancestors, no structure end, run-in ignored, table rows, a flat duplicate reader, PART whitespace), 9 for suppression (`prior`, `historical`, and any negation back as cues; no requirement-verb barrier; no neighbor bounds; `by` as glue; "as previously" counting; "deviate from" as a rejection verb; bare `not`), 5 for syntax (old ASCE pattern, no century check, no Title 24, no "Edition of the", dashes not separators), 6 for placeholders, and 5 for naming (plurality, unknown names voting, a tie picking a winner, compact or SECTION not read).
- Docs: CLAUDE.md ("Stale-cycle suppression window", the §5 table and a contract paragraph after it, the §9 fixture list). Handbook ch. 4 (a currency note, the detector table, a rewritten suppression section, a new "Reading the heading structure" section, the design-tensions paragraph, and the takeaways). Handbook ch. 15's test-map row. README needed no change; its pre-screen list is still accurate.
- Review: the Codex bot left one P2 finding, and it was fixed. A cue shared by a coordinated list ("Previously, the 2019 CBC and 2019 CMC applied") reached only one member, because each window stopped at the neighboring citation (reproduced first). A list is now judged as one citation. The fix exposed an older phantom citation in comma lists ("CBC, 2019"), which was fixed as well (see "Decisions and deviations"). 5 more mutations (no lists, anything joins a list, paragraph breaks join a list, containment instead of overlap, a member judged alone) were each caught.
- Tests: 3.11 had 4,563 passed, 18 skipped, 23 xfailed. 3.12 with Tk had 4,741 passed, 3 skipped, 26 xfailed. `pip check` was clean, and the JavaScript-required HTML suites passed with `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1` (the HTML exporter's naming intro changed).
- **Next:** S04.

### 2026-09-24 — S02: Different findings and ambiguous files (WP-06A, WP-07)
- **PR:** [#376](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/376)
- Started at master `af3fc72` (the merge of #375). Both baselines matched "After S01" exactly: 3.11 had 4,159 passed, 18 skipped, 48 xfailed; 3.12 with Tk had 4,337 passed, 3 skipped, 51 xfailed. No failures existed on master.
- **WP-06A.** `_normalize_issue_text` no longer deletes everything from a CSI-shaped number through the next `.docx`. The new `FindingIdentityContext`, an immutable set of the run's exact file names, removes only those names, as literal whole tokens, longest first. With no corpus, the text is kept. `finding_identity_context_for_submission` derives one context from the submission, and the review dedup and the `cf-` / `lc-` stampers each pass it explicitly. The prefixes are unchanged.
- **WP-07.** `_index_specs` maps each name to every distinct supplied file. `_plan_files` binds every document and checks every destination before any document is opened. New outcomes `FILE_AMBIGUOUS` and `DESTINATION_CONFLICT` hold a whole document with a specific reason, and the receipt lists `candidate_paths`. The CLI exits `3` when anything is held this way. `--assist` never sees a held document, and a dry run plans identically.
- Tests: `tests/test_finding_identity_normalization.py` (52) and `tests/test_applier_bindings.py` (39) are new. The four S02 strict xfails were converted to regression tests; the WP-06A one now also runs with Appendix A's known corpus. `test_the_source_is_never_the_destination` now expects `DESTINATION_CONFLICT` and runs in dry-run mode too.
- Mutation-checked: 9 breakages of the normalization (old rule back, no boundaries, shortest first, no escaping, and each stage losing or mis-sourcing its context) and 16 of the applier (first input wins, repeats read as ambiguous, each destination rule, the sidecar-spelling and path rules, the zero-inode guard, the last-line write guard, exit 3 only under `--strict`, order-dependent candidates, and five for the file-identity checks added after review). Every one turned a test red.
- Docs: CLAUDE.md (flow line, the finding-identity paragraph under "Finding-id namespacing", the applier's binding and destination rules and outcome list), README (dedup line and applier bullets), `applier/README.md` (a new "Which file an instruction goes to" section and exit codes), and handbook ch. 7 (the dedup-key row, the defect story, and the heading, which said a false merge was impossible).
- Review: the Codex bot left one P2 finding, and it was fixed. An existing destination that is a hard link to a supplied spec passed the path comparison, so saving rewrote that spec in place (reproduced first). Destinations are now also compared by file identity, in planning and in the last check before the save.
- Tests: 3.11 had 4,256 passed, 18 skipped, 44 xfailed. 3.12 with Tk had 4,434 passed, 3 skipped, 47 xfailed. `pip check` was clean, and the JavaScript-required HTML suites passed with `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`.
- **Next:** S03.

### 2026-09-24 — S01: Starting point and shared test fixtures (WP-01)
- **PR:** [#375](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/375)
- Re-measured the starting point at master `6b48503`. It matches the reference (see "Starting point").
- Added `tests/fixtures/spec_docx.py`, the shared DOCX builders. `tests/test_spec_docx_fixtures.py` (55 tests) pins their XML, their determinism, and the ground truth each declares.
- Added `tests/test_contract_pins.py` (97 tests): reconstruction, unique ids, the physical meaning of `pN` / `tN` through the applier's own resolver, established text keeping its id, group-vs-occurrence identity, the repair request re-sending the primary's input, and edits aimed inside Word wrappers being exact-or-refused.
- Converted all 25 checks in `plans/check_plan_status.py` into `tests/test_plan_open_defects.py` and deleted the script. The module holds 51 strict xfails, each naming the chunk that fixes it (3 skip without tkinter), and 29 controls. Added `tests/fixtures/chat_key_probe.js` for the chat-key probe.
- Mutation-checked both directions. Simulated fixes (key kept in page memory, one GUI env write removed, Retry-After honored) turned their xfails into `[XPASS(strict)]` failures while the controls kept passing. Deliberate regressions (`pN` from a paragraph counter, raw merged cells in the applier, the repair suffix moved ahead of the spec) were each caught by the pins.
- Updated CLAUDE.md §9, README "Testing", and handbook ch. 15, which said there was no shared DOCX fixture module.
- Review: the Codex bot left two P2 findings, and both were fixed. The auto-numbered fixture now gets only numbering-neutral pins, so S14 may show labels in the extracted text without breaking a pin. An edit applied inside a wrapper must now keep every wrapper's identity and keep the new text inside it. Self-tests prove the check rejects a removed link and text moved out of one.
- Tests: 3.11: 4,159 passed, 18 skipped, 48 xfailed. 3.12 with Tk: 4,337 passed, 3 skipped, 51 xfailed. `pip check` clean. The JavaScript-required HTML suites pass with `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`.
- **Next:** S02.

### 2026-09-23 — Plan revision (before S01)
- **PR:** [#374](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/374)
- Re-checked every package against master `f9da027`; nothing had been implemented, and all defects are still present (plan, Part 3).
- Rewrote the plan as 25 sessions with rules for each session (plan, Parts 1 and 2), and added this tracker and `plans/check_plan_status.py`.
- Corrected the plan's WP-17 item 8. Recorded the starting test result above.
- **Next:** S01.
