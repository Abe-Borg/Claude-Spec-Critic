# Spec Critic plan — progress tracker

This file records what's done and what's next. The instructions live in
[`spec-critic-implementation-plan.md`](spec-critic-implementation-plan.md).

**Coding agents:** update this file in the same pull request as your work (plan, Part 1). Master's
copy of this file is the truth: a chunk counts as done only once the PR that marks it DONE is merged.

---

## ▶ Right now

| | |
|---|---|
| **Next chunk** | **S03 — Detectors** (WP-04) |
| **Last finished** | S02 — Different findings and ambiguous files (WP-06A, WP-07) |
| **Last merged PR** | [#376](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/376) (S02) |
| **Overall** | 2 of 25 chunks done |

**Prompt for the next session** (paste it into a new Claude Code session on this repository):

```text
Continue the Spec Critic implementation plan.

Next chunk: S03 — Detectors (WP-04).

Start from the latest master. Read CLAUDE.md, then plans/PROGRESS.md, then Part 1
and chunk S03 in plans/spec-critic-implementation-plan.md, and its packages in Part 4.
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
| S03 | Fix the false structure alerts and the text checks | WP-04 | TODO | |
| S04 | Make the report chat recover from errors | WP-12 | TODO | |
| S05 | Stop caching "couldn't verify" as an answer | WP-10 | TODO | |
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
- [ ] Heading candidates carry number, title, level, and position. Integer-led prose and quantities ("2 coats…", "12 inches…", "1.5 inches…") are not headings.
- [ ] A heading's content runs through its whole subtree, so a PART with articles is not empty. The clean 3-PART fixture and its table-only variant produce no empty or duplicate alerts; a truly empty article and a truly duplicated heading still alert.
- [ ] Stale-citation suppression uses only citation-related historical or rejection phrases. The three WP-04B examples and the "shall not deviate" / "cannot depart" forms are flagged. Genuine "previous edition" / "superseded" contexts stay suppressed, and sentences with several citations are tested.
- [ ] ASCE/SEI, the optional word "Standard", Unicode dashes, and 2- and 4-digit edition years are recognized. The California long-form references go into the California module's vocabulary only.
- [ ] Bare TBD is detected once, with no double count alongside `[TBD]`. Keyword boundaries hold (EDITION is not EDIT, SELECTED is not SELECT), `TBDF-200` stays clean, and the `TBD-200` policy is written down.
- [ ] File naming: separated, compact, and SECTION-prefixed names are recognized. Unknown names don't hide a mixture, and when no style dominates a neutral mixture notice is issued.
- [ ] Rule IDs, alert order, and alert limits are unchanged, and so are the location-aware modules' policies. Changed goldens are reviewed one by one.

### S04 — Make the report chat recover from errors (WP-12)
- [ ] A Node test harness runs the exact script the exporter ships (extracted the same way the CSP test extracts it) against scripted event streams.
- [ ] API error events, transport and reader failures, malformed required data, and premature EOF all reach the chat's state. A response without a valid terminal event is not treated as complete.
- [ ] Split UTF-8 characters, arbitrary chunk boundaries, LF and CRLF separators, and multi-line data frames reconstruct correctly.
- [ ] Invalid or incomplete tool arguments never become `{}`. Stop reasons, the continuation limit, and the tool-round limit are handled visibly.
- [ ] Committed history never holds a `tool_use` without its `tool_result`, under one documented transaction model. Partial text is shown as interrupted and never replayed as a complete answer.
- [ ] Stop, New Chat, and a model change can't let an old response write into a newer conversation, and the controls are always restored.
- [ ] Citation deltas stay with their text block and survive the next request.
- [ ] The API key lives in page memory only. The legacy `sc_api_key` storage entry is removed and never re-imported, Forget Key clears memory, and a storage failure doesn't break chat.
- [ ] Escaping and CSP-hash tests still pass, and the key policy in CLAUDE.md's "HTML report + Ask AI" section is updated.

### S05 — Stop caching "couldn't verify" as an answer (WP-10)
- [ ] Real time and batch share one verdict-classification contract. A malformed or missing verdict after an ordinary end turn is an operational failure that keeps its known usage, not an UNVERIFIED. Refusal, max tokens, malformed tool input, and unexpected stops are each handled explicitly.
- [ ] One cache-eligibility predicate is used at write, read, and disk load. Only grounded conclusive verdicts that meet the source and quote rules are reused. UNVERIFIED, failed, budget-exhausted, and local results never are. Invalid timestamps and non-finite numbers are rejected record by record.
- [ ] Legacy UNVERIFIED rows are ignored individually and valid conclusive rows still load. There is no blanket flush.
- [ ] Within a run, a well-formed UNVERIFIED is shared once among equivalent in-flight findings; parse failures, local results, and budget failures are not shared. Followers settle correctly when the leader succeeds, is uncertain, fails, or is cancelled, and they add no billed usage.
- [ ] A later run retries an earlier UNVERIFIED under the normal escalation policy.
- [ ] Empty or whitespace-only sources are rejected on both transports and in `classify_status`. A DISPUTED backed only by `""` is not DISPUTED.
- [ ] The CLAUDE.md "Budget-exhaustion sentinel" sentence that says the grounded guard drops every UNVERIFIED is corrected, along with any related cache text.

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
| [ ] | `CLAUDE.md`, "Budget-exhaustion sentinel" (~line 441) | "The `grounded` guard already drops every UNVERIFIED" | A grounded UNVERIFIED is cached and replayed | S05 |
| [ ] | `src/core/tokenizer.py` (~lines 105, 169–170, 433), `src/orchestration/pipeline.py` (~lines 671, 686) | `count_tokens` results are "exact" | They are provider estimates | S06 |
| [ ] | `handbook/04_input.md` (~line 20) | "Still not extracted" list | Content controls, field results, and smart tags are also not extracted; automatic numbering is lost too | S10 (numbering part in S14) |
| [ ] | `handbook/11_trust_model_and_output.md` (~line 28) | "The sidecar no longer under-emits" | True across files only; repeated locations in one file still collapse to one entry | S12 |
| [ ] | `CLAUDE.md`, "Prompt Caching" table (~line 633); `src/core/api_config.py` (~lines 961, 999) | Haiku's cache minimum is 2048 tokens | 4,096 for Haiku 4.5 | S18 |
| [ ] | `handbook/12_configuration_and_models.md` (~line 359), found by S01 | Haiku's cache minimum is 2,048 tokens | 4,096 for Haiku 4.5 | S18 |

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
- **2026-09-24, S02 — case policy.** Two paths are one input when `Path.resolve()` + `os.path.normcase` agree (on Windows this also absorbs case). On a case-insensitive volume that `normcase` doesn't know (macOS), the same path up to case also counts when the filesystem reports one non-zero inode. A zero inode never counts, because some filesystems report 0 for every file. Destination checks compare case-folded paths everywhere, which is the refusing direction.
- **2026-09-24, S02 — found: CLAUDE.md's applier test count was stale.** It said 213, but master had 221. It now says 255 and lists `test_applier_bindings.py`.

---

## Release-note lines (collected for S19)

Each session adds one plain line per user-visible change. S19 moves them into README.md.

- (S02) Findings that differ only in wording no longer merge because both mention a file name. "…requires copper pipe in 210500.docx" and "…requires PVC pipe in 210500.docx" are now two findings, while the same issue reported in several files still groups. Some finding IDs change because the old key was wrong; existing reports and sidecars are untouched.
- (S02) Edit applier: when two different supplied files share a file name, neither is edited (`FILE_AMBIGUOUS`), in either input order. An edited copy that would overwrite any supplied file, such as last run's `*.applied.docx` left in the folder, is refused (`DESTINATION_CONFLICT`). Both are decided before anything is written, the other files are still processed, and the run exits 3.

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

### 2026-09-24 — S02: Different findings and ambiguous files (WP-06A, WP-07)
- **PR:** [#376](https://github.com/Abe-Borg/Claude-Spec-Critic/pull/376)
- Started at master `af3fc72` (the merge of #375). Both baselines matched "After S01" exactly: 3.11 had 4,159 passed, 18 skipped, 48 xfailed; 3.12 with Tk had 4,337 passed, 3 skipped, 51 xfailed. No failures existed on master.
- **WP-06A.** `_normalize_issue_text` no longer deletes everything from a CSI-shaped number through the next `.docx`. The new `FindingIdentityContext`, an immutable set of the run's exact file names, removes only those names, as literal whole tokens, longest first. With no corpus, the text is kept. `finding_identity_context_for_submission` derives one context from the submission, and the review dedup and the `cf-` / `lc-` stampers each pass it explicitly. The prefixes are unchanged.
- **WP-07.** `_index_specs` maps each name to every distinct supplied file. `_plan_files` binds every document and checks every destination before any document is opened. New outcomes `FILE_AMBIGUOUS` and `DESTINATION_CONFLICT` hold a whole document with a specific reason, and the receipt lists `candidate_paths`. The CLI exits `3` when anything is held this way. `--assist` never sees a held document, and a dry run plans identically.
- Tests: `tests/test_finding_identity_normalization.py` (52) and `tests/test_applier_bindings.py` (33) are new. The four S02 strict xfails were converted to regression tests; the WP-06A one now also runs with Appendix A's known corpus. `test_the_source_is_never_the_destination` now expects `DESTINATION_CONFLICT` and runs in dry-run mode too.
- Mutation-checked: 9 breakages of the normalization (old rule back, no boundaries, shortest first, no escaping, and each stage losing or mis-sourcing its context) and 11 of the applier (first input wins, repeats read as ambiguous, each destination rule, the sidecar-spelling and path rules, the zero-inode guard, the last-line write guard, exit 3 only under `--strict`, order-dependent candidates). Every one turned a test red.
- Docs: CLAUDE.md (flow line, the finding-identity paragraph under "Finding-id namespacing", the applier's binding and destination rules and outcome list), README (dedup line and applier bullets), `applier/README.md` (a new "Which file an instruction goes to" section and exit codes), and handbook ch. 7 (the dedup-key row, the defect story, and the heading, which said a false merge was impossible).
- Tests: 3.11 had 4,250 passed, 18 skipped, 44 xfailed. 3.12 with Tk had 4,428 passed, 3 skipped, 47 xfailed. `pip check` was clean, and the JavaScript-required HTML suites passed with `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`.
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
