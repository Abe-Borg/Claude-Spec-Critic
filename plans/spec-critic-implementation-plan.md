# Spec Critic — Implementation Plan

**Prepared:** September 22, 2026  
**Purpose:** An implementation specification for coding agents addressing the updated independent review.  
**Reviewed baseline:** Spec Critic 3.9.0; commit 01781ed5556c8bdf1c3a3594b4e867c0ade2df82; Anthropic dependency pin 1.7.0.  
**Repository at review:** C:/Github-Repos/Claude-Spec-Critic. All source and test paths below are relative to that root.  
**Status:** Planning only. No application changes or evaluations have been performed as part of producing this document.

## 1. Outcomes and scope

Implement the confirmed correctness and reliability fixes before making model, prompt, or architecture changes whose benefits require measurement. The intended outcomes are:

1. Supported Word content reaches review completely, in order, with trustworthy document locations.
2. Deterministic checks recognize ordinary specification structure without generating misleading alerts.
3. Distinct findings and distinct edit locations survive review, grouping, export, and application.
4. Every outbound package request has a model-aware input budget.
5. Reports distinguish completed analysis, incomplete coverage, operational failure, and legitimate uncertainty.
6. Outstanding paid work retains a usable recovery record, and every known paid attempt is accounted for.
7. The embedded chat recovers from stream/tool failures without corrupting subsequent conversations.
8. Verification evidence is more inspectable without pretending that retrieval or text overlap proves a claim.
9. Performance changes are promoted only after reproducible quality and cost comparisons.

### Required work versus gated work

- **WP-01 through WP-17 are required implementation packages.** Their acceptance criteria define the correctness release. WP-03 includes bounded support for common Word automatic numbering; unsupported formats must remain visible as limitations.
- **EX-01 through EX-06 are evaluation or staged capability packages.** Complete their investigation and record an evidence-backed decision. A default change is not required when the experiment fails, evidence is insufficient, or an authorized evaluation budget is unavailable.
- Do not label a gated experiment implemented merely because a prompt or flag exists. Separate implementation, offline validation, live evaluation, and default enablement.
- Do not reopen already-correct behavior just to match an obsolete report statement. In particular, preserve the coverage-first confidence rubric, the standard-edition/adoption qualifications, forced default Haiku triage, existing continuation caching, and real-time per-attempt telemetry.

The implementation agents should inspect the then-current repository before editing. This plan identifies behavior and contracts, not immutable line numbers. If a defect has been fixed since the baseline, demonstrate that with the acceptance tests and close the corresponding item without rewriting it.

## 2. Execution rules and engineering invariants

Read the repository's current guidance, including CLAUDE.md, applicable AGENTS.md files, and CI configuration. Preserve unrelated user changes. Use isolated branches/worktrees when multiple agents would otherwise edit the same files; follow the repository's branch and PR conventions.

### Invariants that must survive every package

- The main application emits edit instructions. The separate applier remains separate; nothing under src/ imports applier/.
- The input specification is never overwritten by the applier.
- Failed verification, inconclusive verification, local classification, and grounded conclusive verification remain distinct.
- A valid zero-findings review is different from a failed or unparseable review.
- Partial failures never erase completed findings.
- Every executable instruction retains the actual source file, target location, and original locator text.
- Report grouping must not destroy executable occurrences.
- Source retrieval, native attribution, and semantic support are separate concepts.
- Governing editions derive from applicable adoption/project authority, not merely the newest publication or a module default.
- No duplicate retry ownership: app-owned loops disable SDK retries; single-shot SDK-owned paths retain them.
- No duplicate billing from cached/shared results or aggregate-plus-per-attempt telemetry.
- Persisted state never contains API keys or full specification bodies. Do not casually expand persisted content as part of recovery work.
- GUI widget changes remain marshaled through the existing dispatch mechanism.
- The final HTML stays self-contained, escapes untrusted content, and has a CSP hash matching its exact executable script.
- Golden changes must be reviewed for meaning. Bulk regeneration is not proof of correctness.

### Validation policy

Write regression tests for confirmed bugs and changed contracts, not tests that merely mirror an implementation. Prefer complete, adversarial examples and boundary transitions.

Tests should use the existing fake Anthropic fixtures, injected clocks/random sources, and temporary fixture outputs. No normal test may call a paid API. Explicitly select non-network tests even if the developer's environment contains a real key.

Live comparisons require a bounded evaluation run with a dataset, maximum spend, stopping rule, and results artifact. CLAUDE.md already treats applicability evaluation and cache adoption as measurement-gated. This plan does not invent an unlimited API-spend authorization.

## 3. Traceability from the review

| Review issue or adjustment | Owning package |
|---|---|
| P1-1: false empty headings; related duplicate-heading false positives | WP-04, WP-01 |
| P1-2: issue normalization merges different findings | WP-06A |
| P1-3: content controls, simple fields, smart tags | WP-02 |
| Automatic-numbering information loss noted under P1-1 | WP-03 |
| P1-4 / API-4: unsafe package token gates | WP-08 |
| P1-5: swallowed stream errors and unmatched tool calls | WP-12 |
| P2-1: persistent UNVERIFIED cache entries | WP-10 |
| P2-2 / API-8: Retry-After and synchronized retries | WP-11 |
| P2-3: SECTION-heading and compact-name routing | WP-05 |
| P2-4/5/6/7: suppression, ASCE, TBD, naming checks | WP-04 |
| P2-8: trace startup strands the GUI | WP-13 |
| P3: Haiku minimum, request-count terminology, standards wording | WP-17 |
| P3: extended-output threshold | WP-08 |
| P3: search-first fetch restriction | WP-16, WP-17 |
| P3: summarized thinking for deep traces | WP-13 |
| P3: placeholder prefixes and long-form editions | WP-04 |
| P3: blank sources and missing no-op demotion reason | WP-10, WP-17 |
| P3: chat citation replay and session key persistence | WP-12 |
| P3: process environment API-key exposure | WP-13 |
| API-1: shared project-context caching | EX-01 |
| API-2: native citation capture and calibrated validation | WP-16, EX-04 |
| API-3/7: escalation model and review effort | EX-03 |
| API-5: schema-constrained final output | EX-02 |
| API-6: coverage-first prompt already substantially addressed | WP-17, EX-03 |
| ARCH-1: cross-chunk and cross-module coordination | WP-17, EX-06 |
| ARCH-2: collector divergence | WP-14 |
| ARCH-3: shared citation resolution | EX-04 |
| ARCH-4: research reuse | EX-05 |
| ARCH-5: realistic clean and mutated fixtures | WP-01 |
| New: multiple locations in one file collapse | WP-06B |
| New: applier chooses the first same-named input | WP-07 |
| New: absent compliance coverage can look complete | WP-09 |
| New: malformed real-time verdict is treated as cacheable uncertainty | WP-10 |
| New: pending repair loses its automatic recovery record | WP-14 |
| New: repair replaces the original attempt's cost | WP-15 |

## 4. Dependencies and parallel work

Start with a short contract review, not a broad framework rewrite. Agree the document-location contract, occurrence/sidecar contract, coverage-completeness metadata, collection outcome contract, and attempt-usage contract before agents edit shared models.

| Track | Work | Dependencies and integration boundaries |
|---|---|---|
| Input | WP-02, WP-03, WP-04, WP-05 | WP-01 starts fixtures. Numbering follows traversal/source-mapping decisions. Coordinate extraction IDs with applier and occurrence work. |
| Finding and edit identity | WP-06, WP-07 | WP-07 can land first. New sidecar reader must land before or with the new writer. |
| Request sizing and coverage | WP-08, WP-09 | Share result metadata with recovery/report owners. Coverage must preserve partial chunk findings. |
| Verification | WP-10, WP-11, WP-16 | Parse/cache/sharing changes land together. Citation enforcement remains separate from capture. |
| UI and chat | WP-12, WP-13 | HTML exporter overlaps citation/report work; assign one integrator or serialize edits. |
| Recovery and accounting | WP-14, WP-15 | Agree repair lifecycle and attempt identities first. Both affect pipeline.py and diagnostics. |
| Integration | WP-17 | Follow all required packages; review changed goldens and documentation together. |
| Experiments | EX-01 through EX-06 | Start measurements after WP-15 makes repair costs trustworthy and relevant correctness packages pass. |

The largest merge-conflict surfaces are pipeline.py, verifier.py, reviewer.py, api_config.py, both exporters, and CLAUDE.md. Do not give independent agents unrestricted concurrent ownership of these files. Each agent should return its contract changes, tests, compatibility consequences, and unresolved assumptions to the integrator.

Small fixes need not wait for the largest migration. Good early releases include the filename-normalization defect, applier filename rejection, heading correction, chat error propagation, trace failure handling, and malformed-verdict/cache policy. Keep each change reviewable; do not hide schema or recovery redesign inside a regex-fix PR.

## 5. WP-01 — Baseline, fixtures, and contract tests

**Purpose:** Make the expected behavior independent of model responses and prevent newly supported input structures from breaking edit application.

**Primary targets:** tests/fixtures/, tests/test_deterministic_checks.py, tests/test_preprocessor_policy.py, extraction tests, routing tests, tests/test_edit_sidecar.py, applier tests, and tests/fixtures/fake_anthropic.py.

### Work

1. Record the actual starting commit and existing non-network test result. Identify any pre-existing failures or skips without attributing them to new work.
2. Create a small, anonymized specification fixture family, using deterministic DOCX builders where clearer than committed binaries.
3. Include manual CSI numbering, automatic numbering, rich/dropdown controls, simple fields, hyperlinks, tracked insertions/deletions, table-only article bodies, merged/nested tables, and representative compact filenames.
4. Keep clean fixtures and single-defect mutations separate. A clean zero-alert document cannot prove that TBD or an alternate stale citation is detected.
5. Pin public contracts: extraction reconstruction, unique IDs, old-ID meaning, safe unsupported-element handling, group versus occurrence identity, coverage completeness, and collection cleanup eligibility.
6. Turn the offline reproductions from the review into maintained behavioral tests. Do not depend on the original review session's scratch files.
7. Ensure generated input/output fixtures live in temporary test directories; never alter real project documents.

### Acceptance

- Clean documents produce zero alerts for the categories they are intended to exercise.
- Each mutated document produces its expected alert/warning/failure and does not produce unrelated alerts.
- Every new XML container is tested through extraction and through the locator/editor boundary.
- The same extracted input reaches ordinary and repair request construction consistently.
- No network connection or production credential is needed.

**Suggested new focused suites:** test_extraction_content_controls.py, test_extraction_numbering.py, test_heading_structure.py, and a compact input-to-sidecar integration suite. Use existing suites when they already express the behavior cleanly.

## 6. WP-02 — Restore supported Word content without corrupting locations

**Priority:** P1.  
**Targets:** src/input/extractor.py; src/review/prompt_serialization.py; applier/locator.py; applier/docx_edit.py; extraction, prompt-serialization, and applier tests.

### Required behavior

Support block and inline content controls, smart-tag containers, and stored visible results of simple fields. Traverse body paragraphs, cells, nested tables, and currently supported supplemental surfaces without losing or duplicating text.

### Implementation requirements

1. Add explicit traversal for w:sdt/w:sdtContent, w:smartTag, and w:fldSimple where appropriate. Use structured traversal, not a catch-all descendant-text search.
2. Apply Accept-All revision semantics at every supported depth: include insertions and move-to content; exclude deletions and move-from content. Audit hyperlinks containing revisions too.
3. Read stored field results only. Never execute fields, fetch external field content, or treat field instructions as visible prose.
4. Preserve source order and emit each visible fragment once. Nested tables and independently collected text boxes must not be double-counted.
5. Warn on unsupported text-bearing structures that remain omitted. Distinguish known unsupported content from a proven count of lost characters; do not fabricate completeness metrics.
6. Preserve legacy element-ID meaning. Existing pN IDs address physical body children; ordinary tN IDs correspond to the existing table indexing semantics. Wrapped tables must not silently renumber ordinary tables.
7. Assign newly supported wrapped blocks a distinct stable structural path namespace. Either implement matching deterministic resolution in the same release or explicitly classify the new surface as unsupported for automatic application.
8. Treat reviewability and editability separately. The writer currently handles direct runs. An inline field/control becoming readable does not authorize editing through it. Refuse an edit intersecting an unsupported wrapper, even if similar text elsewhere would match.
9. Keep ExtractedSpec content and paragraph-map reconstruction consistent, preserving source_path and unique element identity.

### Acceptance cases

- A block control containing a paragraph and table yields both in the correct order.
- Inline dropdown text survives between surrounding runs; an unresolved stored dropdown placeholder reaches preprocessing.
- The stored REF result “23 05 00” and smart-tag text survive.
- Nested controls and controls in table cells honor accepted/deleted revisions.
- Adding a wrapped table before an ordinary table does not make the ordinary table's existing address point elsewhere.
- Each new ID resolves to the intended physical container or produces an explicit unsupported result.
- Identical text inside and outside a control never causes an edit to target the wrong occurrence.
- Existing supported documents retain their established text and legacy locations.

**Release boundary:** Do not claim universal Word support. Document exactly which wrappers and surfaces are handled and which remain readable-only or unsupported.

## 7. WP-03 — Preserve automatic numbering as display metadata with source mapping

**Priority:** P2; necessary to restore article identifiers and reliable structural review on common Word templates.  
**Targets:** src/input/extractor.py and an optional focused numbering helper; ParagraphMapping; prompt serialization; section attribution; applier locator/editor boundaries.

### Design decision

Represent the displayed number separately from literal editable run text. A synthesized “1.01” is not a substring in w:t and must never be treated as an ordinary replaceable span.

### Work

1. Resolve common Word numbering via numPr, numId, abstractNum, level text, starts, overrides, restarts, and style-inherited numbering.
2. Keep numbering state document-local and scoped to the correct list instance. Do not share counters across files or extraction workers.
3. Supply displayed labels to review, section attribution, and structural detection. Also define how plain context-DOCX extraction exposes labels.
4. Preserve both literal source text and any synthetic display spans. If content becomes the displayed view, update reconstruction deliberately and retain a separate source-text contract for editing.
5. Keep the actual XML structural locator independent of the display label.
6. Do not duplicate manually typed numbering.
7. Preserve body text and issue a clear warning for unsupported numbering formats or ambiguous resolution. Do not guess counters.
8. Refuse automatic edits to synthetic labels. Translate an edit involving only actual body text only when the source mapping proves the offsets; otherwise report it for manual action.

### Acceptance

- An automatically numbered article is shown as “1.01 SUMMARY” and attributed to section 1.01.
- Multilevel lists, overrides, restarts, and independent list instances remain distinct.
- Typed labels are not repeated.
- Revision handling never resurrects deleted body content.
- An edit containing a synthetic prefix cannot delete unrelated source characters.
- An edit to genuine body text following the label still locates correctly.
- Unsupported numbering produces an honest warning, not silent loss.

**Tests:** Add numbering-specific fixtures and an extraction → prompt → finding → applier boundary test. Do not extend the XML writer to edit numbering definitions as part of this package.

## 8. WP-04 — Correct deterministic structure and text detectors

**Priority:** P1 for heading false positives; P2/P3 for the narrower checks.  
**Targets:** src/input/preprocessor.py, module-owned detector vocabulary, and relevant deterministic/golden tests.

### A. Heading hierarchy

- Replace the flat heading interpretation with qualified candidates carrying normalized number, title, level, source position, and available style/numbering provenance.
- Exclude ordinary integer-led prose, including “2 coats of primer,” “12 inches minimum,” and “1 year from Substantial Completion.”
- Do not merely accept every dotted number: “1.5 inches minimum” is also prose. Use structural shape and metadata conservatively.
- A heading's content extends through its subtree to the next sibling/ancestor or EOF. A PART with populated articles is not empty.
- Report a truly empty leaf article. Report an empty PART when it has no substantive descendant content. Define a nonredundant ancestor/leaf alert policy.
- Reuse qualified candidates for duplicate-heading checks; fixing empty detection alone must not leave quantity paragraphs classified as duplicate headings.
- Preserve rule IDs, original text, positions, deterministic ordering, and alert limits.

### B. Stale-citation suppression

Replace unrelated nearby keywords with citation-related historical/rejection phrases. Preserve genuine “previous edition” and “superseded citation” contexts while flagging active requirements in:

- “Submit shop drawings prior to fabrication in accordance with 2022 CBC Section 1704.”
- “Coordinate with the historical society and comply with 2022 CBC.”
- “Contractor may not deviate from 2022 CBC Chapter 17.”
- Equivalent “shall not deviate” and “cannot depart” formulations.

Retain clause boundaries and test multiple citations in one sentence. Preserve modules that intentionally suppress stale-cycle checks; do not turn a syntax improvement into a new governing-edition policy.

### C. Citation syntax

Recognize ASCE/SEI, the optional word Standard, ordinary Unicode dash variants, and two-/four-digit edition years. Normalize editions before comparison, preserving plausibility checks and century handling.

Add the demonstrated long-form California references through the relevant module vocabulary: “2019 California Building Standards Code,” “2022 Edition of the CBC,” “CBC (2022 edition),” and jurisdiction-qualified “Title 24, 2022.” Do not equate generic Title 24 with CBC or activate California assumptions in other modules.

### D. Placeholders

- Detect standalone TBD and deduplicate overlap with bracketed TBD.
- Require keyword boundaries so EDITION does not become EDIT and SELECTED does not become SELECT.
- Keep product identifiers such as TBDF-200 clean. Define the policy for TBD-200 explicitly.
- Decide whether a complete bracketed OPTIONAL marker is an editorial choice in the supported templates; do not automatically classify every such marker as a false positive.
- Preserve existing legitimate marker detection and avoid treating new text extraction as proof that every bracketed phrase is defective.

### E. Filename consistency

Recognize separated and compact six-digit names, optional SECTION prefixes, and extension case variants. Unknown names must not suppress an observed mixture among recognized styles.

When there is no dominant style, report a neutral mixture rather than inventing a winning convention. Keep this informational naming notice separate from coverage/routing defects.

### Acceptance

The clean three-PART fixture in Appendix A produces no empty alerts. True empty/duplicate articles still alert. Every syntax expansion has positive and negative tests. The original suppression examples flag, genuine historical references remain suppressed, and location-aware module policies remain unchanged.

**Tests:** test_deterministic_checks.py, test_preprocessor_policy.py, test_asce7_stale_editions.py, test_keyword_word_boundaries.py, test_preprocessor_synthetic_paragraphs.py, and the affected golden-domain suites.

## 9. WP-05 — Route from credible document metadata and preserve source identity

**Priority:** P2.  
**Targets:** src/programs/assignments.py, src/programs/routing.py, routing evidence models, extractor metadata, and headless preparation entry points.

### Work

1. Extract an actual SECTION heading from a bounded opening body region. Require heading-shaped text; “See Section 21 13 13” in a related-sections paragraph is not the document's identity.
2. Carry section number and section title separately, with provenance. Avoid extracting identity repeatedly with different rules in assignment and routing code.
3. Support a compact leading filename number when corroborated by the body heading. Preserve guards against dates, project identifiers, NFPA references, and arbitrary embedded numbers.
4. Surface contradictory strong filename/body evidence as ambiguity. Do not silently prefer whichever regex runs first.
5. Preserve intended unsupported-division behavior and legacy fire-alarm routing corroboration.
6. Use the ExtractedSpec's trustworthy source path or an unambiguous input mapping. Reject distinct inputs with colliding basenames at non-GUI boundaries before submission; the GUI-only guard is not a universal invariant.
7. Retain assignment provenance and behavior across saved-state serialization and relevant resume paths.

### Acceptance

- 210500.docx + SECTION 21 05 00 routes to fire suppression.
- 211313.docx + the corresponding wet-pipe heading is supported.
- A compact filename without credible corroboration remains conservative.
- Related-section references do not override the real heading.
- Contradictory headings/names produce an explicit ambiguous result.
- Unsupported Division 27/28 scopes remain unsupported as intended.
- Reversed input order cannot silently select a different same-named source.

**Tests:** test_program_routing.py, test_datacenter_routing.py, test_domain_routing_pins.py, test_program_pipeline.py, and test_file_name_collision_guard.py. Cover the headless boundary, not just the GUI selector.

## 10. WP-06 — Preserve semantic findings and executable occurrences

### WP-06A: Remove unsafe filename normalization

**Priority:** P1.  
**Targets:** pipeline.py functions _normalize_issue_text, _dedup_key, compute_finding_id, and _deduplicate_findings.

1. Delete the generic pattern that strips a CSI number through the next .docx.
2. Normalize only exact known corpus filenames with literal escaping and clear boundaries. Prefer retaining an unknown filename to deleting potentially meaningful prose.
3. Apply one normalization context consistently across review, cross-check, compliance, and stable finding-ID generation. Avoid global mutable corpus state.
4. When no corpus context is available, preserve the text rather than guessing a filename.
5. Preserve case/whitespace normalization that does not change meaning. Keep rf-/cf-/lc- namespaces.
6. Document that newly generated IDs may change when the old key was wrong; do not rewrite existing exported reports.

**Acceptance:** The copper/PVC examples in Appendix A remain distinct. Otherwise identical issues mentioning different known source filenames can still group. Overlapping filenames, spaces, punctuation, uppercase extensions, unknown filenames, and reordered input do not corrupt identity.

### WP-06B: Separate display groups from executable locations

**Priority:** P1.  
**Targets:** Finding, FindingOccurrence, group_findings, per-original lookup, edit_sidecar.py, both exporters, and applier models/reader/receipt/policy.

1. A display group may have several locations in one file and several files. Replace “filename → first original” with location-preserving occurrences.
2. Prefer validated element identity; combine it with instruction identity where different actions or insertion sides target one anchor.
3. Collapse genuine duplicate emissions at the same target. Preserve distinct validated targets.
4. Without a resolvable element ID, preserve uncertainty. Identical prose does not prove identical location. Never manufacture two actionable locations from indistinguishable anchors.
5. Create stable occurrence IDs independent of input order and presentation counters such as grp-0000. Include module identity for program entries.
6. Keep representative verification/display fields separate from each occurrence's original executable fields.
7. Preserve explicit missing-original status; do not silently borrow another location's anchor.
8. Audit all consumers of finding_id and EditEntry.key, including receipts, filters, chat links, and conflict detection.
9. Detect incompatible instructions targeting the same region. Hold/report the conflict; do not let application order choose a winner.
10. Keep resolve-before-mutate behavior so earlier insertions cannot shift later targets.

### Sidecar migration

The current single-module schema is 4 and the program schema is 5. A naive increment would reuse an existing meaning.

Reserve **6 for single-module occurrence-aware output and 7 for program occurrence-aware output**, after verifying these are still unused. New entries must carry occurrence_id and a documented unique-entry contract; program identity must include module provenance.

Upgrade the applier to read 4/5/6/7 before or with the writer. Preserve legacy 4/5 interpretation without pretending missing location information was recovered. LoadedSidecar.is_program must handle both program versions. Old readers should refuse new schemas cleanly.

Update sidecar documentation, receipts, compatibility tests, and release notes together. This package is an actual contract migration.

### Acceptance

- Same issue at p4 and p8 produces two sidecar entries and two correct tracked changes.
- Duplicate emission for p4 produces one instruction.
- Two files with two locations each produce four instructions with their own anchors.
- Cross-module entries do not collide.
- Conflicting edits are visible and withheld appropriately.
- Legacy sidecars still behave identically.
- New report → sidecar → applier → receipt preserves every executable occurrence.

**Tests:** test_dedup_edit_identity.py, test_cross_check_finding_ids.py, test_edit_sidecar.py, test_applier_sidecar.py, test_applier_run.py, test_applier_docx_edit.py, and test_applier_isolation.py.

## 11. WP-07 — Refuse ambiguous applier file bindings and destinations

**Priority:** P1. This safety fix can land independently of the schema migration.  
**Targets:** applier/run.py, applier/cli.py, applier/models.py, applier/receipt.py.

### Work

1. Map a normalized basename to all distinct resolved supplied paths, not the first path.
2. Repeating the same actual input is harmless; different same-named inputs are ambiguous.
3. Resolve every file binding and destination before writing. Hold affected instructions with a specific ambiguity reason and account for them in the receipt.
4. Check output collisions, source/destination aliasing, and destinations that would overwrite another supplied source—not just the current source.
5. Do not let model assistance select among ambiguous files.
6. If a later explicit mapping is introduced, restrict it to supplied inputs. A path embedded in a sidecar is not authority to read or overwrite arbitrary files.
7. Keep uniquely bound files actionable when other files are held, with an appropriately non-success exit/report outcome.
8. Ensure dry-run and real-run decision logic match.

### Acceptance

Two project folders containing spec.docx remain ambiguous in either input order. Repeating one resolved file does not create false ambiguity. Case behavior matches the supported filesystem policy. Colliding destinations are rejected before writes. Every held instruction appears in the receipt. No source file is overwritten.

**Tests:** test_applier_run.py, test_applier_sidecar.py, relevant CLI/receipt tests, and a multi-file output-dir scenario.

## 12. WP-08 — Use canonical, model-aware request budgets

**Priority:** P1.  
**Targets:** core/tokenizer.py, core/api_config.py, cross_check/cross_checker.py, compliance/compliance_checker.py, core/chunked_pass.py, review/review_request_builder.py, and review preparation/preflight paths.

### Budget contract

Build each actual request once and derive its counting form from the same inputs. Include system, user content, tools, project context, prior findings, chunk notes, and supported request features that affect input counting.

The API count is a model-aware estimate, not a mathematical guarantee. Keep a documented reserve. The effective input ceiling must not exceed the selected model's context window minus its actual requested output cap and the safety reserve. Preserve existing practical phase limits where more conservative.

Never treat a raw cl100k count or a safety multiplier as measured Anthropic usage.

### Work

1. Introduce a small common request-budget result: count, count source, selected model, input ceiling, requested output reserve, fit decision, and unavailability reason when applicable.
2. Reuse the existing preflight toggle and API-count helper, correcting misleading “exact” terminology. Validate malformed/absent count responses; they must not become a trustworthy zero.
3. When preflight is unavailable or explicitly disabled, use model-specific conservative padding on all locally counted request components, including tool overhead.
4. Decide single-call versus chunked execution using that result. Apply the same policy to every final chunk.
5. Avoid duplicate preflights for identical shapes within a run. Any count cache must include the model and every shape-changing input; never reuse a count across model overrides.
6. Extend grouping with bounded token-aware subdivision when one CSI bucket is still too large. Preserve order, source ownership, and stable chunk identities. A large single bucket must not be assumed safe merely because grouping occurred.
7. If a single document plus required context cannot fit, report an explicit unanalyzed portion or skipped request. Do not silently truncate document text, requirements, or prior-finding context.
8. Preserve completed chunks if another cannot run. Surface reduced cross-chunk coordination and exact failed/skipped coverage.
9. Honor per-network-call concurrency gates during preflight where needed; avoid holding a permit across an entire multi-chunk operation.
10. Use the same count source for the batch extended-output threshold. Resolve input shape/count before output-cap selection to avoid a circular builder. Recheck the final context fit after choosing the cap. Real-time review must remain on its supported non-extended path.

### Acceptance

Use stubbed counts rather than giant synthetic requests:

- Local count under 822k, API count over the safe ceiling → chunking, no oversized message submission.
- Preflight unavailable → padded estimate blocks the same unsafe case.
- Fitting API count → one request with the counted shape.
- Model override to a smaller window → smaller safe ceiling.
- Tool/context overhead changes → a different decision when appropriate.
- One oversized CSI group → deterministic subdivision; an unsplittable item is explicitly surfaced.
- Mixed successful/failed chunks preserve all completed findings.
- Batch extended-output threshold uses the selected count basis; streaming never receives the batch-only beta.
- Invalid count responses never authorize an oversized call.

**Tests:** test_token_budgets.py, test_cross_check_chunking.py, test_compliance_pass.py, test_chunked_pass_engine.py, test_token_analysis_gate_and_threading.py, test_prompt_serialization.py, test_realtime_review.py, and client-factory/retry ownership tests.

**Numerical note:** Under the existing 822,000 input budget and 1.45 fallback multiplier, the corresponding local count is about 566,897, not 690,000. Do not hard-code that conversion as a universal limit; derive it from the model/request policy.

## 13. WP-09 — Make compliance completeness explicit

**Priority:** P1 because an incomplete analysis can currently appear successful.  
**Targets:** src/compliance/compliance_checker.py; src/core/chunked_pass.py; review result models; both exporters; diagnostics; program aggregation.

### Required contract

A successful API response is not evidence that every controlling requirement was assessed. Track execution status and coverage completeness separately.

1. Define the expected coverage set from grounded, controlling, non-process requirements. Unverified research items remain advisory and must not silently become mandatory coverage rows.
2. Reconcile prompt instructions and examples with that set. Remove contradictory instructions about covering every profile item versus only controlling items.
3. Normalize returned coverage against known IDs, then compute omitted expected IDs. Distinguish an explicitly returned “unclear” assessment from a row the model never returned.
4. Represent an omitted row as synthetic “unclear/not assessed,” with a reason and an origin marker. Never infer “represented,” “missing from the specification,” or a successfully completed assessment from omission.
5. Add metadata for expected count, returned expected count, omitted IDs/count, and completeness. Document zero-expected-item behavior.
6. Preserve usable findings and returned rows. Do not discard an otherwise useful response merely because some rows are absent.
7. Propagate completeness through chunk synthesis, program aggregation, JSON/profile output where applicable, DOCX, HTML, and diagnostics. A prominent partial-analysis notice must survive every output path.

### Chunk semantics

Inspect every consumer before introducing new status values. At the baseline, chunk synthesis keeps findings only for results whose status is “completed.” Adding a “partial” status in one producer could erase valid findings downstream.

Prefer additive completeness metadata while retaining existing execution status semantics, unless the entire status contract is migrated together. A completed request with omitted coverage can have incomplete assessment metadata; an operationally failed request stays failed.

A conclusion that a requirement is absent across a scope requires every relevant chunk to have been assessed sufficiently to support that conclusion. A failed, skipped, or omitted chunk cannot establish global absence. Preserve useful supported findings, but suppress executable additions that depend on unassessed absence; retain them visibly as conditional/report-only findings with the reason.

Review the existing coverage merge precedence and ADD filtering together. Do not let synthetic rows turn unknown coverage into a proved deficiency. Define how contradictory and represented evidence in different chunks is summarized without discarding their locations.

### Acceptance

- Empty coverage with a nonempty controlling set produces an incomplete report.
- One omitted ID remains distinguishable from one explicitly returned unclear row.
- Unknown IDs, duplicate rows, malformed rows, and reordered rows are handled deterministically.
- A zero-controlling-item profile produces a valid no-applicable-items result.
- Failed/skipped chunks preserve completed findings and prevent unsupported global-absence edits.
- Single-file, package, program, GUI, CLI, and recovered reports display the same completeness semantics.

**Tests:** test_compliance_pass.py, test_chunked_pass_engine.py, test_report_status.py, test_program_pipeline.py, and exporter tests.

## 14. WP-10 — Separate failed verification, uncertainty, and reusable verdicts

**Priority:** P1.  
**Targets:** src/verification/verifier.py; src/verification/verification_cache.py; pipeline verification sharing and diagnostics.

### Parsing and failure classification

1. Audit real-time and batch verdict construction against one classification contract.
2. An ordinary end-of-turn response with malformed or missing verdict content is an operational/parsing failure, not a grounded UNVERIFIED result.
3. Preserve attempt usage, error details, and any safely captured evidence on failures. Failure must not become zero-cost merely because no verdict parsed.
4. A legitimate, well-formed UNVERIFIED verdict remains genuine uncertainty. Do not force it to be “ungrounded” just to bypass the cache.
5. Handle refusal, output exhaustion, malformed tool input, missing expected tool output, and unexpected stop reasons explicitly.
6. Do not add an unbounded repair/escalation loop. Use the existing bounded policy, with a clear terminal result.

### Persistent cache eligibility

Use one predicate consistently at write, read, and disk-load boundaries.

- Reuse only eligible grounded conclusive verdicts, such as properly evidenced CONFIRMED or DISPUTED results that satisfy existing source/quote requirements.
- Do not persist or reuse UNVERIFIED, malformed, failed, budget-exhausted, or local-only results as conclusive evidence.
- Ignore ineligible old entries individually. Do not flush unrelated valid cache entries.
- Preserve schema compatibility when a policy-only change suffices. If the serialized evidence contract changes, document migration and versioning.
- Continue validating expiry and standards fingerprints. Treat invalid timestamps or nonfinite numeric data as invalid records, not cache hits.
- A later independent run must be able to retry an earlier inconclusive finding under the normal escalation policy.

### Same-run sharing must remain functional

The current sharing helper assumes grounded results are handled by the persistent cache. Removing UNVERIFIED from that cache requires a coordinated single-flight change.

Allow an eligible, well-formed UNVERIFIED result to be shared among equivalent in-flight findings within the same run, without making it a persistent success. Explicitly exclude parsing failures, local classifications, and budget failures from this uncertainty-sharing rule. Ensure cancellation or leader failure releases waiting followers.

Followers must not duplicate billed usage. Preserve evidence and outcome, but clear or otherwise exclude attempt usage and chargeable counters through the existing shared-result accounting contract.

### Nonblank source validation

Reject citation arrays whose entries contain only whitespace or empty fields. Apply the same minimum substantive-source rule across batch and real-time paths. Preserve existing authority and quote-validation rules; source presence alone never proves support.

### Acceptance

- The same malformed response fails in batch and real-time modes.
- Failed parses retain known token usage and never become durable cache hits.
- A valid UNVERIFIED result is shared once within a run but retried in a later run.
- Existing valid conclusive cache entries still load; legacy UNVERIFIED entries are ignored.
- Whitespace-only citations fail evidence validation.
- Concurrent followers settle correctly after success, uncertainty, failure, and cancellation.
- Cost diagnostics bill only the actual leader attempts.

**Tests:** verifier parsing suites; verification cache serialization/source-quote/LRU tests; pipeline sharing and concurrency tests; diagnostic cost tests.

## 15. WP-11 — Respect rate-limit timing and release concurrency during backoff

**Priority:** P2.  
**Targets:** src/verification/retry_policy.py; application-owned API loops; research, verification, batch retrieval, and token-count call gates.

### Retry contract

1. Normalize applicable server delay headers, including numeric Retry-After seconds, HTTP-date Retry-After, and supported millisecond delay headers.
2. Validate values. Missing, malformed, negative, nonfinite, or expired values fall back to the local retry policy.
3. Use bounded exponential backoff with injected randomness when no valid server floor exists. Add jitter without retrying before a valid server-requested delay.
4. Bound both attempt count and elapsed retry budget. If a server delay exceeds the remaining budget, stop or defer with a clear outcome; do not shorten the delay and immediately retry.
5. Preserve documented meaning of existing retry-count settings, including zero. Do not inadvertently change “retries” into “total attempts.”
6. Keep retryable classifications explicit. Authentication and invalid-request errors should not be repeatedly retried as transient failures.
7. Keep batch per-item error waves distinct from transport requests with HTTP headers. Apply the appropriate policy to each layer without inventing missing headers.

### Concurrency and ownership

Acquire request concurrency at the actual outbound call boundary, including continuations and escalation calls. Release it before sleeping. Do not wrap an entire research dimension or finding-verification lifecycle in a network semaphore and then sleep while holding a slot.

Avoid nested acquisition of the same gate. Keep any deliberate server-limited pool semantics separate from local network concurrency.

For each outbound path, document its retry owner. App-owned retry loops must use a client with SDK retries disabled. Paths intentionally relying on SDK retries should not acquire a second app retry loop. Include batch-result retrieval and token-count calls in this audit.

### Acceptance

Use injected clocks/sleep/random functions; tests must not wait in real time.

- Numeric, HTTP-date, millisecond, malformed, and absent headers behave as specified.
- Concurrent calls receive different local backoff delays.
- No retry precedes a valid server floor.
- Another request can use a slot while the first call backs off.
- Cancellation interrupts retry waiting promptly.
- Attempt and elapsed limits both terminate correctly.
- One simulated transient failure produces the expected number of actual API calls, without hidden double retries.

**Tests:** test_retry_policy.py, test_client_retry_policy.py, test_batch_results_retry.py, test_research_concurrency.py, test_collection_call_gate.py, and token-analysis gate tests.

## 16. WP-12 — Make embedded chat streaming transactional and recoverable

**Priority:** P1.  
**Targets:** src/output/html_report_exporter.py and the exact embedded JavaScript it emits; HTML JavaScript/exporter tests.

### Stream parser and error boundaries

1. Narrow catch blocks to the operation they can recover from. A JSON decoding guard must not swallow an API error thrown by event handling.
2. Surface API error events, transport failures, reader failures, malformed required data, and premature EOF to the chat state machine.
3. Process split UTF-8 characters, arbitrary byte boundaries, LF/CRLF event separators, multiple data lines where supported, and the decoder's final buffered data.
4. Require a valid terminal stream state before treating a response as complete. EOF without the expected terminal event is not success.
5. Parse tool arguments explicitly. Invalid/incomplete arguments must not silently become an empty object that triggers a tool with unintended defaults.
6. Handle each relevant stop reason, continuation limit, and tool-call limit visibly and deterministically.

### Conversation transaction

Maintain a working turn and a committed, replayable conversation.

- Never leave an assistant tool-use block in committed history without the corresponding tool result required for the next request.
- On failure, either roll back the uncommitted portion or append a valid error tool result according to a documented policy. Choose one consistent transaction model and test it.
- Commit complete assistant/tool exchanges at explicit boundaries. Retain earlier committed turns.
- Show partial text as interrupted when useful, but do not silently replay it as a complete assistant answer.
- Preserve supported server-tool blocks and thinking/signature fields needed for valid continuation rather than flattening everything to text.
- Stop, New Chat, and model changes must invalidate outstanding callbacks using a turn/session identity. An old promise must not append text or tool results into a newer conversation.
- Restore controls in a finally path regardless of the failure origin.

### Citation replay

Accumulate streamed citation deltas into the correct text block. Preserve their association when rendering, serializing history, and replaying subsequent messages. Handle multiple text blocks and unknown optional citation shapes without fabricating attribution.

### API key lifetime

Keep the key in page memory for the current session. Stop storing it in sessionStorage; remove the legacy stored-key entry without automatically reimporting it. Clear in-memory references when the user chooses Forget Key and on page teardown where practical.

Wrap optional nonsecret preference storage so unavailable or restricted storage does not break chat. Preserve report context and other intended functionality.

### Acceptance

Run behavior tests against the exact shipped script, not a hand-copied simplified parser.

- An error event following tool_use cannot corrupt the next turn.
- Premature EOF, invalid tool JSON, request rejection, and reader failure all restore the UI and leave valid history.
- Split frames and multibyte text reconstruct correctly.
- Citation deltas survive the next request.
- Stop/New Chat/model-change races cannot mutate the wrong session.
- No API key is written to web storage or interpolated into the exported report.
- Existing HTML escaping and CSP checks still pass.

Use the existing Node-based test harness for deterministic stream tests. Add a browser smoke test only if it exercises behavior the harness cannot cover; keep any browser interception tooling a test dependency.

**Tests:** test_html_report_javascript.py and test_html_report_exporter.py; new focused behavioral cases in the existing harness.

## 17. WP-13 — Make tracing optional, honor deep-trace settings, and limit credential lifetime

**Priority:** P2.  
**Targets:** src/gui/review_run_controller.py, src/gui/batch_controller.py, and src/gui/context_controller.py; tracing startup/teardown; src/core/api_config.py and client construction; token-analysis and other consumers of runtime client configuration.

### Failure-safe run startup

Move trace startup, session reattachment, and other fallible preparation inside the worker's lifecycle protection. A trace-directory or recorder failure must not strand a busy GUI or suppress the primary review error.

- Continue the review without tracing when only optional tracing fails, with one concise warning.
- Dispose of a partially initialized recorder and its worker resources.
- Clear the global recorder only if it belongs to the failed/finished run; an old callback must not clear a newer run's recorder.
- Ensure widget restoration and completion notification run on every exit.
- Teardown failures must not replace the meaningful review exception.
- Cover both fresh-run and resumed/reattached-run entry points.

### Thinking display in deep traces

When a supported deep-trace setting is enabled, request the documented summarized thinking display on the relevant core API paths. Keep ordinary requests unchanged. Apply model/feature capability checks and preserve existing chat behavior, which already requests summarized thinking.

The setting changes diagnostic visibility; it is not a promised cost reduction. Do not log absent/omitted thinking as if it had been returned.

### GUI credential ownership

Stop copying a GUI-entered key into process-global environment variables in the three affected controller flows.

Pass a nonserialized runtime credential/client provider through the existing orchestration boundaries. Capture the selected credential/client for a run so changing the UI field mid-run does not unexpectedly mix accounts or clients.

Preserve command-line environment input as a supported input path. Do not temporarily set and restore a global environment variable around a call; that remains unsafe with concurrent workers.

Review subprocess launches the application controls and sanitize inherited sensitive variables where feasible without breaking platform behavior. Do not claim that os.startfile supports an explicit environment. The primary fix is eliminating GUI injection into the global environment.

### Acceptance

- Trace initialization and reattachment failures leave a usable GUI and an otherwise functioning review.
- Teardown after a partial startup leaves no live recorder thread or stale active recorder.
- A failed old run cannot clear a new run's recorder.
- Deep trace changes only the intended supported request field; normal-mode request snapshots remain stable.
- Fake GUI keys do not appear in the process environment, saved state, report payloads, or tracing metadata.
- CLI environment credentials and concurrent runs still behave correctly.

**Tests:** trace recorder teardown/retention tests; GUI controller tests; prompt/request serialization tests; client configuration tests. Use fake keys exclusively.

## 18. WP-14 — Preserve pending repairs and unify collection completion decisions

**Priority:** P1.  
**Targets:** src/orchestration/pipeline.py; src/orchestration/program_pipeline.py; GUI batch/review controllers; CLI collection; saved batch state; repair collection tests.

### Collection outcome contract

Separate “there are reportable primary findings” from “the remote job and its repairs are finished.”

Represent at least these distinctions explicitly: no repair needed, repair submitted/pending, repair temporarily unreachable, repair consumed, and repair conclusively terminal/unusable. Unknown transport state is not terminal completion.

Persist the submitted repair batch ID and the item/request mapping required to retrieve it. Preserve compatibility with existing recovery manifests. Infer legacy state conservatively; missing newer metadata must not authorize destructive cleanup.

Return a structured collection outcome that carries completion and cleanup eligibility alongside usable results. Do not hide pending repair inside an ordinary success return.

### Cleanup policy

1. Centralize the decision to clear saved work and use it in GUI, CLI, single-module, and program flows.
2. Keep state while a repair is pending, temporarily unreachable, or otherwise retrievable but unconsumed.
3. Keep an all-failed collection recoverable by default, matching the safer existing CLI behavior. Explicit discard is a separate user action.
4. A valid zero-findings completed review may clear state when no outstanding work remains.
5. Partial results may be displayed without clearing unresolved state.
6. Validate the run/batch identity before clearing. A stale callback cannot delete a newer run's recovery record.
7. Avoid blind resubmission. Resume the existing repair batch unless it is conclusively unusable and the retry policy explicitly permits replacement.

### Prevent duplicate downstream work on resume

A reportable primary result with a pending repair must not trigger a fresh cycle of paid downstream analysis every time collection is retried.

Prefer the simplest reliable policy: expose primary results as provisional and defer dependent verification, cross-check, compliance, and drawing coordination until the required repair inputs settle. Indicate which stages are awaiting repair.

For program runs, resolve collection readiness before launching dependent paid stages, or maintain explicit completed-stage checkpoints with input fingerprints. Do not introduce checkpoints casually: a changed repair result can invalidate downstream inputs and legitimately require new work, which must be visible and accounted for.

Re-reading remote completed results is different from starting new paid analysis. Tests should count actual paid submissions, not prohibit harmless collection polling.

### Collector consolidation

Share the pure outcome/cleanup and stage-readiness decisions first. Consolidate more of the four collection paths only if that reduces verified drift without a risky wholesale rewrite. Preserve progress callbacks and GUI dispatch.

### Acceptance

- Primary output requiring repair remains automatically recoverable while repair is pending.
- Restart/resume collects the same repair ID and does not submit a duplicate.
- A temporary retrieval failure preserves state.
- All-failed, valid-empty, partial, successful, and explicitly discarded outcomes follow the same rules across entry points.
- Program children retain unresolved state independently and do not repeat completed paid work.
- A provisional report identifies pending stages.
- A stale completion cannot clear a newer manifest.

**Tests:** existing batch/repair collection and program-pipeline tests; saved-state roundtrip tests; GUI/CLI parity cases; paid-call-count assertions.

## 19. WP-15 — Account for every actual review and repair attempt

**Priority:** P1 for trustworthy cost reporting.  
**Targets:** reviewer/repair result aggregation; src/orchestration/diagnostics.py; batch and real-time collectors; cost/report displays.

### Accounting contract

Keep finding selection separate from attempt accounting. Replacing unusable primary findings with repaired findings must not replace the original request's known billable usage.

1. Create or consistently use a per-attempt usage representation carrying operation, model, transport, token/cache categories, known search usage, and a stable attempt identity.
2. For remote batches, batch ID plus item/custom ID and attempt role should identify an attempt without conflating the original review and its repair.
3. Count known usage from failed, truncated, or malformed attempts. Unknown usage stays unknown, with a diagnostic qualifier; do not invent zero or estimate it as an exact invoice.
4. Preserve primary and repair attempts even when only repaired findings become the final result.
5. Choose one billing input per operation: attempt records or an already aggregated total. Do not charge both.
6. Preserve existing real-time attempt telemetry, which already includes primary/repair calls. Refactor consumers without double-counting it.
7. Apply the relevant pricing categories: ordinary versus batch token prices, input/output, cache writes by TTL, reads, and separately priced search usage.
8. Cached/shared followers must not duplicate the leader's charge.

### Recovery scope and presentation

When recovering an older batch, distinguish known historical batch spend from new spending caused by recovery. Do not imply that a recovery report reconstructs an account invoice or earlier research costs that were never saved.

Expose useful subtotals for original review, repair, verification/escalation, and other passes where records exist. Keep estimates labeled as estimates.

### Acceptance

- Primary plus repair cost equals the sum of both attempts even when the primary result is replaced.
- Failed parses with usage remain billable in diagnostics.
- Batch discount, cache TTL categories, reads, and search fees are each applied once.
- Repeated collection of the same attempt does not duplicate it.
- Existing real-time totals remain unchanged for an equivalent scenario.
- Shared followers cost zero additional API spend.
- Legacy results without attempt metadata remain readable and visibly limited.

**Tests:** test_diagnostics_cost_pricing.py, test_cache_write_accounting.py, test_diagnostics_budget_telemetry.py, test_realtime_review.py, and batch-repair collection tests.

## 20. WP-16 — Capture native evidence and correct fetch instructions

**Priority:** P2.  
**Targets:** verifier tool/result parsing; evidence/cache models; trace and report presentation; verifier prompt goldens.

### Evidence capture now; stronger enforcement later

Capture the native citation and retrieval information already returned by supported responses. Preserve, where available:

- Source URL/title and retrieval/tool identity.
- Quoted cited text and returned locator information.
- Association between a citation, its source document/result, and the claim/verdict.
- Which attempt/model produced it and whether it was fresh, cached, or shared.

Some citation shapes use document indexes and locations rather than a direct URL. Resolve through the corresponding response document metadata. Never attach an arbitrary nearby URL when the mapping is unknown.

Preserve compatibility with legacy cache entries lacking these optional fields. Bound stored/displayed evidence to what supports the verdict; do not start persisting whole fetched documents. Unknown optional citation shapes should be observable without discarding an otherwise valid result.

Maintain distinctions between model-written source text, native attribution, retrieved evidence, and validated semantic support. A native citation is evidence of attribution, not proof that the requirement or proposed edit is correct.

### Fetch prompt correction

Update instructions that categorically require search before every fetch. Document the provider-supported path for fetching a URL explicitly supplied by the user or already present in allowed conversation context, subject to the tool's actual constraints.

This does not mean a user-supplied URL is already verified. The verifier must still retrieve relevant content and assess support, edition, authority, and applicability.

Preserve existing tool capability gates. Do not enable fetch for a response-format/model combination that the client currently cannot support merely because the prompt mentions it.

### Acceptance

- Native citation metadata survives parsing, evidence display, and optional cache roundtrip.
- Missing native metadata leaves legacy behavior usable and honestly labeled.
- Document-index citations resolve only to the correct source.
- Blank sources remain invalid under WP-10.
- Search-derived and user-supplied permitted URL paths have consistent instructions.
- No lexical-overlap threshold changes acceptance in this package.

**Tests:** verifier evidence/source tests; cache serialization; prompt goldens; trace/HTML display tests.

## 21. WP-17 — Reconcile prompts, diagnostics, and documentation

**Priority:** P2/P3; complete before the correctness release.

1. Correct the Haiku prompt-cache minimum comment/documentation to the provider's current supported threshold; the reviewed value is 4,096 tokens. Verify capability tables against the selected model before implementation.
2. Describe token-count API results as estimates, not an exact guarantee. Distinguish provider counts, local estimates, fallback padding, context capacity, and configured output capacity.
3. Preserve and document existing cache continuation behavior instead of presenting it as missing.
4. Preserve the implemented coverage-first confidence rubric and governing-edition/adoption qualifications. Resolve remaining contradictory examples without weakening evidence requirements.
5. Make no-op demotion explainable. Derive or record a clear demotion reason at a stable normalization boundary; do not mutate findings inside a read-only report helper. Keep report severity counts and sidecar exclusion consistent.
6. Document the current cross-check scope before EX-06: analysis is within its actual chunks/modules, and a small program is not automatically checked across disciplines merely because it fits in one context window.
7. Distinguish “analysis incomplete,” “verification inconclusive,” “operational failure,” and “no issue found” in banners and status summaries.
8. Keep model/price commentary accurate. At the reviewed listed rates, Sonnet 4.6 is 40% of Opus 4.6 token pricing, not one fifth. Recheck current prices before a live comparison.
9. Describe thinking display as visibility control, not a reduction in billed reasoning.
10. Update architecture notes, troubleshooting, schema/recovery documentation, and trust-audit checklists only after their corresponding behavior is validated.
11. Review affected prompt/report goldens individually. Explain intentional changes and retain assertions for evidence, authority, escaping, and confidence behavior.
12. Keep release notes specific: fixed omissions, safer edits, visible incomplete coverage, retained repairs, and corrected accounting. Do not announce unmeasured quality or cost gains.

**Acceptance:** Documentation matches actual defaults and supported paths. Every previously identified misleading statement is corrected or explicitly marked as an unresolved measured proposal. No experiment is described as enabled before its promotion criteria pass.

## 22. EX-01 — Measure shared project-context prompt caching

**Classification:** Measured optimization; do not promise savings before observing cache reads.  
**Prerequisites:** WP-08 and WP-15; stable request serialization.

### Investigation and implementation

1. Capture the exact current request layout: tools, system blocks, project context, per-file content, continuation history, cache breakpoints, TTLs, and model.
2. Identify a genuinely identical project prefix across eligible requests. Module instructions, changing project facts, or different tools/models can prevent reuse.
3. Test whether moving stable project context ahead of variable file content improves reuse without altering prompt meaning or introducing a new trust boundary.
4. Respect the provider's current breakpoint limits and mixed-TTL ordering rules. Count breakpoints already used by continuation/resume logic; do not add one in isolation.
5. Keep variable filename/content, repair-specific material, and changing metadata outside the intended stable prefix where feasible.
6. Make cache write/read usage visible per attempt using WP-15. Include write premiums, expiry, batch behavior, and real-time request timing in the comparison.

### Evaluation and promotion

Compare baseline and candidate on the same corpus, model, prompts, and settings. Measure cold writes and warm repeated requests separately. Include a changed-context control that must invalidate the intended prefix.

Report cacheable-prefix size, actual read/write tokens, net cost, latency, and any output-quality change. Do not infer cache reuse solely from a configured cache_control field or a request occurring “within five minutes.”

Promote only if measured net savings or latency gains are worthwhile and the request semantics remain sound. Otherwise retain the baseline and document the result. Do not force a caching redesign merely to complete this item.

## 23. EX-02 — Evaluate schema-constrained final outputs

**Classification:** Reliability improvement requiring capability and stop-reason validation.  
**Prerequisites:** WP-10; current client/model capability inventory.

### Scope

Evaluate provider-supported schema-constrained final output for review, compliance, research, or verification where it reduces real parse failures. Default Haiku triage already forces its output tool; preserve that behavior rather than treating it as missing.

### Requirements

1. Select a bounded first consumer with a measurable parse-failure problem.
2. Distinguish strict tool argument validation, forced tool invocation, and a constrained final response. One does not automatically imply the others.
3. Verify supported combinations of model, thinking, tools, citations, and output format against current documentation and the pinned SDK.
4. Preserve current capability guards, including incompatible fetch/format paths. Do not assume native citations and every constrained-output mode compose.
5. Keep semantic validation after schema validation: valid JSON can still have unknown IDs, missing coverage, blank evidence, unsafe edits, or contradictory claims.
6. Keep explicit refusal and output-limit handling. Schema guarantees do not eliminate all terminal failure modes.
7. Read older saved batch responses using the compatible legacy parser. Do not make a pending pre-upgrade batch uncollectible because new submissions use a new format.
8. Implement behind a narrow configuration switch until validated.

### Promotion criteria

Demonstrate fewer unparseable outputs without increased omission, unsupported findings, or loss of useful evidence. Compare retry/repair rate, total attempts, latency, and cost.

Offline fake responses must cover valid constrained output, legacy output, refusal, truncation, missing content, and unsupported capability selection. An SDK upgrade, if required, belongs in a separate reviewed change with its own compatibility checks.

## 24. EX-03 — Evaluate model selection, effort, and confidence behavior

**Classification:** Quality/cost decision; preserve current defaults until measured.  
**Prerequisites:** WP-01, WP-10, WP-15, and WP-16 where evidence is scored.

### Dataset and controls

Build an adjudicated set of representative findings with source evidence and expected outcomes. Include:

- Correct citations and supported deficiencies.
- Wrong editions and adoption-date ambiguity.
- Numeric/units differences, exceptions, and negation.
- Ambiguous or inaccessible evidence that should remain UNVERIFIED.
- Plausible but false model claims.
- Low-severity factual issues and severe omissions.
- Project-specific overrides and mixed module authority.

Record corpus/prompt/schema versions and dataset hashes. Separate training/tuning examples from held-out evaluation cases.

Isolate both in-memory and disk caches by experimental arm, or use fresh isolated instances. The existing cache is intentionally not simply keyed by model; disabling disk persistence alone does not prevent in-memory cross-arm contamination.

### Experiments

Evaluate one change at a time:

1. Current escalation path versus a compatible alternative model.
2. Current review effort versus one justified lower or higher setting.
3. Specific remaining prompt contradictions or confidence-calibration changes.

Keep governing-basis fingerprint behavior and existing feature defaults unchanged unless that is itself the isolated experiment. Do not silently enable an unrelated currently disabled feature.

### Metrics and decision

Measure severe-defect recall, unsupported-finding rate, false CONFIRMED and false DISPUTED rates separately, legitimate uncertainty, evidence quality, repair/failure rate, cost, and latency distribution. Report sample sizes and uncertainty; a few examples cannot establish broad quality equivalence.

Set acceptable regressions before running the evaluation. A cheaper model is not a success if it misses consequential defects. Publish the observed tradeoff and retain the current default when the result is inconclusive.

Recheck model capability and prices at evaluation time. Avoid assumptions that one model is a fixed fraction of another's cost or that an effort setting imposes a hard reasoning-token cap.

## 25. EX-04 — Calibrate evidence validation and reuse resolved sources

**Classification:** Two related but independently gated changes.  
**Prerequisites:** WP-10 and WP-16.

### A. Evidence validation in observation mode

Develop validation against an adjudicated evidence set before allowing it to change verdicts.

- Check source identity, edition, authority, quoted support, and claim meaning.
- Include paraphrases, omitted exceptions, reversed negation, and changed numbers/units.
- Treat lexical similarity as a diagnostic feature, never an arbitrary universal acceptance threshold.
- Keep retrieval success, native citation attribution, and semantic entailment separate.
- Report disagreement with current verdicts for human assessment before enforcing a new rule.

If a stronger acceptance policy is promoted, version its semantics and prevent old cached verdicts from silently bypassing it. Target affected entries/policies rather than wiping unrelated caches.

### B. Shared source resolution

Prototype reuse of retrieved/resolved evidence across findings about the same authoritative material.

Keys must reflect the actual claim context: normalized reference, edition, jurisdiction/adoption basis, relevant authority or client standard, source snapshot/freshness, applicable profile/module, and resolver-policy version. A bare standard name and calendar month are insufficient.

Reuse source retrieval, not a verdict stripped of its claim context. Every finding still needs its own support assessment. When the required source is absent, stale, incompatible, or insufficient, fall back to fresh resolution.

Preserve provenance and distinguish shared-source retrieval from same-finding verdict reuse. Do not pretend the current call freshly fetched content that came from a prior cache.

### Acceptance and promotion

Offline tests prove key separation, invalidation, fallback, provenance, and no double billing. A bounded live comparison must show saved retrieval work without degraded support judgments. Promote validation and source reuse separately if only one has adequate evidence.

## 26. EX-05 — Evaluate requirements-research reuse

**Classification:** Measured optimization with freshness and applicability risks.  
**Prerequisites:** Correct research accounting and stable requirements-profile serialization.

### Cache contract

Design the key from canonical, materially relevant inputs, including:

- Location/jurisdiction and the effective date basis.
- Client/project requirements and applicable module(s).
- Relevant corpus signals and explicit edition constraints.
- Research prompts, schema, tools, source policy, and resolver version.
- Settings that change authority or applicability decisions.

Do not normalize unknown values into assumed equivalents. Define how dates are resolved; “same month” is not a sufficient applicability guarantee.

Store a bounded completed research profile with its provenance and freshness metadata, not full input specifications. Incomplete/failed research is not a normal reusable success. Define partial-profile behavior explicitly if later supported.

Offer a deliberate refresh path. Show the reused profile's age and governing basis. A cache hit must not silently override newly supplied project constraints.

### Acceptance and promotion

Test same-input reuse, every materially relevant changed-input miss, stale-entry handling, corrupted-entry rejection, and explicit refresh. Measure hit rate, saved calls, and inappropriate reuse on representative repeated projects.

Do not enable by default until the key and freshness policy have demonstrated safe applicability. Keep current governing-basis feature defaults unchanged unless separately evaluated.

## 27. EX-06 — Add bounded cross-chunk and cross-module coordination

**Classification:** Staged capability development, not a guaranteed small-cost patch.  
**Prerequisites:** WP-02 through WP-09, WP-15, and stable source identity.

### Problem and initial scope

Chunked analysis can miss relationships across chunk boundaries. Existing module-specific checks also do not establish program-wide coordination, even for a small project.

Start with a narrow set of coordination facts likely to support useful, verifiable checks: equipment/system identity, capacity/rating, material, supply characteristics, location, interface requirements, and responsibility assignments. Choose the initial categories from corpus evidence rather than building a universal fact schema.

### Architecture constraints

1. Extract source-anchored facts with original text, file/element locations, normalized values, units, scope, and uncertainty.
2. Retain raw values alongside normalized values. Do not equate similar-looking systems or units without a justified mapping.
3. Use facts to select candidate relationships and retrieve relevant original passages for final reasoning.
4. Every reported conflict must cite both sides and explain why they refer to the same relevant scope.
5. Carry module and governing-basis provenance into mixed-discipline verification. Do not verify every cross-module claim using an arbitrary default module.
6. Keep discovery bounded by explicit budgets and candidate limits. Surface unassessed areas rather than claiming complete coordination.
7. Preserve finding-group and occurrence identities through the new pass.
8. Keep the current detailed per-file analysis. A lossy digest must not silently replace the source review.
9. Apply the same token, failure, completeness, and cost contracts used by existing passes.

### Rollout

Begin in observation mode behind a capability switch. First prove cross-chunk detection within one module, then cross-module checks for a small program.

Include fixtures where each document is individually plausible but two documents conflict, and controls where similar values belong to different equipment or phases. Measure false joins and missed conflicts, not only the number of findings generated.

Promote by category after adjudication. Report actual incremental cost and latency; do not promise “a few percent” overhead without measurement.

## 28. Validation matrix and execution order

### Focused validation by contract

| Area | Minimum evidence before integration |
|---|---|
| Extraction and numbering | Supported visible text recovered once; revision semantics preserved; old IDs retain meaning; synthetic numbering cannot corrupt edits. |
| Deterministic checks | Clean fixtures stay clean; each single-defect mutation triggers its intended result; suppression is context-sensitive. |
| Routing | Explicit, compact, conflicting, unknown, and ambiguous inputs resolve predictably; related-section references cannot override the document's own identity. |
| Finding identity | Distinct issues survive normalization; repeated locations survive grouping/export/application; stable IDs survive ordering changes. |
| Applier | Ambiguous basename fails before mutation; legacy and new schema behavior is explicit; source files are not overwritten. |
| Token budgets | Final request shape drives the decision; fallback padding is complete; oversized single groups are handled; model overrides change ceilings. |
| Compliance | Missing rows and failed chunks remain visibly incomplete; no unsupported global-absence edit is emitted. |
| Verification/cache | Malformed output fails; UNVERIFIED is not durable; same-run sharing works; valid old entries survive. |
| Retry/concurrency | Server floors respected; jitter deterministic under tests; no slot held during backoff; one retry owner. |
| Chat | Exact exported script survives split streams, API errors, orphan-tool scenarios, cancellation races, and citation replay. |
| Tracing/credentials | Startup/teardown failure restores UI; no stale recorder survives; GUI keys remain out of process-global environment and persistence. |
| Recovery | Pending repairs survive restart; no duplicate paid submission; cleanup policy agrees across entry points. |
| Cost | Primary and repair attempts both counted once; failed-output usage retained; cached/shared results do not add charges. |
| Evidence | Native attribution roundtrips honestly; legacy data remains readable; stronger enforcement remains gated. |
| Experiments | Isolated baseline/candidate measurements; reproducible datasets; explicit enable/defer/reject decision. |

### Test execution

1. Run focused tests while developing each package.
2. Run integration tests at contract boundaries before merging consumers.
3. Run the repository's full non-network suite once the integrated correctness candidate is stable.
4. Run dependency consistency checks and the JavaScript-required CI configuration.
5. Re-run relevant checks after further changes or failures; do not repeatedly run the whole suite without a reason.
6. Perform a Windows application smoke test for review startup, recoverable batch collection, HTML chat, and safe sidecar application using controlled fixtures.
7. Validate changed report output for readable partial-status notices, evidence labels, and occurrence locations.

Use the current repository-prescribed commands and environment. At the reviewed baseline, the relevant checks include:

~~~text
python -m pip check
python -m pytest -m "not network"
~~~

Set SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1 for the required JavaScript coverage, as CI does. Ensure Node is available rather than accepting an unnoticed skip. The reviewed CI uses Python 3.11 and Node 22; follow the actual CI at implementation time.

Do not run release/version checks that assume a version change unless the release process calls for one. Do not run live API tests as part of the default suite.

### Representative integration scenarios

Keep these as a few comprehensible end-to-end cases plus focused failure injections, rather than one enormous brittle test.

**A. Input to safe edit:** A document contains a content-control paragraph, an automatically numbered article, a table-only article body, and the same correctable issue in two locations. Extraction preserves all supported content. Grouping retains both occurrences. The sidecar preserves literal locators. Application edits only supported targets and refuses unsupported wrapper intersections.

**B. Incomplete analysis:** Mocked request counts force chunking. One chunk fails, another omits a controlling coverage row, and another returns useful findings. The final report retains useful findings, displays incomplete coverage, and emits no unsupported global-absence addition.

**C. Paid repair and resume:** A primary review is truncated with known usage. A repair batch remains pending. A provisional report is available, saved state survives restart, and downstream paid work is deferred. Later collection consumes the existing repair, accounts for both attempts, completes downstream work once, and only then clears eligible state.

**D. Verification and user recovery:** Repeated equivalent findings share one legitimate UNVERIFIED result within the run. A malformed result is a failure rather than a cache hit. A later independent run retries the uncertainty. Chat encounters an API error after a tool request, restores controls, and sends a valid next turn.

## 29. Compatibility, rollout, and rollback

### Sidecar and applier migration

Land a reader capable of the legacy and new sidecar contracts before or with the new writer. Document exact supported versions and fail clearly on unsupported versions.

Do not downgrade a new multiple-occurrence sidecar into a legacy format by dropping locations. A rollback to an older application build may require retaining the newer applier reader for already-generated sidecars. Original sidecar files and source documents remain unchanged.

### Saved state and result models

Prefer additive optional fields with explicit legacy defaults. Missing completeness, attempt-usage, or repair-state metadata must not be interpreted as newly proven success.

Include compatibility tests using baseline serialized examples. Resuming an old batch must still use the parser and context necessary for that batch.

### Verification caches

Invalidate entries by eligibility, evidence-policy version, standards fingerprint, or actual schema incompatibility. Avoid destructive blanket invalidation when safe entries can be retained.

### Request and experiment switches

Keep experimental prompt/model/caching/coordination behavior independently switchable. Disabling an experiment must restore the validated request behavior without reverting unrelated correctness fixes.

Do not expose confusing implementation toggles in ordinary product flows. Keep developer/evaluation controls where the repository already supports such controls.

### Release order

1. Land baseline regressions and low-risk independent fixes.
2. Land shared result contracts and compatible readers.
3. Integrate producers and consumers of extraction, occurrences, coverage, recovery, and attempt usage.
4. Complete required cross-entry-point tests, reports, and documentation.
5. Release the correctness work.
6. Run and assess gated experiments independently; release only promoted changes.

A measured optimization should not delay urgent correctness fixes. Conversely, a passing unit test is not sufficient grounds to enable an unmeasured model or architecture change.

## 30. Agent handoff requirements and completion criteria

### Deliverable from each implementation package

Each agent should provide:

1. The behavior changed and the defect it prevents.
2. Files/contracts changed, including consumer migrations.
3. Focused test results and meaningful edge cases covered.
4. Compatibility, rollback, and pending-state consequences.
5. Any remaining unsupported case or revised assumption.
6. For experiments: dataset/configuration hashes, isolated arm settings, measurements, spend, and an enable/defer/reject recommendation.

If current code differs from this baseline, explain the difference and adapt the implementation. Do not mechanically apply stale function names or force a redesign when a smaller verified fix satisfies the contract.

### Correctness release is complete when

- Every WP-01 through WP-17 acceptance criterion has a linked implementation/test or a documented evidence-backed closure because the behavior was already fixed.
- All supported text survives extraction with usable provenance; unsupported editing is refused safely.
- Different issues and different occurrences remain distinct through application.
- Request budgeting uses the assembled selected-model request and a conservative failure path.
- Missing coverage and operational failure cannot appear as complete clean analysis.
- Inconclusive verdicts do not become long-lived cache successes.
- Retry waits respect server timing and do not consume request slots.
- Chat failures leave replayable history and usable controls.
- Trace failures cannot strand a run; GUI credentials do not enter global environment state.
- Pending paid repairs remain recoverable, with no blind duplicate submissions or repeated downstream spending.
- Attempt totals include both original and repair requests exactly once.
- Legacy sidecars, caches, and recovery records follow their documented compatibility rules.
- Required offline/JavaScript checks pass, and material skips or limitations are explicit.
- Documentation and report wording match shipped behavior.

### Experimental work is complete when

Each EX item has a reproducible result and a recorded enable/defer/reject decision. If a live comparison has not run, say “not evaluated”; do not substitute a speculative savings estimate or mark the capability production-ready.

Default enablement requires its own stated quality, reliability, and cost criteria to pass. An unfavorable result is a valid outcome of an experiment.

## Appendix A. Compact regression examples

These examples express intended behavior; use the repository's actual input and result models.

| Case | Expected result |
|---|---|
| Article heading followed by valid nested subparagraphs | Not an empty article. |
| Article body exists only in its table | Not an empty article. |
| Quantity such as 1.5 or an ordinary numbered list item | Not automatically a CSI article heading. |
| “Prior to fabrication, submit shop drawings” | Does not suppress an unrelated stale edition. |
| “Historical Society” as an organization name | Does not make surrounding requirements historical. |
| “May not deviate” / “shall not deviate” | Does not suppress the mandatory requirement. |
| Bare “TBD” in active specification content | Detected once, subject to explicit placeholder policy. |
| A SECTION heading appears only under Related Sections | Cannot override the document's actual module identity. |
| Copper and PVC requirements in one known file | Remain distinct findings after filename normalization. |
| Same valid issue at p4 and p8 in one file | One presentation group may contain two executable occurrences. |
| Two selected files share a basename in different directories | Applier refuses an ambiguous basename binding before any write. |
| Nonempty controlling set and zero coverage rows | Incomplete assessment, not a clean completed compliance result. |
| Well-formed UNVERIFIED verdict | May share within the current run; is not a durable conclusive cache hit. |
| Malformed verdict after an ordinary end turn | Operational failure with retained known usage. |
| API error following assistant tool_use | Valid recovery/rollback; no unmatched tool call in the next request. |
| Trace initialization fails | Review lifecycle and GUI recover; optional tracing is disabled visibly. |
| Primary review needs repair and repair is still pending | Recovery manifest survives; dependent paid stages do not repeat. |
| Primary costs X and repair costs Y | Known review total includes X + Y exactly once. |

### Clean three-PART structure

Build a DOCX with the following manual heading structure and ordinary body paragraphs. It should produce no empty-heading or duplicate-heading alerts.

~~~text
PART 1 GENERAL
1.01 SUMMARY
A. Provide the specified piping system.
1.02 SUBMITTALS
A. Submit product data before fabrication.
PART 2 PRODUCTS
2.01 MATERIALS
A. Provide materials meeting the scheduled requirements.
PART 3 EXECUTION
3.01 INSTALLATION
A. Install in accordance with the approved product instructions.
~~~

Create separate variants with a table as the only article body, automatic numbering, an actually empty article, and an actually duplicated article heading. Keep the clean control separate from each defect mutation.

### Filename-normalization collision

With 210500.docx in the known corpus, use two findings whose other deduplication fields are deliberately identical:

- “Section 21 05 00 requires copper pipe in 210500.docx.”
- “Section 21 05 00 requires PVC pipe in 210500.docx.”

The old broad pattern can remove the meaningful material distinction between the section number and the filename. The corrected normalization may remove the exact known filename, but must preserve copper versus PVC and produce distinct issue identities.


## Appendix B. Provider documentation to recheck during implementation

These references supported the September 22, 2026 review. Provider behavior, limits, pricing, and SDK support can change; use the current documentation for the exact selected model and pinned SDK.

- [Token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting): request counts are estimates; validate the assembled request and fallback behavior.
- [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching): model minimums, cache prefixes, breakpoint limits, TTL rules, and observed read/write accounting.
- [Web fetch tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool): eligible URLs, tool behavior, and citation/result shapes.
- [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs): capability constraints, refusal/truncation exceptions, and format/tool distinctions.
- [Model pricing](https://platform.claude.com/docs/en/about-claude/pricing): model, batch, caching, and search price categories.
- [Thinking, steering, and cost](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost): effort and thinking-display semantics.

Repository guidance, current source, executable regression tests, and measured results remain the implementation authority. External documentation must not be used to justify removing a local compatibility guard without testing the actual request path.
