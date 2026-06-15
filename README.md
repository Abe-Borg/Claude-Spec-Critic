# Spec Critic

**v3.0.0** — AI-assisted M&P specification review for California K-12 DSA projects.

Spec Critic reviews mechanical and plumbing CSI-format `.docx` specifications against California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) and the NFPA / ASHRAE / IAPMO / UL editions adopted for the current cycle, using Claude. It produces structured findings with severity classifications, confidence scores, web-search-backed verification verdicts, optional cross-spec coordination analysis, and structured edit instructions — rendered in a Word report and written to a machine-readable JSON sidecar for a separate, downstream applier to ingest. Spec Critic emits edit instructions but does not apply them.

Configured for the **California 2025 code cycle** by default (`src/core/code_cycles.py`). The cycle pins the adopted NFPA / ASHRAE / IAPMO / UL editions as an ordered collection of `StandardEdition` records (each with the base edition, a California-amendment flag, and a `source` documenting where the edition was confirmed). The reviewer prompt, the verifier prompt, and the exported report's methodology note all render from that one collection, so the model verifies claims against the editions California actually adopted — including cases where California diverges from the latest national edition (e.g. NFPA 25 is the 2013 California Edition). The NFPA fire-protection editions are verified against the California Fire Code 2025, Chapter 80 adoption table; entries whose `source` is marked `UNVERIFIED` (currently the ASHRAE energy editions, the IAPMO TSC, and the UL listings) still need confirmation against the published code.

## Design Emphasis

- **Evidence-grounded verification.** `CONFIRMED` / `CORRECTED` verdicts require at least one cited URL that the `web_search` tool actually retrieved.
- **Cost-aware defaults.** Sonnet-default verifier with Opus escalation, automatic Haiku triage (for eligible findings), severity-tiered + profile-aware search budgets, persistent on-disk claim cache.
- **Robust batch processing.** Message Batches API (50% cost savings) with bounded polling and progressive backoff across the review, verification, and cross-check phases.
- **Emit-only edit instructions.** Findings carry structured edit proposals (action / existing → replacement / target element id / confidence) rendered inline in the Word report and written to a `<report-stem>.edits.json` sidecar. Spec Critic never mutates spec documents — applying edits is left to a separate, downstream tool.
- **Trust-model report output.** Every finding renders one of nine `ReportStatus` labels (including `VERIFICATION_FAILED` for transient operational errors and `VERIFIED_CONTESTED` when the initial and escalated verifiers disagreed on a grounded verdict) and one of two `EditActionLabel` values (`EDIT_SUGGESTED` / `REPORT_ONLY`) so the report makes uncertainty visible.

## Pipeline at a Glance

1. **Text Extraction** — `.docx` paragraphs, tables, text boxes, footnotes/endnotes, and headers/footers. Cached by file hash. Each element gets a stable `element_id` (`p7`, `t0r2`, `tb0p0`, `fn1p0`, `s1h0`, …).
2. **Local Pre-Screening** — Deterministic detectors run before any API call: LEED, placeholders, template markers, stale/invalid code cycles, empty sections, duplicate headings/paragraphs, inconsistent file naming.
3. **Per-Spec Review** — Each spec sent to Claude Opus 4.8 via the `submit_review_findings` tool. Tagged-JSON text parser as fallback.
4. **Deduplication** — Identical findings consolidated across specs; per-file occurrences tracked separately so multi-file edit proposals keep their per-file existing/replacement text.
5. **Verification** — Findings routed into one of four modes (`local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`). Sonnet 4.6 default; CRITICAL/HIGH `UNVERIFIED` escalates to Opus 4.8. Persistent on-disk cache.
6. **Cross-Spec Coordination** *(optional)* — Runs after verification using verified verdicts as input (DISPUTED findings are filtered out of the "already identified" context). Chunked by CSI division (21 / 22 / 23 / Controls / 25 + 01) on large projects. Its own coordination findings are then put through a second verification pass.
7. **Report + Edit Sidecar** — A Word report is exported with every finding, its trust-model status, and any proposed replacement; a machine-readable `<report-stem>.edits.json` sidecar lists each finding's edit proposal for a downstream applier. Spec Critic does not modify spec documents.

## Edit Instructions (Emit-Only)

Spec Critic emits edit instructions but does not apply them. Each finding
may carry a structured edit proposal (action / existing text → replacement
text / target element id / confidence). Proposals are rendered inline in the
Word report ("Proposed replacement") and written to a machine-readable
`<report-stem>.edits.json` sidecar next to the report, as a clean hand-off to
a separate, future program that ingests the instructions and applies them.

The locating-and-mutating write-back stack — and the auto-edit confidence
gating that only existed to decide whether to auto-apply — was removed in
v3.0.0. A finding's verification status (`VERIFIED_SUPPORTED` /
`VERIFICATION_FAILED` / `VERIFIED_CONTESTED` / …) and `edit_confidence` ride
along in the report and the JSON sidecar so a downstream applier can do its
own gating.

## Processing Mode

All reviews submit via the Message Batches API — queued at 50% cost savings, typical turnaround ~45 min – 2 hrs (24 hrs max). The 300k extended-output path is batch-only (`output-300k-2026-03-24` beta header) and triggers only for inputs ≥200k tokens.

A submitted review batch keeps running on Anthropic's servers even if the app closes or the network drops. Spec Critic persists the small amount of state needed to reconnect — the batch id, its request map, and your project-context text (which can include text extracted from attached `.docx`/`.pdf` context files); the spec bodies themselves are re-extracted rather than stored — so an interrupted run can be finished without re-submitting or re-paying for the review. The startup resume prompt rejoins a still-running batch from that saved state; the manual **Recover batch…** action (and `scripts/recover_batch.py`) recover a batch by id even with no saved state, rebuilding the request map from the batch's results — which requires the batch to have **ended** first. The state file lives at `~/.spec_critic/pending_batch.json` (override with `SPEC_CRITIC_PENDING_BATCH_PATH`).

## Model Stack

Defaults (each overridable via its `SPEC_CRITIC_*_MODEL` env var **except cross-check**, which is bound directly to `CROSS_CHECK_MODEL_DEFAULT`; see `api_config.py`):

- Review: Claude Opus 4.8
- Cross-check: Claude Sonnet 4.6
- Verification (initial): Claude Sonnet 4.6
- Verification (escalation / deep-reasoning): Claude Opus 4.8
- Triage: Claude Haiku 4.5

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

Test markers: `token_budget`, `prompt_serialization`, `network`. Fake Anthropic response builders live in `tests/fixtures/fake_anthropic.py`; DOCX inputs are built inline per test with `python-docx`.

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
default action is human review rather than applying either side's
edit. The `VERIFIED_CONTESTED` status is carried into the report and the
JSON sidecar so a downstream applier sees the disagreement and can
withhold the edit, even when the finding still carries an
`EDIT_SUGGESTED` proposal.

The per-finding evidence panel surfaces both verdicts inline:
- The "Escalation history" line shows the initial → final verdict
  transition with each verifier's model name and a "manual review
  recommended" sentence.
- A dedicated "Initial verifier sources" sub-section lists the
  citations the initial verifier produced, alongside the final
  verifier's citations in the regular "Web/code evidence" sub-section.

The contested telemetry round-trips through the verification cache
(no schema bump — runtime telemetry, not verdict semantics), so a
cache replay renders the same `VERIFIED_CONTESTED` status the original
run produced.

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
durable verdict would suppress re-verification). The flag is in-memory
runtime telemetry for the current run.

The calibration eval (`python -m evals.calibration.runner`) reports a
`Budget-exhausted findings: N` line in the summary header so the
recheck can confirm end-to-end telemetry. The
`tp_unverified_budget_exhausted` fixture is the canonical example.

## Agent Tracing

Every run captures a forensic trace of agent invocations to JSONL on disk. When a verdict looks off or a finding landed in an unexpected status, the trace lets you reconstruct what the model actually saw, what it produced, and how the pipeline interpreted that output.

**Default-on.** The trace directory lives at `~/.spec_critic/traces/<run_id>/` (override via `SPEC_CRITIC_TRACE_DIR`). The `<run_id>` matches `DiagnosticsReport.run_id` so a trace can be correlated with the diagnostics report by directory name.

### Files

| File | Contents |
|---|---|
| `run.json` | Run metadata: run_id, mode, model, cycle, files_reviewed, capture_level, started/ended timestamps. |
| `spans.jsonl` | One line per closed span. Spans nest via `parent_span_id` — `pipeline` → `review` / `cross_check` / `verification_initial` → `api_call` → `web_search`. |
| `events.jsonl` | One line per event, keyed by `span_id`. Types include `thinking_block`, `tool_use`, `web_search_query`, `web_search_result`, `pause_turn`, `parse_attempt`, `grounding_outcome`, `escalation_decision`, `budget_exhausted_marker`. |
| `prompts.jsonl` | Default-level only: content-deduped prompts referenced by SHA-256 hash from span `inputs`. Deep mode inlines prompts on each span instead. |
| `findings.jsonl` | One line per finding at terminal state, snapshotted at run end. Carries every verification telemetry field (web_fetch_requests, fetched_sources, models_disagreed, initial_sources, budget_exhausted). |

### Env vars

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_TRACE` | on | Disable with `0` / `false` / `no` / `off`. |
| `SPEC_CRITIC_TRACE_DEEP` | off | Enable with any truthy value to record per-stream chunks, full web_search snippet bodies, batch-verification thinking / tool-use blocks, untruncated raw responses, and inline prompts. Implies trace enabled. |
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

### Trace silo guarantees

- The trace never alters `Finding` / `ReviewResult` / `VerificationResult` / `DiagnosticsReport` shape. Hooks read existing state; they don't add to it.
- `DiagnosticsReport.summary()` output is byte-identical with and without tracing enabled.
- Capture-hook failures never escape into pipeline code. A first-of-kind warning is logged once per (exception-type, frame) and suppressed afterward.
- API keys and bearer tokens are redacted before serialization (shared regex with `diagnostics.py`).

## Changelog (recent)

### v3.0.0
- **Emit-but-don't-apply edits.** Removed the surgical write-back stack (the `src/editing/` package: locator, spec_editor, apply_edits, replacement_style, edit_candidates), the GUI apply dialogs, and the auto-edit confidence gating (composite confidence, numeric/standards demotion, the auto-edit floor). Spec Critic now emits structured edit proposals — rendered inline in the Word report and written to a machine-readable `<report-stem>.edits.json` sidecar — for a separate, future applier to ingest.
- `EditActionLabel` collapsed to `EDIT_SUGGESTED` / `REPORT_ONLY` (the `SUPPRESSED` label was later removed along with the cross-check dependency-suppression feature); `classify_edit_action` is now simply "does this finding carry a proposal?" (verification status and `edit_confidence` ride along for a downstream applier to gate on).
- Removed the now-dead edit-application env vars (`SPEC_CRITIC_TABLE_CELL_AUTO_EDIT`, `_EDIT_TRANSACTIONAL`, `_NORMALIZE_REPLACEMENT_STYLE`, `_PUNCTUATION_BOUNDARY_FIX`, `_ADD_INHERITS_LIST_NUMBERING`, `_RESTORE_KNOWN_FORMATTING`, `_USE_VERIFIER_CORRECTION_AS_REPLACEMENT`, `_AUTO_EDIT_CONFIDENCE_FLOOR`). The verification / grounding system and its calibration eval are unchanged.

### v2.11.0
- Default review/cross-check model upgraded to Claude Opus 4.7; escalation model also Opus 4.7
- Persistent verification cache at `~/.spec_critic/verification_cache.json` (atomic temp-file + rename; **60-day default TTL** with age-based pruning on load — set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS=0` to keep the legacy database mode). Cache replays render an inline "Cache replay — Nd old" badge in the report (amber <30d / orange 30-90d / red >90d) so reviewers can spot stale verdicts at a glance; the evidence panel surfaces the cache file path for force-refresh workflows.
- Haiku 4.5 verification triage (always-on for eligible findings); hard safety contract (CRITICAL/HIGH and findings with a code reference are never eligible; override model via `SPEC_CRITIC_TRIAGE_MODEL`)
- Severity-tiered web-search budgets: CRITICAL=8, HIGH=7, MEDIUM=5, GRIPES=3
- Verification output cap tightened to 16k; `SYNTHESIS_OUTPUT_CAP` and `HAIKU_TRIAGE_OUTPUT_CAP` added
- Cross-check chunking refined (Div 21 / 22 / 23 / Controls / 25 + 01)
- **Trust Upgrade Chunk 12**: New `VERIFIED_CONTESTED` status (⚡, purple) when initial and escalated verifiers disagreed on grounded verdicts; routes to `MANUAL_EDIT_CANDIDATE` regardless of confidence. Evidence panel renders both verdicts and citation sets side-by-side.
- **Trust Upgrade Chunk 13**: New `VerificationResult.budget_exhausted` sentinel on UNVERIFIED results whose verifier consumed its full mode-scaled `web_search` budget. The report's per-finding status line appends a `(search budget exhausted)` sub-label; the Run Diagnostics banner gets a "Budget-exhausted findings" row plus a recovery-hint paragraph pointing operators at the severity-tiered budget knob. Cache refuses to persist exhausted results (transient signal — re-run with higher severity allocates more budget). Calibration eval surfaces the count in its summary header.

Older changelog entries trimmed; see git history for v2.10.0, v2.8.x, and the non-GUI refactor chunks A–P.
