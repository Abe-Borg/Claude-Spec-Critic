# CLAUDE.md — Spec Critic v3.11.0

Engineering reference for the Spec Critic codebase. Non-obvious invariants and orientation only — read the source for type signatures, and `plans/experiments/` for experiment protocols.

---

## Working agreements

**PR workflow (standing instruction):** After pushing commits to a feature branch, open a pull request against `master` without waiting to be asked — update the existing open PR if one is already open for the branch. This durably authorizes PR creation and overrides the default "don't open a PR unless explicitly asked" behavior. Still confirm before merging, force-pushing, or other destructive / irreversible actions.

**Implementation plan in progress (standing instruction):** `plans/spec-critic-implementation-plan.md` is being worked through one chunk per session, in order; `plans/PROGRESS.md` records what is done, which chunk is next, and the prompt that starts it. A session working on the plan does exactly one chunk, opens one PR, and updates `plans/PROGRESS.md` in that PR (the plan's Part 1 has the full rules). Until their chunks land, a few statements in this file are known to be wrong; `plans/PROGRESS.md` ("Known-wrong statements") lists them, and where the two disagree, trust the tracker.

**Trust maintenance:** The trust surfaces are a contract. Any change to a behavior they describe updates them in the same change. Triggers include a new user action, network call, automatic behavior, model or setting change, or new limit. Update `docs/TRUST_CLAIMS.md` first, then `src/gui/trust_content.py`, its native presentation in `src/gui/trust_dialogs.py`, and the tests. Regenerate the reviewed offline dossier:

```bash
python -c 'from pathlib import Path; from src.gui.trust_content import markdown_dossier; Path("docs/TRUST.md").write_text(markdown_dossier(), encoding="utf-8")'
python -m pytest tests/test_trust_content.py tests/test_trust_dialogs.py
```

Native dialog tests require a display (for example `xvfb-run -a python -m pytest tests/test_trust_dialogs.py`); they skip explicitly when one is unavailable. Run them under a display before claiming keyboard/layout validation. The shared copy binds facts to engine constants; the saved dossier pins the reviewed values so a changed limit, model, or price also requires a reviewed copy update. Companion assistance facts are source-pinned without importing the separate applier into `src/`.

---

## 1) What it is

Python desktop app (CustomTkinter) that reviews construction-specification `.docx` files under a selectable **review program**. A program groups validated domain modules from `src/modules/` (code basis, prompts, detector vocabulary, routing keywords, chunk map). The engine itself has no domain content.

The default program is **California K-12 DSA mechanical/plumbing** (`california_k12_mep`). **Hyperscale Data Centers — USA and Canada** routes each spec to independently versioned fire-suppression (`datacenter_fire`), architecture (`datacenter_architecture`), electrical (`datacenter_electrical`), and electronic-safety-and-security (`datacenter_electronic_safety_security`) modules. The last is phase-one **Fire Detection & Alarm** only: legacy Division 28 31 and current Division 28 46 go there; Division 21 stays with Fire Suppression; Division 26 stays with Electrical. Division 27 and the rest of Division 28 (access control, video surveillance, intrusion detection) are explicit coverage gaps. Do not dual-route alarm specs. All four data-center modules set `project_profile_enabled` (profile → research → compliance → location-aware verification).

Cross-spec coordination is per module. A Division 21 spec is never compared with a Division 28 spec. A small program is not checked across disciplines just because it would fit in one request — each module sees only its own partition. A program-level pass exists only as experiment EX-06 (default off, observation only, no report). See `plans/experiments/EX-06-cross-coordination.md`.

**The app emits edit instructions and does not apply them.** Proposals are rendered in the report and written to `<report-stem>.edits.json`. **`applier/`** (`python -m applier`) applies them as Word tracked changes into a copy. Nothing under `src/` may import it.

Default review model is Claude Sonnet 5.5 (`claude-sonnet-5-5`, `SPEC_CRITIC_REVIEW_MODEL`) at effort `high`: an owner decision of 2026-10-08 on price and Anthropic's guidance, not measured on this workload (`plans/cost-optimization-audit.md`). Every Opus request is held to `api_config.OPUS_EFFORT_CEILING` (`medium`), so a review pinned back to Opus runs at `medium`. A pending batch keeps the model it was submitted with, for collection and repair (bare-id recovery has no record, so it assumes the current default). Batch review is the default. Inputs at or above 200k tokens use the batch-only 300k output beta (`output-300k-2026-03-24`); smaller inputs use the 128k baseline. An operator toggle can run per-spec review and verification synchronously at standard API price, with no resume story. A submitted batch is persisted (`~/.spec_critic/pending_batch.json`) so an interrupted run can rejoin it. Saved state carries the `request_map` and project-context text, never spec bodies.

## Source layout

```
src/
├── __init__.py             # Package version (3.11.0)
├── core/                   # api_config, credentials, pricing, tokenizer, request_budget,
│                           #   chunked_pass, project_profile, resend_sanitizer, resource_pressure,
│                           #   ui_state, updates
├── modules/                # ReviewModule + registry (CA K-12, four data-center modules)
├── programs/               # Programs, deterministic routing, per-spec assignments
├── gui/                    # CustomTkinter shell and thin *_controller.py bridges
├── orchestration/          # pipeline, program_pipeline, batch_resume, collection_outcome,
│                           #   occurrences, diagnostics
├── review/                 # reviewer, realtime_review, request builder, schemas, prompts
├── cross_check/            # Per-module cross-spec coordination
├── research/               # Requirements-research fan-out + EX-05 cache (off)
├── coordination/           # EX-06 cross-chunk / cross-module pass (off)
├── compliance/             # Local-code compliance + coverage completeness
├── drawing_impact/         # Post-review drawing-impact synthesis
├── verification/           # Verifier, governing basis, cache, routing, grounding,
│                           #   native citations, retry policy, triage
├── batch/                  # Message Batches wrapper + bounded polling
├── input/                  # DOCX extraction, numbering, headings, section identity,
│                           #   input_files, preprocessor, drawing analysis (text attach)
├── tracing/                # Optional JSONL trace + viewer
└── output/                 # Word report, HTML report, edit sidecar, report status

applier/                    # Separate program. src/ must not import it.
packaging/windows/          # PyInstaller + Inno Setup; check_release_version.py
```

## High-level flow

```
.docx files
  → extract (cached) → preprocess → request-budget preflight
  → submit review batch (or real-time streams)
  → collect results (+ one review repair batch)
       provisional while that repair is pending or unreachable:
       verification, cross-check, compliance, drawing impact, and EX-06 wait;
       the report says so; the saved record is kept
  → dedupe findings and stamp rf- ids
  → verify findings
  → cross-check (per module; chunked for oversized input or output recovery)
  → compliance (location-aware modules with a requirements profile)
  → verify cross-check and compliance findings
  → drawing impact (only when a drawing-analysis block is in Project Context)
  → EX-06 coordination, only when that switch is on (observation only)
  → Word report + .edits.json (one entry per occurrence)
  → apply_saved_state_cleanup
```

---

## 2) Non-obvious invariants

Field-level details live in the source. These are the contracts a local reading will not make obvious, and whose violation is silent or expensive.

### Grounding invariant

`CONFIRMED`, `CORRECTED`, and `DISPUTED` require at least one **accepted** external citation: a model-cited URL whose normalized form matched a URL verification tools retrieved in **this** conversation (`searched ∪ fetched`). Grounding proves retrieval, not that the page supports the claim. In every mode except the two fetch-eligible ones — and on the Opus escalation tier, because web fetch is off for Opus 5 and Opus 5.5 — the model saw a search snippet, not the full page. `VERIFIED_SUPPORTED` means "checked against a retrieved source."

`VerificationResult.sources` is the accepted list. Reports and the cache never persist invented URLs. Both transports run `_apply_source_grounding` then `_enforce_grounding_invariant`. `report_status.classify_status` re-applies the rule: an uncited `DISPUTED` is `INSUFFICIENT_EVIDENCE`, and the review confidence stays prominent.

The prompt asks `DISPUTED` for the contradicting quote. The parser and the cache do not require it. Quote-gating is `CONFIRMED` / `CORRECTED` only. A substantive accepted citation is what makes a `DISPUTED` cacheable.

**Native citations are not grounding.** `VerificationResult.native_citations` never feeds the grounding check, the cache predicate, or `classify_status`.

One exception, off by default: `SPEC_CRITIC_SOURCE_REUSE=supply` (real-time only) may ground a verdict on a passage retrieved earlier in the run. Those URLs are `reused_sources`, the report says they were supplied, and the cache refuses the result. See `plans/experiments/EX-04-evidence-validation-source-reuse.md`.

### FindingGroup vs FindingOccurrence

`orchestration/occurrences.py` (stdlib-only; `pipeline` re-exports it). A display group is one finding. An executable occurrence is one file and one target (the proposal's `target_element_id`, else `evidenceElementId`) plus that place's own instruction.

- With an element index, a place is `validated` only when the element exists and contains the locator text (exact, or whitespace-collapsed; never case-tolerant). Without files to check, a named element is `claimed`. A missing or failed element is `unresolved`. A file with no recorded original is `missing_original` and gets the shared edit text with **no element and no anchor**.
- Duplicate means byte-identical instruction at one target. Case or spacing differences stay separate. All unresolved members of one file and instruction are one uncertain occurrence, never folded into a located one.
- `executable_proposal()` uses that place's own original. The representative's proposal is never borrowed. Report-only representatives emit no places.
- `occurrence_id` = `oc-` + 12 hex of `sha256(repr((module_id, finding_id, file, target)))`. Module-qualified, order-independent. The occurrence model does not mint finding ids.
- `edit_occurrences` lists each `occurrence_id` once. Content twins share the least-trusted status (`STATUS_TRUST_ORDER`); twins whose edit text differs are two entries.

The sidecar and both reports consume this view. Schema 6 (single module) and 7 (program) write one entry per occurrence, keyed `(module_id, occurrence_id)`. Schemas 4 and 5 are still read by the applier and are not written.

### Finding-id namespacing (review `rf-` vs cross-check `cf-`)

`compute_finding_id` is the only id minter: `sha256(repr(_dedup_key(f)))[:12]` under a prefix. Review findings get `rf-` inside dedup. Cross-check findings get `cf-` in `assign_cross_check_finding_ids` before verification and the sidecar. Compliance findings get `lc-`. The prefix is what keeps a review finding and a coordination finding with the same body from collapsing. Same-content coordination findings intentionally share a `cf-` id. The helper only fills empty ids.

The dedup key ignores the run's own file names and nothing else (`FindingIdentityContext`: exact names, whole tokens, longest first, escaped). A space is a token boundary. With no corpus, nothing is guessed to be a filename. One context per run, derived from the submission and passed explicitly — never from a filtered `specs=` list, never stored on the module.

### Cross-check chunking

The pass is per module. Within a module, `run_chunked_cross_check` uses CSI chunks when the whole request misses its budget (the smaller of `CROSS_CHECK_RECOMMENDED_MAX` and the model ceiling), or for one output recovery after `max_tokens`. Recovery forces smaller parts even when input fits. Cross-check and compliance each share one output recovery allowance across their chunks: either a parse re-request or a truncation recovery, with returned usage summed. An indivisible package still fails. A division that still does not fit is split into contiguous parts. A stranded spec may borrow a neighbor when both new parts fit. A spec that still cannot be sent is **not analyzed** — never sent, never truncated — and counts as a skip.

Every spec is in exactly one planned chunk. Findings keep their division label. Once chunking is on, a conflict across chunks or across parts of one division is invisible. The model is told (`chunk_subset=True` → `_CHUNK_SUBSET_NOTE`). Partial failure keeps completed chunks; combined status is `completed` when at least one chunk completed, and `chunk_failures` / `chunk_skips` stop the diagnostics banner from looking clean. `error` is set only when zero chunks completed.

### REPORT_ONLY action

`validate_edit_shape` demotes `EDIT` / `DELETE` / `ADD` findings that lack required fields, and demotes a no-op `EDIT` whose `existingText` is byte-for-byte identical to `replacementText`. Case-only or whitespace-only deltas are real edits. `reviewer.normalize_edit_shapes` applies that rule at the start of dedup and of `cf-` / `lc-` stamping, including merged members, so ids, the banner, and the sidecar agree. The reason is `demotion_reason`.

### Edit instructions are emitted, not applied

`Finding.as_edit_proposal()` is the accessor (`None` for `REPORT_ONLY` and invalid shapes). `edit_sidecar.write_edit_instructions_sidecar` serializes occurrences. Nothing in `src/` locates or applies edits. Emission follows the representative's proposal. A place whose own finding has no usable edit is written with `edit_proposal: null` so the applier can refuse it. Nothing is lent from another place.

### The applier is a separate program

`tests/test_applier_isolation.py` fails the build if `src/` imports `applier` (including dynamically), if `applier` imports outside its allowlist (`input.extractor`, `input.numbering`, `output.report_status`, `output.edit_sidecar`, `core.api_config`, `core.api_key_store`), if it reaches `src.gui` / `src.orchestration` / `src.review`, if an allowlist entry is unused, or if `pyproject` stops declaring the package. The extractor and numbering imports are load-bearing: element ids and displayed numbers must come from the code that minted them.

Edits are tracked changes into `<stem>.applied.docx`. The source file is never written. The locator refuses rather than guesses. `DISPUTED`, `VERIFIED_CONTESTED`, and `VERIFIED_CONTRADICTED` are withheld by every policy. `VERIFIED_CONTRADICTED` is withheld because the sidecar stores the original proposal and not `VerificationResult.correction`; applying it would write wording the verifier refuted. Carrying `correction` into the sidecar is a `src/` schema change and is deliberately not done here. `--force-status` overrides the verdict gate and does not override `--min-edit-confidence`.

Every document is bound and given a destination before the first write. Two different files with one case-folded name are `FILE_AMBIGUOUS`. A `fileName` containing a path separator never binds. A destination that is any supplied spec, or another document's destination, is `DESTINATION_CONFLICT` (resolved path, and inode when the destination exists). Exit code 3 when anything was held this way. `--assist` never sees a held document.

`DocumentEditor.plan` decides every edit against the unmutated document, then `apply_planned` writes. Element ids are positional, so resolving after an insertion targets the wrong paragraph. Overlapping plans with different instructions are all `EDIT_CONFLICT`; identical instructions are one write and the rest `DUPLICATE`. `--dry-run` takes the same path and skips `document.save()`.

Readable is not writable. The writer matches the extractor's visible text and refuses content controls, fields, smart tags, hyperlinks, text boxes, notes, and any span that would move more than bookmarks and formatting-only runs. An automatic number is display text: a match into the number is refused, except an `EDIT` that quotes the number as exact context and replaces the paragraph's own text. Numbering the resolver cannot read is a miss, never a guess. The assist tier may choose an element id it was shown; it cannot supply specification text.

The reader accepts schemas 4, 5, 6, and 7. Any other `schema_version` raises. Every sidecar entry becomes exactly one receipt outcome.

### HTML report + Ask AI

`output/html_report_exporter.py` renders one self-contained file. It does not mutate the result, does not import the pipeline, and makes no API call while building. It imports the Word exporter's classifiers so counts cannot drift. The full behavior lives in that module's docstring. Contracts that are easy to break:

- Every report string is HTML-escaped. The one executable inline script's CSP hash is over the exact bytes written (`write_html_report` writes binary). CSP is `default-src 'none'`, plus `connect-src https://api.anthropic.com` only when chat is included.
- Report chat offers Opus 5.5 (default), Sonnet 5.5 and Haiku 5.5 at selectable low/medium/high effort. Haiku 5.5 rates rise above 100k total prompt tokens; search fees still apply.
- The exported file never contains an API key. The key lives in page memory only. `include_chat=False` emits no API reference.
- `web_fetch` is attached only for models `model_capabilities` marks `supports_web_fetch`. Opus 5.5 and Haiku 5.5 are off; Sonnet 5.5 is on. The default model must not be sent `web_fetch`.
- A chat turn commits only on `end_turn` or a stop sequence with visible text in the final assistant reply. Thinking-only replies and every other ending discard the turn, so history never holds a `tool_use` without its `tool_result`.
- Chat captures a UTC date from the browser clock at the first request of each conversation. Keep that date stable in the system prefix across all turns/continuations; New chat captures a new date.
- History trimming drops whole turns and strips `thinking` / `redacted_thinking` from the turns it keeps (preserved thinking). Do not replay a thinking block after an edited prefix.
- Finding anchors are unique per report. Drawing-impact links use the payload's `anchor`.

### Prompt-cache breakpoint stability

The instruction prefix in front of `<spec ` stays byte-identical across calls. `<final_task>` sits after the spec body. Cross-check and compliance close the same way: `<final_task>` is the last user-message block, after the corpus, and it is engine protocol (byte-identical across modules).

`pause_turn` resumes in the verifier and research loops set request-level automatic `cache_control` only when `messages` already contains an assistant turn, at the five-minute TTL. The first call of a conversation stays byte-identical. The batch wave path does not set it.

`SPEC_CRITIC_PROJECT_CONTEXT_CACHE` (EX-01, off) is the only review user-message breakpoint. Off is byte-identical. See `plans/experiments/EX-01-project-context-caching.md`.

### Experiments (all off; do not enable them to finish an open item)

Each switch defaults off. Off is byte-identical, or adds no request and changes no verdict, report, or sidecar. Protocols, scores, and promotion rules live under `plans/experiments/`. None of these has met the live API.

| Switch | Off means |
|---|---|
| `SPEC_CRITIC_PROJECT_CONTEXT_CACHE` | EX-01. No review user-message breakpoint. |
| `SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT` | EX-02. `forced_tool` or `json_schema` on the per-spec review only. Parsing never reads the switch. |
| `SPEC_CRITIC_REVIEW_EFFORT` / `SPEC_CRITIC_REVIEW_SCOPE_WORDING` | EX-03. Review effort, or the `<review_scope>` sentence. The effort override is not held to the Opus ceiling. |
| `SPEC_CRITIC_REVIEW_PROCEDURE` | Prompt-audit arm. `open_ended` replaces the numbered review procedure. |
| `SPEC_CRITIC_EVIDENCE_VALIDATION` | EX-04. `observe` only. `enforce` is refused. Changes no verdict or cache decision. |
| `SPEC_CRITIC_SOURCE_REUSE` | EX-04. `shadow` or `supply`. No truthy shorthand. `supply` is real-time only. |
| `SPEC_CRITIC_RESEARCH_CACHE` | EX-05. `reuse` or `refresh`. No truthy shorthand. Off never touches the cache file. |
| `SPEC_CRITIC_CROSS_COORDINATION` | EX-06. `candidates` or `observe`. Observation only: no finding, report line, or sidecar entry. |

EX-07 (native-citation quote provenance) has no code switch. See §10.

### Real-time review transport

Batch remains the default. Branch on `BatchSubmission.review_transport`, never on the `REALTIME_JOB_SENTINEL` batch id. Requests come from `build_review_request` and responses from `reviewer.review_result_from_message`. Real-time omits `service_tier`, pins extended output off, and refuses any spec whose counted input is at least `LARGE_REVIEW_INPUT_THRESHOLD` (200k) before spend. A spec that cannot be sized is refused the same way.

`pipeline.verify_findings_for_run` is the only verification entry both drivers call. A real-time run does no batch polling. It never writes pending-batch state, and its cleanup decision has `saved_state_applies=False`, so a real-time success cannot delete an earlier batch's record. Real-time bills at standard prices (no 50% batch discount). Say so in any UI copy.

Permits are per stream and are released before a retry wait. Routed programs share one pool. GUI worker choices are 2/4/6/8; headless `SPEC_CRITIC_REALTIME_REVIEW_WORKERS` is 1–8, default 4.

### Edition authority

On a `project_profile_enabled` module the verifier presents pinned editions as **reference assumptions**, not as the adopted code. The stale-cycle pre-screen is suppressed on that same flag. The verification cache appends `BASIS_POLICY_NAMESPACE` (`bp1`) for those modules so an old "pin is authoritative" verdict cannot replay. California's prompt wording and cache-key shape stay byte-identical. Its eight `UNVERIFIED` pins are a known scoped-out gap. Do not "fix" California as a side effect of a data-center change.

**The correction.** `_reference_assumption_standards_lines` and `_base_code_assumption_lines` tell the model not to treat a pin as authoritative, and not to treat the newest edition as automatically correct either. A differing citation is not by itself an error. Return `UNVERIFIED` when adoption cannot be established. The review prompt's standards label matches (`Module reference editions (assumptions…)` vs California's `Pinned standard editions`). The report note is `_render_pinned_editions_note` (both exporters). The three surfaces move together.

**The defect.** Research reaches review, cross-check, and compliance through `project_context`. It does not reach verification that way. `src/verification/` must not read `project_context`. The governing basis is the only channel, and it carries labeled claims, not retrieved evidence.

**Cache namespace.** Derived inside `make_cache_key` from the cycle. Bump `bp1` if the wording changes again. California keys stay five segments.

**Build and carry.** `pipeline.build_run_governing_basis` runs inside `prepare_batch_review` after research and before any review spend, then rides `PreparedBatchReview` → `BatchSubmission` → `PendingBatch` → `PipelineResult`. Never rebuild it downstream from today's module data. The gate is `project_profile_enabled`, so the California module carries `None` even if a profile is present. A profile-enabled module with no research still gets a provenance-only basis.

**Persistence and recovery.** Additive on `PendingBatch`, no schema bump. `PendingBatch._resolve_governing_basis` is the one resume decision. `None` means "this module has no basis concept," never "we lost one." A snapshot this build cannot use becomes `recovered_basis()` with the reason in `omissions`. A record written before the basis existed recovers the saved research and marks only the module pins unreconstructable. Bare-id recovery stamps `recovered_basis()`, not a fresh basis. Paid review results are kept in every degradation.

**Researched-context expansion.** Behind `SPEC_CRITIC_GOVERNING_BASIS_CONTEXT`, default off (`0` / `false` / `no` / `off` disable). With the flag unset, every verifier prompt and cache key is byte-identical to the provenance-only shape. Turning the flag off does not restore the old "authoritative for the cycle" wording. `verifier.resolve_governing_basis` returns the prompt lines and the fingerprint together. The block is appended at the end of `<code_basis>`, escaped with `wrap_document_block` (`TAG_GOVERNING_BASIS`) because research text is untrusted and this block sits in the system prompt. `make_cache_key` appends `| gb:<fingerprint>` only when a basis was rendered. Entries written while the flag was on become unreachable when it is off (they still occupy the LRU). Re-enabling the flag finds them again only if the same fingerprint recurs.

Trust rules the basis enforces: claims and qualifications are verbatim; `grounded` is re-derived as `claimed and bool(accepted)`; contradictory claims are both kept; process advisories never become controlling; contractual authority stays separate from adopted law; selection is deterministic; items drop whole, with every omission in the rendered block and the fingerprint. `StandardPin` qualifiers (`edition_phrase`, `note`, `ca_amended`) render and are in the fingerprint. `project` is copied under a `MappingProxyType` at construction. `historical_sources` are research URLs and must never enter the verification accepted-source pool. `fingerprint()` is computed, never stored; confidence is excluded. `basis_from_dict` recomputes it and raises `BasisPolicyIncompatible` on an unknown `schema_version` or `policy_version`. `DEFAULT_BASIS_TOKEN_BUDGET` (4,000) is an unvalidated parameter (§10).

`src/research/` imports `src/verification/`. The basis module must not import the research runner back.

### Project Context attachments

Free text on every review, cross-check, and compliance call. It does not reach verification. Attachments (`.docx` / `.pdf` / `.md` / `.txt`) merge through `gui/context_attachment.py`. Over `PROJECT_CONTEXT_MAX_TOKENS` (100k) the merge is refused, never truncated. Research text spliced into this blob is still invisible to the verifier; the basis is the structured channel.

### Drawing analysis attachment

The app does not read drawings, and every API call is text. `input/drawing_analysis.py` reads the operator's drawing-analyzer output (`.txt` / `.md` / `.json`, verbatim, UTF-8 with replacement; empty, or over `MAX_DRAWING_ANALYSIS_BYTES` (8 MiB), is refused) and counts it with the local tokenizer. The block is `wrap_attachment(DIGEST_ATTACHMENT_LABEL, ...)` with a first line `Drawing analysis file: <name>`. `wrap_attachment` escapes any body line that starts like a BEGIN/END ATTACHMENT marker with a leading backslash (`escape_attachment_markers`, idempotent; the analysis is escaped on read so its count is the count the readout shows), so no attachment, this one or a context file, can close its block early; the digest regexes require a marker at line start. `DIGEST_ATTACHMENT_LABEL` (`Construction Drawing Digest`) is a schema string: it gates drawing impact, and a pending record saved by a build that still digested PDFs carries the same label, so a resumed run keeps its pass. The merge is refused over `PROJECT_CONTEXT_MAX_TOKENS`, never truncated.

The FILES-panel readout is a pure function of the textbox (`context_attachment.drawing_analysis_readout`): one row per block with the live count of its body without the source line, re-derived on every settled edit and memoized by block text; nothing else remembers what was attached, so a block the operator deletes by hand disappears from the readout. The flow takes no credential, makes no request, and shares the attach-files running flag. `OPERATION_DRAWING_DIGEST` stays in the attempt vocabulary only so an older diagnostics export still prices under its own label; no current path records it.

### Drawing-impact synthesis

Runs after round-2 verification, only when `extract_drawing_digest` finds a `Construction Drawing Digest` attachment block. EX-06, when on, is the only later stage. The gate is block presence, not `project_profile_enabled`. It does not run while a review repair is outstanding. Unknown finding ids are dropped at parse time. A failed pass is an amber note, not "the drawings were ignored." The digest is the operator's text in the analyzer's own citation form: the prompt tells the model to copy the digest's sheet or page references verbatim and never invent one, and the few-shot examples use one placeholder form (`[<file> p.N]`) and say so.

### Token preflight raises (not warns): request budgets

`core/request_budget.RequestBudget` sizes the request that will be sent (`count_request_from_params` from the phase's one builder). `count_source` is `api_estimate` (Anthropic `count_tokens` — a provider estimate, never "exact"), else `local_padded`, else `unavailable`. Unavailable never fits. Nothing is sent without a size.

`input_ceiling = min(phase_limit, context_window − max_tokens − 5% reserve)`. Practical limits: review `RECOMMENDED_MAX` (500k), cross-check and compliance `CROSS_CHECK_RECOMMENDED_MAX` (822k). Padding follows the tokenizer: Opus 5.5 / Opus 5 / Opus 4.8 / Sonnet 5.5 / Sonnet 5 use 1.45×; Sonnet 4.6 uses 1.10×; Haiku 5.5 uses 1.50×; Haiku 4.5 uses 1.15×; unknown uses 1.50×. The factor is clamped to at least 1. An API estimate is never overruled by the padded guess. Valid API estimates are cached per process by counting-form digest, model included.

Review extended output (300k) is batch-only, and only when the counted input is at least 200k on a beta-whitelisted model. A rejected `BATCH_OUTPUT_BETA` header clamps to the model's ordinary ceiling and resubmits once. `assert_extended_output_allowed` fails a request built above that ceiling without the header.

Cross-check and compliance chunk when the whole package does not fit. Count calls for those passes use the no-retry client, one attempt, and take `call_gate` per call. A failed count falls back to the padded estimate.

### Model capability whitelist

`api_config.model_capabilities` is the source of truth. Unknown model ids disable every capability flag and log one warning. Do not send `thinking: disabled`. Do not send a forced `tool_choice` to Opus 5.5 or Sonnet 5.5. `strict: true` goes only to models with `supports_strict_tools`. Web fetch stays off unless `supports_web_fetch` (off for Opus 5 and Opus 5.5).

Default phase effort is at most `high` (review, research, drawing impact; cross-check and compliance `medium`, verification `medium`). Every request to a model in `OPUS_MODELS` is then held to `OPUS_EFFORT_CEILING` (`medium`): the escalation tier, and a review pinned to Opus. The EX-03 review-effort override is the exception and is not a phase default. `xhigh` is clamped to `high` on models without `supports_xhigh_effort`. Cross-check and compliance have no model env override; they use `CROSS_CHECK_MODEL_DEFAULT` / `COMPLIANCE_MODEL_DEFAULT` (Sonnet 5.5). The separate applier's `--assist` sends effort `medium` where the model accepts it, with a 16k output cap (`applier/assist.py`).

Output ceilings come from the whitelist (Opus 5.5 / Opus 5 / Opus 4.8 / Sonnet 5.5 / Sonnet 5 / Haiku 5.5 = 128k; Sonnet 4.6 / Haiku 4.5 / unknown = 64k). An unregistered phase silently caps at `UNREGISTERED_PHASE_OUTPUT_CAP` (16k).

### Verification cache key

`cycle_label | standards_fingerprint | actionType | codeReference | sha256(claim_summary)` (24 hex), plus `| jurisdiction_fingerprint` only when the run has a `ProjectProfile`, plus `| gb:<fingerprint>` only when the governing basis was rendered. A profile-less California run stays the five-segment key. The key omits the verifier model. `standards_fingerprint` is the edition lines, not pin provenance. See "Cache namespace" and "Researched-context expansion."

### Verification outcomes: one classification contract, one cache predicate

Three distinctions: an operational **failure** (nothing was reliably checked), the verifier's **uncertainty** (a well-formed `UNVERIFIED`), and a **reusable verdict** (a grounded conclusive one).

`verifier.classify_verification_turn` classifies a finished conversation for both transports. Incomplete stops, a finished turn with no successful search or fetch, a missing submission, and an unreadable submission are failures (`FAILURE_OUTCOMES`): `verification_failed=True`, ungrounded, not repaired, not escalated, not cached, not shared. A well-formed verdict is the only `OUTCOME_VERDICT`. `should_escalate_verification` never escalates a failed pass. A failed escalated pass does not replace the first pass.

`verification_cache.cache_ineligibility_reason` is the one predicate at `put`, `get`, and `load_from_disk`. Reusable means a grounded `CONFIRMED` / `CORRECTED` / `DISPUTED` with a substantive accepted citation and, for `CONFIRMED` / `CORRECTED`, a non-blank quote. Refused: every `UNVERIFIED`, every failure, budget exhaustion, local classification, a replay (`hit` / `shared`), and (EX-04) a result with `reused_sources`. Contested conclusive verdicts stay cacheable. Invalid rows are ignored one by one (`rejected_on_load`); the next save writes only what loaded.

A well-formed `UNVERIFIED` (`outcome == OUTCOME_VERDICT`) is shared in-process for the run (`cache_status="shared"`) and verified again on a later run. Followers never inherit a failure, a shortfall, a local classification, or anything the verifier did not stamp as a verdict. A follower's wait is bounded (`SPEC_CRITIC_VERIFICATION_SINGLEFLIGHT_WAIT_SECONDS`, default 900).

One reminder to submit, for the verifier and for research: a finished turn that retrieved evidence and submitted nothing readable gets one appended user message. It is not a pause, and it does not edit prior blocks (preserved thinking). A second empty turn fails.

### Native citations: attribution, not support

`src/verification/native_citations.py` records the API's own citations beside the verdict. They are attribution, not retrieval and not semantic support. Document indexes resolve only against `web_fetch` documents that appeared earlier in that conversation; a contradiction leaves the record `unresolved` with `url=""`. `native_citations is None` means not captured; `[]` means the conversation was read and had none. Persisted additively; eligibility ignores the field. Both reports use `report_exporter._evidence_concepts`: retrieval, native attribution, and semantic support ("not checked by this app") are separate sentences.

`<web_fetch_usage>` allows any URL already in the conversation (the finding, or an earlier search or fetch result), never a URL that appears only in the system prompt or only in the model's own output. A finding-supplied URL is a lead, not evidence.

### Cache-write accounting (per-TTL)

Five-minute cache writes bill at 1.25×, one-hour writes at 2×, reads at 0.1×. The app's own breakpoints are one-hour. Server tools insert their own five-minute breakpoints. The invariant is `known_5m + known_1h + unknown == aggregate`. Missing detail is unknown, never zero. The aggregate is never charged on top of its components. `api_config.extract_cache_usage` is the only parser. `estimate_cost_breakdown` prices an omitted unknown remainder as the aggregate minus the declared components, so a partial breakdown is not double-charged. When no components are supplied, the whole aggregate is priced at 2×, which keeps older callers' numbers.

Haiku 5.5 rates are $0.10 input / $0.50 output per million tokens for total prompt size ≤100,000, and $0.50 / $2.50 above it. The selected tier applies to the whole request, including output and cache rates. Prompt size counts uncached input plus cache writes and reads once; TTL components partition the cache-write aggregate. `price_for(model, prompt_tokens=...)` selects a tier; `estimate_cost_breakdown` derives prompt size from usage. Searches retain the $0.01 per-search fee and get no batch discount. Static UI summaries and the trust table disclose both tiers.

### Attempt accounting

One `AttemptUsage` per paid request (`core/attempt_usage.py`). A read response has known usage. An errored, canceled, or expired batch item is a known zero. A request that raised before its response was read, or a batch item never read, has `usage_known=False` and no counters — counted, never priced, never shown as zero. Scope `earlier` is spend billed before this collection started (a resumed primary batch, a repair an earlier collection submitted). Scope `run` is everything else.

`DiagnosticsReport` copies each event's billing input into a ledger that event caps never evict. An event with `attempts` is priced from those records; its flat totals are display only. `diagnostics.cost_summary_lines` is the only wording of the estimate. Shared single-flight followers and cache replays bill nothing. Both collection drivers record through `record_pass_api_call`, `record_verification_findings`, and `review_pass_extra`. The combined review carrier is recorded only on the batch transport; the real-time runner already records one row per call, and collect must not record those again. Triage has no result carrier; both drivers pass `triage_usage_sink`. A recovered review batch's usage is in the recovery figure as earlier spend. Research usage from the original session is not in pending state, so recovery cannot reconstruct it.

### Paid repair recovery

`collect_review_batch_results` sets `CollectionOutcome`. `reportable` means at least one spec produced a usable review. `remote_settled` means no repair is outstanding. `pending` (still processing) and `unreachable` (status or results could not be read) are outstanding. Once `create` returned an id, a later poll, retrieve, or exception is `unreachable`, not `not_submitted`. Only an expired, failed, or canceled saved repair is replaced. A pending or unreachable repair leaves the primary results in place and submits nothing.

A provisional run defers finding verification, cross-spec coordination, compliance, drawing impact, and (when on) EX-06. A routed program collects every module's review and repair first, and runs dependent stages only when no module is outstanding. One module's pending repair holds the others. `ProgramPipelineResult.status` is `partial` while provisional.

`decide_saved_state_cleanup` keeps the record when a module could not be collected, when a module recorded no outcome, when a repair is pending or unreachable, or when every submitted spec failed review. Otherwise the record is cleared unless `--keep-state`. Real-time runs do not use this rule. `clear_saved_state_for` deletes the file only when every primary batch id belongs to this run and every repair id is one this run settled. A record this build cannot read is never deleted. Pending-file read-modify-write takes one process lock (`record_repair_batch`). Two processes are not coordinated; the identity check is what keeps one from deleting the other's record.

Re-attach uses the saved repair id and request map and does not need local files. A kept record is re-stamped with any repair id that failed to save. Bare-id recovery of an outstanding repair adopts a record only when the state file is free. `--module` is required on the bare-id CLI path. `ensure_batch_ended` runs before a bare-id results read.

The report, sidecar (`provisional`, `collection`), and diagnostics event carry the outcome only as additive fields. A settled run's report shape stays what it was. Tell the operator to collect again. Do not advise a separate re-run of a spec whose repair is still outstanding.

### Run credentials and optional tracing

A key typed into the GUI never enters `os.environ`. `core/credentials.py` holds an `ApiCredential` for the run, bound with `run_with_credential` / `bind_credential`. A pool worker does not see its submitter's context: every executor submission under `src/` that can reach the API passes `bind_credential`. Child processes the app launches use `credentials.child_process_env()`, which drops `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`. Resolution: a bound credential wins; otherwise the environment key, which is the command-line path.

Tracing is optional. `start_run_recorder` / `reattach_run_recorder` / `stop_run_recorder` never raise. `clear_recorder` clears only its own recorder. While a deep recorder's writer is running, models with `supports_thinking_display` get `thinking.display: "summarized"`. Every other request is byte-identical. The field changes visibility, not the bill.

**Secret redaction.** Before serialization, `tracing.redaction.scrub_data` and diagnostics (`_scrub_and_bound`, `DiagnosticsReport.log` messages, and `TraceRecorder.prompt_ref`) replace recognized credential patterns (`sk-ant-`, `Bearer `, `AKIA`) in messages, nested payloads, and captured prompts. A container past the depth bound collapses to a marker; a scalar past the bound is still scrubbed. The prompt digest is taken from the stored text. Spec text is kept in full. Matching is those prefixes only, and traces already on disk stay as written.

### Other contracts that are easy to break silently

**Web-fetch for follow-up reads.** The tool is `web_fetch_20260209` and it takes no `anthropic-beta` header. An unrecognized beta value is HTTP 400. Attach it only on `standard_reasoning` and `deep_reasoning`, and only when `supports_web_fetch` is true (off for Opus 5 and Opus 5.5). The fetch budget is `DEFAULT_VERIFICATION_MAX_FETCHES` (3). Which URLs the prompt allows is under "Native citations."

**Server-tool containers.** `web_search_20260209` / `web_fetch_20260209` pause inside a code-execution container. A resume must send the bare container id string (`apply_container_config`). Keep the last id seen. The three sites are the research loop, `verifier._run_verification_call`, and `build_verification_request`. No id means write nothing, so a first request stays byte-identical. The HTML chat carries the id the same way, within one turn.

**Fetched-PDF resend.** `resend_sanitizer.sanitize_messages_for_resend` runs at every continuation resume. Past 600 PDF pages it elides fetched PDFs and drops every later `thinking` / `redacted_thinking` block. A conversation that fits returns the same list object.

**Retry ownership.** App-level loops call `reviewer._get_client(sdk_retries=False)`. `Retry-After` is a floor and is never shortened to fit the wait budget. Only `RATE_LIMIT`, `SERVER_ERROR`, and `CONNECTION` retry. Spend-cap 429 and `INVALID_REQUEST` (including 400) do not. Permits are per outbound call and are released before any wait. Batch submit and the review `count_tokens` preflight stay on the SDK. Cross-check and compliance budget counts are one attempt on the no-retry client.

**Resource-pressure telemetry (observation only).** `core/resource_pressure.py` is a stdlib-only leaf. It records the waiting a run did — retry waits and calls given up on (`RetrySchedule`, labelled per loop; the batch poll's error waits under `batch_poll`), permit contention (`MeteredSemaphore`, one per pool: `review`, `verification`, `research`, and `collection`), single-flight follower waits (`pipeline._wait_for_singleflight_leader`), batch poll outcomes, and every HTTP response's status plus `anthropic-ratelimit-*` headroom (an httpx response hook `reviewer._build_sdk_client` attaches to the SDK's HTTP client after construction, so request bytes, SDK retries, and timeouts are untouched). Nothing it records changes a request, a wait, a permit, a decision, or a verdict; every `record_*` is a no-op with no recorder and never raises. `DiagnosticsReport` owns one `PressureRecorder` (`pressure`); `start_pressure_recording` installs it process-wide (both GUI run starts, and `scripts/recover_batch.py` before its batch poll) (pool threads do not inherit context variables; one run per process, as tracing assumes) and `finish` removes only its own. The ledger is never evicted; `summary()["resource_pressure"]` is the block, `observed` / `signals` the verdict, and a few occurrences also become `warning` timeline events whose data never reads as an API call. A capacity-class stop (`rate_limit` / `server_error` / `connection`) that could have retried is "given up on"; a refused request, a parse re-request, or a cancelled loop is not. Permit waits under `PERMIT_WAIT_FLOOR_SECONDS` (10 ms) are not waits; one wait of `PERMIT_SIGNAL_SECONDS` (1 s) makes local concurrency a signal; headroom under `LOW_HEADROOM_FRACTION` (10%) is low. Reading the headers adds no request.

**JSON readers.** Text fallbacks take the last JSON value of the expected shape (`structured_schemas.json_values_in_text`), not the first `{` through the last `}`. A tool whose name differs only by letter case is that tool (`tool_name_matches`). A different word is not.

**Escalation disagreement.** `models_disagreed` is set only when both passes are grounded, both verdicts are conclusive (`CONFIRMED` / `CORRECTED` / `DISPUTED`), and they differ. A grounded `UNVERIFIED` followed by a conclusive escalation is the escalation working. `classify_status` checks the sentinel before the verdict, so a contested finding stays `VERIFIED_CONTESTED`.

**Budget exhaustion.** `budget_exhausted` marks an `UNVERIFIED` that used its full search budget. Status stays `INSUFFICIENT_EVIDENCE`. The cache refuses it. The flag is not persisted.

**One code basis per module.** The California module is California 2025 only. Do not reintroduce a 2022 cycle on it. A different basis is a different module. `module_id` and the cycle label are permanent once shipped: pending state refuses a module id that no longer resolves or a module whose cycle label changed.

**Module registry.** `validate_module_registry` fails at import. Never relax it to fit content. California goldens and routing pins stay byte-identical. Registering a module does not list it in the GUI; programs do (`AVAILABLE_PROGRAMS`). Few-shot examples must anchor every action type and at least one `CRITICAL`, and must not mention element ids (they sit in the cached system prefix). Verifier source tiers are one tuple; the rendered order is the tier order.

**Routing.** `programs/routing.route_spec` reads the document's own SECTION heading (`input/section_identity.py`) plus the file name. Two credible numbers, or a file name and a document that name different module sets, are `AMBIGUOUS` (confidence 0.50). Headless, ambiguous routes are refused. Division 27 and non-alarm Division 28 stay unsupported.

**One input per file.** `input_files.unique_spec_inputs` refuses two different files with the same case-folded name (`BasenameCollisionError`) before any spend. The same file given twice is one input.

**Location-aware pipeline.** Flag off means every California-facing surface stays byte-identical. Research runs inside `start_batch_review` before review spend and is not re-run on resume. All dimensions failing aborts before review spend; a partial profile continues. Compliance runs only when the flag is on and a structured profile is present. Only grounded `spec_requirement` items are controlling. An `ADD` stays executable only when a controlling requirement is an established `missing` (`origin="model"`). Every other `ADD` is held as `REPORT_ONLY` with a reason that quotes the insertion. Zero expected requirements is a completed `no_applicable_items` result, not a red skip. Missing completeness metadata renders as not recorded, never as complete. One `ProjectProfile` per run.

**DOCX supplemental content extraction.** `extract_text_from_docx` shows the displayed view, including automatic numbers as labeled prefixes with recorded spans (`label_spans`). `source_text` is the document's own text. Reconstruction (`content` vs paragraph map, and the label spans) raises on mismatch. Content controls, fields' stored results, smart tags, and hyperlinks are read and are not writable by the applier. A Word TOC gallery control is skipped. Equations, `w:altChunk`, legacy drop-down form fields, and tables inside a control within a table row or cell are not read and each kind adds one extraction warning that counts elements. SmartArt, and tables inside headers, footers, text boxes, and notes, are still not read and do not warn. The content-loss warning fires when the share of body children containing a drawing, picture, or OLE object is strictly greater than 0.20.

**Stale-cycle suppression.** Only cues that reject the citation itself suppress it, inside an 80-character, clause-bounded window. A negated duty to comply is still a requirement. Coordinated citations joined only by comma / and / or are one group. Location-aware modules skip the detector entirely. Placeholder and keyword lists match whole words; `TBD-200` is an identifier; `formatting` is not a local-skip keyword.

**Edit-action labels.** `classify_edit_action` is "is there a proposal?" — `EDIT_SUGGESTED` or `REPORT_ONLY`. There is no confidence gate. Verification status travels beside it for the applier.

**Confidence vs verdict.** `Finding.confidence` is the review model's pre-verification number. When the status is in `VERDICT_SUPERSEDES_CONFIDENCE`, the report shows it as a footnote. Membership follows the classified status, so an uncited `DISPUTED` keeps the percentage prominent. `<review_scope>` still has a certainty sentence; replacing it is EX-03 and is off.

**Review-failure honesty.** A failed spec produces zero findings. `PipelineResult.failed_review_specs` is what the report, title block, and file list use. "No issues found." is green only when every submitted spec was reviewed and no repair is outstanding. A model refusal is not a truncation and is not repaired on either transport. A run with `review_result.error`, a provisional collection, or a failed report export ends amber.

**Program result integrity.** After spend, bad re-hydrated coverage figures are warned and clamped, not raised. `integrity_warnings` make `status` `"partial"` and appear on the banner and the program sidecar.

**Windows release literals.** `packaging/windows/check_release_version.py` requires the git tag to equal `pyproject.toml`, `src/__init__.py`, README.md's `**vX.Y.Z**` headline, this file's title line, and the `# Package version (X.Y.Z)` note in the source-layout tree. The updater accepts only https manifest and installer URLs, including after redirects, and promotes the download only after SHA-256 matches.

**Diagnostics wording.** `cost_summary_lines` is the one cost paragraph and `resource_pressure_lines` the one resource-pressure paragraph (text export, GUI window, recovery CLI). The Run Diagnostics banner is `_summarize_run_diagnostics` (program reports use `_program_run_diagnostics` once, at the top). Conditional rows (failed review, provisional collection, integrity warnings, incomplete compliance) appear only when they apply, so a clean run stays stable.

---

## 3) Verification routing

`verification_routing.select_routing` is the pure selector. Profile order: internal-coordination, then jurisdictional, then manufacturer, then code-standard (or any non-empty `codeReference`), then constructability. Search budget is severity-only (`api_config._SEVERITY_MAX_USES`): CRITICAL 8, HIGH 7, MEDIUM 5, GRIPES 3. The prompt asks for the search; the tool enforces the budget.

| Mode | When | Model | Effort | web_fetch | Escalates |
|---|---|---|---|---|---|
| `local_skip` | Keyword or Haiku triage | none | — | no | no |
| `strict_structured` | GRIPES, or non-GRIPES internal coordination | Sonnet 5.5, fixed | low; thinking key omitted (adaptive thinking stays on) | no | no |
| `standard_reasoning` | Default substantive claim | Sonnet 5.5 | medium | yes, if the model allows it | yes |
| `deep_reasoning` | Escalated, or the initial pass of a CRITICAL jurisdictional finding | Opus 5.5 | medium (Opus ceiling) | no on Opus 5.5 / Opus 5 | no |

Haiku triage never runs on CRITICAL, HIGH, or any finding with a `codeReference`. On API or parse failure, refusal or truncation every finding in the chunk is `web_required`. Default Haiku 5.5 requests adaptive thinking with medium effort and a 16k output cap; legacy/unknown triage overrides retain omitted thinking/effort and an 8k cap. Triage is the only phase that forces its tool, and only on Haiku (`supports_forced_tool_choice`). Haiku 5.5 accepts the forced classification tool with adaptive thinking; forcing can suppress up-front thinking, so this does not promise a think-before-classify step. No live triage accuracy comparison is recorded.

Real-time fallback: when a batch retry tail is under 5 findings, the rest run synchronously. Every finding ends with exactly one `VerificationResult`. The batch continuation check is `count > cap` (not `>=`), matching the real-time pause budget. `MAX_VERIFICATION_WAVES` is 3. A continuation cap is `INSUFFICIENT_EVIDENCE`, not `VERIFICATION_FAILED`.

`user_location` is a web_search parameter only. It must not be copied onto web_fetch or into the governing-basis prompt path. `None` falls back to the module's `default_web_search_user_location` (California's historic dict; data-center modules `None`).

---

## 4) Trust model / report output

`report_status.py` is the closed set. `classify_status` is the implementation; this is the contract.

| Status | When |
|---|---|
| `VERIFIED_SUPPORTED` | `CONFIRMED`, grounded |
| `VERIFIED_CONTRADICTED` | `CORRECTED`, grounded |
| `DISPUTED` | `DISPUTED`, grounded, with an accepted citation |
| `INSUFFICIENT_EVIDENCE` | Well-formed `UNVERIFIED`, a demoted verdict, or a budget terminal. Never a failure |
| `LOCALLY_CLASSIFIED` | `local_skip` |
| `NOT_CHECKED` | Verification did not run |
| `VERIFICATION_FAILED` | `verification_failed=True` (`FAILURE_OUTCOMES`). Not cached, not shared |
| `VERIFIED_CONTESTED` | `models_disagreed=True` |

`VERIFICATION_OUTCOME_GROUPS` and `verification_outcome_sentence` keep verified, inconclusive, operational failure, locally classified, and not checked apart. Do not collapse the last four into "could not be verified."

Evidence panels separate retrieval, native attribution, and semantic support. Rejected URLs render with `describe_rejection`. Cache replays show the entry age. The configured cache path is on the panel so one entry can be deleted.

---

## 5) Deterministic pre-screen

Detectors run before any API call. Each alert has a public `deterministic_rule` id. The implementation is `input/preprocessor.py`; the non-obvious rules are in §2 (stale-cycle suppression, whole-word placeholders, location-aware modules skip stale-cycle). Rule ids: `leed_reference`, `placeholder`, `template_marker`, `stale_code_cycle`, `stale_asce7`, `invalid_code_cycle`, `empty_section`, `duplicate_heading`, `duplicate_paragraph`, `inconsistent_filename`, `wrong_polity_token`. Stale and invalid year sets are disjoint by construction (`plausible_cycle_years ⊆ valid_cycle_years`). Headings come from `input/headings.py` (PART or dotted article numbers with a title-shaped title; a bare integer is never a heading). An empty heading is reported only when its parent is not empty.

---

## 6) Token budgets

Caps live in `api_config._PHASE_OUTPUT_BUDGET` and clamp through `phase_output_cap`. Thinking counts toward `max_tokens`. A higher cap costs nothing unless it is used.

| Phase | Cap |
|---|---|
| Review | 128k (300k batch-only when the counted input is ≥ 200k) |
| Cross-check | 96k |
| Compliance, research, verification | 64k |
| Drawing impact | 32k |
| Coordination experiment, unregistered phase | 16k |
| Triage | 16k for Haiku 5.5; 8k for legacy/unknown overrides |

Context limits: `MAX_CONTEXT_TOKENS` 1,000,000, review `RECOMMENDED_MAX` 500,000, cross-check and compliance 822,000. Each request is also held to the model's window minus its `max_tokens` minus a 5% reserve. Local padding: 1.45× on the Opus 4.7-family tokenizer (the models named in §2), 1.10× Sonnet 4.6, 1.50× Haiku 5.5, 1.15× Haiku 4.5, 1.50× unknown.

---

## 7) Prompt caching

`api_config.cache_policy_for(phase)` places the app's breakpoints. Their TTL is `1h`. Two other writes exist: the five-minute automatic `cache_control` on real-time `pause_turn` resumes (§2), and the five-minute breakpoints server tools insert after their results.

| Phase | Cached |
|---|---|
| Review, cross-check, verification | yes |
| Research, compliance, drawing impact, coordination experiment | system + tools |
| Triage | no — the short classification prefix remains uncached; on legacy Haiku 4.5 it is below the 4,096-token Haiku 4.5 cache minimum. No Haiku 5.5 cache-minimum claim is made. |

`SPEC_CRITIC_CACHE_DIAGNOSTICS` (off) requests cache-prefix diagnosis on the synchronous verification loop only. Default off means a byte-identical body.

---

## 8) Environment variables

Boolean flags accept `0` / `false` / `no` / `off` to disable. Experiment switches in §2 have no truthy shorthand unless that row says otherwise. The parsers live in `src/core/api_config.py`, `src/core/credentials.py`, and `src/core/ui_state.py`. Overrides that change request bytes:

| Variable | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Command-line key only. The GUI never writes it. |
| `SPEC_CRITIC_REVIEW_MODEL` | Sonnet 5.5 | Review model (`claude-opus-5-5` restores the previous default). |
| `SPEC_CRITIC_VERIFICATION_MODEL` | Sonnet 5.5 | Initial verifier. |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | Opus 5.5 | Escalation model. |
| `SPEC_CRITIC_RESEARCH_MODEL` | Sonnet 5.5 | Research fan-out. |
| `SPEC_CRITIC_DRAWING_IMPACT_MODEL` | Sonnet 5.5 | Drawing-impact synthesis. |
| `SPEC_CRITIC_TRIAGE_MODEL` | Haiku 5.5 | Triage; Haiku 4.5 remains supported. |
| `SPEC_CRITIC_GOVERNING_BASIS_CONTEXT` | off | Render the basis into the verifier. See §2. |
| `SPEC_CRITIC_STRICT_TOOL_USE` | on | Drop `strict: true` from tool schemas when disabled. |
| `SPEC_CRITIC_ELEMENT_IDS` | on | Disable for legacy plain-body spec rendering. |
| `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` | 60 | `0` means no expiry. Malformed values stay 60. |
| `SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES` | 5000 | `0` disables the LRU cap. |
| `SPEC_CRITIC_*_PATH` / `SPEC_CRITIC_TRACE*` | see `api_config` and `tracing/config.py` | State locations and trace retention. |

Worker caps (`SPEC_CRITIC_REALTIME_REVIEW_WORKERS`, `SPEC_CRITIC_RESEARCH_WORKERS`, `SPEC_CRITIC_PROGRAM_PREPARE_WORKERS`, `SPEC_CRITIC_PROGRAM_COLLECTION_WORKERS`, `SPEC_CRITIC_REALTIME_COLLECTION_CALLS`) bound in-flight calls. Permits are released before retry sleeps. There is no env override for the cross-check or compliance model.

---

## 9) Test harness

Hermetic by default: no API key, no network. `tests/conftest.py` blocks live `Messages.count_tokens` outside `@pytest.mark.network` and clears the count cache per test. GUI tests are import-guarded. `tests/test_plan_open_defects.py` is regressions only — since S18 no defect reproduction is an open xfail. A new defect gets a new strict xfail (`raises=AssertionError`); deleting the check is not a fix.

Pins that fail the build when relaxed: applier import isolation, module-registry validation, California golden bytes, release version literals (this file's title and `# Package version (X.Y.Z)` note), the Haiku cache-minimum statement in §7 (4,096), and the occurrence / sidecar / extraction contracts named in §2. `node --check` parses the HTML report's script; CI sets `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS`. The Ask AI chat is driven under Node against scripted streams. No live chat run against the API is recorded (§10).

**Live-fixture oracles.** Every capture under `evals/calibration/fixtures_live/` has a record in the adjudication ledger (`evals/calibration/oracle_reviews.py`, stored outside that directory). A resolved record carries a verdict and a status. An unresolved record carries a reason and no label. `validate_against_fixtures` reports an error — never a quiet exclusion — for a missing record, a record with no fixture, an evidence-digest mismatch, and a resolved oracle that disagrees with its fixture file. The digest covers `IMMUTABLE_FIXTURE_KEYS` only, so changing a label leaves the record valid and editing the capture does not. `--reviewed-only` writes that scope into the score output.

---

## 10) Open items

Retired plans are cited by tag in source comments. They are not in the tree. Read one with `git show <commit>:<path>` (`01781ed` for the hyperscale and data-center plans and `TRUST_AUDIT.md`; `f14eb27` for the review-implementation plan). Do not treat a tag as a live spec.

These stay open because the repository cannot produce the evidence. Do not close them by turning a flag on or by editing a prompt:

- **`SPEC_CRITIC_GOVERNING_BASIS_CONTEXT`** stays off until `evals/dc_applicability.py` is run and incorrect confirmations and incorrect disputes are reported separately. `EVALUATION_PROTOCOL["status"]` is NOT RUN.
- **`DEFAULT_BASIS_TOKEN_BUDGET` (4,000)** has not been measured against real saved profiles. Do not present a character count as that measurement.
- **EX-01 through EX-07** are not evaluated. Protocols are in `plans/experiments/`. Recovery exports include the recovered review batch as earlier spend, and omit the original session's research, because pending state does not store it.
- **Ask AI has no recorded live API run.** Hermetic tests do not catch a request the live API rejects. Opus 5.5 must not be sent `web_fetch`.

Scoped out on purpose: California's eight `UNVERIFIED` pins; structural detectors for duplicate article numbers, empty lettered paragraphs, and doubled words; engine-global local-skip vocabulary (`leed` still local-skips a data-center GRIPES finding); one location per run; no deterministic Canadian code-year check.

`SPEC_CRITIC_GOVERNING_BASIS_CONTEXT=0` is the rollback that strands `gb:` cache entries (they remain on disk and count against the LRU). The edition-authority correction has no flag. Attempt records and per-TTL cache breakdowns are runtime telemetry; reverting the code does not require a cache flush.

---

## 11) Dependencies

Python 3.11+ (`pyproject.toml`). Runtime pins are `requirements.txt` (`anthropic`, `python-docx`, `customtkinter`, `tkinterdnd2`, `tiktoken`, `platformdirs`, `pypdf`, `pydantic`, `lxml`, `keyring`, plus `truststore` for the frozen updater). `applier/` adds no dependency. Test tooling is `requirements-dev.txt`. `keyring`'s import is guarded. `importlib_metadata` and `backports.tarfile` are markers for Python < 3.12 only.
