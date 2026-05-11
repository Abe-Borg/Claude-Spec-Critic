# Chunk C Implementation Notes

## Goal

Make every verification request path agree about which tools the model has
access to. Before Chunk C, three of the four verification request builders
told Claude (via the system prompt) that it had `submit_verification_verdict`
available, but only attached `web_search` to the request payload — the
structured verdict tool was unreachable on those paths and every verdict
silently fell back to JSON-text parsing.

## What was already in place

- `batch._build_verification_request_params` (the **batch initial**
  verification builder) was the only path that already attached the
  verdict tool when `structured_outputs_enabled()` returned True.
- `structured_schemas.verification_verdict_tool()` and the `_strict_enabled`
  toggle were already authored.
- Chunk A request-shape tests already captured kwargs for retry and
  continuation request builders, with two `xfail` markers explicitly
  flagging the Chunk C bug for fix-up.

## Headline bug fixed

The verifier system prompt unconditionally claimed:

> The available tools are `web_search` (server-side) and
> `submit_verification_verdict` (the structured verdict tool).

But:

- `verifier._run_verification_call` (real-time streaming verification)
  built `tools_payload = tools_with_cache([severity_tool])` — verdict tool
  missing.
- `verifier._build_retry_request` (batch retry wave) built
  `tools_with_cache([web_tool])` — verdict tool missing.
- `verifier._build_continuation_request` (batch continuation wave) built
  `tools_with_cache([web_tool])` — verdict tool missing.

So three out of four verification paths advertised a tool to the model
that wasn't in the request payload. The model couldn't call the verdict
tool, the strict-schema verdict object never arrived, and every verdict
on those paths went through fragile fallback parsing in
`_parse_verification_response`.

## What this chunk added

1. **`src/batch.py`** — two new public helpers:
   - `verification_request_includes_verdict_tool()` — single source of
     truth for whether the verdict tool will be attached. Wraps
     `structured_outputs_enabled()` with a verification-specific name so
     the prompt builder and the request builder can call the same helper
     instead of duplicating the env-var check.
   - `build_verification_tools(severity)` — single source of truth for
     the verification tool list. Returns `[web_search]` (severity-tiered
     `max_uses`) plus `[submit_verification_verdict]` when structured
     outputs are enabled. Cache controls are NOT applied here so callers
     can wrap with `tools_with_cache(...)` themselves.

2. **`src/batch.py`** — `_build_verification_request_params` now routes
   through `build_verification_tools(severity)` instead of duplicating
   the `[web_tool, ?verdict_tool]` construction. The hoist also moved the
   `verification_verdict_tool` import to module level (was lazy-imported
   inside the function).

3. **`src/verifier.py`** —
   - `_get_verification_system_prompt(cycle, *, include_verdict_tool=None)`
     now branches the Tool usage section on the flag. When False, the
     prompt advertises only `web_search` and instructs the model to emit
     a JSON object. Defaults to `verification_request_includes_verdict_tool()`.
   - `_build_verification_prompt(finding, *, cycle, include_verdict_tool=None)`
     similarly branches the intro line ("call submit_verification_verdict
     exactly once" vs "emit the verdict as a JSON object"). Same default.
   - `_run_verification_call` (real-time) now computes
     `include_verdict_tool` once and threads it into both prompt builders
     and the tool list (built via `build_verification_tools`).
   - `_build_retry_request` and `_build_continuation_request` route
     through the shared helpers.
   - `start_verification_batch` computes `include_verdict_tool` once and
     wraps both `build_prompt_fn` and `system_prompt_fn` lambdas so the
     batch-initial path matches the same flag as the real-time path.
   - Cleaned up now-unused imports (`web_search_tool_for_severity`,
     `web_search_max_uses_for_severity`, `WEB_SEARCH_TOOL`).

4. **`tests/test_request_payload_shape.py`** — removed the two `xfail`
   markers (the bugs they flagged are fixed) and added 15 new tests in
   `TestVerificationToolPayloadConsistency`:

   - `build_verification_tools` includes/omits the verdict tool based on
     the env flag (and uses severity-tiered `max_uses`).
   - Real-time `verify_finding` includes the verdict tool by default; omits
     it when `SPEC_CRITIC_STRUCTURED_OUTPUTS=0`.
   - `_build_retry_request` and `_build_continuation_request` omit the
     verdict tool when structured outputs are disabled.
   - System prompt mentions `submit_verification_verdict` iff
     `include_verdict_tool=True`. Same for the user prompt.
   - Three "prompt and tools agree" tests over the batch-initial, retry,
     and continuation paths — the prompt mentions the verdict tool iff
     it's actually in the request payload.
   - Repo-wide guard `test_no_inline_web_search_tool_construction_in_verifier`
     fails if any future change in `verifier.py` hand-rolls
     `web_search_tool_for_severity(...)` instead of going through
     `batch.build_verification_tools` — re-introducing the Chunk C bug
     would trip this.

## Tradeoffs and decisions

- **Helper lives in `batch.py`, not a new module.** `verifier.py` already
  imports `BatchJob`, `submit_verification_batch`,
  `retrieve_verification_results_detailed`, `submit_verification_followup_wave`,
  and `_extract_api_error_message` from `batch.py`. Adding two more
  imports keeps the dependency direction the same and avoids creating a
  third module just for two helpers. The helper is named
  `build_verification_tools` (no leading underscore) to make it clear
  this is the public surface other modules should call.

- **Caller threads `include_verdict_tool`, doesn't infer it from the
  tools list.** Each verification request builder explicitly computes
  `include_verdict_tool = verification_request_includes_verdict_tool()`
  and passes it to both the prompt builder and (implicitly via the
  helper) the tool list. The alternative — inferring the flag by
  inspecting the tools list — is more indirect and breaks if a future
  change adds another tool to the payload.

- **Default-on `include_verdict_tool=None` in the prompt builders.**
  `_get_verification_system_prompt` and `_build_verification_prompt`
  default `include_verdict_tool` to `None`, which then resolves to
  `verification_request_includes_verdict_tool()`. This keeps backward
  compatibility for any callers (production or test) that don't pass
  the flag explicitly. The verifier's three production callers all pass
  it explicitly so the connection is visible at the call site.

- **Fallback parsing preserved.** The plan directive 8 explicitly says
  fallback parsing must remain because thinking-enabled tool use cannot
  force the model to call the verdict tool. `_parse_verification_response`
  and the text-fallback branch in `_verdict_from_tool_use`-then-text
  selection in `_run_verification_call` and `_classify_wave_results` are
  untouched.

- **No new env flag.** The plan directive 4 implies the gating should
  follow `structured_outputs_enabled()`, which is the existing single
  toggle. Adding a separate "include verdict tool in verification" flag
  would multiply the configuration surface with no operational benefit.

- **Cache control behavior preserved.** `tools_with_cache(...)` still
  pins the cache breakpoint on the last tool. Before Chunk C the last
  tool on the verifier real-time/retry/continuation paths was the
  web_search tool (the only tool); now it's the verdict tool when
  structured outputs are enabled, which matches the batch-initial path.
  The web_search prefix is shared across severity tiers, so per-severity
  budgets still rotate cache prefixes the way Phase 10 designed them.

- **System prompt change invalidates existing prompt cache prefix.**
  The Tool usage section is now conditional on `include_verdict_tool`.
  The default branch (structured outputs on) is byte-identical to the
  pre-Chunk-C wording, so cached prefixes from earlier runs continue to
  hit. The `include_verdict_tool=False` branch is a different cache
  prefix — that's correct, those are different prompts.

## Acceptance criteria coverage

| Plan acceptance criterion | Where covered |
| --- | --- |
| Every verification path that expects structured verdicts includes the verdict tool | `test_realtime_verification_includes_verdict_tool`, `test_retry_request_includes_verdict_tool_by_default`, `test_continuation_request_includes_verdict_tool_by_default`, `TestBatchVerificationRequestShape::test_request_carries_verdict_tool_when_structured_outputs_enabled` (existing) |
| No verification prompt claims access to a tool that is missing from the request payload | `test_system_prompt_omits_verdict_tool_when_excluded`, `test_user_prompt_omits_verdict_tool_when_excluded`, `test_batch_initial_prompt_and_tools_agree`, `test_retry_request_prompt_and_tools_agree`, `test_continuation_request_prompt_and_tools_agree` |
| Batch, real-time, retry, and continuation paths are consistent | All three "prompt and tools agree" tests + `test_realtime_verification_includes_verdict_tool` + the existing batch-initial test in `TestBatchVerificationRequestShape` |
| Request-shape tests fail if future code drops the verdict tool | `test_no_inline_web_search_tool_construction_in_verifier` (repo-wide guard) + the per-path inclusion tests above |

## Deferred / out of scope

- Stop reason / parser unification across paths (Chunk D's job).
  Chunk C only fixes the request side; the canonical structured-verdict
  parser already accepts both `end_turn` and `tool_use` stop reasons.
- Verification routing modes (Chunk I's job). Chunk C does not change
  which model handles which finding, only the tool payload shape.
- Source grounding policy (Chunk H's job). Chunk C preserves the
  existing `_collect_search_evidence` / grounding-invariant behavior
  unchanged.

## How to verify

```
pytest -q                                                     # full suite — 387 pass
pytest tests/test_request_payload_shape.py::TestVerificationToolPayloadConsistency  # 15 new Chunk C tests
pytest tests/test_request_payload_shape.py::TestVerifierRetryAndContinuationShape   # 3 pass; xfail markers removed
```
