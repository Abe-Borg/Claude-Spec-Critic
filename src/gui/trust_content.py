# Purpose: explain the shipped trust mechanisms to the design professional who
# must put their name on the output. If the implementation changes, this changes
# with it. A trust document that has drifted from the code is worse than none.
"""Source-bound trust copy, shared by the native dialogs and offline dossier.

Claim IDs refer to docs/TRUST_CLAIMS.md. Keep this module free of Tk and I/O.
Numbers, models and destinations belong in fact_values, never copy templates.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Block:
    kind: str
    title: str
    body: object
    claims: tuple[str, ...]


@dataclass(frozen=True)
class Topic:
    anchor: str
    kicker: str
    title: str
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Action:
    identity: str
    title: str
    trigger: str
    runs: str
    sent: str
    ai: str
    bound: str
    claims: tuple[str, ...]

    def rows(self, facts: dict[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple((label, value.format_map(facts)) for label, value in zip(
            RUNTIME_LABELS, (self.trigger, self.runs, self.sent, self.ai, self.bound)
        ))


# The app must not import the separate writer. Tests pin these to its source AST.
ASSIST_ROUNDS_PIN = 6
ASSIST_TOKENS_PIN = 16_000
ASSIST_EFFORT_PIN = "medium"
ABOUT_REFERENCE_HOSTS_PIN = ("polyformproject.org", "www.linkedin.com", "github.com")
TRACE_LOCATIONS_PIN = "%LOCALAPPDATA%/SpecCritic/traces (Windows), ~/Library/Application Support/SpecCritic/traces (macOS), or $XDG_STATE_HOME/SpecCritic/traces (Linux; usually ~/.local/state/SpecCritic/traces)"

RUNTIME_LABELS = ("You do", "What runs", "What is sent", "AI involved", "Bounded by")
DETAIL_BUTTON = "I'm not convinced — show me exactly what runs →"
LEAD = "Spec Critic gives you findings to check, with visible evidence checks and failure signals, so you can decide what deserves your name."
CLOSING = "Fair. The points above are claims — here is the mechanism behind each one, action by action, plus how to audit any of it yourself."

SHORT_POINTS = (
    Block("text", "You can see how findings were checked", "The report separates the model's confidence from the verification status and shows accepted and rejected citations. A source match proves where evidence came from; you still check whether it supports the conclusion.", ("C01", "C07")),
    Block("text", "Rules and models have different jobs", "Local pattern checks flag template problems. Models propose findings and interpret sources; module rules are each discipline's built-in instructions. 'Locally classified' can follow AI triage — sorting claims for later checks — so it does not mean no AI was used.", ("C02", "C03", "C05")),
    Block("text", "Your review produces proposals for your decision", "The reviewer exports a report and edit instructions. It checks quoted anchors — text used to locate an edit — against available extracted text, allowing whitespace differences. The separate applier can change copies. Export can overwrite your chosen path; use a different report filename.", ("C08",)),
    Block("text", "Background work has named triggers and exceptions", "Changing loaded inputs can send text to Anthropic for token counting, sizing a model request, before a review. Windows startup can check for updates; source installs may download tokenizer data. Started reviews launch research, verification, retries and selected follow-up checks automatically.", ("C04", "C06", "C12")),
    Block("text", "Your documents cross a visible service boundary", "AI requests send relevant project text, including any attached drawing-analysis text, to Anthropic by default; no drawing file is uploaded. Typed keys stay in memory, but optional key files are plaintext. Traces, local run records, are on by default and can contain project content; dollar figures are estimates.", ("C04", "C10", "C11", "C12")),
    Block("text", "Failures and reused results leave visible signals", "Diagnostics name failed reviews and skipped work. Evidence panels show the age of reused results, sources and verifier details. Closing a batch window leaves the remote batch running; the live transport has no resume.", ("C06", "C07", "C14")),
    Block("text", "A clean report still needs your judgment", "Routing, extraction, research and models can miss issues. Cross-spec checking stays within a module and its chunks by default. Neither a Verified label nor an edit proposal certifies compliance or approves a change.", ("C01", "C09")),
)


# No URL appears in a loadable asset attribute. All external references are
# explicit links in Further reading. Tk draws these SVG primitives itself.
FLOW_DESCRIPTION = (
    "Your computer extracts specifications and keeps reports, state and traces. "
    "AI and counting requests send project text to Anthropic. "
    "Anthropic may search and fetch public sources. Optional or automatic paths "
    "go to update hosts, tokenizer storage, configured endpoints and browser links. "
    "Dashed lines mark automatic counting and conditional paths; results return to your computer."
)
FLOW_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 300" role="img" aria-label="''' + FLOW_DESCRIPTION + '''">
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
</svg>'''

FURTHER_READING = (
    ("Anthropic API privacy", "https://privacy.anthropic.com/en/collections/10631468-api"),
    ("Anthropic Trust Center", "https://trust.anthropic.com/"),
    ("Anthropic API and pricing documentation", "https://platform.claude.com/docs/en/about-claude/pricing"),
    ("Source, claims ledger and tests", "https://github.com/Abe-Borg/Claude-Spec-Critic"),
    ("Spec Critic releases", "https://github.com/Abe-Borg/Claude-Spec-Critic/releases"),
)


def _action(identity, title, trigger, runs, sent="Nothing.", ai="None.",
            bound="Local work; no model charge. Closing a picker without choosing makes no change.",
            claims=("C03",)):
    return Action(identity, title, trigger, runs, sent, ai, bound, claims)


ACTIONS = (
    _action("A01", "Browse specifications", "Browse; select Word specification files.",
            "Accumulate readable DOCX files, reject different files sharing a name, extract text locally, then schedule the counting card below.",
            "Counting can send the largest selected spec, its filename, module prompt and Project Context to {api_host}.",
            bound="Unreadable files are warned about; duplicate inputs collapse. Counting can happen before Submit Batch.", claims=("C03", "C04")),
    _action("A02", "Drop specifications", "Drop files into the specification input.",
            "Parse dropped paths, filter supported files and run the same selection and counting flow as Browse.",
            "The counting request described under Browse, when readable files are added.",
            bound="Only supported specification formats enter this flow; unavailable drag-and-drop falls back to Browse.", claims=("C03", "C04")),
    _action("A03", "Choose the files to review", "Toggle a file checkbox, All or None.",
            "Recompute the selected-input gauge and review gate; schedule counting when selected extracted specs remain.",
            "Selected largest spec and current prompt/context to {api_host} for counting.",
            bound="Unchecking files changes future submissions. An active count request is not canceled.", claims=("C03", "C04")),
    _action("A04", "Clear the file selection", "Clear beside Browse.",
            "Clear selection/gauge, disable review, invalidate old analysis results and cancel scheduled counting.",
            bound="Active HTTP requests can finish. Clear does not erase reports, traces or a remote batch.", claims=("C03", "C06")),
    _action("A05", "Edit project context", "Type, paste or delete Project Context.",
            "Recount locally after editing settles; re-derive the drawing-analysis readout (one row per attached analysis with its current token count) and refresh selected-input counting.",
            "When specs are loaded, updated context may enter the counting request to {api_host}.",
            bound="Context limit: {context_cap} tokens, units used to size model text. Delete a drawing-analysis block to remove it from future requests; requests already sent with it are not retracted.", claims=("C03", "C04")),
    _action("A06", "Expand and save context", "Expand; Save & Close, or close the editor window.",
            "Open a local editor; Save & Close checks the context limit and copies text into the main field, whose counting flow then runs.",
            "Saving can trigger the input-count request; closing without saving sends nothing from this editor.",
            bound="Over-limit text is refused. Window close discards modal edits; this is not a project-file save.", claims=("C03", "C04")),
    _action("A07", "Attach reference files", "Attach Files… in either context editor.",
            "Read {context_formats} locally; wrap filenames and extracted text as context. Then merge and refresh counting.",
            "The merged text can enter counting and later AI requests to {api_host}; this attachment flow does not upload the original file bytes.",
            bound="Merged context over {context_cap} tokens is refused; failed reads are named. Images in a text-only extraction can be missed.", claims=("C03", "C04")),
    _action("A08", "Attach a drawing analysis", "Attach Drawing Analysis… in either context editor; choose the text output of your drawing-analysis program.",
            "Read the file ({analysis_formats}) locally and count its tokens with the local tokenizer; wrap it as the drawing-digest block, named for its file, and merge it into context. Show the count in the activity log and the files-panel readout, then run the ordinary context counting flow.",
            "Nothing by this step; no drawing file is uploaded. The merged text later enters counting and AI requests to {api_host} like the rest of Project Context.",
            bound="A file over {analysis_mib} MiB, an empty file and merged context over {context_cap} tokens are refused, never truncated; unusable files are named. The count is a local estimate, not the provider's. Spec Critic does not read drawings: the analysis is your program's output, and nothing checks it against the sheets.", claims=("C03", "C04")),
    _action("A09", "Choose a review program", "Review program selector.",
            "Persist the choice and update module scope/profile fields; re-analyze loaded files and count their new prompts.",
            "Loaded inputs can be sent for counting to {api_host}.", bound="An in-flight run keeps its submitted program. Scope and routing are not a guarantee of full discipline coverage.", claims=("C04", "C09", "C10")),
    _action("A10", "Describe the project location and client", "Edit city, state/province, country or client.",
            "Normalize the local fields; snapshot and save them when a profile-enabled review starts.",
            bound="No research starts just from these fields. Wrong or incomplete location/client data can lead to wrong researched requirements.", claims=("C09", "C10")),
    _action("A11", "Supply an API key", "Type or replace the API key.",
            "Hold the value in the field; counting/review workers capture their credential. Each run keeps its captured key.",
            "The key authenticates requests to the configured API endpoint when requests run; typing alone sends no request.",
            bound="The app does not save typed keys or put them into the environment. Startup can load a saved keyring/plaintext/environment key. Protect those stores yourself.", claims=("C10", "C12")),
    _action("A12", "Choose batch or live transport", "Real-time toggle; Keep Real-time; Use Batch instead; suppress the cost warning.",
            "Show the session cost warning when applicable and persist the chosen transport/warning preference.",
            bound="Batch is the fresh-install default. A persisted live choice stays live; changing the toggle does not convert an active run. Live work uses standard token rates.", claims=("C05", "C10", "C11")),
    _action("A13", "Choose live concurrency", "Real-time workers: {workers}.",
            "Persist the limit used by the next live run across routed modules.",
            bound="This bounds simultaneous reviews, not total requests or spend. Batch ignores it.", claims=("C06", "C10")),
    _action("A14", "Choose cross-spec checking", "Cross-spec coordination checkbox.",
            "Persist the option for the next review; a started review later runs the cross-check card if selected.",
            bound="Selected by default on a fresh install; checking is limited to each module and chunk.", claims=("C09", "C10")),
    _action("A15", "Adjust reading size", "Font size selector.", "Change widget scaling and persist your preference.", claims=("C03", "C10")),
    _action("A16", "Choose trace capture", "Show tracing tools; Trace; Deep.",
            "Reveal controls and change next-run capture. Trace is on and Deep is off by default; Deep can request summarized model thinking.",
            bound="Deep captures sensitive prompt/response detail and overrides trace-off if enabled. Hiding tracing tools does not turn tracing off.", claims=("C10", "C14")),
    _action("A17", "Submit a batch review", "Submit Batch; confirm routing when asked.",
            "Snapshot inputs/key/options; extract, pre-screen and route locally. Profile-enabled modules research first. Count/size built requests, submit review batches and save recovery state. Poll and launch dependent stages automatically.",
            "Relevant spec text/filename, effective context, module instructions and profile go to {api_host}. Research sends profile and observed corpus signals; batches send request mappings.", "{review_ai}",
            "Output baseline is in the engine table; large batch inputs at {review_threshold} tokens can request {review_extended} output tokens with {review_beta}. Canceling routing avoids review submission; prior counts/research may already have happened. No whole-run spend ceiling.", ("C04", "C05", "C06", "C09", "C11")),
    _action("A18", "Start a live review", "Start Review (live); confirm cost/routing when asked.",
            "Prepare as for batch; stream per-spec reviews under the global worker limit, then verification and follow-up passes. No saved live review state.",
            "Spec text, filenames, prompts/context/profile and later stage results to {api_host}.", "{review_ai}",
            "Concurrency choices: {workers}. Output stays at the baseline cap; oversized requests are refused. Failed calls/repair attempts can still cost money. Closing loses live results and cannot resume them.", ("C04", "C05", "C06", "C11")),
    _action("A19", "Resume saved work", "Resume unfinished batch? Yes.",
            "Load saved mappings/context/profile, reattach review/recorded repair batches, poll, collect and run outstanding dependent stages.",
            "Batch IDs for status/results, then findings/context/evidence for remaining calls to {api_host} or the returned results URL.",
            "Review retrieval: None. Remaining AI: {verifier_ai}; cross/compliance/impact use the engine table.",
            "Avoids resubmitting the saved primary review; dependent stages can incur new spend. Saved state lacks a complete prior-session usage ledger.", ("C04", "C06", "C10", "C11")),
    _action("A20", "Discard a recovery record", "Resume unfinished batch? No.",
            "Delete the matching local saved record.", bound="This does not cancel a remote batch or undo its billing. Record removal is not undoable through the GUI.", claims=("C06", "C10", "C11")),
    _action("A21", "Recover by batch ID", "Recover batch…; enter the ID and requested module/files.",
            "Use matching saved state when available; otherwise wait for the batch to end and reconstruct its mappings from results. Collect and run remaining stages.",
            "Batch ID/status/results requests; re-extracted context/findings/evidence for remaining API work.",
            "Retrieval: None. Remaining AI uses the engine table.",
            "Polling detaches at {poll_hours} hours. Bare-ID recovery needs the right module; missing inputs reduce available source checks. A results download can restart from its beginning.", ("C05", "C06", "C11")),
    _action("A22", "Save a Word report", "Save Review Report at completion; Save Word Report…; Retry or Cancel.",
            "Write the Word report, edit-instruction JSON and, when applicable, requirements-profile JSON beside it. Retry repeats local export from retained results.",
            bound="Save-dialog Cancel writes nothing. Export has no source-file guard; accepting an input filename can overwrite it. Same-stem sidecars overwrite without their own confirmation. A failure can leave partial output; no multi-file rollback. Results remain in memory until close. Check sidecar warnings.", claims=("C03", "C06", "C08", "C10")),
    _action("A23", "Save an HTML report", "Save HTML Report…",
            "Write a self-contained report and open its local file in your browser. The GUI export includes Ask AI; the exporter also supports a chat-free option.",
            bound="Opening the report makes no app request. Chat requires your browser key and a submitted question. HTML export has no source-path guard and can overwrite your chosen file; it does not write Word sidecars.", claims=("C03", "C08", "C10", "C13")),
    _action("A24", "Inspect run diagnostics", "Run Diagnostics; Copy to Clipboard.",
            "Render retained phase status, usage, estimated cost and timeline; copy diagnostic text to the OS clipboard.",
            bound="This is selective instrumentation, not the provider's invoice or a complete raw request log. Unknown usage is reported.", claims=("C03", "C11", "C14")),
    _action("A25", "Open local traces", "Show trace folder.", "Create/open {trace_path} with your OS file browser.",
            bound="The app passes a local path. Your file browser's own integrations are outside the app's network controls.", claims=("C03", "C10", "C14")),
    _action("A26", "Read traces offline", "Open trace viewer; choose a trace directory; select tabs, spans, findings or events.",
            "Open the bundled local HTML viewer; your browser reads the directory you select and renders its JSON/JSONL records.",
            bound="No app listening server or external viewer assets. Default traces omit some raw detail; a missing capture is not proof that work did not run.", claims=("C03", "C10", "C14")),
    _action("A27", "Change local panels and activity log", "Expand/collapse input, files or activity panels; activity-log Clear.",
            "Change visible local widgets; Clear empties the activity display/queue and resumes later log entries.",
            bound="Clearing the display does not clear disk logs, diagnostics, traces, remote work or spend.", claims=("C03", "C14")),
    _action("A28", "Read help and this explanation", "How It Works; How to Use; Why Trust It; About; contents/back/Close/Escape; a Further reading link.",
            "Render local native help; the dossier stacks over the short topic. A link deliberately opens your browser.",
            "Nothing for opening/scanning trust. An external link sends a browser request to its displayed destination.",
            bound="One Escape closes the top trust dialog and returns focus to its opener. Browser cookies/extensions are governed by your browser.", claims=("C03", "C12", "C15")),
    _action("A29", "Check for an update", "Check for Updates.", "Fetch and validate the release manifest, compare versions and show the result.",
            "GET {update_url} by default, plus redirects; connection metadata and updater User-Agent, without project text/key added by the updater.",
            bound="{manifest_timeout}-second socket timeout; {manifest_kib} KiB manifest limit. SPEC_CRITIC_UPDATE_URL changes the URL; SPEC_CRITIC_DISABLE_UPDATE_CHECK disables checks. Non-Windows runs are directed to releases for installation.", claims=("C06", "C12")),
    _action("A30", "Download and install an update", "Download & Install; then confirm Install update Continue?",
            "Download to a partial file, compare its {update_hash} fingerprint with the manifest, promote the file after a match. With your second confirmation, launch the installer and quit.",
            "GET the manifest's HTTPS installer URL and redirects; no project/key added. Hosts are not restricted to GitHub.",
            bound="{download_timeout}-second timeout per socket operation, no total byte cap. Failure deletes the partial file. A matching hash proves agreement with that manifest, not independent publisher identity. Busy app defers install.", claims=("C06", "C12")),
    _action("A31", "Dismiss an update offer", "Later; Skip this Version; close the update window.",
            "Close the offer or persist the skipped version. During download, suppress completion/install prompts.",
            "An already-started download can continue to its destination.",
            bound="Dismissal does not cancel transfer. A verified downloaded installer can remain in {download_path}; there is no automatic age cleanup.", claims=("C06", "C10", "C12")),
    _action("A32", "Close or stop desktop work", "Close the app; confirm Close anyway? for live review or report export.",
            "Drain the trace recorder and destroy the window. Batch work continues remotely and may be resumed next launch; there is no review Stop button.",
            "Already-started remote requests may have been sent; close is not a remote batch-cancel request.",
            bound="Live results can be lost; active export can be incomplete. Paid work is not rolled back or refunded by the app. Batch record may survive; local close has no undo.", claims=("C06", "C10", "C11")),
    _action("A33", "Read an exported report", "Filter/search; expand/collapse; contents; Copy; Print; open evidence links.",
            "Filter/render/copy/print local report data. An evidence link opens the source in your browser.",
            "Nothing for report controls; evidence links contact their displayed public destinations with normal browser metadata.",
            bound="Display filters do not change saved findings. Browser printing, clipboard and external-link behavior belong to your browser/OS.", claims=("C03", "C12", "C13")),
    _action("A34", "Configure report chat", "Ask AI open/close; Use key; Forget; model/effort selectors; New chat; Copy chat; Print chat.",
            "Hold a reader-supplied key in memory and model/effort preferences in tab session storage. Reset/copy/print local chat; a model change, Forget or New chat stops an active turn.",
            bound="These controls do not start an AI request. Key is forgotten on reload/close, not embedded in the report. Closing the chat panel merely hides it and does not stop an active request.", claims=("C05", "C10", "C13")),
    _action("A35", "Ask about the report", "Send; Enter; a starter question; Ask about selected text. Paste adds text to the question.",
            "Stream a browser API request, then automatically run requested report tools/web tools and continuations. Commit conversation only on a normally finished final reply with visible answer text; thinking-only replies are incomplete.",
            "Report text and the UTC date captured at the conversation's first request in the system context, committed chat history, your question/pasted or selected text, and tool results to {api_host} with your browser key.", "{chat_ai}",
            "Up to {chat_tools} report-tool rounds and {chat_continuations} pause continuations per turn; history trims whole turns toward {chat_history} messages, so a large single turn can exceed that target. Each request allows {chat_searches} searches and {chat_fetches} fetches. No total turn dollar cap or explicit browser request timeout/retry loop.", ("C05", "C06", "C11", "C13")),
    _action("A36", "Stop report chat", "Stop; leave the page; change model, Forget or New chat during a turn.",
            "Abort the browser stream and reject late events. Remove the unfinished turn from future conversation; partial text can remain visibly marked.",
            "No new question; previously sent content and consumed usage cannot be retracted.",
            bound="Completed filter/scroll/highlight tool effects are not undone. The app cannot certify when provider billing stops. Hiding the panel alone is not Stop.", claims=("C06", "C11", "C13")),
    _action("A37", "Inspect traces from a terminal", "python -m src.tracing list or show; --help.",
            "Read selected local run metadata and records; print a summary or detail.", bound="No AI or network. Access requires permission to read the trace folder; the output may contain project content.", claims=("C03", "C10", "C14")),
    _action("A38", "Prune traces explicitly", "python -m src.tracing prune with the chosen age/count options.",
            "Select local run directories and delete the chosen traces (the command lists them before asking for confirmation unless --yes is set).",
            bound="Deletion has no app undo and does not delete reports, provider records or verification cache. Check the command's --help before running it.", claims=("C03", "C10", "C14")),
    _action("A39", "Recover from a terminal", "Run scripts/recover_batch.py; --help for arguments; Ctrl-C to stop local polling.",
            "Use saved state or explicit batch ID/module/inputs; poll/collect and perform remaining pipeline stages; optionally export diagnostics JSON.",
            "Batch IDs and subsequent stage payloads to configured API/results destinations, as for GUI recovery.",
            "Help/polling: None. Remaining stages use the engine table.",
            "Local polling is bounded; Ctrl-C does not cancel the remote batch. Recovery costs omit earlier unsaved research usage. Follow-up calls can be billed.", ("C05", "C06", "C11")),
    _action("A40", "Use the separate edit applier", "python -m applier or spec-critic-apply; optional --assist; --help for policies and modes.",
            "Read the sidecar/specs, gate proposals, locate targets, check conflicts and write edited copies plus receipts. Default tracked changes lets you accept/reject in Word. Optional assistance searches/reads document elements and selects a location.",
            "Default: nothing. With --assist, issue/proposal/available candidate excerpts and requested document text go to the configured Anthropic API endpoint.",
            "Default: None. Optional: {assist_ai}",
            "Assist uses up to {assist_rounds} tool rounds and {assist_tokens} output tokens per call. Candidate ID/text is validated; it cannot rewrite replacement wording. --mode direct, broader policies and force-status options can weaken defaults. This is a separately invoked writer, not part of the desktop review.", ("C05", "C06", "C08", "C11", "C12")),
    _action("B01", "Automatic startup and UI housekeeping", "Launch Spec Critic; no review click.",
            "Configure rotating logs; load key/preferences; offer saved-batch resume; then on Windows check for updates when due. UI animation, queue-drain and debounce timers repaint/schedule local work.",
            "Windows update GET/redirects to {update_url} by default, at most once per {update_days} day(s) at launch. No project/key added by updater.",
            bound="SPEC_CRITIC_DISABLE_UPDATE_CHECK disables checks; URL/state overrides are supported. Resume waits for your answer. Startup does not submit a fresh AI review.", claims=("C04", "C10", "C12")),
    _action("B02", "Automatic input counting", "Loaded file/selection/context/program changes; request preflight during runs.",
            "Count locally with cached tokenizer data; if absent in a source install, download {tokenizer_url}. Count the largest selected request through Anthropic; pipeline preflights size built requests too.",
            "Tokenizer download sends no project content/key. Provider counts send the constructed spec/context/prompt/tool request. Routed GUI gauge may use a local estimate instead.",
            bound="Counting is not a generated answer (AI involved: None). Failed counts use padded local estimates where available. SDK defaults: {sdk_retries} retries; {sdk_timeout}-second I/O and {sdk_connect}-second connect timeout, not a whole-run deadline. Clearing prevents scheduled calls but does not stop active HTTP.", claims=("C03", "C04", "C06", "C12")),
    _action("B03", "Automatic location and client research", "A started run uses a profile-enabled module.",
            "Data-center programs research a shared jurisdiction core once, then run discipline supplements in parallel. Standalone modules also get the core. Search/fetch runs on the vendor's servers; cited URLs are matched to retrieved evidence. Shared items need explicit module applicability to control compliance or the governing basis. The profile is spliced into context and can be trimmed to fit.",
            "Location/client and shared jurisdiction questions to {api_host}; discipline supplements also send the previously researched core and signals extracted from their specs as untrusted data. Model-chosen queries/URLs reach vendor web tools. Pending-batch resume uses saved profiles without researching again.", "{research_ai}",
            "Default dimension search/fetch budgets: {research_budgets}. Up to {research_workers} research calls; {research_continuations} pause continuations plus a submission reminder. Paused search overrun is checked after the response, not a global billing cap. Partial failure is labeled; all-dimension failure stops review submission. web_fetch is attached even for an unsupported override.", ("C05", "C06", "C07", "C09")),
    _action("B04", "Automatic review retry and repair", "A started review suffers transient error, unusable/truncated output, or an extended-output beta rejection.",
            "Retry eligible live failures; try the bounded review repair pass. Reattach saved repair batches; preserve primary results on repair failure. A rejected extended beta is resubmitted at the ordinary cap.",
            "The relevant review request again, with repair instructions where needed, to the API.", "{review_ai}",
            "Shared live policy allows {retry_attempts} attempts and {retry_wait} seconds of retry waiting. Refusals do not retry as truncation. Repair is bounded, not an indefinite loop; batch submission uses SDK retries. Paid primary results survive failed repair; pending repair can defer downstream paid work.", ("C05", "C06", "C11")),
    _action("B05", "Automatic batch polling and collection", "A batch was submitted or resumed.",
            "Poll status, download results, parse records and reattach a recorded repair. Routed partitions may collect in parallel. A broken results stream restarts its download under the shared retry policy.",
            "Batch IDs for status and API-provided results URLs; network metadata/authentication managed by the SDK.",
            bound="Local polling: {poll_hours} hours, {poll_errors} consecutive errors; nominal intervals {poll_intervals} seconds with error backoff. Detaching does not cancel the batch. Result download allows {retry_attempts} attempts. SPEC_CRITIC_PENDING_BATCH_PATH changes recovery location.", claims=("C06", "C10", "C11")),
    _action("B06", "Automatic finding triage", "A review has eligible lower-stakes findings.",
            "Local rules route some findings to skips. Haiku classifies eligible findings as locally resolvable or needing web verification. Critical/high or code-referenced findings are excluded from Haiku eligibility.",
            "Eligible finding descriptions/references and triage instructions to {api_host}.", "{triage_ai}",
            "Classification failures, refusals and truncated responses fall back to web-required. The forced classification tool can suppress up-front thinking even when adaptive thinking is requested. Local skips avoid the web verifier, not necessarily the earlier reviewer/triage AI. Local-skip routing is always enabled in the shipped code; there is no disable switch.", ("C02", "C05", "C07")),
    _action("B07", "Automatic verification, reuse and escalation", "New findings need external checking.",
            "Try eligible cache entries/share equivalent in-flight work; otherwise route verification, search/fetch, parse verdicts and apply evidence gates. Continue paused calls or remind missing verdicts; retry eligible failures. Unresolved batch tails can run live. Eligible high-stakes insufficient evidence can escalate; grounded disagreement can be contested.",
            "Finding details, quoted spec passage where available, governing context and retrieved/tool history to {api_host}; public queries/URLs through vendor tools. Optional supplied-source reuse is named separately.", "{verifier_ai}",
            "Search caps per request: {searches}; fetch cap {fetches}, content {fetch_tokens} tokens. Default/deep pause continuations: {continuations}/{deep_continuations}; shared retries {retry_attempts} attempts. Operational failures do not trigger escalation. Cache defaults: {cache_days} days, {cache_entries} entries, single-flight wait {flight_wait} seconds; cache age is shown. These bounds do not cap all phases' combined spend.", ("C05", "C06", "C07", "C10", "C11")),
    _action("B08", "Automatic selected cross-spec check", "Cross-spec coordination was selected for the started run.",
            "Compare the module's specs, split oversized inputs by module chunk rules, parse/anchor-check findings and verify the new ones. One output recovery per pass can re-request an unparseable response or split truncated output into smaller requests; all returned usage is counted.",
            "Current chunk's spec texts and already-identified findings with the module prompt to {api_host}.", "{cross_ai}",
            "Request budgets/chunking/shared retries apply; the output recovery allowance is shared across chunks. Indivisible packages still fail. Reduced coordination scope and failed/skipped chunks are named. The default pass cannot see relationships across module or chunk boundaries.", ("C05", "C06", "C09", "C11")),
    _action("B09", "Automatic requirements compliance check", "A profile-enabled run has researched requirements.",
            "Compare specs to the profile, normalize coverage and settle addition proposals; mark missing coverage rows. One output recovery per pass can re-request an unparseable response or split truncated output into smaller requests; all returned usage is counted. Verify new findings.",
            "Profile items, relevant specs and prior findings to {api_host}.", "{compliance_ai}",
            "Request/chunk/retry bounds apply; the output recovery allowance is shared across chunks. Indivisible packages still fail. Smaller requests disclose their reduced scope; failed/skipped chunks keep coverage incomplete and absence-based additions held. A coverage label is the model's judgment; complete rows do not prove all applicable law was researched or the design complies.", ("C05", "C06", "C09", "C11")),
    _action("B10", "Automatic drawing-impact explanation", "An attached drawing-analysis block is present in the completed review context.",
            "Relate your drawing-analysis text to the final findings; validate referenced finding IDs and discard unknown IDs.",
            "The attached drawing-analysis text, final finding summaries and synthesis instructions to {api_host}; no drawing file is uploaded by this pass.", "{impact_ai}",
            "Shared request/retry bounds apply. This interprets your program's text; it does not read the drawings, the CAD/BIM model or any sheet, and its cited sheet references are copied from that text.", ("C05", "C06", "C09")),
    _action("B11", "Automatic local audit and report preparation", "Start/finish a run; read/write state and cache.",
            "Prune old/excess traces at recorder startup; record selective events/findings; deduplicate while preserving per-file edit occurrences. Prepare diagnostics and open the save-report prompt at completion. Update cache and saved state.",
            bound="Trace defaults: {trace_days} days/{trace_runs} recent runs, pruned on run start. Disable/adjust with SPEC_CRITIC_TRACE_RETENTION_DAYS and SPEC_CRITIC_TRACE_MAX_RUNS. Trace failure warns and review continues; queue growth is warned about, not hard-capped. Reports/state have no general age cleanup.", claims=("C03", "C06", "C10", "C14")),
    _action("B12", "Automatic report-chat tools and follow-ups", "A submitted chat answer asks for report tools or pauses for web work.",
            "Run named local tools to query/filter/navigate/highlight/calculate; send tool results and continue. Web search/fetch runs on Anthropic's servers. No per-tool confirmation.",
            "Report-tool results and conversation history to {api_host}; web queries/URLs through vendor tools.", "{chat_ai}",
            "{chat_tools} tool rounds and {chat_continuations} pause continuations. Unknown/malformed tools return errors; partial-turn conversation is discarded on failure. Browser report changes already made remain. Chat web tools do not carry the desktop source-quality blocklist.", ("C05", "C06", "C12", "C13")),
    _action("B13", "Explicitly enabled experiments", "Set experiment environment switches before a run.",
            "SPEC_CRITIC_PROJECT_CONTEXT_CACHE changes prompt caching; SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT changes review output shape; SPEC_CRITIC_EVIDENCE_VALIDATION observes support; SPEC_CRITIC_SOURCE_REUSE can supply earlier evidence; SPEC_CRITIC_RESEARCH_CACHE can reuse a profile; SPEC_CRITIC_CROSS_COORDINATION finds candidates or sends observation-only judgments.",
            "Default: no added experiment requests. Observe-mode coordination sends bounded passage pairs to the API; other switches alter supplied context/request shape or replace retrieval with reuse.",
            "Observation coordination uses {coordination_ai}. Candidate-only and local cache lookup: None. Other active stages keep their configured models.",
            "All these experiments are off by default. Coordination limits {coordination_candidates} candidates, {coordination_pair} per file pair, {coordination_batch} per request; observations do not change reported findings. Source reuse weakens the 'retrieved in this conversation' claim and is labeled. Research reuse is not fresh research.", ("C05", "C06", "C07", "C09", "C10")),
)


TOPICS = (
    Topic("answer", "Start here", "The short answer", (
        Block("commitments", "", (
            ("Know the origin and the gate", "You get origin/stage labels, status and evidence details. Those distinguish supplied text, local detection, model proposals and retrieved or reused sources; they do not label the ancestry of every sentence."),
            ("Know what starts without another click", "Startup state loading/update checks and input counting are automatic. Once you start a review or a chat question, its retries, tools and dependent stages proceed automatically. A remote batch can outlive your window."),
            ("Recheck the mechanical decisions", "Gates are programmed checks that accept or downgrade a result. You can inspect and reproduce source matching, routing, edit-shape checks and pricing arithmetic from recorded inputs and code. You cannot expect another model call to reproduce the same judgment."),
        ), ("C01", "C02", "C04", "C06", "C14")),
    )),
    Topic("origins", "Provenance", "Where your findings come from", (
        Block("table", "Origin | What it actually is | How you can tell", (
            ("You and your files", "Extracted specification text; context, attachments, location/client fields.", "Files Reviewed, quoted passages, context attachment labels and profile fields."),
            ("Built-in module rules", "Versioned prompts, code/edition pins, detector vocabulary and routing rules.", "Selected program/module, code-basis and methodology; inspect module source."),
            ("Local detection", "Pattern/structure checks and later mechanical gates.", "Pre-detected alerts. Locally classified is a routing status and can also follow model triage."),
            ("Model interpretation", "Review, coordination, compliance, research and drawing proposals, using model training as well as supplied context.", "Finding origin/stage, confidence and methodology; these are proposals."),
            ("Retrieved public sources", "Search/fetch results the hosted tools returned.", "Evidence panel: accepted/rejected URLs, search/fetch detail and source quote."),
            ("Reused evidence or verdicts", "A cached verdict (a locally saved eligible result), shared equivalent work, or opt-in source/profile reuse.", "Cache replay age/path and reuse diagnostics; not newly searched evidence."),
        ), ("C02", "C07", "C09", "C14")),
        Block("note", "Things that are not happening", "The shipped reviewer has no downloaded standards corpus, shared-user project database or training job. It does not run the edit applier. These are statements about client code; Anthropic's storage/training terms and your own synced folders need separate checks.", ("C02", "C08", "C12")),
    )),
    Topic("engine", "Components", "What the engine actually is", (
        Block("text", "", "By default, models run on Anthropic's servers. Extraction and report components run on your computer. A token is a unit used to measure model text; output caps include thinking as well as the answer. Effort is a requested reasoning level, not a correctness score. Adaptive thinking lets the provider allocate reasoning work. Temperature is the answer-variation setting; the app omits it. The SDK is the provider's client library that sends requests.", ("C03", "C05", "C11")),
        Block("engine", "Job | Model or component | Why", "", ("C03", "C05")),
        Block("note", "Defaults and switches", "Model overrides: SPEC_CRITIC_REVIEW_MODEL, SPEC_CRITIC_VERIFICATION_MODEL, SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL, SPEC_CRITIC_TRIAGE_MODEL, SPEC_CRITIC_RESEARCH_MODEL and SPEC_CRITIC_DRAWING_IMPACT_MODEL. Cross-check/compliance defaults have no model environment switch. SPEC_CRITIC_REVIEW_EFFORT changes review effort when explicitly set, and is not held to the Opus medium ceiling (which otherwise applies to a review model overridden to Opus). The table reflects this process's configured values; arbitrary override IDs can fail or lack a price. Temperature is omitted. Trace Deep can request summarized thinking; strict verifier omits explicit thinking, which is not a promise that thinking is disabled.", ("C05", "C10")),
    )),
    Topic("boundary", "The boundary", "What leaves your computer, and where it goes", (
        Block("diagram", "Data flow", FLOW_DESCRIPTION, ("C04", "C12", "C13")),
        Block("contrast", "Leaves your machine | Stays on your machine", (
            "Relevant specs, filenames, context/profile (any attached drawing-analysis text included), findings, evidence and tool history go to {api_host} for counting or AI; no drawing file is ever uploaded. Batch collection follows a returned results URL; the SDK follows redirects and configured base URLs/proxies.\n\nUpdates GET {update_url} and manifest-selected HTTPS installer hosts/redirects. A missing source-install tokenizer downloads {tokenizer_url}.\n\nVendor web tools reach dynamic public sources. Explicit browser links reach evidence sites and these fixed reference hosts: {browser_hosts}. Browsers apply their own cookies. The updater/tokenizer add no project text or API key.",
            "Local parsing/routing/pattern checks, evidence comparisons, pricing math, reports, sidecars, UI state, caches, recovery state and trace files. They can contain copies of content that was also sent.\n\nTyped desktop/browser keys remain in memory and are sent for API authentication. Optional key files/keyring live locally. 'Local storage' does not mean encryption, and a folder you sync/share may send these artifacts elsewhere.",
        ), ("C03", "C04", "C10", "C12", "C13")),
        Block("note", "A configurable boundary", "The desktop SDK honors ANTHROPIC_BASE_URL and ANTHROPIC_CUSTOM_HEADERS; system proxies, intermediary network services, also matter. The app passes its captured API key explicitly, so SDK credential-profile discovery does not run. There is no app list of permitted hostnames. The browser report's API destination is fixed in that exported file. Public-source hosts, results URLs, redirects and installer hosts cannot be exhaustively named before the response exists; watch a firewall log to see the actual ones.", ("C12", "C13", "C14")),
    )),
    Topic("runtime", "Runtime", "What runs for each action", (
        Block("text", "", "The cards include desktop controls, exported-report controls, shipped companion commands and automatic stages. Related controls sharing a mechanism share a card. API destinations describe the default endpoint; ANTHROPIC_BASE_URL and proxies can redirect desktop traffic. 'AI involved: None' refers to that action's own computation; counting can still send data to a hosted endpoint. Review phases and their retries are not separate approvals.", ("C03", "C04", "C05", "C06", "C12")),
        Block("runtime", "", "", ("C04", "C06")),
    )),
    Topic("tools", "Blast radius", "What the AI may touch", (
        Block("table", "Tool or capability | What it can do | Constraint", (
            ("Review/cross/compliance submit tools", "Return proposed structured findings and coverage.", "Required field structures, called schemas, plus local shape/anchor checks; no desktop write tool. SPEC_CRITIC_STRICT_TOOL_USE can disable strict schemas; text fallback remains reachable."),
            ("Verifier/research web_search and web_fetch", "Search/fetch public information on the vendor's servers.", "Per-request budgets and desktop source-quality blocklist; fetch gated by model for verification, not research. Internal vendor code execution can filter results; no desktop shell is exposed."),
            ("Verdict/profile/triage/impact submit tools", "Return judgments, source references, classifications or impact statements.", "Named payload parsing and mechanical gates. A valid schema does not establish truth."),
            ("Report get_findings", "Read structured findings and return a bounded subset.", "Reads the embedded report; does not read your source DOCX files."),
            ("Report filter_report / clear_filters", "Change visible filters without another click.", "Known filter choices/local report data; does not change saved findings."),
            ("Report navigate_to_section", "Scroll to an existing page element.", "Existing element IDs; no browser navigation to a new URL."),
            ("Report highlight_terms / clear_highlights", "Mark words in the report display.", "Escaped literal search terms and bounded marks; effects remain after a stopped turn."),
            ("Report calculate", "Evaluate basic arithmetic over supplied numbers.", "A token parser and operator stack; no JavaScript eval or general code execution."),
            ("Separate applier assistance", "Search/read candidate elements; choose_element or decline.", "Candidate IDs/text validated; replacement wording comes from sidecar. Default off; no model-authored replacement slot."),
        ), ("C05", "C08", "C12", "C13")),
        Block("note", "The categorical limit", "The desktop model tool lists expose no local shell, arbitrary filesystem write or installer launch. Models cannot approve a design or apply reviewer edits inside the GUI. The Python application itself has your user account's file/network privileges; it is not a sandbox, an isolated execution area. Hosted tools, report display tools and explicitly invoked applier assistance have the exceptions described above.", ("C08", "C12", "C13")),
    )),
    Topic("terms", "Exact words", "What the labels mean, exactly", (
        Block("note", "Grounded — the exact claim", "A cited URL matched an accepted URL retrieved by a tool, or eligible supplied evidence under the opt-in reuse path. This does NOT prove the page is authoritative or that its words support the model's judgment.", ("C07",)),
        Block("note", "Verified — the exact claim", "A supported confirmed/corrected verdict passed the implemented grounding/citation gates, including the required source-quote field. URL comparison normalizes spelling differences such as tracking parameters. This does NOT prove the quoted words appear on that page, the source is primary/current/adopted, or the conclusion is correct. Default validation does not re-derive the engineering judgment.", ("C07",)),
        Block("note", "Locally classified — the exact claim", "The routing/triage path treated a finding as locally resolvable rather than requiring external verification. This does NOT mean no AI participated, that a regulation was checked, or that the issue is safe to ignore.", ("C02", "C07")),
        Block("note", "Edit suggested — the exact claim", "A structured proposal survived the applicable shape and available-text checks. Anchor matching accepts whitespace differences; unavailable or unattributable source text can remain unchecked. This does NOT mean the proposed edit is approved, uniquely located, suitable for automatic application or complete.", ("C08",)),
        Block("note", "Completed and covered — the exact claim", "The reported stage returned its expected form or the coverage record names a requirement's status. Diagnostics expose failed/skipped/incomplete work. This does NOT mean every possible conflict or applicable requirement was reviewed; cross-check partitions and research gaps still matter.", ("C06", "C09", "C14")),
        Block("note", "Secure — the exact claim", "This dossier names specific mechanisms: credential lifetime, selective redaction, prompt boundaries, escaped report rendering and updater integrity checks. It does NOT claim security certification, end-to-end local privacy, encrypted artifacts, resistance to malware, or guaranteed model obedience.", ("C10", "C12")),
    )),
    Topic("local", "No AI here", "The parts with no AI in them", (
        Block("bullets", "", (
            "DOCX/context extraction, path deduplication and duplicate-name refusal.",
            "Program routing from module signals; local pattern/structure alerts and code-pin formatting.",
            "Local token estimates, request budgets, edit-shape/available-text anchor checks and occurrence grouping.",
            "URL normalization/acceptance, cache keys/eligibility/age and deterministic status labels.",
            "Cost arithmetic, diagnostics rendering, Word/JSON/HTML export and report filters/copy/print.",
            "Trace recording/pruning/viewing, preference/recovery file handling, version comparison and updater fingerprint checks.",
            "This help, its contents navigation and its vector diagram.",
        ), ("C03", "C06", "C07", "C08", "C10", "C12", "C14", "C15")),
        Block("note", "With the network disconnected", "You can read saved reports/traces, use their local controls, read this help, edit context, and extract/count locally if tokenizer data is already present. Exporting retained results and the applier's default local work still work. AI review/chat/research/verification, remote batch recovery and update downloads need a connection. A source install missing tokenizer data cannot complete local counting until that data is available.", ("C03", "C12", "C13", "C14")),
    )),
    Topic("security", "Mechanisms", "Security and privacy in specific terms", (
        Block("table", "Concern | How it is handled", (
            ("Desktop credentials", "Startup prefers keyring service {key_service}, account {key_account}; then {key_filename} in the user config directory or executable/source fallback; then ANTHROPIC_API_KEY. Typed keys stay in captured in-memory credentials. Plaintext fallback is not encrypted; POSIX permissions are tightened best-effort. No GUI save-key command."),
            ("Browser credentials", "Ask AI key is memory-only, deleted on reload/close; old session-storage key is removed. Model/effort preferences, not the current key, use session storage. Browser extensions/compromise can still read page memory."),
            ("Untrusted documents and pages", "Requests place project text inside marked data boundaries and escape boundary characters; model instructions ask it to ignore embedded directives. HTML output escapes data and restricts scripts with a Content Security Policy, browser rules governing loads. These reduce injection risk, not model hallucination or malicious source text."),
            ("Traces and logs", "Trace/diagnostic redaction recognizes secret-shaped fields and key/bearer patterns; it does not remove project text. Ordinary rotating log has no universal redaction layer. Default trace is selective; Deep can store full prompts and response details. Protect exported/copied artifacts."),
            ("Local state", "{ui_path}: preferences/profile; {pending_path}: request mappings, paths and effective context/profile, not saved spec bodies; {cache_path}: verdict cache. Readable JSON, no application encryption; recovery re-extracts available source files."),
            ("Retention", "Verification defaults {cache_days} days/{cache_entries} entries, via SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS / SPEC_CRITIC_VERIFICATION_CACHE_MAX_ENTRIES; zero disables that bound. Traces at {trace_path}, {trace_files}; {trace_days} days/{trace_runs} runs, pruned at run start. SPEC_CRITIC_TRACE_RETENTION_DAYS / SPEC_CRITIC_TRACE_MAX_RUNS adjust or disable those bounds; SPEC_CRITIC_TRACE_DIR changes the folder. Trace/Deep controls or SPEC_CRITIC_TRACE / SPEC_CRITIC_TRACE_DEEP change capture. Other artifacts remain until overwritten/deleted."),
            ("Other disk artifacts", "{log_path}: rotating text log, {log_mib} MiB and {log_backups} backups by default; SPEC_CRITIC_LOG_PATH changes it. {update_state}: JSON update state; {download_path}: installers. Opt-in research reuse stores {research_cache_path}. Tokenizer cache follows TIKTOKEN_CACHE_DIR, then DATA_GYM_CACHE_DIR, otherwise OS temporary cache. Reports/sidecars use your selected location."),
            ("Network and local server", "No listening server, analytics client or crash-upload client is implemented in shipped paths. API SDK/proxy/base-URL configuration and explicit browser links change network behavior. Your desktop process is not isolated from your user account."),
            ("Update integrity", "Initial/final HTTPS and a manifest-provided {update_hash} fingerprint are checked. Redirects can be followed before final-scheme rejection; there is no hostname allowlist, independent signature check in this downloader, or total installer byte cap. Closing an offer does not stop transfer."),
            ("Provider terms", "Client code cannot prove provider retention, training exclusion or access policy. Check Anthropic's current API terms and your organization's agreement before submitting sensitive work."),
        ), ("C04", "C06", "C10", "C12", "C13", "C14")),
    )),
    Topic("money", "Money", "What you pay for, and what the meter knows", (
        Block("text", "", "Anthropic bills API work to the account behind your key. You pay for consumed input/output/thinking tokens, cache activity and web-search requests. A tokenizer/count estimate is not a measured invoice. The app's rate table can drift from provider prices, tiers or contract terms; the provider's console is the billing record.", ("C05", "C11")),
        Block("prices", "Configured model / prompt tier | Input / output per million tokens (USD) | Cached read per million tokens (USD)", "", ("C11",)),
        Block("text", "", "For a tiered model, total prompt size selects the rate for the whole request, including output and cache tokens. The prompt count includes uncached input and cache writes/reads; cache write details partition their aggregate rather than adding to it. Tokens above the threshold are not priced separately.", ("C11",)),
        Block("text", "", "The estimator applies a {batch_discount}% token discount in batch mode, not to searches. Cache write/read multipliers: {cache_multipliers}; model-specific read prices override the read multiplier. Searches are estimated at {search_price}. Fetched content contributes tokens. Provider usage counts, when available, are observations; dollar totals remain estimates. Unknown/unpriced attempts are named, not made exact by a total.", ("C11",)),
        Block("note", "Failed and stopped work", "Retries, repairs, continuations, research and small-tail live fallback can add cost without another confirmation. Consumed work can still be billed after failure, Stop or close. Recovered-run estimates do not include earlier unsaved research usage. HTML chat has no invoice meter. There is no whole-run app spend cap or app refund mechanism.", ("C04", "C06", "C11", "C13")),
    )),
    Topic("limits", "Honesty", "What this does not do", (
        Block("bullets", "", (
            "It does not approve work, certify compliance or replace you, a licensed reviewer, the client or the authority having jurisdiction (the body that adopts/enforces the local requirements).",
            "It can miss Word/PDF content, symbols, revision meaning, scope or routing signals, and it does not read drawings at all. Check extracted passages and input coverage against the originals, and your attached drawing analysis against your sheets.",
            "It can confidently apply the wrong code basis or misread a real page. Code pins, URL gates and source quotes reveal assumptions; they do not establish current adoption or semantic truth. Check primary adopted text and amendments.",
            "It cannot search private/unpublished authority requirements or guarantee access to paywalled standards. Partial research, refused calls and exhausted searches are not evidence of no requirement.",
            "It does not compare every cross-discipline or cross-chunk relationship by default. Unsupported {unsupported_divisions} are visible coverage gaps; independent modules are not a comprehensive coordination review.",
            "It cannot prove a suggested edit is uniquely anchored or professionally correct. Shape checks and available-text matching reduce bad instructions; read the whole clause before applying.",
            "It does not protect a source file from a report export aimed at that same path. Use a separate output filename and preserve originals; existing same-stem sidecars are replaced without their own confirmation.",
            "It cannot reconstruct every request or every dollar from selective traces and recovered state. Trace capture may fail; retention may remove records. Preserve what you need before cleanup.",
            "It cannot confine project data to your machine, encrypt your artifacts or guarantee an external service/browser is private. Read the boundary before loading sensitive inputs.",
        ), ("C01", "C06", "C07", "C08", "C09", "C10", "C12", "C14")),
    )),
    Topic("audit", "The point", "Check it yourself", (
        Block("steps", "", (
            "Read the report's Run Diagnostics banner first. You will see failed specs, skipped work and integrity/coverage warnings; investigate these before treating zero findings as reassurance.",
            "Open a consequential finding's evidence panel. You will see model/status, accepted/rejected URLs, source quote and cache age. Follow the link yourself and compare the actual clause, edition, adoption and amendments.",
            "Check the reviewed file list, selected program and researched location/client profile. Compare your attached drawing analysis to your sheets; unreviewed/misrouted files and omitted requirements remain your responsibility.",
            "Open the exported .edits.json and applicable .profile.json in a text editor. You will see proposals/occurrences and the researched profile, not an approval. Compare the existing/replacement text with your original; the reviewer has not applied it.",
            "Open Run Diagnostics and Copy to Clipboard, or inspect the trace folder/viewer. You will see recorded attempts, tool/evidence events and usage. Default capture is selective; Deep adds sensitive detail on future runs, not retroactively.",
            "Compare the estimate and unknown/unpriced attempts against your Anthropic console. You will see billed usage that may include other apps and earlier failed/stopped work; recovery figures omit some earlier stages.",
            "Disconnect the network and reopen this help, saved report and local trace viewer. Their local reading controls still work; AI and recovery do not. A missing local tokenizer cache can block fresh extraction/count preparation.",
            "Use an approved firewall/network monitor while selecting files, changing context, attaching a drawing analysis, launching on Windows and starting a review/chat. You will see the pre-review count, update/download and follow-up paths described here, including actual redirect hosts.",
            "Inspect the source register in docs/TRUST_CLAIMS.md and run the repository's tests locally. You will see which constants generate this copy and which mechanical contracts are checked. Compare the source version to the app version; local tests cannot prove hosted-provider behavior.",
        ), ("C03", "C04", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14")),
        Block("links", "Further reading", "", ("C12", "C14")),
        Block("note", "Still not convinced?", "Good. Use Spec Critic as a second reader whose claims you challenge. Start with material you can independently check, keep failed and uncertain items visible, and verify consequential findings against the project record and adopted primary sources. If you cannot establish a finding's basis, leave the decision open and ask the responsible professional.", ("C01", "C07", "C09")),
    )),
)


def fact_values() -> dict[str, str]:
    """Read shipped constants/settings; no network or credential access."""
    from urllib.parse import urlsplit
    from anthropic._constants import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
    from ..core import api_config as cfg, pricing, tokenizer, updates, ui_state, logging_setup, app_paths, api_key_store
    from ..verification import retry_policy as retry, verification_cache as cache
    from ..verification.verification_modes import mode_policy
    from ..batch import batch_runtime as poll
    from ..input import drawing_analysis, extractor
    from ..tracing import config as trace, recorder
    from ..research import requirements_research as research, research_cache
    from ..research.shared_jurisdiction import JURISDICTION_DIMENSIONS
    from ..orchestration import batch_resume
    from ..output import html_report_exporter as html
    from ..coordination import candidates, adjudication
    from ..modules.registry import AVAILABLE_MODULES

    def n(value):
        return f"{value:,}" if isinstance(value, int) else f"{value:g}"

    def line(model, phase, *, effort=None, thinking=None):
        config = cfg.effort_config_for(model=model, phase=phase, effort_override=effort)
        level = config["effort"] if config else "provider default (omitted)"
        think = thinking if thinking is not None else ("adaptive thinking" if cfg.thinking_config_for(model=model, phase=phase) else "thinking omitted")
        return f"{model}; effort {level}; {think}; output cap {n(cfg.phase_output_cap(phase, model=model))} tokens; temperature omitted."

    def js_int(name):
        match = re.search(r"var " + re.escape(name) + r" = (\d+);", html._CHAT_JS)
        if not match:
            raise ValueError(f"Trust binding missing: {name}")
        return match.group(1)

    # Fixed report endpoint; SDK defaults and overrides are independently pinned.
    api_host = urlsplit(html._build_chat_config({})["api_url"]).hostname
    roles = {
        "review": (cfg.REVIEW_MODEL_DEFAULT, cfg.PHASE_REVIEW),
        "research": (cfg.RESEARCH_MODEL_DEFAULT, cfg.PHASE_RESEARCH),
        "cross": (cfg.CROSS_CHECK_MODEL_DEFAULT, cfg.PHASE_CROSS_CHECK),
        "compliance": (cfg.COMPLIANCE_MODEL_DEFAULT, cfg.PHASE_COMPLIANCE),
        "impact": (cfg.DRAWING_IMPACT_MODEL_DEFAULT, cfg.PHASE_DRAWING_IMPACT),
    }
    facts = {f"{job}_ai": line(*pair, effort=cfg.review_effort_override() if job == "review" else None) for job, pair in roles.items()}
    facts["coordination_ai"] = line(cfg.COORDINATION_MODEL_DEFAULT, cfg.PHASE_COORDINATION)
    strict = mode_policy("strict_structured")
    standard = mode_policy("standard_reasoning")
    deep = mode_policy("deep_reasoning")
    facts["verifier_ai"] = (
        "Strict: " + line(strict.model, cfg.PHASE_VERIFICATION, effort=strict.effort, thinking="thinking omitted")
        + " Standard: " + line(standard.model, cfg.PHASE_VERIFICATION)
        + " Deep/escalated: " + line(deep.model, cfg.PHASE_VERIFICATION)
        + " Local-skip/cache lookup: None."
    )
    facts["triage_ai"] = line(cfg.TRIAGE_MODEL_DEFAULT, cfg.PHASE_TRIAGE)
    facts["chat_ai"] = f"{html.CHAT_DEFAULT_MODEL} by default (choices: {', '.join(mid for mid, _ in html.CHAT_ALT_MODELS)}); effort {html.CHAT_DEFAULT_EFFORT} by default, selectable {', '.join(html.CHAT_EFFORT_LEVELS)}; adaptive summarized thinking; {n(html.CHAT_MAX_TOKENS)} output tokens; temperature omitted."
    facts["assist_ai"] = f"{cfg.MODEL_SONNET_55} by default (--assist-model can change it); effort {ASSIST_EFFORT_PIN} where the model accepts it; thinking and temperature omitted (adaptive thinking is the model default); {n(ASSIST_TOKENS_PIN)} output tokens."
    facts.update({
        "models": ", ".join(sorted({pair[0] for pair in roles.values()} | {cfg.TRIAGE_MODEL_DEFAULT, standard.model, deep.model})),
        "review_extended": n(cfg.REVIEW_OUTPUT_CAP_BATCH_EXTENDED), "review_threshold": n(cfg.LARGE_REVIEW_INPUT_THRESHOLD), "review_beta": cfg.BATCH_OUTPUT_BETA,
        "workers": " / ".join(map(str, cfg.REALTIME_REVIEW_WORKER_CHOICES)),
        "research_workers": n(cfg.RESEARCH_MAX_WORKERS_DEFAULT), "prepare_workers": n(cfg.PROGRAM_PREPARE_MAX_WORKERS_DEFAULT), "collection_workers": n(cfg.PROGRAM_COLLECTION_MAX_WORKERS_DEFAULT), "call_permits": n(cfg.REALTIME_COLLECTION_MAX_CALLS_DEFAULT),
        "context_cap": n(tokenizer.PROJECT_CONTEXT_MAX_TOKENS),
        "retry_attempts": n(retry.DEFAULT_REALTIME_RETRY_POLICY.max_attempts), "retry_wait": n(retry.DEFAULT_REALTIME_RETRY_POLICY.max_retry_wait_seconds),
        "continuations": n(retry.DEFAULT_MAX_CONTINUATIONS), "deep_continuations": n(retry.DEEP_MAX_CONTINUATIONS),
        "poll_hours": n(poll.DEFAULT_REVIEW_POLL_POLICY.max_elapsed_seconds / 3600), "poll_errors": n(poll.DEFAULT_REVIEW_POLL_POLICY.max_consecutive_errors), "poll_intervals": f"{poll.DEFAULT_POLL_INTERVAL_SECONDS} to {poll.DEFAULT_POLL_MAX_INTERVAL_SECONDS}",
        "sdk_retries": n(DEFAULT_MAX_RETRIES), "sdk_timeout": n(DEFAULT_TIMEOUT.read), "sdk_connect": n(DEFAULT_TIMEOUT.connect), "api_host": api_host,
        "searches": ", ".join(f"{severity} {cfg.web_search_max_uses_for_severity(severity)}" for severity in ("CRITICAL", "HIGH", "MEDIUM", "GRIPES")),
        "fetches": n(cfg.DEFAULT_VERIFICATION_MAX_FETCHES), "fetch_tokens": n(cfg.WEB_FETCH_MAX_CONTENT_TOKENS), "research_continuations": n(research.RESEARCH_MAX_CONTINUATIONS),
        "research_budgets": "Shared jurisdiction core: " + ", ".join(
            f"{d.dimension_id} {d.max_searches}/{d.max_fetches}" for d in JURISDICTION_DIMENSIONS
        ) + "; " + "; ".join(f"{module.display_name}: " + ", ".join(f"{d.dimension_id} {d.max_searches or cfg.RESEARCH_DEFAULT_MAX_SEARCHES}/{d.max_fetches or cfg.RESEARCH_DEFAULT_MAX_FETCHES}" for d in module.research_dimensions) for module in AVAILABLE_MODULES.values() if module.research_dimensions),
        "analysis_formats": ", ".join(sorted(drawing_analysis.DRAWING_ANALYSIS_EXTENSIONS)), "analysis_mib": n(drawing_analysis.MAX_DRAWING_ANALYSIS_BYTES // (1024 * 1024)),
        "cache_days": n(cache._DEFAULT_CACHE_TTL_DAYS), "cache_entries": n(cache._DEFAULT_CACHE_MAX_ENTRIES), "flight_wait": n(cache._DEFAULT_SINGLEFLIGHT_WAIT_SECONDS), "cache_path": str(cache.default_cache_path()),
        "trace_days": n(trace.DEFAULT_TRACE_RETENTION_DAYS), "trace_runs": n(trace.DEFAULT_TRACE_MAX_RUNS), "trace_path": str(trace.default_trace_root()), "trace_files": ", ".join((recorder.FILE_RUN_META, recorder.FILE_SPANS, recorder.FILE_EVENTS, recorder.FILE_PROMPTS, recorder.FILE_FINDINGS)),
        "log_path": str(logging_setup.default_log_path()), "log_mib": n(logging_setup._DEFAULT_MAX_BYTES // (1024 * 1024)), "log_backups": n(logging_setup._DEFAULT_BACKUPS),
        "pending_path": str(batch_resume.pending_batch_path()), "ui_path": str(ui_state.ui_state_path()), "update_state": str(updates.default_state_path()), "download_path": str(updates.default_download_dir()), "research_cache_path": str(research_cache.default_research_cache_path()),
        "key_filename": app_paths.API_KEY_FILENAME, "key_service": api_key_store._KEYRING_SERVICE, "key_account": api_key_store._KEYRING_USERNAME,
        "update_url": updates._DEFAULT_MANIFEST_URL, "tokenizer_url": tokenizer.CL100K_BASE_BLOB_URL,
        "update_hash": "SHA-256",
        "browser_hosts": ", ".join(sorted({urlsplit(url).hostname for _, url in FURTHER_READING} | set(ABOUT_REFERENCE_HOSTS_PIN))),
        "update_days": n(updates.DEFAULT_MIN_INTERVAL_DAYS), "manifest_timeout": n(updates.DEFAULT_MANIFEST_TIMEOUT), "download_timeout": n(updates.DEFAULT_DOWNLOAD_TIMEOUT), "manifest_kib": n(updates.MAX_MANIFEST_BYTES // 1024),
        "chat_tokens": n(html.CHAT_MAX_TOKENS), "chat_model_options": ", ".join(mid for mid, _ in html.CHAT_ALT_MODELS), "chat_efforts": ", ".join(html.CHAT_EFFORT_LEVELS), "chat_tools": js_int("MAX_TOOL_ROUNDS"), "chat_continuations": js_int("MAX_CONTINUATIONS"), "chat_history": js_int("MAX_HISTORY_MESSAGES"),
        "chat_searches": re.search(r"WEB_SEARCH_TOOL = .*?max_uses: (\d+)", html._CHAT_JS).group(1), "chat_fetches": re.search(r"WEB_FETCH_TOOL = .*?max_uses: (\d+)", html._CHAT_JS).group(1), "chat_fetch_tokens": re.search(r"WEB_FETCH_TOOL = .*?max_content_tokens: (\d+)", html._CHAT_JS).group(1),
        "assist_rounds": n(ASSIST_ROUNDS_PIN), "assist_tokens": n(ASSIST_TOKENS_PIN),
        "batch_discount": n((1 - pricing.BATCH_DISCOUNT) * 100), "cache_multipliers": f"short write {pricing.CACHE_WRITE_5M_MULTIPLIER:g}×, long/unknown write {pricing.CACHE_WRITE_1H_MULTIPLIER:g}×, usual read {pricing.CACHE_READ_MULTIPLIER:g}×", "search_price": f"${pricing.WEB_SEARCH_USD_PER_1000:g} per {n(1000)} searches",
        "coordination_candidates": n(candidates.MAX_CANDIDATES), "coordination_pair": n(candidates.MAX_PER_FILE_PAIR), "coordination_batch": n(adjudication.CANDIDATES_PER_REQUEST),
        "context_formats": ", ".join(sorted(extractor.CONTEXT_ATTACHMENT_EXTENSIONS)), "unsupported_divisions": "Division 27 and non-fire-alarm Division 28 work",
    })
    return facts


def engine_rows(facts):
    return (
        ("Per-spec review", facts["review_ai"], "Propose issues from the supplied spec and module basis; hosted."),
        ("Requirements research", facts["research_ai"], "Retrieve and summarize profile requirements; hosted."),
        ("Verification", facts["verifier_ai"], "Test proposed claims against retrieved evidence; hosted."),
        ("Eligible finding triage", facts["triage_ai"], "Choose local resolution or web-required; hosted."),
        ("Cross-spec coordination", facts["cross_ai"], "Compare module/chunk text; hosted."),
        ("Compliance", facts["compliance_ai"], "Compare researched requirements to specs; hosted."),
        ("Drawing impact", facts["impact_ai"], "Relate your attached drawing-analysis text to findings; hosted."),
        ("Optional coordination observation", facts["coordination_ai"], "Judge bounded passage pairs; default off; hosted."),
        ("Exported report chat", facts["chat_ai"], "Answer and use report/web tools after you submit; hosted."),
        ("Separate optional applier assistance", facts["assist_ai"], "Choose an element location, not edit wording; hosted."),
        ("Local preparation/reporting", "Python, DOCX/PDF parsers, tokenizer and module rules; no model.", "Extract, route, check shapes/URLs, estimate costs and render/export locally."),
    )



def price_rows(facts):
    from ..core.pricing import price_for
    rows = []
    for model in facts["models"].split(", "):
        price = price_for(model)
        if price is None:
            rows.append((model, "Unknown model; no price estimate", "Unknown"))
            continue
        threshold = price.long_context_threshold
        label = model if threshold is None else f"{model} (prompt ≤ {threshold:,} tokens)"
        rows.append((label, f"${price.input_per_mtok:g} / ${price.output_per_mtok:g}", f"${price.cache_read_rate_per_mtok:g}"))
        if threshold is not None:
            long_price = price_for(model, prompt_tokens=threshold + 1)
            rows.append((f"{model} (prompt > {threshold:,} tokens)", f"${long_price.input_per_mtok:g} / ${long_price.output_per_mtok:g}", f"${long_price.cache_read_rate_per_mtok:g}"))
    return tuple(rows)


def markdown_dossier() -> str:
    """Deterministic offline counterpart; caller chooses whether/where to save."""
    facts = fact_values()
    # Saved docs show default home paths portably, while the UI shows real paths.
    from pathlib import Path
    facts = {key: value.replace(str(Path.home()), "~") for key, value in facts.items()}
    for key in ("cache_path", "log_path", "pending_path", "ui_path", "update_state", "download_path", "research_cache_path"):
        facts[key] = facts[key].replace("\\", "/")
    # A reviewed snapshot must mean the same thing on Windows/macOS/Linux.
    # The native dialog uses the exact configured path; the saved copy names
    # platform defaults, pinned to platformdirs and default_trace_root in tests.
    facts["trace_path"] = TRACE_LOCATIONS_PIN
    lines = ["# Why trust Spec Critic?", "", "<!-- Generated from src/gui/trust_content.py; update the source and regenerate. -->", "", LEAD, ""]
    for point in SHORT_POINTS:
        lines.extend((f"**{point.title}**", "", str(point.body).format_map(facts), ""))
    lines.extend(("**Not convinced?**", "", CLOSING, "", f"[{DETAIL_BUTTON}](#the-short-answer)", "", "# The detailed view", ""))
    for topic in TOPICS:
        lines.extend((f'<a id="{topic.anchor}"></a>', f"## {topic.title}", "", f"*{topic.kicker}*", ""))
        for block in topic.blocks:
            body = block.body
            if block.kind in ("text", "note"):
                if block.title:
                    lines.extend((f"**{block.title}**", ""))
                lines.extend((str(body).format_map(facts), ""))
            elif block.kind in ("table", "engine", "prices"):
                rows = engine_rows(facts) if block.kind == "engine" else price_rows(facts) if block.kind == "prices" else body
                headers = block.title.split(" | ")
                lines.extend(("| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"))
                lines.extend("| " + " | ".join(str(cell).format_map(facts).replace("\n", "<br>").replace("|", "\\|") for cell in row) + " |" for row in rows)
                lines.append("")
            elif block.kind in ("bullets", "steps", "commitments"):
                for index, item in enumerate(body, 1):
                    prefix = f"{index}. " if block.kind == "steps" else "- "
                    value = f"**{item[0]}:** {item[1]}" if block.kind == "commitments" else item
                    lines.extend((prefix + value.format_map(facts), ""))
            elif block.kind == "contrast":
                for label, text in zip(block.title.split(" | "), body):
                    lines.extend((f"**{label}**", "", text.format_map(facts), ""))
            elif block.kind == "diagram":
                lines.extend((FLOW_SVG, "", FLOW_DESCRIPTION, ""))
            elif block.kind == "runtime":
                for index, action in enumerate(ACTIONS, 1):
                    lines.extend((f"### {index}. {action.title}", ""))
                    lines.extend(f"- **{label}:** {value}" for label, value in action.rows(facts))
                    lines.append("")
            elif block.kind == "links":
                lines.extend(("**Further reading**", ""))
                lines.extend(f"- [{label}]({url})" for label, url in FURTHER_READING)
                lines.append("")
    return "\n".join(lines)
