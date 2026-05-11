# Chunk B Implementation Notes

## Goal

Centralize model capability decisions so unsupported request parameters
(most importantly `thinking`) never reach the Anthropic API. Replace the
eight hard-coded `thinking={"type": "adaptive"}` literals scattered across
`reviewer.py`, `batch.py`, `cross_checker.py`, and `verifier.py` with one
policy helper.

## What was already in place

- `MODEL_OPUS_46/47`, `MODEL_SONNET_46`, `MODEL_HAIKU_45` already lived in
  `api_config.py`.
- Output-token caps (`MAX_OUTPUT_TOKENS_OPUS/SONNET/HAIKU`) already keyed
  off the same constants.
- `triage.py` already omitted `thinking` (uses Haiku 4.5).
- Chunk A request-shape tests already captured kwargs for every relevant
  request path, so this chunk's regressions land in the existing
  `tests/test_request_payload_shape.py` framework.

## Headline bug fixed

`cross_checker._run_cross_discipline_synthesis` defaulted to
`SYNTHESIS_MODEL_DEFAULT` (Haiku 4.5) but unconditionally sent
`thinking={"type": "adaptive"}`. Anthropic rejects that combination — the
synthesis pass would fail on every chunked cross-check until someone
overrode the synthesis model to Opus/Sonnet via env.

## What this chunk added

1. **`src/api_config.py`** — `ModelCapabilities` frozen dataclass,
   `_MODEL_CAPABILITIES` whitelist registry (4 known models), safe-default
   `_DEFAULT_CAPABILITIES` for unknown models, `model_capabilities()`,
   `model_supports_adaptive_thinking()`, `thinking_config_for()`, and
   `apply_thinking_config()`. Eight phase identifier constants
   (`PHASE_REVIEW`, `PHASE_BATCH_REVIEW`, `PHASE_CROSS_CHECK`,
   `PHASE_SYNTHESIS`, `PHASE_VERIFICATION`, `PHASE_VERIFICATION_RETRY`,
   `PHASE_VERIFICATION_CONTINUATION`, `PHASE_TRIAGE`) plus the opt-out set
   `_PHASES_NO_THINKING` (currently `{triage}`).

2. **`src/reviewer.py`** — `_stream_review` now calls
   `apply_thinking_config(..., phase=PHASE_REVIEW)`.

3. **`src/batch.py`** — `submit_review_batch` (batch review) and
   `_build_verification_request_params` (batch verification) thread through
   `apply_thinking_config` with `PHASE_BATCH_REVIEW` /
   `PHASE_VERIFICATION`.

4. **`src/cross_checker.py`** — `run_cross_check` (cross-check pass) and
   `_run_cross_discipline_synthesis` (synthesis pass) thread through
   `apply_thinking_config` with `PHASE_CROSS_CHECK` / `PHASE_SYNTHESIS`.
   The synthesis call now correctly omits `thinking` on the Haiku default
   and adds it back if an operator overrides to Opus/Sonnet.

5. **`src/verifier.py`** — `_run_verification_call` (real-time streaming
   verification), `_build_retry_request`, and `_build_continuation_request`
   thread through `apply_thinking_config` with `PHASE_VERIFICATION` /
   `PHASE_VERIFICATION_RETRY` / `PHASE_VERIFICATION_CONTINUATION`.

6. **`tests/test_chunk_b_capability_policy.py`** — 41 unit tests covering
   the `ModelCapabilities` registry (each known model + one unknown), the
   `thinking_config_for` / `apply_thinking_config` helpers across every
   phase × model combination, and the frozen-dataclass invariant.

7. **`tests/test_request_payload_shape.py`** — Added 21 request-shape
   tests across `TestModelAwareThinkingRequestShape`,
   `TestSynthesisRequestShape`, and `TestNoLiteralThinkingPayloadsRemain`.
   The synthesis-on-Haiku regression test (`test_synthesis_omits_thinking
   _on_haiku_default`) pins the headline bug fix. The repo-wide grep test
   guards against new hardcoded `thinking` payloads slipping in.

## Tradeoffs and decisions

- **Whitelist registry, not pattern matching.** Plan directive 3 prefers
  a whitelist over a blacklist. Adding a new model means one new entry in
  `_MODEL_CAPABILITIES`. Unknown models fall back to
  `_DEFAULT_CAPABILITIES` which disables every capability flag — strictly
  safer than sending an invalid request shape on an unrecognized model ID.
  Operators who override to a brand-new Opus version via env may
  temporarily lose `thinking` until the registry is updated; that is
  acceptable because (a) the call still succeeds, just without thinking,
  and (b) discovery of new models is rare and explicit.

- **Phase-level opt-out set kept minimal.** `_PHASES_NO_THINKING` only
  contains `PHASE_TRIAGE` today. The synthesis phase is *not* in the set;
  capability-based filtering is enough to fix the Haiku bug. If an
  operator overrides synthesis to Opus, they still get thinking — which
  preserves prior behavior on capable models per plan directive 7 ("do
  not change model defaults unless required to prevent API errors").

- **`apply_thinking_config` vs `thinking_config_for`.** The two-helper
  layout lets request builders that already maintain a kwargs dict use
  the imperative form (`apply_thinking_config(kwargs, ...)`), while
  future builders that prefer functional composition can call
  `thinking_config_for` and merge themselves. Today only
  `apply_thinking_config` is used at the call sites.

- **No `display: omitted` flag yet.** Plan directive 6 mentions using
  `display: omitted` for automated calls. The current code never reads
  the API's thinking deltas (`stream.text_stream` already excludes them),
  so adding `display: omitted` is a no-op optimization rather than a
  correctness fix. Deferred to a future chunk — keeps Chunk B focused on
  the capability bug.

- **GUI / pipeline / report_exporter intentionally untouched.** Those
  files reference `ReviewResult.thinking` (a string field carrying the
  tool's `analysis_summary`), not the API `thinking` parameter. Naming
  collision, but no code change required here.

## Acceptance criteria coverage

| Plan acceptance criterion | Where covered |
| --- | --- |
| No hardcoded adaptive-thinking payloads remain outside central policy helpers | `TestNoLiteralThinkingPayloadsRemain` (regex scan over `src/`) |
| Haiku requests do not include adaptive thinking | `test_batch_review_omits_thinking_for_haiku`, `test_realtime_review_omits_thinking_for_haiku`, `test_batch_verification_omits_thinking_for_haiku`, `test_retry_request_omits_thinking_for_haiku`, `test_continuation_request_omits_thinking_for_haiku`, `test_cross_check_omits_thinking_for_haiku`, `test_synthesis_omits_thinking_on_haiku_default` |
| Sonnet/Opus requests include adaptive thinking only when supported | `test_*_includes_thinking_for_sonnet`, `test_request_carries_adaptive_thinking_for_opus`, `test_synthesis_adds_thinking_when_overridden_to_opus`, `test_cross_check_request_carries_adaptive_thinking_for_opus` |
| Unsupported models do not produce invalid request payloads | `test_unknown_model_degrades_safely`, `test_batch_review_omits_thinking_for_unknown_model`, `test_apply_thinking_config_omits_key_for_unknown_model` |
| Tests cover real-time review, batch review, verification, retry, continuation, cross-check, and synthesis request shapes | See `TestModelAwareThinkingRequestShape` + `TestSynthesisRequestShape` + `TestRealtimeReviewRequestShape` + `TestCrossCheckRequestShape` |

## Deferred / out of scope

- Populating `_PHASES_NO_THINKING` for the synthesis phase. Doing so would
  change behavior on operator overrides to Opus/Sonnet — that's a routing
  decision better made in Chunk I (verification modes + model routing).
- Adding `display: omitted` to `thinking_config_for` output. Pure
  optimization; defer until there is a concrete need.
- Refactoring `ReviewResult.thinking` (which carries the tool's
  `analysis_summary`) — naming collision with the API parameter is
  confusing but the field is consumed by report exporter and resume
  serialization. Renaming is a separate refactor.

## How to verify

```
pytest -q                                       # full suite — 370 pass, 2 xfail (Chunk C)
pytest tests/test_chunk_b_capability_policy.py  # 41 unit tests on the policy helpers
pytest tests/test_request_payload_shape.py      # 60 pass + 2 xfail, includes Chunk B regression coverage
```
