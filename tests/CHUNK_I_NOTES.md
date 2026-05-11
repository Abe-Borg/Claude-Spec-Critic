# Chunk I Implementation Notes

## Goal

Stop treating every verification task as a deep reasoning task. Route
simple verification through cheaper, more deterministic modes and
reserve expensive reasoning for genuinely hard cases. Make the routing
decision explicit (named, testable, surfaced in logs / reports /
diagnostics) so a future tuning pass has a single place to adjust the
policy.

## What was already in place

Before Chunk I, the verifier already had four functional behaviors that
roughly corresponded to the plan's four modes — they just weren't named:

| Plan mode | Pre-Chunk-I behavior |
|---|---|
| **Local skip** | `verification_router.classify_finding_for_verification(...)` keyword classifier + optional `triage.classify_findings_with_haiku(...)` Haiku triage. Result: `_local_skip_result()` returns an UNVERIFIED record with `cache_status="local_skip"`. |
| **Strict structured** | None. Every non-local-skip call used the same Sonnet + adaptive thinking + full profile budget path. GRIPES-severity findings that slipped through local-skip burned the same tokens as a CRITICAL claim. |
| **Standard reasoning** | The Sonnet default — `VERIFICATION_MODEL_DEFAULT = MODEL_SONNET_46`, `apply_thinking_config(..., phase=PHASE_VERIFICATION)` always set, profile-aware web_search budget. |
| **Deep reasoning** | The escalation path — `should_escalate_verification(...)` decides if a Sonnet UNVERIFIED for a CRITICAL/HIGH finding should be re-run on Opus. |

The bug Chunk I closes: nothing in the codebase named which mode a
given verification call was using, so reports / diagnostics / logs
could not say "5 findings were verified at standard reasoning, 12 at
strict structured, 2 at deep reasoning, 30 locally skipped." Without
that visibility, tuning the routing policy was an exercise in reading
the verifier source.

A second bug: there was no mode that captured "GRIPES that slipped past
local-skip" — they paid the full Sonnet-with-thinking cost. Chunk I's
`STRICT_STRUCTURED` mode is the new home for those.

## What this chunk added

### 1. `src/verification_modes.py` — the new module

Pure-function module (no I/O, no network). Imports from `api_config`
and `verification_profiles` only.

| Symbol | Purpose |
| --- | --- |
| `VerificationMode` | `str` Enum: `local_skip`, `strict_structured`, `standard_reasoning`, `deep_reasoning`. Inherits from `str` so serialization to JSON / cache / resume state is trivial. |
| `ModePolicy` | Frozen dataclass with `(mode, model, thinking_enabled, search_budget_multiplier, web_search_enabled, allows_escalation)`. The single source of truth for per-mode policy. |
| `mode_policy(mode)` | Table lookup. Unknown / malformed inputs fall back to STANDARD_REASONING so pre-Chunk-I cache entries (with `verification_mode = ""`) produce the pre-Chunk-I request shape. |
| `select_verification_mode(finding, *, local_skip, escalated, cached_mode)` | The pure-function router. Priority order: cache-hit replay → local_skip → escalated → CRITICAL CALIFORNIA_AHJ initial pass → GRIPES (any profile) → non-GRIPES INTERNAL_COORDINATION → STANDARD_REASONING. |
| `mode_search_budget(mode, *, profile_ceiling)` | Composes the mode's multiplier with the profile/severity ceiling from `profile_max_uses`. Returns 0 for LOCAL_SKIP, full ceiling for STANDARD/DEEP, ceiling × 0.5 (floor of 1) for STRICT_STRUCTURED. |
| `mode_label(mode)` | Human-readable label for reports / diagnostics. None-safe; unknown strings round-trip. |

### 2. Per-mode policy table

| Mode | Model | Thinking | Search multiplier | Allows escalation |
|---|---|---|---|---|
| `LOCAL_SKIP` | `"local"` | off | 0.0 | no |
| `STRICT_STRUCTURED` | Sonnet 4.6 | off | 0.5 (floor 1) | no |
| `STANDARD_REASONING` | Sonnet 4.6 (defers to `VERIFICATION_MODEL_DEFAULT`) | on | 1.0 | yes |
| `DEEP_REASONING` | Opus 4.7 (defers to `VERIFICATION_ESCALATION_MODEL`) | on | 1.0 | no (terminal) |

STRICT_STRUCTURED keeps Sonnet even when the operator flips
`SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT=0` — the point of the mode is
"cheap path for findings that do not need deep reasoning," so promoting
it to Opus would defeat the purpose. STANDARD_REASONING does defer to
the default so the everywhere-Opus override flows through.

### 3. Routing rules in priority order

The router is a pure function over `(finding, local_skip, escalated,
cached_mode)`. Rules, top to bottom:

1. **Cache hit replay.** If the caller passes `cached_mode`, the
   router returns it (when parseable) so a restored cache entry
   carries its original routing tag into reports. An unparseable
   cached_mode value falls through to the regular rules.
2. **Local skip wins.** If the keyword classifier or Haiku triage
   already said "no web verification needed," return LOCAL_SKIP
   regardless of severity / profile.
3. **Escalation forces DEEP_REASONING.** Any non-initial pass (the
   caller sets `escalated=True`) gets DEEP_REASONING. The
   `should_escalate_verification` policy in `verification_router`
   decides *whether* to escalate; this rule names the mode the
   escalation runs in.
4. **CRITICAL CALIFORNIA_AHJ jumps to DEEP_REASONING initially.** The
   initial pass for these almost always escalates anyway (the
   ambiguity surface is wide: Title 24 amendments, DSA / HCAI nuance,
   AHJ interpretation), so skipping the wasted Sonnet call is a
   direct cost win. This rule is gated on
   `verification_sonnet_default_enabled()` — when the operator has
   flipped to everywhere-Opus, there is no distinct "deep" tier and
   STANDARD_REASONING is the correct label.
5. **GRIPES severity → STRICT_STRUCTURED.** Editorial / cosmetic /
   placeholder findings that escape the local-skip classifier (typically
   because they have a non-empty `codeReference`) still don't need
   deep reasoning.
6. **Non-GRIPES INTERNAL_COORDINATION → STRICT_STRUCTURED.** Even
   when a HIGH-severity internal-contradiction finding slips past
   local-skip, web search adds little signal — the contradiction is
   verifiable from the spec text itself. Match the profile's tight
   search budget with the cheap mode.
7. **Default → STANDARD_REASONING.** The pre-Chunk-I behavior for
   everything else.

### 4. `VerificationResult.verification_mode`

New `str` field on the dataclass, default `""`. The field round-trips
through:

- `verification_cache._result_to_dict` / `_clone_for_store` /
  `_clone_for_hit` — disk cache stores and replays the mode.
- `resume_state.serialize_verification_result` /
  `deserialize_verification_result` — resume payloads carry it.
- `triage._local_skip_result()` — stamps `local_skip` directly.
- `_run_verification_call` — stamps the routed mode on every result
  it builds (both UNVERIFIED short-circuit and successful CONFIRMED /
  CORRECTED / DISPUTED returns).
- `_classify_wave_results` — the batch wave path re-derives the mode
  for each wave finding so initial-wave entries get the initial mode
  and retry-wave entries get DEEP_REASONING.

Pre-Chunk-I cache entries deserialize with `verification_mode = ""`
which `mode_policy` interprets as STANDARD_REASONING — same shape as
pre-Chunk-I behavior, so no migration is needed.

### 5. `_run_verification_call` rewiring

The real-time per-finding call now:

1. Classifies the verification profile (existing Chunk H step).
2. Picks the mode via `select_verification_mode(...)`.
3. Looks up the policy via `mode_policy(...)`.
4. Computes the effective web_search `max_uses` as
   `mode_search_budget(mode, profile_ceiling=profile_max_uses(...))`.
5. Builds the tools list via the existing
   `build_verification_tools_for_profile(...)` helper, then patches
   the web_search entry's `max_uses` if the mode multiplier narrowed
   it.
6. Conditionally calls `apply_thinking_config` — only when the mode
   policy says thinking is enabled. STRICT_STRUCTURED skips this call
   so the `thinking` key never lands on the payload.

The `model=` keyword override still wins so operator overrides and
test injection behave exactly the same as before. When no override is
given, `verify_finding` now reads `policy.model` from
`mode_policy(initial_mode)` so CRITICAL CALIFORNIA_AHJ findings
actually start on Opus.

### 6. `_classify_wave_results` rewiring

Each wave-finding result is stamped with the mode derived from the
finding + the wave's `escalated` flag. Initial-wave results get the
initial-pass mode (typically STANDARD_REASONING), retry-wave results
get DEEP_REASONING. The same `verification_mode` field flows into
`VerificationResult` and the cache.

The batch wave's request payloads themselves are not yet mode-aware in
the same way the real-time path is — that's a deliberate scope limit
for this chunk. The batch path already uses the profile-aware tool
builder (Chunk H), so the search budget shape is consistent with the
real-time path; the missing piece is the mode-level multiplier on top.
See "Deferred" below.

### 7. Diagnostics counters

`DiagnosticsReport.summary()` now exposes:

- `verification_modes` — `{mode_string: count}` keyed by
  `VerificationMode.value`. Events without a mode tag are bucketed
  under `"unknown"` so legacy events stay visible.
- `verification_profiles` — `{profile_string: count}` keyed by
  `VerificationProfile.value`. Same bucketing for legacy events.

`to_text()` renders both as `Modes:` and `Profiles:` lines in the run
summary when at least one event recorded the field.

### 8. Pipeline / controllers emit the new fields

`review_run_controller` and `batch_controller` were already emitting
`verdict / grounded / cache_status / model_used / escalated` on
verification events. They now also emit `verification_mode` and
`verification_profile` so the diagnostics counters above have data to
aggregate.

### 9. Test additions — `tests/test_chunk_i_verification_modes.py`

46 new tests, marked `verification_modes`. Structure mirrors Chunk I
Directive 6:

| Test class | Scope |
| --- | --- |
| `TestVerificationModeEnum` (3) | four modes exist, string values stable, str inheritance. |
| `TestModeLabel` (4) | each mode has a label, None → "", string round-trip, unknown string passes through. |
| `TestModePolicy` (6) | each mode's policy bundle is correct (local/strict/standard/deep), unknown mode → STANDARD_REASONING fallback, string mode value accepted. |
| `TestSelectVerificationMode` (13) | one test per routing rule + representative cases from the plan: low-severity editorial → STRICT_STRUCTURED, simple stale-code → STANDARD_REASONING, high-severity code → STANDARD_REASONING initially, internal coordination HIGH → STRICT_STRUCTURED, source-disputed escalation → DEEP_REASONING, prior cache hit preserves stored mode, CRITICAL CALIFORNIA jumps to DEEP initially, sonnet-default-off keeps it STANDARD, cached_mode string accepted, unknown cached_mode falls through, None finding safe-defaults. |
| `TestModeSearchBudget` (6) | LOCAL_SKIP → 0, STANDARD / DEEP → full ceiling, STRICT_STRUCTURED → ½ ceiling, floor-of-1, zero-ceiling → 0. |
| `TestVerificationResultModeField` (2) | field defaults to `""`, `_local_skip_result()` stamps `local_skip`. |
| `TestCacheRoundTripsMode` (3) | disk cache save/load preserves mode, resume state round-trips, legacy resume payload (no `verification_mode` key) deserializes with empty default. |
| `TestDiagnosticsCountsModes` (3) | summary includes per-mode + per-profile breakdowns, legacy events bucketed as `unknown`, `to_text()` renders the Modes line. |
| `TestRealTimeCallRespectsMode` (3) | STANDARD_REASONING attaches `thinking` and full budget, STRICT_STRUCTURED omits `thinking` and scales budget, escalated call stamps `deep_reasoning`. End-to-end through `_run_verification_call` via a fake streaming client. |
| `TestBatchWavePathStampsMode` (2) | wave stamps standard_reasoning for non-escalated HIGH code finding; stamps deep_reasoning for an escalated wave finding. |
| `TestCacheDoesNotBypassRouting` (1) | cache hit returns the cached record (with its original mode tag); a monkeypatched `_run_verification_call` confirms the cache path short-circuits before the routing decision. |

The smoke test was extended to import `src.verification_modes` and to
assert `verification_mode` is a field on `VerificationResult`.

All 609 pre-Chunk-I tests pass unchanged. New total: 655 passing
(609 baseline + 46 Chunk I).

## Tradeoffs and decisions

- **Modes are conservative.** STANDARD_REASONING is observationally
  identical to pre-Chunk-I behavior — same model, same thinking, same
  profile budget. STRICT_STRUCTURED is the only mode that changes the
  request shape (no `thinking`, halved search budget). LOCAL_SKIP and
  DEEP_REASONING are just names for paths the codebase already had.
  This is intentional: the bulk of Chunk I's value is *visibility*
  into the routing decision; behavioral changes are a separate axis
  that operators can tune via the policy table once the mode tags are
  populated in their telemetry.
- **The router lives in a new module, not in `verification_router`.**
  `verification_router` already had two distinct responsibilities
  (local-skip classifier + escalation policy); folding mode selection
  into it would have made the file a grab bag. The new module has one
  job: pick the mode and describe its policy. The cross-references
  are deliberately one-way — `verification_modes` reads from
  `api_config` and `verification_profiles`, nothing imports from it
  except `verifier` and the tests.
- **The `model=` keyword still wins.** Operator overrides
  (`SPEC_CRITIC_VERIFICATION_MODEL=...`), explicit test injection, and
  the existing escalation loop all pass `model=` explicitly; the mode
  policy only supplies the default when no override is given. This
  matches the pre-Chunk-I contract where `model` was an explicit
  parameter on `verify_finding` and `_run_verification_call`.
- **The `_run_verification_call` body conditionally calls
  `apply_thinking_config` instead of having the helper accept a
  policy.** The thinking helper already encapsulates "is the
  parameter even valid for this model?"; adding another axis would
  have inverted its public surface. The mode policy supplies the
  boolean and the call site decides whether to call the helper at
  all. This keeps Haiku-model triage (which has its own no-thinking
  rule via `_PHASES_NO_THINKING`) cleanly separate from the
  per-mode rule.
- **`STRICT_STRUCTURED.search_budget_multiplier = 0.5` rather than
  enumerating exact integers per profile.** A multiplier composes
  cleanly with `profile_max_uses` — the profile + severity logic
  stays in one place (Chunk H), and the mode logic stays in one
  place (here). The combinatorial table (profile × severity × mode)
  is a third axis that nobody has asked for yet; the multiplier is
  simpler.
- **Floor-of-1 on the strict-structured budget.** A profile/severity
  ceiling of 1-2 (e.g. internal-coordination CRITICAL = 2,
  manufacturer GRIPES = 3) scaled by 0.5 would otherwise produce a
  budget of 0 or 1; the floor ensures the model can at least issue
  one search before declaring UNVERIFIED.
- **Cache hits replay the stored mode rather than being relabeled.**
  A cache entry created when the routing rules said
  "STANDARD_REASONING" should keep that label when it's restored,
  even if the rules change. Otherwise a single rules tweak would
  silently rewrite every cached entry's mode tag on first restore.
  The `cached_mode` parameter lets callers explicitly opt into that
  preservation; the unparseable-string fallback keeps the router from
  crashing on future mode values.
- **Pre-Chunk-I resume / cache payloads have no migration.** Every
  new field reads with `payload.get("verification_mode", "")`; an
  empty string deserializes to STANDARD_REASONING in the policy
  lookup, which is exactly the pre-Chunk-I behavior. Operators with
  an existing on-disk cache lose no data and pay no migration cost.
- **The batch wave path stamps the mode but does not yet apply the
  mode-level search-budget multiplier on the wave request payloads.**
  The wave loop builds its retry / continuation requests via
  `_build_retry_request` / `_build_continuation_request`, which use
  the profile-aware tool builder (Chunk H). Adding the mode-level
  multiplier there is a small follow-up; for this chunk the wave
  path uses the profile ceiling directly and the result is still
  tagged with the routed mode so diagnostics are accurate. See
  "Deferred" below.

## Risks

- **Mode tags depend on accurate classification.** The keyword
  classifier in `verification_profiles` decides whether a finding is
  CALIFORNIA_AHJ / INTERNAL_COORDINATION / etc., which then drives
  the mode. A mis-classified CRITICAL finding (e.g. CALIFORNIA_AHJ
  that the keyword set missed) will route through STANDARD_REASONING
  instead of DEEP_REASONING. The `should_escalate_verification`
  policy still catches that on the retry pass, so the end result is
  one wasted Sonnet call rather than a missed verification — but
  operators should watch the `verification_modes` counter for
  unexpected distributions.
- **STRICT_STRUCTURED's halved budget on GRIPES findings could under-
  resource a GRIPES with a real code claim.** The cache + escalation
  policy + grounding invariant still apply, so an under-grounded
  STRICT_STRUCTURED verdict would come back UNVERIFIED. But this
  mode is new behavior; a future audit should check the
  `STRICT_STRUCTURED / UNVERIFIED` count to make sure the budget is
  not too tight.
- **Verifier system prompt is unchanged.** The mode policy chose not
  to alter the prompt because the prompt's stable prefix is pinned
  for prompt caching. Per-mode prompt language (e.g. "this is a
  shallow factual check; do not exhaustively search the literature")
  is a future tuning opportunity.

## Deferred / out of scope

- **Mode-aware retry / continuation request payloads in the batch
  wave loop.** `_build_retry_request` / `_build_continuation_request`
  could read `mode_policy(...)` and apply the multiplier the same way
  `_run_verification_call` does. The wave path currently stamps the
  mode onto the result but uses the profile ceiling directly for the
  wave request. This is a contained follow-up that can land without
  schema or routing changes.
- **Per-mode prompt language.** `profile_priority_domains(profile)`
  already exists (Chunk H) but is not appended to the live system
  prompt because that would invalidate the prompt-cache breakpoint
  per profile. Per-mode language is a separate axis with the same
  trade-off and was deferred for the same reason.
- **`mode_policy.allows_escalation` is not yet consulted by
  `should_escalate_verification`.** Today the escalation decision
  reads the current model + verdict + severity directly. A future
  pass could fold the mode in (and remove the special-case for
  Sonnet-default-disabled in the router) so the whole escalation
  decision is "policy.allows_escalation AND
  severity_qualifies_for_escalation AND
  verdict_says_escalate" — but the current behavior is correct,
  just slightly redundant.
- **Report Word-export integration.** The Word report does not yet
  render the mode label per finding. The data is on
  `f.verification.verification_mode`; adding a `Mode:` row alongside
  the existing severity / verdict / sources rows is a one-line
  change to `report_exporter.py`. The diagnostics text and JSON
  output already include the per-run mode breakdown.
- **Telemetry by mode.** `verification_evidence` rolls
  grounded/ungrounded/escalated up across all modes. A per-mode
  rollup of those same fields would help tune the strict-structured
  budget, but adds report complexity and was deferred.

## How to verify

```
pytest -q                                                  # full suite, 655 pass
pytest -m verification_modes                                # Chunk I tests only, 46 pass
pytest tests/test_chunk_i_verification_modes.py -q          # same set, explicit path
```

The `verification_modes` pytest marker is registered in
`pyproject.toml`.
