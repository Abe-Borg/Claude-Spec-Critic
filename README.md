# Spec Critic

**v2.11.0** — AI-assisted M&P specification review for California K-12 DSA projects.

Spec Critic reviews mechanical and plumbing CSI-format `.docx` specifications against California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) and the NFPA / ASHRAE / IAPMO / UL editions adopted for the current cycle, using Claude. It produces structured findings with severity classifications, confidence scores, web-search-backed verification verdicts, optional cross-spec coordination analysis, and either inline edits or yellow-highlighted suggestion annotations on a copy of each spec.

Configured for the **California 2025 code cycle** by default (`src/core/code_cycles.py`). The reviewer and verifier prompts pin the adopted NFPA / ASHRAE / IAPMO / UL editions for the cycle so the model verifies claims against the editions California actually adopted; the exported report's methodology note enumerates the pinned editions.

## Design Emphasis

- **Evidence-grounded verification.** `CONFIRMED` / `CORRECTED` verdicts require at least one cited URL that the `web_search` tool actually retrieved.
- **Cost-aware defaults.** Sonnet-default verifier with Opus escalation, optional Haiku triage, severity-tiered + profile-aware search budgets, persistent on-disk claim cache.
- **Robust batch processing.** Durable resume across every pipeline phase with content + source-file SHA-256 digests.
- **Safe Word output.** Id-anchored matching when the model cites a paragraph id; surgical edits gated by safety categories; offset revalidation runs immediately before every mutation. Annotate mode is non-destructive.
- **Trust-model report output.** Every finding renders one of nine `ReportStatus` labels (including `VERIFICATION_FAILED` for transient operational errors and `VERIFIED_CONTESTED` when the initial and escalated verifiers disagreed on a grounded verdict) and one of four `EditActionLabel` values so the report makes uncertainty visible.

## Pipeline at a Glance

1. **Text Extraction** — `.docx` paragraphs, tables, headers/footers. Cached by file hash. Each element gets a stable `element_id` (`p7`, `t0r2`, `s1h0`, …).
2. **Local Pre-Screening** — Deterministic detectors run before any API call: LEED, placeholders, template markers, stale/invalid code cycles, empty sections, duplicate headings/paragraphs, inconsistent file naming.
3. **Per-Spec Review** — Each spec sent to Claude Opus 4.7 via the `submit_review_findings` tool. Tagged-JSON text parser as fallback.
4. **Deduplication** — Identical findings consolidated across specs; per-file occurrences tracked separately for multi-file edits.
5. **Cross-Spec Coordination** *(optional)* — Chunked by CSI division (21 / 22 / 23 / Controls / 25 + 01) on large projects. Runs in parallel with verification.
6. **Verification** — Findings routed into one of four modes (`local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`). Sonnet 4.6 default; CRITICAL/HIGH `UNVERIFIED` escalates to Opus 4.7. Persistent on-disk cache.
7. **Edit Application** *(optional)* — **Edit mode** applies surgical edits to a copy. **Annotate mode** inserts yellow-highlighted suggestions without mutating the original.

## Auto-Apply Quality Guarantees

Edit mode applies surgical edits to a copy of each spec. The auto-apply
pipeline enforces the following quality guarantees so the output
document reads consistently with the source:

- **Replacement text style matching.** Before each edit is applied, the
  source document is profiled for its typographic conventions (curly vs
  straight quotes, em-dash vs hyphen, ASCII vs Unicode apostrophe, NBSP
  in measurements). The model's `replacement_text` is rewritten to
  match the profile, so an edit landing in a curly-quote document keeps
  curly quotes (and vice versa). Counter:
  `DiagnosticsReport.replacement_text_normalized_count`. Kill switch:
  `SPEC_CRITIC_NORMALIZE_REPLACEMENT_STYLE=0`.
- **Punctuation boundary preservation.** When the model's `existingText`
  ends with `.`, `,`, `;`, or `:` and the corresponding
  `replacement_text` does not (or vice versa), the applied edit silently
  drops or doubles the punctuation. The fix is a deterministic pass
  that adds back the original trailing punctuation when the next live
  character is whitespace / end-of-paragraph, and strips a doubled
  trailing mark when the replacement adds one already present in the
  live paragraph. Counter:
  `DiagnosticsReport.punctuation_boundary_fixed_count`. Kill switch:
  `SPEC_CRITIC_PUNCTUATION_BOUNDARY_FIX=0`.
- **Whole-paragraph DELETE inside table cells.** A DELETE that covers
  the entire matched paragraph inside a table cell now removes the
  `<w:p>` element (when the cell has additional paragraphs) instead of
  clearing its text and leaving a blank line in the cell. When the
  paragraph is the cell's only one, its text is cleared in place so
  Word's "every cell needs at least one paragraph" rule still holds.
- **ADD-inserted paragraphs do not join the anchor's list.** When an
  ADD action's anchor paragraph is part of a numbered/bulleted list,
  the inherited paragraph properties are scrubbed before the new
  paragraph is built: `<w:numPr>`, `<w:outlineLvl>`, and `<w:pBdr>`
  are always stripped; `<w:ind>` is stripped only when the inserted
  text itself does not read as list-shaped (no `A.` / `1.` / `•` /
  `–` / `-` prefix). `<w:pStyle>`, `<w:jc>`, `<w:spacing>`, and the
  pPr `<w:rPr>` are preserved so the new paragraph still inherits
  font, size, and alignment from its anchor. Kill switch:
  `SPEC_CRITIC_ADD_INHERITS_LIST_NUMBERING=1` reverts to the legacy
  verbatim-deepcopy behavior.
- **ADD without explicit insertPosition is refused.** ADD findings
  whose `insertPosition` is missing or invalid ("before"/"after" are
  the only acceptable values) are demoted to REPORT_ONLY at parse
  time. The auto-apply layer also refuses defensively when a legacy
  resume payload or test fixture sneaks an ADD without a usable
  position past parsing. The buggy heuristic that previously guessed
  position by comparing normalized text but slicing raw bytes — which
  produced inserted paragraphs containing chopped fragments of the
  anchor — has been removed. Counter:
  `DiagnosticsReport.add_demoted_missing_position_count`.
- **Smarter paragraph splitting for inserted content.** ADD
  replacement text is split into paragraphs as follows: double-newline
  (`\n\s*\n+`) separators always produce paragraph breaks; chunks
  whose every non-empty line starts with a list prefix (`A.` / `1.` /
  `•` / `–` / `-`) are split into one paragraph per item; otherwise
  single-newline-separated lines are treated as soft breaks inside one
  paragraph and collapsed to single-space separators. Word renders the
  result correctly instead of leaving embedded line breaks visible.
- **Span-aware formatting-loss detection.** The extractor records a
  per-run `(start_offset, end_offset, format_signature)` map on every
  body paragraph (`ParagraphMapping.run_format_map`, in stripped-text
  coordinates). The locator's downgrade pass walks that map to decide
  whether a partial replacement actually crosses runs with distinct
  formatting — an EDIT that lands entirely inside one uniformly-
  formatted region of an otherwise richly-formatted paragraph stays
  `AUTO_SAFE`, while an EDIT that crosses bold/italic/font boundaries
  downgrades to `AUTO_WITH_CAUTION`. Whole-paragraph EDITs on a
  multi-format paragraph still route to `MANUAL_REVIEW` because the
  full replacement would erase every inline emphasis the paragraph
  carried. Legacy resume-state payloads without a per-run map fall
  back to the coarser paragraph-level check, so the new behavior is
  opt-in by extraction.
- **Known-pattern formatting restoration (opt-in).** When a partial
  EDIT crosses runs with distinct character formatting,
  `_replace_in_paragraph` collapses the affected runs into the first
  run's formatting and silently drops bold/italic markup on tokens
  inside the replacement span. After the mutation, the auto-apply
  pipeline can scan the new replacement text for tokens matching a
  small registry of recognized standards / code references
  (`NFPA 13`, `ASCE 7-22`, `CBC 2025`, `Section 23 21 13`, …) and
  re-apply bold formatting to each match by splitting the containing
  run. The feature is **default off** because a wrong match could
  bold something that shouldn't be bold; flip
  `SPEC_CRITIC_RESTORE_KNOWN_FORMATTING=1` once your workflow has
  validated the registry. Counter:
  `DiagnosticsReport.known_pattern_formatting_restored_count`. The
  registry lives in `src/editing/replacement_style.py:KNOWN_BOLD_PATTERNS`
  — add new entries when a real workflow proves the new pattern is
  unambiguous in spec documents.
- **Conflict resolver surfaces lost narrower-edit intent.** When two
  edits in the same paragraph have strict containment (broader fully
  contains narrower, spans not identical), the broader edit still
  wins — but the conflict resolver now checks whether the narrower
  edit's correction is preserved in the broader's `replacement_text`
  (whitespace-normalized, case-insensitive substring). When the
  narrower's correction is preserved, the skipped outcome's detail
  reads "intent preserved by broader edit's replacement" and the new
  `EditOutcome.contained_edit_lost_intent` flag stays False. When the
  narrower's correction is NOT preserved (a GRIPES typo nested inside
  a MEDIUM rewrite that picks different text), the broader still
  applies (preserving user agency), but the flag is set so the report
  surfaces the loss and the diagnostics counter
  `DiagnosticsReport.contained_edits_lost_intent_count` aggregates the
  run-wide frequency. Identical-span duplicates still resolve via the
  severity / confidence tie-break with no change.
- **Per-file edit originals survive case/whitespace-only dedup
  collisions.** `_deduplicate_findings` keys on a digest of normalized
  (lowercase + whitespace-stripped) issue / existing / replacement
  text, so two findings whose `existingText` differs only in case or
  trailing whitespace collapse to one representative. The merged
  representative's `occurrence_originals` lists every group member as
  the original `Finding` object, which still carries its
  pre-normalization text. Edit execution looks up each affected file's
  original by `fileName` and uses that file's actual `existingText` for
  locator matching — so the case-only collision does not break either
  file's edit. This invariant is now locked in by regression tests in
  `tests/test_chunk_8_dedup_edit_identity.py`.
- **Cross-paragraph multi-window matches route to manual review.**
  All cross-paragraph window matches carry the same flat 0.88
  confidence, so the previous behavior — `max(filtered_spans, ...)`
  picking the first window by insertion order when multiple windows
  matched identically — was a coin flip on which paragraph actually
  got edited. The locator now sets
  `LocatorResult.cross_paragraph_ambiguous=True` on the multi-window
  case, sets `safety_category=SAFETY_MANUAL_REVIEW` explicitly, and
  emits a warning that names the multi-window cause. The
  single-window cross-paragraph match (one valid window of N
  paragraphs) keeps its previous behavior (`status="matched"`,
  AUTO_WITH_CAUTION). Counter:
  `DiagnosticsReport.cross_paragraph_ambiguity_routed_to_manual_count`.
- **Verifier correction is sanity-checked before being used as
  replacement text.** When the verifier returns `CORRECTED` with a
  non-empty `verification.correction`, the locator previously used
  the correction string verbatim as the applied edit's replacement.
  The verifier's prompt is optimized for explanation — corrections
  often carry parenthetical citations (`(per CBC § 1613.1)`), URLs,
  paragraph-length expansions, or temporal qualifiers (`current`,
  `latest`, `as of <year>`) that don't belong in spec body text.
  `replacement_style.correction_looks_replaceable(correction,
  original_replacement)` now gates the swap: when it returns False,
  the locator falls back to the model's original `replacement_text`
  for the applied edit and sets
  `LocatorResult.correction_rejected_as_replacement=True`. The
  verifier's correction stays on `Finding.verification.correction`
  for the report (so the user still sees the verifier's
  explanation) — only the *applied* edit text changes. The same
  sanity check runs in the candidate UI so the preview matches what
  the apply path will actually land. Counter:
  `DiagnosticsReport.verifier_correction_rejected_as_replacement_count`.
  Kill switch: `SPEC_CRITIC_USE_VERIFIER_CORRECTION_AS_REPLACEMENT=1`
  reverts to the legacy verbatim path. Unlike the other Phase-1 flags
  this one defaults *off* (the new sanity-checked behavior); set it
  to an enable token (`1`/`true`/`yes`/`on`) to restore the legacy
  verbatim path.

Counters render under the "AUTO-APPLY QUALITY" section of the
diagnostics report; the section is hidden entirely when no quality
guard fired.

## Processing Mode

All reviews submit via the Message Batches API — queued at 50% cost savings, typical turnaround ~45 min – 2 hrs (24 hrs max). The 300k extended-output path is batch-only (`output-300k-2026-03-24` beta header) and triggers only for inputs ≥200k tokens.

## Model Stack

Defaults (all overridable via env var; see `api_config.py`):

- Review: Claude Opus 4.7
- Cross-check: Claude Sonnet 4.6
- Verification (initial): Claude Sonnet 4.6
- Verification (escalation / deep-reasoning): Claude Opus 4.7
- Synthesis / Triage: Claude Haiku 4.5

Unknown model ids degrade to safe defaults via `api_config.model_capabilities(...)` — a misconfigured `SPEC_CRITIC_*_MODEL` env var produces a smaller request rather than an API rejection.

## Requirements

- Python 3.11+
- Anthropic API key (`ANTHROPIC_API_KEY`)
- See `requirements.txt`: `anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic`

## Testing

Test suite is hermetic by default — no API key, no network. `tests/conftest.py` injects a placeholder `ANTHROPIC_API_KEY`. GUI-dependent tests skip when `tkinter` is unavailable.

```
pytest -q              # full hermetic suite
```

Test markers: `token_budget`, `prompt_serialization`, `network`. Fake Anthropic response builders live in `tests/fixtures/fake_anthropic.py`; in-memory DOCX builders in `tests/fixtures/docx_fixtures.py`.

## Further Reading

- **`CLAUDE.md`** — Engineering reference: source layout, module-level invariants, verification routing tables, feature flag table, test conventions.

## Escalation Disagreement Surfacing

When the initial Sonnet verifier and the escalated Opus verifier reach
different grounded verdicts on the same finding (both with accepted
external citations), the finding renders as
`VERIFIED_CONTESTED` (⚡, purple) in the report rather than as
`VERIFIED_SUPPORTED` (✓, green) or `VERIFIED_CONTRADICTED` (✎, amber).
The disagreement itself is the quality signal: two capable models
reading real sources reached different conclusions, and the right
default action is human review rather than auto-applying either side's
edit. `VERIFIED_CONTESTED` is intentionally not in the
auto-edit-supportive status set, so contested findings always route to
`MANUAL_EDIT_CANDIDATE`.

The per-finding evidence panel surfaces both verdicts inline:
- The "Escalation history" line shows the initial → final verdict
  transition with each verifier's model name and a "manual review
  recommended" sentence.
- A dedicated "Initial verifier sources" sub-section lists the
  citations the initial verifier produced, alongside the final
  verifier's citations in the regular "Web/code evidence" sub-section.

The contested telemetry round-trips through the verification cache and
the resume state (no schema bump — runtime telemetry, not verdict
semantics), so a cache replay or a resumed report renders the same
`VERIFIED_CONTESTED` status the original run produced.

## Re-Verifying Operationally-Failed Findings (Stub)

`VERIFICATION_FAILED` findings (transient operational errors — rate
limit, server error, network failure, parse error, INVALID_REQUEST,
batch cancellation) are not persisted in the verification cache, so a
re-run will re-attempt verification for them automatically. For
larger runs where re-running the entire pipeline is expensive, the
`SPEC_CRITIC_RESUME_RETRY_FAILED_ONLY=1` env var is reserved as the
toggle for "on the next resume, only re-submit findings whose previous
verification failed operationally" — the actual implementation is
deferred to a focused future change (the helper currently logs a
warning at startup when the flag is set so the operator knows it is
noted but not yet wired).

## Budget-Exhausted Findings

When the verifier consumes its full mode-scaled `web_search` budget
without grounding a verdict, the result carries a
`VerificationResult.budget_exhausted` sentinel. The trust-level
classification stays `INSUFFICIENT_EVIDENCE` (no new top-level status),
but the report distinguishes these findings in two places:

- **Per-finding status line:** the status renders with an inline
  italic sub-label, e.g. `? Insufficient evidence (search budget
  exhausted)`. The sub-label color matches the status so the badge
  reads as part of the status.
- **Run Diagnostics banner:** a "Budget-exhausted findings" row
  (highlighted red when count > 0) and a recovery-hint paragraph
  pointing operators at the severity-tiered budget knob
  (CRITICAL=8, HIGH=7, MEDIUM=5, GRIPES=3 — see
  `api_config._SEVERITY_MAX_USES`) as the actionable remedy.

Budget-exhausted results are NOT persisted in the verification cache
(same transient-signal logic as `VERIFICATION_FAILED` — a re-run with
elevated severity allocates more budget; freezing the shortfall as a
durable verdict would suppress re-verification). The flag round-trips
through resume state, so a resumed report keeps the sub-label.

The calibration eval (`python -m evals.calibration.runner`) reports a
`Budget-exhausted findings: N` line in the summary header so the
recheck can confirm end-to-end telemetry. The
`tp_unverified_budget_exhausted` fixture is the canonical example.

## Agent Tracing

Every run captures a forensic trace of agent invocations to JSONL on disk. When a verdict looks off or a finding got suppressed for non-obvious reasons, the trace lets you reconstruct what the model actually saw, what it produced, and how the pipeline interpreted that output.

**Default-on.** The trace directory lives at `~/.spec_critic/traces/<run_id>/` (override via `SPEC_CRITIC_TRACE_DIR`). The `<run_id>` matches `DiagnosticsReport.run_id` so a trace can be correlated with the diagnostics report by directory name.

### Files

| File | Contents |
|---|---|
| `run.json` | Run metadata: run_id, mode, model, cycle, files_reviewed, capture_level, started/ended timestamps. |
| `spans.jsonl` | One line per closed span. Spans nest via `parent_span_id` — `pipeline` → `review` / `cross_check` / `verification_initial` → `api_call` → `web_search`. |
| `events.jsonl` | One line per event, keyed by `span_id`. Types include `thinking_block`, `tool_use`, `web_search_query`, `web_search_result`, `pause_turn`, `parse_attempt`, `grounding_outcome`, `escalation_decision`, `budget_exhausted_marker`. |
| `prompts.jsonl` | Default-level only: content-deduped prompts referenced by SHA-256 hash from span `inputs`. Deep mode inlines prompts on each span instead. |
| `findings.jsonl` | One line per finding at terminal state, snapshotted at run end. Carries every Chunk 11-13 verification field (web_fetch_requests, fetched_sources, models_disagreed, initial_sources, budget_exhausted). |

### Env vars

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_TRACE` | on | Disable with `0` / `false` / `no` / `off`. |
| `SPEC_CRITIC_TRACE_DEEP` | off | Enable with any truthy value to record per-stream chunks, full web_search snippet bodies, untruncated raw responses, and inline prompts. Implies trace enabled. |
| `SPEC_CRITIC_TRACE_DIR` | `~/.spec_critic/traces/` (state dir on macOS/Linux, equivalent on Windows) | Override the trace root. `~` and `$VAR` are expanded. |

### GUI

The GUI's Tracing row exposes two checkboxes ("Record agent trace", "Deep mode"), a "Show folder" button that opens the trace root in the OS file explorer, and an "Open viewer" button that opens the bundled HTML viewer in the default browser. The checkboxes set the env vars at run start, so toggling between runs takes effect without a process restart. The default is "Record agent trace" on, "Deep mode" off.

### HTML viewer

`src/tracing/viewer/trace_viewer.html` is a single-file, zero-build replay tool (open it in any browser, then pick a trace folder). Four views: **By Finding** (finding → review → verification → grounding → verdict), **By Span** (raw tree + prompt resolution), **Timeline** (filterable events), **Search / Grounding** (queries + accepted/rejected URLs). Colors and glyphs mirror the Word report.

### CLI

```
python -m src.tracing list                          # enumerate runs
python -m src.tracing show <run_id>                 # finding-by-finding summary
python -m src.tracing prune --keep-last 20          # keep the 20 newest
python -m src.tracing prune --older-than 30d --yes  # delete runs older than 30 days
```

All subcommands accept `--trace-dir DIR` to point at a non-default root. `show` resolves `<run_id>` by directory name or by the `run_id` embedded in `run.json`.

### Batch-resume continuity

A batch run's trace survives an app restart: `start_batch_review` stamps the run's trace `run_id` / `trace_dir` / `capture_level` into the resume state, and the resume path reopens that same trace directory (appending, not truncating) so the whole run lands in one trace.

### Trace silo guarantees

- The trace never alters `Finding` / `ReviewResult` / `VerificationResult` / `DiagnosticsReport` shape. Hooks read existing state; they don't add to it.
- `DiagnosticsReport.summary()` output is byte-identical with and without tracing enabled.
- Capture-hook failures never escape into pipeline code. A first-of-kind warning is logged once per (exception-type, frame) and suppressed afterward.
- API keys and bearer tokens are redacted before serialization (shared regex with `diagnostics.py`).

## Changelog (recent)

### v2.11.0
- Default review/cross-check model upgraded to Claude Opus 4.7; escalation model also Opus 4.7
- Persistent verification cache at `~/.spec_critic/verification_cache.json` (atomic temp-file + rename; **60-day default TTL** with age-based pruning on load — set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS=0` to keep the legacy database mode). Cache replays render an inline "Cache replay — Nd old" badge in the report (amber <30d / orange 30-90d / red >90d) so reviewers can spot stale verdicts at a glance; the evidence panel surfaces the cache file path for force-refresh workflows.
- Haiku 4.5 verification triage (always-on for eligible findings); hard safety contract (CRITICAL/HIGH and findings with a code reference are never eligible; override model via `SPEC_CRITIC_TRIAGE_MODEL`)
- Severity-tiered web-search budgets: CRITICAL=8, HIGH=7, MEDIUM=5, GRIPES=3
- Verification output cap tightened to 16k; `SYNTHESIS_OUTPUT_CAP` and `HAIKU_TRIAGE_OUTPUT_CAP` added
- Cross-check chunking refined (Div 21 / 22 / 23 / Controls / 25 + 01)
- **Trust Upgrade Chunk 12**: New `VERIFIED_CONTESTED` status (⚡, purple) when initial and escalated verifiers disagreed on grounded verdicts; routes to `MANUAL_EDIT_CANDIDATE` regardless of confidence. Evidence panel renders both verdicts and citation sets side-by-side. `SPEC_CRITIC_RESUME_RETRY_FAILED_ONLY` env var reserved (stub) for a future "re-verify only operationally-failed findings" resume mode.
- **Trust Upgrade Chunk 13**: New `VerificationResult.budget_exhausted` sentinel on UNVERIFIED results whose verifier consumed its full mode-scaled `web_search` budget. The report's per-finding status line appends a `(search budget exhausted)` sub-label; the Run Diagnostics banner gets a "Budget-exhausted findings" row plus a recovery-hint paragraph pointing operators at the severity-tiered budget knob. Cache refuses to persist exhausted results (transient signal — re-run with higher severity allocates more budget). Calibration eval surfaces the count in its summary header.

Older changelog entries trimmed; see git history for v2.10.0, v2.8.x, and the non-GUI refactor chunks A–P.
