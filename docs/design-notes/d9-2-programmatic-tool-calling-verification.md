# D9.2 — Programmatic Tool Calling (PTC) for Repeated Verification

**Type:** Research only — no production code changes.
**Status:** Recommendation below.
**Scope:** Whether Spec Critic's verification path should adopt Anthropic's Programmatic Tool Calling (PTC) to batch multiple finding verifications, filter intermediate web_search results, or reduce per-finding token cost.
**Prepared:** Phase D9 (Wave 4).

---

## 1. What PTC is, in one paragraph

Programmatic Tool Calling lets the model write a Python script that runs inside Anthropic's `code_execution_20260120` sandbox and calls tools as Python functions (`await query_tool(...)`). Tool calls pause the container, return a `tool_use` block, and the orchestrator returns a `tool_result` whose body is fed back into the running script — not into Claude's context. Only the script's final stdout makes it back into the conversation. The pattern is documented at `platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling`.

Two things make this interesting for verification: (a) intermediate `web_search` results never enter Claude's context, so processing many findings in one script is dramatically cheaper than N round-trips; (b) the script can filter, sort, and short-circuit before deciding what to surface to the model for the final verdict.

---

## 2. What we have today

Per-finding verification (real-time and batch alike) runs a single Claude turn with `web_search` + `submit_verification_verdict` attached. The model issues 1–7 searches (profile + severity tiered, see `api_config.web_search_max_uses_for_severity` and `verification_profiles.profile_max_uses`), reasons over the results, and emits a verdict. Sonnet 4.6 handles initial verification; Opus 4.7 handles escalation. The verification cache (`verification_cache.VerificationCache`) keys on cycle + actionType + codeReference + claim hash, so duplicate claims across files dedupe.

In the typical batch (a few dozen findings on a 30-spec project) verification is the slowest and most expensive phase. Each finding pays the full cost of:

- a fresh system prompt + tool list (cached for the prefix after the first call),
- 1–7 `web_search` requests at $10 / 1,000 searches plus the search-result tokens that flow back into context,
- a Sonnet inference over those results plus reasoning.

The total "search-result tokens loaded into context" is the line item PTC most directly attacks.

---

## 3. Question 1 — Is the feature available for the models in use?

Yes. The PTC documentation explicitly supports:

| Model | PTC supported |
|---|---|
| Claude Opus 4.7 (`claude-opus-4-7`) | ✅ |
| Claude Opus 4.6 (`claude-opus-4-6`) | ✅ |
| Claude Sonnet 4.6 (`claude-sonnet-4-6`) | ✅ |
| Claude Haiku 4.5 (`claude-haiku-4-5`) | **not listed** |

Spec Critic's default `VERIFICATION_MODEL` is Sonnet 4.6 with Opus 4.7 escalation. Both support PTC. Haiku 4.5 — used only for triage (`triage.py`) and synthesis (`cross_checker._run_cross_discipline_synthesis`) — is *not* on the supported list, so a Haiku-based PTC verification path is not currently available.

PTC requires `code_execution_20260120` to be enabled. It works on the Claude API, Claude Platform on AWS, and Microsoft Foundry; it is not yet on Amazon Bedrock or Vertex AI. Spec Critic uses the first-party API, so this is not a blocker.

---

## 4. Question 2 — Does it support web_search + code_execution + a custom verdict tool simultaneously?

**Partial yes, but with caveats.**

What the docs guarantee:

- `code_execution_20260120` (server tool) and `web_search_20260209` (server tool) coexist on the same request — the web_search dynamic-filtering feature itself "requires the code execution tool to be enabled" (web_search docs).
- A custom tool with `allowed_callers: ["code_execution_20260120"]` is callable from the running script.
- Tools with `strict: true` are **not supported** with programmatic calling (PTC constraints section). Spec Critic's verdict tool currently uses `strict` only when `SPEC_CRITIC_STRICT_TOOLS=1`; the production default is `strict` off, so this is not a hard blocker — but the existing escape hatch must stay off if PTC is enabled.
- `tool_choice` cannot force programmatic calling of a specific tool. The verifier already uses `{"type": "auto"}` (forced choices conflict with thinking), so this matches.
- `disable_parallel_tool_use: true` is **not supported** with PTC. The verdict tool currently sets `disable_parallel_tool_use: true` in `cross_check_tool_choice` (not the verification path), but `review_tool_choice` and `cross_check_tool_choice` both do. The verification tool_choice is never forced, so this is fine — but a future change that turns on `disable_parallel_tool_use` for verification would conflict with PTC.

What is unclear:

- Whether the verdict tool can be invoked *from inside the script* (`allowed_callers: ["code_execution_20260120"]`) or only as a terminal direct tool call. The docs lean toward "PTC is for tools the script needs to gather data from, not for the final model output," so the natural shape is: script calls `web_search` repeatedly, processes results, prints a verdict JSON to stdout, and the model then emits the structured verdict tool call as its terminal action *after* the script ends. That is two different tool roles in one turn.

Best read of the docs: **yes, all three coexist, but the verdict tool stays in the `direct` caller bucket and the script only calls `web_search`.**

---

## 5. Question 3 — What data-retention implications follow from code execution containers?

The PTC documentation states:

> Container data, including execution artifacts and outputs, is retained for up to 30 days.

And:

> This feature is not eligible for Zero Data Retention (ZDR). Data is retained according to the feature's standard retention policy.

Spec Critic does not currently advertise ZDR to operators, and CLAUDE.md does not impose ZDR requirements. But the project is reviewing California K-12 DSA specs — finding text *can* include project-identifying language, equipment tags, or stamped engineer names. If an operator's workflow is governed by an end-user agreement that requires ZDR, **PTC is incompatible** and they must use the existing tool-use path.

This is a deployment-level constraint, not a code constraint. A future PTC-enabled path would need an explicit operator opt-in (e.g. `SPEC_CRITIC_PROGRAMMATIC_VERIFICATION=1`) with a docstring that calls out the ZDR ineligibility.

Container lifetime detail: 4.5 minutes of idle time before cleanup, 30 days hard maximum. For verification, which finishes a script in seconds, the idle timeout is not a practical concern.

---

## 6. Question 4 — Can multiple findings be verified in one call without cross-contaminating verdicts?

**Mechanically yes — semantically risky.** This is the central question.

The PTC pattern that would batch verifications would look like (sketch, do not implement):

```python
# Hypothetical script the model writes
findings = [
    {"id": "rf-001", "issue": "...", "code_ref": "CBC 1234", ...},
    {"id": "rf-002", "issue": "...", "code_ref": "CMC 567", ...},
    # ... up to ~N findings
]

verdicts = {}
for f in findings:
    # Each iteration uses web_search separately
    results = await web_search(query=f"{f['code_ref']} {f['issue'][:80]}")
    # Filter to the most relevant 2-3 sources
    relevant = [r for r in results if any_useful_signal(r, f)]
    verdicts[f["id"]] = synthesize_verdict(f, relevant)

print(json.dumps(verdicts))
```

The model then emits one terminal `submit_verification_verdict` call per finding (or a batch verdict tool — a new schema) summarizing what the script found.

**Why it is risky**:

1. **Search-result bleed.** Each iteration's `web_search` results stay in the script's memory until the script ends. The script may use sources from finding *A* when synthesizing finding *B* without realizing the source's claim is actually about A. The model reasoning about the batch summary may inherit that confusion.
2. **Source attribution.** The current grounding gate (`verifier._apply_source_grounding`) compares `cited_sources` (URLs the model emitted) against `searched_sources` (URLs `web_search` actually returned), per finding. In a PTC batch, the `searched_sources` set is per *script run*, not per finding. Either the script writes finding-scoped source maps (more code) or the grounding gate has to be weakened to "the script searched these URLs across the batch" (worse signal).
3. **Cache invalidation.** The verification cache keys on per-finding claim hash. A batched script that produces N verdicts in one call cannot trivially cache hit or miss — the cache helper would have to be moved out of the per-call path and into the post-script verdict loop. Doable but adds complexity.
4. **Escalation routing.** Sonnet vs Opus is chosen *per finding* by `verification_router.should_escalate_verification`. A batched PTC call commits to one model up front, so escalation either happens after the batch (defeating the batching efficiency) or never (losing escalation quality).
5. **Web search budget.** `max_uses` on `web_search` is per-request, not per script iteration. A 20-finding batch with `max_uses=5` per finding would need `max_uses=100` total, which is dramatically more rope than Anthropic's docs suggest is normal.

**Conservative read: PTC is well-suited to a workflow where one Claude turn produces one verdict — even if that turn uses many tools internally. It is *not* well-suited to producing N verdicts at once.**

If the goal is "verify many findings in one round trip," the existing batch API (Messages Batches) already provides this at 50% cost, with the per-finding isolation guaranteed by construction.

---

## 7. Question 5 — Would this simplify or complicate source attribution?

**Complicate.** The current grounding model (Chunk H) treats source attribution as a per-finding fact:

- `searched_sources` — URLs `web_search` returned for *this finding's* verification call.
- `cited_sources` — URLs the model put in the verdict tool's `sources` array for *this finding*.
- `accepted_sources` — intersection after normalization.

Under PTC, the script controls the search loop. The natural shape — one script, many findings — does not preserve per-finding `searched_sources` unless the script itself bookkeeps them. That bookkeeping is brittle (the model wrote the script) and easy to get wrong.

A "one PTC script per finding" shape preserves attribution trivially, but at that point PTC is just adding the code-execution container overhead without batching benefits. The token saving from filtering `web_search` results before they hit context is real but small — most verification calls today use 1–3 searches with short result snippets.

---

## 8. Question 6 — What is the estimated cost-per-finding compared with current verification?

Cost components per finding today (rough estimate for a typical Sonnet 4.6 verification):

| Component | Tokens / units | Cost |
|---|---|---|
| System prompt + tool list (cached after first call) | ~1,800 in / 0 out (cache read at ~0.1×) | ~$0.0001 |
| User prompt (finding text) | ~400 in | ~$0.001 |
| web_search results loaded into context | ~3,000–8,000 in across 1–3 searches | ~$0.01–$0.025 |
| Model reasoning + verdict | ~800 out | ~$0.012 |
| web_search billing | 1–3 searches at $10 / 1,000 | ~$0.01–$0.03 |
| **Per-finding total** | — | **~$0.03–$0.07** |

With PTC (one finding per script):

| Component | Tokens / units | Cost |
|---|---|---|
| System prompt + tool list (cached) | similar | ~$0.0001 |
| User prompt | similar | ~$0.001 |
| Script written by model (`server_tool_use` code) | ~300 out | ~$0.0045 |
| web_search results — **not loaded into context** | 0 in | $0 |
| Filtered final summary that hits context | ~500 in | ~$0.0015 |
| Model reasoning + verdict | ~800 out | ~$0.012 |
| web_search billing | same 1–3 searches | same ~$0.01–$0.03 |
| Code execution billing | $0.05/hour container after 1,550 free hours/month — effectively free for normal use | ~$0 |
| **Per-finding total** | — | **~$0.025–$0.05** |

Savings come almost entirely from "search results don't hit context." That is real but modest at the current search-result sizes, and most callers will not see a material improvement because the existing search budgets are already tight.

With PTC (batched, N findings per script) the savings *would* be larger because the system prompt and tool list are amortized — but the §6 risks (search bleed, attribution, escalation routing, cache) make the batched shape unrealistic in the near term.

---

## 9. Question 7 — Anthropic-specific request shape and beta header

PTC does not require a beta header — `code_execution_20260120` is GA on the supported models. The request shape is:

```python
tools=[
    {"type": "code_execution_20260120", "name": "code_execution"},
    # web_search 20260209 — server tool, available alongside
    {"type": "web_search_20260209", "name": "web_search", ...},
    # Verdict tool, callable only via direct (terminal action by the model)
    {**verification_verdict_tool(), "allowed_callers": ["direct"]},
]
```

Tools the model invokes from the script need `allowed_callers: ["code_execution_20260120"]`. `web_search` is a server tool and is implicitly callable from the script (per the dynamic-filtering docs). The verdict tool would stay `direct`-only so it is the model's terminal action, not something the script calls.

Note: `tools[].strict: true` is not supported under PTC. `SPEC_CRITIC_STRICT_TOOLS=1` would have to be disabled when PTC is enabled.

---

## 10. Risk assessment

| Risk | Severity | Notes |
|---|---|---|
| Cross-finding source bleed in batched script | High | The whole point of grounding (Chunk H) is per-finding attribution. PTC batching weakens this by default. |
| ZDR incompatibility | Medium | Spec Critic doesn't currently require ZDR, but operators on California DSA projects may be subject to it under separate agreements. Needs explicit opt-in if adopted. |
| Escalation routing breaks under batching | Medium | Sonnet → Opus is per-finding today. A batched PTC call would need to either pre-classify or do single-finding scripts. |
| Cache key incompatibility | Medium | Per-finding claim cache assumes per-finding calls. PTC batching would require restructuring. |
| `strict: true` regression | Low | Easy to detect at request build time. |
| Web search budget inflation | Low | Each script iteration counts as one `web_search`. Budget would need profile-aware scaling for batches. |
| Container expiry mid-call | Very low | Verification scripts finish in seconds; 4.5-minute idle timeout is not a factor. |
| No materially better economics for single-finding PTC | Medium | Without batching, PTC saves ~20–40% on context tokens but adds container overhead complexity. Possibly not worth the engineering cost. |

---

## 11. Recommendation

**Recommendation: do not use, revisit if Anthropic ships a feature that makes per-finding attribution trivial under PTC.**

Rationale:

1. **The headline benefit of PTC is batching many tool calls into one round trip.** Spec Critic's verification path is already per-finding by design — every safety property (grounding, escalation, caching, profile budgeting) is keyed on a single finding. PTC's biggest lever (script-level batching) directly fights that design.
2. **Per-finding PTC (one script per finding) is technically possible but the cost savings are modest** and come at the price of a more complex request shape, container retention concerns, and a `strict: true` regression hazard.
3. **The Messages Batches API already provides the parallelism and 50% cost reduction** that operators looking for cheaper verification want. That path is already wired up (`src/batch.py`).
4. **The grounding model is Spec Critic's most user-facing trust feature** (Chunk H, source partitioning into searched / cited / accepted / rejected). Weakening it for batching efficiency would be a step backward.

**Concrete revisit triggers:**

- Anthropic adds first-class per-finding attribution helpers to PTC (e.g. a `caller_metadata` field that flows through to verdict tool calls).
- Anthropic adds ZDR eligibility for code execution containers used in PTC.
- Spec Critic's verification cost-per-run becomes a top-three operator complaint and the easy wins (better caching, smaller search budgets) are exhausted.
- An audit of `parse_verification_response` shows context size, not search count, is the dominant cost line item.

**No prototype now. No code changes.**

---

## 12. References used

- Spec Critic source: `src/verifier.py` (`_run_verification_call`, `_apply_source_grounding`, `_enforce_grounding_invariant`), `src/verification_cache.py` (`make_cache_key`), `src/verification_router.py` (`should_escalate_verification`), `src/batch.py` (`build_verification_tools_for_profile`), `src/verification_profiles.py` (`profile_max_uses`), `src/structured_schemas.py` (`verification_verdict_tool`, `_strict_enabled`).
- Anthropic documentation (fetched 2026-05-11):
  - `platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling.md` (model compatibility, `allowed_callers`, container lifecycle, ZDR ineligibility, `strict: true` constraint, response format).
  - `platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool` (dynamic filtering depends on `code_execution`, citations always-on, `pause_turn` semantics).
- Delta plan: Chunk D9.2 in `1e4f5a14-spec_critic_delta_plan_from_second_agent.md`.
