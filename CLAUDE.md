# CLAUDE.md — Spec Critic

Python desktop app that reviews California K-12 DSA mechanical/plumbing CSI-format `.docx` specs against California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) using Claude.

`README.md` is the long-form architecture and pipeline reference. This file is the short list of constraints, decisions, and gotchas Claude needs that aren't obvious from reading the code.

## Commands

- Tests: `pytest -q` from the project root. Hermetic by default — no API key, no network.
- Fast pre-commit pass: `pytest -q -m smoke`.
- Single category: `pytest -m request_shape` (or `parser_unification`, `source_grounding`, `verification_modes`, etc. — markers declared in `pyproject.toml`).
- Run the app: `python main.py`.
- Python 3.11+. Install deps with `pip install -r requirements.txt`.

`tests/conftest.py` injects a placeholder `ANTHROPIC_API_KEY` so production imports succeed. Tests that need real network opt in via `@pytest.mark.network`. GUI-dependent tests skip automatically when `tkinter` is unavailable.

## Domain vocabulary

- **DSA** — California Division of the State Architect. Reviews K-12 school construction docs. Primary project context.
- **HCAI** — California Department of Health Care Access and Information. Healthcare facility AHJ; appears alongside DSA in spec language.
- **AHJ** — Authority Having Jurisdiction (the local code official, often city/county building department).
- **CSI** — Construction Specifications Institute. Specs are organized by CSI division: 21 fire suppression, 22 plumbing, 23 HVAC, 25 integrated automation, 01 general requirements. Cross-check chunks by division.
- **CBC / CMC / CPC** — California Building / Mechanical / Plumbing Code (each on a code cycle, currently 2025).
- **LEED** — Green building rating; not typically applicable to DSA K-12, so LEED mentions in these specs are usually copy-paste artifacts (preprocessor flags them).
- **Reviewer vs. verifier.** *Reviewer* calls the model to find issues from the spec text. *Verifier* calls the model again, with web search, to adjudicate a finding against external sources. Separate phases, different prompts, different models, different output caps.
- **Finding vs. FindingGroup vs. FindingOccurrence.** A *Finding* is one issue. After dedup, a *FindingGroup* is "same issue, multiple files" for display; a *FindingOccurrence* is "apply this change to file X at location Y." Edit application iterates occurrences, not findings.
- **element_id.** Stable id stamped on every paragraph / table cell / heading at extraction (`p7`, `t0r2`, `s1h0`, …). When a finding cites `evidenceElementId`, the locator looks up the element directly instead of fuzzy-searching the document.
- **Local-skip.** Verification mode that resolves a finding without an API call (placeholder / LEED / duplicate-paragraph GRIPES, etc.). Saves Sonnet+web_search round-trips.

## Architectural decisions

- **Real-time and batch share identical prompts, models, tool schemas, output caps, and parsing logic** so findings are functionally equivalent across modes. Real-time = streaming, full price, immediate. Batch = queued, ~50% cheaper, async (45 min – 24 h). The single intentional asymmetry is the 300k extended-output path, batch-only because Anthropic does not honor the `output-300k-2026-03-24` beta header on streaming. Do not introduce mode-specific divergence without explicit reason.
- **Structured tool-use is the primary parsing path; tagged-JSON text is the documented fallback.** Adaptive-thinking calls cannot force `tool_choice` (API constraint), so the model is *strongly steered* to call the tool but not contractually required to. Do not remove the text fallback parsers in `reviewer.py`, `cross_checker.py`, or `verifier.py`.
- **`CONFIRMED` / `CORRECTED` requires a grounded citation.** The verdict cannot claim verified unless at least one cited URL matches a URL the `web_search` tool actually retrieved (after `source_grounding.normalize_url` normalization). The verification cache enforces the same invariant on `put`, and `load_from_disk` re-validates entries on read. Do not add a code path that emits CONFIRMED without an accepted citation.
- **Edits are surgical, not regenerative.** The pipeline never regenerates a DOCX. It locates the exact span on disk, revalidates the precondition immediately before the write, and replaces. If the expected text is missing or duplicated, the edit drops to manual review rather than guessing.
- **Id-anchored matching does not fall back to whole-document text search.** If `evidenceElementId` is set but the recorded quote no longer matches the cited element, the locator returns MANUAL_REVIEW. A text match elsewhere is treated as suspect — almost certainly a different occurrence with different surrounding context.
- **Verification routing is profile × mode × severity.** Profile sets the per-severity web-search budget ceiling; severity modulates within it. Mode picks the model, thinking flag, and search-budget multiplier. Do not add a verification call that bypasses `verification_routing.select_routing(...)`.
- **Cross-check uses dependency tracking, not heuristic file/section overlap.** A coordination finding survives unless every `upstream_finding_id` is `DISPUTED` *and* there is no `independent_evidence_id`. Dropped findings go on `suppressed_findings` with a `suppression_reason` — they do not silently disappear.
- **Deterministic preprocessor checks are not LLM jobs.** LEED / placeholder / stale-cycle / invalid-cycle / template-marker / duplicate-paragraph / etc. detection runs locally before any API call. The deterministic rule id is plumbed end-to-end so verification can local-skip and the report can label these `(deterministic check)`. Don't ask the model to redo them.
- **Trust-model statuses are derived, not stored.** `report_status.classify_status(finding)` is a pure function of `verification` / `suppression_reason` / `edit_proposal`. Adding a new status means extending the classifier, not adding a column to `Finding` or the cache.

## Gotchas

- **Don't force `tool_choice` when adaptive thinking is enabled.** The API rejects it. Use `tool_choice={"type": "auto"}` and rely on the system prompt to steer the model toward the tool. The tagged-JSON parser exists for the rare miss.
- **`tool_use` is a complete stop reason, not incomplete.** `classify_verification_stop_reason` treats `end_turn` *and* `tool_use` as COMPLETE; `pause_turn` as PAUSE; everything else as INCOMPLETE. Don't reintroduce a path that treats `tool_use` as a partial response.
- **Haiku phases must not carry a `thinking` payload.** `apply_thinking_config(...)` is the single gate. Synthesis (Haiku) and triage (Haiku) never get the `thinking` key; an extra one will be rejected by the API.
- **Synthesis and triage prompts are below the cache minimum** (1024 for Sonnet/Opus, 2048 for Haiku). `cache_policy_for(phase)` already disables caching for those phases. Don't re-enable it — you pay the write cost with no possible hit.
- **Resume state validates file hashes.** `deserialize_extracted_spec` warns if either the extracted content or the source file digest differs at resume time. Don't suppress the warning — it indicates the user edited the file mid-run and the resumed findings may no longer match on-disk content.
- **Stale-cycle and invalid-cycle detectors must stay disjoint.** Stale = a real published California cycle that is not the current one (e.g. `2019 CBC`). Invalid = a year that is not a real cycle at all (e.g. `2018 CBC`). A year is one or the other, never both.
- **The 2022 California cycle mapping was removed; do not reintroduce it.** Configured cycle is California 2025. The cycle label is part of the verification cache key, so a future cycle bump invalidates prior entries automatically.
- **The verification cache key intentionally omits the verifier model.** The same claim under the same code cycle has the same grounded verdict semantics; `model_used` is stored as provenance only. Don't add the model to the key.
- **Web-search tool config uses a blocked-domain list only, no allow-list.** The tool does not support mixing allow + block. California priority sources live in the verifier system prompt instead.
- **Edit output is all-or-none.** `spec_editor` saves to an in-memory buffer, validates by reopening as a `Document`, then suppresses the disk write entirely if any individual outcome failed. Previously-applied outcomes demote to `skipped`. Don't add a "partial save" escape hatch (`SPEC_CRITIC_EDIT_TRANSACTIONAL=0` exists as the documented operator opt-out).
- **Don't keyword-sniff log messages to infer phase.** Pipeline callers pass an explicit `phase=` to log/progress callbacks; the GUI consumes it directly. Don't reintroduce message-text matching.
- **Don't trust an unsafe-markup element.** `detect_unsafe_markup` walks the paragraph / cell subtree for hyperlinks, field characters, drawings, comments, tracked changes, bookmarks, content controls, footnotes, smart tags, custom XML. A hit refuses the mutation (`refused_unsafe_markup=True`) — do not "just try the edit anyway."

## Don't change without discussion

- The four-mode / five-profile / seven-status / four-edit-action closed sets in `verification_modes.py`, `verification_profiles.py`, and `report_status.py`. They are deliberately small.
- `pipeline.compute_finding_id` — the dedup key is the stable id exposed to cross-check upstream tracking.
- The 24-hex-char claim digest in `verification_cache`. `_LEGACY_CLAIM_DIGEST_LEN = 16` is preserved for a future migration tool; lookups against legacy-length keys miss and re-ground.
- Feature flags are listed in `README.md`. New ones belong in `api_config.py` with a documented default and a README entry.
