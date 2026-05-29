# Agent Prompt — Chapter 12: Configuration, Models & Token Economics

**Full title:** *Configuration, Models & Token Economics*

## Your mission
Explain the **control plane**: the single sources of truth for which models are
used and what they're allowed to do, the output-cap and prompt-cache policies,
the token-counting and preflight math, the pinned-standards code-cycle data, and
the operator-facing environment variables. This is the chapter that explains why
a misconfigured model id produces a *smaller* request instead of a crash — and
why that safety has a sharp edge of its own.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (facts: model stack, caps, budgets, context limits),
   §7.
2. `CLAUDE.md` — "Model capability whitelist," "§6 Token Budgets," "§7 Prompt
   Caching," "§8 Environment Variables" (the full table), "Pinned standards
   editions," "Code cycle: California 2025 only."
3. Source you own:
   - `src/core/api_config.py` (~750 lines) — model id defaults, `ModelCapabilities`
     + `model_capabilities` (the whitelist + degrade-to-safe-defaults),
     `_PHASE_OUTPUT_BUDGET` + `phase_output_cap` + per-phase `*_max_tokens`,
     `assert_extended_output_allowed`, `thinking_config_for`/`apply_thinking_config`,
     `effort_config_for`/`apply_effort_config`, `CachePolicy` + `cache_policy_for`
     + `system_prompt_with_cache` + `tools_with_cache`, `_SEVERITY_MAX_USES` +
     `web_search_max_uses_for_severity`, `build_web_search_tool` /
     `build_web_fetch_tool`, `batch_service_tier`, the env-flag parsing.
   - `src/core/code_cycles.py` — `CodeCycle`, `CALIFORNIA_2025`, the pinned
     edition fields (NFPA/ASHRAE/IAPMO/UL), the hashable UL tuple, `DEFAULT_CYCLE`.
   - `src/core/tokenizer.py` — local cl100k + Anthropic counting,
     `MAX_CONTEXT_TOKENS`/`RECOMMENDED_MAX`/`CROSS_CHECK_RECOMMENDED_MAX`,
     `safe_local_estimate` and the per-model safety multipliers.
   - `src/core/api_key_store.py` — API key loading/persistence.
   - `src/core/app_paths.py` — platform config/state directories.
4. `TRUST_AUDIT.md` P0-3 (whitelist staleness silently degrades a *newer/better*
   model — e.g. an unlisted `opus-4-8`), P0-4 (the hardcoded 300k beta header),
   P2-2 (`safe_local_estimate` not clamped ≥ 1.0), P2-3 (extended-output threshold
   vs. model).

## In scope (what you own)
- **The model stack & capability whitelist.** The default model ids per phase and
  that every one is env-overridable. `model_capabilities` as the **single source
  of truth** for adaptive-thinking / extended-output / 1M-context / effort
  eligibility; the whitelist (Opus 4.7, Sonnet 4.6, Haiku 4.5); and the central
  design choice: **unknown ids degrade to safe defaults** (every capability flag
  off, 200k context) so a misconfigured env var yields a smaller request, never
  an API rejection. Haiku phases never carry `thinking`.
- **Token economics.** The tokenizer (local estimate + exact Anthropic count),
  the context constants, and `safe_local_estimate`'s per-model padding (Opus/
  Sonnet 1.10×, Haiku 1.15×, unknown 1.20×) used when the exact count is
  unavailable. Tie to the preflight that *raises* (the preflight call site is
  Ch 7; you own the constants and the estimator).
- **Output caps & extended output.** `_PHASE_OUTPUT_BUDGET` and `phase_output_cap`
  clamping to the model ceiling (reproduce the §6 cap table); the 300k extended
  path gating (`assert_extended_output_allowed`) and its batch-only, ≥200k-input
  conditions.
- **Prompt caching policy.** `cache_policy_for` as the single source of truth
  (1h TTL; which phases cache and why; triage doesn't — one-off, below the Haiku
  cache minimum); `system_prompt_with_cache` / `tools_with_cache` and how cache
  breakpoints attach (the *prompt-side* discipline is Ch 5; you own the policy).
- **Pinned standards editions.** The `CodeCycle` dataclass and `CALIFORNIA_2025`;
  the NFPA/ASHRAE/IAPMO/UL edition fields; why UL editions are a tuple-of-tuples
  (hashable under `frozen=True`); and that empty fields degrade gracefully across
  the three surfaces that render them (reviewer prompt, verifier prompt, report
  note — those *uses* are Ch 5/10/11; you own the *data*).
- **The environment variables.** Reproduce the §8 table (model overrides, cache
  persist/TTL/path, element-ids toggle, trace toggles) and the boolean-parsing
  convention. API-key store and app-paths.

## Explicitly OUT of scope (owned elsewhere)
- *How* budgets/caps/caching are consumed: review prompt caching → **Ch 5**;
  search-budget routing → **Ch 9**; verifier prompt's use of pinned editions →
  **Ch 10**; report's pinned-editions note → **Ch 11**; trace env vars' effect →
  **Ch 14**. You own the *definitions and policy*; defer the *consumption*.
- The token-preflight *call* that raises → **Ch 7**.

## Narrative beats to hit
- *Configuration as a safety system.* The whole module is built so the program
  fails toward "smaller, valid request" rather than "crash." Explain why that's
  the right default for an operator-tunable tool.
- *The sharp edge of safe degradation (Audit P0-3).* The same mechanism that
  protects against a typo also **silently degrades a genuinely newer, better
  model**: set `SPEC_CRITIC_REVIEW_MODEL` to an unlisted id (e.g. a successor
  Opus) and you quietly lose extended thinking, drop from 300k to 128k, and lose
  effort tuning — with no error. Whether the default `opus-4-7` is still current,
  and whether an unknown id should *warn loudly*, are open questions. Present this
  candidly — it's the chapter's most interesting tension.
- *The hardcoded beta header risk (Audit P0-4).* `output-300k-2026-03-24` is
  checked only for *presence*, not *acceptance* — the same risk class as the
  retired web-fetch header that already crashed the codebase once (full story →
  Ch 10/17). Note whether a graceful fallback to 128k exists.
- *Hand-maintained edition strings.* The pinned editions are transcribed from the
  California adoption matrix and must be verified against it before changing — a
  quiet correctness dependency.

## Invariants & facts you MUST get right
- Whitelist = Opus 4.7 / Sonnet 4.6 / Haiku 4.5; unknown → all flags off, 200k.
- Cap table per §6; 300k is batch-only, inputs ≥200k.
- `safe_local_estimate` multipliers: 1.10 / 1.15 / 1.20.
- Cache TTL 1h; triage not cached.
- `DEFAULT_CYCLE = CALIFORNIA_2025`; 2022 removed; cycle label is in the
  verification cache key (so a cycle bump invalidates entries — detail Ch 10).
- UL editions are a tuple-of-tuples for hashability.

## Diagrams & tables
- The **output-cap table** (phase → cap) and a **context-limits table**.
- The **env-var table** (reproduce §8).
- A small "model id → capabilities" table showing the whitelist vs. the
  degraded default.

## Cross-references to make
- To **Ch 5** (prompt caching use), **Ch 7** (preflight raises), **Ch 9** (budget
  routing), **Ch 10** (cache key, pinned-edition prompt use, the beta-header
  story), **Ch 11** (pinned-editions report note), **Ch 14** (trace env vars),
  **Ch 16/17** (P0-3/P0-4 and the lessons).

## Deliverable
- Write to **`handbook/12_configuration_and_models.md`**. H1 = the full title.
  Target **3,500–5,000 words**.

## Quality bar
- A reader can configure the tool safely, predict what an unknown model id does,
  and read the cap/cache/budget policy from one place. The degrade-vs-quality-loss
  tension is explained, not glossed.
