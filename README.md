# Spec Critic

**v3.4.0** — AI-assisted construction-specification review, organized around selectable **review programs** backed by independently versioned discipline modules. The default program is California K-12 DSA mechanical/plumbing.

Spec Critic reviews CSI-format `.docx` specifications against the applicable module's code basis and pinned standards editions, using Claude. With the default **California K-12 DSA M&P** program that means California building codes (CBC, CMC, CPC, Energy Code, CALGreen, ASCE 7) and the NFPA / ASHRAE / IAPMO / UL editions adopted for the current cycle. The single **Hyperscale Data Centers — USA and Canada** GUI choice instead routes each specification to its Fire Suppression, Architecture, Electrical, or Electronic Safety & Security module, a justified combination, or an explicit unsupported coverage gap. The Electronic Safety & Security module's first phase is limited to Fire Detection & Alarm. It produces structured findings with severity classifications, confidence scores, web-search-backed verification verdicts, optional cross-spec coordination analysis, and structured edit instructions — rendered in a Word report and written to a machine-readable JSON sidecar for a separate, downstream applier to ingest. Spec Critic emits edit instructions but does not apply them.

The default module pins the **California 2025 code cycle** (`src/core/code_cycles.py`). The cycle pins the adopted NFPA / ASHRAE / IAPMO / UL editions as an ordered collection of `StandardEdition` records (each with the base edition, a California-amendment flag, and a `source` documenting where the edition was confirmed). The reviewer prompt, the verifier prompt, and the exported report's methodology note all render from that one collection, so the model verifies claims against the editions California actually adopted — including cases where California diverges from the latest national edition (e.g. NFPA 25 is the 2013 California Edition). The NFPA fire-protection editions are verified against the California Fire Code 2025, Chapter 80 adoption table; entries whose `source` is marked `UNVERIFIED` (currently the ASHRAE energy editions, the IAPMO TSC, and the UL listings) still need confirmation against the published code.

## Review Programs and Modules

Everything discipline-specific — the jurisdiction/code basis and pinned standards, the reviewer/cross-check/verifier prompt content, the deterministic detectors' vocabulary (including whether LEED references are flagged), the verification-routing keywords and source tiers, the cross-check chunk map, and the report wording — lives on one frozen, registry-validated `ReviewModule` object (`src/modules/`). A `ProgramDefinition` (`src/programs/`) groups one or more of those modules behind one GUI choice. Per-spec routing is deterministic and auditable; ambiguous matches require confirmation, unsupported files remain visible as skipped coverage gaps, and a spec can intentionally route to more than one module. Child review, research, cross-check, compliance, verification, resume, and report state remain module-scoped so code bases and provenance are never mixed.

Currently registered: `california_k12_mep` (the default), `datacenter_fire` (hyperscale fire suppression, principally Division 21), `datacenter_architecture` (hyperscale architecture/enclosure/life-safety coordination), `datacenter_electrical` (hyperscale electrical power, distribution, reliability, controls, and commissioning), and `datacenter_electronic_safety_security` (phase-one hyperscale fire detection and alarm). The four data-center modules remain separate under the hood but share the existing **city, state/province, country, and client** inputs and one GUI program choice; the per-spec router selects the appropriate module before each runs its own location/client research and review. Division 21 routes to Fire Suppression, Division 26 and explicit utility/generation sections route to Electrical, and fire-alarm specifications in legacy Division 28 31 or current Division 28 46 route to Electronic Safety & Security. Division 27 and other Division 28 scopes — including access control, video surveillance, and intrusion detection — remain explicit coverage gaps rather than being sent to the wrong reviewer. Modules are validated at import: bad prompt slots, few-shot examples the parser would reject, inconsistent detector vocabulary, or an ambiguous chunk map fail app startup rather than a run.

Cross-spec coordination currently runs inside each routed module partition. The
reviewers still evaluate fire-suppression/fire-alarm interface requirements that
appear in their assigned specifications, but direct package-level comparison of
a Division 21 specification against a Division 28 specification is deferred to a
future program-level coordination pass.

## Design Emphasis

- **Evidence-grounded verification.** `CONFIRMED` / `CORRECTED` verdicts require at least one cited URL that the `web_search` tool actually retrieved.
- **Cost-aware defaults.** Sonnet-default verifier with Opus escalation, automatic Haiku triage (for eligible findings), severity-tiered + profile-aware search budgets, persistent on-disk claim cache.
- **Robust batch processing.** Message Batches API (50% cost savings) with bounded polling and progressive backoff across the review, verification, and cross-check phases.
- **Emit-only edit instructions.** Findings carry structured edit proposals (action / existing → replacement / target element id / confidence) rendered inline in the Word report and written to a `<report-stem>.edits.json` sidecar. Spec Critic never mutates spec documents — applying edits is left to a separate, downstream tool.
- **Trust-model report output.** Every finding renders one of nine `ReportStatus` labels (including `VERIFICATION_FAILED` for transient operational errors and `VERIFIED_CONTESTED` when the initial and escalated verifiers disagreed on a grounded verdict) and one of two `EditActionLabel` values (`EDIT_SUGGESTED` / `REPORT_ONLY`) so the report makes uncertainty visible.

## Pipeline at a Glance

1. **Text Extraction** — `.docx` paragraphs, tables, headers/footers. Cached per file, keyed by path, size, modification time, and a content fingerprint of the file's head and tail (not a hash of the whole file). Each element gets a stable `element_id` (`p7`, `t0r2`, `s1h0`, …).
2. **Program Routing** — Under a multi-module program, each extracted spec is assigned to zero, one, or several implemented modules from CSI number/title/content evidence. Ambiguous routes are resolved before review submission.
3. **Local Pre-Screening** — Deterministic detectors run separately under each assigned module before any review call: LEED (module-dependent — flagged for CA K-12, where it is usually a copy/paste error), placeholders, template markers, stale/invalid code cycles, empty sections, duplicate headings/paragraphs, inconsistent file naming.
4. **Per-Spec Review** — Each routed `(spec, module)` request is sent to Claude Opus 5 via the `submit_review_findings` tool. Tagged-JSON text parser as fallback.
5. **Deduplication** — Identical findings consolidated within each module result; per-file occurrences tracked separately so multi-file edit proposals keep their per-file existing/replacement text.
6. **Verification** — Findings routed into one of four modes (`local_skip` / `strict_structured` / `standard_reasoning` / `deep_reasoning`). Sonnet 5 default (`strict_structured` runs at effort `low`); CRITICAL/HIGH `UNVERIFIED` escalates to Opus 5 unless the initial pass failed operationally (reported as VERIFICATION_FAILED instead). Persistent on-disk cache.
7. **Cross-Spec Coordination** *(optional)* — Runs after verification within each assigned module using verified verdicts as input (DISPUTED findings are filtered out of the "already identified" context). Large projects are chunked by that module's CSI division families. Its own coordination findings are then put through a second verification pass.
8. **Report + Edit Sidecar** — A Word report is exported with module-scoped sections, every finding, its trust-model status, and any proposed replacement; a machine-readable `<report-stem>.edits.json` sidecar carries `program_id` and `module_id` provenance for downstream use. Spec Critic does not modify spec documents.

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

## HTML Report & Ask AI (Post-Run)

After a run completes, **Save HTML Report…** (in the footer next to "Check for
Updates"; enabled once a completed result is in memory) exports the review as a
single self-contained `.html` file carrying the full content of the Word report
— Run Diagnostics with its recovery hints, files reviewed, methodology and
pinned editions, summary and trust-model tables, every deterministic alert
(never truncated, unlike the Word report's 5-per-file cap), and every finding
with its verification evidence — plus browser niceties: full-text search,
severity/file/status/edit-action filters with live counts, collapsible finding
cards, a copy-full-report action, and print / Save-as-PDF styling. The file is
portable: no external assets, opens offline by double-clicking, and is safe to
share — it never contains your API key or local file paths. The automatic Word
report + JSON sidecars at run completion are unchanged; HTML is an additional
post-run action, and a canceled or failed save never disturbs the completed
results.

**Ask AI.** The exported report embeds a chat assistant grounded in the report
itself. On first use it asks for an Anthropic API key — the key lives only in
that browser tab's session storage (a visible "Forget key" clears it), opening
the file makes zero network requests, and chat usage bills to your key at
standard API prices. The assistant streams answers with summarized reasoning,
can search the public web for code/standards references (with cited
links) — and read full pages on models that support web fetch (Sonnet 5 does,
Opus 5 does not; the tool is attached per request from the selected model) —
and can act on the page for you: filter the visible findings, jump to
sections, highlight terms, query the structured findings data, and run
arithmetic. It sees the report only — not the original specification documents
— and says so when a question would need source text. The exporter API can also
emit a chat-free variant (`include_chat=False`) with no API reference and no
network permission at all.

## Processing Mode

By default, reviews submit via the Message Batches API — queued at 50% cost savings, typical turnaround ~45 min – 2 hrs (24 hrs max); the opt-in real-time transport described below streams them synchronously instead. The 300k extended-output path is batch-only (`output-300k-2026-03-24` beta header) and triggers only for inputs ≥200k tokens.

A submitted review batch keeps running on Anthropic's servers even if the app closes or the network drops. Spec Critic persists the small amount of state needed to reconnect — the batch id, its request map, and your project-context text (which can include text extracted from attached `.docx`/`.pdf` context files); the spec bodies themselves are re-extracted rather than stored — so an interrupted run can be finished without re-submitting or re-paying for the review. The startup resume prompt rejoins a still-running batch from that saved state; the manual **Recover batch…** action (and `scripts/recover_batch.py`) recover a batch by id even with no saved state, rebuilding the request map from the batch's results — which requires the batch to have **ended** first (on that bare-id path the CLI requires `--module`, since a batch id does not carry its discipline). `scripts/recover_batch.py` with no arguments resumes whatever the app saved — a single-module batch or a routed Hyperscale program run (every child batch polled and combined into one program report). If a collect step had submitted a review repair batch, its id and request map are kept in the saved state too, and the resumed collect re-attaches to that batch rather than submitting another. The state file lives at `~/.spec_critic/pending_batch.json` (override with `SPEC_CRITIC_PENDING_BATCH_PATH`).

**Real-time review transport (opt-in).** The GUI's Options block has a "Real-time review (streaming)" toggle that runs the per-spec reviews and verification synchronously instead of via the Batches API — results arrive immediately (as little as ~10 minutes vs. batch's typical under-2-hours) at standard, non-discounted API pricing, since real-time forfeits the 50% batch savings and verification runs live too. Switching into real-time pops a one-time cost-warning dialog (dismissable). Real-time runs have no resume story — nothing is persisted for an in-progress synchronous run — so the startup resume prompt and **Recover batch…** stay batch-only. Batch remains the default transport.

The GUI also offers **2 / 4 / 6 / 8 concurrent live spec reviews**, with 4 as the persisted default. An existing `SPEC_CRITIC_REALTIME_REVIEW_WORKERS` value of 2/4/6/8 seeds the selector until the user saves a GUI choice. This is one global budget across routed modules, and the app never creates more workers than there are review requests. Higher settings usually finish sooner and spend API budget faster, but increase rate-limit pressure and can add retry cost; they do not change the planned set of spec reviews. Batch mode ignores this control because Anthropic schedules batch requests server-side.

For routed multi-module programs, preparation/research, realtime per-spec reviews, and each module's verification/cross-check/compliance tail now overlap under bounded global worker budgets. The scheduler preserves module-specific prompts and dependency order, completes every preflight before review spend, single-flights shared file extraction and equivalent grounded verification work (a follower whose leader never completes falls back to its own verification after `SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS`, default 900), and deterministically merges results after all workers finish. Conservative defaults protect ordinary API tiers; higher-tier operators can tune `SPEC_CRITIC_REALTIME_REVIEW_WORKERS`, `SPEC_CRITIC_RESEARCH_WORKERS`, `SPEC_CRITIC_PROGRAM_PREPARE_WORKERS`, `SPEC_CRITIC_PROGRAM_COLLECTION_WORKERS`, and `SPEC_CRITIC_REALTIME_COLLECTION_CALLS`.

## Model Stack

Defaults (each overridable via its `SPEC_CRITIC_*_MODEL` env var **except cross-check and compliance**, which are bound directly to `CROSS_CHECK_MODEL_DEFAULT` / `COMPLIANCE_MODEL_DEFAULT`; see `api_config.py`):

- Review: Claude Opus 5
- Cross-check: Claude Sonnet 5
- Verification (initial): Claude Sonnet 5
- Verification (escalation / deep-reasoning): Claude Opus 5
- Requirements research / compliance / drawing digest / drawing impact: Claude Sonnet 5
- Triage: Claude Haiku 4.5

Unknown model ids degrade to safe defaults via `api_config.model_capabilities(...)` — a misconfigured `SPEC_CRITIC_*_MODEL` env var produces a smaller request rather than an API rejection.

Review and verification-escalation moved from Opus 4.8 to **Claude Opus 5** — identical $5/$25 per-MTok pricing, the same 1M context / 128k output ceiling and `output-300k-2026-03-24` batch beta, and a May 2026 knowledge cutoff (vs. Opus 4.8's January 2026), which matters for a tool that flags stale code cycles and standards editions. Opus 4.8 and Sonnet 4.6 stay registered so a pinned `SPEC_CRITIC_*_MODEL` override still builds a correct request shape.

## Construction Drawing Attachments

The **Attach Drawings…** action in the Project Context panel accepts construction-drawing PDFs and turns them into plain text: one synchronous vision pass digests each drawing set into a structured summary (sheet index, general notes, schedules, coordination observations) that's merged into Project Context, so every review, cross-check, and verification call sees it at ordinary text cost. The vision cost is paid once at attach time, not re-paid on resume.

When a drawing digest is attached, a final synthesis pass ("drawing-impact synthesis") runs after cross-check and verification to explain how the drawings actually informed the review — an impact rating (substantial/moderate/minimal/none), a short narrative, and per-finding links back to specific findings, rendered as a "How the Drawings Informed This Review" report section. Works under any module, including the default California one; a run with no drawings attached is unaffected.

## Location-Aware Review (Data-Center Program)

`datacenter_fire`, `datacenter_architecture`, `datacenter_electrical`, and `datacenter_electronic_safety_security` turn on a location-aware pipeline layered on top of the ordinary review flow, gated by one shared per-run **project profile** (city / state or province / country / client, entered when the hyperscale program is selected):

- **Requirements research** — before review submission, a fan-out of grounded web searches gathers jurisdiction- and client-specific requirements for the project's location and folds a rendered summary into Project Context.
- **Compliance pass** — a synchronous pass (modeled on cross-spec coordination) checks the specs against that requirements profile and reports coverage — represented / contradicted / unclear / missing — for anything that's an actual controlling requirement (advisory items are excluded).
- **Location-aware verification** — the project's city/state feeds the verifier's web-search tool so grounding favors sources local to the project, and the verification cache key folds in a jurisdiction fingerprint so a verdict grounded for one city never replays for another.

Output includes a "Jurisdiction & Client Requirements" report section and a standalone `<report-stem>.profile.json` sidecar. A profile-less run (every run under the default California module) is untouched by any of this — the flag is the switch.

## Download & Install (Windows)

Spec Critic ships as a normal downloadable Windows app — no server to host,
nothing running in the cloud. It runs entirely on your own PC and talks only to
the Anthropic API with your own key.

1. Go to the [latest release](https://github.com/Abe-Borg/Claude-Spec-Critic/releases/latest)
   and download **`SpecCriticSetup.exe`**.
2. Run it. Because the app is not code-signed, Windows SmartScreen may show a
   **"Windows protected your PC"** notice — click **More info → Run anyway**.
   This is expected for independent software; you'll see it on the first
   install and on each update.
3. It installs per-user (no admin prompt), adds a Start-menu shortcut, and
   launches. Paste your Anthropic API key into the field at the top and you're
   ready.

**Staying up to date.** The app silently checks for a new version once a day at
launch, and the **Check for Updates** button (bottom-right footer) checks on
demand. When an update exists, a dialog shows what's new and lets you choose
**Download & Install**, **Skip this Version**, or **Later** — nothing installs
without your say-so. Every download is verified against a published SHA-256
hash before it runs, so a corrupted or tampered installer is rejected
automatically. Set `SPEC_CRITIC_DISABLE_UPDATE_CHECK=1` to turn the check off
entirely; `SPEC_CRITIC_UPDATE_URL` points the updater at a fork's releases.
See `docs/RELEASE_WINDOWS.md` for how releases are built and published.

## Requirements

- Python 3.11+ (source install; the Windows installer bundles its own)
- Anthropic API key (`ANTHROPIC_API_KEY`)
- See `requirements.txt`: `anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic`, `lxml`, `keyring` (API-key storage), `truststore` (OS trust store for corporate TLS proxies in the frozen app). Test tooling (`pytest` and friends) lives in `requirements-dev.txt`.

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

**Default-on.** Traces live under the platformdirs state directory — `%LOCALAPPDATA%\SpecCritic\traces\` on Windows, `~/.local/state/SpecCritic/traces/` on Linux, `~/Library/Application Support/SpecCritic/traces/` on macOS — one `<run_id>/` directory per run (override the root via `SPEC_CRITIC_TRACE_DIR`). This is the one piece of state not under `~/.spec_critic/`; the cache, pending-batch, UI-state, update, and log files stay there. The `<run_id>` matches `DiagnosticsReport.run_id` so a trace can be correlated with the diagnostics report by directory name.

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
| `SPEC_CRITIC_TRACE_DEEP` | off | Enable with any truthy value to record per-stream chunks, full web_search snippet bodies, untruncated raw responses, and inline prompts. Implies trace enabled. |
| `SPEC_CRITIC_TRACE_DIR` | platformdirs state dir: `%LOCALAPPDATA%\SpecCritic\traces\` (Windows), `~/.local/state/SpecCritic/traces/` (Linux), `~/Library/Application Support/SpecCritic/traces/` (macOS) | Override the trace root. `~` and `$VAR` are expanded. |
| `SPEC_CRITIC_TRACE_RETENTION_DAYS` | `30` | Runs older than this are deleted on every run start (never the run being started). `0` disables. |
| `SPEC_CRITIC_TRACE_MAX_RUNS` | `50` | Only the N most recent runs are kept on every run start. `0` disables. |

### GUI

The GUI's Tracing row is hidden by default — enable the "Show agent tracing tools" option (persisted as `show_tracing_tools` in the UI state) to reveal it. It exposes two checkboxes ("Record agent trace", "Deep mode"), a "Show folder" button that opens the trace root in the OS file explorer, and an "Open viewer" button that opens the bundled HTML viewer in the default browser. The checkboxes set the env vars at run start, so toggling between runs takes effect without a process restart. The default is "Record agent trace" on, "Deep mode" off.

### HTML viewer

`src/tracing/viewer/trace_viewer.html` is a single-file, zero-build, fully offline replay tool — its styling is an inline stylesheet, it references no external script, stylesheet, font, or image, and opening it performs zero network requests (it renders local prompts and full spec text). Open it in any browser, then pick a trace folder. Four views: **By Finding** (finding → review → verification → grounding → verdict), **By Span** (raw tree + prompt resolution), **Timeline** (filterable events), **Search / Grounding** (queries + accepted/rejected URLs). Colors and glyphs mirror the Word report.

### CLI

```
python -m src.tracing list                          # enumerate runs
python -m src.tracing show <run_id>                 # finding-by-finding summary
python -m src.tracing prune --keep-last 20          # keep the 20 newest
python -m src.tracing prune --older-than 30d --yes  # delete runs older than 30 days
```

Pruning also runs automatically on every run start (`SPEC_CRITIC_TRACE_RETENTION_DAYS` / `SPEC_CRITIC_TRACE_MAX_RUNS`, default 30 days / 50 runs; `0` disables either) through the same code path, so the manual command is only needed for one-off cleanup.

```bash
```

All subcommands accept `--trace-dir DIR` to point at a non-default root. `show` resolves `<run_id>` by directory name or by the `run_id` embedded in `run.json`.

### Trace silo guarantees

- The trace never alters `Finding` / `ReviewResult` / `VerificationResult` / `DiagnosticsReport` shape. Hooks read existing state; they don't add to it.
- `DiagnosticsReport.summary()` output is byte-identical with and without tracing enabled.
- Capture-hook failures never escape into pipeline code. A first-of-kind warning is logged once per (exception-type, frame) and suppressed afterward.
- API keys and bearer tokens are redacted before serialization (shared regex with `diagnostics.py`).
- The HTML viewer loads nothing from the network, and every trace-derived string is HTML-escaped for both text and attribute context (`& < > " '`); `tests/test_trace_viewer_offline.py` pins both.

## Changelog (recent)

### Unreleased
- **Cost estimates are complete.** Estimates now price one-hour prompt-cache writes (2× input), cache reads (0.1×), and web searches ($10 per 1,000, never batch-discounted) alongside tokens; the diagnostics report shows the line items per phase and counts calls on unrecognized model ids instead of pricing them at zero.
- **Rejected citations say why.** The evidence panel (Word and HTML) explains each rejected URL — a blocked domain with its category, or a URL that was never among the searched or fetched results.
- **Verification cache bounded.** A 5,000-entry LRU cap (`SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES`) and compact writes (about 21% smaller); a single-flight follower whose leader never completes falls back to its own verification after `SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS` instead of waiting forever.
- **Cross-check and compliance share one chunking engine** (`src/core/chunked_pass.py`), so their merges can no longer drift; a chunked cross-check now reports its real cache-token counts.
- **Hygiene.** `requires-python`, `lxml` and `keyring` declared, test tooling split into `requirements-dev.txt`, `pip check` in CI; the release version guard covers the README and CLAUDE.md literals; the test suite is hermetic without `tkinter` and a meta-test keeps it that way; CLI output is UTF-8 on legacy Windows consoles; the updater writes its state atomically and tolerates timezone-aware timestamps; the two sidecar schema versions are named constants; bare-id recovery tells apart files whose sanitized names collide; spec discovery is case-insensitive on Linux/macOS; program results degrade instead of raising after spend; the research-profile trim is logarithmic; default report filenames carry the time and program so sidecars are never silently overwritten; the diagnostics timeline renders in chunks; the trace directory is documented at its real per-platform location; dead code removed.
- **Windows installer is self-contained.** The build bundles tiktoken's `cl100k_base` rank file (previously fetched from a public blob host at first use, which corporate networks block even when `api.anthropic.com` is allowed — token analysis raised and the Run button never enabled), points the frozen app at it before any import, and `--selfcheck` now counts a token so CI catches a missing rank file. The executable embeds a `longPathAware` manifest.
- **The app has a log file.** Warnings the console-less build used to drop (unknown-model fallbacks, trace-writer crashes, token-count API failures) now land in `~/.spec_critic/logs/spec_critic.log` (`SPEC_CRITIC_LOG_PATH`).
- **Closing the window is safe.** The close button stops the trace recorder first (queued trace lines and `run.json` end time are written) and asks for confirmation only when it would discard a real-time run or a drawing analysis; a batch run closes silently and resumes next launch.
- **The window no longer freezes** on drawing validation/splitting, context-file extraction, the completion Word export, or the token gauge for routed programs — all run in the background. The drawing cost-confirm dialog measures one request exactly and scales the rest (each PDF is uploaded once, not twice), and the progress bar tracks the analysis.
- **Export failures are recoverable.** A failed Word export prompts Retry/Cancel with the usual cause (the `.docx` open in Word) and ends the run in the amber state; a footer **Save Word Report…** button re-runs the export with the same sidecars. Every dialog names its owner window, font scale and the cross-check toggle persist, and the Run button gate survives a reset.
- **Trace viewer is offline and traces are pruned.** The viewer no longer loads a stylesheet from a CDN and escapes attribute quotes; runs older than 30 days or beyond the 50 most recent are pruned on every run start (`SPEC_CRITIC_TRACE_RETENTION_DAYS` / `SPEC_CRITIC_TRACE_MAX_RUNS`).
- **Verifier policy.** The cheap `strict_structured` mode now runs adaptive thinking at effort `low` (its old "thinking off" setting merely omitted a key that current models read as adaptive-on at medium); the California search location is module data rather than an engine default, so profile-less data-center runs no longer search as California; escalation never fires after an operational failure; app-level retry loops run with SDK retries off so attempts do not stack; the verdict tool is matched by name; single-flight verification shares a clean ungrounded verdict across equivalent findings instead of re-verifying each in turn; the collection-call permit is held per API call, not per pass; a cross-check parse failure gets one retry; the verification cache enforces its source-quote invariant; budget-saturation telemetry uses each finding's severity budget.
- **DOCX extraction.** Horizontally/vertically merged table cells are extracted once (they were repeated per spanned column), tables nested inside cells are extracted, running headers/footers no longer trip the duplicate-paragraph detector, and the stale-code-cycle suppression window cuts at the earliest sentence boundary.
- **Ask AI no longer fails on its default model.** The embedded chat attached `web_fetch` to every request, but web fetch is not available on Claude Opus 5 (the chat's default), so the first message returned HTTP 400 until the reader switched models. The exporter now embeds a per-model fetch map derived from the capability whitelist and the page builds its server-tool list per request from the selected model.
- **Hyperscale (program) reports now open with the Run Diagnostics banner and carry the trust-model summary.** Previously only single-module reports rendered them, so a program run where a spec's review failed read as clean. The banner is aggregated across modules (failed specs are named with their module), in both the Word and HTML reports, and the report title uses the program's display name.
- **Batch verification continuations keep earlier waves' search evidence.** A finding that paused after searching and delivered its verdict in a later wave was judged on the last wave alone, so it failed the "did not search" gate or had its citations rejected. Every wave now sees the whole conversation, including the running search budget.
- **DISPUTED verdicts must be grounded.** A DISPUTED with no accepted citation is downgraded to UNVERIFIED, renders as insufficient evidence, keeps the review confidence prominent, and is neither cached nor replayed from an older cache file.
- **Incomplete verification stops classify the same on both transports.** A `max_tokens` (or refusal) stop in a real-time verification now counts as a verification failure with its consumed search budget reported, exactly as in batch, instead of a clean "insufficient evidence".
- **Model refusals during review are reported as refusals, not truncation, and are not retried** by either transport; the refused spec still surfaces in the failed-review banner.
- **Review repair pass hardened.** A failure anywhere in the repair batch (submit, poll, retrieve) no longer discards the already-paid primary results; the repair batch id and request map are saved in the pending-batch state (single-batch record or program-manifest child) and a resumed collect re-attaches to that batch instead of paying for a second repair; a state write that fails is reported as a warning, not as saved; repair polling reports progress; and the repair prompt's pre-detected alerts now include the polity and file-naming alerts the original carried.
- **Recovery CLI handles routed program runs**, and `--module` is required when recovering a bare batch id (a batch id does not carry its discipline; the old California K-12 default would have reviewed a data-center batch under the wrong prompts).
- **Keyword routing matches whole words.** "bleed valve" no longer matches the `leed` local-skip keyword, and `"formatting"` was removed from every module's internal-coordination vocabulary so real code formatting requirements are verified rather than routed to the cheap path.
- **Chunked cross-check failures carry their error text**, and pending-batch state saves retry and log a warning instead of silently dropping the write.

### v3.4.0
- **HTML report + Ask AI.** A completed run can now be saved as **one self-contained HTML file** via the footer's **Save HTML Report…** button (see "HTML Report & Ask AI" above). Inline CSS/JS, zero external assets, and content parity with the Word report by construction — it imports the DOCX exporter's own summarizers, classifiers, and color constants, so counts and labels cannot drift. The embedded **Ask AI** chat is grounded in the report only, runs entirely in the reader's browser, and never ships an API key inside the file (the reader supplies one; it lives in tab-scoped `sessionStorage`). Opening the file performs zero network requests, and a chat-free variant is available. The automatic Word report + JSON sidecars at run completion are unchanged — HTML is purely additive, and no review-lifecycle code path changed.
- **Review and verification-escalation models upgraded to Claude Opus 5.** Identical $5/$25 per-MTok pricing to Opus 4.8, the same 1M context / 128k output ceiling and `output-300k-2026-03-24` batch beta, plus a May 2026 knowledge cutoff (vs. Opus 4.8's January 2026) — which matters for a tool whose job includes flagging stale code cycles and standards editions. Opus 4.8 and Sonnet 4.6 stay registered so pinned `SPEC_CRITIC_*_MODEL` overrides still build a correct request shape.
- **`web_fetch` is now gated on a per-model capability flag.** Web fetch is not available on Claude Opus 5 (a documented exception in Anthropic's Opus 5 migration guide), and `deep_reasoning` — the mode that routes to the Opus escalation tier — was the one path that would have attached it. An unsupported model now omits the tool, producing a smaller request rather than risking a rejection. `web_search` is unaffected, so the grounding invariant still holds on a fetch-less deep pass.
- **Trust & security modal** in the GUI, detailing how spec content, API keys, and verification evidence are handled.
- **Fixed: the activity log could stay blank for the rest of a run** after clicking Clear while a review was processing. `clear()` emptied the log's queue and textbox but never reset the drain guard or cancelled the in-flight pacing timer, so every later write saw the guard stuck True and refused to restart the pump — and Clear, the natural reset, could not recover it.

### v3.3.0
- **Parallelized routed-program pipeline.** Multi-module program runs (the Hyperscale Data Centers program) now overlap module preparation/research, realtime per-spec reviews, and each module's verification/cross-check/compliance tail under bounded global worker budgets instead of running one module fully behind the next. Dependency order within a module, single-flighted file extraction, and single-flighted equivalent verification cache lookups are preserved; see "Processing Mode" above and `SPEC_CRITIC_PROGRAM_PREPARE_WORKERS` / `SPEC_CRITIC_PROGRAM_COLLECTION_WORKERS` / `SPEC_CRITIC_REALTIME_COLLECTION_CALLS`.
- The GUI's realtime worker selector (2/4/6/8, persisted) now seeds itself from an existing `SPEC_CRITIC_REALTIME_REVIEW_WORKERS` value on first use.

### v3.2.0
- **Hyperscale Electronic Safety & Security module, phase one.** The existing Hyperscale Data Centers choice now routes fire detection and alarm specifications in legacy Division 28 31 or current Division 28 46 to an independently versioned Electronic Safety & Security reviewer. This phase covers fire alarm only; Division 27 and other Division 28 scopes such as access control, video surveillance, and intrusion detection remain explicit coverage gaps. Its US model-code fallback pins NFPA 72-2022 and NFPA 70-2023, while Canadian code and CAN/ULC editions are researched dynamically from the entered project location and local adoption.
- **Hyperscale electrical module.** The existing Hyperscale Data Centers choice now routes Division 26 and explicit electrical utility/generation specifications to an independently versioned Electrical reviewer. It uses the same city, state/province, country, and client inputs as the Architecture, Fire Suppression, and Fire Detection & Alarm reviewers, with project-specific utility, AHJ, adoption, reliability, and commissioning research for the USA and Canada. Division 27 and out-of-scope Division 28 work remain explicit coverage gaps.
- **Hyperscale architecture + per-spec routing.** The GUI exposes one Hyperscale Data Centers program for the USA and Canada. Each spec routes to the independently versioned Fire Suppression, Architecture, Electrical, and/or Electronic Safety & Security modules; unsupported disciplines remain explicit coverage gaps. Composite batch resume, combined reporting, module-qualified edit sidecars, and a single program-level drawing-impact pass preserve provenance without multiplying drawing-analysis cost.

### v3.1.0
- **Windows desktop app + auto-updater.** Spec Critic now ships as a downloadable Windows installer (`SpecCriticSetup.exe`, PyInstaller one-folder + Inno Setup, distributed via GitHub Releases — see the install section above and `docs/RELEASE_WINDOWS.md`). The app checks for updates once a day at launch and on demand via the footer's **Check for Updates** button; downloads are SHA-256-verified against the release manifest before they ever run, with https enforced end-to-end (including post-redirect URLs). The updater is Windows-gated (source runs on macOS/Linux are never offered the installer), and the update dialog defers to in-flight reviews/digests. New env vars: `SPEC_CRITIC_UPDATE_URL`, `SPEC_CRITIC_DISABLE_UPDATE_CHECK`, `SPEC_CRITIC_UPDATE_STATE_PATH`.
- **`datacenter_fire` review module** added alongside the default California K-12 module, opting into a new **location-aware pipeline**: a per-run project profile, a requirements-research fan-out, a local-code compliance pass, and location-aware verification (see "Location-Aware Review" above).
- **Real-time review transport.** An opt-in GUI toggle streams per-spec review and verification synchronously instead of via the Message Batches API, trading the 50% batch discount for immediate results (see "Processing Mode" above).
- **Construction drawing attachments.** Drawing-set PDFs can be attached and digested into Project Context via a one-time vision pass, plus a post-review "drawing-impact synthesis" pass that explains how the drawings informed the findings (see "Construction Drawing Attachments" above).
- Default verification (initial) and cross-check models moved from Sonnet 4.6 to Sonnet 5.

### v3.0.0
- **Emit-but-don't-apply edits.** Removed the surgical write-back stack (the `src/editing/` package: locator, spec_editor, apply_edits, replacement_style, edit_candidates), the GUI apply dialogs, and the auto-edit confidence gating (composite confidence, numeric/standards demotion, the auto-edit floor). Spec Critic now emits structured edit proposals — rendered inline in the Word report and written to a machine-readable `<report-stem>.edits.json` sidecar — for a separate, future applier to ingest.
- `EditActionLabel` collapsed to `EDIT_SUGGESTED` / `REPORT_ONLY` (the `SUPPRESSED` label was later removed along with the cross-check dependency-suppression feature); `classify_edit_action` is now simply "does this finding carry a proposal?" (verification status and `edit_confidence` ride along for a downstream applier to gate on).
- Removed the now-dead edit-application env vars (`SPEC_CRITIC_TABLE_CELL_AUTO_EDIT`, `_EDIT_TRANSACTIONAL`, `_NORMALIZE_REPLACEMENT_STYLE`, `_PUNCTUATION_BOUNDARY_FIX`, `_ADD_INHERITS_LIST_NUMBERING`, `_RESTORE_KNOWN_FORMATTING`, `_USE_VERIFIER_CORRECTION_AS_REPLACEMENT`, `_AUTO_EDIT_CONFIDENCE_FLOOR`). The verification / grounding system and its calibration eval are unchanged.

### v2.11.0
- Default review/cross-check model upgraded to Claude Opus 4.7; escalation model also Opus 4.7
- Persistent verification cache at `~/.spec_critic/verification_cache.json` (atomic temp-file + rename, written compact; **60-day default TTL** with age-based pruning on load — set `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS=0` to keep the legacy database mode — and a 5000-entry LRU cap, `SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES`, `0` disables). Cache replays render an inline "Cache replay — Nd old" badge in the report (amber <30d / orange 30-90d / red >90d) so reviewers can spot stale verdicts at a glance; the evidence panel surfaces the cache file path for force-refresh workflows.
- Haiku 4.5 verification triage (always-on for eligible findings); hard safety contract (CRITICAL/HIGH and findings with a code reference are never eligible; override model via `SPEC_CRITIC_TRIAGE_MODEL`)
- Severity-tiered web-search budgets: CRITICAL=8, HIGH=7, MEDIUM=5, GRIPES=3
- Verification output cap tightened to 16k; `SYNTHESIS_OUTPUT_CAP` and `HAIKU_TRIAGE_OUTPUT_CAP` added
- Cross-check chunking refined (Div 21 / 22 / 23 / Controls / 25 + 01)
- **Trust Upgrade Chunk 12**: New `VERIFIED_CONTESTED` status (⚡, purple) when initial and escalated verifiers disagreed on grounded verdicts; routes to `MANUAL_EDIT_CANDIDATE` regardless of confidence. Evidence panel renders both verdicts and citation sets side-by-side.
- **Trust Upgrade Chunk 13**: New `VerificationResult.budget_exhausted` sentinel on UNVERIFIED results whose verifier consumed its full mode-scaled `web_search` budget. The report's per-finding status line appends a `(search budget exhausted)` sub-label; the Run Diagnostics banner gets a "Budget-exhausted findings" row plus a recovery-hint paragraph pointing operators at the severity-tiered budget knob. Cache refuses to persist exhausted results (transient signal — re-run with higher severity allocates more budget). Calibration eval surfaces the count in its summary header.

Older changelog entries trimmed; see git history for v2.10.0, v2.8.x, and the non-GUI refactor chunks A–P.

## License

Copyright © 2025–2026 Abraham Borg.

Spec Critic is licensed under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) — see [`LICENSE`](LICENSE). You may use, copy, modify, and share it for **any noncommercial purpose** — personal use, study, research, hobby projects, and use by charitable, educational, or government organizations. **Commercial use requires the copyright holder's prior written permission.** Anyone redistributing the software (or part of it) must pass along the license terms and the `Required Notice:` line from the `LICENSE` file.

Third-party dependencies — direct and transitive, pinned in `requirements.txt` — are installed separately and remain under their own licenses (MIT / BSD / Apache-2.0 / MPL-2.0); a bundled binary distribution must carry every bundled package's license text.

Spec Critic is an AI-assisted review aid, not an authority. Its output is advisory and is not a substitute for review by a licensed design professional.
