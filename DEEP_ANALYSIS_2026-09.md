# Spec Critic — Deep Analysis (2026-09)

Scope: a full read of the API-facing core (`core/api_config.py`, the review /
verification / cross-check / compliance / research / drawing request builders,
`batch/`, `retry_policy.py`), the orchestration core (`pipeline.py`,
`program_pipeline.py`, `batch_resume.py`), the verification internals
(`verifier.py`, `verification_cache.py`, single-flight), the routing classifier,
and reproduction-driven checks of the deterministic detectors, the DOCX
extractor, the edit sidecar, `report_status`, the HTML report's embedded chat,
the updater, the key store, and GUI threading. Every API claim below was checked
against the current Anthropic documentation (web search / web fetch / server
tools / batch / prompt caching / models overview / tool reference pages, fetched
2026-09-16) and the installed SDK (`anthropic==1.5.0`). Every finding marked
**reproduced** was triggered with a script in this session; the scripts live
with this analysis' session scratchpad and are described inline so they can be
re-created.

Posture: the request shapes are correct and unusually well-defended (§5). The
real problems are (1) two deterministic detectors that misfire on ordinary CSI
structure and silently prime every review, (2) the *local* token gate in front
of the two biggest synchronous calls, (3) what the verification cache is allowed
to freeze, and (4) evidence the API already provides that the app throws away.
Nothing here needs a schema bump; several items need a measurement before they
are acted on.

Severity: **P1** = wrong result, lost finding, or lost paid work on an ordinary
input; **P2** = silent cost, robustness, or a documented invariant the code does
not hold; **P3** = accuracy of comments/docs, low impact.

---

## 1. Confirmed bugs

### P1-1 — `detect_empty_sections` flags every `PART n` heading and every integer-led body line (reproduced)

- **Where:** `src/input/preprocessor.py:491-547` (`_HEADING_LINE_RE`,
  `detect_empty_sections`). The heading regex is
  `(?:PART\s+\d+|\d+(?:\.\d+){0,2})\s+<title>`, so a bare integer (`2 coats of
  primer…`, `1 year from Substantial Completion.`) parses as a heading, and a
  heading is "empty" whenever the *next* heading follows it directly.
- **Reproduction:** a normal spec (`PART 1 - GENERAL / 1.01 SUMMARY / A. … /
  PART 2 - PRODUCTS / 2.01 … / 2.03 QUALITY ASSURANCE / 2 coats of primer … /
  2.04 WARRANTY / 1 year from … / PART 3 - EXECUTION / 3.01 …`) through
  `preprocess_spec` produced **seven** `Empty section` alerts: `PART 1 -
  GENERAL`, `PART 2 - PRODUCTS`, `PART 3 - EXECUTION`, `2.03 QUALITY ASSURANCE`,
  `2 coats of primer…`, `2.04 WARRANTY`, `1 year from…`. Expected: none.
- **Why it matters:** every CSI spec has PART headings followed directly by an
  article heading, so the PART case fires on essentially **every** file. The
  alerts ride `structural_alerts` into the `<pre_detected>` block of every
  review request (`_prepare_specs`, and the repair path's
  `_repair_pre_detected_alerts`) and into the report's "(deterministic check)"
  section; the review prompt also tells the model "do not duplicate items
  already flagged as pre-detected", so a bogus alert about `2.04 WARRANTY` can
  suppress a real finding about that article. Word auto-numbering keeps list
  labels in `numPr`, so "2 coats…" is exactly what extraction yields.
- **Fix:** a heading is empty only when the next heading is at the *same or a
  higher* level (PART → next PART/EOF; `x.xx` → next `x.xx`/PART/EOF), and the
  number must be `PART\s+\d+` or a dotted `\d+\.\d+(\.\d+)?` — never a plain
  integer. Add the spec above to `tests/test_preprocessor*.py` as a
  zero-alert case.

### P1-2 — `_normalize_issue_text` merges genuinely different findings (reproduced)

- **Where:** `src/orchestration/pipeline.py:439-441`. The regex
  `\d{2}\s?\d{2}\s?\d{2}[^.]*\.docx` was meant to strip a filename mention; the
  `[^.]*` gap swallows arbitrary prose up to the next `.docx`.
- **Reproduction:** two REPORT_ONLY findings in section `2.01`, no
  `codeReference`: *"Section 23 05 00 requires copper; the schedule in
  mech-schedule.docx lists steel"* (g.docx) and *"Section 23 21 13 requires PVC;
  the schedule in mech-schedule.docx lists steel"* (h.docx) both normalize to
  `'section lists steel'`; `_deduplicate_findings` returns **one** finding,
  "…requires copper… (found in 2 specs: g.docx, h.docx)". The PVC finding is
  gone from the report and the sidecar.
- **Fix:** constrain the gap to filename characters, lazily —
  `\b\d{2}\s?\d{2}\s?\d{2}(?:[ \w&()-]{0,80}?)?\.docx\b` — so prose containing
  `.`/`;`/`,`/`:` is never consumed.

### P1-3 — Word content controls lose their text silently (reproduced)

- **Where:** `src/input/extractor.py:622` (body walk handles only `w:p` /
  `w:tbl`); `:410` (`_collect_accept_all_text` descends only `w:r`,
  `w:hyperlink`, `w:ins`, `w:moveTo`).
- **Reproduction (python-docx + lxml-built file):** a body-level
  `<w:sdt><w:sdtContent>` holding a paragraph and a table: both strings absent
  from `content`, `extraction_warnings == []`, the content-loss warning does not
  fire (it counts drawings only). Inline: `Rating: [sdt dropdown "INLINE-SDT
  choice"] end.` → `'Rating:  end.'`; `See Section [fldSimple REF "23 05 00"]
  for details.` → `'See Section  for details.'`; `w:smartTag` text lost. A text
  box *inside* the same sdt **is** captured (the `.//` search), which shows the
  inconsistency. Complex fields, hyperlinks, drawings, `w:br`, tracked
  insertions are fine.
- **Why it matters:** rich-text and dropdown content controls are how spec
  templates implement "choose one" and structured blocks; an unresolved
  "Choose an item." dropdown also escapes the placeholder detector because its
  text never reaches the extractor output. This is not in CLAUDE.md's
  known-gap list (SmartArt, boxes in headers, tables in boxes).
- **Fix:** recurse into `w:sdt/w:sdtContent` at body level and inside cells;
  add `w:sdt→w:sdtContent`, `w:smartTag`, `w:fldSimple` to the descended-run
  containers; at minimum, count skipped sdt blocks into `extraction_warnings`.

### P1-4 — Cross-check and compliance gate on a cl100k count the Sonnet 5 tokenizer exceeds by 30–45%

- **Where:** `src/cross_check/cross_checker.py:320` (`run_cross_check`) and
  `:639` (`run_chunked_cross_check`); `src/compliance/compliance_checker.py:587`
  and `:771`; the ceiling is `tokenizer.CROSS_CHECK_RECOMMENDED_MAX = 822_000`.
- **What the code does:** `count_tokens(system) + count_tokens(user_message)` —
  the raw tiktoken cl100k count — is compared to 822k. Chunking starts only
  above 822k; below it the whole corpus goes out as one Sonnet 5 request.
- **Why it is wrong:** the repo's own `_LOCAL_SAFETY_FACTORS` records Sonnet 5
  at **1.45×** cl100k (the migration guide: ~30% more tokens than the
  4.6-family tokenizer). The per-spec review gate applies that factor and,
  above it, an exact `count_tokens` preflight; the two package-level passes
  apply neither. A corpus that counts **~769k–822k locally is ~1.0M–1.19M real
  tokens**: not chunked, and it cannot fit the 1M window (safe local ceiling at
  1.45× is ~690k).
- **Failure scenario:** 25 specs, local count 800k → `run_cross_check` submits
  → 400 `prompt is too long` (or a `model_context_window_exceeded` stop) →
  `INVALID_REQUEST`, non-retryable → `cross_check_status="failed"`; the
  compliance pass fails on the same corpus. No money is lost (a 400 is not
  billed) but the two passes produce nothing on exactly the large projects
  chunking exists for. Each *chunk* is size-checked by the same cl100k gate.
- **Fix:** both passes are one synchronous request each, so call the free
  `count_tokens_via_api` on the assembled request before choosing single vs
  chunked (fallback `safe_local_estimate(count, model=model)`), and size each
  chunk the same way.

### P1-5 — The report's Ask AI chat swallows mid-stream `error` events (agent-reproduced in node; confirmed by reading)

- **Where:** `src/output/html_report_exporter.py` `parseSSE` (≈2667-2681):
  `try { onEvent(JSON.parse(raw)); } catch (err) { /* ignore malformed frame */ }`
  wraps the *handler*, not just the parse. The handler (≈2789-2791) does
  `throw { streamError: … }` on `event.type === "error"`, which that catch
  swallows, so the `err.streamError` branch in `send().catch` (≈2909) is
  unreachable.
- **Trigger:** an HTTP-200 stream that emits `{"type":"error","error":{"type":
  "overloaded_error",…}}` and closes — the normal shape under load.
- **Observed:** `stopReason` stays `null`, the partial assistant content is
  pushed into `history`, no notice is shown, and if that partial content holds
  a `tool_use` block every later message 400s ("tool_use ids found without
  tool_result") until "New chat".
- **Fix:** parse inside the `try`, call `onEvent` outside it; or record
  `streamError` in state and reject from `streamOnce`'s `.then`. Related
  (agent-verified, P2): three more paths leave an orphaned assistant `tool_use`
  in `history` — the `MAX_TOOL_ROUNDS` exit, a `max_tokens` stop while a
  `tool_use` block is open (its `input` is coerced to `{}`), and the case above.
  When a turn ends without continuing, pop trailing `tool_use` blocks or append
  a `user` message of `is_error: true` `tool_result`s.

### P2-1 — The verification cache freezes grounded UNVERIFIED verdicts for 60 days (reproduced; contradicts CLAUDE.md)

- **Where:** `src/verification/verifier.py:2568` (`parsed.grounded = True` on
  every completed call), `_enforce_grounding_invariant` (returns early for
  UNVERIFIED), `src/verification/verification_cache.py:634` (`put` refuses only
  `not grounded`, `verification_failed`, `budget_exhausted`, and uncited
  CONFIRMED/CORRECTED/DISPUTED).
- **Reproduction:** `VerificationResult(verdict="UNVERIFIED", grounded=True,
  sources=[…])` → `cache.put` stores it; `cache.get` returns it
  (`stats()["size"] == 1`).
- **Why it matters:** CLAUDE.md says twice that "the `grounded` guard already
  drops every UNVERIFIED". It does not. A verifier that searched, retrieved
  sources, and still could not settle the claim — the most common outcome for
  paywalled code text, as the escalation section notes — is replayed as a
  cache hit for `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` (60). So (a) a re-run
  renders "Insufficient evidence — cache replay Nd old" without searching again
  even though a different query or a newer page might resolve it, and (b)
  `verify_finding` returns the cached result *before* the escalation gate, so a
  CRITICAL/HIGH finding that ended UNVERIFIED once is never escalated to Opus on
  any later run.
- **Fix (decision needed):** stop persisting UNVERIFIED (the in-run
  single-flight already shares them in-process), or give UNVERIFIED entries a
  short TTL (days), and let a cache hit on CRITICAL/HIGH UNVERIFIED still pass
  through `should_escalate_verification`. Then correct the two CLAUDE.md
  sentences.

### P2-2 — App-level retry loops ignore `Retry-After`, have no jitter, and give a 429 two short waits

- **Where:** `src/verification/retry_policy.py:467` (`compute_backoff_seconds`:
  `base * multiplier ** attempt`, base 5s, rate-limit multiplier 2 → 5s then
  10s, then terminal with `max_attempts=3`); `src/review/reviewer.py:473`
  (`_get_client(sdk_retries=False)` hands these loops a `max_retries=0` client).
- **Why it is wrong:** the SDK's built-in retry reads `retry-after` /
  `retry-after-ms` and jitters; turning it off (to stop 3×3 stacking — a good
  call) moved that duty to the app loop, which never took it on. Under a 429
  the server's instruction (commonly 10–60s) is ignored, every worker in a pool
  (up to 8 review streams + 5 verification streams + 4 research streams)
  retries in lockstep, and a finding is stamped `VERIFICATION_FAILED` after
  ~15s of total waiting. The app can trigger that storm on itself on a lower
  rate-limit tier.
- **Fix:** on `RateLimitError` sleep `max(retry-after header, computed)`, add
  ±25% jitter, and give RATE_LIMIT a larger attempt budget than transport blips.

### P2-3 — Routing never reads the document's own `SECTION NN NN NN` header; compact CSI filenames fall through (reproduced)

- **Where:** `src/programs/assignments.py:130-150` (`section_number =
  getattr(spec, "section_number", "")` — `ExtractedSpec` has no such field, so it
  is always empty and the filename is the title); `src/programs/routing.py:342`
  (`_extract_csi_section` accepts compact `211313` only from the dedicated
  field).
- **Reproduction (`route_spec`):**

  | filename (content) | result |
  |---|---|
  | `211313.docx` (body starts `SECTION 21 13 13 / WET-PIPE SPRINKLER SYSTEMS`) | **ambiguous**, 0.35 |
  | `210500.docx` (body `SECTION 21 05 00 - COMMON WORK RESULTS FOR FIRE SUPPRESSION`) | **unsupported**, 0.80 |
  | `21 13 13 Wet-Pipe Sprinkler Systems.docx` | supported, 0.99 |
  | `284621.docx` (body `SECTION 28 46 21 FIRE DETECTION AND ALARM …`) | supported, 0.85 (content terms rescued it) |

- **Why it matters:** compact six-digit numbers are one of the two common export
  conventions. "Unsupported" makes the run offer to skip the file as a coverage
  gap, so a Division 21 spec named `210500.docx` is skipped by default unless
  the operator overrides — while the explicit header the classifier was built
  to trust sits in line 1 of the content.
- **Fix:** in `assignments_for_specs`, scan the first ~15 lines of
  `spec.content` with `_EXPLICIT_CSI_RE` (or have the extractor expose the first
  `SECTION` heading) and pass it as `section_number`; accept a compact leading
  filename number when the same number appears as a `SECTION` header in the
  body (cross-confirmation keeps the "NFPA 13 ≠ Division 13" guard).

### P2-4 — Stale-cycle suppressors hide active citations: `prior to`, `historical`, `may not` (reproduced)

- **Where:** `src/input/preprocessor.py:~315` (`_should_suppress_stale_cycle`
  term list).
- **Reproduction:** *"Submit shop drawings prior to fabrication in accordance
  with 2022 CBC Section 1704."*, *"Coordinate with the historical society and
  comply with 2022 CBC."*, *"Contractor may not deviate from 2022 CBC Chapter
  17."* → no stale alert; *"Comply with 2022 CBC Section 1704."* → flagged.
  "prior to" is ubiquitous spec prose.
- **Fix:** `\bprior\b(?!\s+to\b)` (or `\bprior\s+(?:edition|version|cycle|code)`),
  drop bare `historical`, reconsider `may not`.

### P2-5 — The ASCE 7 detector misses the official designation (reproduced)

- **Where:** `src/input/preprocessor.py:~271`.
- **Reproduction:** `ASCE/SEI 7-16`, `ASCE 7–16` (en dash), `ASCE 7-2016`,
  `ASCE Standard 7-16` → nothing; `ASCE 7-16` → flagged.
- **Fix:** `\bASCE(?:/SEI)?(?:\s+Standard)?[\s–-]*7[\s–-]*(?:19|20)?(\d{2})\b`.

### P2-6 — Bare `TBD` is not a placeholder (reproduced)

`src/input/preprocessor.py:~130` matches only `[TBD]`; `Rating TBD.` → nothing,
although CLAUDE.md §5 lists `TBD`. `\bTBD\b` keeps `TBDF-200` clean (checked).

### P2-7 — `detect_inconsistent_file_naming` goes silent on mixed conventions (agent-reproduced)

`src/input/preprocessor.py:592-621`: `["23 05 00.docx","230500.docx","23-05-00
Common Work Results.docx","SECTION 23 05 00.docx"]` → no alert ("other" wins
and returns early); a 2–2 tie flags whichever style loses dict order;
`SECTION 23 05 00.docx` and `230500.docx` never parse. Fix: optional
`(?:SECTION\s+)?` prefix, a six-digit "compact" style, never let "other"
suppress, treat ties as "no dominant style".

### P2-8 — The submit and reconnect workers start the trace recorder *before* their `try`, so a recorder failure strands the GUI (confirmed by reading)

- **Where:** `src/gui/batch_controller.py:166-179` (`_maybe_start_recorder`
  before `try:` at 180; same shape at ~1285-1293 before `try:` at 1294).
  `_maybe_start_recorder` is a thin wrapper over `start_run_recorder`, and
  `TraceRecorder.start` does `self._trace_dir.mkdir(...)` with no guard
  (`src/tracing/recorder.py:233`).
- **Trigger:** tracing on (default) with `SPEC_CRITIC_TRACE_DIR` pointing at an
  uncreatable path (existing file, read-only directory, full disk).
- **Observed:** `OSError` kills the daemon thread before the `except` that
  dispatches `_on_review_error`; `is_processing` stays `True`, the run button
  stays "processing", the module selector stays disabled until restart. The
  export, context, drawing, and update workers all catch and marshal.
- **Fix:** move the recorder start inside the `try`, or make
  `start_run_recorder` never raise (log + return `None`, like
  `apply_startup_retention`).

### P3 — accuracy and low-impact items

- `api_config.py` (`_PHASE_CACHE_POLICY` comment) says Haiku's cache minimum
  is 2048 tokens; the caching docs list **4096** for Haiku 4.5. Inert.
- `_resolve_extended_output` (`review_request_builder.py`) compares a raw
  cl100k count to the 200k threshold: an Opus 5 spec at ~185k cl100k (~205k
  real) stays on the 128k cap. Harmless (truncation is repaired) but
  inconsistent with `_prepare_specs`, which pads.
- The verifier system prompt says `web_fetch` "can ONLY retrieve URLs that
  already appeared in a prior web_search result"; the docs also allow URLs in
  *user messages*, so a `codeReference` carrying a URL is fetchable without a
  search. The instruction forbids a cheaper path.
- No request sets `thinking.display`; on Opus 5 / Sonnet 5 every thinking block
  arrives empty, so `SPEC_CRITIC_TRACE_DEEP`'s "batch-verification thinking"
  capture is hollow. Send `{"type":"adaptive","display":"summarized"}` when deep
  tracing is on.
- `datacenter_fire` pins NFPA 13 at 2022 (what the 2024 IBC/IFC reference).
  Defensible as a reference assumption, but NFPA 13-**2025** is the current
  edition and AHJs commonly adopt "the current edition"; the pin's `note`
  should say so (likewise NFPA 72-2022 vs 2025, and NFPA 24/25/2001) so the
  verifier is not led to dispute a spec citing the current edition. NFPA 75/76
  are pinned at 2024, which is current.
- Placeholder prefix matches (`preprocessor.py:118-124`): `[EDITION 2022]` →
  "EDIT placeholder"; `[OPTIONAL]`, `[SELECTED BIDDER]` flagged. Use
  `\[\s*EDIT\b` etc. (agent-reproduced)
- `classify_status` treats `sources=[""]` as an accepted citation → `DISPUTED`.
  Require a non-blank source. (agent-reproduced)
- Long-form editions are not detected: `2019 California Building Standards
  Code`, `2022 Edition of the CBC`, `CBC (2022 edition)`, `Title 24, 2022`.
  (agent-reproduced)
- A directly-constructed no-op EDIT (existing == replacement) is correctly
  REPORT_ONLY with no sidecar entry, but `demotion_reason` stays `None` (only
  the parser stamps it), so the banner's demotion count under-reports on
  non-parser paths. (agent-reproduced)
- Chat: `citations_delta` is pushed to the Sources list but never merged into
  the text block that is replayed on the next turn, so provenance is dropped
  from later turns (accepted by the API). (agent-verified)
- Chat: the key lives in `sessionStorage` on a `file://` page; Chromium gives
  all `file://` documents one storage origin, so another local HTML file opened
  in the *same tab* could read `sc_api_key`. Per-tab scope limits exposure;
  keeping the key in a closure variable, or clearing on `pagehide`, closes it.
  (agent-verified)
- The API key is exported into `os.environ` by three controllers and inherited
  by the spawned installer / browser. Same-user impact only; passing the key to
  `_get_client` directly avoids it. (agent-verified)

---

## 2. Claude API usage — problems and material improvements

Nothing sends a rejected parameter today. The items below are where the app
pays more than it needs to or throws away evidence the API already provides.

### API-1 — Project Context is sent uncached on every per-spec review

- **Where:** `prompts.get_single_spec_user_message` renders intro + code-basis
  line + reminders + `<project_context>` + `<spec>` + `<pre_detected>` +
  `<final_task>` as **one** user string; the only `cache_control` breakpoints
  are on the system prompt and the trailing tool (`grep cache_control src/`).
- **Effect:** the research profile plus a drawing digest can reach the 100k
  `PROJECT_CONTEXT_MAX_TOKENS` cap and is re-billed at full input price for
  **every** spec (and again on cross-check/compliance). A 20-spec hyperscale run
  with an 80k context: 1.6M uncached tokens on Opus 5, ~$4 batch / ~$8
  real-time per run, before thinking.
- **Improvement:** split the user content into two text blocks — `[shared
  intro + <project_context>]` with `cache_control` and `[<spec> … <final_task>]`
  without — three breakpoints total (limit 4). The intro/reminder lines are
  byte-identical across specs of one module. Caveats that make this
  measure-then-keep: (1) batch items run in no guaranteed order, so N items can
  each pay the 2× 1h write before any read lands — `cache_read_input_tokens`
  telemetry already exists to check the hit rate; (2) on the real-time transport
  with 4 workers the worst case is 4 writes then reads, a clear win. Keep the 1h
  TTL for batch (per the batch docs) and use 5m for real-time (1.25× writes,
  workers never idle > 5 min).

### API-2 — The API's citation channel is discarded; "grounded" means retrieved, not attributed

- **Where:** nothing under `src/verification/` reads `citations` /
  `cited_text` / `web_search_result_location` (grep). Search snippets arrive as
  `encrypted_content`, so `source_quote` (free text the model asserts in the
  verdict tool) can never be checked locally — CLAUDE.md concedes "grounding
  establishes retrieval, not support".
- **What the API already gives you:** citations are *always on* for web search.
  Every cited text block carries `CitationsWebSearchResultLocation` objects with
  plaintext `cited_text` (≤150 chars), `url`, `title` (SDK 1.5.0, verified).
  That is the one plaintext, API-attested link between a passage and a URL.
- **Improvement:** (a) harvest citations from every text block in the
  conversation (`_collect_conversation_evidence` already walks the blocks);
  (b) for CONFIRMED/CORRECTED/DISPUTED require that `source_quote` overlaps a
  `cited_text` whose `url` is in the accepted pool (token overlap ≥ ~0.6, since
  `cited_text` is truncated); (c) instruct the model to state the supporting
  passage in a text block *before* calling `submit_verification_verdict`, so
  citations exist to check. This moves the trust model from "the URL was
  returned by a tool" to "the API attributed this passage to that URL", closes
  the gap the trust audit named, and costs no extra calls. Render `cited_text`
  in the evidence panel beside the model's quote.

### API-3 — The escalation tier is the only tier without `web_fetch`

Confirmed against the web-fetch tool page: dynamic-filtering fetch lists
Opus 4.8, Sonnet 5, Sonnet 4.6, Fable 5/5.1, Mythos — **not Opus 5**. The code
gates correctly, so DEEP_REASONING — the path reserved for CRITICAL
jurisdictional findings and every escalation — is snippet-only while the cheap
initial pass can read full code pages. Options: escalate to **Opus 4.8** (same
$5/$25, fetch-capable, Jan-2026 cutoff; retrieval matters more than cutoff for
a verifier) or **Sonnet 5 at `xhigh`** (fetch-capable, a fifth the price). One
default change plus a golden update; measure with the calibration harness first.

### API-4 — Exact token counting is free and unused where it matters most

Ties to P1-4. `count_tokens` is free; the per-spec review uses it, the two
package-level passes do not, and they are the requests most likely to approach
the window. One call per pass, before submit.

### API-5 — Structured outputs for the phases that carry no server tools

Review, cross-check, compliance, drawing impact, and triage attach one custom
tool and instruct the model to call it; with `tool_choice: auto` the model can
still answer in prose, which is why the `<findings_json>` /
`<drawing_impact_json>` fallback parsers and the "failed review, zero findings"
class exist. `output_config: {"format": {...}}` makes the *response* itself
schema-constrained for those phases (no server tools, so no citation conflict),
removing the plain-text detour and the fallback parsers. Keep tool use for
verification and research (server tools + citations). Validate on the live
smoke test first (`tests/test_network_smoke.py`), as was done for strict tools.

### API-6 — The review prompt is written for conservative reporting, but a verifier sits downstream

`prompts.py` says "Only report a finding if you have concrete evidence", "emit
it only when it is genuinely useful", "Return exactly as many findings as
genuinely supported". The Sonnet 5 / Opus 5 migration guidance is explicit that
review harnesses with conservative-reporting instructions lose recall on these
models because they follow the bar literally, and recommends a coverage-first
prompt ("report every issue you find, including ones you are uncertain about …
a separate verification step will filter") — which is this app's architecture.
Expected: higher recall, more verification spend, same precision after
filtering. Measure with `evals/live_capture` + `evals/labeled_specs`.

### API-7 — Effort sweep on Opus 5 review

The Opus 5 guide says `low`/`medium` are "unusually effective" and that effort
defaults carried from prior models are usually wrong. Review runs at `high`
with a 128k output cap that thinking shares. Run the labeled-spec eval at
`medium`: if recall holds, the review phase (the largest spend) gets cheaper and
the truncation-repair rate falls.

### API-8 — `Retry-After` (see P2-2)

### Checked and clean (API)

- Every large synchronous call streams; only 8k-output triage uses `create`.
- `thinking={"type":"adaptive"}` wherever sent; no `budget_tokens`, sampling
  params, or prefill; `tool_choice` stays `auto` under thinking; Haiku triage
  alone forces its tool (no thinking); `strict: true` schemas stay inside the
  supported subset.
- Batch: `custom_id` matches `^[a-zA-Z0-9_-]{1,64}$`; results keyed by
  `custom_id`; `output-300k-2026-03-24` is a real header and the batch page
  names Opus 5 / Sonnet 5 for it; `service_tier: "auto"` inside batch params is
  not on the batch API's rejected list (inert there).
- Web tools: `web_search_20260209` / `web_fetch_20260209` are current (the
  `_20260318` variants add `response_inclusion`, which must **not** be used —
  it drops the nested result blocks the grounding pool is built from);
  `user_location` shape is right (ISO-2 country); `blocked_domains` only; the
  53-entry blocklist is under no documented Messages-API cap (64 is Managed
  Agents) — watch `search_error_count` for `request_too_large`.
- `pause_turn`: assistant content re-sent as-is with the same tools; container
  id carried across resumes and waves; fetched-PDF resend sanitized.
- Cache breakpoints ≤ 4 everywhere; single 1h TTL so no long-before-short
  ordering issue; system prompts (~2.7k–3.1k tokens) clear every minimum.
- Retry layering: SDK retries disabled exactly where an app loop owns retries.
- No unknown `anthropic-beta` values are sent.
- Chat (agent-verified): model ids valid; `web_fetch` attached only where
  `supports_web_fetch` (Opus 5: no, Sonnet 5: yes); tool_result turns contain
  only tool_result blocks; server-tool blocks (incl. `encrypted_content`)
  replayed unchanged; `pause_turn` capped at 5, tool rounds at 8; header
  `anthropic-dangerous-direct-browser-access` sent; no `eval`/`innerHTML`; CSP
  hash matches the exact bytes; key never in the file.

---

## 3. Architecture and alternative approaches

### ARCH-1 — Cross-division coordination is lost the moment chunking starts

Known (TRUST_AUDIT P1-3), but cheaper to fix than the limitation suggests: a
two-level pass. Level 1 runs per chunk as today and also returns a compact list
of *coordination facts* (equipment tags, setpoints, responsibilities,
referenced sections — a few thousand tokens per chunk). Level 2 runs one
cross-chunk call over the fact lists and emits cross-division findings under
the same `cf-` id discipline. A few percent of the chunked pass's cost; it
restores the Division 21 ↔ 28 (suppression ↔ alarm) coordination the
hyperscale program explicitly says it cannot do.

### ARCH-2 — Three collect drivers are in lockstep today, by inspection

`batch_controller._do_collect`, `run_batch_collection_headless`, and
`collect_program_results` were compared step by step: same stage order, same
DISPUTED-exclusion rule, same transport routing, same cache persistence, same
pending-state clearing rule. The code comment proposing to collapse the GUI
sequence onto the headless core is the right next step; the divergence risk
returns every time a stage is added (drawing impact was added to both by hand).

### ARCH-3 — Per-finding verification repeats the same citation lookup

Single-flight dedupes *identical* cache keys only. Twelve findings that all cite
`NFPA 13 §8.15.1` in different sections are twelve full verification
conversations, each with up to 8 searches, each re-establishing what §8.15.1
says. Intermediate design: group findings by normalized `codeReference`, run
one *citation-resolution* call per reference (fetch once, quote once), then
verify each finding's claim against the resolved text in a search-free call.
Per-finding verdicts and the grounding invariant survive (the resolution
call's accepted URLs and `cited_text` carry forward); searches fall by the
citation duplication factor.

### ARCH-4 — Research results are per-run and never reused

CLAUDE.md lists a cross-run research cache as open. Adoption facts change on a
code-cycle timescale; keying a profile by (module, jurisdiction fingerprint,
research month) with a "reuse last month's profile" prompt removes the
four-dimension fan-out (the phase with the most searches) from repeat runs in
the same city. The governing-basis fingerprint already makes a replayed profile
safe for the verifier.

### ARCH-5 — Deterministic detectors have no fixture corpus of real spec structure

P1-1, P2-4, P2-5, P2-6 and the P3 detector items share one cause: the
detectors are tested on hand-written strings, not on a corpus of ordinary CSI
spec structure (PART/article scaffolding, auto-numbered lists, standard
"prior to" prose). A small set of clean real-shaped specs asserted to produce
**zero** alerts would have caught every one of them; `evals/labeled_specs`
already has the shape for this.

---

## 4. Verified clean (checked and holding)

- Dedup: `_dedup_key` merges across files; merged findings carry per-file
  originals; the sidecar emits one entry per affected file with that file's own
  `anchorText` / `evidenceElementId` and `has_per_file_original`; singleton,
  REPORT_ONLY, and no-op EDIT cases behave as documented; `(finding_id,
  fileName)` is unique; `rf-`/`cf-`/`lc-` prefixes namespace ids; anchor
  validation runs pre-dedup on review, cross-check, and compliance findings.
- Repair batch: refusals never retried; repair results merge only when they
  parsed OK; a saved repair batch is re-attached, never re-paid; primary
  results are never discarded on any repair failure path.
- Batch verification waves: exactly-once terminal result, fallback and
  follow-up wave mutually exclusive, continuation-cap `>` parity with
  real-time, whole-conversation evidence and container carried across waves,
  escalation wave skips already-escalated findings.
- Escalation merge: single merge helper; `models_disagreed` requires two
  conclusive grounded verdicts; both paid conversations priced.
- Grounding: `sources` is the accepted list; downgrade paths set
  `grounded=False`; DISPUTED needs an accepted citation at render time.
- `classify_status` / `classify_edit_action`: every branch in CLAUDE.md §4
  reproduced (DISPUTED+grounded+sources → DISPUTED; grounded without sources →
  INSUFFICIENT_EVIDENCE; `verification_failed` beats CONFIRMED and
  `models_disagreed`; `models_disagreed` beats `local_skip`;
  `budget_exhausted` stays INSUFFICIENT_EVIDENCE).
- Pending state: real-time runs refused; defensive load; cycle-label mismatch
  blocks resume; bare-id recovery resolves stem collisions by submission order.
- Research fan-out: one failure never kills the pool; all-fail aborts before
  review spend; merged in declared order.
- Compliance merge precedence and chunk-local ADD filtering behave as D-7
  states; chunk synthesis keeps every completed chunk's findings.
- Detectors that hold: `2022 CBC` / `CBC 2022` / `2019 California Building
  Code` flagged, `2018 CBC` invalid-only, `superseded by` suppressed, `2025 CBC`
  clean, clause bounding on `;`/`.`; ASCE century pivot; DC module suppresses
  stale by design while invalid still fires; template markers (`PXXX-200`,
  `XXXX`, `??` clean; `TODO:`/`FIXME`/`XXX`/`???`/`Lorem ipsum` flagged);
  bracket placeholders; duplicate-paragraph 80-char boundary and synthetic
  `[Header]`/`[Text Box]` skips; LEED word boundaries.
- Extractor: reconstruction invariant and element-id uniqueness hold on every
  probe document; hyperlinks, complex fields, text + drawing, `w:br`, tracked
  insertions kept / deletions dropped; merged cells emitted once; nested tables
  under path ids.
- Updater: https enforced on manifest, installer, and both post-redirect URLs;
  `.part` streaming with SHA-256 during the stream; `os.replace` only after a
  match; temp unlinked on any failure; basename-only `.exe`; `parse_version`
  orders `3.10.0 > 3.7.0` and `rc` below final; download worker and install
  prompt guarded.
- Key store: read-only; no code path writes a key to disk; POSIX fallback file
  chmod 0600; redaction covers `sk-ant-` / `Bearer` / `AKIA` shapes.
- GUI threading: every worker-side widget mutation in the context, batch,
  report, and diagnostics controllers is marshaled through `app.after`, and
  except-paths reset running flags — with the one exception in P2-8.
- The three collect drivers match (ARCH-2).

---

## 5. Suggested order of work

1. P1-1 (empty-section detector) and P1-2 (issue-text normalization) — both are
   one-regex fixes with a test each, and both change what the model is told on
   every run.
2. P1-4 (exact/padded count on cross-check + compliance) — small; closes a real
   failure band on large projects.
3. P1-3 (content controls) — extractor recursion plus a warning; add the sdt
   probe document to the extractor tests.
4. P1-5 (chat stream errors, orphaned `tool_use`) — small JS change; re-run
   `tests/test_html_report_javascript.py` and the CSP hash test.
5. P2-2 (`Retry-After` + jitter) and P2-8 (recorder inside `try`) — small.
6. P2-1 (stop freezing UNVERIFIED) — decision, then a small change and a
   CLAUDE.md correction.
7. P2-3 (routing reads the SECTION header) and the P2-4/5/6/7 detector fixes;
   add the compact-filename and clean-spec cases to the routing / preprocessor
   pins (ARCH-5).
8. API-1 (cache the context block) — measure hit rate on the next two runs.
9. API-2 (citations as evidence) — the single largest trust improvement.
10. API-6 / API-7 / API-3 — eval runs, then decide.
11. ARCH-1, ARCH-3, ARCH-4 — design work, in that order of value.
