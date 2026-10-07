# Trust claims ledger

Discovery completed before writing the replacement trust copy. Scope: the
Spec Critic desktop reviewer, its exported HTML report and offline trace viewer,
and the shipped recovery, trace and separate applier commands. Evaluation scripts,
packaging tools and Python library entry points are developer tools, not desktop
actions. Reader: the design professional who must decide whether to act on a finding.

The inventory has **40 user-action families**, **13 automatic-behavior families**,
and **8 network routes** (including dynamic destinations). Related controls that
execute the same mechanism share a card; the triggers are spelled out below.
Default model identities are **3**: Opus, Sonnet and Haiku. There are **8** registered
model identities including older models; arbitrary environment/CLI overrides remain
possible. Counts are derived by `test_trust_inventory_complete` and
`test_trust_models_and_settings`; they are inventory counts, not API-call ceilings.

## Claim register

Every block in `src/gui/trust_content.py` carries these claim IDs. A row covers
the enumerated statements, including their limitations; it does not authorize a
stronger paraphrase. File paths below are relative to the repository root.

| ID | Verified fact and exact scope | Implementation: file — symbol | Verification |
| --- | --- | --- | --- |
| C01 | Advisory findings; no approval decision or proof of compliance. Mechanical gates can be reproduced; model judgments cannot be reproduced reliably. | `src/output/report_status.py` — `classify_status`, `classify_edit_action`; `src/review/reviewer.py` — `Finding` | `test_report_status.py`; manual source audit |
| C02 | Origins include supplied specs/context/profile, module rules, local alerts, model review/research/coordination/compliance/drawing interpretation, retrieved evidence and cached verdicts. Origin labels describe a stage, not the origin of every phrase. Local classification includes AI triage. | `src/input/preprocessor.py` — `preprocess_spec`; `src/modules/base.py` — `ReviewModule`; `src/review/reviewer.py` — `Finding`; `src/output/html_report_exporter.py` — `_serialize_finding`; `src/verification/triage.py` — `filter_local_skips` | `test_report_status.py`, `test_edit_occurrences.py`; manual |
| C03 | Extraction, routing, pattern checks, edit-shape/available-text anchor checks, deduplication, evidence matching, pricing and exports are local. GUI changes can trigger remote counting. | `src/input/extractor.py` — `extract_text`, `extract_context_text`; `src/programs/routing.py` — `route_specs`; `src/review/reviewer.py` — `validate_edit_shape`, `validate_finding_anchors`; `src/orchestration/pipeline.py` — `_deduplicate_findings`; `src/verification/source_grounding.py` — `normalize_url`, `validate_cited_sources`; `src/core/pricing.py` — `estimate_cost_breakdown` | existing extraction, routing, anchor, source-grounding and pricing suites; manual |
| C04 | No periodic autonomous review. Startup loads preferences/key/pending state, offers resume, then Windows update check. File/context/program selection schedules counting; attaching a drawing analysis reads and token-counts a text file locally (no upload) and then schedules the same counting. Started runs launch downstream work/retries without further clicks. | `src/gui/gui.py` — `main`, `SpecReviewApp.__init__`; `src/gui/token_analysis_controller.py` — `analyze_tokens`, `refresh_exact_token_count`; `src/gui/context_controller.py` — `attach_drawing_analysis`; `src/gui/update_controller.py` — `maybe_auto_check_for_updates`; `src/gui/batch_controller.py` — `collect_batch_results` | `test_token_analysis_gate_and_threading.py`, `test_context_controller_background.py`, `test_updates.py`; manual |
| C05 | Exact default/override models, settings, output caps and tool shapes are in API config and request builders. Temperature is omitted. Default Haiku 5.5 triage requests adaptive thinking and medium effort with a forced classification tool (which can suppress up-front thinking); legacy triage overrides and separate applier assistance omit explicit effort/thinking; strict verifier omits explicit thinking and sends low effort. Deep traces request summarized thinking where supported. | `src/core/api_config.py` — model constants, `phase_output_cap`, `effort_config_for`, `thinking_config_for`; `src/verification/verification_modes.py` — `mode_policy`; `src/review/review_request_builder.py` — `build_review_request`; `src/research/requirements_research.py` — `build_dimension_request`; `applier/assist.py` — `assist_location`; `src/output/html_report_exporter.py` — `_build_chat_config`, `_CHAT_JS` | `test_trust_models_and_settings`, existing capability/effort/request-shape tests; manual |
| C06 | Polling/retry/continuation/concurrency/request bounds are per phase or request, not a whole-run price/time cap. Remote batches outlive local polling/window close; GUI has no review Stop button. Failure preserves paid primary repair results; exports are not transactional across sidecars. | `src/batch/batch_runtime.py` — `PollPolicy`, `poll_batch_bounded`; `src/verification/retry_policy.py` — `RetryPolicy`, `RetrySchedule`, continuation constants; `src/verification/verifier.py` — `collect_verification_batch_results`; `src/orchestration/pipeline.py` — `_recover_retryable_review_batch_results`; `src/gui/gui.py` — `_on_close`; `src/gui/report_controller.py` — `_write_report_and_sidecars` | retry/poll/repair/close/export suites; `test_trust_fact_sources`; manual |
| C07 | Verified/disputed verdicts need accepted citations matching normalized searched/fetched URLs (or supplied evidence when source-reuse experiment is enabled). Confirmed/corrected verdicts need a source quote, but default gates do not prove that quote appears on the page or supports the conclusion. Cache and report repeat eligibility gates; operational failures differ from insufficient evidence; grounded disagreement may be contested. | `src/verification/verifier.py` — `_enforce_grounding_invariant`, `_apply_source_grounding`, `_demote_if_missing_source_quote`, `should_escalate_verification`; `src/verification/verification_cache.py` — `cache_ineligibility_reason`; `src/output/report_status.py` — `classify_status`; `src/verification/source_reuse.py` — `SourceStore` | grounding, source-quote, contested, cache-eligibility and escalation tests; manual |
| C08 | Reviewer emits edit instructions. Anchor checks accept exact or collapsed-whitespace substrings; unavailable/unattributed file text is skipped. Separate applier can edit a copy, defaults to tracked/conservative, can be overridden, optional AI chooses location, not replacement wording. Report exports accept an input path and can overwrite it; same-stem sidecars have no separate overwrite prompt. | `src/review/reviewer.py` — `validate_finding_anchors`, `validate_edit_shape`; `src/output/edit_sidecar.py` — `write_edit_instructions_sidecar`; `src/gui/report_controller.py` — `_write_report_and_sidecars`, `export_html_report_to_file`; `applier/cli.py` — `build_parser`; `applier/run.py` — `apply_sidecar`; `applier/assist.py` — `_tools`, `assist_location` | anchor/edit/applier suites; `test_applier_isolation.py`, `test_export_can_overwrite_a_source_path_as_disclosed`; manual |
| C09 | Program routing/scope and code pins are built-in guidance, not a current-law database. Cross-check is within module/chunk; optional experimental observation does not alter findings. Unsupported disciplines, skipped/failed chunks and missing requirement coverage are visible. Extraction/model/search/routing/input assumptions can be wrong. | `src/programs/catalog.py` — `AVAILABLE_PROGRAMS`; `src/programs/routing.py` — `route_specs`; `src/modules/*.py` — module definitions; `src/core/chunked_pass.py` — `run_chunked_pass`; `src/compliance/completeness.py` — `CoverageCompleteness`; `src/coordination/runner.py` — `run_coordination` | routing/chunk/completeness/coordination suites; manual |
| C10 | Plain readable local JSON/JSONL/DOCX/HTML state; no app encryption. Keyring first, then plaintext key files, then GUI environment fallback; typed key held in memory, not saved or inserted into environment. Trace default on/deep off; retention pruning only at run start; verification TTL pruning on load and LRU on writes. Other artifacts have no age policy. | `src/core/api_key_store.py` — `load_api_key_from_file`; `src/core/app_paths.py` — `api_key_paths`; `src/core/credentials.py` — `ApiCredential`, `child_process_env`; `src/tracing/config.py` — defaults/path/switches; `src/tracing/recorder.py` — file constants, `prompt_ref`; `src/tracing/retention.py` — `apply_startup_retention`; `src/verification/verification_cache.py` — default/path/TTL/LRU; `src/core/ui_state.py` — `ui_state_path`; `src/orchestration/batch_resume.py` — `pending_batch_path`; `src/core/logging_setup.py` — `configure_file_logging`; `src/research/research_cache.py` — `default_research_cache_path` | credential/redaction/trace/cache/state suites; `test_trust_fact_sources`; manual |
| C11 | Anthropic bills hosted usage. Local token/rate forecasts and post-run dollar figures are estimates, with known/unpriced/unknown attempts exposed. Haiku 5.5 uses base rates through 100,000 total prompt tokens and higher rates above that threshold, including output and cache tokens; the total prompt includes uncached input plus cache writes/reads, with cache TTL components counted once. Batch discount applies to tokens, not web searches. Failed/stopped calls can consume paid work; no refund or overall app spend cap. | `src/core/pricing.py` — `MODEL_PRICING`, `estimate_cost_breakdown`; `src/core/attempt_usage.py` — `AttemptUsage`; `src/orchestration/diagnostics.py` — `DiagnosticsReport.summary`; `src/output/html_report_exporter.py` — `_CHAT_JS` | pricing/attempt-accounting/diagnostics tests; manual |
| C12 | No local listening server in shipped app. Desktop Python is not sandboxed; models have named output/web tools, no desktop shell/file-write tool. Search uses a blocklist, not allowlist; hosted tools may use vendor code execution internally. Prompt delimiters/redaction/HTML escaping reduce risks, not guarantees. Updater checks HTTPS initial/final and manifest hash, allows arbitrary HTTPS hosts/redirects, no total download byte bound; close suppresses install handling, not transfer. SDK URL/auth/proxy config can change destinations. | `src/core/updates.py` — `fetch_manifest`, `download_installer`; `src/gui/update_controller.py` — `close_update_dialog`; `src/review/reviewer.py` — `_get_client`; `src/review/prompt_serialization.py` — `wrap_document_block`; `src/tracing/redaction.py` — `scrub_data`; `src/output/html_report_exporter.py` — `_assemble_document`, `_CHAT_JS`; pinned `anthropic/_client.py`, `_constants.py`, `_base_client.py` | updates/redaction/prompt/HTML/client suites; `test_trust_fact_sources`; exhaustive source/network scan (manual) |
| C13 | HTML opens without requests; chat sends report context, a UTC date captured on the first request of each conversation, prior committed conversation, question/pasted material and tool results directly from browser. Prompts ask the assistant to keep grounding rules throughout repeated requests for exceptions; this is a mitigation, not a guarantee. Memory-only key, session-stored model/effort preferences; bounded automatic report tools change filters/scroll/highlights. Only a normal final reply with visible text commits conversation; thinking-only replies are incomplete. Stop discards uncommitted conversation but does not undo UI tool effects or refund work. No chat invoice meter. Viewer reads selected local trace directory. | `src/output/html_report_exporter.py` — `_CHAT_JS`, `_APP_JS`, `_build_chat_config`; `src/tracing/viewer/trace_viewer.html` — directory input/render handlers | HTML chat harness/offline viewer suites; `test_trust_fact_sources`; manual |
| C14 | Real audit hooks: diagnostics banner/window/copy, accepted/rejected evidence and age, JSON sidecars, local traces and trace CLI/viewer, provider console, source tests and firewall/disconnect checks. Default traces are selective; Deep adds sensitive prompts/response details, not a guaranteed complete audit log. Log has no universal secret scrubber. | `src/gui/diagnostics_controller.py` — `open_diagnostics_window`; `src/gui/widgets.py` — `DiagnosticsWindow`; `src/output/report_exporter.py` — evidence/diagnostics rendering; `src/tracing/capture_hooks.py` — capture hooks; `src/tracing/cli.py` — `build_parser`; `src/core/logging_setup.py` — `configure_file_logging` | diagnostics/trace/report tests; manual |
| C15 | Trust UI uses native Tk modal windows (OS dialog semantics, no DOM roles), nested grabs/focus/one Escape, one content scroll, persistent wide-screen contents rail, anchored sections, inline SVG drawn locally with a visible text alternative. No trust assets or network on open; links are explicit browser actions. | `src/gui/trust_dialogs.py` — `TrustDialog`, `Section`, `RuntimeCard`, `Table`, `Contrast`, `Note`, `Diagram`; `src/gui/trust_content.py` — `FLOW_SVG`; `src/gui/about_usage_dialogs.py` — trust wrapper functions | `test_trust_dialogs.py`, `test_trust_no_external_assets`; native integration under virtual display |

## Action inventory (one matching runtime card per ID)

| ID | Triggers (UI wording or command) | Source file — symbol | Claims |
| --- | --- | --- | --- |
| A01 | Browse specifications | `src/gui/file_selection_controller.py` — `apply_selected_specs` | C03 C04 |
| A02 | Drop specification files | `src/gui/file_selection_controller.py` — `parse_dropped_paths` | C03 C04 |
| A03 | File checkbox; All; None | `src/gui/token_analysis_controller.py` — `on_file_selection_change` | C03 C04 |
| A04 | Clear file selection | `src/gui/file_selection_controller.py` — `clear_selection` | C03 C06 |
| A05 | Type/delete Project Context | `src/gui/context_controller.py` — `do_context_change` | C03 C04 |
| A06 | Expand; Save & Close; window close in context editor | `src/gui/context_controller.py` — `open_context_modal` | C03 C04 |
| A07 | Attach Files… (DOCX/PDF/MD/TXT) | `src/gui/context_controller.py` — `attach_context_files` | C03 C04 |
| A08 | Attach Drawing Analysis… (TXT/MD/JSON output of a separate drawing-analysis program; read and token-counted locally) | `src/gui/context_controller.py` — `attach_drawing_analysis`; `src/input/drawing_analysis.py` — `read_drawing_analysis` | C03 C04 |
| A09 | Review program selector | `src/gui/gui.py` — `_on_module_selected` | C04 C09 C10 |
| A10 | City/state/province/country/client fields | `src/gui/gui.py` — `_on_profile_country_changed`, `_gather_project_profile` | C09 C10 |
| A11 | API key field | `src/gui/review_run_controller.py` — `_capture_run_credential` | C10 C12 |
| A12 | Real-time toggle; Keep Real-time; Use Batch instead; warning suppression | `src/gui/gui.py` — `_apply_transport_choice` | C05 C10 C11 |
| A13 | Real-time worker selector | `src/gui/gui.py` — `_on_realtime_workers_selected` | C06 C10 |
| A14 | Cross-spec coordination toggle | `src/gui/gui.py` — `_on_cross_check_toggle` | C09 C10 |
| A15 | Font size selector | `src/gui/gui.py` — `_on_font_scale_change` | C03 C10 |
| A16 | Show tracing tools; Trace; Deep | `src/gui/gui.py` — `_on_trace_toggle` | C10 C14 |
| A17 | Submit Batch; routing confirmation; cancel confirmation | `src/gui/review_run_controller.py` — `start_review` | C04 C05 C06 C09 C11 |
| A18 | Start Review (live); cost/routing confirmation | `src/gui/batch_controller.py` — `submit_batch_thread` | C04 C05 C06 C11 |
| A19 | Resume unfinished batch? Yes | `src/gui/batch_controller.py` — `start_batch_resume` | C04 C06 C10 C11 |
| A20 | Resume unfinished batch? No (discard) | `src/gui/batch_controller.py` — `offer_batch_resume` | C06 C10 C11 |
| A21 | Recover batch… | `src/gui/batch_controller.py` — `recover_batch_dialog` | C05 C06 C11 |
| A22 | Save Review Report; Save Word Report…; Retry; Cancel | `src/gui/report_controller.py` — `export_report_to_file` | C03 C06 C08 C10 |
| A23 | Save HTML Report… | `src/gui/report_controller.py` — `export_html_report_to_file` | C03 C10 C13 |
| A24 | Run Diagnostics; Copy to Clipboard | `src/gui/widgets.py` — `DiagnosticsWindow` | C03 C11 C14 |
| A25 | Show trace folder | `src/gui/gui.py` — `_on_show_trace_folder` | C03 C10 C14 |
| A26 | Open trace viewer; choose folder; tabs/filters/span/finding/event selection | `src/tracing/viewer/trace_viewer.html` — event handlers | C03 C10 C14 |
| A27 | Input/file/log expand-collapse; activity-log Clear | `src/gui/widgets.py` — `EnhancedLog.clear`, `FileListPanel._toggle` | C03 C14 |
| A28 | How It Works; How to Use; Why Trust It; About; trust contents/close/back; external links | `src/gui/about_usage_dialogs.py` — dialog functions | C03 C12 C15 |
| A29 | Check for Updates | `src/gui/update_controller.py` — `start_update_check` | C06 C12 |
| A30 | Download & Install; Install update Continue? | `src/gui/update_controller.py` — `start_update_download`, `on_update_download_done` | C06 C12 |
| A31 | Later; Skip this Version; close update dialog | `src/gui/update_controller.py` — `skip_update_version`, `close_update_dialog` | C06 C10 C12 |
| A32 | Close application; Close anyway? | `src/gui/gui.py` — `_on_close` | C06 C10 C11 |
| A33 | HTML report filters/search/expand/collapse/contents/copy/print/evidence links | `src/output/html_report_exporter.py` — `_APP_JS` | C03 C12 C13 |
| A34 | Ask AI open/close; Use key; Forget; model/effort; New chat; copy/print | `src/output/html_report_exporter.py` — `_CHAT_JS` | C05 C10 C13 |
| A35 | Chat Send; Enter; starter question; ask about selection; paste | `src/output/html_report_exporter.py` — `_CHAT_JS` | C05 C06 C11 C13 |
| A36 | Chat Stop; leave page; model change/Forget/New chat during turn | `src/output/html_report_exporter.py` — `_CHAT_JS` | C06 C11 C13 |
| A37 | python -m src.tracing list/show (including help) | `src/tracing/cli.py` — `main` | C03 C10 C14 |
| A38 | python -m src.tracing prune | `src/tracing/cli.py` — `cmd_prune` | C03 C10 C14 |
| A39 | scripts/recover_batch.py; Ctrl-C; --help | `scripts/recover_batch.py` — `main` | C05 C06 C11 |
| A40 | python -m applier / spec-critic-apply; --assist; policies/modes; --help | `applier/cli.py` — `main`; `applier/assist.py` — `assist_location` | C05 C06 C08 C11 C12 |

There is no review Stop button, project Save/Load command, review undo stack,
spec export command, install approval in the model, or periodic review scheduler.
Context editor closes without saving if its Save & Close button is not used.
Browser/Tk/OS clipboard, selection and text editing are local platform behavior.

## Automatic inventory

| ID | Trigger and behavior | Source file — symbol | Claims |
| --- | --- | --- | --- |
| B01 | Launch: logging/key/preferences/pending-resume prompt; Windows throttled update check; UI timers | `src/gui/gui.py` — `main`, `SpecReviewApp.__init__` | C04 C10 C12 |
| B02 | Changed loaded inputs/context/program/selection: debounced provider count; local tokenizer first-use download if cache missing | `src/gui/token_analysis_controller.py` — `refresh_exact_token_count`; `src/core/tokenizer.py` — `get_encoder` | C03 C04 C12 |
| B03 | Profile-enabled run: shared jurisdiction core once, discipline supplements; explicit module applicability, grounding, partial failure, profile trimming | `src/research/requirements_research.py` — `run_requirements_research`; `src/research/shared_jurisdiction.py` — `compose_module_profile`, `SupplementSignals`; `src/core/research_applicability.py` — `item_applies_to_module`; `src/orchestration/program_pipeline.py` — `prepare_program_review` | C05 C06 C07 C09 |
| B04 | Review response errors/truncation/parse failure: bounded retry/repair; beta rejection resubmit | `src/review/realtime_review.py` — `_review_one_spec`; `src/orchestration/pipeline.py` — `_recover_retryable_review_batch_results`; `src/batch/batch.py` — `_create_review_batch` | C05 C06 C11 |
| B05 | Submitted/resumed batch: automatic status polling, result downloads/retries, saved repair reattachment | `src/batch/batch_runtime.py` — `poll_batch_bounded`; `src/batch/batch.py` — `_collect_batch_results_with_retry` | C06 C10 C11 |
| B06 | Eligible lower-stakes findings: AI triage; deterministic skips; failures fall back to web | `src/verification/triage.py` — `classify_findings_with_haiku` | C02 C05 C07 |
| B07 | Findings: cache/single-flight, verifier web tools, submission reminders, continuations/retries, high-stakes escalation, small-tail live fallback | `src/verification/verifier.py` — `verify_finding`, `collect_verification_batch_results`; `src/orchestration/pipeline.py` — `verify_findings_for_run` | C05 C06 C07 C10 C11 |
| B08 | Cross-check selected: per-module/chunk calls; one output recovery per pass, either a parse re-request or smaller chunks after max_tokens; reduced coordination scope and failed/skipped chunks disclosed; all returned usage summed; verify new findings | `src/cross_check/cross_checker.py` — `run_cross_check`, `run_chunked_cross_check`; `src/core/pass_recovery.py` — `PassRecovery` | C05 C06 C09 C11 |
| B09 | Profile-enabled run: compliance/coverage; one output recovery per pass shared across chunks, either a parse re-request or smaller chunks after max_tokens; indivisible packages still fail; all returned usage summed; coverage gaps and held additions retain their existing rules; verify new findings | `src/compliance/compliance_checker.py` — `run_compliance_check`, `run_chunked_compliance_check`; `src/core/pass_recovery.py` — `PassRecovery` | C05 C06 C09 C11 |
| B10 | Attached drawing-analysis block present: post-review impact synthesis over that text | `src/drawing_impact/impact_synthesizer.py` — `run_drawing_impact` | C05 C06 C09 |
| B11 | Run start/finish: trace pruning/recording, local dedup/occurrences/report save prompt; state/cache writes | `src/tracing/session.py` — `start_run_recorder`; `src/gui/review_run_controller.py` — `on_review_complete` | C03 C06 C10 C14 |
| B12 | Submitted chat: automatic server tools and client report tools; pause continuation | `src/output/html_report_exporter.py` — `_CHAT_JS` | C05 C06 C12 C13 |
| B13 | Explicit environment experiments: context caching, schema output, evidence observation, source reuse, research reuse, cross-coordination candidate/observe | `src/core/api_config.py` — experiment switches; `src/review/structured_schemas.py` — `requested_review_output_constraint` | C05 C06 C07 C09 C10 |

## Network inventory

| ID | Destination / classification | Trigger, sent content, authentication and exceptions | Source |
| --- | --- | --- | --- |
| N01 | `api.anthropic.com` — only with credentials and input counting/AI/batch work | SDK sends prompts/spec/context/profile/findings/evidence/tool histories, key headers, model/settings (no drawing files: an attached drawing analysis is text inside context); polling sends batch id; browser chat sends report/history/question/tools with reader key. App passes a captured key explicitly; browser API URL fixed. | `src/review/reviewer.py::_get_client`; `src/core/tokenizer.py::count_input_tokens`; request builders; `src/output/html_report_exporter.py::_build_chat_config`; pinned `anthropic/_client.py` |
| N02 | API-supplied batch results URL + HTTP-client redirects — only on collection | Download request/results; SDK authentication headers handled by SDK; no app host allowlist. Exact URL determined remotely, not enumerable in source. | `src/batch/batch.py::_collect_batch_results_with_retry`; pinned `anthropic/resources/messages/batches.py::results`, `_base_client.py` |
| N03 | `github.com` release manifest and redirect hosts — automatic Windows/on-demand elsewhere | GET path/User-Agent, IP and normal connection metadata; no spec or API key added by updater. Manifest URL overridable with `SPEC_CRITIC_UPDATE_URL`; redirects/final HTTPS check. | `src/core/updates.py::_DEFAULT_MANIFEST_URL`, `fetch_manifest`; `src/gui/update_controller.py::maybe_auto_check_for_updates` |
| N04 | Manifest-specified installer HTTPS host/redirects — optional download | GET installer, ordinary connection metadata; no key/spec added; hostname unrestricted (normal GitHub release asset host varies). | `src/core/updates.py::parse_manifest`, `download_installer`, `_open_url` |
| N05 | `openaipublic.blob.core.windows.net` — only missing local tokenizer ranks | Downloads `cl100k_base.tiktoken`; no document content/key sent. Cached source installs avoid repeat; Windows installer bundles verified ranks. | `src/core/tokenizer.py::CL100K_BASE_BLOB_URL`, `get_encoder`; `packaging/windows/bundle_assets.py`, `app_entry.py` |
| N06 | Public search/fetch destinations on Anthropic's servers — only researched/verified/chat | Model-chosen queries, project location and URL requests processed by vendor; the data-center core is researched once and passed as untrusted context to module supplements, with explicit applicability required for controlling shared items; public domains are dynamic. Desktop tools have source-quality blocklist; HTML chat does not attach that blocklist. Vendor server tools can use internal code execution. | `src/core/api_config.py::build_web_search_tool`, `build_web_fetch_tool`; `src/output/html_report_exporter.py::_CHAT_JS` |
| N07 | Explicit browser links — optional | Evidence URLs, `github.com`, `privacy.anthropic.com`, `trust.anthropic.com`, `platform.claude.com`, `polyformproject.org`, `www.linkedin.com`; browser applies its own cookies/extensions/network behavior. Trust dossier restricts its links to Further reading. | `src/gui/about_usage_dialogs.py` link constants; `src/gui/update_controller.py::open_releases_page`; report evidence links; `src/gui/trust_content.py::FURTHER_READING` |
| N08 | Configured SDK base URL and system proxies — configured | `ANTHROPIC_BASE_URL` and HTTP(S) proxy can redirect SDK traffic (including sensitive payload/auth); `ANTHROPIC_CUSTOM_HEADERS` can add/override headers. Explicit app keys suppress SDK credential-profile discovery. Updater honors system proxies. These are not bounded by a desktop host allowlist. | `src/review/reviewer.py::_get_client`; pinned `anthropic/_client.py`, `_base_client.py`; `src/core/updates.py::fetch_manifest` |

The app implements no analytics/crash-upload/update telemetry client, local
listening server, automatic training job, shared-user database or downloaded
standards corpus. This is an exhaustive source/import scan of shipped paths,
not a guarantee about the provider, OS browser, enterprise proxies or future
dependency versions. No claim about vendor training/retention is inferred from
client code: link current API policies and contractual terms instead.

## Exact fact bindings

`src/gui/trust_content.py::fact_values` reads code constants, request builders,
model capabilities, pricing tables and path helpers. Its named substitutions are
the numeric/model/hostname ledger: `test_trust_fact_sources` requires every
substitution to have a binding here and forbids literal numerals/hostnames in
the copy templates. Numbers used to enumerate cards/sections or draw the SVG
are presentation, not claims about the engine.

| Binding | Source file — symbol (unless qualified: src/core/api_config.py) | Verification |
| --- | --- | --- |
| models, engine_rows, verifier_ai, review_ai, research_ai, cross_ai, compliance_ai, impact_ai, triage_ai, coordination_ai | role model constants; `effort_config_for`, `thinking_config_for`, `phase_output_cap`; `src/verification/verification_modes.py::mode_policy` | `test_trust_models_and_settings` |
| review_extended, review_threshold, review_beta | `REVIEW_OUTPUT_CAP_BATCH_EXTENDED`, `LARGE_REVIEW_INPUT_THRESHOLD`, `BATCH_OUTPUT_BETA` | `test_trust_fact_sources` |
| workers, research_workers, prepare_workers, collection_workers, call_permits | worker defaults/getters and environment variable names | `test_trust_fact_sources` |
| context_cap | `src/core/tokenizer.py::PROJECT_CONTEXT_MAX_TOKENS` | `test_trust_fact_sources` |
| retry_attempts, retry_wait, continuations, deep_continuations | `src/verification/retry_policy.py::DEFAULT_REALTIME_RETRY_POLICY`, `DEFAULT_MAX_CONTINUATIONS`, `DEEP_MAX_CONTINUATIONS` | `test_trust_fact_sources` |
| poll_hours, poll_errors, poll_intervals | `src/batch/batch_runtime.py::DEFAULT_REVIEW_POLL_POLICY`, poll constants | `test_trust_fact_sources` |
| sdk_retries, sdk_timeout, sdk_connect, api_host | pinned `anthropic/_constants.py::DEFAULT_MAX_RETRIES`, `DEFAULT_TIMEOUT`; `anthropic/_client.py::Anthropic` default URL (also matches HTML config) | `test_trust_fact_sources` |
| searches, fetches, fetch_tokens, research_budgets, research_continuations | severity map; `DEFAULT_VERIFICATION_MAX_FETCHES`, `WEB_FETCH_MAX_CONTENT_TOKENS`; `src/modules/*.py` module `research_dimensions`; `src/research/shared_jurisdiction.py::JURISDICTION_DIMENSIONS`; `src/research/requirements_research.py::RESEARCH_MAX_CONTINUATIONS` | `test_trust_fact_sources` |
| analysis_formats, analysis_mib | `src/input/drawing_analysis.py::DRAWING_ANALYSIS_EXTENSIONS`, `MAX_DRAWING_ANALYSIS_BYTES` (the merged-context token cap is `context_cap`) | `test_trust_fact_sources` |
| cache_days, cache_entries, flight_wait, cache_path | `src/verification/verification_cache.py::_DEFAULT_CACHE_TTL_DAYS`, `_DEFAULT_CACHE_MAX_ENTRIES`, `_DEFAULT_SINGLEFLIGHT_WAIT_SECONDS`, `default_cache_path` | `test_trust_fact_sources` |
| trace_days, trace_runs, trace_path, trace_files | `src/tracing/config.py::DEFAULT_TRACE_RETENTION_DAYS`, `DEFAULT_TRACE_MAX_RUNS`, `default_trace_root`; `src/tracing/recorder.py::FILE_*`. Offline `TRACE_LOCATIONS_PIN` lists platformdirs' Windows/macOS/Unix user-state defaults; GUI resolves actual path. | `test_trust_fact_sources` |
| log_path, log_mib, log_backups | `src/core/logging_setup.py::default_log_path`, `_DEFAULT_MAX_BYTES`, `_DEFAULT_BACKUPS` | `test_trust_fact_sources` |
| pending_path, ui_path, update_state, download_path, research_cache_path, key_filename, key_service, key_account | corresponding path helpers in `src/orchestration/batch_resume.py`, `src/core/ui_state.py`, `updates.py`, `app_paths.py`, `api_key_store.py`, `src/research/research_cache.py` | `test_trust_fact_sources` |
| update_url, tokenizer_url, update_days, manifest_timeout, download_timeout, manifest_kib | `src/core/updates.py::_DEFAULT_MANIFEST_URL`, `DEFAULT_MIN_INTERVAL_DAYS`, `DEFAULT_MANIFEST_TIMEOUT`, `DEFAULT_DOWNLOAD_TIMEOUT`, `MAX_MANIFEST_BYTES`; `src/core/tokenizer.py::CL100K_BASE_BLOB_URL` | `test_trust_fact_sources` |
| update_hash | `src/core/updates.py::verify_sha256` and `download_installer` use `hashlib.sha256` (SHA-256) | `test_trust_fact_sources` |
| browser_hosts | `src/gui/trust_content.py::FURTHER_READING`; `src/gui/about_usage_dialogs.py::_LICENSE_URL`, `_LINKEDIN_URL`, `_GITHUB_PROFILE_URL` (About hosts pinned without importing Tk into shared content) | `test_trust_fact_sources` |
| chat_ai, chat_model_options, chat_efforts, chat_tokens, chat_tools, chat_continuations, chat_history, chat_searches, chat_fetches, chat_fetch_tokens | `src/output/html_report_exporter.py::CHAT_*`, `_build_chat_config`, `_CHAT_JS` runtime declarations | `test_trust_fact_sources` (AST/JS extraction) |
| assist_ai, assist_rounds, assist_tokens | `applier/assist.py::AssistConfig`, `MAX_TOOL_ROUNDS`, `ASSIST_MAX_TOKENS` | `test_trust_fact_sources` |
| price_rows, batch_discount, cache_multipliers, search_price | `src/core/pricing.py::MODEL_PRICING`, tier-aware `price_for` and `estimate_cost_breakdown`, `BATCH_DISCOUNT`, cache multipliers, `WEB_SEARCH_USD_PER_1000` | `test_trust_fact_sources` |
| coordination_candidates, coordination_pair, coordination_batch | `src/coordination/candidates.py::MAX_CANDIDATES`, `MAX_PER_FILE_PAIR`; `src/coordination/adjudication.py::CANDIDATES_PER_REQUEST` | `test_trust_fact_sources` |
| context_formats, unsupported_divisions | `src/input/extractor.py::CONTEXT_ATTACHMENT_EXTENSIONS`; `src/programs/routing.py` unsupported discipline rules | `test_trust_fact_sources` |

## Findings and consistency audit (behavior deliberately unchanged)

1. File-selection/context/program changes upload a provider counting request
   before Submit Batch; clearing cancels scheduled work, not active HTTP requests.
2. Windows startup update check and unbundled tokenizer first-use download are
   exceptions to click-only networking. Automatic follow-up work/retries are paid.
3. Anchor matching is whitespace tolerant and skips unknown/unattributable text.
   Existing trust help's unconditional word-for-word promise was false.
4. “Locally classified” can follow Haiku triage; it does not prove no AI was used.
   “Two independent AIs for every substantive finding” ignores local skips, cache
   replay, mode routing, overrides and shared vendor/model assumptions.
5. URL provenance and a nonempty source quote do not prove source truth, exact
   quote support, adoption, or the model's interpretation. Evidence validation
   is an optional observation experiment, not the default guarantee.
6. Updater allows arbitrary HTTPS manifest/installer/redirect hosts; an HTTPS to
   HTTP redirect can be followed before the final-scheme rejection. Installer
   download has no total byte ceiling; close does not cancel transfer.
7. Trace retention runs at run start, not continuously; other state/reports/
   installers/research cache have no general automatic age deletion. Ordinary
   logs lack the trace/diagnostics universal redaction pass. Traces can contain
   project content; the queue warns without imposing a hard queue cap.
8. Research always attaches web_fetch, including on an unsupported model override.
   Verification/chat capability-gate it. That override can fail research.
9. Batch close/discard does not cancel the remote batch; live close has no resume.
    No review Stop control or whole-run dollar cap exists. Exports may partly
    succeed. Cross-module/chunk coordination is not comprehensive by default.
10. HTML chat uses memory-only key today. README's changelog and CLAUDE.md
    changelog still described sessionStorage keys; handbook/22 already describes
    memory-only keys correctly. Handbook/16 also describes
    older Opus/Sonnet models and hard ceilings; it is historical analysis.
11. Existing How to Use timing copy says 24-hour maximum, while the local poller
    detaches at its 4-hour bound. It describes provider turnaround, not local
    waiting; this distinction is added to current help. README/CLAUDE changelogs
    retain older-model history, not current default settings.

Current trust dialogs are replaced; current README/help claims are corrected.
Historical handbook/model analysis is explicitly marked historical and linked
to this ledger, rather than silently presented as today's contract. Vendor price,
privacy and trust documents are external references, not code-verifiable claims.

13. The explicit SPEC_CRITIC_REVIEW_EFFORT override bypasses the default Opus
    effort ceiling; local-skip routing is always enabled, with no disable switch.
    The new copy names these instead of inventing controls.
14. Other current help overstated cross-module/full-package coordination,
    one-search-call research and all-API-call context propagation. These copy
    statements were narrowed to the actual per-stage/module/chunk mechanisms.
    Unsupported speed figures were removed from current help and README;
    there is no code-enforced turnaround guarantee.
15. Report evidence labels said “verbatim from search result” despite not
    comparing the supplied quote to source text. Word/HTML labels now say
    “supplied by verifier”; quote parsing/grounding behavior is unchanged.
    Sources: `src/output/report_exporter.py::_write_evidence_panel`,
    `src/output/html_report_exporter.py::_render_evidence_panel`;
    verified by `test_report_quote_label_does_not_claim_the_gate_compared_words`.
16. README's unconditional strict-schema and default Opus-effort statements
    omitted supported switches; README/CLAUDE said unknown models avoid API
    rejection, which the code cannot guarantee. These current statements are
    qualified; the switches, model routing and research tools are unchanged.

17. Export accepts the selected output path without checking against input
    paths; selecting an input DOCX can replace it with the report. Same-stem
    sidecars overwrite without their own prompt. Reproduced on a temporary
    source DOCX by `test_export_can_overwrite_a_source_path_as_disclosed`.
    Current help/README and both trust levels now qualify source preservation;
    export and sidecar behavior deliberately remain unchanged.


### Remaining documentation disagreements

These locations are retained as older engineering narrative, rather than used
as sources for this contract. They should not be read as stronger guarantees:

| Surface | Disagreement with shipped code | Current account |
| --- | --- | --- |
| `handbook/03_end_to_end_flow.md:48`, `handbook/06_batch_processing.md:52` | Typical 45-minute/two-hour turnaround and provider 24-hour ceiling presented without local wait distinction. | No code-guaranteed turnaround; bounded local poller detaches without remote cancellation (C06). |
| `handbook/03_end_to_end_flow.md:375` | “Two files and only two files.” | Profile-enabled exports also write a profile sidecar; HTML, diagnostics and trace artifacts are separate available outputs (A22–A26). |
| `handbook/12_configuration_and_models.md:567`, `handbook/13_gui.md:386`, `handbook/14_observability.md:79`, `handbook/14_observability.md:93` | Default trace root given as `~/.spec_critic/traces`. | Per-platform SpecCritic user-state folder, not that home folder (C10). |
| `handbook/11_trust_model_and_output.md:129` | Left-hand statuses called directly actionable and locally classified called deterministic. | Model triage can precede local classification; professional checking is required (C01, C02, C07). |
| `handbook/15_quality_engineering.md:188` | “Wrapper escaping is injection-proof.” | Escaping protects the wrapper structure; it does not guarantee model obedience or safe public-source content (C12). |
| `handbook/22_html_report.md:133` | Heading says “only the report,” although its following table lists web tools. | Report plus deliberately started web lookups; automatic report display tools also run (C13). |
| `handbook/16_trust_under_the_microscope.md` and older model/limit examples in other handbook chapters | Historical model/cap/audit snapshots. | This chapter now has an explicit historical banner; current role IDs/settings/caps come from fact bindings (C05). |

The current help, README and report labels were brought into agreement. The
remaining narrative locations are listed here and in the implementation report,
not silently rewritten as if their historic evaluations had been rerun.
