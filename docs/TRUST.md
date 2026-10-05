# Why trust Spec Critic?

<!-- Generated from src/gui/trust_content.py; update the source and regenerate. -->

Spec Critic gives you findings to check, with visible evidence checks and failure signals, so you can decide what deserves your name.

**You can see how findings were checked**

The report separates the model's confidence from the verification status and shows accepted and rejected citations. A source match proves where evidence came from; you still check whether it supports the conclusion.

**Rules and models have different jobs**

Local pattern checks flag template problems. Models propose findings and interpret sources; module rules are each discipline's built-in instructions. 'Locally classified' can follow AI triage — sorting claims for later checks — so it does not mean no AI was used.

**Your review produces proposals for your decision**

The reviewer exports a report and edit instructions. It checks quoted anchors — text used to locate an edit — against available extracted text, allowing whitespace differences. The separate applier can change copies. Export can overwrite your chosen path; use a different report filename.

**Background work has named triggers and exceptions**

Changing loaded inputs can send text to Anthropic for token counting, sizing a model request, before a review. Windows startup can check for updates; source installs may download tokenizer data. Started reviews launch research, verification, retries and selected follow-up checks automatically.

**Your documents cross a visible service boundary**

AI requests send relevant project text to Anthropic by default; drawing preflight can send a PDF chunk before cost confirmation. Typed keys stay in memory, but optional key files are plaintext. Traces, local run records, are on by default and can contain project content; dollar figures are estimates.

**Failures and reused results leave visible signals**

Diagnostics name failed reviews and skipped work. Evidence panels show the age of reused results, sources and verifier details. Closing a batch window leaves the remote batch running; the live transport has no resume.

**A clean report still needs your judgment**

Routing, extraction, research and models can miss issues. Cross-spec checking stays within a module and its chunks by default. Neither a Verified label nor an edit proposal certifies compliance or approves a change.

**Not convinced?**

Fair. The points above are claims — here is the mechanism behind each one, action by action, plus how to audit any of it yourself.

[I'm not convinced — show me exactly what runs →](#the-short-answer)

# The detailed view

<a id="answer"></a>
## The short answer

*Start here*

- **Know the origin and the gate:** You get origin/stage labels, status and evidence details. Those distinguish supplied text, local detection, model proposals and retrieved or reused sources; they do not label the ancestry of every sentence.

- **Know what starts without another click:** Startup state loading/update checks and input counting are automatic. Once you start a review or a chat question, its retries, tools and dependent stages proceed automatically. A remote batch can outlive your window.

- **Recheck the mechanical decisions:** Gates are programmed checks that accept or downgrade a result. You can inspect and reproduce source matching, routing, edit-shape checks and pricing arithmetic from recorded inputs and code. You cannot expect another model call to reproduce the same judgment.

<a id="origins"></a>
## Where your findings come from

*Provenance*

| Origin | What it actually is | How you can tell |
| --- | --- | --- |
| You and your files | Extracted specification text; context, attachments, location/client fields. | Files Reviewed, quoted passages, context attachment labels and profile fields. |
| Built-in module rules | Versioned prompts, code/edition pins, detector vocabulary and routing rules. | Selected program/module, code-basis and methodology; inspect module source. |
| Local detection | Pattern/structure checks and later mechanical gates. | Pre-detected alerts. Locally classified is a routing status and can also follow model triage. |
| Model interpretation | Review, coordination, compliance, research and drawing proposals, using model training as well as supplied context. | Finding origin/stage, confidence and methodology; these are proposals. |
| Retrieved public sources | Search/fetch results the hosted tools returned. | Evidence panel: accepted/rejected URLs, search/fetch detail and source quote. |
| Reused evidence or verdicts | A cached verdict (a locally saved eligible result), shared equivalent work, or opt-in source/profile reuse. | Cache replay age/path and reuse diagnostics; not newly searched evidence. |

**Things that are not happening**

The shipped reviewer has no downloaded standards corpus, shared-user project database or training job. It does not run the edit applier. These are statements about client code; Anthropic's storage/training terms and your own synced folders need separate checks.

<a id="engine"></a>
## What the engine actually is

*Components*

By default, models run on Anthropic's servers. Extraction and report components run on your computer. A token is a unit used to measure model text; output caps include thinking as well as the answer. Effort is a requested reasoning level, not a correctness score. Adaptive thinking lets the provider allocate reasoning work. Temperature is the answer-variation setting; the app omits it. The SDK is the provider's client library that sends requests.

| Job | Model or component | Why |
| --- | --- | --- |
| Per-spec review | claude-opus-5-5; effort medium; adaptive thinking; output cap 128,000 tokens; temperature omitted. | Propose issues from the supplied spec and module basis; hosted. |
| Requirements research | claude-sonnet-5-5; effort high; adaptive thinking; output cap 64,000 tokens; temperature omitted. | Retrieve and summarize profile requirements; hosted. |
| Verification | Strict: claude-sonnet-5-5; effort low; thinking omitted; output cap 64,000 tokens; temperature omitted. Standard: claude-sonnet-5-5; effort medium; adaptive thinking; output cap 64,000 tokens; temperature omitted. Deep/escalated: claude-opus-5-5; effort medium; adaptive thinking; output cap 64,000 tokens; temperature omitted. Local-skip/cache lookup: None. | Test proposed claims against retrieved evidence; hosted. |
| Eligible finding triage | claude-haiku-4-5; 8,000 output tokens; effort, thinking and temperature omitted. | Choose local resolution or web-required; hosted. |
| Cross-spec coordination | claude-sonnet-5-5; effort high; adaptive thinking; output cap 96,000 tokens; temperature omitted. | Compare module/chunk text; hosted. |
| Compliance | claude-sonnet-5-5; effort high; adaptive thinking; output cap 64,000 tokens; temperature omitted. | Compare researched requirements to specs; hosted. |
| Drawing digest | claude-sonnet-5-5; effort medium; adaptive thinking; output cap 24,000 tokens; temperature omitted. | Interpret uploaded PDF chunks; hosted. |
| Drawing impact | claude-sonnet-5-5; effort high; adaptive thinking; output cap 32,000 tokens; temperature omitted. | Relate digest text to findings; hosted. |
| Optional coordination observation | claude-sonnet-5-5; effort high; adaptive thinking; output cap 16,000 tokens; temperature omitted. | Judge bounded passage pairs; default off; hosted. |
| Exported report chat | claude-opus-5-5 by default (choices: claude-opus-5-5, claude-sonnet-5-5); effort medium by default, selectable low, medium, high; adaptive summarized thinking; 64,000 output tokens; temperature omitted. | Answer and use report/web tools after you submit; hosted. |
| Separate optional applier assistance | claude-sonnet-5-5 by default (--assist-model can change it); effort, thinking and temperature omitted; 2,000 output tokens. | Choose an element location, not edit wording; hosted. |
| Local preparation/reporting | Python, DOCX/PDF parsers, tokenizer and module rules; no model. | Extract, route, check shapes/URLs, estimate costs and render/export locally. |

**Defaults and switches**

Model overrides: SPEC_CRITIC_REVIEW_MODEL, SPEC_CRITIC_VERIFICATION_MODEL, SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL, SPEC_CRITIC_TRIAGE_MODEL, SPEC_CRITIC_RESEARCH_MODEL, SPEC_CRITIC_DRAWING_DIGEST_MODEL and SPEC_CRITIC_DRAWING_IMPACT_MODEL. Cross-check/compliance defaults have no model environment switch. SPEC_CRITIC_REVIEW_EFFORT changes review effort, overriding the default Opus medium ceiling when explicitly set. The table reflects this process's configured values; arbitrary override IDs can fail or lack a price. Temperature is omitted. Trace Deep can request summarized thinking; strict verifier omits explicit thinking, which is not a promise that thinking is disabled.

<a id="boundary"></a>
## What leaves your computer, and where it goes

*The boundary*

<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 300" role="img" aria-label="Your computer extracts specifications and keeps reports, state and traces. AI and counting requests send project text or drawing PDFs to Anthropic. Anthropic may search and fetch public sources. Optional or automatic paths go to update hosts, tokenizer storage, configured endpoints and browser links. Dashed lines mark automatic counting and conditional paths; results return to your computer.">
<style>svg{color:#17212b;background:#f8fafc}text{fill:currentColor;font:15px sans-serif}rect{fill:none;stroke:currentColor}line{stroke:currentColor;stroke-width:2}@media(prefers-color-scheme:dark){svg{color:#f1f5f9;background:#1a1a1a}}</style>
<rect x="12" y="25" width="218" height="82" rx="8"/>
<text x="26" y="53">Your computer</text><text x="26" y="80">Specs · reports · traces</text>
<rect x="280" y="25" width="212" height="82" rx="8"/>
<text x="294" y="53">Anthropic</text><text x="294" y="80">AI · counting · batches</text>
<rect x="540" y="25" width="206" height="82" rx="8"/>
<text x="554" y="53">Public sources</text><text x="554" y="80">Search · fetch</text>
<line x1="230" y1="54" x2="280" y2="54"/><line x1="280" y1="88" x2="230" y2="88"/>
<line x1="230" y1="70" x2="280" y2="70" stroke-dasharray="6 5"/>
<line x1="492" y1="67" x2="540" y2="67" stroke-dasharray="6 5"/>
<rect x="12" y="175" width="734" height="103" rx="8"/>
<text x="26" y="201">Conditional network paths</text>
<text x="26" y="228">Updates / redirects · tokenizer download · SDK endpoints / proxies</text>
<text x="26" y="254">Explicit browser links · vendor-selected batch result URLs</text>
<line x1="122" y1="107" x2="122" y2="175" stroke-dasharray="6 5"/>
</svg>

Your computer extracts specifications and keeps reports, state and traces. AI and counting requests send project text or drawing PDFs to Anthropic. Anthropic may search and fetch public sources. Optional or automatic paths go to update hosts, tokenizer storage, configured endpoints and browser links. Dashed lines mark automatic counting and conditional paths; results return to your computer.

**Leaves your machine**

Relevant specs, filenames, context/profile, drawings, findings, evidence and tool history go to api.anthropic.com for counting or AI. Batch collection follows a returned results URL; the SDK follows redirects and configured base URLs/proxies.

Updates GET https://github.com/Abe-Borg/Claude-Spec-Critic/releases/latest/download/latest.json and manifest-selected HTTPS installer hosts/redirects. A missing source-install tokenizer downloads https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken.

Vendor web tools reach dynamic public sources. Explicit browser links reach evidence sites and these fixed reference hosts: github.com, platform.claude.com, polyformproject.org, privacy.anthropic.com, trust.anthropic.com, www.linkedin.com. Browsers apply their own cookies. The updater/tokenizer add no project text or API key.

**Stays on your machine**

Local parsing/routing/pattern checks, evidence comparisons, pricing math, reports, sidecars, UI state, caches, recovery state and trace files. They can contain copies of content that was also sent.

Typed desktop/browser keys remain in memory and are sent for API authentication. Optional key files/keyring live locally. 'Local storage' does not mean encryption, and a folder you sync/share may send these artifacts elsewhere.

**A configurable boundary**

The desktop SDK honors ANTHROPIC_BASE_URL and ANTHROPIC_CUSTOM_HEADERS; system proxies, intermediary network services, also matter. The app passes its captured API key explicitly, so SDK credential-profile discovery does not run. There is no app list of permitted hostnames. The browser report's API destination is fixed in that exported file. Public-source hosts, results URLs, redirects and installer hosts cannot be exhaustively named before the response exists; watch a firewall log to see the actual ones.

<a id="runtime"></a>
## What runs for each action

*Runtime*

The cards include desktop controls, exported-report controls, shipped companion commands and automatic stages. Related controls sharing a mechanism share a card. API destinations describe the default endpoint; ANTHROPIC_BASE_URL and proxies can redirect desktop traffic. 'AI involved: None' refers to that action's own computation; counting can still send data to a hosted endpoint. Review phases and their retries are not separate approvals.

### 1. Browse specifications

- **You do:** Browse; select Word specification files.
- **What runs:** Accumulate readable DOCX files, reject different files sharing a name, extract text locally, then schedule the counting card below.
- **What is sent:** Counting can send the largest selected spec, its filename, module prompt and Project Context to api.anthropic.com.
- **AI involved:** None.
- **Bounded by:** Unreadable files are warned about; duplicate inputs collapse. Counting can happen before Submit Batch.

### 2. Drop specifications

- **You do:** Drop files into the specification input.
- **What runs:** Parse dropped paths, filter supported files and run the same selection and counting flow as Browse.
- **What is sent:** The counting request described under Browse, when readable files are added.
- **AI involved:** None.
- **Bounded by:** Only supported specification formats enter this flow; unavailable drag-and-drop falls back to Browse.

### 3. Choose the files to review

- **You do:** Toggle a file checkbox, All or None.
- **What runs:** Recompute the selected-input gauge and review gate; schedule counting when selected extracted specs remain.
- **What is sent:** Selected largest spec and current prompt/context to api.anthropic.com for counting.
- **AI involved:** None.
- **Bounded by:** Unchecking files changes future submissions. An active count request is not canceled.

### 4. Clear the file selection

- **You do:** Clear beside Browse.
- **What runs:** Clear selection/gauge, disable review, invalidate old analysis results and cancel scheduled counting.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Active HTTP requests can finish. Clear does not erase reports, traces or a remote batch.

### 5. Edit project context

- **You do:** Type, paste or delete Project Context.
- **What runs:** Recount locally after editing settles; update the drawings readout and refresh selected-input counting.
- **What is sent:** When specs are loaded, updated context may enter the counting request to api.anthropic.com.
- **AI involved:** None.
- **Bounded by:** Context limit: 100,000 tokens, units used to size model text. Delete a drawing digest to remove it from future context; this cannot retract an earlier upload.

### 6. Expand and save context

- **You do:** Expand; Save & Close, or close the editor window.
- **What runs:** Open a local editor; Save & Close checks the context limit and copies text into the main field, whose counting flow then runs.
- **What is sent:** Saving can trigger the input-count request; closing without saving sends nothing from this editor.
- **AI involved:** None.
- **Bounded by:** Over-limit text is refused. Window close discards modal edits; this is not a project-file save.

### 7. Attach reference files

- **You do:** Attach Files… in either context editor.
- **What runs:** Read .docx, .md, .pdf, .txt locally; wrap filenames and extracted text as context. Then merge and refresh counting.
- **What is sent:** The merged text can enter counting and later AI requests to api.anthropic.com; this attachment flow does not upload the original file bytes.
- **AI involved:** None.
- **Bounded by:** Merged context over 100,000 tokens is refused; failed reads are named. Images in a text-only extraction can be missed.

### 8. Analyze attached drawings

- **You do:** Attach Drawings…; choose PDFs; answer Analyze drawings?
- **What runs:** Validate/split PDFs locally. Send an anchor PDF chunk for count preflight, scale estimates for other chunks, then ask about paid analysis. If you agree, digest chunks in parallel and merge their summaries into context.
- **What is sent:** A PDF chunk plus prompt goes to api.anthropic.com before the cost-confirmation dialog. Paid analysis sends the selected chunk PDFs, labels and instructions.
- **AI involved:** claude-sonnet-5-5; effort medium; adaptive thinking; output cap 24,000 tokens; temperature omitted.
- **Bounded by:** Chunks obey effective model page limits, at most 600 PDF pages and 20 MiB raw bytes; up to 4 calls at once. Partial failures are named; total failure adds no digest. Context refusal after analysis does not refund usage. No digest Stop control.

### 9. Choose a review program

- **You do:** Review program selector.
- **What runs:** Persist the choice and update module scope/profile fields; re-analyze loaded files and count their new prompts.
- **What is sent:** Loaded inputs can be sent for counting to api.anthropic.com.
- **AI involved:** None.
- **Bounded by:** An in-flight run keeps its submitted program. Scope and routing are not a guarantee of full discipline coverage.

### 10. Describe the project location and client

- **You do:** Edit city, state/province, country or client.
- **What runs:** Normalize the local fields; snapshot and save them when a profile-enabled review starts.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** No research starts just from these fields. Wrong or incomplete location/client data can lead to wrong researched requirements.

### 11. Supply an API key

- **You do:** Type or replace the API key.
- **What runs:** Hold the value in the field; counting/drawing/review workers capture their credential. Each run keeps its captured key.
- **What is sent:** The key authenticates requests to the configured API endpoint when requests run; typing alone sends no request.
- **AI involved:** None.
- **Bounded by:** The app does not save typed keys or put them into the environment. Startup can load a saved keyring/plaintext/environment key. Protect those stores yourself.

### 12. Choose batch or live transport

- **You do:** Real-time toggle; Keep Real-time; Use Batch instead; suppress the cost warning.
- **What runs:** Show the session cost warning when applicable and persist the chosen transport/warning preference.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Batch is the fresh-install default. A persisted live choice stays live; changing the toggle does not convert an active run. Live work uses standard token rates.

### 13. Choose live concurrency

- **You do:** Real-time workers: 2 / 4 / 6 / 8.
- **What runs:** Persist the limit used by the next live run across routed modules.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** This bounds simultaneous reviews, not total requests or spend. Batch ignores it.

### 14. Choose cross-spec checking

- **You do:** Cross-spec coordination checkbox.
- **What runs:** Persist the option for the next review; a started review later runs the cross-check card if selected.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Selected by default on a fresh install; checking is limited to each module and chunk.

### 15. Adjust reading size

- **You do:** Font size selector.
- **What runs:** Change widget scaling and persist your preference.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Local work; no model charge. Closing a picker without choosing makes no change.

### 16. Choose trace capture

- **You do:** Show tracing tools; Trace; Deep.
- **What runs:** Reveal controls and change next-run capture. Trace is on and Deep is off by default; Deep can request summarized model thinking.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Deep captures sensitive prompt/response detail and overrides trace-off if enabled. Hiding tracing tools does not turn tracing off.

### 17. Submit a batch review

- **You do:** Submit Batch; confirm routing when asked.
- **What runs:** Snapshot inputs/key/options; extract, pre-screen and route locally. Profile-enabled modules research first. Count/size built requests, submit review batches and save recovery state. Poll and launch dependent stages automatically.
- **What is sent:** Relevant spec text/filename, effective context, module instructions and profile go to api.anthropic.com. Research sends profile and observed corpus signals; batches send request mappings.
- **AI involved:** claude-opus-5-5; effort medium; adaptive thinking; output cap 128,000 tokens; temperature omitted.
- **Bounded by:** Output baseline is in the engine table; large batch inputs at 200,000 tokens can request 300,000 output tokens with output-300k-2026-03-24. Canceling routing avoids review submission; prior counts/research may already have happened. No whole-run spend ceiling.

### 18. Start a live review

- **You do:** Start Review (live); confirm cost/routing when asked.
- **What runs:** Prepare as for batch; stream per-spec reviews under the global worker limit, then verification and follow-up passes. No saved live review state.
- **What is sent:** Spec text, filenames, prompts/context/profile and later stage results to api.anthropic.com.
- **AI involved:** claude-opus-5-5; effort medium; adaptive thinking; output cap 128,000 tokens; temperature omitted.
- **Bounded by:** Concurrency choices: 2 / 4 / 6 / 8. Output stays at the baseline cap; oversized requests are refused. Failed calls/repair attempts can still cost money. Closing loses live results and cannot resume them.

### 19. Resume saved work

- **You do:** Resume unfinished batch? Yes.
- **What runs:** Load saved mappings/context/profile, reattach review/recorded repair batches, poll, collect and run outstanding dependent stages.
- **What is sent:** Batch IDs for status/results, then findings/context/evidence for remaining calls to api.anthropic.com or the returned results URL.
- **AI involved:** Review retrieval: None. Remaining AI: Strict: claude-sonnet-5-5; effort low; thinking omitted; output cap 64,000 tokens; temperature omitted. Standard: claude-sonnet-5-5; effort medium; adaptive thinking; output cap 64,000 tokens; temperature omitted. Deep/escalated: claude-opus-5-5; effort medium; adaptive thinking; output cap 64,000 tokens; temperature omitted. Local-skip/cache lookup: None.; cross/compliance/impact use the engine table.
- **Bounded by:** Avoids resubmitting the saved primary review; dependent stages can incur new spend. Saved state lacks a complete prior-session usage ledger.

### 20. Discard a recovery record

- **You do:** Resume unfinished batch? No.
- **What runs:** Delete the matching local saved record.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** This does not cancel a remote batch or undo its billing. Record removal is not undoable through the GUI.

### 21. Recover by batch ID

- **You do:** Recover batch…; enter the ID and requested module/files.
- **What runs:** Use matching saved state when available; otherwise wait for the batch to end and reconstruct its mappings from results. Collect and run remaining stages.
- **What is sent:** Batch ID/status/results requests; re-extracted context/findings/evidence for remaining API work.
- **AI involved:** Retrieval: None. Remaining AI uses the engine table.
- **Bounded by:** Polling detaches at 4 hours. Bare-ID recovery needs the right module; missing inputs reduce available source checks. A results download can restart from its beginning.

### 22. Save a Word report

- **You do:** Save Review Report at completion; Save Word Report…; Retry or Cancel.
- **What runs:** Write the Word report, edit-instruction JSON and, when applicable, requirements-profile JSON beside it. Retry repeats local export from retained results.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Save-dialog Cancel writes nothing. Export has no source-file guard; accepting an input filename can overwrite it. Same-stem sidecars overwrite without their own confirmation. A failure can leave partial output; no multi-file rollback. Results remain in memory until close. Check sidecar warnings.

### 23. Save an HTML report

- **You do:** Save HTML Report…
- **What runs:** Write a self-contained report and open its local file in your browser. The GUI export includes Ask AI; the exporter also supports a chat-free option.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Opening the report makes no app request. Chat requires your browser key and a submitted question. HTML export has no source-path guard and can overwrite your chosen file; it does not write Word sidecars.

### 24. Inspect run diagnostics

- **You do:** Run Diagnostics; Copy to Clipboard.
- **What runs:** Render retained phase status, usage, estimated cost and timeline; copy diagnostic text to the OS clipboard.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** This is selective instrumentation, not the provider's invoice or a complete raw request log. Unknown usage is reported.

### 25. Open local traces

- **You do:** Show trace folder.
- **What runs:** Create/open %LOCALAPPDATA%/SpecCritic/traces (Windows), ~/Library/Application Support/SpecCritic/traces (macOS), or $XDG_STATE_HOME/SpecCritic/traces (Linux; usually ~/.local/state/SpecCritic/traces) with your OS file browser.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** The app passes a local path. Your file browser's own integrations are outside the app's network controls.

### 26. Read traces offline

- **You do:** Open trace viewer; choose a trace directory; select tabs, spans, findings or events.
- **What runs:** Open the bundled local HTML viewer; your browser reads the directory you select and renders its JSON/JSONL records.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** No app listening server or external viewer assets. Default traces omit some raw detail; a missing capture is not proof that work did not run.

### 27. Change local panels and activity log

- **You do:** Expand/collapse input, files or activity panels; activity-log Clear.
- **What runs:** Change visible local widgets; Clear empties the activity display/queue and resumes later log entries.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Clearing the display does not clear disk logs, diagnostics, traces, remote work or spend.

### 28. Read help and this explanation

- **You do:** How It Works; How to Use; Why Trust It; About; contents/back/Close/Escape; a Further reading link.
- **What runs:** Render local native help; the dossier stacks over the short topic. A link deliberately opens your browser.
- **What is sent:** Nothing for opening/scanning trust. An external link sends a browser request to its displayed destination.
- **AI involved:** None.
- **Bounded by:** One Escape closes the top trust dialog and returns focus to its opener. Browser cookies/extensions are governed by your browser.

### 29. Check for an update

- **You do:** Check for Updates.
- **What runs:** Fetch and validate the release manifest, compare versions and show the result.
- **What is sent:** GET https://github.com/Abe-Borg/Claude-Spec-Critic/releases/latest/download/latest.json by default, plus redirects; connection metadata and updater User-Agent, without project text/key added by the updater.
- **AI involved:** None.
- **Bounded by:** 8-second socket timeout; 64 KiB manifest limit. SPEC_CRITIC_UPDATE_URL changes the URL; SPEC_CRITIC_DISABLE_UPDATE_CHECK disables checks. Non-Windows runs are directed to releases for installation.

### 30. Download and install an update

- **You do:** Download & Install; then confirm Install update Continue?
- **What runs:** Download to a partial file, compare its SHA-256 fingerprint with the manifest, promote the file after a match. With your second confirmation, launch the installer and quit.
- **What is sent:** GET the manifest's HTTPS installer URL and redirects; no project/key added. Hosts are not restricted to GitHub.
- **AI involved:** None.
- **Bounded by:** 60-second timeout per socket operation, no total byte cap. Failure deletes the partial file. A matching hash proves agreement with that manifest, not independent publisher identity. Busy app defers install.

### 31. Dismiss an update offer

- **You do:** Later; Skip this Version; close the update window.
- **What runs:** Close the offer or persist the skipped version. During download, suppress completion/install prompts.
- **What is sent:** An already-started download can continue to its destination.
- **AI involved:** None.
- **Bounded by:** Dismissal does not cancel transfer. A verified downloaded installer can remain in ~/.spec_critic/updates; there is no automatic age cleanup.

### 32. Close or stop desktop work

- **You do:** Close the app; confirm Close anyway? for live review, drawing analysis or report export.
- **What runs:** Drain the trace recorder and destroy the window. Batch work continues remotely and may be resumed next launch; there is no review Stop button.
- **What is sent:** Already-started remote requests may have been sent; close is not a remote batch-cancel request.
- **AI involved:** None.
- **Bounded by:** Live/drawing results can be lost; active export can be incomplete. Paid work is not rolled back or refunded by the app. Batch record may survive; local close has no undo.

### 33. Read an exported report

- **You do:** Filter/search; expand/collapse; contents; Copy; Print; open evidence links.
- **What runs:** Filter/render/copy/print local report data. An evidence link opens the source in your browser.
- **What is sent:** Nothing for report controls; evidence links contact their displayed public destinations with normal browser metadata.
- **AI involved:** None.
- **Bounded by:** Display filters do not change saved findings. Browser printing, clipboard and external-link behavior belong to your browser/OS.

### 34. Configure report chat

- **You do:** Ask AI open/close; Use key; Forget; model/effort selectors; New chat; Copy chat; Print chat.
- **What runs:** Hold a reader-supplied key in memory and model/effort preferences in tab session storage. Reset/copy/print local chat; a model change, Forget or New chat stops an active turn.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** These controls do not start an AI request. Key is forgotten on reload/close, not embedded in the report. Closing the chat panel merely hides it and does not stop an active request.

### 35. Ask about the report

- **You do:** Send; Enter; a starter question; Ask about selected text. Paste adds text to the question.
- **What runs:** Stream a browser API request, then automatically run requested report tools/web tools and continuations. Commit conversation only on a complete answer.
- **What is sent:** Report text in the system context, committed chat history, your question/pasted or selected text, and tool results to api.anthropic.com with your browser key.
- **AI involved:** claude-opus-5-5 by default (choices: claude-opus-5-5, claude-sonnet-5-5); effort medium by default, selectable low, medium, high; adaptive summarized thinking; 64,000 output tokens; temperature omitted.
- **Bounded by:** Up to 8 report-tool rounds and 5 pause continuations per turn; history trims whole turns toward 24 messages, so a large single turn can exceed that target. Each request allows 5 searches and 3 fetches. No total turn dollar cap or explicit browser request timeout/retry loop.

### 36. Stop report chat

- **You do:** Stop; leave the page; change model, Forget or New chat during a turn.
- **What runs:** Abort the browser stream and reject late events. Remove the unfinished turn from future conversation; partial text can remain visibly marked.
- **What is sent:** No new question; previously sent content and consumed usage cannot be retracted.
- **AI involved:** None.
- **Bounded by:** Completed filter/scroll/highlight tool effects are not undone. The app cannot certify when provider billing stops. Hiding the panel alone is not Stop.

### 37. Inspect traces from a terminal

- **You do:** python -m src.tracing list or show; --help.
- **What runs:** Read selected local run metadata and records; print a summary or detail.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** No AI or network. Access requires permission to read the trace folder; the output may contain project content.

### 38. Prune traces explicitly

- **You do:** python -m src.tracing prune with the chosen age/count options.
- **What runs:** Select local run directories and delete the chosen traces (the command lists them before asking for confirmation unless --yes is set).
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Deletion has no app undo and does not delete reports, provider records or verification cache. Check the command's --help before running it.

### 39. Recover from a terminal

- **You do:** Run scripts/recover_batch.py; --help for arguments; Ctrl-C to stop local polling.
- **What runs:** Use saved state or explicit batch ID/module/inputs; poll/collect and perform remaining pipeline stages; optionally export diagnostics JSON.
- **What is sent:** Batch IDs and subsequent stage payloads to configured API/results destinations, as for GUI recovery.
- **AI involved:** Help/polling: None. Remaining stages use the engine table.
- **Bounded by:** Local polling is bounded; Ctrl-C does not cancel the remote batch. Recovery costs omit earlier unsaved research/drawing usage. Follow-up calls can be billed.

### 40. Use the separate edit applier

- **You do:** python -m applier or spec-critic-apply; optional --assist; --help for policies and modes.
- **What runs:** Read the sidecar/specs, gate proposals, locate targets, check conflicts and write edited copies plus receipts. Default tracked changes lets you accept/reject in Word. Optional assistance searches/reads document elements and selects a location.
- **What is sent:** Default: nothing. With --assist, issue/proposal/available candidate excerpts and requested document text go to the configured Anthropic API endpoint.
- **AI involved:** Default: None. Optional: claude-sonnet-5-5 by default (--assist-model can change it); effort, thinking and temperature omitted; 2,000 output tokens.
- **Bounded by:** Assist uses up to 6 tool rounds and 2,000 output tokens per call. Candidate ID/text is validated; it cannot rewrite replacement wording. --mode direct, broader policies and force-status options can weaken defaults. This is a separately invoked writer, not part of the desktop review.

### 41. Automatic startup and UI housekeeping

- **You do:** Launch Spec Critic; no review click.
- **What runs:** Configure rotating logs; load key/preferences; offer saved-batch resume; then on Windows check for updates when due. UI animation, queue-drain and debounce timers repaint/schedule local work.
- **What is sent:** Windows update GET/redirects to https://github.com/Abe-Borg/Claude-Spec-Critic/releases/latest/download/latest.json by default, at most once per 1 day(s) at launch. No project/key added by updater.
- **AI involved:** None.
- **Bounded by:** SPEC_CRITIC_DISABLE_UPDATE_CHECK disables checks; URL/state overrides are supported. Resume waits for your answer. Startup does not submit a fresh AI review.

### 42. Automatic input counting

- **You do:** Loaded file/selection/context/program changes; request preflight during runs.
- **What runs:** Count locally with cached tokenizer data; if absent in a source install, download https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken. Count the largest selected request through Anthropic; pipeline preflights size built requests too.
- **What is sent:** Tokenizer download sends no project content/key. Provider counts send the constructed spec/context/prompt/tool request; drawing counts include an anchor PDF. Routed GUI gauge may use a local estimate instead.
- **AI involved:** None.
- **Bounded by:** Counting is not a generated answer (AI involved: None). Failed counts use padded local estimates where available. SDK defaults: 2 retries; 600-second I/O and 5-second connect timeout, not a whole-run deadline. Clearing prevents scheduled calls but does not stop active HTTP.

### 43. Automatic location and client research

- **You do:** A started run uses a profile-enabled module.
- **What runs:** Data-center programs research a shared jurisdiction core once, then run discipline supplements in parallel. Standalone modules also get the core. Search/fetch runs on the vendor's servers; cited URLs are matched to retrieved evidence. Shared items need explicit module applicability to control compliance or the governing basis. The profile is spliced into context and can be trimmed to fit.
- **What is sent:** Location/client and shared jurisdiction questions to api.anthropic.com; discipline supplements also send the previously researched core and signals extracted from their specs as untrusted data. Model-chosen queries/URLs reach vendor web tools. Pending-batch resume uses saved profiles without researching again.
- **AI involved:** claude-sonnet-5-5; effort high; adaptive thinking; output cap 64,000 tokens; temperature omitted.
- **Bounded by:** Default dimension search/fetch budgets: Shared jurisdiction core: jurisdiction_governing_codes 24/8, jurisdiction_ahj 20/6, jurisdiction_client 14/5, jurisdiction_site 12/5; Hyperscale Data Center — Fire Suppression (US/Canada): fire_suppression_details 28/9; Hyperscale Data Center — Architecture (US/Canada): architectural_details 28/9; Hyperscale Data Center — Electrical (US/Canada): electrical_details 28/9; Hyperscale Data Center — Electronic Safety & Security: Fire Detection & Alarm (US/Canada): fire_alarm_details 28/9. Up to 4 research calls; 8 pause continuations plus a submission reminder. Paused search overrun is checked after the response, not a global billing cap. Partial failure is labeled; all-dimension failure stops review submission. web_fetch is attached even for an unsupported override.

### 44. Automatic review retry and repair

- **You do:** A started review suffers transient error, unusable/truncated output, or an extended-output beta rejection.
- **What runs:** Retry eligible live failures; try the bounded review repair pass. Reattach saved repair batches; preserve primary results on repair failure. A rejected extended beta is resubmitted at the ordinary cap.
- **What is sent:** The relevant review request again, with repair instructions where needed, to the API.
- **AI involved:** claude-opus-5-5; effort medium; adaptive thinking; output cap 128,000 tokens; temperature omitted.
- **Bounded by:** Shared live policy allows 3 attempts and 300 seconds of retry waiting. Refusals do not retry as truncation. Repair is bounded, not an indefinite loop; batch submission uses SDK retries. Paid primary results survive failed repair; pending repair can defer downstream paid work.

### 45. Automatic batch polling and collection

- **You do:** A batch was submitted or resumed.
- **What runs:** Poll status, download results, parse records and reattach a recorded repair. Routed partitions may collect in parallel. A broken results stream restarts its download under the shared retry policy.
- **What is sent:** Batch IDs for status and API-provided results URLs; network metadata/authentication managed by the SDK.
- **AI involved:** None.
- **Bounded by:** Local polling: 4 hours, 10 consecutive errors; nominal intervals 15 to 120 seconds with error backoff. Detaching does not cancel the batch. Result download allows 3 attempts. SPEC_CRITIC_PENDING_BATCH_PATH changes recovery location.

### 46. Automatic finding triage

- **You do:** A review has eligible lower-stakes findings.
- **What runs:** Local rules route some findings to skips. Haiku classifies eligible findings as locally resolvable or needing web verification. Critical/high or code-referenced findings are excluded from Haiku eligibility.
- **What is sent:** Eligible finding descriptions/references and triage instructions to api.anthropic.com.
- **AI involved:** claude-haiku-4-5; 8,000 output tokens; effort, thinking and temperature omitted.
- **Bounded by:** Classification failures fall back to web-required. Local skips avoid the web verifier, not necessarily the earlier reviewer/triage AI. Local-skip routing is always enabled in the shipped code; there is no disable switch.

### 47. Automatic verification, reuse and escalation

- **You do:** New findings need external checking.
- **What runs:** Try eligible cache entries/share equivalent in-flight work; otherwise route verification, search/fetch, parse verdicts and apply evidence gates. Continue paused calls or remind missing verdicts; retry eligible failures. Unresolved batch tails can run live. Eligible high-stakes insufficient evidence can escalate; grounded disagreement can be contested.
- **What is sent:** Finding details, quoted spec passage where available, governing context and retrieved/tool history to api.anthropic.com; public queries/URLs through vendor tools. Optional supplied-source reuse is named separately.
- **AI involved:** Strict: claude-sonnet-5-5; effort low; thinking omitted; output cap 64,000 tokens; temperature omitted. Standard: claude-sonnet-5-5; effort medium; adaptive thinking; output cap 64,000 tokens; temperature omitted. Deep/escalated: claude-opus-5-5; effort medium; adaptive thinking; output cap 64,000 tokens; temperature omitted. Local-skip/cache lookup: None.
- **Bounded by:** Search caps per request: CRITICAL 8, HIGH 7, MEDIUM 5, GRIPES 3; fetch cap 3, content 50,000 tokens. Default/deep pause continuations: 2/4; shared retries 3 attempts. Operational failures do not trigger escalation. Cache defaults: 60 days, 5,000 entries, single-flight wait 900 seconds; cache age is shown. These bounds do not cap all phases' combined spend.

### 48. Automatic selected cross-spec check

- **You do:** Cross-spec coordination was selected for the started run.
- **What runs:** Compare the module's specs, split oversized inputs by module chunk rules, parse/anchor-check findings and verify the new ones. One output recovery per pass can re-request an unparseable response or split truncated output into smaller requests; all returned usage is counted.
- **What is sent:** Current chunk's spec texts and already-identified findings with the module prompt to api.anthropic.com.
- **AI involved:** claude-sonnet-5-5; effort high; adaptive thinking; output cap 96,000 tokens; temperature omitted.
- **Bounded by:** Request budgets/chunking/shared retries apply; the output recovery allowance is shared across chunks. Indivisible packages still fail. Reduced coordination scope and failed/skipped chunks are named. The default pass cannot see relationships across module or chunk boundaries.

### 49. Automatic requirements compliance check

- **You do:** A profile-enabled run has researched requirements.
- **What runs:** Compare specs to the profile, normalize coverage and settle addition proposals; mark missing coverage rows. One output recovery per pass can re-request an unparseable response or split truncated output into smaller requests; all returned usage is counted. Verify new findings.
- **What is sent:** Profile items, relevant specs and prior findings to api.anthropic.com.
- **AI involved:** claude-sonnet-5-5; effort high; adaptive thinking; output cap 64,000 tokens; temperature omitted.
- **Bounded by:** Request/chunk/retry bounds apply; the output recovery allowance is shared across chunks. Indivisible packages still fail. Smaller requests disclose their reduced scope; failed/skipped chunks keep coverage incomplete and absence-based additions held. A coverage label is the model's judgment; complete rows do not prove all applicable law was researched or the design complies.

### 50. Automatic drawing-impact explanation

- **You do:** A drawing digest is present in the completed review context.
- **What runs:** Relate the text digest to final findings; validate referenced finding IDs and discard unknown IDs.
- **What is sent:** Digest text, final finding summaries and synthesis instructions to api.anthropic.com; no second raw-PDF upload by this pass.
- **AI involved:** claude-sonnet-5-5; effort high; adaptive thinking; output cap 32,000 tokens; temperature omitted.
- **Bounded by:** Shared request/retry bounds apply. This is interpretation of the digest, not a check of the actual CAD/BIM model or every sheet.

### 51. Automatic local audit and report preparation

- **You do:** Start/finish a run; read/write state and cache.
- **What runs:** Prune old/excess traces at recorder startup; record selective events/findings; deduplicate while preserving per-file edit occurrences. Prepare diagnostics and open the save-report prompt at completion. Update cache and saved state.
- **What is sent:** Nothing.
- **AI involved:** None.
- **Bounded by:** Trace defaults: 30 days/50 recent runs, pruned on run start. Disable/adjust with SPEC_CRITIC_TRACE_RETENTION_DAYS and SPEC_CRITIC_TRACE_MAX_RUNS. Trace failure warns and review continues; queue growth is warned about, not hard-capped. Reports/state have no general age cleanup.

### 52. Automatic report-chat tools and follow-ups

- **You do:** A submitted chat answer asks for report tools or pauses for web work.
- **What runs:** Run named local tools to query/filter/navigate/highlight/calculate; send tool results and continue. Web search/fetch runs on Anthropic's servers. No per-tool confirmation.
- **What is sent:** Report-tool results and conversation history to api.anthropic.com; web queries/URLs through vendor tools.
- **AI involved:** claude-opus-5-5 by default (choices: claude-opus-5-5, claude-sonnet-5-5); effort medium by default, selectable low, medium, high; adaptive summarized thinking; 64,000 output tokens; temperature omitted.
- **Bounded by:** 8 tool rounds and 5 pause continuations. Unknown/malformed tools return errors; partial-turn conversation is discarded on failure. Browser report changes already made remain. Chat web tools do not carry the desktop source-quality blocklist.

### 53. Explicitly enabled experiments

- **You do:** Set experiment environment switches before a run.
- **What runs:** SPEC_CRITIC_PROJECT_CONTEXT_CACHE changes prompt caching; SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT changes review output shape; SPEC_CRITIC_EVIDENCE_VALIDATION observes support; SPEC_CRITIC_SOURCE_REUSE can supply earlier evidence; SPEC_CRITIC_RESEARCH_CACHE can reuse a profile; SPEC_CRITIC_CROSS_COORDINATION finds candidates or sends observation-only judgments.
- **What is sent:** Default: no added experiment requests. Observe-mode coordination sends bounded passage pairs to the API; other switches alter supplied context/request shape or replace retrieval with reuse.
- **AI involved:** Observation coordination uses claude-sonnet-5-5; effort high; adaptive thinking; output cap 16,000 tokens; temperature omitted.. Candidate-only and local cache lookup: None. Other active stages keep their configured models.
- **Bounded by:** All these experiments are off by default. Coordination limits 40 candidates, 4 per file pair, 10 per request; observations do not change reported findings. Source reuse weakens the 'retrieved in this conversation' claim and is labeled. Research reuse is not fresh research.

<a id="tools"></a>
## What the AI may touch

*Blast radius*

| Tool or capability | What it can do | Constraint |
| --- | --- | --- |
| Review/cross/compliance submit tools | Return proposed structured findings and coverage. | Required field structures, called schemas, plus local shape/anchor checks; no desktop write tool. SPEC_CRITIC_STRICT_TOOL_USE can disable strict schemas; text fallback remains reachable. |
| Verifier/research web_search and web_fetch | Search/fetch public information on the vendor's servers. | Per-request budgets and desktop source-quality blocklist; fetch gated by model for verification, not research. Internal vendor code execution can filter results; no desktop shell is exposed. |
| Verdict/profile/triage/impact submit tools | Return judgments, source references, classifications or impact statements. | Named payload parsing and mechanical gates. A valid schema does not establish truth. |
| Report get_findings | Read structured findings and return a bounded subset. | Reads the embedded report; does not read your source DOCX files. |
| Report filter_report / clear_filters | Change visible filters without another click. | Known filter choices/local report data; does not change saved findings. |
| Report navigate_to_section | Scroll to an existing page element. | Existing element IDs; no browser navigation to a new URL. |
| Report highlight_terms / clear_highlights | Mark words in the report display. | Escaped literal search terms and bounded marks; effects remain after a stopped turn. |
| Report calculate | Evaluate basic arithmetic over supplied numbers. | A token parser and operator stack; no JavaScript eval or general code execution. |
| Separate applier assistance | Search/read candidate elements; choose_element or decline. | Candidate IDs/text validated; replacement wording comes from sidecar. Default off; no model-authored replacement slot. |

**The categorical limit**

The desktop model tool lists expose no local shell, arbitrary filesystem write or installer launch. Models cannot approve a design or apply reviewer edits inside the GUI. The Python application itself has your user account's file/network privileges; it is not a sandbox, an isolated execution area. Hosted tools, report display tools and explicitly invoked applier assistance have the exceptions described above.

<a id="terms"></a>
## What the labels mean, exactly

*Exact words*

**Grounded — the exact claim**

A cited URL matched an accepted URL retrieved by a tool, or eligible supplied evidence under the opt-in reuse path. This does NOT prove the page is authoritative or that its words support the model's judgment.

**Verified — the exact claim**

A supported confirmed/corrected verdict passed the implemented grounding/citation gates, including the required source-quote field. URL comparison normalizes spelling differences such as tracking parameters. This does NOT prove the quoted words appear on that page, the source is primary/current/adopted, or the conclusion is correct. Default validation does not re-derive the engineering judgment.

**Locally classified — the exact claim**

The routing/triage path treated a finding as locally resolvable rather than requiring external verification. This does NOT mean no AI participated, that a regulation was checked, or that the issue is safe to ignore.

**Edit suggested — the exact claim**

A structured proposal survived the applicable shape and available-text checks. Anchor matching accepts whitespace differences; unavailable or unattributable source text can remain unchecked. This does NOT mean the proposed edit is approved, uniquely located, suitable for automatic application or complete.

**Completed and covered — the exact claim**

The reported stage returned its expected form or the coverage record names a requirement's status. Diagnostics expose failed/skipped/incomplete work. This does NOT mean every possible conflict or applicable requirement was reviewed; cross-check partitions and research gaps still matter.

**Secure — the exact claim**

This dossier names specific mechanisms: credential lifetime, selective redaction, prompt boundaries, escaped report rendering and updater integrity checks. It does NOT claim security certification, end-to-end local privacy, encrypted artifacts, resistance to malware, or guaranteed model obedience.

<a id="local"></a>
## The parts with no AI in them

*No AI here*

- DOCX/context extraction, path deduplication and duplicate-name refusal.

- Program routing from module signals; local pattern/structure alerts and code-pin formatting.

- Local token estimates, request budgets, edit-shape/available-text anchor checks and occurrence grouping.

- URL normalization/acceptance, cache keys/eligibility/age and deterministic status labels.

- Cost arithmetic, diagnostics rendering, Word/JSON/HTML export and report filters/copy/print.

- Trace recording/pruning/viewing, preference/recovery file handling, version comparison and updater fingerprint checks.

- This help, its contents navigation and its vector diagram.

**With the network disconnected**

You can read saved reports/traces, use their local controls, read this help, edit context, and extract/count locally if tokenizer data is already present. Exporting retained results and the applier's default local work still work. AI review/chat/research/verification, remote batch recovery and update downloads need a connection. A source install missing tokenizer data cannot complete local counting until that data is available.

<a id="security"></a>
## Security and privacy in specific terms

*Mechanisms*

| Concern | How it is handled |
| --- | --- |
| Desktop credentials | Startup prefers keyring service SpecCritic, account anthropic_api_key; then spec_critic_api_key.txt in the user config directory or executable/source fallback; then ANTHROPIC_API_KEY. Typed keys stay in captured in-memory credentials. Plaintext fallback is not encrypted; POSIX permissions are tightened best-effort. No GUI save-key command. |
| Browser credentials | Ask AI key is memory-only, deleted on reload/close; old session-storage key is removed. Model/effort preferences, not the current key, use session storage. Browser extensions/compromise can still read page memory. |
| Untrusted documents and pages | Requests place project text inside marked data boundaries and escape boundary characters; model instructions ask it to ignore embedded directives. HTML output escapes data and restricts scripts with a Content Security Policy, browser rules governing loads. These reduce injection risk, not model hallucination or malicious source text. |
| Traces and logs | Trace/diagnostic redaction recognizes secret-shaped fields and key/bearer patterns; it does not remove project text. Ordinary rotating log has no universal redaction layer. Default trace is selective; Deep can store full prompts and response details. Protect exported/copied artifacts. |
| Local state | ~/.spec_critic/ui_state.json: preferences/profile; ~/.spec_critic/pending_batch.json: request mappings, paths and effective context/profile, not saved spec bodies; ~/.spec_critic/verification_cache.json: verdict cache. Readable JSON, no application encryption; recovery re-extracts available source files. |
| Retention | Verification defaults 60 days/5,000 entries, via SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS / SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES; zero disables that bound. Traces at %LOCALAPPDATA%/SpecCritic/traces (Windows), ~/Library/Application Support/SpecCritic/traces (macOS), or $XDG_STATE_HOME/SpecCritic/traces (Linux; usually ~/.local/state/SpecCritic/traces), run.json, spans.jsonl, events.jsonl, prompts.jsonl, findings.jsonl; 30 days/50 runs, pruned at run start. SPEC_CRITIC_TRACE_RETENTION_DAYS / SPEC_CRITIC_TRACE_MAX_RUNS adjust or disable those bounds; SPEC_CRITIC_TRACE_DIR changes the folder. Trace/Deep controls or SPEC_CRITIC_TRACE / SPEC_CRITIC_TRACE_DEEP change capture. Other artifacts remain until overwritten/deleted. |
| Other disk artifacts | ~/.spec_critic/logs/spec_critic.log: rotating text log, 2 MiB and 3 backups by default; SPEC_CRITIC_LOG_PATH changes it. ~/.spec_critic/update_check.json: JSON update state; ~/.spec_critic/updates: installers. Opt-in research reuse stores ~/.spec_critic/research_cache.json. Tokenizer cache follows TIKTOKEN_CACHE_DIR, then DATA_GYM_CACHE_DIR, otherwise OS temporary cache. Reports/sidecars use your selected location. |
| Network and local server | No listening server, analytics client or crash-upload client is implemented in shipped paths. API SDK/proxy/base-URL configuration and explicit browser links change network behavior. Your desktop process is not isolated from your user account. |
| Update integrity | Initial/final HTTPS and a manifest-provided SHA-256 fingerprint are checked. Redirects can be followed before final-scheme rejection; there is no hostname allowlist, independent signature check in this downloader, or total installer byte cap. Closing an offer does not stop transfer. |
| Provider terms | Client code cannot prove provider retention, training exclusion or access policy. Check Anthropic's current API terms and your organization's agreement before submitting sensitive work. |

<a id="money"></a>
## What you pay for, and what the meter knows

*Money*

Anthropic bills API work to the account behind your key. You pay for consumed input/output/thinking tokens, cache activity and web-search requests. A tokenizer/count estimate is not a measured invoice. The app's rate table can drift from provider prices, tiers or contract terms; the provider's console is the billing record.

| Configured model | Input / output per million tokens (USD) | Cached read per million tokens (USD) |
| --- | --- | --- |
| claude-haiku-4-5 | $1 / $5 | $0.1 |
| claude-opus-5-5 | $4 / $20 | $0.2 |
| claude-sonnet-5-5 | $2 / $10 | $0.2 |

The estimator applies a 50% token discount in batch mode, not to searches. Cache write/read multipliers: short write 1.25×, long/unknown write 2×, usual read 0.1×; model-specific read prices override the read multiplier. Searches are estimated at $10 per 1,000 searches. Fetched content contributes tokens. Provider usage counts, when available, are observations; dollar totals remain estimates. Unknown/unpriced attempts are named, not made exact by a total.

**Failed and stopped work**

Retries, repairs, continuations, research and small-tail live fallback can add cost without another confirmation. Consumed work can still be billed after failure, Stop or close. Drawing estimates count an anchor chunk and scale the others; canceled/refused merging can follow paid analysis. Recovered-run estimates do not include earlier unsaved research/drawing usage. HTML chat has no invoice meter. There is no whole-run app spend cap or app refund mechanism.

<a id="limits"></a>
## What this does not do

*Honesty*

- It does not approve work, certify compliance or replace you, a licensed reviewer, the client or the authority having jurisdiction (the body that adopts/enforces the local requirements).

- It can miss Word/PDF content, symbols, revision meaning, scope or routing signals. Check extracted passages, input coverage and the drawing digest against the originals.

- It can confidently apply the wrong code basis or misread a real page. Code pins, URL gates and source quotes reveal assumptions; they do not establish current adoption or semantic truth. Check primary adopted text and amendments.

- It cannot search private/unpublished authority requirements or guarantee access to paywalled standards. Partial research, refused calls and exhausted searches are not evidence of no requirement.

- It does not compare every cross-discipline or cross-chunk relationship by default. Unsupported Division 27 and non-fire-alarm Division 28 work are visible coverage gaps; independent modules are not a comprehensive coordination review.

- It cannot prove a suggested edit is uniquely anchored or professionally correct. Shape checks and available-text matching reduce bad instructions; read the whole clause before applying.

- It does not protect a source file from a report export aimed at that same path. Use a separate output filename and preserve originals; existing same-stem sidecars are replaced without their own confirmation.

- It cannot reconstruct every request or every dollar from selective traces and recovered state. Trace capture may fail; retention may remove records. Preserve what you need before cleanup.

- It cannot confine project data to your machine, encrypt your artifacts or guarantee an external service/browser is private. Read the boundary before loading sensitive inputs.

<a id="audit"></a>
## Check it yourself

*The point*

1. Read the report's Run Diagnostics banner first. You will see failed specs, skipped work and integrity/coverage warnings; investigate these before treating zero findings as reassurance.

2. Open a consequential finding's evidence panel. You will see model/status, accepted/rejected URLs, source quote and cache age. Follow the link yourself and compare the actual clause, edition, adoption and amendments.

3. Check the reviewed file list, selected program and researched location/client profile. Compare the drawing digest to your sheets; unreviewed/misrouted files and omitted requirements remain your responsibility.

4. Open the exported .edits.json and applicable .profile.json in a text editor. You will see proposals/occurrences and the researched profile, not an approval. Compare the existing/replacement text with your original; the reviewer has not applied it.

5. Open Run Diagnostics and Copy to Clipboard, or inspect the trace folder/viewer. You will see recorded attempts, tool/evidence events and usage. Default capture is selective; Deep adds sensitive detail on future runs, not retroactively.

6. Compare the estimate and unknown/unpriced attempts against your Anthropic console. You will see billed usage that may include other apps and earlier failed/stopped work; recovery figures omit some earlier stages.

7. Disconnect the network and reopen this help, saved report and local trace viewer. Their local reading controls still work; AI and recovery do not. A missing local tokenizer cache can block fresh extraction/count preparation.

8. Use an approved firewall/network monitor while selecting files, changing context, attaching drawings, launching on Windows and starting a review/chat. You will see the pre-review count, update/download and follow-up paths described here, including actual redirect hosts.

9. Inspect the source register in docs/TRUST_CLAIMS.md and run the repository's tests locally. You will see which constants generate this copy and which mechanical contracts are checked. Compare the source version to the app version; local tests cannot prove hosted-provider behavior.

**Further reading**

- [Anthropic API privacy](https://privacy.anthropic.com/en/collections/10631468-api)
- [Anthropic Trust Center](https://trust.anthropic.com/)
- [Anthropic API and pricing documentation](https://platform.claude.com/docs/en/about-claude/pricing)
- [Source, claims ledger and tests](https://github.com/Abe-Borg/Claude-Spec-Critic)
- [Spec Critic releases](https://github.com/Abe-Borg/Claude-Spec-Critic/releases)

**Still not convinced?**

Good. Use Spec Critic as a second reader whose claims you challenge. Start with material you can independently check, keep failed and uncertain items visible, and verify consequential findings against the project record and adopted primary sources. If you cannot establish a finding's basis, leave the decision open and ask the responsible professional.
