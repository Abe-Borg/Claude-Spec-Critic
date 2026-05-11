# D9.1 — Structured Outputs (`output_config.format` / `messages.parse()`) for Verification

**Type:** Research only — no production code changes.
**Status:** Recommendation below.
**Scope:** Whether Spec Critic's `verifier.py` verdict path should migrate from a custom `submit_verification_verdict` tool to Anthropic's structured outputs (`output_config.format` / `client.messages.parse()`).
**Prepared:** Phase D9 (Wave 4).

---

## 1. What we have today

Verification currently runs as a single streaming `messages.stream` call per finding (or one batch request per finding for the batch path) with two tools attached:

1. `web_search_20260209` (server tool, severity- and profile-tiered `max_uses`).
2. `submit_verification_verdict` (custom tool, schema in `src/structured_schemas.py`).

The system prompt instructs the model to call `web_search` first, then call the verdict tool exactly once as its terminal action. `tool_choice` is `{"type": "auto"}` because forcing tool_choice (`{"type": "tool", ...}` or `{"type": "any"}`) is incompatible with adaptive thinking on the verifier (see `structured_schemas._strict_enabled` docstring and the project's rejected-change note R0.1).

Response parsing lives in `verifier.parse_verification_response`. It prefers the `submit_verification_verdict` tool input and falls back to a `<verdict_json>`-style text parser. The schema (`structured_schemas.VERIFICATION_VERDICT_SCHEMA`) requires `verdict`, `explanation`, `sources`, and an optional `correction`.

Citations from `web_search` already flow through `_collect_search_evidence_detailed`. Chunk H partitions citations into `searched_sources` / `cited_sources` / `accepted_sources` / `rejected_sources` and downgrades `CONFIRMED` / `CORRECTED` to `UNVERIFIED` when every citation is ungrounded. That helper consumes `web_search_result` and `web_search_tool_result_error` blocks from the assistant content list directly.

---

## 2. The hypothesis worth checking

The verdict tool is effectively a hand-rolled structured output: a fixed schema, one terminal invocation, and a parser that walks tool-use blocks. Anthropic's GA structured outputs (`output_config.format` with a JSON schema, or the SDK helper `client.messages.parse()`) provide the same guarantee with less custom code:

- The model is forced to emit a JSON object matching the schema.
- The SDK validates and (with `parse()`) deserializes into a Pydantic / typed object.
- The fallback text parser (`<verdict_json>...</verdict_json>`) becomes unnecessary.

Before migrating, three compatibility questions must be answered.

---

## 3. Question 1 — Does `output_config.format` coexist with `web_search` in the current API shape?

**Short answer: probably not for the same request, based on documented compatibility.**

The structured outputs documentation (fetched 2026-05-11 from `platform.claude.com/docs/en/build-with-claude/structured-outputs`) lists the following compatible features explicitly:

- Batch processing
- Token counting
- Streaming
- Agentic workflows with user-defined tools

The page does **not** list `web_search` (or any other server-side tool) as compatible. The only tool examples shown use `strict: true` on a user-defined tool. The compatibility section is implicitly an allow-list, and an allow-list that does not name `web_search` is the strongest signal we have that the combination is unsupported.

The closer the page got to discussing tools at all was:

> JSON outputs and strict tool use solve different problems and work together:
> - JSON outputs control Claude's response format (what Claude says)
> - Strict tool use validates tool parameters (how Claude calls your functions)

That sentence is about user-defined tools with `strict: true`. It is not about server tools, and server tools (`web_search`, `code_execution`, `web_fetch`) have their own response-shape contracts (the assistant content stream interleaves `server_tool_use`, `web_search_tool_result`, `text` with attached `citations` blocks). Those contracts conflict with the structured-outputs contract, which says the response is constrained to a single JSON document matching the schema.

**Independent confirmation from the web_search docs.** The `web_search_20260209` reference says citations are *always* enabled and that text blocks emitted at the end of a search turn carry `citations` arrays referencing `web_search_result_location` entries. That output shape cannot be expressed as a single schema-conformant JSON document — the citations live next to the text, not inside the JSON payload.

**Bottom line: assume `output_config.format` and `web_search` are mutually exclusive on the same request, pending Anthropic adding the combination to the supported list.**

---

## 4. Question 2 — Does web_search always imply citations that conflict with structured JSON outputs?

**Yes.** Per the web_search reference, citations are not optional — every text block emitted after a search includes a `citations` array. The SDK exposes these as Pydantic objects on the text block, and the Chunk H grounding gate depends on the `web_search_tool_result` blocks (not on a single JSON payload) to compute `searched_sources` and `accepted_sources`.

Structured JSON outputs, by contrast, constrain the model to emit a single JSON document and discard whatever ambient text or citations would otherwise be produced. The skill notes that "Incompatible with: Citations (returns 400 error), message prefilling" for structured outputs on supported models.

So in addition to the implicit allow-list signal in §3, there is an explicit "citations are incompatible with structured outputs" statement that rules out the combination for any verification path that needs grounding.

This matters because the *whole point* of Spec Critic's verifier is grounded verdicts. The grounding invariant in `verifier._enforce_grounding_invariant` requires `CONFIRMED` / `CORRECTED` verdicts to have at least one successful web_search block. Take citations away and the grounding gate has nothing to validate against.

---

## 5. Question 3 — Does `client.messages.parse()` support the required streaming/server-tool workflow?

**Streaming: documented, but messy.** The structured outputs page says streaming works with structured outputs, but the per-language example uses Java's `MessageAccumulator` to assemble JSON fragments. The Python `messages.parse()` helper is not explicitly documented as streaming-aware. For Spec Critic that's a separate question from compatibility — the verifier uses `client.messages.stream(...).get_final_message()` and would have to switch to the lower-level `parse()` shape.

**Server tools: not supported.** Same allow-list argument as §3 — server tools are not listed as compatible with `parse()` or `output_config.format`.

**Net effect: `messages.parse()` is not a drop-in for the current verifier.** It would only fit a verification path that does *not* call `web_search` — i.e. the local-skip / non-web verification path.

---

## 6. Question 4 — Would structured outputs replace `submit_verification_verdict`, or only some non-web verification path?

**Only a non-web verification path** is realistic. Specifically, the `STRICT_STRUCTURED` mode in `verification_modes.py` is the natural fit:

- It targets GRIPES severity findings *and* non-GRIPES `internal_coordination` findings (placeholders, LEED tags, internal contradictions, typos, duplicate paragraphs).
- These are issues the model can verify from the finding text itself, without external sources.
- The mode policy currently still runs Sonnet + thinking-off + half-budget web_search. If structured outputs replace the verdict tool *and* web_search is removed for this mode, the request becomes a pure constrained-JSON call — fastest and most reliable shape.

For everything else (`STANDARD_REASONING`, `DEEP_REASONING`), the verifier needs web_search and therefore needs the current tool-use shape.

**Concrete migration sketch (do not implement):**

```python
# verifier._run_verification_call, STRICT_STRUCTURED branch (proposed)
if mode == VerificationMode.STRICT_STRUCTURED:
    response = client.messages.create(
        model=model,
        max_tokens=output_limit,
        system=system_payload,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": VERIFICATION_VERDICT_SCHEMA}},
    )
    # parse_verification_response handles the single JSON text block
```

Notes on that sketch:

- The verdict schema is already strict-mode-compatible (every property required; optional values nullable; no `oneOf`/`anyOf`). It can be reused as the `json_schema`.
- The grounding gate (`_enforce_grounding_invariant`) must be relaxed for this mode because no web_search will occur. The `local_skip` flow already does this — `STRICT_STRUCTURED` would join it. The verification mode would need to carry an explicit `requires_grounding: bool` flag (currently grounding is implied by whether the mode runs web_search). This is a small, contained change but it does cross the verification-mode policy surface, so it cannot be done without a separate design decision.
- The verification cache key (`VerificationCache.make_cache_key`) does not need to change — it intentionally omits the verifier model.
- The verdict tool itself can stay defined for `STANDARD_REASONING` / `DEEP_REASONING`. The two paths would coexist.

---

## 7. Question 5 — What would happen to source/citation capture?

Under the proposed STRICT_STRUCTURED-only migration: **no change for that mode** because there are no sources to capture in the first place. Internal-coordination verdicts cite the finding's own quoted text, not external URLs. `VerificationResult.accepted_sources` already supports an empty list, and the report already renders "no external sources" gracefully.

For `STANDARD_REASONING` / `DEEP_REASONING`: **no change at all** because those paths would not move to structured outputs.

---

## 8. Question 6 — What tests would prove compatibility?

If implementation is ever authorized, the prove-it tests are:

1. **Schema fidelity round-trip.** A unit test that constructs the `VERIFICATION_VERDICT_SCHEMA` JSON schema, sends a stub prompt through a `FakeClient`, asserts the request payload contains `output_config.format` (not a `tools=[...verification_verdict_tool()...]`), and asserts the parsed response matches the existing `VerificationResult` shape. This pattern is already supported by `tests/test_request_payload_shape.py`.
2. **Web-search incompatibility regression.** A unit test that asserts requests *with* web_search never include `output_config.format`, even when STRICT_STRUCTURED would otherwise enable it. This locks in the §3 / §4 finding so a future refactor cannot accidentally combine them.
3. **Grounding-gate bypass.** A test that asserts STRICT_STRUCTURED verdicts skip `_enforce_grounding_invariant` and are not downgraded to `UNVERIFIED` for lack of sources.
4. **Live smoke test.** One real Anthropic call per supported model (Opus 4.7, Sonnet 4.6, Haiku 4.5) confirming the request actually succeeds. The skill notes that `output_config.format` is supported on Opus 4.7 / Sonnet 4.6 / Haiku 4.5 (and prior 4.x), so all three of Spec Critic's relevant models are eligible. Live tests should be marked `@pytest.mark.network` per the existing harness convention.

---

## 9. Risks if implemented

- **Mode-policy drift.** Adding a `requires_grounding` flag to `ModePolicy` is a real cross-cutting change. It is small but it lands in code paths that have already been audited heavily during Phases D1–D8.
- **Schema drift.** `VERIFICATION_VERDICT_SCHEMA` would then be load-bearing for both `output_config.format` *and* the verdict tool. Changes to the schema would need to be tested in both shapes.
- **Operator confusion.** Two request shapes for verification (`output_config.format` for STRICT_STRUCTURED, tool-use for everything else) is more surface than one. The diagnostics page would need to show which shape produced a given verdict (mirrors the existing `parse_status` field).
- **Anthropic API drift.** If Anthropic later announces structured outputs + web_search co-compatibility (e.g. the citations issue is resolved), the right migration moves from "STRICT_STRUCTURED only" to "everything except DEEP_REASONING" or similar. The design note should be revisited at that point.
- **No clear win on top-line cost.** STRICT_STRUCTURED already runs Sonnet with thinking off and half web_search budget. Switching to constrained JSON would save the ~hundreds of tokens spent on the tool-use envelope and the prompt's "call submit_verification_verdict" instructions — meaningful but not transformational. The bigger win would be reliability (no `parse_status="text_parse_error"` fallback path), not cost.

---

## 10. Recommendation

**Recommendation: only use for non-web verification (STRICT_STRUCTURED mode), and revisit after API changes.**

Rationale: Anthropic's current documentation gives every signal that `output_config.format` is incompatible with `web_search` and with citations — the structured outputs page's compatibility section omits server tools, and citations are explicitly listed as incompatible. The verifier's core grounded-verdict path needs both `web_search` and citations, so it cannot move now without losing grounding.

A migration confined to STRICT_STRUCTURED is technically clean (the schema is already strict-compatible, the mode is already non-web-grounded in spirit) but adds policy surface and a second request shape. The expected gain is "no more text-fallback parse errors" for the ~10–20% of internal-coordination findings that take this path. That is real but not load-bearing.

**Concrete action items if this is later authorized:**

1. Run the four tests in §8 against the current `claude-sonnet-4-6` and `claude-haiku-4-5` model IDs.
2. Add a `requires_grounding: bool` field to `ModePolicy`; default True for every existing mode.
3. Branch `_run_verification_call` on the new flag.
4. Keep `submit_verification_verdict` defined and used by the other modes.
5. Leave the verification cache schema unchanged.

**No prototype now.** Revisit when either:

- Anthropic announces structured outputs + `web_search` + citations co-compatibility, or
- The diagnostics report starts showing a material `text_parse_error` rate from `parse_verification_response` on STRICT_STRUCTURED runs.

---

## 11. References used

- Spec Critic source: `src/verifier.py` (verification loop, `parse_verification_response`, `_enforce_grounding_invariant`), `src/structured_schemas.py` (`verification_verdict_tool`, `VERIFICATION_VERDICT_SCHEMA`, `_strict_enabled`), `src/batch.py` (`build_verification_tools_for_profile`), `src/verification_modes.py` (`VerificationMode`, `mode_policy`), `src/api_config.py` (`WEB_SEARCH_TOOL`, `web_search_max_uses_for_severity`).
- Anthropic documentation (fetched 2026-05-11):
  - `platform.claude.com/docs/en/build-with-claude/structured-outputs.md` (compatibility allow-list, model support, citations incompatibility).
  - `platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool` (citations always-on, `web_search_20260209` shape).
- Delta plan: Chunk D9.1 in `1e4f5a14-spec_critic_delta_plan_from_second_agent.md`.
- Rejected change R0.1 in the same delta plan (forced tool_choice with thinking enabled — incompatible, do not implement).
