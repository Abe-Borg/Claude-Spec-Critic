# Spec Critic

**v2.11.0** — AI-assisted M&P specification review for California K-12 DSA projects.

Spec Critic reviews mechanical and plumbing CSI-format `.docx` specifications against California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) using Claude. It produces structured findings with severity classifications, confidence scores, verification verdicts backed by web search, optional cross-spec coordination analysis, and either inline edits or yellow-highlighted suggestion annotations on a copy of each spec.

The project is optimized for DSA-oriented K-12 workflows and is configured for the **California 2025 code cycle** by default; the cycle definition lives in `src/code_cycles.py` (`DEFAULT_CYCLE = CALIFORNIA_2025`) and is part of the verification cache key so a future cycle bump naturally invalidates prior entries. The codebase no longer carries the 2022-cycle mapping that earlier releases shipped with — do not reintroduce it.

Design emphasis:

- **Structured tool-use as the primary output path, with text fallback as a safety net.** Review, cross-check, and verification responses are parsed by reading the `submit_*` tool input first; a tagged-JSON text parser remains as a documented fallback because adaptive-thinking-enabled requests cannot force `tool_choice` (Anthropic API constraint), so the model is *strongly steered* — not contractually forced — to call the tool.
- **Evidence-grounded verification.** Web-search-backed verdicts; `CONFIRMED` / `CORRECTED` are only emitted when the model cites at least one URL that the `web_search` tool actually retrieved (see "Source Grounding" below).
- **Explicit verification modes with model routing.** Local-skip, strict-structured, standard-reasoning, and deep-reasoning modes pick the model, thinking flag, and search budget per finding — simple verification no longer always pays for the deep-reasoning path.
- **Cost-aware defaults.** Sonnet-default verifier with Opus escalation, optional Haiku triage, severity-tiered + profile-aware search budgets, persistent on-disk claim cache.
- **Robust batch processing.** Durable resume across every pipeline phase with content + source-file SHA-256 digests.
- **Safe Word output.** Surgical edits gated by safety categories, with id-anchored matching preferred when the model cites a paragraph id; offset revalidation runs immediately before every mutation so a stale offset cannot replace the wrong text. Annotate mode is non-destructive and writes yellow-highlighted suggestion paragraphs instead of mutating the source.
- **Trust-model report output.** Every finding renders one of seven `ReportStatus` labels (Verified Supported, Verified Contradicted, Disputed, Insufficient Evidence, Locally Classified, Not Checked, Manual Review Required) and one of four `EditActionLabel` values so the report makes uncertainty visible instead of mixing supported and disputed findings together.

---

## Pipeline at a Glance

1. **Text Extraction** — Reads `.docx` files locally (paragraphs, tables, headers/footers). Cached by file hash so repeated runs over the same files do not re-parse. Each paragraph / table cell / heading is stamped with a stable `element_id` (`p7`, `t0r2`, `s1h0`, …) so downstream findings can cite a deterministic target instead of relying on fuzzy text rediscovery.
2. **Local Pre-Screening** — Deterministic detectors run before any API call: LEED references, unresolved placeholders (`[SELECT]`, `[VERIFY]`, `TBD`, etc.), unresolved template markers (`TODO:` / `FIXME` / `XXX` / lorem ipsum), stale and invalid code-cycle references, empty sections, duplicate headings, duplicate paragraphs (whole-paragraph copy-paste mistakes), and inconsistent file naming. Every alert carries a stable `deterministic_rule` id and is rendered under a `(deterministic check)` heading in the report.
3. **Per-Spec Review** — Each spec sent individually to Claude Opus 4.7 for code-compliance review. The model is asked to call the `submit_review_findings` tool whose `input_schema` matches the finding shape; the schema is the primary parsing path. A tagged-JSON text parser remains as a fallback because the API rejects forcing `tool_choice` when adaptive thinking is enabled — with only one tool exposed and the system prompt instructing the model to call it, the tool is reliably (but not contractually) invoked.
4. **Deduplication** — Consolidates identical findings across multiple specs (full-text SHA-256 keys; per-file occurrences tracked separately so multi-file edits fan out correctly). Every review finding is stamped with a stable `finding_id` (`rf-<12hex>`) so cross-check findings can cite upstream review findings by id.
5. **Cross-Spec Coordination** *(optional)* — Full-content analysis of all specs together. Large projects are chunked by CSI division (21 / 22 / 23 / Controls / 25 + 01) and merged. Runs in parallel with verification by default. Cross-check findings emit `upstreamFindingIds` (review-finding ids the coordination claim depends on) and `independentEvidenceIds` (raw-spec element ids supporting the claim independently); the post-verification suppression filter drops a cross-check finding only when every upstream is `DISPUTED` *and* it has no independent spec evidence.
6. **Verification** — Findings classified into one of four modes (`local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`) before any API call. Standard mode runs on Sonnet 4.6 with adaptive thinking; CRITICAL/HIGH `UNVERIFIED` findings escalate to Opus 4.7 (`deep_reasoning`). Verdicts come back through the `submit_verification_verdict` tool whose payload is parsed by the canonical `parse_verification_response` parser (text JSON is the documented fallback). The verifier validates every model-cited URL against the URLs the `web_search` server tool actually retrieved; ungrounded citations downgrade `CONFIRMED` / `CORRECTED` to `UNVERIFIED`. A persistent on-disk claim-keyed cache and local-skip classification (placeholders, LEED, internal contradictions, duplicate GRIPES) avoid redundant searches. Optional Haiku verification triage further filters internally-verifiable findings. Small batch retry tails fall back to real-time verification.
7. **Edit Application** *(optional)* — Two modes:
   * **Edit mode** — Surgical edits applied to a copy of each spec. When the model cited an `evidenceElementId`, the locator looks up the element directly and revalidates the recorded quote against it (AUTO_SAFE for body paragraphs, AUTO_WITH_CAUTION for table cells); if the id is set but the quote no longer matches the cited element, the edit is dropped to manual review **without** falling back to whole-document text matching. When no id is cited, the locator uses the legacy exact / normalized / section-anchored / fuzzy match cascade. Ambiguous, table, header/footer, or rich-formatted matches are downgraded to manual review. Every mutation revalidates the precondition immediately before the write — corrected offsets are used if the expected text shifted to a unique new location, and duplicates / missing text never auto-apply.
   * **Annotate mode** — Yellow-highlighted suggestion paragraphs inserted after each anchor; the original text is never mutated. Safer for table cells, header/footer text, and richly formatted paragraphs.

## Project Identity

- **App version:** `2.11.0` (package `src.__version__`).
- **Packaging version:** `2.8.0` in `pyproject.toml` (sync to `src/__init__.py` when cutting a release).
- **Runtime:** Python 3.11+ desktop app (CustomTkinter + TkinterDnD2).
- **Model stack** (defaults; every model identifier lives in `api_config.py` and is overridable via env var):
  - Review / Cross-check: Claude Opus 4.7
  - Verification (initial): Claude Sonnet 4.6
  - Verification (escalation / deep-reasoning mode): Claude Opus 4.7
  - Cross-discipline synthesis: Claude Haiku 4.5
  - Optional verification triage: Claude Haiku 4.5
- **Model capability policy:** `api_config.model_capabilities(model)` is the single source of truth for adaptive-thinking support, output-token ceiling, batch-extended-output eligibility, and 1M-context eligibility. The whitelist currently covers Opus 4.6 / 4.7, Sonnet 4.6, and Haiku 4.5. Unknown model ids degrade to safe defaults that disable every capability flag (no adaptive thinking, conservative output cap), so a misconfigured `SPEC_CRITIC_*_MODEL` env var produces a smaller request rather than an API rejection. Adaptive thinking is added to a request only when both the model supports it and the phase opts in — Haiku phases (triage, synthesis) never carry the `thinking` key.

## Processing Modes

- **Real-time** — Immediate in-session processing (streaming API, higher cost).
- **Batch** — Queued processing at 50% cost savings (usually 45 min – 2 hrs, 24 hrs max).

Both modes share identical prompts, models, tool schemas, output caps, and parsing logic, so findings should be functionally equivalent across modes. The 300k extended-output path is the only intentional asymmetry — batch-only by API design (`output-300k-2026-03-24` beta header is not honored on streaming requests) and only used for inputs ≥200k tokens. Batch state is persisted to disk with content + source-file SHA-256 digests and survives app restarts — resume from any phase: review-poll, review-collect, cross-check, verification (poll / wave-poll), or finalize.

## Review Modes

The reviewer prompt has three modes that adjust scope and edit safety:

| Mode | Scope | Auto-edit |
|---|---|---|
| Strict | Evidence-backed contradictions and code-cycle issues only | Allowed |
| Comprehensive *(default)* | Strict scope + constructability, TAB/commissioning, equipment schedule conflicts, Division 01 coordination, warranty, basis-of-design, controls sequence, DSA/HCAI/Title 24 closeout, fire/smoke damper access, seismic restraints, sprinkler/hydraulic, pipe/duct material, submittal/O&M | Allowed |
| Safe-edit | Findings with exact editable anchors and low-risk replacements only | Allowed |

The active mode is recorded in resume state so a resumed run uses the same prompt path it started with.

## End-to-End Workflow

1. Load spec files (`.docx` only).
2. Extract body + table + header/footer text while preserving useful paragraph mapping metadata.
3. Run local pre-screen checks (LEED references, unresolved placeholders, stale code cycles, empty sections, duplicate headings, file-naming inconsistencies) without API calls.
4. Run primary compliance review per spec (real-time or batch mode) via the `submit_review_findings` tool.
5. Deduplicate findings across specs and group them into `FindingGroup` / `FindingOccurrence` for display vs. per-file edit execution.
6. Optionally run cross-spec coordination check to catch contradictions, scope gaps, and interface misses (chunked by CSI division on large projects).
7. Run verification phase with web-search-backed adjudication for each finding (Sonnet default, Opus escalation, persistent claim cache, local-skip classification, optional Haiku triage).
8. Export the `.docx` review report.
9. Optionally generate and apply surgical edits — or non-destructive annotations — back into source Word documents.

## Output Surfaces

- **Word Report** — Formatted `.docx` report with Word-native heading collapse, colored severity table, verification verdicts with sources (cited URLs rendered inline in blue after the verdict/explanation/correction block), and coordination summary.
- **Apply Edits** — `Finding.affected_files` drives multi-file fan-out so a deduped finding edits (or annotates) every affected spec.

## Module Map

| File | Purpose |
|---|---|
| `main.py` | PyInstaller entry point |
| `src/gui.py` | CustomTkinter GUI — inputs, mode selection, batch resume, diagnostics |
| `src/widgets.py` | Custom GUI widgets — TokenGauge (with API-exact preflight), FileListPanel, EnhancedLog, EditSelectionDialog, DiagnosticsWindow (renders token / cache / evidence / output / search telemetry) |
| `src/pipeline.py` | Core orchestration — preparation, review, cross-check, verification, finalization. Defines `FindingGroup` / `FindingOccurrence` for display vs. edit-execution split |
| `src/api_config.py` | Centralized model identifiers, model capability policy (Chunk B), phase output-budget registry (Chunk E), phase-aware prompt-cache policy (Chunk J), web-search tool config, feature flags |
| `src/structured_schemas.py` | Tool-use schemas for review, cross-check, and verification — the primary parsing path. Tagged-JSON text parser stays as a documented fallback because adaptive-thinking calls cannot force `tool_choice` |
| `src/review_modes.py` | Strict / Comprehensive / Safe-edit mode definitions |
| `src/prompts.py` | System prompt and user message construction (mode-aware) |
| `src/prompt_serialization.py` | Central escape / wrap helpers for prompt boundaries (Chunk G); `render_spec_with_ids` for id-tagged document rendering (Chunk K2) |
| `src/reviewer.py` | Claude API client — streaming review, tool-use parsing, finding parsing |
| `src/cross_checker.py` | Cross-spec coordination reviewer — chunked by CSI division for large projects; emits `upstreamFindingIds` / `independentEvidenceIds` (Chunk M) |
| `src/verifier.py` | Web-search verification — canonical `parse_verification_response` parser (Chunk D), source grounding (Chunk H), Sonnet-default with Opus escalation, real-time fallback for small retry tails |
| `src/verification_router.py` | Initial / escalation model selection and local-skip classification |
| `src/verification_modes.py` | Explicit verification modes (`local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`) and per-mode policy (Chunk I) |
| `src/verification_profiles.py` | Verification profile classifier (`california_ahj` / `code_standard` / `manufacturer` / `constructability` / `internal_coordination`) and per-profile search budgets (Chunk H) |
| `src/source_grounding.py` | URL normalization and cited-source validation (Chunk H) |
| `src/verification_cache.py` | Claim-keyed verification cache (only grounded results stored), with disk persistence at `~/.spec_critic/verification_cache.json` |
| `src/triage.py` | Optional Haiku-based verification triage (off by default) — classifies findings as `web_required` vs. `local_skip` with hard safety rails (CRITICAL/HIGH and any finding with a code reference are never eligible) |
| `src/verification_config.py` | Backward-compat re-exports from `api_config` |
| `src/batch.py` | Anthropic Message Batches API — submit, poll, retrieve for review and verification; profile-aware tool builder |
| `src/batch_runtime.py` | Bounded polling runtime with progressive backoff and error thresholds |
| `src/extractor.py` | DOCX text extraction with paragraph mapping (parallelized across files); stable `element_id` / `section_id` stamps (Chunk K1) |
| `src/extraction_cache.py` | LRU cache for extraction and exact API token counts (keyed by file mtime + config hash) |
| `src/preprocessor.py` | Deterministic local detectors (Chunk O): LEED, placeholders, template markers, stale code cycles, invalid code cycles, empty sections, duplicate headings, duplicate paragraphs, file naming. Each alert carries a stable `deterministic_rule` id |
| `src/tokenizer.py` | Token counting (cl100k_base + Anthropic count_tokens), model-aware fallback safety multiplier (Chunk E), per-call limits, cross-check budget |
| `src/edit_locator.py` | Id-anchored fast path (Chunk K4) + legacy exact / normalized / section-anchored / fuzzy fallback (with length-ratio + quick_ratio prefilters) |
| `src/edit_candidates.py` | Edit safety categories (AUTO_SAFE / AUTO_WITH_CAUTION / MANUAL_REVIEW / REPORT_ONLY) |
| `src/spec_editor.py` | Surgical DOCX edits + annotation/change-log mode; offset revalidation immediately before every mutation (Chunk F) |
| `src/apply_edits.py` | Locate → action build → apply (or annotate) workflow; locator-method telemetry (Chunk K5) |
| `src/report_exporter.py` | Word document report generation with trust-model labels (Chunk N) |
| `src/report_status.py` | Trust-model statuses (`ReportStatus`) and edit-action labels (`EditActionLabel`) + classifiers (Chunk N) |
| `src/resume_state.py` | Durable serialization with content + source-file SHA-256 digests for change detection |
| `src/diagnostics.py` | In-memory diagnostics report — events, phase telemetry (Chunk J), verification modes / profiles (Chunk I), locator methods (Chunk K5), evidence / output / search-budget telemetry |
| `src/code_cycles.py` | California 2025 code cycle definition (`DEFAULT_CYCLE = CALIFORNIA_2025`) |

## Verification Architecture

Verification is not a trivial post-process. Every finding flows through:

### Mode selection (Chunk I)

`verification_modes.select_verification_mode(...)` picks one of four explicit modes before any API call. The decision is deterministic and visible in logs and report metadata:

| Mode | When | Model | Thinking | Search budget | Escalates? |
|---|---|---|---|---|---|
| `local_skip` | keyword classifier or Haiku triage said `local_skip`, or a deterministic preprocessor rule flagged the issue | (none — no API call) | n/a | 0 | no |
| `strict_structured` | GRIPES severity OR non-GRIPES `internal_coordination` profile | Sonnet | off | profile ceiling × 0.5 (floor 1) | no |
| `standard_reasoning` | default for substantive technical claims | Sonnet (defers to `VERIFICATION_MODEL_DEFAULT`) | on | full profile ceiling | yes (via `should_escalate_verification`) |
| `deep_reasoning` | escalation, OR initial pass for CRITICAL `california_ahj` (when Sonnet-default is on) | Opus | on | full profile ceiling | no (terminal) |

The routed mode is stamped on every `VerificationResult` and round-trips through the verification cache and resume state.

### Profile selection (Chunk H)

`verification_profiles.classify_finding_profile(finding)` picks one of five profiles before the search budget is resolved. Profile sets the per-severity ceiling; severity modulates within that ceiling (severity is *subordinate* to profile, per the directive):

| Profile | When | `max_uses` ceiling (CRITICAL → HIGH → MEDIUM → GRIPES) |
|---|---|---|
| `california_ahj` | mentions California / DSA / HCAI / Title 24 / AHJ | 8 / 7 / 5 / 3 |
| `code_standard` | cites a code section or standards body (CBC, NFPA, ASHRAE, IAPMO, …) without California signals | 7 / 7 / 5 / 3 |
| `manufacturer` | mentions a manufacturer / model number / datasheet / submittal | 6 / 5 / 4 / 3 |
| `constructability` | default for substantive technical claims with no clear kind signal | 5 / 5 / 4 / 3 |
| `internal_coordination` | mentions internal contradiction / placeholder / LEED / typo / duplicate paragraph | 2 / 2 / 1 / 1 |

### Pre-pass classification

- **Keyword classifier** marks placeholder / LEED / typo / duplicate / internal-contradiction GRIPES (with no `codeReference`) as `local_skip`. Chunk O extended the keyword set to recognize the deterministic preprocessor rule names (`todo`, `fixme`, `xxx`, `???`, `lorem ipsum`, `duplicate paragraph`, `empty section`, `invalid code cycle`, `template marker`, `inconsistent csi`, `inconsistent filename`) so a finding emitted by a deterministic detector is locally skipped instead of paying for a Sonnet+web_search round-trip. CRITICAL/HIGH severity and any non-empty `codeReference` always override into `web_required`.
- **Persistent cache hits** are resolved before any API call. The cache key is `cycle_label | actionType | codeReference | sha256(claim_summary)`; only `grounded=True` results are cached. Cache round-trips to disk via atomic temp-file + rename. Default is database mode (no automatic expiration); set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` for opt-in TTL pruning.
- **Optional Haiku triage** (`SPEC_CRITIC_HAIKU_TRIAGE=1`) augments the keyword classifier for findings the classifier did not catch. Hard safety contract: any non-empty `codeReference` is never eligible; `CRITICAL` / `HIGH` are never eligible; on API failure or parse error, all affected findings default to `web_required`.

### Source grounding (Chunk H)

The verifier captures four distinct source concepts on every `VerificationResult`:

- `searched_sources` — URLs the `web_search` tool actually retrieved.
- `cited_sources` — URLs the model emitted in its `submit_verification_verdict` payload.
- `accepted_sources` — cited URLs whose normalized form matched a searched URL.
- `rejected_sources` — `[{"url", "reason"}, …]` for cited URLs that did **not** match any searched URL.

`source_grounding.normalize_url` folds `http`/`https`, drops default ports / fragments / tracking params, sorts query params, and trims trailing punctuation so trivial URL differences never reject a real citation. The public `VerificationResult.sources` field is replaced with `accepted_sources` so reports and the verification cache never persist model-invented URLs. If the model emitted citations but **every citation was ungrounded**, `CONFIRMED` / `CORRECTED` is downgraded to `UNVERIFIED` with an explanation suffix. The grounding invariant (no `CONFIRMED` / `CORRECTED` without `grounded=True`) is still enforced afterwards.

### Source quality

A blocked-domain list filters social / AI-assistant / forum / general-encyclopedia sources from `web_search_20260209`. California priority sources are documented in the verifier system prompt rather than encoded as an allow-list (mixing allow + block lists is unsupported by the tool).

### Verdict parsing (Chunk D)

`verifier.parse_verification_response(messages) -> VerificationParseOutcome` is the canonical parser routed through by every verification path — real-time initial, batch initial, batch retry, batch continuation. Order of attempts:

1. Structured `submit_verification_verdict` tool input (searched in reverse order across the message list so the most recent verdict wins on continuations).
2. Strict JSON text fallback over the concatenated text of every message.
3. Conservative unparseable / incomplete classification.

`classify_verification_stop_reason(stop_reason)` centralizes the allowlist (`end_turn` / `tool_use` → COMPLETE, `pause_turn` → PAUSE, everything else → INCOMPLETE) so a `tool_use` stop reason no longer triggers the legacy "incomplete" path.

### Real-time fallback

When a batch retry tail shrinks below `SPEC_CRITIC_REALTIME_FALLBACK_THRESHOLD` (default 5), the remaining items flip to real-time verification rather than waiting another batch cycle.

## Edit Safety

### Finding vs. Edit Proposal (Chunk L)

A *finding* is not the same thing as an *edit proposal*. The structured tool schema includes a `REPORT_ONLY` action so coordination / interpretation findings are no longer forced to manufacture a replacement quote — they emit `REPORT_ONLY` and the edit-shaped slots (`existingText`, `replacementText`, `anchorText`, `insertPosition`) stay null. The edit-application path only considers findings whose `actionType` is `ADD` / `EDIT` / `DELETE` with a valid edit proposal; everything else renders in the report with the `REPORT_ONLY` `EditActionLabel`.

### Auto-edit eligibility

`report_status.classify_edit_action(finding)` is the single source of truth for which findings auto-apply. A proposal earns `AUTO_EDIT_CANDIDATE` only when:

- it has a supportive status (`VERIFIED_SUPPORTED` / `VERIFIED_CONTRADICTED` / `LOCALLY_CLASSIFIED`); *and*
- `edit_confidence >= AUTO_EDIT_CONFIDENCE_FLOOR` (0.7); *and*
- it is not suppressed by cross-check dependency tracking.

Everything else with a proposal becomes `MANUAL_EDIT_CANDIDATE`. Findings without a proposal become `REPORT_ONLY`. Suppressed findings become `SUPPRESSED`. The locator and spec-editor preconditions still gate the actual mutation so a misclassified status cannot cause a wrong-text replacement.

### Locator path (Chunk K4)

When `Finding.evidenceElementId` is set, `edit_locator._id_anchored_match` is the fast path: it looks up the paragraph / table cell / heading by `element_id` and revalidates the recorded `existingText` quote (exact substring first, then normalized) against the live element. A successful id+quote match becomes a `LocatorResult` with `match_method="id"` and AUTO_SAFE safety (AUTO_WITH_CAUTION for table cells so the table-cell precondition revalidation in `spec_editor` still gates the mutation). When the id is set but unusable — id missing from the map, or quote no longer matches the cited element — the locator returns `safety_category=SAFETY_MANUAL_REVIEW` and **does not** fall back to whole-document text matching (a quoted-text match elsewhere in the document is treated as suspect). The fuzzy / text path is reached only when `evidenceElementId is None`.

### Offset safety (Chunk F)

Every mutation revalidates its preconditions immediately before the write. If the expected text shifted to a unique new location, the precondition result carries corrected offsets and the mutation uses them; if the expected text appears multiple times or has gone missing, the edit is dropped to manual review instead of guessing. The same principle applies to table-cell edits. The result is that a sequential edit pass can no longer cause a wrong-span replacement if an earlier edit shifted a later edit's target.

### Safety categories

The locator (`edit_locator.py`) and edit-action builder (`spec_editor.py`) gate auto-application by safety category:

| Category | Behavior |
|---|---|
| AUTO_SAFE | Id-anchored match (body) or exact/normalized match, plain paragraph, single formatting run — applied automatically |
| AUTO_WITH_CAUTION | Id-anchored table-cell match, fuzzy match, or section-anchored match in a still-tractable paragraph — applied when `allow_caution=True` |
| MANUAL_REVIEW | Ambiguous / multi-run / table / header/footer / id-set-but-quote-mismatch — auto-edit suppressed; surfaced for review |
| REPORT_ONLY | Defaulted when no anchor can be located safely — finding shown in report only |

Annotate mode bypasses these gates by writing a yellow-highlighted suggestion paragraph after the anchor, never modifying the original text.

## Cross-Check Dependencies (Chunk M)

Cross-discipline findings carry explicit dependency-tracking fields instead of relying on heuristic file/section overlap to decide whether a coordination claim survives verification:

- `upstreamFindingIds` — review-finding ids the coordination claim depends on. The review pipeline stamps every review finding with a stable `rf-<12hex>` id derived from the dedup key, so the cross-check model can cite review findings by id.
- `independentEvidenceIds` — raw-spec element ids (`p7`, `t0r2`, `s1h0`, …) that independently support the claim regardless of which review findings end up disputed.

`pipeline.classify_cross_check_dependencies` partitions cross-check findings into `(kept, suppressed)` after verification. A finding is dropped only when **every** cited upstream is `DISPUTED` *and* there is no independent spec evidence — otherwise it survives. Findings without cited ids fall back to the legacy `(filename, section)` heuristic (the path is labeled as such in logs so the operator can tell when the heuristic was used).

Dropped findings land on `cross_check_result.suppressed_findings` with `suppression_reason` set so the report can explain the decision rather than silently making the finding disappear. The corresponding row shows up under the `MANUAL_REVIEW_REQUIRED` status in the Trust Model Summary.

## Trust Model / Report Output (Chunk N)

`report_status.py` defines the closed sets the report uses to make uncertainty visible:

| `ReportStatus` | When |
|---|---|
| `VERIFIED_SUPPORTED` | verifier returned `CONFIRMED`, grounded against searched URLs |
| `VERIFIED_CONTRADICTED` | verifier returned `CORRECTED`, grounded against searched URLs |
| `DISPUTED` | verifier emitted explicit DISPUTED verdict or grounding downgraded a `CONFIRMED` / `CORRECTED` to UNVERIFIED |
| `INSUFFICIENT_EVIDENCE` | verifier returned `UNVERIFIED` with no contradictory citation |
| `LOCALLY_CLASSIFIED` | `local_skip` mode resolved the finding (deterministic detector, keyword classifier, or Haiku triage) |
| `NOT_CHECKED` | no verification ran (e.g. user disabled verification) |
| `MANUAL_REVIEW_REQUIRED` | suppressed by cross-check dependency tracking, or precondition / parser failure surfaced for human review |

| `EditActionLabel` | When |
|---|---|
| `AUTO_EDIT_CANDIDATE` | edit proposal + supportive status + `edit_confidence >= 0.7` |
| `MANUAL_EDIT_CANDIDATE` | edit proposal but status / confidence does not clear the auto-apply bar |
| `REPORT_ONLY` | no edit proposal (coordination / interpretation finding) |
| `SUPPRESSED` | `suppression_reason` set (cross-check dependency drop) |

Both labels are *derived* from already-stored `Finding` fields (`verification`, `suppression_reason`, `edit_proposal`) so the verification cache and resume state do not need a new column. The report renders a Trust Model Summary section between the severity table and the alerts that shows the per-status histogram and the per-edit-action breakdown so a reader sees how many findings are actually trustworthy before scrolling to individual findings.

The per-finding rendering uses explicit labels — `Spec evidence:` / `Proposed replacement:` / `Verification rationale:` — instead of the unlabeled "Existing Text / Replace With / explanation" layout, and the collapsible Sources sub-heading distinguishes accepted citations (`Web/code evidence`) from rejected citations (`Unsupported / rejected sources` — URLs the model cited but `web_search` never returned).

## Deterministic Checks (Chunk O)

Simple, repetitive, high-confidence checks run before any LLM call and are clearly labeled as deterministic in the report (`(deterministic check)` suffix on every section header):

| `deterministic_rule` | What it catches |
|---|---|
| `leed_reference` | LEED mentions inappropriate for the project context |
| `placeholder` | unresolved bracketed placeholders like `[SELECT]`, `[VERIFY]`, `TBD` |
| `template_marker` | `TODO:` / `FIXME` / `XXX` / `???` / lorem-ipsum left in the spec (conservative regex so prose like "things to do" does not trigger) |
| `stale_code_cycle` / `stale_asce7` | references to a real published cycle that is not the current one |
| `invalid_code_cycle` | references to year/code combinations that are not a real cycle (e.g. `2018 CBC`, `2020 CMC`) |
| `empty_section` | section headings with no body |
| `duplicate_heading` | the same section heading repeated within a single document |
| `duplicate_paragraph` | substantial paragraphs (≥80 chars) repeated verbatim within a single document (whitespace-collapsed casefolded compare) |
| `inconsistent_filename` | CSI-number / filename mismatches across the project |

Rule ids are exposed as `DETERMINISTIC_RULE_*` constants and the `DETERMINISTIC_RULES` frozenset in `preprocessor.py` so downstream code can branch on the id without keyword-sniffing the human-readable `type` string. Pipeline plumbing (`_PreparedSpecs`, `BatchSubmission`, `CollectedBatchState`, `PipelineResult`) carries every alert list end-to-end — code-cycle, structural, and naming alerts used to be collected and silently dropped before the report saw them. The verification router's local-skip keyword list is also extended with the rule names so a GRIPES finding whose `issue` text mentions one of these is locally skipped instead of paying for a Sonnet+web_search round-trip.

## Prompt Caching & Telemetry (Chunk J)

### Phase-aware cache policy

Prior to Chunk J every call site decided independently whether to attach `cache_control` breakpoints. Synthesis (425-token system prompt, called once per run) and triage (375-token system prompt) paid the cache-write overhead even though both prompts are below the Anthropic cache minimum (1024 for Opus/Sonnet, 2048 for Haiku) and could never produce a hit.

`api_config.cache_policy_for(phase)` is now the single source of truth. The defaults:

| Phase | System | Tools | Rationale |
|---|---|---|---|
| `PHASE_REVIEW` / `PHASE_BATCH_REVIEW` | cached | cached | same prefix reused across every spec in a multi-file selection |
| `PHASE_CROSS_CHECK` | cached | cached | chunked path can fire 5+ calls with the same system prompt |
| `PHASE_VERIFICATION` (+ retry / continuation) | cached | cached | system prompt and tool list are large and reused across waves |
| `PHASE_SYNTHESIS` | uncached | uncached | one-off per run, below the cache minimum |
| `PHASE_TRIAGE` | uncached | uncached | below Haiku's 2048-token cache minimum |

`SPEC_CRITIC_CACHE_DISABLE` is a comma-separated list of phase names that lets operators opt individual phases out without flipping the global `SPEC_CRITIC_PROMPT_CACHE` switch (useful when a particular phase is misbehaving).

### Telemetry

`diagnostics.DiagnosticsReport.record_api_call(...)` is the standardized helper for recording a single Anthropic call. Every call site passes `phase`, `model`, `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `web_search_requests`, `max_output_tokens`, `stop_reason`, `mode` (real-time / batch), and `retry_status`. `DiagnosticsReport.summary()` produces:

- `phase_telemetry` — per-phase rollup of calls / tokens / cache hits / retries / continuations / truncations / model mix
- `cost_summary` — cross-phase totals and a global `cache_hit_ratio`
- `verification_modes` / `verification_profiles` — per-mode and per-profile counts (Chunk I)
- `locator_methods` — counter of how often each locator path (id / exact / normalized / section_anchored / fuzzy) actually landed a match, useful for measuring the Chunk K rollout

`DiagnosticsReport.to_text()` renders a `Phase Telemetry:` block with one compact line per phase plus a `Cache Hit Ratio:` line so an operator can spot whether caching is paying off without poking at the JSON dump.

## Token Counting & Output Budgets (Chunk E)

Exact Anthropic token counting (`anthropic.messages.count_tokens`) is the authoritative input guard when available, called with the **selected model** rather than a hardcoded one. The local `cl100k_base` estimate from `tiktoken` is used:

- as a fast pre-API estimate,
- as the fallback when `count_tokens_via_api` returns `None`,
- as a UI hint for the token gauge.

When the exact count is unavailable, `tokenizer.safe_local_estimate(local_tokens, *, model)` pads the local count by a model-specific safety multiplier (Opus / Sonnet 1.10×, Haiku 1.15×, unknown 1.20×) before applying the per-call limit check. The pipeline preflight (`_prepare_specs`) raises `ValueError` when the exact count exceeds `RECOMMENDED_MAX` — previously it only logged a warning while the cl100k count was the only hard gate.

Output caps are centralized in `api_config._PHASE_OUTPUT_BUDGET` and clamped to the selected model's ceiling via `phase_output_cap(phase, *, model)`:

| Phase | Cap | Notes |
|---|---|---|
| `PHASE_REVIEW` / `PHASE_BATCH_REVIEW` | 128k | real-time and batch share the baseline so findings cannot diverge between modes |
| Extended batch review | 300k | batch-only; gated by the `output-300k-2026-03-24` beta header for inputs ≥200k tokens |
| `PHASE_CROSS_CHECK` | 96k | cross-check needs more than verify |
| `PHASE_VERIFICATION` (+ retry / continuation) | 16k | verdicts are 1–2 sentences |
| `PHASE_SYNTHESIS` | 32k | cross-discipline synthesis on Haiku |
| `PHASE_TRIAGE` | 8k | triage classifications |

Unknown phases fall back to the verification cap (the most conservative value) so a phase that forgets to register loses headroom instead of accidentally inheriting the 128k review cap.

## Prompt Serialization (Chunk G)

`prompt_serialization.py` is the single source of truth for safely embedding untrusted content (spec bodies, project context, finding fields, filenames) in pseudo-XML wrappers. Previously each prompt builder had its own escape helper and several wrappers (spec body, project context, triage findings) were entirely unescaped, so a filename like `weird".docx` could silently truncate the opening tag or document content containing a `</spec>` substring could break the prompt boundary.

Public API:

- `escape_text(value)` — escape `&`, `<`, `>` for element content.
- `escape_attr(value)` — escape `&`, `<`, `>`, `"`, `'` for attribute values.
- `wrap_data_block(tag, content, *, attrs=None)` / `wrap_document_block(tag, content, *, attrs=None)` — single-line / multi-line wrappers with both halves escaped.
- `render_blocks(iterable)` — `\n`-join that drops empties.

Chunk K2 adds `render_spec_with_ids(content, paragraph_map, *, filename)` which emits one `<para id="p7" section="1.01 SUMMARY">…</para>` (or `<row id="t0r2" …>` / `<heading id="p0">`) per `ParagraphMapping` so the model can cite `evidenceElementId` alongside the exact quote. The id rendering only touches the *body* of the user message — the cached system-prompt prefix and the instruction text up to the id-hint line are unchanged byte-for-byte, so prompt-cache breakpoints continue to land where they did. `element_ids_enabled()` (`SPEC_CRITIC_ELEMENT_IDS=0`) reverts to the legacy `<spec>`-only rendering.

## Code Cycles

Configured for the **California 2025 code cycle**. The cycle definition lives in `src/code_cycles.py` (`DEFAULT_CYCLE = CALIFORNIA_2025`) and a new cycle is added by appending a `CodeCycle` instance to `AVAILABLE_CYCLES`. The cycle label is part of the verification cache key, so a future cycle bump naturally invalidates persistent cache entries from the prior cycle. The codebase no longer carries the 2022-cycle mapping that earlier releases shipped with — do not reintroduce it.

The deterministic preprocessor recognizes two distinct kinds of bad code-cycle citation:

- **Stale-cycle alerts** (`deterministic_rule="stale_code_cycle"` / `"stale_asce7"`) flag references to a *real published* cycle that is not the current one (e.g. `2019 CBC` when the configured cycle is California 2025).
- **Invalid-cycle alerts** (`deterministic_rule="invalid_code_cycle"`) flag references to years that are not a real cycle at all (e.g. `2018 CBC`, `2020 CMC`), which usually indicate a typo or hallucinated code year.

The two sets are disjoint by construction — a year is either a real cycle or it isn't.

## Requirements

- Python 3.11+
- Anthropic API key (`ANTHROPIC_API_KEY`)
- Dependencies (see `requirements.txt`): `anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic`

## Testing

The suite is hermetic by default — no Anthropic API key, no network. `tests/conftest.py` injects a placeholder `ANTHROPIC_API_KEY` so module imports work; tests that need real network access opt in via `@pytest.mark.network`. Run everything with `pytest -q` from the project root. GUI-dependent tests skip automatically when `tkinter` is not installed.

### Smoke checks

For a fast confidence pass before committing, run:

```
pytest -q -m smoke
```

This runs the Chunk A import / compile sanity checks (`tests/test_chunk_a_smoke.py`) and finishes in a few seconds.

### Test markers (declared in `pyproject.toml`)

| Marker | Purpose |
|---|---|
| `smoke` | Fast import / compile sanity checks (`tests/test_chunk_a_smoke.py`) |
| `fixtures` | Round-trips the fake Anthropic response builders through the production parsers (`tests/test_chunk_a_fixtures.py`) |
| `request_shape` | Captures the request kwargs production code passes to the Anthropic SDK so later refactors fail at the request layer, not at the API (`tests/test_request_payload_shape.py`) |
| `parser_unification` | Chunk D verification parser regression tests |
| `token_budget` | Chunk E token counting and output budget regression tests |
| `prompt_serialization` | Chunk G prompt boundary / serialization hardening tests |
| `source_grounding` | Chunk H source-grounding and verification-profile tests |
| `verification_modes` | Chunk I verification-mode and model-routing tests |
| `slow` | Reserved for tests noticeably slower than the rest of the suite |
| `network` | Reserved for tests that hit a real Anthropic endpoint; skipped unless `ANTHROPIC_API_KEY` is set to a non-placeholder value |

Run a single category with e.g. `pytest -m smoke` or `pytest -m request_shape`. The fake Anthropic response builders live in `tests/fixtures/fake_anthropic.py` and cover the canonical response cases: structured review tool call, structured verification verdict tool call (including `stop_reason="tool_use"`), JSON-text fallback, and `max_tokens` incomplete (each builder accepts `dict_shape=True` to also emit plain-dict responses for the batch retrieval path). Small in-memory DOCX builders are in `tests/fixtures/docx_fixtures.py` for paragraph / table / real-world-section edit-safety tests.

### Request-shape testing

`tests/test_request_payload_shape.py` exposes a `FakeClient` that captures the kwargs production code passes to `messages.stream`, `messages.batches.create`, and `beta.messages.batches.create`. The `fake_client` fixture monkeypatches `_get_client` in `reviewer` / `batch` / `verifier` / `cross_checker` so any request-building path can be exercised without a real API key. Use this when refactoring request construction — a payload-shape test fails immediately if a future change drops a required tool or sends an unsupported `thinking` config.

## Feature Flags

All flags read from environment variables; the listed default applies when the variable is unset.

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_PROMPT_CACHE` | `1` (on) | `0` disables prompt caching globally |
| `SPEC_CRITIC_PROMPT_CACHE_TTL` | `1h` | `5m` switches to ephemeral 5-minute cache (lower write cost, narrower payback window) |
| `SPEC_CRITIC_CACHE_DISABLE` | (empty) | Comma-separated phase names to opt out of caching individually (e.g. `verification,cross_check`) — leaves other phases caching normally |
| `SPEC_CRITIC_STRUCTURED_OUTPUTS` | `1` (on) | `0` falls back to tagged-JSON text parsing (still always-on as a safety net even with `1`) |
| `SPEC_CRITIC_ELEMENT_IDS` | `1` (on) | `0` reverts spec rendering to the legacy plain-body `<spec>` wrapper (no id-tagged `<para>` / `<row>` / `<heading>` elements) |
| `SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT` | `1` (on) | `0` skips Anthropic count_tokens before submission |
| `SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT` | `1` (on) | `0` reverts to Opus-everywhere for verification |
| `SPEC_CRITIC_LOCAL_VERIFICATION_SKIP` | `1` (on) | `0` web-verifies all findings (disables local-skip mode) |
| `SPEC_CRITIC_PARALLEL_CROSS_CHECK` | `1` (on) | `0` runs cross-check after verification instead of in parallel |
| `SPEC_CRITIC_REALTIME_FALLBACK_THRESHOLD` | `5` | Items remaining at which a small retry tail flips to real-time |
| `SPEC_CRITIC_VERIFICATION_MAX_USES` | `5` | Default web_search `max_uses` (used when neither severity tiering nor profile ceiling applies) |
| `SPEC_CRITIC_HAIKU_TRIAGE` | `0` (off) | `1` enables Haiku verification triage augmenting the keyword classifier |
| `SPEC_CRITIC_REVIEW_MODEL` | `claude-opus-4-7` | Override review model |
| `SPEC_CRITIC_CROSS_CHECK_MODEL` | `claude-opus-4-7` | Override cross-check model |
| `SPEC_CRITIC_SYNTHESIS_MODEL` | `claude-haiku-4-5` | Override cross-discipline synthesis model |
| `SPEC_CRITIC_TRIAGE_MODEL` | `claude-haiku-4-5` | Override Haiku verification triage model |
| `SPEC_CRITIC_VERIFICATION_MODEL` | (auto from sonnet flag) | Override verifier model |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | `claude-opus-4-7` | Override escalation / deep-reasoning model |
| `SPEC_CRITIC_VERIFICATION_CACHE_PERSIST` | `1` (on) | `0` disables on-disk verification cache (database mode) |
| `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` | `0` | Positive integer enables age-based cache pruning (0 = no expiry) |
| `SPEC_CRITIC_CACHE_PATH` | `~/.spec_critic/verification_cache.json` | Override cache path |
| `SPEC_CRITIC_EXTRACTION_CACHE` | `1` (on) | `0` disables file-extraction cache |

## Token Limits

| Constant | Value | Purpose |
|---|---|---|
| `MAX_CONTEXT_TOKENS` | 1,000,000 | Hard ceiling for combined input+output |
| `MAX_OUTPUT_TOKENS_OPUS` | 128,000 | Per-call Opus output cap |
| `MAX_OUTPUT_TOKENS_SONNET` | 64,000 | Per-call Sonnet output cap |
| `MAX_OUTPUT_TOKENS_HAIKU` | 64,000 | Per-call Haiku output cap |
| `REVIEW_OUTPUT_CAP` | 128,000 | Unified per-spec review cap (real-time and batch) |
| `REVIEW_OUTPUT_CAP_BATCH_EXTENDED` | 300,000 | Batch-only; requires the 300k beta header |
| `CROSS_CHECK_OUTPUT_CAP` | 96,000 | Cross-check needs more than verify |
| `CROSS_CHECK_OUTPUT_BUDGET` | 128,000 | Reserved for cross-check output during budgeting |
| `VERIFICATION_OUTPUT_CAP` | 16,000 | Verdicts are 1–2 sentences (tightened from 32k) |
| `SYNTHESIS_OUTPUT_CAP` | 32,000 | Cross-discipline synthesis on Haiku |
| `HAIKU_TRIAGE_OUTPUT_CAP` | 8,000 | Triage classifications |
| `RECOMMENDED_MAX` | 500,000 | Per-spec input warning threshold |
| `CROSS_CHECK_OVERHEAD` | 50,000 | Reserved for system + user-prompt overhead in cross-check |
| `CROSS_CHECK_RECOMMENDED_MAX` | 822,000 | Combined input ceiling for single-pass cross-check |

## Resume Phases

`resume_state.py` exposes the canonical phase set:

- `PHASE_REVIEW_POLL`
- `PHASE_REVIEW_COLLECT`
- `PHASE_VERIFICATION_POLL`
- `PHASE_VERIFICATION_WAVE_POLL`
- `PHASE_CROSS_CHECK`
- `PHASE_CROSS_CHECK_VERIFICATION_POLL`
- `PHASE_CROSS_CHECK_VERIFICATION_WAVE_POLL`
- `PHASE_FINALIZE`

`build_resume_state(...)` and `deserialize_resume_state(payload)` round-trip a run-state payload. `serialize_extracted_spec` records SHA-256 digests of both the extracted content and the underlying source file; `deserialize_extracted_spec` warns when either differs at resume time. The active review mode is also persisted so a resumed run uses the same prompt path it started with.

## Changelog

### Unreleased — Non-GUI Refactor (Chunks A–P)

The non-GUI refactor staged the work into independent chunks rather than a single rewrite. The codebase now reflects all chunks A through O; chunk P (this entry) cleans up documentation. Summarized work:

- **Chunk A — test harness.** Hermetic test suite, `smoke` / `fixtures` / `request_shape` markers, fake Anthropic response builders, in-memory DOCX builders.
- **Chunk B — model-aware thinking policy.** `model_capabilities(...)` whitelist (Opus 4.6/4.7, Sonnet 4.6, Haiku 4.5); unknown models degrade safely. `apply_thinking_config(...)` adds the `thinking` key only when both the model and the phase opt in — Haiku phases (synthesis, triage) no longer carry an unsupported `thinking` payload.
- **Chunk C — verification tool payload consistency.** Real-time / batch / retry / continuation all route through one tool builder that includes `submit_verification_verdict` whenever structured outputs are enabled. Prompts no longer claim access to tools the request doesn't actually carry.
- **Chunk D — canonical verification parser.** `parse_verification_response` is the single parser used by every verification path. Stop-reason allowlist (`end_turn`, `tool_use`) recognizes structured tool-use responses; legacy text-only batch retrieval removed.
- **Chunk E — token counting and output budgets.** Exact Anthropic token counting uses the selected model; preflight raises on `RECOMMENDED_MAX` overflow instead of just logging. Phase output budgets live in one registry (`_PHASE_OUTPUT_BUDGET`) clamped to each model's ceiling.
- **Chunk F — edit precondition offset safety.** Every mutation revalidates its preconditions immediately before the write and uses corrected offsets when the expected text shifted to a unique new location. Duplicates / missing text never auto-apply.
- **Chunk G — prompt serialization hardening.** Central `prompt_serialization.py` escape / wrap helpers; attribute values are properly escaped (closing the `weird".docx` filename injection hole) and document bodies are wrapped consistently.
- **Chunk H — verification profiles and source grounding.** Five profiles (`california_ahj` / `code_standard` / `manufacturer` / `constructability` / `internal_coordination`) set per-severity search budgets; severity modulates within the profile ceiling. Verifier captures `searched_sources` / `cited_sources` / `accepted_sources` / `rejected_sources` separately; ungrounded citations downgrade `CONFIRMED` / `CORRECTED` to `UNVERIFIED`.
- **Chunk I — verification modes.** Four explicit modes (`local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`). Simple verification no longer defaults to the deep-reasoning path. Mode is stamped on every result and round-trips through cache and resume state.
- **Chunk J — phase-aware prompt caching and telemetry.** `cache_policy_for(phase)` decides per-phase whether to attach `cache_control` breakpoints; synthesis and triage no longer pay cache-write overhead for prompts below Anthropic's cache minimum. `DiagnosticsReport.record_api_call(...)` produces per-phase telemetry and a cross-phase `cache_hit_ratio`.
- **Chunk K — stable element IDs.** Extraction stamps every paragraph / table cell / heading with a stable `element_id`. Prompts render specs with `<para id=...>` blocks. Schema adds `evidenceElementId`. Locator prefers id-anchored matches and does not fall back to whole-document text matching when the id is set but unusable.
- **Chunk L — finding vs. edit proposal split.** `actionType` includes `REPORT_ONLY` so coordination findings no longer manufacture replacement text. Auto-edit eligibility is gated by a small set of explicit rules.
- **Chunk M — cross-check dependency tracking.** Cross-check findings cite `upstreamFindingIds` and `independentEvidenceIds`. Suppression after verification is deterministic instead of heuristic; dropped findings land on `suppressed_findings` with `suppression_reason` set.
- **Chunk N — trust-model report output.** Seven-status closed set (`ReportStatus`) and four-label closed set (`EditActionLabel`) make uncertainty visible. Report renders a Trust Model Summary section between the severity table and alerts.
- **Chunk O — deterministic checks expansion.** New detectors for unresolved template markers, invalid code cycles, and duplicate paragraphs. Every alert carries a stable `deterministic_rule` id. Pipeline plumbing carries every alert list end-to-end (code-cycle / structural / naming alerts no longer get silently dropped before the report).
- **Chunk P — documentation cleanup.** This changelog entry plus README updates that remove stale overclaims (no more "parse failure eliminated"), document the fallback strategy explicitly, and document the new mode / profile / status / telemetry surfaces.

### v2.11.0
- Default review/cross-check model upgraded to Claude Opus 4.7; escalation model also Opus 4.7
- Persistent verification cache: claim-keyed verdicts round-trip to `~/.spec_critic/verification_cache.json` via atomic temp-file + rename; database mode by default with optional TTL pruning (`SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS`)
- Optional Haiku 4.5 verification triage (`SPEC_CRITIC_HAIKU_TRIAGE=1`) augments the keyword classifier with hard safety contract (CRITICAL/HIGH and findings with a code reference are never eligible; API failure → `web_required`)
- Cross-discipline synthesis model exposed (Haiku 4.5 default; `SPEC_CRITIC_SYNTHESIS_MODEL` override)
- Severity-tiered web-search budgets standardized: CRITICAL/HIGH=7, MEDIUM=5, GRIPES=3
- Verification output cap tightened to 16k (verdicts are 1–2 sentences); `SYNTHESIS_OUTPUT_CAP` and `HAIKU_TRIAGE_OUTPUT_CAP` added
- `VerificationResult.sources` reports only cited URLs; total retrieved URLs preserved on `successful_source_count`
- Cross-check chunking grouping refined (Div 21 / 22 / 23 / Controls / 25 + 01)

### v2.10.0
- Structured outputs: review, cross-check, and verification now use Anthropic tool-use schemas instead of `<findings_json>` regex parsing
- Sonnet 4.6 is the default verifier (Opus escalates for CRITICAL/HIGH UNVERIFIED)
- Local-skip classification, parallel cross-check, real-time retry tail (default threshold 5), and Anthropic token-count preflight are now on by default
- Annotation / change-log edit mode (yellow-highlighted suggestion paragraphs; never mutates source text)
- `FindingGroup` / `FindingOccurrence` types formalize the display-dedup vs. per-file edit split
- Resume state stores content and source-file SHA-256 digests; warns on disk-side changes
- Fuzzy matcher gains length-ratio + `quick_ratio` prefilters for large documents
- Token gauge labels approximate vs. exact (API) counts; runs the API count async after the live estimate
- DiagnosticsWindow renders cache, verification evidence, edit skip reasons, ambiguous locators, output telemetry, and search-budget telemetry inline
- GUI no longer keyword-sniffs log messages; pipeline passes explicit `phase=` to log/progress callbacks
- Stale `CODE_EXECUTION_TOOL` export removed from `verification_config.py`

### v2.8.3
- Verbose Word report now includes verification source URLs for each finding (rendered inline with blue text after the verdict/explanation/correction block)

### v2.8.0
- Batch-only enforcement for verification (real-time verification removed at the time; later restored as a small-tail fallback in v2.10.0)
- Multi-wave verification batch with retry/continuation support
- Bounded polling runtime with configurable timeouts and error thresholds
- Durable resume state serialization across all pipeline phases
