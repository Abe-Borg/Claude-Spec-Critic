# Spec Critic v2.11.0 — Code-Quality Implementation Plan

This document captures the actionable items from the v2.11 code review. It is structured for handoff to autonomous coding agents. Each chunk is **independent and standalone** — pick one up cold, complete it, run the acceptance criteria, move on. Do not bundle chunks unless explicitly told to.

---

## How to use this document

1. **Branch.** All work happens on `claude/code-review-ZanKq`. Create the branch if it does not exist (`git checkout -b claude/code-review-ZanKq`). Commit per-chunk with a clear message referencing the chunk number (e.g., `chunk 1.1: fix pause_turn handler in verifier`). Push to `origin` when the chunk is complete and acceptance criteria pass.
2. **Verify before implementing.** This plan was authored against the v2.11.0 source tree on a specific date. **Some chunks may already be partially or fully implemented** — perhaps by an earlier agent on this branch, perhaps because the codebase has moved. Before touching a file, read it. If a chunk's "What to change" already reflects the current code, mark the chunk done in your commit message (e.g., `chunk 2.1: already implemented; no changes`) and move on. Do not redo completed work.
3. **No speculative changes.** Each chunk has a defined scope. Do not refactor adjacent code that is not in scope. Do not change behavior the chunk does not call out. Suspected bugs you find that are not in the plan should be noted in a new file `REVIEW_FOLLOWUPS.md` for human triage, not silently fixed.
4. **Tests.** The repo has a `tests/` directory. Before making changes, run the test suite to capture a baseline. After your changes, run it again. Net new failures are blockers. If the existing tests are inadequate for your chunk (e.g., no test currently covers the path you're modifying), add a minimal test that exercises the fix.
5. **Hooks and signing.** Do not pass `--no-verify` or skip pre-commit hooks. If a hook fails, fix the underlying issue.
6. **Source of truth.** When the plan and the code disagree about an invariant, **trust the code first** — read the file thoroughly, then update your understanding of the chunk. The plan reflects the reviewer's reading; the code is reality.
7. **One in-progress task at a time.** Use the TodoWrite tool to track which chunk you are on. Mark completed immediately after acceptance criteria pass.

---

## Codebase orientation (read once before starting)

Spec Critic is a Python desktop app for reviewing California K-12 mechanical/plumbing specifications using the Anthropic Claude API. Read `CLAUDE.md` and `src/__init__.py` first — `CLAUDE.md` is unusually accurate and explains the runtime topology. Key entry points:

- `src/api_config.py` — centralized model IDs, output caps, web-search config, prompt-caching helpers, feature flags. Touch one file to change a model.
- `src/prompts.py` — system + user prompt builders. Mode-aware (Strict/Comprehensive/Safe-edit), cycle-aware (CA 2022/2025).
- `src/structured_schemas.py` — tool-use JSON schemas for review/cross-check/verification/triage, plus `extract_tool_use_block` helper.
- `src/reviewer.py` — streaming review API client.
- `src/batch.py` — Anthropic Message Batches wrapper.
- `src/verifier.py` — verification with web_search server tool; Sonnet-first with Opus-4.7 escalation.
- `src/cross_checker.py` — cross-spec coordination, chunked by CSI division.
- `src/pipeline.py` — central orchestrator.
- `src/edit_locator.py`, `src/spec_editor.py`, `src/apply_edits.py` — edit application path.
- `src/gui.py` and `src/widgets.py` — CustomTkinter UI.

Models in use (all current as of this review): `claude-opus-4-7` (review/cross-check), `claude-sonnet-4-6` (verification default), `claude-haiku-4-5` (triage/synthesis), with Opus 4.7 escalation. Prompt caching uses 1h TTL ephemeral on the system prompt and trailing tool definition. Web search uses tool version `web_search_20260209` with severity-tiered `max_uses` (3–7) and a blocked-domains list.

---

# PHASE 1 — Critical correctness bugs

High-impact, mostly small, mostly independent. Tackle in any order. Each one materially affects either correctness or user data safety.

---

## Chunk 1.1 — Fix `pause_turn` handler in verifier

**Files:** `src/verifier.py` (around lines 536–540 in v2.11.0).

**Status check before starting:** Read the current `_run_verification_call` function. If the code already does *not* append a synthetic `"continue"` user message after `pause_turn`, this chunk is done.

**What and why.** When a streaming Claude call uses a server-side tool (web_search), the server runs a sampling loop with an iteration cap. If the cap is hit, the response comes back with `stop_reason == "pause_turn"`. The official Anthropic guidance for resuming is to re-send the messages with the assistant's response appended — the API detects the trailing `server_tool_use` block and resumes automatically. **The current code instead also appends a synthetic user-turn message with the literal text `"continue"`.** This is wrong: it wastes input tokens, can derail the model's reasoning ("the user just said 'continue' — should I keep searching?"), and on Opus 4.7 with adaptive thinking, the model thinks *about* the synthetic message before resuming.

**What to change.** When `stop_reason == "pause_turn"`, append only `{"role": "assistant", "content": response.content}` to messages and loop. Remove the second `messages.append(...)` line that adds the user-turn `"continue"` text. Keep the `max_continuations` cap.

**Acceptance criteria.**
- The verification loop no longer adds a user-turn `"continue"` message after `pause_turn`.
- Existing tests still pass.
- Add (or update) a test that simulates a verification call returning `pause_turn`, confirming the next iteration's `messages` does not contain a user-turn with literal `"continue"` text.

**Risks.** None significant; this brings the code in line with the documented API contract.

**Dependencies.** None.

---

## Chunk 1.2 — Make resume-state writes atomic

**Files:** `src/gui.py` (around `save_batch_state()` near lines 168–172 in v2.11.0). Also confirm whether `src/resume_state.py` has any direct write paths that bypass the GUI helper.

**Status check before starting:** Read `save_batch_state` and any other write site for resume state. If the writes already use a temp-file + `os.replace` pattern, this chunk is done. (Note: `verification_cache.py` already uses atomic writes — use it as a model.)

**What and why.** `save_batch_state()` writes resume state with `Path.write_text()`. A crash, OOM, or full disk during the write produces a truncated JSON file. On the next launch, `load_batch_state()` catches the JSON decode error and silently deletes the saved session — meaning a crash during a multi-hour batch costs the user a full re-run plus the original API spend.

**What to change.** Write to a temp file in the same directory as the target (so the rename stays on one filesystem), `flush()` + `fsync()` the temp file, then `os.replace(temp_path, target_path)`. Look at the on-disk write in `src/verification_cache.py` as the local reference implementation.

**Acceptance criteria.**
- `save_batch_state()` never leaves a partially-written file at the target path.
- Add a test that simulates a write failure mid-stream (e.g., monkey-patch the underlying write call to raise after partial write) and verifies the original target file is unchanged.
- Existing tests still pass.

**Risks.** If the resume-state file lives on a network or sandbox filesystem where `os.replace` semantics differ, document the assumption in a comment. On standard local filesystems (Windows NTFS, macOS APFS, Linux ext4) `os.replace` is atomic.

**Dependencies.** None.

---

## Chunk 1.3 — Fix multi-edit-per-paragraph offset bug

**Files:** `src/spec_editor.py` (the conflict resolver around lines 309–388 and the replacement application around lines 655–674 in v2.11.0).

**Status check before starting:** Locate the function that applies in-place EDIT replacements (currently `_replace_in_paragraph` or similar). Read the conflict resolver. The bug exists if two non-overlapping EDIT actions targeting the same paragraph (different start offsets) can both pass the conflict check, and then the first replacement's character delta is not accounted for by the second.

**What and why.** When two EDITs target the same paragraph at non-overlapping spans (e.g., `"old1"` at chars 10–15 and `"old2"` at chars 30–35), both pass the overlap check. The first applies, the paragraph text changes length, and the second's saved start offset is now stale. The precondition substring fallback (`_precondition_holds_for_paragraph` around line 248) sometimes rescues this when the second pattern is locally unique, but it silently fails or mis-targets when it isn't.

**What to change.** Two viable strategies (pick one — discuss in the commit message which you chose and why):

- **Strategy A (simpler).** Group same-paragraph EDITs into a batch. Apply them in *descending start-offset order* (rightmost edit first) so earlier edits never shift later offsets.
- **Strategy B (more general).** Collapse all EDITs against one paragraph into a single replacement: walk the paragraph text once, applying each edit by absolute offset to a working string, then write the final string back in one operation. This sidesteps the offset-stability question entirely.

Either way, ensure ADD and DELETE order assumptions still hold (ADDs descending body_index, DELETEs descending after replacements).

**Acceptance criteria.**
- Two non-overlapping EDITs to the same paragraph both apply correctly without one being silently dropped or mis-targeted.
- Add a test fixture with a paragraph containing `"alpha bravo charlie delta echo"` and two EDIT actions: `bravo` → `BRAVO`, `delta` → `DELTA`. Verify the final paragraph is `"alpha BRAVO charlie DELTA echo"`.
- Add a test where the first EDIT *grows* the paragraph (replacement is longer than original) and the second EDIT still targets the correct downstream span.
- Existing tests still pass.

**Risks.** Edit ordering is load-bearing for index stability. Read the full `spec_editor.apply_edits_to_spec` to understand the existing ordering invariants (in-place replacements → ADDs descending → DELETEs descending). Do not break them.

**Dependencies.** None.

---

## Chunk 1.4 — Cancel/dedupe in-flight token-count requests when file list churns

**Files:** `src/gui.py` (around `_refresh_exact_token_count()` near lines 943–987 in v2.11.0).

**Status check before starting:** Read the current implementation. If it already debounces or cancels in-flight requests when the file list changes, this chunk is done.

**What and why.** When the user rapidly selects/deselects files, each change spawns a daemon thread calling `count_tokens_via_api()` on the largest spec. The epoch pattern prevents stale results from corrupting the UI, but the API calls themselves are not cancelled — they complete and their results are silently discarded. Costs the user real tokens for nothing.

**What to change.** Pick the simpler of two options:

- **Option A — Debounce.** Use a Tk `after()` timer to delay the launch by 300–500 ms after the last file-list change. If another change arrives before the timer fires, reschedule. Most rapid churn never launches a request at all.
- **Option B — Single-flight.** Track the in-flight epoch in an instance variable. When starting a new preflight, check if a previous one is in flight; if so, mark the previous epoch as stale via a `threading.Event` or a flag the worker checks before issuing the API call. (This requires the worker to re-check the flag right before the network call — Python doesn't have request cancellation primitives for `requests`/`httpx` blocking calls.)

Option A is simpler and recommended unless you have a strong reason to prefer B.

**Acceptance criteria.**
- Rapidly toggling files (programmatically simulate 10 toggles in 200 ms) results in at most one outbound `count_tokens_via_api` call.
- Existing tests still pass.

**Risks.** Tk `after()` runs on the main thread — do not put long-running work inside the timer callback; only use it to schedule the worker thread launch.

**Dependencies.** None.

---

## Chunk 1.5 — Stale-code-cycle detector false positives on quoted historical references

**Files:** `src/preprocessor.py` (around `detect_stale_code_cycle_references` near lines 186–268 in v2.11.0).

**Status check before starting:** Read the detector. If it already has negation/deprecation context awareness, this chunk is done.

**What and why.** The detector flags any bare year reference like `"2019 CBC"` as stale when the active cycle is 2022 or 2025. It does not look at surrounding context, so sentences like:

- `"previously per CBC 2019, now superseded by CBC 2025"`
- `"shall NOT follow the 2019 CBC approach"`
- `"the prior cycle (2019) is referenced here for historical context only"`

— all generate false alerts. These leak into the report, eroding user trust in the tool.

**What to change.** Add a context window check around each match. If any of the following appear within ~6 words *before* the matched year token, suppress the alert: `previously`, `formerly`, `superseded`, `withdrawn`, `obsolete`, `not`, `no longer`, `prior`, `historical`. Tune the word distance based on a few hand-crafted examples; six words is a reasonable starting point.

A cleaner alternative — discuss in the commit message if you choose it — is to delete the detector entirely and let the LLM catch stale-cycle references. The LLM does this well and the preprocessor adds little signal here.

**Acceptance criteria.**
- For each of the three example sentences above, the detector emits zero alerts when the active cycle is 2022.
- For an unambiguous stale reference like `"all work shall conform to the 2019 CBC"` with active cycle 2022, the detector still emits the alert.
- Existing tests still pass.

**Risks.** Negation/deprecation language in English is unbounded — accept that this is a heuristic and will miss some edge cases. Document the heuristic in a comment.

**Dependencies.** None.

---

## Chunk 1.6 — Cross-check disputed-upstream filter under/over-drops on multi-file findings

**Files:** `src/pipeline.py` (around `_drop_cross_check_findings_with_disputed_upstream` near lines 556–599 in v2.11.0).

**Status check before starting:** Read the function. If the matching logic already considers partial coverage of the upstream files set, this chunk may already be done.

**What and why.** A cross-check finding with `affected_files=[A, B]` is currently dropped only when *both* `(A, section)` and `(B, section)` appear in the disputed-upstream set. Cross-check findings — the entire point of the cross-checker — claim coordination problems between specs. When only one side's upstream is disputed, the coordination finding still applies (the other side is unchallenged). The current strict-AND filter over-preserves findings; the inverse (strict-OR) would over-drop them.

**What to change.** Decide and document the correct rule. A defensible policy: drop a cross-check finding only when *all* its referenced upstream files have a disputed finding at the same section. If *any* of the upstreams is undisputed, keep the cross-check finding (the coordination problem still exists relative to the undisputed side). Add a comment explaining the policy and why partial coverage keeps the finding.

**Acceptance criteria.**
- Add a test with a cross-check finding citing two files. When only one file's upstream is disputed, the cross-check finding is preserved.
- When both files' upstreams are disputed, the cross-check finding is dropped.
- Existing tests still pass.

**Risks.** Policy choice is judgment; document the rationale in the commit message and in a comment near the implementation.

**Dependencies.** None.

---

# PHASE 2 — API best-practice improvements

Lower risk than Phase 1 but real cost/quality wins. Several can be parallelized across agents.

---

## Chunk 2.1 — Add `effort` parameter to verification calls

**Files:** `src/verifier.py` (the `_run_verification_call` function around line 510; the streaming `client.messages.stream(...)` invocation around line 520).

**Status check before starting:** If `output_config={"effort": ...}` already appears on the verification stream invocation, this chunk is done.

**What and why.** Opus 4.6 / Sonnet 4.6 default to `effort: "high"`. Verification is a classification task (CONFIRMED / DISPUTED / CORRECTED / UNVERIFIED plus a 1–2 sentence explanation) with a search-and-compare phase. It does not need maximum effort. Setting `effort: "medium"` on Sonnet (the default verifier) reduces total tokens per call by 20–40% in typical cases with no quality loss for grounded verdicts.

**What to change.**
- Add a helper in `src/api_config.py`: `verification_effort()` returning `"medium"` by default, overridable via `SPEC_CRITIC_VERIFICATION_EFFORT` env var. Accept values `low | medium | high | max` and validate.
- In `verifier.py`, pass `output_config={"effort": verification_effort()}` to `client.messages.stream(...)` in `_run_verification_call`.
- On Opus 4.7 escalation calls specifically, consider keeping `effort: "high"` (or use `"xhigh"` — see below). Document the choice.
- Note: Haiku does not accept the `effort` parameter — do not add it to triage/synthesis calls. The triage and synthesis paths must remain unchanged.

**Opus 4.7 specific.** Opus 4.7 supports `effort: "xhigh"` as a new level between `"high"` and `"max"`. For verification escalation specifically — which is reserved for CRITICAL/HIGH findings where Sonnet returned UNVERIFIED — `"high"` or `"xhigh"` is appropriate. Do not use `"max"` (it can over-think and consume excessive tokens).

**Acceptance criteria.**
- Sonnet verification calls pass `output_config.effort == "medium"`.
- Opus escalation calls pass `output_config.effort == "high"` (or `"xhigh"`).
- The env var `SPEC_CRITIC_VERIFICATION_EFFORT` overrides the default and validates input.
- Existing tests still pass.
- Add a test asserting the request kwargs include the expected `output_config`.

**Risks.** Quality regression on hard verifications. Recommend running the existing test suite plus one real-world verification against a representative spec set before merging — but if the test suite doesn't have an integration test, ship the change behind a feature flag that defaults on and document the rollback knob.

**Dependencies.** None.

---

## Chunk 2.2 — Switch to forcing `tool_choice` and remove fallback JSON parsers

**Files:**
- `src/structured_schemas.py` (`review_tool_choice`, `cross_check_tool_choice`, `triage_tool_choice` around lines 294–322).
- `src/prompts.py` (the `<output>` block in the system prompt around lines 161–181).
- `src/reviewer.py` (the tagged-JSON fallback parser around lines 136–180).
- `src/batch.py` (the fallback parsing path).
- `src/cross_checker.py` (the fallback parsing path).

**Status check before starting:** This is the largest chunk in Phase 2. Read all five files first. If the `review_tool_choice` is already `{"type": "tool", "name": "submit_review_findings"}` and the fallback parsers have already been deleted, this chunk is done.

**What and why.** The current code uses `tool_choice: {"type": "auto", "disable_parallel_tool_use": true}` everywhere, justified by a comment claiming that forcing `tool_choice` is rejected when `thinking` is enabled. **This is no longer true** — current Anthropic docs explicitly show `tool_choice: {"type": "tool", "name": ...}` working on Opus 4.7 — and review calls don't even enable thinking. The cost of the "defensive" auto-choice is a fragile tagged-JSON fallback parser duplicated across four modules, plus a paragraph in the system prompt instructing the model how to emit JSON tags as a fallback. The fallback path can silently fire on any tool-call hiccup, swapping the structured path for regex-parsing free-form text.

**What to change.**

1. **`structured_schemas.py`:**
   - Change `review_tool_choice()` to return `{"type": "tool", "name": "submit_review_findings", "disable_parallel_tool_use": True}`.
   - Change `cross_check_tool_choice()` to return `{"type": "tool", "name": "submit_cross_check_findings", "disable_parallel_tool_use": True}`.
   - Change `triage_tool_choice()` to return `{"type": "tool", "name": "submit_triage_classifications", "disable_parallel_tool_use": True}`.
   - **Do not change** `verification_verdict_tool` choice — the verification path needs `auto` because the model must call `web_search` *first*, then `submit_verification_verdict`. Leave verification untouched.
   - Update the module docstring to reflect the new behavior.

2. **`prompts.py`:**
   - In the system prompt `<output>` block, remove the paragraph that begins with `"Fallback: if for any reason you cannot call the submit_review_findings tool, emit the same payload as JSON wrapped in ``<findings_json>...</findings_json>`` tags."` and the surrounding "Prefer the tool — the fallback is only for cases where the tool call would otherwise be skipped entirely." sentence. The model will always call the forced tool now.

3. **`reviewer.py`:**
   - Delete the tagged-JSON fallback parser around lines 136–180 (`_parse_tagged_json` or similar; check the exact name).
   - In the streaming response handler, replace the fallback path with: if `extract_tool_use_block(response, REVIEW_TOOL_NAME)` returns `None`, return a `ReviewResult` with `error` set to `"Model did not emit submit_review_findings tool call"` and `findings=[]`. Do not attempt to regex-parse text.

4. **`batch.py`:**
   - Same change: remove fallback bracket-finding logic in `retrieve_review_results`. If the tool_use block is absent, mark the spec as `parse_error` in the existing retryable-recovery flow (`_recover_retryable_review_batch_results`). Do not regex-parse text.

5. **`cross_checker.py`:**
   - Same change: remove fallback JSON extraction. If the tool_use block is absent, log a parse error and return zero cross-check findings rather than fabricating them from text.

**Acceptance criteria.**
- All three forced `tool_choice` payloads are `{"type": "tool", "name": "..."}` shape.
- No regex-based JSON extraction from free-form text remains in `reviewer.py`, `batch.py`, or `cross_checker.py`. (Keep `extract_tool_use_block` and the JSON-loads-of-tool-input in `verifier.py` — those are correct uses.)
- The system prompt no longer mentions `<findings_json>` tags or fallback JSON shape.
- Existing tests still pass. (Some tests may have been written *against* the fallback path. If so, update them to assert that absent tool_use blocks produce an error result, not a recovered finding list.)
- Add a test that asserts the request includes a forced `tool_choice` for review and cross-check calls.

**Risks.**
- If the forced `tool_choice` is *also* now incompatible with some specific model-mode combination (e.g., a future release tightens up `tool_choice` + adaptive thinking again), this could regress. **Validate by running one real review against `claude-opus-4-7` before merging** — the SDK will surface a 400 immediately if the combination is rejected.
- The CLAUDE.md mentions the fallback paths exist; update CLAUDE.md to remove those references in this chunk's commit.

**Dependencies.** None. This chunk touches five files but they're cohesive — keep them in one commit.

---

## Chunk 2.3 — Hoist redundant `count_tokens` calls in batch submission

**Files:** `src/batch.py` (`submit_review_batch` near lines 95–110 in v2.11.0).

**Status check before starting:** Read `submit_review_batch`. If the system-prompt token count is already computed outside the per-spec loop, this chunk is done.

**What and why.** Inside the batch submission loop, `count_tokens` is called once per spec on both the system prompt and the user message. The system prompt is identical across all specs in the batch (it depends only on cycle + mode), so the per-loop system count is redundant. A 50-spec batch makes 50 unnecessary `count_tokens` calls.

**What to change.** Move the system-prompt token count outside the per-spec loop. Compute it once at the top of `submit_review_batch` and reuse the value for the budget check on every spec. The user-message count still runs per spec because the user message varies per spec.

**Acceptance criteria.**
- For an N-spec batch, the number of system-prompt `count_tokens` calls drops from N to 1.
- The user-message count is still per-spec.
- The pre-submission budget check still produces correct totals.
- Existing tests still pass.

**Risks.** None significant. The system prompt is intentionally stable; if a future change makes it spec-dependent, the comment should call out that this optimization needs revisiting.

**Dependencies.** None.

---

## Chunk 2.4 — Soften aggressive instruction language in system prompts

**Files:** `src/prompts.py` (especially the safe-edit editability clause around lines 110–127). Also audit the verifier system prompt inside `src/verifier.py` (function `_get_verification_system_prompt` or similar) for the same language patterns.

**Status check before starting:** Search for `MUST`, `CRITICAL:`, `If in doubt`, `ALWAYS`, `NEVER` (in uppercase, as commands to the model) in all prompt-building code. If those have already been softened, this chunk is done.

**What and why.** Per the Opus 4.6/4.7 migration guidance: aggressive imperatives like `CRITICAL: You MUST...` and `If in doubt, use [tool]` were written to overcome the earlier-generation Claude's reluctance. They now over-trigger on 4.6/4.7, which follow the system prompt much more closely. The result is over-conservative outputs (e.g., the safe-edit mode emitting fewer findings than it should because the clause says `existingText ... MUST be copied verbatim`).

**What to change.** Audit the prompt files for the patterns above. Reword imperatives as plain instructions:

- `"existingText ... MUST be copied verbatim"` → `"copy existingText verbatim from a single paragraph in the source spec"`
- `"CRITICAL: never CONFIRM without grounding"` → `"only CONFIRMED or CORRECTED verdicts are valid when web evidence supports them; otherwise mark UNVERIFIED"` (grounding is already enforced in code; the prompt language should match that level of expectation, not amplify it)
- `"If in doubt, choose web_required"` (in `triage.py`) is acceptable here because the entire point is to err toward over-verification. Leave that one.

This is a judgment chunk — the goal is calibration, not stripping all emphasis. The triage prompt (`src/triage.py`) is already well-written; use it as a reference for the right tone.

**Acceptance criteria.**
- The safe-edit editability clause no longer uses `MUST` in all-caps. The semantic intent is preserved.
- Any `CRITICAL:` / `ALWAYS` / `NEVER` in user-facing model instructions (not code comments) is reworded unless there is a documented reason to keep it.
- Existing tests still pass.
- If you have a way to run a real review against a representative spec, do so on Strict, Comprehensive, and Safe-edit modes and confirm finding rates are not dramatically different. If you cannot, commit the change with a note in the commit message that this is a judgment call and rate impact should be monitored.

**Risks.** Real risk of finding-rate change. This chunk is intentionally low priority within Phase 2 for that reason. If your acceptance run shows a dramatic drop or spike in finding counts, revert and add a `REVIEW_FOLLOWUPS.md` note.

**Dependencies.** None.

---

# PHASE 3 — Architectural simplifications

Bigger scope, more judgment required. These reduce code volume and complexity. Each should be approached as: read the existing code thoroughly first, write a 1-paragraph design note in the commit message explaining the chosen approach, then implement.

---

## Chunk 3.1 — Audit and simplify cross-check chunking

**Files:** `src/cross_checker.py` (especially `run_chunked_cross_check` around lines 604–710), `src/tokenizer.py` (for the `CROSS_CHECK_RECOMMENDED_MAX` constant), `src/pipeline.py` (the caller path).

**Status check before starting:** Read `run_chunked_cross_check`. If chunking has already been gated more conservatively (e.g., only triggers above 900k tokens) or removed entirely, this chunk may already be done.

**What and why.** The cross-checker chunks by CSI division (21/22/23/Controls/25+01) whenever the combined input exceeds `CROSS_CHECK_RECOMMENDED_MAX = 822_000` tokens. Opus 4.7 has a 1M-token context window at standard pricing. A typical M&P spec set is 150k–500k tokens; chunking very rarely triggers in practice but adds significant complexity (chunk boundary handling, the cross-discipline synthesis pass on Haiku to recover missed inter-division coordination findings, chunk-deduplication logic). Chunked output is also at risk of double-flagging coordination issues across chunks.

**What to change.** Choose one of two strategies:

- **Strategy A (recommended for most cases).** Raise the chunking threshold to `~950_000` tokens (give 50k headroom under the 1M context window for overhead). Single-pass cross-check becomes the common case. The chunked path remains as a safety net for genuinely enormous projects. The synthesis pass becomes a no-op for single-chunk runs (it already short-circuits when `len(completed_chunks) < 2`).

- **Strategy B (more aggressive).** Remove chunking entirely. If a project exceeds context window, surface a clear error to the user with two options: split the project manually, or run cross-check in two phases by discipline group. Delete `run_chunked_cross_check` and the cross-discipline synthesis pass.

Strategy A is lower risk. Strategy B is the simpler endpoint but loses an edge-case feature.

**Acceptance criteria.**
- Document the chosen strategy in a design-note comment at the top of the relevant function.
- For median-sized projects (a representative test fixture under 500k tokens), cross-check now runs in a single Opus call.
- If Strategy A: a test with a synthetic >950k input still chunks correctly. The synthesis pass still runs when chunks > 1.
- If Strategy B: a test with a synthetic >950k input surfaces a clear error rather than chunking.
- Existing tests still pass (some may need to be updated if they specifically exercise the chunking path with smaller inputs).

**Risks.** Real projects of unusual size could regress. Run on a representative sample if available.

**Dependencies.** None.

---

## Chunk 3.2 — Audit and simplify the edit locator cascade

**Files:** `src/edit_locator.py` (the whole file is in scope, especially `locate_edits` and `_fuzzy_match`). Also `src/prompts.py` (the editability clause may need a sentence added) and `src/structured_schemas.py` (if you add a new finding field).

**Status check before starting:** Read the locator end-to-end. If it has already been simplified to a one-or-two-stage match with fuzzy reserved for explicit manual-review-flagging, this chunk is done.

**What and why.** The locator runs a four-stage cascade: exact → normalized-whitespace → fuzzy SequenceMatcher → section-anchored fuzzy. The prompt (safe-edit mode) already requires the model to quote `existingText` verbatim from a single paragraph. The fuzzy stages are rescuing model output that violates that contract — but rescuing it imperfectly, because fuzzy can silently match similar-but-wrong content in a different section.

**Two strategies — choose one:**

- **Strategy A (low risk, recommended).** Keep the cascade but lower the surface area: drop the fuzzy stages from the "auto-apply" decision tree. Specifically, fuzzy matches always classify as `MANUAL_REVIEW`, never `AUTO_SAFE` or `AUTO_WITH_CAUTION`. The cascade still produces a candidate for the user to review in annotation mode, but auto-application is restricted to exact and normalized matches.

- **Strategy B (cleaner endpoint).** Have the model emit a paragraph anchor index alongside the verbatim quote. Add an optional `paragraphIndex` field to the finding schema. The locator becomes: read the index, verify the quote is verbatim at that paragraph, done. Fuzzy stages become unnecessary and can be deleted. Section-anchored matching becomes unnecessary. Requires a prompt change asking the model to emit the index.

Strategy A is a smaller change and lower risk. Strategy B is the deeper simplification. **Recommended: Strategy A first; queue Strategy B as a separate follow-up chunk if you have time and Strategy A lands cleanly.**

**Acceptance criteria (Strategy A).**
- Any locator result from fuzzy or section-anchored stages has `safety_category != AUTO_SAFE` and `safety_category != AUTO_WITH_CAUTION`.
- Existing tests still pass. Tests that previously asserted fuzzy matches were auto-applied need updating to reflect the new policy.
- Add a test that confirms a fuzzy-only match is not in `build_edit_actions` output when `allow_caution=True`.

**Acceptance criteria (Strategy B, if chosen).**
- The finding schema accepts an optional `paragraphIndex` integer.
- The system prompt asks the model to emit the index.
- The locator uses the index when present; falls back to exact/normalized match when absent.
- Fuzzy and section-anchored code paths are deleted.
- Existing tests still pass.

**Risks.** Existing user workflows may depend on fuzzy auto-application. Document the policy change clearly in the commit message and in CLAUDE.md.

**Dependencies.** None for Strategy A. Strategy B benefits from Chunk 2.4 (the prompt-softening pass) being complete first, so the system prompt isn't growing while you change it.

---

## Chunk 3.3 — Preprocessor disposition decision

**Files:** `src/preprocessor.py`, `src/prompts.py` (to potentially feed preprocessor output into the LLM prompt), `src/pipeline.py` (caller path), `src/gui.py` (display path).

**Status check before starting:** Read `preprocessor.py` end-to-end and trace how its output flows to the report and to the LLM. The current state (per the v2.11.0 review): preprocessor output goes into the report alongside LLM findings, and is *not* fed into the LLM prompt. If that has already changed, this chunk is done.

**What and why.** The preprocessor runs detectors (LEED, placeholders, stale-cycle, structural alerts) before any LLM call. Its output is displayed in the report alongside LLM findings. The LLM is then asked to review the same content and will redetect the same patterns. This is duplicated work and produces duplicate alerts.

**Three options — pick one and justify in the commit message.**

- **Option A (recommended). Feed preprocessor output into the LLM system prompt.** Add a `<pre_detected>` block in `get_single_spec_user_message` containing the preprocessor alerts. Tell the model "these patterns have already been detected — do not re-emit them as findings; your job is to find issues beyond these." Cleaner reports; saves output tokens.
- **Option B. Keep preprocessor as instant-feedback only.** Display preprocessor alerts in the GUI while the LLM call is pending, but exclude them from the final report. The LLM is the source of truth for the report.
- **Option C. Delete the preprocessor entirely.** The LLM will catch LEED/placeholder/stale-cycle issues better.

A is the most code-light improvement. C is the largest simplification. B requires the least change but keeps the preprocessor as a non-load-bearing UX optimization.

**Acceptance criteria.**
- Decision is documented in the commit message and (if A or B) in a comment in `preprocessor.py`.
- If A: the user message contains a `<pre_detected>` block when preprocessor alerts exist; the LLM is told not to re-emit them; the report shows alerts only from the LLM findings, with preprocessor alerts visible as a separate "Pre-detected" section that doesn't duplicate finding entries.
- If B: preprocessor alerts visible in the GUI in the pre-LLM phase; absent from the final report.
- If C: `preprocessor.py` is deleted (or reduced to a thin shim); callers are updated.
- Existing tests still pass. Tests for the preprocessor's specific detectors may be removed if going with C.

**Risks.** This changes report content. Confirm with the user before going with C. A is the safest choice if unsure.

**Dependencies.** None.

---

## Chunk 3.4 — Address verification-cache key documentation gap

**Files:** `src/verification_cache.py` (the `make_cache_key` function docstring around lines 73–79).

**Status check before starting:** Read the function and its docstring. If the docstring already documents the model-omission and provides a manual-clear instruction, this chunk is done.

**What and why.** The cache key is `cycle_label | actionType | codeReference | sha256(claim_summary)`. **The verifier model is intentionally absent from the key** — meaning if a user switches verification models (e.g., flipping `SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT=0` to revert to Opus-everywhere), cached Sonnet verdicts are still served. This is correct behavior for normal use (model choice should not invalidate grounded verdicts), but it is non-obvious and worth documenting.

**What to change.** Expand the `make_cache_key` docstring to explicitly state: model is not part of the key by design; switching the verifier model does not invalidate the cache; users wanting a fresh re-verification should clear the cache file at `~/.spec_critic/verification_cache.json` (or whatever `SPEC_CRITIC_CACHE_PATH` resolves to).

Also document the same point in CLAUDE.md under the verification cache section.

**Acceptance criteria.**
- The docstring contains the documented behavior.
- CLAUDE.md mentions the model-omission policy.
- Existing tests still pass (no functional change).

**Risks.** None — documentation only.

**Dependencies.** None.

---

# PHASE 4 — Optional design explorations

These are larger-scope or exploratory items. Treat each as a research-and-prototype chunk: investigate, write a design note, decide whether to proceed. **Do not implement these without explicit go-ahead from the human reviewer.** The output of each is a design note (saved to `docs/design-notes/<chunk-id>.md`) and, optionally, a prototype branch.

---

## Chunk 4.1 — Investigate `messages.parse()` for verification verdicts

**Goal.** Verification verdicts are a canonical structured-output use case (one of four enums + explanation + sources). Currently they go through tool-use (`submit_verification_verdict`). The cleaner Anthropic SDK idiom is `client.messages.parse()` with a Pydantic `BaseModel`, which validates the response against the schema automatically.

**What to investigate.**
- Whether `messages.parse()` is compatible with the web_search server tool (the verifier needs both `web_search` AND structured output).
- Whether `messages.parse()` supports streaming (the verifier requires streaming because of the server tool).
- Whether `output_config.format` (the underlying API parameter) can coexist with `tools=[web_search_tool]` in a single call.

**Deliverable.** A design note in `docs/design-notes/4.1-messages-parse-verification.md` covering: compatibility, expected code simplification (line count delta), tradeoffs, and a go/no-go recommendation.

**Acceptance criteria.** Design note exists; no production code changes.

**Risks.** None — investigation only.

---

## Chunk 4.2 — Investigate Programmatic Tool Calling for verification

**Goal.** Verification calls `web_search` 3–5 times per finding, with intermediate search results landing in the context window. Programmatic Tool Calling (PTC) lets the model write a script that calls `web_search` and filters results before they reach context. For multi-finding batches against the same code-cycle questions, this could dramatically reduce token spend.

**What to investigate.**
- Whether PTC is available on Opus 4.7 (or which model is required).
- Whether PTC supports verifying *multiple* findings in one PTC call against a shared search corpus.
- Token cost comparison: estimate cost-per-finding under current approach vs. PTC approach.

**Deliverable.** A design note in `docs/design-notes/4.2-ptc-verification.md`. Include estimated savings, implementation complexity, and a recommendation.

**Acceptance criteria.** Design note exists.

**Risks.** None — investigation only.

---

## Chunk 4.3 — Add telemetry for escalation success rate

**Files:** `src/verifier.py`, `src/diagnostics.py`.

**Goal.** Track how often Sonnet → Opus escalation actually changes a verdict (Sonnet UNVERIFIED → Opus CONFIRMED/CORRECTED). If the success rate is low, escalation may be cost-without-value; if high, it's load-bearing.

**What to change.**
- In `verifier.py`, when escalation runs, record `escalation_attempted` and `escalation_changed_verdict` flags on the `VerificationResult`.
- In `diagnostics.py`, roll these into a section under `verification_evidence`: `escalation_attempts`, `escalation_verdict_changes`, `escalation_success_rate`.
- Surface the rate in the `DiagnosticsWindow`.

**Acceptance criteria.**
- Diagnostics report shows escalation telemetry.
- Tests cover the new fields.
- Existing tests still pass.

**Risks.** None significant.

**Dependencies.** None.

---

## Chunk 4.4 — Investigate compaction for cross-check

**Goal.** Server-side compaction (`context_management.edits: [{type: "compact_20260112"}]`, requires beta header `compact-2026-01-12`) summarizes earlier context as you approach the context window limit. This could replace cross-check chunking entirely for very large projects.

**What to investigate.**
- Compatibility with the existing tool-use flow.
- Cost model (compaction has its own pricing).
- Whether the resulting summary is useful for cross-spec coordination (which depends on fine-grained text comparison, not summary).

**Deliverable.** A design note in `docs/design-notes/4.4-compaction-cross-check.md`. Include a go/no-go recommendation.

**Acceptance criteria.** Design note exists.

**Risks.** Compaction is lossy. Cross-check by nature requires seeing all original text; this may not be a fit. Investigation should answer that clearly.

---

# PHASE 5 — Cleanup and verification

---

## Chunk 5.1 — Update CLAUDE.md to reflect Phase 1–3 changes

**Files:** `CLAUDE.md`.

**Status check before starting:** This chunk runs *after* the relevant Phase 1–3 chunks land. Skip until those are merged. If CLAUDE.md is already up to date with the Phase 1–3 changes, this chunk is done.

**What to change.** Walk through `CLAUDE.md` section by section. Update any mention of the items that changed:

- `<output>` block fallback paragraph (Chunk 2.2) — remove mentions of `<findings_json>` fallback.
- Tool-choice strategy (Chunk 2.2) — update the description of how the structured output is enforced.
- Cross-check chunking threshold (Chunk 3.1) — update the threshold and behavior.
- Preprocessor disposition (Chunk 3.3) — update the description if A/B/C was chosen.
- Locator cascade (Chunk 3.2) — update the description of safety categories.
- Verification cache model-omission policy (Chunk 3.4) — add the documented policy.
- Effort parameter (Chunk 2.1) — add to the feature-flag table.

**Acceptance criteria.**
- CLAUDE.md reflects the current state of the source.
- The "Source layout" and high-level flow sections are accurate.

**Risks.** None.

**Dependencies.** All Phase 1–3 chunks must be merged before this is meaningful.

---

## Chunk 5.2 — Full test sweep + integration smoke test

**Goal.** Confirm the cumulative effect of Phase 1–3 changes hasn't regressed any user-facing behavior.

**What to do.**
- Run the full pytest suite. All tests must pass.
- If the repo has any end-to-end fixtures (sample .docx files in `tests/fixtures/` or similar), run a full review pipeline against one or two of them and confirm finding shape, count rough-order-of-magnitude, and report generation all complete without errors.
- Compare verification cache hit rate before vs. after (cache should be unchanged unless Chunk 3.4 or Chunk 4.3 added new fields).
- Verify the GUI launches and a small batch can be submitted (manual smoke test — does not require automated coverage if the test suite doesn't have GUI tests).

**Acceptance criteria.**
- All tests pass.
- Smoke test against a representative fixture produces a non-empty `PipelineResult` with findings on a known-buggy spec.
- No new error logs from the GUI launch.

**Risks.** None.

**Dependencies.** All other chunks intended for this release.

---

# Out of scope (do not do)

The following came up in the review and are explicitly **not in scope** for this round:

- Rewriting `report_exporter.py` (879 lines) using Word styles or replacing it with code-execution server tool. Big effort, low risk, deferred.
- Replacing the polling logic in `batch_runtime.py` with a different polling strategy. Current is fine; keep it.
- Migrating from the manual agentic loop in `verifier.py` to the SDK tool runner. Investigation only would be appropriate; deferred to Phase 4 if added.
- Cross-checking the LLM-emitted analysis_summary against the findings array for consistency. Cosmetic.
- Replacing the four-category edit safety enum with a three-category one (Chunk 3.2 already handles the substantive part).

If any of these come up as "easy wins" while working other chunks, note them in `REVIEW_FOLLOWUPS.md` and move on.

---

# Suggested chunk ordering

For a team of parallel agents, this is a sensible distribution. None of the chunks have hard dependencies on each other within Phase 1–2, so they can run concurrently on separate worktrees if desired.

**Wave A (parallel, low risk):** 1.1, 1.2, 1.5, 1.6, 2.3, 3.4
**Wave B (parallel, medium risk):** 1.3, 1.4, 2.1, 2.4
**Wave C (cohesive larger change):** 2.2 (single agent, single commit, all five files together)
**Wave D (judgment-heavy):** 3.1, 3.2, 3.3
**Wave E (optional explorations):** 4.1, 4.2, 4.3, 4.4
**Wave F (cleanup):** 5.1, 5.2

Each wave can land independently. Push to `claude/code-review-ZanKq` after each chunk passes acceptance.

---

# When in doubt

- Read the code before the plan.
- Trust the code when they disagree.
- Note unexpected discoveries in `REVIEW_FOLLOWUPS.md`.
- Do one chunk per commit; reference the chunk ID in the message.
- Run tests before and after.
- Do not skip hooks or pass `--no-verify`.
- If a chunk's scope is unclear, stop and ask the human reviewer rather than making it up.
