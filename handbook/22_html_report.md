# The HTML Report & Ask AI

[**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md)
describes the Word report: the authoritative artifact, generated automatically at
run completion, with its status labels, evidence panels, and edit proposals. That
report is not going anywhere, and nothing in this chapter changes it.

What this chapter adds is a second rendering of the same result — **one
self-contained HTML file** — and a chat interface embedded inside it that is
grounded in the report's own content. It is the only part of Spec Critic that a
person can hand to someone else as a file and have it *do* something.

That property is exactly why this chapter spends most of its length on security
and parity rather than on features. A portable HTML file with an embedded chat is
a document that will be emailed, opened on machines the author does not control,
and read by people who did not run the review. Every design decision below falls
out of taking that seriously.

## 1. What it is, and what it is not

`output/html_report_exporter.py` renders a completed `PipelineResult` or
`ProgramPipelineResult` as a single HTML file — `render_html_report` and
`write_html_report(..., include_chat=True)`.

Its constraints are worth stating as a list, because each one is load-bearing:

- **Inline CSS and JS; zero external assets.**
- **No API call during construction.** Building the file is pure rendering.
- **Never mutates the result.**
- **Never imports orchestration.** The dependency arrow points one way.
- **The pipeline does not know it exists.**

That last point is not modesty; it is the reason the feature could be added
without touching the review lifecycle. Nothing in the run calls the HTML
exporter. It is a post-run, output-only consumer of a finished object.

## 2. Parity is by construction, not by discipline

The obvious failure mode for a second report format is drift: the DOCX says 14
findings, the HTML says 13, and nobody notices for six months.

The exporter forecloses this by **importing the DOCX exporter's own pure
helpers** rather than reimplementing them:

- `_summarize_run_diagnostics` and `_summarize_verification_outcomes`
- `classify_status` and `classify_edit_action`
- `_render_pinned_editions_note`
- The severity, status, and verdict color and label maps

and by mirroring the DOCX section walk for both report types. Counts and labels
therefore *cannot* diverge, because there is only one implementation of each.

The program (routed multi-module) report follows the same rule one level up.
Its title is the program's display name inside the shared "Spec Critic — …
Specification Review Report" framing (`_program_report_title`), and it opens
with **one program-level Run Diagnostics banner** — aggregated across the child
modules by `_aggregate_run_diagnostics` (counts sum; failed-review spec names
are unioned and prefixed with their module's display name; the oldest cache age
and the worst cross-check / compliance status win) — followed by one
program-level Trust Model Summary after the program severity summary. Both
exporters consume the single `_program_run_diagnostics(program_result)`
computation, so a program run where one module's review of a spec failed
renders the same red "Specs that failed review" row and the same "absence of
findings does NOT mean … compliant" hint in Word and HTML alike; the per-module
sections render no banner of their own.

This is the same anti-drift argument [**Ch 21 — The Real-Time Review
Transport**](21_realtime_transport.md) makes about shared request builders, and
it generalizes: when two paths must agree, sharing the function is a guarantee
and matching the behavior is a hope.

### The deliberate deviations

Four differences are intentional, and all four are documented in the module
docstring so that a future reader can tell a decision from a bug:

| Deviation | Why |
|---|---|
| Alerts render **in full** (the DOCX truncates at 5 per file) | A browser has scroll; a printed page has a budget |
| The cache force-refresh hint names `SPEC_CRITIC_CACHE_PATH` and its default, not the resolved absolute path | **A portable file must not leak local paths** |
| The collapse tip is browser-appropriate | The DOCX tip describes Word's outline controls |
| Source URLs render as https-only links | A clickable link in a shared document is an attack surface worth narrowing |

The second one is the one to notice. The DOCX report is written for the person
who ran the review, so naming `/Users/abe/.spec_critic/verification_cache.json` is
helpful. The HTML file may be forwarded to a client. Leaking the reviewer's
username and directory layout into a deliverable is a small but real disclosure,
and the fix — name the environment variable and its default instead — loses
nothing.

## 3. Security

Every report-derived string is HTML-escaped. That alone is not sufficient,
because the file also embeds structured data blocks for the chat to read.

**The embedded JSON payload and config `\u`-escape `&`, `<`, and `>`.** Finding
text is model output derived from specification content, which means it is
attacker-influenceable in the threat model where a specification is untrusted
input. Escaping those three characters inside the JSON blocks makes hostile
finding text inert where it sits, rather than relying on the surrounding parser
to be well-behaved.

**There is exactly one executable inline script**, and its CSP `sha256` is
computed over the exact bytes written:

```python
digest = hashlib.sha256(script_text.encode("utf-8")).digest()
```

`write_html_report` writes **binary**. That detail is the whole point: if the
file were written in text mode, platform newline translation on Windows would
rewrite `\n` to `\r\n` after the hash was computed, and the CSP hash would no
longer match the bytes on disk. The page would then refuse to run its own script
— a failure that appears only on one platform, only in the shipped artifact, and
never in a developer's test run.

The policy is `default-src 'none'`, plus exactly one origin —
`connect-src https://api.anthropic.com` — and only when chat is included. With
`include_chat=False` the file has no API reference at all.

## 4. Ask AI: grounded in the report, and only the report

The embedded chat (default on) answers questions about the report in the reader's
browser. Its grounding is deliberately narrow:

- A **plaintext digest** of the report rides as a prompt-cached system block.
- The **structured payload** backs a `get_findings` tool.

Everything else it can do is either a browser-local action or an explicit web
lookup:

| Tool | Kind |
|---|---|
| `get_findings` | Report data |
| `filter_report`, `clear_filters` | Report-local |
| `navigate_to_section` | Report-local |
| `highlight_terms`, `clear_highlights` | Report-local |
| `calculate` | Report-local |
| `web_search_20260209` | External — attached on every offered model |
| `web_fetch_20260209` | External — attached **only on models that support it** (Sonnet 5 yes, Opus 5 no) |

Web fetch is not uniform across current models, and the API rejects a request
that attaches the tool to a model lacking it — so an unconditional tool list
would fail on the first message under the default model. The exporter therefore
embeds a per-model `model_web_fetch` map in the chat config, derived at render
time from `api_config.model_capabilities(...)` — the same capability whitelist
the verifier consults, never a second hand-kept list — and the script builds the
server-tool list **per request from the selected model**: `web_search` always,
`web_fetch` only when that model's flag is true. Switching models in the
selector therefore also switches whether fetch rides along; the system prompt
and the key-view copy are worded so they never promise fetching a model cannot
do.

The report-local tools are the interesting design choice. Rather than having the
model *describe* where to look ("scroll to the Division 23 findings"), it can
drive the document — filter it, navigate it, highlight terms. The chat is an
interface to the report, not merely a conversation about it.

Answers stream over SSE. Thinking is summarized and adaptive. Loops are bounded:
**8 tool rounds** (`MAX_TOOL_ROUNDS`) and **5 `pause_turn` continuations**, each
with a visible notice when the bound is hit rather than a silent stop. The model
selector offers an Opus 5 default and a Sonnet 5 option (only the latter carries
`web_fetch`, per the table above).

### A question joins the conversation only when it is answered

The chat talks to the Messages API directly and keeps the conversation itself, so
the history it sends back is its own responsibility. Until plan chunk S04 (WP-12),
that history was built as the stream arrived: each response's blocks were added
when the stream ended, whatever the reason it ended. That broke in a specific way.
The event handler sat inside the `try` that guarded `JSON.parse`, so an `error`
event from the API was swallowed as a "malformed frame". The stream then ended with
no stop reason, and the half-built response went into history as if it were
complete, including a `tool_use` block whose input had arrived only partly and had
been replaced by `{}`. No `tool_result` ever followed it. The API rejects a
`tool_use` without its result, so every later message in that conversation failed.

The chat now treats each question as a transaction. A *turn* is the question plus
every request it takes to answer it (report-tool rounds and `pause_turn`
continuations), and its messages build up beside the committed conversation. They
join it only when the model finishes (`end_turn`, or a stop sequence). Every other
ending discards the whole turn: an API error event, an HTTP rejection, a dropped
connection, a stream cut off before `message_stop`, malformed data, a refusal, the
length limit, either loop limit, Stop, New chat, or a model change. The next
request then carries exactly the conversation from before the question.

Rollback won over the other valid policy, answering an unfinished call with an
error `tool_result`, for two reasons the API imposes. A follow-up user message may
contain only `tool_result` blocks, so a result cannot share a message with the
reader's next question. And a call whose input never arrived whole has no input to
send back. Rolling back needs neither.

Nothing the reader saw is hidden. The partial answer stays on screen, marked
"Interrupted — this answer was not added to the conversation"; the question goes
back into the message box; and the reason appears as a notice (red for an error,
grey for an ordinary limit or the reader's own Stop). A turn that ends early also
aborts its request, so a response nobody will read stops streaming, and stops
being billed.

Three supporting rules make the transaction hold:

- **The stream is read to its contract.** Lines end at CRLF, LF, or CR, and a CR at
  the end of a network chunk waits for the next chunk, since it may be half of a
  CRLF. Multi-line `data:` frames are joined, and a final event without its blank
  line is never dispatched. The decoder rejects bad UTF-8 and is flushed at the
  end. Each event is checked as it arrives, and a response counts only if it
  reaches `message_stop` with a stop reason and every block closed. The `try` now
  guards only `JSON.parse`. Unknown event and delta types are ignored, as the API's
  versioning policy asks.
- **Tool input is parsed strictly.** Input is parsed when its block closes and is
  never replaced by `{}`. A call whose input is not a whole JSON object is neither
  run nor sent back; one missing a required field gets an error result instead of
  running on defaults.
- **A closed turn stays closed.** Stop, New chat, a model change, Forget key, and
  leaving the page close the turn at once and restore the controls without waiting
  for the aborted request. Anything that request reports later is dropped, so an
  old answer cannot write into a newer conversation.

The blocks themselves are kept exactly as the API sent them — thinking signatures,
web search results with their `encrypted_content`, citations — because the API
needs them back unchanged. Citation deltas now stay on the text block they belong
to; they used to be collected into a separate list and dropped from history. On
screen, each cited source gets a number after the text it supports. A search that
paused while filtering its results is continued with the `container` id the paused
response reported, which the API requires in that case.

The system prompt does two things that matter for trust: it **treats report
content as untrusted data**, and it **discloses that the source specifications are
not available to it.** The second is the honest limit — the chat can tell you what
a finding says and what the verifier concluded, but it cannot re-read the
specification to check, because the specification is not in the file.

## 5. Key policy

**The exported file never contains an API key.** This is absolute, and the
mechanics around it follow:

- The reader enters a key on first use.
- It lives **only in the page's memory**, never in `sessionStorage` or
  `localStorage`. Reloading or closing the page forgets it.
- A visible **Forget key** action clears it, and so does leaving the page
  (`pagehide`).
- Older reports kept the key in tab-scoped `sessionStorage` as `sc_api_key`. A report
  opened now deletes that entry and never reads it back, so a key saved by an old
  report cannot quietly come back.
- **Opening the file performs zero network requests.** Nothing happens until the
  reader chooses to ask something.

The last property is what makes the file safe to forward. A recipient who opens
it to read findings has not contacted anyone, has not spent anyone's money, and
has not been asked for a credential.

The model and effort choices, which are not secrets, are still remembered in
`sessionStorage`, behind guards. A browser that blocks storage, or storage that
throws, costs only the remembered choice, never the chat.

## 6. The GUI seam, and why no lifecycle file changed

This is the most elegant part of the integration and worth studying as a pattern.

`gui.SpecReviewApp._last_result` was turned into a **property whose setter enables
the footer "Save HTML Report…" button.** That single change intercepts an
assignment `review_run_controller.on_review_complete` was *already making*.

The consequence: **no review-lifecycle file changed.** Startup, batch handling,
resume, reset, cancellation, and the automatic DOCX-at-completion flow are all
untouched. A feature that adds a user-visible button in a stateful GUI usually
means editing the state machine; here it meant editing the state.

`report_controller.export_html_report_to_file` mirrors the DOCX controller's
canceled / success / error contract, with two differences: no sidecars are
written, and browser auto-open is nonfatal (failing to launch a browser is not a
failure to export a file).

`tests/test_html_gui_hook.py` pins the additive wiring — including an assertion
that `review_run_controller` kept its plain assignment and gained **no HTML
awareness at all**. That test exists precisely so a future edit cannot quietly
reintroduce coupling.

## 7. Pins

`tests/test_html_report_exporter.py` covers parity sentinels across every status
and section including program reports, the security properties and the
CSP-vs-exact-bytes relationship, no-mutation, determinism, hostile/Unicode/empty/
large states, and both the chat config and the no-chat variant.
`tests/test_html_report_javascript.py` checks that the shipped script parses.

`tests/test_html_chat_behavior.py` runs the chat. It writes a real report, takes
the script the CSP hash covers, and runs it under Node
(`tests/fixtures/chat_harness.js`) against a stand-in page and a scripted API whose
responses stream real SSE bytes: split anywhere, with any line ending, cut off,
failing mid-read, or arriving after an abort. It then drives the chat as a reader
would and checks what the page sent, what it showed, and whether its controls came
back. The conversation is never inspected directly. It is read from the *next*
request, which is where a corrupted history would do its damage.

The hostile-input and CSP-bytes tests are the ones that would be tempting to skip
and expensive to omit: both guard failures that are invisible in normal use and
only appear in the one situation the format exists for — a file that left the
machine that made it.
