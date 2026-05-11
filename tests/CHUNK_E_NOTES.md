# Chunk E Implementation Notes

## Goal

Make the Anthropic `count_tokens` endpoint the authoritative gate when
available, count tokens against the *selected* model rather than a
hard-coded one, apply a model-specific safety factor to local cl100k_base
estimates on the fallback path, and centralize every phase's output-token
cap in one registry so a future tuning pass touches one map.

## What was already in place

Spec Critic had a lot of the scaffolding the plan calls for:

| Plan directive | State before Chunk E |
| --- | --- |
| Per-phase output caps as constants | Already in `api_config.py` (`REVIEW_OUTPUT_CAP`, `CROSS_CHECK_OUTPUT_CAP`, `VERIFICATION_OUTPUT_CAP`, `SYNTHESIS_OUTPUT_CAP`, `HAIKU_TRIAGE_OUTPUT_CAP`). |
| Hard model ceilings clamped via helper | `output_cap_for_model` existed and was used by every `*_max_tokens` helper. |
| Verification cap is modest (≤16k) | `VERIFICATION_OUTPUT_CAP = 16_000`. |
| Real-time vs batch baselines unified | `review_max_tokens(batch=...)` already returns the same baseline; the 300k batch path is gated behind `allow_extended_output=True`. |
| Selected-model token count + preflight wired | `count_tokens_via_api(model=...)` existed; `_prepare_specs` called it. |
| Local cl100k preflight | `count_tokens` + `exceeds_per_call_limit` existed. |

The gaps that Chunk E was actually meant to close:

1. **Preflight model was hard-coded.** `_prepare_specs` passed
   `MODEL_OPUS_47` to `count_tokens_via_api` and to the token-cache key,
   even when the run targeted Haiku or Sonnet. That meant the GUI gauge
   and the pipeline guard counted tokens under a different model than
   the request would actually run against, and the cache key would be
   stale for non-Opus runs.
2. **Local cl100k was the only *hard* gate.** The exact API count was
   logged as a warning and never raised, while the cl100k count was the
   one that could refuse a submission. That inverts directive 3
   ("exact count is the authoritative guard").
3. **Local estimate carried no safety factor.** cl100k undercounts
   Claude's tokenization by a few percent (more on Haiku for structured
   spec text). Without a multiplier, a cl100k count just under the
   recommended max could mask a real overage.
4. **Phase budgets had no central registry.** Each `*_max_tokens`
   helper individually called `output_cap_for_model(..., requested=...)`.
   To re-tune (e.g. give verification continuations more headroom),
   you'd have had to change every helper one by one.
5. **Verification retry and continuation shared one constant.** They
   both pulled `verification_max_tokens(model=...)` directly, with no
   way to differentiate them without changing the call site.

## What this chunk added

### 1. `src/api_config.py` — phase → budget registry

* Moved the `PHASE_*` constants to live next to the output-cap helpers
  (they used to sit alongside the thinking-config helpers further down
  the file). The phase identifiers are now defined once, near the top,
  and consumed by both the budget registry and the thinking-config
  policy.
* Added a private `_PHASE_OUTPUT_BUDGET: dict[str, int]` mapping each
  phase to its nominal output cap.
* Added a public `phase_output_cap(phase, *, model) -> int` helper that
  looks up the phase in the registry and clamps the result through
  `output_cap_for_model`. Unknown phases fall back to
  `VERIFICATION_OUTPUT_CAP` — the smallest value in the registry — so a
  forgotten registration loses headroom instead of accidentally
  inheriting the 128k review cap.
* Rewired every `*_max_tokens` helper to route through `phase_output_cap`.
  Their signatures are unchanged, so all existing callers keep working.
* Added a `phase=` parameter to `verification_max_tokens` so retry and
  continuation paths can pick up phase-specific budgets without
  hard-coding the constant. Today retry / continuation / initial all
  resolve to the same value, but the parameter is the lever for a
  future tuning pass.

### 2. `src/tokenizer.py` — model-aware fallback gate

* Added `local_estimate_safety_factor(model) -> float` returning a
  model-specific multiplier ≥ 1.0:
  - Opus 4.6 / 4.7 and Sonnet 4.6 → 1.10×
  - Haiku 4.5 → 1.15× (cl100k undercounts Haiku tokenization a bit
    more on structured construction-spec text)
  - Unknown / None → 1.20× (the widest margin)
* Added `safe_local_estimate(local_tokens, *, model) -> int` that
  applies the factor and rounds up.
* Added `exceeds_per_call_limit_for_model(spec_tokens, overhead_tokens,
  *, model) -> bool` — the model-aware version of the legacy
  `exceeds_per_call_limit`. Keeps the original symbol around so
  callers that haven't been updated don't break.
* The new functions are documented as the fallback path: when an
  exact Anthropic count is available, that is the authoritative gate;
  the local estimate plus safety factor is only consulted when the
  API call is disabled or returns `None`.

### 3. `src/pipeline.py` — exact count is authoritative

* `_prepare_specs` now accepts a `model` kwarg (defaulting to
  `REVIEW_MODEL_DEFAULT`) and threads it through both the exact
  preflight and the local fallback gate.
* The exact preflight runs first (when enabled). When the API returns a
  value over `RECOMMENDED_MAX`, the pipeline **raises a `ValueError`**.
  Previously this was only a log warning, and the local cl100k count
  was the one that could refuse a submission — that ordering is exactly
  inverted from directive 3.
* The per-spec local gate runs after the exact check. It uses
  `exceeds_per_call_limit_for_model` so the cl100k count is padded by
  the model-specific safety factor before being compared to
  `RECOMMENDED_MAX`.
* Both error messages now name the selected model so an operator
  triaging a runtime failure can see exactly which model the budget
  was computed for.
* `start_batch_review` and `run_review` (the two callers of
  `_prepare_specs`) thread the user-selected model through unchanged.

### 4. `src/verifier.py` — retry / continuation use the phase tag

* `_build_retry_request` now calls `verification_max_tokens(model=...,
  phase=PHASE_VERIFICATION_RETRY)`.
* `_build_continuation_request` now calls `verification_max_tokens(
  model=..., phase=PHASE_VERIFICATION_CONTINUATION)`.
* No behavior change today — the registry currently maps both phases
  to `VERIFICATION_OUTPUT_CAP`. The change is in *where the value
  comes from*: any future divergence is one map edit, not three
  function rewrites.

### 5. `src/token_analysis_controller.py` (GUI wiring)

* Removed the hard-coded `from .reviewer import MODEL_OPUS_47 as _model`.
* `refresh_exact_token_count` now consults `app._get_selected_model()`
  when it exists and falls back to `REVIEW_MODEL_DEFAULT`. The current
  GUI does not yet expose that getter, so the practical effect is
  "use the configured default model" instead of "always use Opus 4.7" —
  if/when the GUI adds a model picker, the gauge will follow.
* This is the only GUI touchpoint in Chunk E; the plan explicitly
  permits minimal wiring of this kind.

### 6. `tests/test_chunk_e_token_budgets.py` — 45 regression tests

Marked `@pytest.mark.token_budget` and grouped by directive:

* `TestPhaseOutputCapRegistry` — every phase resolves through the new
  helper, with the expected constants and the conservative unknown-phase
  fallback.
* `TestPhaseCapsRespectModelCeilings` — pin the model-clamp behavior
  for review (Haiku, Sonnet), cross-check (Sonnet), the no-overshoot
  property over every phase × model combo, and the 300k beta path.
* `TestPhaseHelpersRouteThroughRegistry` — every `*_max_tokens`
  helper equals `phase_output_cap(...)` for the matching phase. If a
  future refactor splits one of those helpers off again, this fails
  loud.
* `TestLocalEstimateSafetyFactor` — Opus / Sonnet / Haiku / unknown /
  None safety factors are inside their expected ranges, and the
  unknown-model factor is at least as wide as any known model.
* `TestExceedsPerCallLimitForModel` — the model-aware gate refuses
  inputs that the legacy gate accepts, when the safety factor pushes
  the cl100k count over `RECOMMENDED_MAX`.
* `TestPipelinePreflightSelectsModel` — `_prepare_specs` calls
  `count_tokens_via_api` with the model that was selected for the run
  (Sonnet, Haiku, …), not Opus.
* `TestPipelinePreflightExactCountAuthoritative` — an exact count over
  the recommended max raises; a count under it does not; the local
  fallback gate still runs when the exact preflight is disabled.
* `TestRequestShapeBudgetsByModel` — pin the actual `max_tokens` value
  emitted into request payloads for each phase × model combination
  the production code uses today. Includes retry / continuation /
  initial verification consistency.
* `TestOutputCapsAreModelLimitAware` — direct coverage of
  `output_cap_for_model` so the floor under the registry stays intact.

### 7. `pyproject.toml`

New marker `token_budget`.

### 8. `CLAUDE.md`

No mechanical edits in this chunk — the file's "Output caps live in
`api_config.py`" section already documents the constants correctly,
and the new `phase_output_cap` / safety-factor helpers are internal
plumbing that callers don't need to know about. The doc continues to
serve as the place where the visible constants are described.

## Tradeoffs and decisions

### Exact count raises; local count raises with the model-specific multiplier

Plan directive 3 says exact count is the *authoritative* gate. That
language stops short of "the local count must stop being a gate" — and
removing the local gate would have been a behavior regression for
operators who run with `SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT=0` (the
endpoint occasionally rate-limits, so the env var is the escape hatch).
The chosen behavior:

1. If the exact preflight runs and returns a value over the budget →
   raise immediately, naming the model in the error message.
2. Otherwise (preflight off, or API returned `None`), apply the
   model-specific safety factor to the cl100k count and raise if the
   padded value is over the budget.

Both gates produce the same outcome — refuse to submit a request that
will breach the budget — but the language in the error message tells
the operator which gate fired.

### Phase registry maps to constants, not functions

The original `*_max_tokens` helpers all took a `model` kwarg. I kept
them as the public API and made the registry an implementation detail.
Reasons:

- Callers in `reviewer.py`, `batch.py`, `cross_checker.py`,
  `verifier.py`, and `triage.py` already import these helpers by name.
  Replacing them with `phase_output_cap(PHASE_X, model=...)` at every
  call site would have churned 8 files for no behavior change.
- The registry is the place a future tuning pass touches — it's the
  one map. The helpers stay as thin wrappers.

### Verification retry and continuation share the verification cap *today*

The plan recommends "centralize phase-specific output budgets:
verification, verification retry/continuation". I read that as "give
the three phases a way to diverge without rewriting call sites" rather
than "make them diverge now." Diverging without evidence would have
been speculative — we don't currently see retry truncations. The
phase parameter is in place if/when that evidence appears.

### Local safety factor is conservative, not measured

The 1.10× / 1.15× / 1.20× values are conservative heuristics, not
measurements. I considered running a sweep against a representative
spec corpus to fit actual cl100k → Claude ratios per model, but the
plan explicitly says "the multipliers should be conservative" rather
than "tight" — the goal is to remove false confidence, not to estimate
exact token counts (the API counter does that). The exact preflight
runs by default, so the safety factor only matters on the fallback
path and a conservative multiplier is the right tradeoff there.

### GUI gauge change is opt-in

The GUI uses `getattr(app, "_get_selected_model", None)` so the change
is a no-op until the GUI exposes a model picker. Today the gauge will
use `REVIEW_MODEL_DEFAULT` (Opus 4.7), which matches the practical
default and matches the previous hard-coded behavior. The plan
allows minimal GUI wiring; this is the wiring that paid for itself.

### `exceeds_per_call_limit` retained for backward compatibility

The legacy signature has callers outside the pipeline (e.g.
`token_analysis_controller.py`). I kept it as a thin wrapper (no
safety factor) and added `exceeds_per_call_limit_for_model` for the
new behavior. Once every caller threads the selected model through,
the legacy function can be deleted in a follow-up.

## Acceptance criteria coverage

| Plan acceptance criterion | Where covered |
| --- | --- |
| Exact token count is authoritative when available | `_prepare_specs` raises on exact-over-budget; `TestPipelinePreflightExactCountAuthoritative::test_exact_count_over_budget_raises` |
| Token-counting model matches selected model | `_prepare_specs` threads the `model` parameter into `count_tokens_via_api` and `token_count_cache_key`; `TestPipelinePreflightSelectsModel` |
| Local tokenizer estimates no longer create false confidence | `local_estimate_safety_factor` + `exceeds_per_call_limit_for_model`; `TestLocalEstimateSafetyFactor::test_safe_local_estimate_pads_upward`, `TestExceedsPerCallLimitForModel::test_safety_factor_pushes_borderline_over_limit` |
| Output caps are centralized, phase-aware, and model-limit-aware | `_PHASE_OUTPUT_BUDGET` + `phase_output_cap`; `TestPhaseOutputCapRegistry`, `TestPhaseCapsRespectModelCeilings::test_no_phase_exceeds_model_ceiling` |
| Tests cover budget behavior without making real API calls | Whole suite is hermetic; `_StubClient` and `FakeClient` are the only client surfaces touched |

## Deferred / out of scope

* **`max_tokens` truncation retry/escalation policy** (directive 8 of
  the plan). The plan recommends preferring a retry policy over
  blanket-allocating huge caps. The existing review-repair batch
  already retries truncated reviews (see
  `pipeline._recover_retryable_review_batch_results`), and the
  verifier's continuation loop handles `pause_turn` and `max_tokens`
  cases — both are correct behaviors that this chunk did not need to
  touch. A focused investigation of which phases actually hit
  truncation in practice would be the right next step.
* **Thinking-token accounting** (directive 9). Thinking tokens are
  charged from the `max_tokens` budget when adaptive thinking is
  enabled. The current verification cap (16k) and synthesis cap (32k)
  already leave plenty of headroom for both thinking and verdict text;
  the review/cross-check caps (128k / 96k) intentionally allow large
  thinking budgets on Opus. No code change here; the choice is already
  reflected in the constants.
* **`safe_local_estimate` calibration via measurement** — see the
  "conservative, not measured" tradeoff above.
* **Removing `exceeds_per_call_limit`** in favor of the model-aware
  version everywhere. Left as follow-up because it requires threading
  the selected model through `token_analysis_controller.py` callers
  that the plan flags as GUI scope.

## How to verify

```
# Full suite (466 tests, hermetic).
python -m pytest -q

# Chunk E regression tests only (45 tests).
python -m pytest -m token_budget -v

# Or by file:
python -m pytest tests/test_chunk_e_token_budgets.py -v
```
