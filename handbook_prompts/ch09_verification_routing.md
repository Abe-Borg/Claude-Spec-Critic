# Agent Prompt — Chapter 9: Verification I — How We Decide to Check

**Full title:** *Verification I: How We Decide to Check (Routing, Modes, Profiles, Triage)*

## Your mission
Explain the **decision layer** of verification: given a finding, *how should we
check it* — or should we web-search at all? This is a cost-vs-coverage routing
problem. Not every finding deserves an expensive Opus deep-reasoning pass with a
web-search budget; a leftover `TODO:` deserves none. This chapter owns the
classifiers and the policy that turn a finding into a `VerificationRoutingDecision`.
Chapter 10 then *executes* that decision.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (modes, profiles, budgets), §7 (glossary).
2. `CLAUDE.md` — **§3 Verification Routing in full** (the Profiles table, the
   Search-budget table, the Modes table), "Local-skip safety,"
   "LOCALLY_CLASSIFIED keyword tightening," "Deterministic-rule ids are public,"
   and the web-fetch eligibility note (which modes get web_fetch).
3. Source you own:
   - `src/verification/verification_prescreen.py` — `local_skip` vs `web_required`
     keyword classification; `_LOCAL_SKIP_KEYWORDS`, the elevated-confidence
     split, `local_skip_requires_elevated_confidence`.
   - `src/verification/verification_profiles.py` — `classify_finding_profile`,
     the five profiles, priority order, severity→budget delegation.
   - `src/verification/verification_modes.py` — `select_verification_mode`, the
     mode selection priority order.
   - `src/verification/verification_routing.py` — `VerificationRoutingDecision`,
     `select_routing`, `build_verification_request`,
     `build_verification_tools_from_decision` (web_fetch eligibility),
     `apply_routing_to_result`.
   - `src/verification/triage.py` — Haiku triage, `is_eligible_for_haiku_triage`,
     the hard safety contract.
   - The search-budget map in `api_config.py` (`_SEVERITY_MAX_USES`,
     `web_search_max_uses_for_severity`, `profile_max_uses`) — reference; full
     config ownership is Ch 12.

## In scope (what you own)
- **The prescreen.** How findings are locally classified `local_skip` vs
  `web_required`; the keyword lists and the tightening story (`"formatting"`
  removed as too broad; `"leed"` / `"internal contradiction"` moved to the
  elevated-confidence list; the `requires_elevated_confidence` flag that rides to
  the sidecar as telemetry but never to the cache). How `deterministic_rule` ids
  from the pre-screen (Ch 4) are recognized here so a `TODO`/placeholder finding
  is locally skipped — but CRITICAL/HIGH or any non-empty `codeReference` forces
  `web_required`.
- **Profiles (5).** `classify_finding_profile` and the priority order
  (internal-coordination → california_ahj → manufacturer → code-standard →
  constructability). What each profile means and that the *profile picks the
  priority-source language* in the verifier prompt, while the **search budget is
  severity-based and identical across profiles.**
- **Search budget.** The flat severity tiers (CRITICAL=8, HIGH=7, MEDIUM=5,
  GRIPES=3) and how the web-search tool builder and verifier read from one map.
- **Modes (4).** `select_verification_mode` and the **selection priority order**
  (cache-hit replay → local_skip → escalated → CRITICAL california_ahj initial →
  GRIPES → non-GRIPES internal_coordination → default). Reproduce the
  mode/model/thinking/budget/web_fetch/escalates table from `CLAUDE.md` §3. Note
  web_fetch is enabled only for STANDARD_REASONING and DEEP_REASONING.
- **The routing decision object.** `VerificationRoutingDecision` as the policy
  bundle, and that `select_routing` is the single pure-function selector and
  `build_verification_request` builds the kwargs dict used by every verification
  path (real-time, batch initial, retry, continuation).
- **Triage.** Haiku-based pre-classification (opt-in / always-on for eligible
  findings); the **hard safety contract**: findings with any non-empty
  `codeReference` are never eligible; CRITICAL/HIGH are never eligible; on API
  failure or parse error, default to `web_required`. Haiku-triaged local skips
  never get the elevated-confidence flag.

## Explicitly OUT of scope (owned elsewhere)
- The actual verification *call*, grounding, verdicts, escalation execution,
  budget-exhaustion detection, the cache → **Ch 10**. You decide the *policy*;
  Ch 10 *executes* it. (E.g., you explain that escalation routes to deep_reasoning
  on Opus; Ch 10 explains how the escalation call runs and how contested is
  detected.)
- Output caps / thinking-config / cache-policy machinery and the full env-var
  table → **Ch 12** (reference the budget map; don't document all of api_config).
- How a finding's resolved status renders → **Ch 11**.

## Narrative beats to hit
- *The core trade*: web search is slow and costly; skipping it risks missing a
  real code error. The routing layer is the program's answer — spend reasoning
  and search budget where severity and content justify it, and nowhere else.
- *Defense against over-skipping*: the safety contract (code references and
  CRITICAL/HIGH always get web search; triage fails safe to `web_required`). Tell
  the story of *why* `"formatting"` was removed and `"leed"`/`"internal
  contradiction"` were reclassified — a too-broad keyword could silently bypass
  verification of a real requirement.
- *One source of truth*: the budget map and the unified `select_routing` exist so
  the four verification paths can't drift apart.

## Invariants & facts you MUST get right
- The five profiles and their priority order; profile sets *priority source
  language*, not budget.
- Severity budgets: CRITICAL=8/HIGH=7/MEDIUM=5/GRIPES=3, flat across profiles.
- Four modes and the exact selection priority order.
- web_fetch enabled for STANDARD/DEEP only.
- Triage hard contract (codeReference/CRITICAL/HIGH never eligible; fail to
  `web_required`).
- The `requires_elevated_confidence` flag goes to the sidecar as telemetry, never
  to the cache.

## Diagrams & tables
- The **routing decision tree** (finding → prescreen → triage? → profile →
  severity → mode), as a diagram.
- The **modes table** (mode | when | model | thinking | budget | web_fetch |
  escalates?) reproduced from `CLAUDE.md` §3.
- The **profiles table** and the **severity-budget table**.

## Cross-references to make
- To **Ch 4** (deterministic_rule ids feed local-skip), **Ch 10** (execution of
  the decision: grounding, escalation, contested, budget exhaustion, cache),
  **Ch 11** (LOCALLY_CLASSIFIED status, the elevated-confidence telemetry in the
  sidecar), **Ch 12** (budget map / config).

## Deliverable
- Write to **`handbook/09_verification_routing.md`**. H1 = the full title. Target
  **3,500–5,000 words**.

## Quality bar
- A reader can predict, for a given finding, which mode/model/budget it gets and
  why. Tables match `CLAUDE.md` §3 and the source exactly. Cleanly defers
  execution to Ch 10.
