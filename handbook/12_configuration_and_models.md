# Configuration, Models & Token Economics

Every other chapter in this handbook describes something the program *does* —
extract a spec, raise a finding, ground a verdict, write a report. This chapter
describes the part that decides *how* all of that is allowed to happen: which
model answers each call, how many tokens it may spend, what it is permitted to
think about, what gets cached, which code editions it measures the spec against,
and which knobs an operator can turn from the outside. This is the **control
plane**. It runs no API calls of its own and produces no findings; it produces
the *shape* of every request the rest of the system makes.

The control plane has one governing design principle, and it is worth stating
before any of the mechanics: **the program is built to fail toward a smaller,
valid request rather than a crash.** A reviewer running Spec Critic is usually
not the person who wrote it. They will set an environment variable they half
remember, point the tool at a model id that does not exist yet, or run it a year
after the betas it depends on have been renamed. When that happens, the question
is not "can we prevent every mistake" — we cannot — but "what does the program do
with a mistake it cannot prevent." Spec Critic's answer, almost everywhere in
this module, is to strip the risky parameter, clamp the budget down, and send a
request the API will still accept. That instinct is correct far more often than
it is wrong. But it has a sharp edge — the same reflex that protects you from a
typo will silently *degrade a genuinely better model* — and the most interesting
part of this chapter is being honest about where the edge cuts.

Five files carry the control plane, each a single source of truth for one
concern:

| File | Owns |
|---|---|
| `src/core/api_config.py` | model ids, capability whitelist, output caps, extended-output gating, prompt-cache policy, effort/thinking policy, search-budget map, web tool builders |
| `src/core/code_cycles.py` | the `CodeCycle` data model and `CALIFORNIA_2025` (pinned standard editions) |
| `src/core/tokenizer.py` | local + exact token counting, context limits, the safety-padding estimator |
| `src/core/api_key_store.py` | resolving the Anthropic API key (keyring → file fallback) |
| `src/core/app_paths.py` | the platform config/state directories |

The recurring word in that table is *owns*. The whole point of this layer is
that there is exactly one place to read each policy. When the verifier needs a
search budget it does not invent one; it asks `api_config`. When the batch path
needs to know whether a model can produce 300k tokens it does not test the model
family by hand; it asks the capability registry. Centralization is not tidiness
for its own sake — it is what makes the program's behavior *predictable from one
file* instead of emergent from a dozen scattered conditionals.

## The model stack

Spec Critic is a multi-model pipeline. Different phases have genuinely different
economics — a per-spec review reasons over a hundred thousand tokens of dense
code-referenced prose and wants the strongest model available; a triage
classification sorts short findings into two buckets and wants the cheapest. So
the defaults are tiered, and every model id lives as a named constant at the top
of `api_config.py` (`MODEL_OPUS_48 = "claude-opus-4-8"`, and likewise for
`claude-sonnet-5`, `claude-sonnet-4-6`, and `claude-haiku-4-5`).

| Phase | Default model | Env override |
|---|---|---|
| Review (per-spec) | Opus 4.8 | `SPEC_CRITIC_REVIEW_MODEL` |
| Cross-spec coordination | Sonnet 5 | *(none — see note)* |
| Verification, initial pass | Sonnet 5 | `SPEC_CRITIC_VERIFICATION_MODEL` |
| Verification, escalation / deep-reasoning | Opus 4.8 | `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` |
| Requirements research (profile modules) | Sonnet 5 | `SPEC_CRITIC_RESEARCH_MODEL` |
| Compliance pass (profile modules) | Sonnet 5 | *(none — cross-check parity)* |
| Triage | Haiku 4.5 | `SPEC_CRITIC_TRIAGE_MODEL` |

Sonnet 4.6 remains a registered constant (`MODEL_SONNET_46`) even though no
phase defaults to it anymore — an operator env override that pins the
previous-generation id must keep its correct request shape, most notably the
`xhigh` → `high` effort clamp that 4.6 needs and Sonnet 5 does not.

The pattern that produces an override is a single line —
`os.environ.get("SPEC_CRITIC_REVIEW_MODEL", MODEL_OPUS_48)` — read once at import
time. The rationale behind the *tiering* is economic and is documented inline:
verification "routes through Sonnet first and reserves Opus for escalation," and
triage is "shallow classification over short inputs; Haiku fits." Opus is the
expensive instrument, spent only where reasoning depth pays for itself — the
initial review and the escalation tier of verification.

> **Drift note.** The handbook's shared-facts sheet describes the stack as
> "defaults, all overridable by env var." The source has one exception:
> **cross-spec coordination is not env-overridable.** `CROSS_CHECK_MODEL_DEFAULT`
> is bound directly to `MODEL_SONNET_5` with no `os.environ.get`, and there is
> no `SPEC_CRITIC_CROSS_CHECK_MODEL` variable anywhere in `src/`. `CLAUDE.md`'s
> environment-variable table agrees — it lists no cross-check override. If a
> future operator needs to retune the coordination model, this is the one phase
> that requires a code change rather than a config change. The *consumption* of
> the cross-check model is [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md); the default is
> defined here.

## The capability whitelist, and why an unknown model is *safe*

A model id is not just a string you pass to the API — it is a promise about what
request shapes the API will accept. Send `thinking` to Haiku 4.5 and the request
is rejected, because Anthropic's model overview lists Haiku without adaptive
thinking. Send `output_config.effort` to a model that does not support it and you
get the same rejection. These failures surface "deep in the request lifecycle" —
after the batch is assembled, the prompt is built, the tokens are counted —
which is the worst possible place to discover a malformed request.

So `api_config` does not guess from the model's *name*. It consults a
**whitelist**: a frozen `ModelCapabilities` record per known model, and
`model_capabilities(model)` is the single accessor every request-shaping decision
flows through.

```python
@dataclass(frozen=True)
class ModelCapabilities:
    supports_adaptive_thinking: bool
    max_output_tokens: int
    supports_extended_output_beta: bool   # 300k batch-only beta
    context_window: int
    supports_effort: bool = False
    supports_strict_tools: bool = False
    supports_xhigh_effort: bool = False   # xhigh effort level gate
```

The whitelist covers exactly four models, plus a default for everything else:

| Model id | thinking | effort | xhigh | 300k extended | context | output ceiling |
|---|---|---|---|---|---|---|
| `claude-opus-4-8` | ✓ | ✓ | ✓ | ✓ | 1,000,000 | 128,000 |
| `claude-sonnet-5` | ✓ | ✓ | ✓ | ✗ *(pending confirmation)* | 1,000,000 | 128,000 |
| `claude-sonnet-4-6` | ✓ | ✓ | ✗ | ✓ | 1,000,000 | 64,000 |
| `claude-haiku-4-5` | ✗ | ✗ | ✗ | ✗ | 200,000 | 64,000 |
| **anything else** | ✗ | ✗ | ✗ | ✗ | 200,000 | 64,000 |

That last row is the load-bearing one. An unknown id falls through
`_MODEL_CAPABILITIES.get(model, _DEFAULT_CAPABILITIES)` to a record with **every
capability flag off** and the most conservative numbers. The reasoning is stated
plainly in the source: "Stripping a feature from a future model is strictly safer
than sending an invalid request that fails deep in the request lifecycle." A
misconfigured env var produces a *smaller* request, never an API rejection.

Two policy helpers read this registry and decide what to attach to a request,
and both follow the same discipline — *omit the key entirely, never set it to
`null`*, because the API rejects `thinking=null` and `output_config=null` just as
firmly as it rejects an unsupported feature:

- **`thinking_config_for(model, phase)`** returns `{"type": "adaptive"}` only when
  the phase is not on the no-thinking list *and* the model supports adaptive
  thinking. Triage is the sole member of `_PHASES_NO_THINKING`, so a Haiku phase
  never carries `thinking` — and it would be stripped anyway because Haiku's flag
  is off. That belt-and-suspenders is deliberate: even if someone overrode triage
  to a thinking-capable model, the phase opt-out still holds.
- **`effort_config_for(model, phase)`** attaches `output_config.effort` — `xhigh`
  for the deep phases (review, cross-check, compliance), `high` for Opus on the
  escalation verification phase and for research, `medium` for Sonnet
  verification, and *nothing* for triage or any model whose `supports_effort`
  flag is off. The usable levels are `low`/`medium`/`high`/`xhigh`, and `xhigh`
  is gated per model by `supports_xhigh_effort` (Opus 4.8 ✓, Sonnet 5 ✓,
  Sonnet 4.6 ✗): `effort_config_for` clamps `xhigh`→`high` on any model whose
  capability entry lacks the flag. On today's defaults nothing clamps —
  cross-check and compliance run their declared `xhigh` natively on Sonnet 5 —
  but the clamp stays load-bearing for a pinned Sonnet 4.6 override, which
  rejects `xhigh` at submit with a 400.

The deeper consequence of the degrade-to-safe default deserves to be made
concrete, because it is the chapter's central tension and it is *sharper than it
looks*. The output ceiling is enforced by `output_cap_for_model`, which now
resolves through the capability registry itself:

```python
return min(requested, model_capabilities(model).max_output_tokens)
```

(It used to be a hand-written family check — `if model in OPUS_MODELS: 128k,
else: 64k` — which silently clamped any 128k-capable model that wasn't an Opus
id; Sonnet 5, the first 128k Sonnet, is what forced the ceiling into the
registry. `OPUS_MODELS` survives for exactly one decision: Opus on a
verification phase is the escalation tier, so effort bumps to `high`.) The
degrade-to-safe tension shows up the moment an operator points review at an id
the registry doesn't know — say a future successor like `claude-opus-4-9`. That
id has no capability entry. It loses adaptive thinking. It loses `xhigh`
effort. It loses the 300k extended-output path. And its baseline output cap
falls to the conservative default: **64,000 tokens**, half of what a listed
Opus gets. The operator did this *to get better reviews*, and the program gives
them a weaker, smaller request. This *used* to happen with no warning at all;
the trust-audit fix (P0-3) now emits one `WARNING` per unrecognized id naming
the conservative caps it fell back to, so the under-powering is at least
visible. We return to this in the closing section — keeping the whitelist
current is still the real remedy.

## Output caps and the extended-output path

Anthropic bills by actual output tokens, so an output cap is not a cost lever —
you pay for what the model emits, not for the ceiling you set. The caps exist as
a **fail-fast guard**: a single review on a normal-size spec should not be able to
run away and emit a quarter-million tokens of findings, and 16k is plenty for a
verifier whose system prompt asks for a one-or-two-sentence verdict.

Every phase resolves its cap through one registry, `_PHASE_OUTPUT_BUDGET`, and
one function, `phase_output_cap(phase, *, model)`, which looks up the phase's
desired cap and then clamps it to the selected model's ceiling. The canonical
caps:

| Phase | Cap |
|---|---|
| Review / batch review | 128,000 |
| Extended batch review | 300,000 *(batch-only, inputs ≥ 200k tokens)* |
| Cross-check | 96,000 |
| Verification (+ retry / continuation) | 16,000 |
| Triage | 8,000 |

Two design choices in that registry are worth pulling out. First, verification
retry and continuation deliberately reuse the plain verification cap — "the
verdict envelope is unchanged across retries, so granting more output only
invites the model to ramble." The retry/continuation phases exist as *separate
registry keys* anyway, so a future tuning pass that discovers continuations need
more headroom changes one line. Second, an **unknown phase** falls back to the
verification cap (16k), the most conservative value in the registry — a future
phase that forgets to register itself "loses headroom instead of accidentally
inheriting the 128k review cap." The whole module leans the same direction:
when in doubt, allocate less.

The **extended-output path** is the one place the program asks for dramatically
more, and it is gated three ways at once. `_should_allow_extended_output` (in
`review_request_builder.py`) returns true only when the model's capability record
permits the beta *and* the local token count of the actual request is at or above
`LARGE_REVIEW_INPUT_THRESHOLD` (200,000). A small spec stays on the 128k cap; only
a genuinely large input lifts to 300k. Then, at the batch call site,
`assert_extended_output_allowed` is the fail-fast backstop: if `max_tokens`
exceeds the Opus single-request ceiling (128k), the `output-300k-2026-03-24` beta
header **must** be present, or it raises before the request is ever handed to the
SDK.

That the extended path is *batch-only* is not a Spec Critic decision — 300k output
is available on the Message Batches API by Anthropic's design, which fits the
program's architecture perfectly, since all reviews already run through batch
(the mechanics are [**Ch 6 — Batch Processing: The Message Batches Backbone**](06_batch_processing.md)).
One subtlety: Sonnet 4.6 also carries the extended-output capability now; an
earlier version of the code gated this by Opus-family membership and *excluded*
Sonnet incorrectly. Reading the flag from the capability registry instead of
testing `model in OPUS_MODELS` is what fixed it — a small object lesson in why the
whitelist is the single source of truth rather than a family check. Sonnet 5, by
contrast, starts with the flag *off* — the conservative pre-confirmation posture
Sonnet 4.6 itself began with: until the beta's supported-model list is confirmed
to include it, a Sonnet-5-overridden extended review caps at the 128k baseline
rather than risking a 400 on the beta header.

## Token economics: counting before you commit

The output cap governs what comes back. The *context* limits govern what you are
allowed to send, and getting them wrong is expensive in a different way — an
oversized request is rejected after you have already paid to assemble and count
it. `tokenizer.py` owns both the limits and the math.

| Constant | Value | Meaning |
|---|---|---|
| `MAX_CONTEXT_TOKENS` | 1,000,000 | the model's full context window |
| `RECOMMENDED_MAX` | 500,000 | per-spec input budget; preflight **raises** above this |
| `PROJECT_CONTEXT_MAX_TOKENS` | 100,000 | hard cap on the reusable project-context block |
| `CROSS_CHECK_RECOMMENDED_MAX` | 822,000 | `1M − 128k output reserve − 50k overhead` |

The per-spec budget (500k) is intentionally conservative against the 1M window
because per-spec review sends one spec at a time and the GUI's token gauge
displays the largest spec against this number. The cross-check budget is far
higher because the coordinator sends *all* spec content in a single call, so the
limit is computed by reserving output and overhead headroom out of the full
window.

Counting happens two ways. The cheap, always-available path is local: `tiktoken`
with `cl100k_base`, used for the responsive GUI gauge. The authoritative path is
`count_tokens_via_api`, which calls Anthropic's `count_tokens` endpoint for the
exact input total of a specific request shape. The contract between them is the
interesting part. `cl100k_base` is *OpenAI's* tokenizer; it does not match
Claude's, and it tends to **undercount** Claude's number on the dense,
section-numbered, table-heavy text that fills a mechanical spec. An undercount is
the dangerous direction — it makes a too-large request look safe. So whenever the
local estimate is used as a *budget gate*, it is padded:

| Model | Safety multiplier |
|---|---|
| `claude-opus-4-8` | 1.10× |
| `claude-sonnet-4-6` | 1.10× |
| `claude-sonnet-5` | 1.45× *(new tokenizer: ~30% more tokens than the 4.6-family tokenizer, compounded onto the family's 1.10× cl100k pad)* |
| `claude-haiku-4-5` | 1.15× |
| unknown | 1.20× |

`safe_local_estimate(local_tokens, model=...)` multiplies and rounds *up* — "the
factor is a safety margin, not a midpoint estimate." The unknown-model factor is
the widest (1.20×) so, consistent with the whole module's posture, a future model
"never silently sails through a budget check that would have been blocked under a
known model." The crucial caveat: this padding only matters on the *fallback*
path. When the exact Anthropic count is available it is authoritative and the
local pad is bypassed entirely. The preflight that actually *raises* a
`ValueError` when the exact count exceeds `RECOMMENDED_MAX` lives at the pipeline
call site — that is [**Ch 7 — Orchestration & State: The Pipeline Spine**](07_orchestration.md); this
chapter owns the constants and the estimator it consults.

## Prompt-cache policy

Prompt caching is where the program's token economics turn into real savings, and
the *policy* — which phases cache and why — is centralized in `cache_policy_for`,
even though the *prompt-side discipline* of where the cache breakpoints physically
land is [**Ch 5 — The Review Engine**](05_review_engine.md)'s to own.

A `CachePolicy` independently toggles whether the system prompt and the trailing
tool block each carry a `cache_control` breakpoint. The default (and the setting
for review, cross-check, and all three verification phases) is to cache both. The
one exception is triage:

| Phase | Cached? | Why |
|---|---|---|
| Review / batch review / cross-check / verification (+ retry/continuation) | yes | the system prompt + tools are large, stable, and re-sent across many specs/waves |
| Triage | no | a ~375-token Haiku prompt is below the 2,048-token Haiku cache minimum — a cache write would be paid for and never hit |

Every cached breakpoint uses a **1-hour TTL** rather than the 5-minute default,
and the reasoning is specific to this workload: a batch verification cycle runs
"30 minutes to several hours," far past the 5-minute window, so a 5-minute cache
would expire between waves and never pay back. The 1-hour TTL costs 2× the cache
*write* but "typically pays back inside the second wave," where the same system
prompt is sent hundreds of times. This is the rare case where the program spends
*more* up front — a deliberate, measured exception to its otherwise frugal
instincts, justified by the batch architecture.

`system_prompt_with_cache` and `tools_with_cache` are the two helpers that apply
the policy; the latter attaches the breakpoint to the *last* tool in the list so
the system prompt and tool definitions share one cache prefix, and changing only
a tool definition invalidates only the tool-level entry. (`extract_cache_usage`
pulls the `cache_creation` / `cache_read` token counts off the response for the
diagnostics layer — [**Ch 14 — Observability**](14_observability.md).)

## Pinned standards editions: the data behind the prompts

A California 2025 spec is not measured against "NFPA 13" in the abstract — it is
measured against the *specific edition* of NFPA 13 the California Building
Standards Commission adopted for the cycle, amendments included. Getting that
edition wrong in either direction is a trust failure: flag a compliant reference
as stale, or fail to flag a genuinely outdated one. The `CodeCycle` dataclass is
where those editions are pinned, and `CALIFORNIA_2025` is the populated instance.

`CodeCycle` carries the core California codes (CBC, CMC, CPC, the Energy Code,
CALGreen, ASCE 7 and its previous edition) plus adopted editions for NFPA
13/14/20/24/25/72, ASHRAE 62.1/90.1/15, the IAPMO Uniform Plumbing trade-standards
companion, and a set of UL listing editions (UL 300/555/555S/268/1479). Edition
strings are free-form precisely so a single field can carry both the base edition
and its amendment provenance — `"2022 with California Amendments"` is one value,
not a parse problem.

One small but instructive implementation detail: the UL editions are stored as a
**tuple of `(standard, edition)` tuples**, not a dict. The reason is that
`CodeCycle` is `frozen=True`, and a frozen dataclass must be hashable to be used
as it is here; a `dict` field would break hashability, but a tuple-of-tuples is
itself hashable. It is the kind of constraint that only shows up when you try to
freeze a dataclass with a mapping in it, and the tuple is the idiomatic way out.

These editions surface in three places downstream — the reviewer system prompt,
the verifier system prompt, and the methodology note in the exported report — and
the rendering logic for each **silently drops empty edition fields**, so a future
cycle that does not populate every standard degrades gracefully instead of
emitting blank lines. Those three *uses* belong to [**Ch 5**](05_review_engine.md), [**Ch 10 —
Verification II**](10_verification_grounding.md), and [**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md) respectively;
this chapter owns the data they read.

`DEFAULT_CYCLE = CALIFORNIA_2025` is the only cycle in `AVAILABLE_CYCLES`. The
2022 cycle was removed and must not be reintroduced. There is a quiet but
important coupling here: the cycle *label* is part of the verification cache key,
so bumping the cycle naturally invalidates every prior cached verdict — operators
do not clear the cache by hand when the cycle changes. The cache-key mechanics
are [**Ch 10**](10_verification_grounding.md)'s; what matters for configuration is that changing one string in
this file has a deliberate, system-wide effect.

## Web tool configuration

The verifier's two server tools — `web_search` and `web_fetch` — are built here,
so their *configuration* is the control plane's, even though their *use* in
routing is [**Ch 9 — Verification I**](09_verification_routing.md)'s and their *grounding* is [**Ch 10**](10_verification_grounding.md)'s.

`build_web_search_tool` pins the tool type to `web_search_20260209`, sets a US /
California approximate user location (so results lean toward the right
jurisdiction), and attaches a **blocked-domains** list. The list is blocked-only
by necessity — the tool does not support mixing `allowed_domains` and
`blocked_domains` — and it strips out aggregators, Q&A forums, other LLMs'
outputs, DIY content farms, social media, and general encyclopedias: sources that
are not "authoritative for code compliance." California priority sources are
expressed as guidance in the verifier prompt rather than as an allow-list.

The per-severity search budget map, `_SEVERITY_MAX_USES`, also lives here:

| Severity | `max_uses` |
|---|---|
| CRITICAL | 8 |
| HIGH | 7 |
| MEDIUM | 5 |
| GRIPES | 3 |
| *(unknown)* | 5 |

`web_search_max_uses_for_severity` is the accessor, and it falls back to 5 for an
unrecognized severity "so a misclassified finding still gets a reasonable
budget." The map is flat across profiles — a CRITICAL claim gets 8 searches
whether it is a California-AHJ question or a manufacturer question. *How* the
router spends this budget is [**Ch 9**](09_verification_routing.md); the numbers are defined here, and they are
defined *once* so the tool builder and the verifier read the same figures.

`build_web_fetch_tool` (type `web_fetch_20260209`) mirrors the same blocklist — "a
domain we won't search is a domain we won't fetch either" — enables citations so
fetched URLs land in the same source-grounding partition as search results, and
caps fetched-page content at `WEB_FETCH_MAX_CONTENT_TOKENS` (50,000) so one fetch
on a giant code-publisher page cannot dominate the verifier's input window. Its
per-request fetch budget is just 3, deliberately lower than the search budget,
because "a verification call typically needs at most one or two full-page fetches
… more than that is a sign the model is spinning."

There is one hard-won rule embedded in this builder, and it is important enough
that the full story is told twice elsewhere ([**Ch 10**](10_verification_grounding.md) and [**Ch 17 — Evolution &
Lessons**](17_evolution_and_lessons.md)): **web fetch is generally available and takes no `anthropic-beta`
header.** The tool dict alone enables it. An *earlier* version of the code
attached a `web-fetch-2026-02-09` beta header on the theory that it was "harmless
when GA, required when gated." That was wrong on both counts — an unrecognized
beta value is rejected with HTTP 400, not silently ignored — and it crashed every
verification on the common path. The builder now attaches no beta header for web
fetch, and the source carries a long comment explaining exactly why, so the
mistake cannot be reintroduced by someone "fixing" a missing header.

## Secrets and filesystem paths

Two small files round out the module. `app_paths.py` centralizes where the
program keeps things: `app_config_dir()` resolves the platform config directory
via `platformdirs` (creating it on demand), and `api_key_paths()` returns the
candidate API-key locations in priority order — the config directory first, then
the executable/source directory, so the legacy "drop a key file next to the .exe"
convention keeps working.

`api_key_store.py` resolves the actual key with a clear preference order: an OS
**keyring** first (keychain / credential manager / kwallet — at least as safe as
a plaintext file and resistant to a stray `cat` of the config directory), falling
back to the plaintext file only when the keyring returns nothing. Keyring is an
*optional* dependency; the import and every keyring call are wrapped so that on a
headless CI box or a minimal Linux install with no backend, the file fallback
always works. And when it does read a fallback file on POSIX, it lazily tightens
that file's permissions to `0600` — so an in-place upgrade improves an existing
key file's posture from a stale `0644` without making the user re-enter anything.
The whole file embodies the same instinct as the rest of the module: prefer the
safer path, but never lock a working configuration out over a hardening tweak.

## The environment variables

The operator's surface is a small, deliberately boring set of variables — model
overrides plus a handful of switches for cache control and rollback. They are the
documented, supported way to retune the program without editing code.

| Variable | Default | Effect |
|---|---|---|
| `SPEC_CRITIC_REVIEW_MODEL` | Opus 4.8 | Override the review model |
| `SPEC_CRITIC_VERIFICATION_MODEL` | Sonnet 5 | Override the verifier initial-pass model |
| `SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL` | Opus 4.8 | Override the escalation model |
| `SPEC_CRITIC_TRIAGE_MODEL` | Haiku 4.5 | Override the triage model |
| `SPEC_CRITIC_ELEMENT_IDS` | on | Disable to revert to legacy plain-body spec rendering |
| `SPEC_CRITIC_VERIFICATION_CACHE_PERSIST` | on | Disable to keep the verification cache in-memory only |
| `SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS` | 60 | Age-based cache pruning; explicit `0` restores no-expiry; malformed/negative falls back to 60 |
| `SPEC_CRITIC_CACHE_PATH` | `~/.spec_critic/verification_cache.json` | Override the on-disk cache file (`~` and `$VAR` expanded) |
| `SPEC_CRITIC_TRACE` | on | Disable to stop writing the forensic JSONL trace |
| `SPEC_CRITIC_TRACE_DEEP` | off | Enable to record per-stream chunks, full snippet bodies, raw responses, inline prompts |
| `SPEC_CRITIC_TRACE_DIR` | `~/.spec_critic/traces/` | Override the trace root (`~` and `$VAR` expanded) |

The boolean flags share one convention: they are *enabled by default* and accept
`0` / `false` / `no` / `off` (case-insensitive) to **disable**; anything else
leaves the default behavior in place. A typo therefore fails safe — `TRACE=yes`
does not accidentally disable tracing, because only the four explicit
disable-tokens turn a flag off.

There is a small structural wart worth flagging honestly: that disable-token set
(`_DISABLE_TOKENS = {"0", "false", "no", "off"}`) and its `_env_flag_disabled`
helper are **re-declared independently in three modules** — `tracing/config.py`,
`verification/verification_cache.py`, and `review/prompt_serialization.py` — rather
than living in one shared place. The convention is consistent today because three
copies happen to agree; nothing structural keeps them in sync, and a fourth flag
added with a fourth copy could quietly drift. The cache-control flags belong to
[**Ch 10**](10_verification_grounding.md), element-ids to [**Ch 5**](05_review_engine.md), and the trace flags to [**Ch 14**](14_observability.md); the
*convention* is what this chapter owns, and the convention has no single home. The
model-id overrides, by contrast, are genuinely centralized here — they are the
only env parsing that lives in `api_config.py`.

## Design tensions & what's still being perfected

This module is the program's safety net, and an honest accounting has to admit
where the net has holes. Four are worth naming — two of them since addressed, kept
here because the *risk class* is permanent even when a specific instance is patched.

**The sharp edge of safe degradation (audit P0-3, since addressed).** The same
degrade-to-safe-defaults mechanism that protects an operator from a typo can also
*degrade a genuinely newer, better model*. Set `SPEC_CRITIC_REVIEW_MODEL` to an id
the registry doesn't know — a future successor like `claude-opus-4-9` — and the
program drops adaptive thinking, drops `xhigh` effort, drops the 300k
extended-output path, and clamps the baseline output cap to 64k, because
`OPUS_MODELS` and the capability registry only know the ids they were taught. The
reviewer thinks they upgraded; they downgraded. This *used* to be the module's most
uncomfortable hole — the degradation was completely silent, and for a tool whose
entire reason to exist is making uncertainty *visible*, silence was the wrong
default. `model_capabilities` now emits one `WARNING` per unrecognized id (deduped
via `_WARNED_UNKNOWN_MODELS` so the per-request hot path can't spam the log) naming
the conservative caps it fell back to, so a stale whitelist that under-powers a
newer model is visible rather than invisible. The deeper remedy is unchanged:
adding a model to the whitelist is a small change (register it in
`_MODEL_CAPABILITIES` — output ceiling and `xhigh` eligibility now live on the
entry itself; only a new *Opus* id also joins `OPUS_MODELS`, for the
escalation-tier effort bump), and the work is *remembering to do it* before the
default goes stale.

**The hardcoded beta header (audit P0-4, since addressed).** `output-300k-2026-03-24`
is a hardcoded constant, and an *unrecognized* `anthropic-beta` value is rejected by
the API with HTTP 400 — the *exact same risk class* as the retired web-fetch header
that already crashed this codebase once. The blast radius is smaller (only inputs
over 200k tokens take the 300k path), but the failure mode would be identical. The
fix the audit pointed toward now exists: `_create_review_batch` attempts the beta
first and, on a beta-header rejection (`_is_beta_header_rejection` — a 400 naming the
`anthropic-beta` header), clamps each request's `max_tokens` back to the model's
standard ceiling (`_clamp_requests_to_model_ceiling`) and re-submits on the non-beta
path. A retired header now degrades output on very large specs — which the
review-stage failure surfacing already reports — rather than crashing the whole run.

**Hand-maintained edition strings.** The pinned editions in `CALIFORNIA_2025` are
*transcribed* from the California Building Standards Commission adoption matrix.
Both the dataclass docstring and the inline comments warn that they are a
"best-effort snapshot" that must be verified against the published matrix before
changing. Nothing in the program checks them — a wrong edition string produces a
confidently-wrong finding (flag a compliant edition, or miss a stale one), which
is precisely the failure this whole tool exists to prevent, hiding in the one
place no model call can catch it. This is a quiet correctness dependency on a
human keeping a table in sync with a state agency.

**Two minor hardening gaps (audit P2-2, P2-3).** `safe_local_estimate` is not
clamped to `≥ 1.0`. The configured factors are all `≥ 1.10`, so it is fine as
shipped — but a future sub-1.0 misconfiguration would silently turn the safety
*pad* into a danger *discount*, shrinking the estimate below the real count. And
`assert_extended_output_allowed` compares `max_tokens` against
`MAX_OUTPUT_TOKENS_OPUS` (128k) regardless of which model is selected; now that
Sonnet also carries the 300k beta, a model-derived threshold would be tidier. Both
are benign today and called out so they do not surprise someone later.

The thread running through all four is the same one running through the whole
module: the program is *very* good at not crashing, and that strength is exactly
what makes its quiet failures quiet — no one notices a request that degrades
without complaint until the findings get thinner after an "upgrade."

## How it connects

The control plane touches nearly every subsystem, always as the *definer* of a
policy someone else *consumes*:

- [**Ch 5 — The Review Engine**](05_review_engine.md) consumes the prompt-cache breakpoints, the
  pinned-edition reviewer prompt, and the element-ids flag.
- [**Ch 6 — Batch Processing**](06_batch_processing.md) consumes the extended-output gating and the batch
  service tier (`batch_service_tier()` returns `"auto"` for priority capacity).
- [**Ch 7 — Orchestration**](07_orchestration.md) owns the token-preflight call that *raises*; this
  chapter owns the limits and the estimator it checks.
- [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md) consumes the (non-overridable) cross-check
  model default.
- [**Ch 9 — Verification I**](09_verification_routing.md) consumes the severity search-budget map and the mode
  routing it feeds.
- [**Ch 10 — Verification II**](10_verification_grounding.md) consumes the cache key (with the cycle label), the
  pinned editions in the verifier prompt, and tells the full web-fetch-header
  story.
- [**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md) consumes the pinned-editions
  methodology note.
- [**Ch 14 — Observability**](14_observability.md) consumes the trace env vars and cache-usage
  extraction.
- [**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md) and [**Ch 17 — Evolution & Lessons**](17_evolution_and_lessons.md)
  carry audit findings P0-3 / P0-4 and the lessons behind them.

## Key takeaways

- The control plane is five files of **single-source-of-truth policy** —
  `api_config.py` (models, caps, caching, effort/thinking, search budgets, web
  tools), `code_cycles.py` (pinned editions), `tokenizer.py` (counting + limits),
  `api_key_store.py`, and `app_paths.py`. They run no API calls; they shape every
  call others make.
- The governing principle is **fail toward a smaller, valid request.** Unknown
  models degrade to all-flags-off / 200k context / 64k output; unknown phases get
  the most conservative cap; the local token estimate is padded upward; effort and
  thinking keys are omitted, never nulled.
- That safety has a **sharp edge** (P0-3): the same reflex silently *degrades a
  newer, better model* — pointing review at an unlisted Opus successor strips
  thinking, effort, and the 300k path and clamps output to 64k, with no warning.
  Whether an unknown id should warn loudly is the module's central open question.
- The **300k extended-output path** is batch-only, fires only for inputs ≥ 200k
  tokens on a capable model, and is gated by a **hardcoded beta header** with no
  graceful 128k fallback — the same stale-header risk that already bit the
  codebase once (P0-4).
- **Prompt caching** uses a 1-hour TTL (a deliberate 2× write cost) because batch
  waves outlive the 5-minute default; triage is the only uncached phase, its
  prompt being below the Haiku cache minimum.
- The **pinned editions** in `CALIFORNIA_2025` are hand-transcribed from the
  California adoption matrix and verified by a human, not the program — a quiet
  correctness dependency. `DEFAULT_CYCLE` is the only cycle; its label is wired
  into the verification cache key.
- The **environment variables** are model overrides plus cache/trace switches;
  booleans are on-by-default, disabled only by `0`/`false`/`no`/`off`. The
  cross-check model is the one phase with *no* override.
