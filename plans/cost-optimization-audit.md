# Cost optimization audit — models, effort, and transports

| | |
|---|---|
| **Date** | 2026-10-08 |
| **Base** | master `4fb2a2e` (Spec Critic 3.11.0, Anthropic SDK 1.11.0) |
| **Scope** | Every Claude API call the app makes (`src/`), the exported report's Ask AI chat, and the separate applier's `--assist` tier, on both review transports (batch, the default, and real time). |
| **Platform** | First-party Claude API (Message Batches, prompt caching, `web_search_20260209`, `web_fetch_20260209`). |
| **Live evaluation** | None. No API key was in the session, and no model request was sent. Spend: $0.00. |
| **Data** | Code read only. No diagnostics export or usage log was available, so savings are given as relative buckets (large / medium / small), never as dollar figures for your bill. |
| **Quality bar** | Four eval harnesses already exist and are pre-registered but have never been run live: `evals/model_effort.py` (EX-03: review and verification), `evals/package_review.py` (cross-check and compliance coverage), `evals/project_context_cache.py` (EX-01), and `evals/research_reuse.py` (EX-05). Every lever marked *tradeoff* below needs one of them run before it is applied. |
| **Provider pages read (2026-10-08)** | Pricing; Optimizing for cost and intelligence; Effort; Models overview; the Opus 5.5, Sonnet 5.5 and Haiku 5.5 model pages; What's new in Haiku 5.5; Web search tool; Web fetch tool; Tool reference. Remembered rates were not used. |

## Short answers

1. **Effort levels.** Every phase's level matches Anthropic's current guidance for its model except one: the applier's `--assist` tier sends no effort to Sonnet 5.5 (so it runs at the API default, `high`) with a 2,000-token output cap that thinking can use up. The review at Opus 5.5 `medium` is the right default. Cross-check and compliance at Sonnet 5.5 `high` are what the guidance says for non-agentic work, but they were chosen for Sonnet 5, and Sonnet 5.5's levels are recalibrated, so `medium` there is worth measuring.
2. **Haiku 5.5.** It is already on the one job it clearly fits (verification triage). The other good fit is the applier's `--assist` tier. Drawing impact and the cheap `strict_structured` verification mode are possible but need a comparison first. Research, review, cross-check, compliance, and standard or deep verification are not Haiku work.
3. **Opus 5.5 → Sonnet 5.5.** Opus runs the per-spec review (and its repair), the deep and escalation verification tier, and the chat default. The review is the one place a switch could matter on the bill, and it is the one place the evidence says not to switch on price alone. Test Sonnet 5.5 at `high` (its documented starting point for non-agentic work) with the EX-03 harness. The escalation tier is more a quality question than a cost one: Sonnet 5.5 could use web fetch there, while the app keeps web fetch off for Opus 5.5 until a live check confirms support. The chat default is your call.
4. **Where the money still is.** The biggest no-quality-change levers are: batching the cross-check and compliance passes on batch runs (today they run at full price even on a batch run), reusing location research on repeat runs of the same project (EX-05, built, off), and caching the Project Context on per-spec reviews (EX-01, built, off), which pays reliably in real time once the first request is sent alone.

## 1. What every call runs today

| Call | Model | Effort | Thinking | Transport on a batch run / real-time run | Prompt cache | Server tools |
|---|---|---|---|---|---|---|
| Per-spec review | Opus 5.5 | `medium` (phase `high`, held to the Opus ceiling) | adaptive (always on) | batch / streaming | tool + system, 1 h; Project Context not cached (EX-01 off) | none |
| Review repair (truncated review) | Opus 5.5 | `low` | adaptive | batch / streaming | same | none |
| Verification triage | Haiku 5.5 | `medium` | adaptive; forced tool call suppresses up-front thinking | synchronous / synchronous | none | none |
| Verification, `strict_structured` | Sonnet 5.5 | `low` | key omitted (adaptive stays on) | batch waves / streaming | tool + system, 1 h | web search (3–8 by severity) |
| Verification, `standard_reasoning` | Sonnet 5.5 | `medium` | adaptive | batch waves / streaming | same | web search + web fetch (3) |
| Verification, `deep_reasoning` (CRITICAL jurisdictional first pass) and escalation (CRITICAL/HIGH not grounded) | Opus 5.5 | `medium` | adaptive | batch wave / streaming | same | web search only (web fetch gated off) |
| Location research (data-center modules) | Sonnet 5.5 | `high` | adaptive | streaming / streaming, before any review spend | system + tools 1 h; resumes 5 min | web search 12–28 and web fetch 5–9 per conversation |
| Cross-check | Sonnet 5.5 | `high` | adaptive | **streaming / streaming** (no batch path) | system + tool 1 h; the corpus is not reusable | none |
| Compliance (data-center modules) | Sonnet 5.5 | `high` | adaptive | **streaming / streaming** | same | none |
| Drawing impact | Sonnet 5.5 | `high` | adaptive | streaming / streaming | system + tool 1 h | none |
| EX-06 coordination (off) | Sonnet 5.5 | `high` | adaptive | streaming | system + tool 1 h | none |
| Report Ask AI chat (browser) | Opus 5.5 default; Sonnet 5.5 and Haiku 5.5 selectable | `medium` default; `low`/`high` selectable | adaptive, summarized | synchronous | report prefix, 1 h | web search 5; web fetch on Sonnet only |
| Applier `--assist` | Sonnet 5.5 | **not sent → API default `high`** | **not sent → adaptive on** | synchronous | none | none (client tools) |

Sources: `src/core/api_config.py:55-96` (models), `:1036-1068` (effort and the Opus ceiling), `:1259-1308` (cache policy, 1 h TTL), `:1526-1551` (search budgets); `src/verification/verification_modes.py:197-227`; `src/review/review_request_builder.py:110`; `src/cross_check/cross_checker.py:513`, `src/compliance/compliance_checker.py:1003`, `src/drawing_impact/impact_synthesizer.py:559` (synchronous streams); `src/output/html_report_exporter.py:3930-3949`; `applier/assist.py:47,360-375`.

A full Hyperscale run makes 8 research conversations (4 shared jurisdiction dimensions plus one supplement per discipline), with budgets of up to 182 searches and 60 fetches in total. (The EX-05 record's "18 dimensions, up to 314 searches" predates the shared-jurisdiction core.)

## 2. Where the tokens go (estimated from the code)

The static prompt prefixes are small and already cached: the review system prompt is about 11–16k characters (roughly 3–4k tokens); verification's is 9–11k characters, compliance's about 8k, cross-check's about 3k, research's about 2k. The 1-hour vs 5-minute TTL question on them is worth cents per run. The money is in per-request content:

| Bucket | Why it is large | Already done well |
|---|---|---|
| **Large: per-spec review** | Every spec body (unique, never cacheable), Opus output (thinking plus findings), and, on data-center modules, the rendered research profile and any drawing-analysis digest inside Project Context, re-sent at the full input rate with every spec. | Batch by default (50% off); `medium` effort; static prefix cached. |
| **Large: verification** | Search results and fetched pages enter as input on every server-tool iteration; Sonnet thinking; $10 per 1,000 searches, which the batch discount never touches. | Batch waves; Haiku triage and keyword local-skip; per-run single-flight; a 60-day cross-run verdict cache; severity-scaled search budgets (live captures used 0–4 searches per finding). |
| **Medium to large: cross-check + compliance** | Each sends the whole module corpus (plus Project Context), so a data-center module pays for its corpus twice (California once, through cross-check), at full price, even on a batch run. | Chunking only when needed. |
| **Medium: research** | Up to 8 long search conversations at `high` effort, at full price, on every run, including re-runs of the same project. | Shared jurisdiction core (researched once per program, not per module). |
| **Small** | Triage (Haiku), drawing impact, review repair, chat per question, applier assist. | — |

To turn these buckets into percentages of your bill at no API cost: run one representative Hyperscale package on the batch transport and save the Run Diagnostics as JSON. Its "By operation" line already prices every call by phase, with cache reads, cache writes, and searches split out.

## 3. Effort levels, phase by phase

Anthropic's current per-model guidance (Effort page, 2026-10-08):

- **Opus 5.5:** default `medium`; "Run an effort sweep on your own evals rather than carrying settings over from an earlier model."
- **Sonnet 5.5:** default `high`, levels recalibrated from Sonnet 5. "Start with `high` unless your workload is agentic or latency-sensitive. For agentic coding and multistep tool use, start with `medium` for well-specified tasks and move to `high` for harder or longer ones. For chat and other latency-sensitive work, start with `medium` or `low`." Run a fresh sweep rather than carrying the Sonnet 5 setting over.
- **Haiku 5.5:** default `medium`. "Use `low` … for chat, short tool tasks, and simple, high-volume requests. In long agent prompts, the model is more likely to skip a search, stop early, or skip a check at `low`. Use `high` for knowledge work, longer agent tasks, and strict instruction following."

Measured (Cost Optimization page): on SWE-bench Pro, Opus 5.5 at `medium` scored about 2.5 points below `high` at about 70% of its cost, and `low` about 8 points below at about a third. On research and knowledge-work benchmarks, `low` gave up 1–3 points for a third to a half off, and `medium` matched the default at 70–87% of its cost.

| Call | Current | Verdict |
|---|---|---|
| Review, Opus 5.5 | `medium` | **Right.** It is Opus 5.5's default and Anthropic's recommended starting point. Do not lower it: `low` is a direct capability trade on the core task. If anything, the arm worth measuring for rigor is `high` (one step up; about 1.4× the cost per task on Anthropic's coding benchmark), not EX-03's `xhigh` (two steps up). |
| Review repair, Opus 5.5 | `low` | Right: a bounded recovery that must only re-emit findings. |
| Verification `standard_reasoning`, Sonnet 5.5 | `medium` | Right: well-specified multistep tool use. |
| Verification `strict_structured`, Sonnet 5.5 | `low` | Right: narrow, low-stakes claims. |
| Deep and escalation, Opus 5.5 | `medium` (ceiling) | Fine for cost. These are by definition the hardest claims, so whether `high` would ground more of them is a quality question for EX-03's tier view, not a saving. |
| Research, Sonnet 5.5 | `high` | Defensible: 12–28-search conversations are the "harder or longer" multistep case. `medium` makes fewer, terser tool calls and less thinking; worth measuring only after research reuse (lever 2), because research feeds compliance and the verifier's governing basis. |
| Cross-check and compliance, Sonnet 5.5 | `high` | Matches the guidance for non-agentic work, but these levels were set for Sonnet 5 (`xhigh` → `high`) and Sonnet 5.5's levels are recalibrated. **A `medium` arm is worth measuring** (lever 6): both passes think over a very large input. |
| Drawing impact, Sonnet 5.5 | `high` | Fine. One bounded call per run; not worth an evaluation cycle. |
| Triage, Haiku 5.5 | `medium` | Fine. The forced tool call suppresses up-front thinking, so effort barely moves cost here. |
| Ask AI chat | `medium` default | Fine; the reader can pick `low` or `high`. |
| Applier `--assist`, Sonnet 5.5 | none sent (→ `high`), adaptive thinking, `max_tokens` 2,000 | **Mismatch.** A short locating task runs at `high`, the level Anthropic gives for non-agentic work rather than short tool tasks, and thinking counts toward a 2,000-token cap. A turn that thinks past the cap stops with `max_tokens` before any tool call, and the loop (which never reads `stop_reason`) reports "assist ended without calling a tool". Fix in lever 4. |

## 4. Haiku 5.5: where it fits

Anthropic positions Haiku 5.5 for "high-volume, latency-sensitive tasks such as classification, extraction, and routing" and subagent work "with checkable outputs". It costs $0.10 / $0.50 per million tokens up to a 100,000-token prompt and $0.50 / $2.50 above it (the higher tier applies to the whole request). On GPQA Diamond it scored 85% against Opus 5.5's 91%, at about a twentieth of the cost per question.

| Call | Haiku 5.5? | Why |
|---|---|---|
| Verification triage | **Already on Haiku 5.5** | Classification with a hard safety net outside the model (CRITICAL/HIGH and code-citing findings are never eligible). The right fit. |
| Applier `--assist` | **Good fit** | A short tool loop that only chooses an element id among validated candidates, where declining is the safe failure. Haiku 5.5 at `medium` is about 20× cheaper per token than Sonnet 5.5; the conservative alternative is Sonnet 5.5 at `low`. Volume is small either way. |
| Drawing impact | Possible | Grounded synthesis over text the run already holds; unknown finding ids are dropped at parse time. Small dollars; compare on a few saved runs before switching. |
| Verification `strict_structured` | Possible, needs an eval | Cheap, narrow claims. But verdict errors are trust-critical (false CONFIRMED / false DISPUTED), and search fees, which do not depend on the model, are a large share of a strict call's cost, so the saving is smaller than the price ratio. Confirm web search support first: the docs say dynamic filtering is available "with Claude 4.6 and later models" and Haiku 5.5 adds programmatic tool calling, but no page names Haiku 5.5 for `web_search_20260209`. Check `capabilities.server_tools.web_search.supported` from the Models API and send one probe. Web fetch is not documented for Haiku 5.5; the app's flag is correctly off. |
| Review, review repair | No | The core intelligence task. |
| Research | No | Long multistep search where Haiku at lower effort "is more likely to skip a search, stop early, or skip a check", and its output becomes the compliance profile and the verifier's governing basis. |
| Cross-check, compliance | No | Deep reasoning over very large inputs, which would also land on Haiku's higher (>100k-token) tier. |
| Standard and deep verification | No | Substantive and high-severity claims. |

**Do not build a cascade or advisor pairing.** Anthropic measured Haiku 5.5 and Sonnet 5.5 executors with an Opus 5.5 advisor: zero consults in 198 questions, no gain, and the advisor tool definition alone added 12% and 25% to their cost per question.

## 5. Opus 5.5 call sites and the Sonnet 5.5 question

Sonnet 5.5 is exactly half Opus 5.5's per-token price ($2 / $10 against $4 / $20; batch $1 / $5 against $2 / $10; cache reads $0.10 against $0.20). Per-token price does not predict cost per task: Sonnet at `high` may think more than Opus at `medium`.

| Opus call | Switch to Sonnet 5.5? | At what effort | Evidence and how to decide |
|---|---|---|---|
| **Per-spec review** (and its repair) | **Not on price alone. Measure.** | `high` — Anthropic's starting point for non-agentic work, and what the app already sends to a non-Opus review model (`SPEC_CRITIC_REVIEW_MODEL=claude-sonnet-5-5`; the `medium` ceiling applies to Opus only). A second arm at `medium`. | For: Sonnet 5.5 tied Opus 5.5 at 91% on GPQA Diamond (Opus led by about 2 points on questions both answered). Against: Anthropic's starting recommendation for most workloads is Opus 5.5 at `medium`, and Opus 5.5 is documented as "more detail-oriented on large inputs … without more false-positive flags" and "much less likely … to state a figure or cite a source the inputs don't support", which describes spec review. Decide with EX-03's review experiment plus a Sonnet arm (lever 8). Its held-out split has 9 severe defects per repetition, enough to catch a large regression but not to prove equivalence; a Sonnet switch should also pass a side-by-side on a few of your own recent packages where you know the answers. |
| **Deep reasoning and escalation** | **Probably, but for evidence quality more than dollars.** | `high` | Escalations are a small share of calls. The real gap is tooling: Sonnet 5.5 gets web fetch, while the app keeps it off for Opus 5.5 pending a live check. A Sonnet escalation at `high` needs two code changes (EX-03 records the first): the gate never escalates to the initial verifier's model (`verification_prescreen.py:228`), and effort today comes from the phase (`medium` for every verification pass), so the escalation tier needs a level of its own. Cheapest first step: run `tests/test_network_smoke.py::test_opus_5_5_web_fetch_probe_smoke` (a few hundred tokens). The web-fetch page's code samples now use `claude-opus-5-5` with `web_fetch_20260318`, which suggests support but is not a statement of it. |
| **Ask AI chat default** | Your call | `medium` (`low` for quick questions) | Halves the per-token price and adds web fetch. The first question writes the report prefix to the 1-hour cache at 2× the input rate ($8 per million tokens on Opus, $4 on Sonnet). It is your reading experience, so it is not a decision to make for you. |

## 6. Ranked shortlist

Ranked by savings ceiling, not application order. Section 7 gives the order to apply them in.

| # | Lever | Type | Ceiling | Runs | Data |
|---|---|---|---|---|---|
| 1 | Batch the cross-check and compliance passes | free win (same requests, same model) | medium–large: 50% of both passes' token cost | batch runs | code estimate |
| 2 | Reuse location research on repeat runs (EX-05) | free win with a freshness policy | large on re-runs within 30 days; none on first runs | both | code estimate |
| 3 | Cache Project Context on per-spec reviews (EX-01), sending the first request alone in real time | free win in real time; needs measurement in batch | medium on data-center modules or with a drawing digest; about zero on California runs without context | both | code estimate |
| 4 | Fix the applier `--assist` request | free win (reliability) | small | applier | code read |
| 5 | Correct the Sonnet 5.5 cache-read price (done here) | accounting accuracy | none (estimates only) | both | provider pages |
| 6 | Cross-check and compliance at Sonnet 5.5 `medium` | tradeoff | medium | both | needs `evals/package_review.py` |
| 7 | Research at Sonnet 5.5 `medium` | tradeoff | small–medium (fewer searches and less thinking) | both | needs an eval; apply after 2 |
| 8 | Per-spec review on Sonnet 5.5 at `high` | tradeoff | large if quality holds | both | needs EX-03 plus a new arm |
| 9 | Escalation and deep tier on Sonnet 5.5 at `high` with web fetch | tradeoff (quality-led) | small | both | needs EX-03's tier view and two code changes |
| 10 | `strict_structured` verification on Haiku 5.5 | tradeoff | small–medium | both | needs a probe and a verification eval |

## 7. Proposed changes, in application order

Free wins first, then effort, then model choice. One lever per pull request so each one's effect can be measured on its own.

1. **Correct the Sonnet 5.5 cache-read price** (lever 5) — **applied in this pull request.** `src/core/pricing.py` priced Sonnet 5.5 cache reads at $0.20 per million tokens (0.1×). Anthropic's pricing page and the Sonnet 5.5 model page both list $0.10 (0.05× input, the same multiple as Opus 5.5); Sonnet 5 stays at $0.20. The run estimate, the trust dialog's price table, and `docs/TRUST.md` overstated Sonnet 5.5 cache reads twofold. Spend is unchanged; the figures you would use to judge the other levers are now right.
2. **Fix the applier `--assist` request** (lever 4, proposed). Send an explicit effort (`low` on Sonnet 5.5, or move the tier to Haiku 5.5 at `medium`), raise `ASSIST_MAX_TOKENS` from 2,000 to about 16,000 (output is billed as generated, so a higher cap costs nothing unless used), and read `stop_reason` so a `max_tokens` stop is reported as a truncation rather than "ended without calling a tool". This changes a documented trust fact ("effort, thinking and temperature omitted; 2,000 output tokens"), so it updates `docs/TRUST_CLAIMS.md`, `src/gui/trust_content.py`, and the dossier in the same change.
3. **Research reuse on repeat runs** (lever 2, built). Turn on `SPEC_CRITIC_RESEARCH_CACHE=reuse` when you re-run a project (addendum, revision). The cache key covers the project, client, specification-derived signals, model, prompts, tools and budgets; entries expire after 30 days, and an entry retires early when a date its research describes as upcoming has arrived. The shared jurisdiction core reuses even when your specs change, because it reads no specification text. The research is exactly what the earlier run produced, so the only risk is staleness within the age limit. A "reuse research from <date>?" prompt in the GUI would make the choice visible per run. This is a freshness policy, so it is your decision.
4. **Project Context caching on per-spec reviews** (lever 3, built behind `SPEC_CRITIC_PROJECT_CONTEXT_CACHE`). On a data-center module, the research profile (and any drawing digest) is re-sent with every spec at the full input rate.
   - *Real time:* today the first `workers` streams start together and all write the cache, which is why EX-01 says the one-hour arm cannot pay below 9 specs. Sending one request first and releasing the rest once its response starts streaming (seconds of added latency per module) makes it one write and N−1 reads at 0.05× (Opus 5.5). EX-01 notes this as not implemented; it is the change that makes the switch pay.
   - *Batch:* item concurrency decides the hit rate (the provider cites 30–98%), and the one-hour breakpoint pays only below about a 49% write share. Measure it on a few ordinary batch runs with the switch on: every review attempt's diagnostics already record cache reads and writes per TTL. The worst case on those runs is the Project Context part of review input costing up to 2× (one-hour) or 1.25× (five-minute). Promote it on batch only if the records show the saving.
5. **Batch cross-check and compliance on batch runs** (lever 1, proposed). Both stream at standard price today, after round-1 verification. Their inputs depend on that order: cross-check leaves DISPUTED review findings out of `<already_identified>` (`pipeline.py:2835`), and compliance also reads cross-check's findings (`pipeline.py:3053`). Keeping the inputs identical means two sequential batch submissions (cross-check, then compliance), which adds two batch turnarounds to a run that already waits on two verification batches. Submitting both in one batch saves one turnaround, but compliance would no longer see cross-check's findings and could repeat one. Simplest robust shape: submit, poll with a timeout, and on timeout cancel the batch (unstarted items are not billed) and fall back to today's streaming call; chunked passes become several items in one batch. Offer it as an operator setting ("cheaper, slower package passes") if the added wait matters to you.
6. **Measure cross-check and compliance at `medium`** (lever 6). Add an effort arm to `evals/package_review.py`, which already isolates state and reuses EX-03's pricing. Promote only if coverage holds on its dataset.
7. **Measure research at `medium`** (lever 7), after lever 2 is in place, with the applicability evaluation (`evals/dc_applicability.py`), so a thinner profile shows up as missed requirements.
8. **Measure the review on Sonnet 5.5** (lever 8). Add arms `review_model_sonnet_5_5_high` and `review_model_sonnet_5_5_medium` to `evals/model_effort.py` (each a single environment setting, like the existing escalation arm) with a rule fixed in advance: promote only if no severe defect is lost, trap hits are not higher, and cost per review falls by a margin worth a default change. Add a `review_effort_high` arm while there, since `high` is the one-step comparison.
9. **Escalation on Sonnet 5.5 `high` with web fetch** (lever 9). First run the Opus 5.5 web-fetch probe; if Opus 5.5 accepts web fetch, the gap that motivates this lever closes. Otherwise make the two code changes in section 5 and score EX-03's tier view.
10. **`strict_structured` on Haiku 5.5** (lever 10), only after the Models API and a probe confirm web search support, and only with verification cases that actually route to that mode.

## 8. Levers considered and skipped

| Lever | Why skipped |
|---|---|
| `response_inclusion: "excluded"` (`web_search_20260318` / `web_fetch_20260318`) | Drops completed search and fetch result blocks from the response. The grounding invariant accepts a citation only when its URL matches one retrieved in the conversation, so the app needs those blocks. It would break `CONFIRMED` / `CORRECTED` / `DISPUTED` grounding. |
| Lower verification search budgets | They are caps, and the live captures used 0–4 searches; lowering them risks budget-exhausted `INSUFFICIENT_EVIDENCE` without data showing waste. |
| 5-minute TTL on the static prefixes | The prefixes are about 1–4k tokens; the difference is cents per run, and batch waves need the 1-hour TTL anyway. |
| Caching the Haiku triage prefix | Even if the prefix now clears Haiku 5.5's cache minimum (not verified here; CLAUDE.md §7 deliberately makes no claim), at $0.10 per million input tokens a cache would save fractions of a cent per run. |
| Batching research | Up to 8 `pause_turn` continuations would become up to 8 batch waves before the review could even be submitted. Research reuse covers repeat runs instead. |
| One merged cross-check-and-compliance pass | Halves the corpus input but changes both tasks. |
| Lower review effort (`low`) | A direct capability trade on the core task (about 8 points below `high` on Anthropic's coding benchmark). |
| Advisor or cascade designs | Measured no gain (section 4). |
| Task budgets | Advisory, and the app's loops are already short and bounded; on Sonnet 5.5 the per-step budget countdown can read as a prompt injection. |
| Lower `max_tokens` caps | Output is billed as generated; a lower cap only truncates. The app sizes caps for thinking correctly. |
| Fast mode, Priority Tier, `inference_geo` | Premiums, not savings. The app sends none (`service_tier: "auto"` falls back to standard on these models). |

## 9. Batch runs and real-time runs

- **Batch runs (default).** Review and verification already take the 50% discount. What still runs at full price is research, cross-check, compliance, drawing impact, triage, and the small real-time tail of a verification wave. Biggest remaining levers: lever 1 (batch the package passes), lever 2 (research reuse on re-runs), lever 3 (Project Context caching, measured first).
- **Real-time runs.** Every token is billed at standard price by design: twice the batch rate on review and verification. The biggest levers are lever 3 in its real-time form (first request alone, then the fan-out) for data-center modules, and lever 2 for re-runs. Operationally, batch is the cheaper default for full packages; real time is worth its price for small or urgent checks.

## 10. Next steps

1. **Measured profile, free.** Save the Run Diagnostics as JSON from one representative Hyperscale batch run. That turns the buckets in section 2 into a percentage of your bill and decides which of levers 1, 3 and 4 to build first.
2. **Decide on research reuse** for re-runs (lever 2): a freshness policy only you can set.
3. **Choose the free-win changes to build:** the assist fix, the real-time first-request-alone fan-out for EX-01, and batched package passes.
4. **Approve a measurement budget** for the tradeoff levers (6–10). The harnesses exist; they need an API key, a spending cap, and the authorization each experiment record asks for.

## Sources

- Anthropic, *Pricing* — `https://platform.claude.com/docs/en/about-claude/pricing` (read 2026-10-08).
- Anthropic, *Optimizing for cost and intelligence* — `https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence` (read 2026-10-08; the page was read to character 100,000 of about 157,000).
- Anthropic, *Effort* — `https://platform.claude.com/docs/en/build-with-claude/effort` (read 2026-10-08).
- Anthropic, *Models overview* and the Opus 5.5, Sonnet 5.5 and Haiku 5.5 model pages; *What's new in Claude Haiku 5.5* (read 2026-10-08).
- Anthropic, *Web search tool*, *Web fetch tool*, *Tool reference* (read 2026-10-08).
- This repository: `plans/experiments/EX-01` through `EX-05`, `evals/`, and the source files cited above.
