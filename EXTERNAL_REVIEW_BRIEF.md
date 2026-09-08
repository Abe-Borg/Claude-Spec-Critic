# Spec Critic v3.5.0 — Independent Review Brief

**To:** the reviewing model
**From:** Claude (Opus 5), after a working pass over the repository at commit `25b9426`
**Date:** 2026-09-08
**Repository:** `Abe-Borg/Claude-Spec-Critic`, branch `claude/exciting-brown-1jr4cy` (2 commits ahead of `master`: a version bump and an About-dialog link — treat it as current `master`)

---

## 0. What I am asking you to do, and what I am not

You are being handed a mature, unusually well-documented Python desktop application. I have read it, run its test suite, and formed opinions. **My opinions are inputs to your review, not conclusions you should adopt.** Several of them are hypotheses I could not confirm without running the app against the live API, and I say so explicitly where that is the case.

**You are not being compelled to change anything.** "This code is in better shape than the brief implies, and here is why each proposed change is not worth making" is a completely acceptable — and genuinely useful — outcome. The author is a working fire-sprinkler and mechanical designer who built this in his spare time; churn has a real cost to him, and this codebase already carries an unusual density of hard-won invariants that a well-intentioned refactor could quietly break. Preserve first, improve second.

What I want from you:

1. **Your own exploration.** Do not take my file:line references as a map of the territory. Read what I did not read. I spent most of my attention on the cost/economics path and the verification trust model; I skimmed the GUI, the tracing subsystem, the Windows packaging/updater, the report exporters' rendering detail, and the eval harnesses.
2. **Your own conclusions**, including where you think I am wrong.
3. **A detailed report** (structure specified in §9).
4. **A proposed implementation plan** — *if and only if* change is warranted. Sequenced, each item with its own justification, risk, blast radius, verification strategy, and an explicit "skip this if…" condition.

---

## 1. What the application is

A CustomTkinter desktop app (Windows-targeted, runs from source on Linux/macOS) that reviews CSI-format construction specification `.docx` files with Claude and produces a Word report, a self-contained HTML report with an embedded browser chat, and a machine-readable JSON sidecar of proposed edits.

The critical framing: **it emits edit instructions but never applies them.** The locating-and-mutating write-back stack was deliberately deleted in v3.0.0. Anything that looks like vestigial auto-apply machinery is either already gone or is deliberately-retained telemetry for a future separate applier program.

The second critical framing: **it is a trust-model product, not a chatbot wrapper.** The whole architecture exists to make the model's uncertainty visible and auditable. A finding gets one of nine `ReportStatus` labels; a `CONFIRMED`/`CORRECTED`/`DISPUTED` verdict is *only* allowed if the model cited a URL that the `web_search` tool actually retrieved, enforced in four independent places. Cheapening the pipeline in a way that erodes that invariant is a failure, not a saving.

### Domain context you need

The author designs fire sprinkler systems for hyperscale data centers across the USA, and mechanical/plumbing for California K-12 (DSA) work. Two consequences:

- **Correctness has physical stakes.** A wrong NFPA 13 edition or a hallucinated code citation in a construction spec propagates into a real building. The trust model's conservatism is a domain requirement, not gold-plating.
- **The default program is California K-12 (`california_k12_mep`) and it is the byte-identical-output baseline.** Multiple golden tests pin CA-program output bytes exactly. Feature work for the data-center modules is expected to leave the CA path unchanged, byte for byte.

### Concrete scale

| | |
|---|---|
| Production Python | ~60,000 lines across ~100 files under `src/` |
| Tests | ~48,000 lines, 126 files, **3,158 tests, all passing hermetically in ~18 s** |
| Docs | `CLAUDE.md` 147 KB, `README.md` 50 KB, `docs/` ~166 KB, `TRUST_AUDIT.md` 28 KB |
| Largest modules | `verification/verifier.py` 3,705 · `output/html_report_exporter.py` 3,695 · `output/report_exporter.py` 3,634 · `orchestration/pipeline.py` 3,280 · `core/api_config.py` 1,464 |

Environment setup that worked for me (the container's `packaging` is Debian-managed and blocks a clean install):

```bash
pip install -q --ignore-installed packaging -r requirements.txt -r requirements-dev.txt
python -m pytest            # 3158 passed, 24 skipped, ~18s
```

`tkinter` is absent in this container, so GUI-dependent tests self-skip; the suite is designed for that and a meta-test (`tests/test_gui_import_hermeticity.py`) enforces the guard discipline. Evals: `python -m evals.runner` and `python -m evals.calibration.runner` — both hermetic, both exit 0 without a key.

---

## 2. Architecture, in the order data flows

Read `CLAUDE.md` first — it is the engineering reference and it is exceptionally good. It is also, as I show in §5, **not perfectly synchronized with the code**, so verify load-bearing claims against the source.

```
.docx files
  → input/extraction_cache.extract_multiple_specs_cached   (parallel, LRU, single-flight per file identity)
  → programs/routing                                       (multi-module programs only: assign each spec to 0..N modules)
  → input/preprocessor.preprocess_spec                     (deterministic detectors, zero API cost)
  → research/requirements_research                         (profile-enabled modules only, BEFORE review submission)
  → core/tokenizer preflight                               (raises above RECOMMENDED_MAX)
  → batch/batch.submit_review_batch  OR  review/realtime_review
  → pipeline._deduplicate_findings                         (exact-text SHA-256 keys, rf- ids)
  → verification/verifier                                  (round 1)
  → cross_check/cross_checker                              (synchronous, chunked by CSI division)
  → compliance/compliance_checker                          (synchronous, chunked; profile-enabled modules only)
  → verification/verifier                                  (round 2: cross-check + compliance findings)
  → drawing_impact/impact_synthesizer                      (only when a drawing digest is in Project Context)
  → output/report_exporter + html_report_exporter + edit_sidecar
```

The orchestration entry point for a single module is `src/orchestration/pipeline.py`; `run_batch_collection_headless` (~line 3180) is the clearest single view of the collect DAG. `src/orchestration/program_pipeline.py` is the composite scheduler for multi-module programs, with bounded concurrency at three levels (module preparation, module collection, and a global synchronous-API-call permit pool).

### The four structural abstractions

1. **`ReviewModule`** (`src/modules/base.py`, 1,094 lines) — a frozen dataclass holding *all* domain content for one reviewable discipline: code cycle, every prompt content slot, detector vocabulary, verification-routing keywords, source tiers, cross-check chunk map, report wording, research dimensions, compliance persona, wrong-polity token rules. The engine contains no domain content. `validate_module_registry` runs at import and fails app startup on a malformed module — including running every few-shot JSON example through the real parser.
2. **`ProgramDefinition`** (`src/programs/`) — groups modules behind one GUI choice; `programs/routing.py` deterministically assigns each spec to modules by CSI number/title/content.
3. **Phase registry** (`src/core/api_config.py`) — one place for per-phase output caps, effort levels, cache policy, and model defaults. Ten phases. An unregistered phase silently degrades to the 16k verification cap, which is a documented footgun.
4. **`VerificationRoutingDecision`** (`src/verification/verification_routing.py`) — one pure function maps (finding, severity, profile, cache state) to a full policy bundle: mode, model, thinking, effort, search budget, tool list, cache phase.

### The invariants that must survive any change

These are non-negotiable. Each is enforced in code and pinned by tests; breaking one is a product regression, not a refactor.

- **Grounding invariant.** `CONFIRMED`/`CORRECTED`/`DISPUTED` require ≥1 *accepted* citation (a model-cited URL that normalizes to one the tool actually retrieved). Enforced in `verifier._apply_source_grounding`, `verifier._enforce_grounding_invariant`, `verification_cache.VerificationCache.put`, and re-checked at render time in `report_status.classify_status`.
- **Exactly-once terminal verification result.** Every finding leaves `collect_verification_batch_results` with exactly one `VerificationResult` — never dropped, never double-written. Three interlocking properties guarantee it (`tests/test_batch_fallback_handoff.py`).
- **CA byte-identity.** `tests/test_golden_domain_surfaces.py` and `tests/test_domain_routing_pins.py` pin the California program's rendered surfaces byte-for-byte. A profile-less run must produce byte-identical tool dicts, cache keys, and report bytes to before the location-aware pipeline existed.
- **Prompt-cache prefix stability.** The instruction prefix ahead of `<spec ` must stay byte-identical across calls. (See §5.1 — this discipline is currently being paid for and not collected on.)
- **Finding-id namespacing.** `rf-` review, `cf-` cross-check, `lc-` compliance. Content-addressed; the prefix is the only thing preventing collision between a review finding and a coordination finding with the same dedup key.
- **After-spend checks degrade, never raise.** `ProgramPipelineResult.__post_init__` runs after every billed call; data-integrity problems are logged, recorded on `integrity_warnings`, and surfaced amber — they never throw away a paid run.
- **Registry validation is a startup gate.** Slot validation only ever gets *stricter*.

---

## 3. The economics of a run — my model, for you to check

This is where I concentrated, because it is where the user's question points. **Treat every number below as an order-of-magnitude estimate built from reading the code, not from a measured run.** The app already has the instrumentation to replace my guesses with facts: `src/orchestration/diagnostics.py` prices every API-call event through `core/pricing.py` into `cost_summary["estimated_cost_usd"]` with per-phase line items (tokens / cache writes / cache reads / web searches). **Your first move should be to get a real phase breakdown from a real run, or from a scripted replay, and then correct me.**

### Call inventory for a representative hyperscale run

Assume: the Hyperscale Data Centers program (4 modules), 12 spec files routing to ~14 (spec, module) pairs, a project profile present, no drawings attached, batch transport (the default), ~120 findings after dedup, ~40 more from cross-check + compliance.

| Phase | Calls | Model | Transport | Batch discount? | Web searches |
|---|---|---|---|---|---|
| Requirements research | **18** (fire 4, architecture 4, electrical 5, ESS 5), each up to 9 turns (`RESEARCH_MAX_CONTINUATIONS = 8`) | Sonnet 5 | **synchronous stream** | **No** | 8–24 per dimension; per-request `max_uses`, post-hoc 2× ceiling |
| Per-spec review | **14** | **Opus 5**, effort `high`, adaptive thinking, 128k cap | Batch | Yes | 0 |
| Haiku triage | ~3 (batches of 20) | Haiku 4.5 | sync | No | 0 |
| Verification round 1 | ~**90** + ~15 Opus escalations | Sonnet 5 / Opus 5 | Batch (waves) | Yes | 3–8 each, severity-tiered |
| Cross-check | 4–12 chunks | Sonnet 5 | **synchronous stream** | **No** | 0 |
| Compliance | 4–12 chunks | Sonnet 5 | **synchronous stream** | **No** | 0 |
| Verification round 2 | ~40 + escalations | Sonnet 5 / Opus 5 | Batch | Yes | 3–8 each |
| Drawing impact | 0–1 | Sonnet 5 | sync | No | 0 |

≈ **200 API calls and 700–1,200 billable web searches** per run. Web search is $10/1,000 and is **never** batch-discounted, so searches alone are on the order of **$7–12 per run** before a single token is counted.

On the research row specifically, do not trust a hand-count — enumerate the registry. `AVAILABLE_MODULES` yields **18** profile-enabled dimensions totalling **314** declared `max_searches` and **108** `max_fetches`:

```
datacenter_fire                        dims=4  max_searches= 64  max_fetches= 22
datacenter_architecture                dims=4  max_searches= 70  max_fetches= 24
datacenter_electrical                  dims=5  max_searches= 90  max_fetches= 31
datacenter_electronic_safety_security  dims=5  max_searches= 90  max_fetches= 31
TOTAL                                  dims=18 max_searches=314  max_fetches=108
```

### The three structural cost facts I believe I established

**Fact A — There is not a single message-level prompt-cache breakpoint in the application.**

`grep -rn "cache_control" src/` returns exactly four hits: `core/api_config.py` (the helpers), `review/review_request_builder.py:288` (a comment about *removing* it for token counting), `core/pricing.py` (a docstring), and `output/html_report_exporter.py:2700` (JavaScript for the browser chat). The two production helpers are `system_prompt_with_cache` and `tools_with_cache`. **Nothing caches any part of `messages`.** The Anthropic API allows up to four breakpoints per request; this app uses two.

**Fact B — Project Context is re-sent, uncached, once per spec review.**

`prompts.get_single_spec_user_message` (`src/review/prompts.py`) assembles one string: intro → code-basis line → reminders → `<project_context>` → `<spec>` → `<pre_detected>` → `<final_task>`. Project Context can legitimately reach the hard cap of `PROJECT_CONTEXT_MAX_TOKENS = 100_000` — it carries the rendered requirements-research profile *and* the drawing digest *and* any attached context files. With 14 review requests, a 50k-token context is 700k tokens of pure repetition at Opus input rates.

The bitter detail: `CLAUDE.md` documents a "Prompt-cache breakpoint stability" invariant and the code carefully places `<final_task>` *after* the spec body specifically so the prefix stays byte-identical — the exact discipline required to make a message breakpoint work. The discipline is enforced. The breakpoint was never added. **The cost of the constraint is being paid and the benefit is not being collected.**

**Fact C — Continuation resumes re-send the entire accumulated conversation, uncached.**

`src/research/requirements_research.py` ~line 690:

```python
messages.append({"role": "assistant", "content": response.content})
messages = sanitize_messages_for_resend(messages)
```

`messages` grows monotonically and is re-sent in full on each `pause_turn` resume, up to 8 times. The accumulated assistant content includes every `web_search` result block *and* every `web_fetch` document — and `WEB_FETCH_MAX_CONTENT_TOKENS = 50_000`, with the `governing_codes` dimension allowing `max_fetches=8`. A dimension that fetches a few large code documents can accumulate several hundred thousand tokens and re-send them several times, all at full uncached input rate. This is quadratic in continuation count. The same pattern exists in the verifier's real-time loop and in `verification_routing.build_verification_request`'s `assistant_content` path.

There is real evidence this path gets large in practice: `core/resend_sanitizer.py` exists *because* a research dimension fetched a complete building code and its continuation was rejected with an HTTP 400 for exceeding the 600-page PDF limit. That is a conversation carrying an entire building code, re-sent.

**Fact D — Cross-check and compliance are synchronous and pay full price.**

`cross_checker.run_cross_check` (line 354) and `compliance_checker` (line 605) both use `client.messages.stream`. `core/chunked_pass.run_chunked_pass` runs chunks sequentially. Only review and verification use the Batch API. These two passes send the *whole spec corpus* (up to `CROSS_CHECK_RECOMMENDED_MAX = 822_000` tokens per chunk) and they send it **twice** — once for cross-check, once for compliance — at 100% of list price, when the run is already batch-latency-bound anyway.

**Fact E — Requirements research has no cross-run cache.**

The verification cache is a well-built, TTL'd, LRU-bounded, jurisdiction-fingerprinted, single-flighted, disk-persisted claim cache. Research has nothing equivalent. The `RequirementsProfile` is persisted for *resume* (`orchestration/batch_resume.py`) and exported as `<report-stem>.profile.json`, but there is no mechanism to reuse it on the next run. A designer doing five hyperscale packages for the same client in the same county in one week re-pays the entire 18-dimension research fan-out five times, for answers that did not change.

---

## 4. Cost-reduction hypotheses, ranked

Ordered by (my estimate of) saving × confidence ÷ quality risk. **Every one of these is a hypothesis for you to validate, cost, and possibly reject.** For each I give the mechanism, my confidence, and the specific way it could hurt quality.

### Tier 1 — structural, large, low quality risk

**1. Add message-level cache breakpoints.** *(Confidence: high that the opportunity exists; medium on magnitude.)*

Three sites, in priority order:

- **Review requests.** Split the user message into content blocks and put a `cache_control` breakpoint on the block that ends at the close of `</project_context>`. The cached prefix becomes tools + system + intro + Project Context; only the spec body and trailing blocks are uncached. Break-even math with the 1-hour TTL, for N requests sharing a prefix of T tokens: uncached costs `N` units of T; cached costs `2 + 0.1(N-1)` (one 2× write, then 0.1× reads). Caching wins when `2 + 0.1(N-1) < N`, i.e. **N > 2.11 — so from the third request onward**. At N = 2 caching is marginally *worse* (2.1 vs 2.0). At N = 14 it is `2 + 1.3 = 3.3` against 14, a **≈4.2× reduction** on the shared portion. (The batch discount scales both sides equally, so the ratio holds.) A single-spec run should not take the breakpoint at all.
- **Continuation resumes** (research pause-turn loop, verifier real-time loop, `build_verification_request`'s `assistant_content` path). Breakpoint on the last block of the accumulated assistant turn. This converts the entire re-sent conversation from 1× to 0.1×. This is the canonical multi-turn caching pattern and it is exactly the case the feature was designed for.
- **Cross-check / compliance chunks** — lower value, since each chunk's corpus differs, but worth measuring.

Watch for: the ≥1024-token minimum for a cacheable block (2048 on Haiku — do not add breakpoints to triage); the four-breakpoint ceiling; cache invalidation if any earlier byte drifts (the existing prefix-stability discipline already protects this, but verify the split preserves the exact bytes the token-count preflight measures — `review_request_builder.build_token_count_request` must count the shape that is actually sent, which is an existing, explicitly-stated invariant); and `tests/test_capability_policy.py` / the golden surface tests, which will catch any request-shape drift on the CA path.

**2. Route cross-check and compliance through the Batch API when the run's transport is `batch`.** *(Confidence: high. Saving: ~50% of two of the heaviest token phases.)*

They are non-interactive, already chunked, already retry-wrapped, already failure-tolerant, and already run behind a batch-latency review. The engine (`core/chunked_pass.py`) is shared, so the change lands in one place. The transport already branches cleanly for verification (`pipeline.verify_findings_for_run`), so the pattern to copy exists. Cost: extra wall-clock. The real-time transport must keep the synchronous path (that is the whole point of real-time).

**3. Add a jurisdiction-keyed, TTL'd requirements-research cache.** *(Confidence: high that it helps a repeat user; the biggest single saving for the author's actual usage pattern.)*

Mirror the verification cache exactly — that design is proven in this codebase, which is most of the argument for it. Key on `module_id | jurisdiction_fingerprint | client | standards_fingerprint | dimension_id`. TTL on the order of 14–30 days. Surface a visible "researched N days ago — refresh" control and a report badge, exactly as the verification cache's age badge does. Honesty requirement: a replayed profile must be *visible* in the report, not silent.

Design question for you: is per-dimension or per-profile the right granularity? Per-dimension survives a module adding a fifth dimension; per-profile is simpler. I lean per-dimension.

**4. Batch the research fan-out.** *(Confidence: medium. Saving: ~50% of research tokens. Cost: one extra latency stage before review submission.)*

The 18 dimension calls are independent and already run in a bounded pool. They could go up as one batch. The trade-off is real: research currently blocks review submission, so batching it adds a wait *before* the wait. Given the run is already batch-shaped, this may be acceptable — but it is an operator-experience decision, not purely a cost one, and it interacts with hypothesis 3 (a cache hit makes this moot). Consider making it conditional or opt-in.

### Tier 2 — policy tuning, requires measurement before you touch it

**5. Research search budgets.** The four profile-enabled modules declare **314** `max_searches` across 18 dimensions (see §3), so the nominal 2× continuation ceiling is **628** searches — $6.28 in search fees at the cap, plus the token cost of every snippet, re-sent across continuations.

Two details about that ceiling matter, and both cut against it being a real guard. First, `max_uses` is enforced by the API **per request**, and every `pause_turn` continuation is a new request with a fresh budget — so the only thing bounding a dimension across its 9 possible turns is the app's own check. Second, that check (`requirements_research.py`, in the `STOP_CLASS_PAUSE` branch) runs *after* a response has been received and billed, and it then **fails the whole dimension** rather than completing it. So the ceiling neither prevents the overshooting call nor salvages the spend that preceded it. **Question to answer with data, not intuition: does the 20th search in a dimension change the resulting profile?** If the marginal value curve flattens at 8–10, the budgets are over-provisioned by 2–3×. If it does not flatten, leave them alone — this is the phase that grounds every downstream jurisdictional claim, and under-researching it is exactly the kind of saving that costs the product its reason to exist.

**6. `WEB_FETCH_MAX_CONTENT_TOKENS = 50_000` × up to 8 fetches per research dimension.** Is a full 50k-token fetch the right granularity for a dimension asking "which IBC edition is adopted"? A lower per-fetch cap for research (keeping verification at 50k) may be a pure win. Interacts strongly with hypothesis 1.

**7. Review effort level.** Review runs Opus 5 at effort `high` with adaptive thinking and a 128k output cap. The team already lowered this from `xhigh` as a spend measure and documented the reasoning. The next step down (`medium`) is a materially larger quality bet. `api_config.py` notes that Sonnet 5 at `medium` ≈ Sonnet 4.6 at `high`; whether that transfers to Opus is unknown. **Do not change this without eval evidence** — see §6.

**8. Two-stage review model routing.** Sonnet 5 first pass on every spec; Opus 5 re-review only where the Sonnet pass or the deterministic pre-screen indicates something serious. Potentially the largest single token saving available (review is the only Opus-by-default phase). Also the largest quality risk, since a missed finding at stage one is never recovered. **Only pursuable with a real recall measurement, which does not currently exist.** I would not do this speculatively.

**9. Batch verification claims that share a `codeReference`.** Today it is strictly one API call per finding. N findings citing NFPA 13 §10.2.5 could share one search-and-verdict call. Saves both calls and searches. Quality risks: attention dilution across claims, muddier grounding attribution (which snippet grounded which verdict?), and pressure on the exactly-once invariant. A conservative version — batch only GRIPES/MEDIUM findings sharing an identical `codeReference`, never CRITICAL/HIGH — may capture most of the value at a fraction of the risk.

**10. Semantic dedup before verification.** `pipeline._dedup_key` is exact-text (normalized issue + section + codeReference + actionType + text digests). Construction specs are heavily templated, so the *same* defect across 20 section files is common — but the review model paraphrases per spec, so near-duplicates survive dedup and each one buys its own verification call with its own search budget. A cheap clustering step (Haiku, or embedding-free shingling on the claim) that verifies one representative and replays the verdict to the cluster could cut verification volume substantially. **The honesty requirement is absolute:** an inherited verdict must be visibly marked as inherited in the report and the sidecar. The codebase already has the vocabulary for this (`cache_status="shared"` in the single-flight path) — extend that pattern rather than inventing one.

**11. Per-phase cache TTL.** `_cache_control_block()` hardcodes the 1-hour TTL (2× write multiplier). That is correct for review and verification, which run for hours. It is a net *loss* for a phase whose prefix is read once or twice in a run — drawing impact is one call per run; compliance on a small single-chunk project is one call. Making TTL a `CachePolicy` field costs little and stops paying a 2× write for a prefix that is never read.

### Tier 3 — non-obvious, worth a look

**12.** Does the `LARGE_REVIEW_INPUT_THRESHOLD = 200_000` extended-output gate interact with a large Project Context in a way worth caring about? A 100k context pushes more specs over the threshold, which raises `max_tokens` from 128k to 300k on Opus. **This is a ceiling, not a charge** — Anthropic bills actual output tokens, as `api_config.py` says of its own caps ("a fail-fast guard, not a billing knob") — so crossing it costs nothing by itself. The real question is whether the *headroom* changes behavior: a review that would have truncated at 128k (and triggered a paid repair pass) can now run to 300k instead, which is cheaper than the repair if the extra output was needed and pure spend if it was not. Treat it as an output-growth risk to measure against real `output_tokens` distributions, not as an automatic second cost. The concrete, certain cost of a large context remains the repeated uncached input in Fact B.

**13.** `tokenizer.safe_local_estimate` pads Sonnet 5 by **1.45×**. Every gate driven by a local estimate is therefore conservative by 45% on the app's most-used model. Check whether that causes unnecessary chunking, unnecessary escalation into the extended-output path, or spurious preflight failures. A more accurate factor (or more use of the free `count_tokens` endpoint) may be worth more than it looks.

**14.** Verification round 2 sends *all* cross-check and compliance findings to verification, unconditionally, by explicit design ("refutation is where verification earns its cost"). I think the reasoning is sound. Confirm the severity-tiered budget still applies to them, so a GRIPES compliance finding is not spending a CRITICAL-sized search budget.

---

## 5. Specific things I suspect are wrong, with locations

Ordered by my confidence. Verify each independently — I did not run the app against the live API.

### 5.1 Documentation asserts a behavior the code does not have (high confidence)

`CLAUDE.md`, "Project Context attachments": *"Project Context is free-text that ships on **every** review, cross-check, AND verification call."* `README.md` line 117 repeats it: *"…so every review, cross-check, and verification call sees it."*

It does not reach verification. `verifier._build_verification_prompt` (`src/verification/verifier.py:553`) takes only `finding` and `cycle`. `verify_finding` (line 1576) has no context parameter. `grep -rn "project_context" src/verification/` returns **nothing**.

Two separable problems, and they pull in opposite directions:

- **The doc is wrong** and should be corrected. Cheap, unambiguous.
- **Should it be true?** This is the more interesting question, and it is a *quality* issue that would *increase* cost. Consider a `datacenter_fire` run in Loudoun County, Virginia. The research pass determines the governing code editions and AHJ requirements. The compliance pass sees them (via `project_context`). The review pass sees them. **The verifier — the component that assigns the trust label the whole product is built around — does not.** It reasons from the module's generic US fallback anchors (NFPA 72-2022, NFPA 70-2023) and gets the project location only as a `web_search` localization hint. So a compliance finding asserting "this spec must cite the 2021 Virginia Construction Code" is adjudicated by a model that was never told which code Virginia adopted, even though the app spent real money finding out 20 minutes earlier.

I think this is a genuine soundness gap in the location-aware pipeline. Splicing the *whole* context into every verification call would be very expensive (it is the highest-call-count phase) — but a compact, structured jurisdiction summary (a few hundred tokens: adopted editions and AHJs, not the full profile) placed in the cached system prefix might be nearly free and materially more correct. **Please evaluate this on the merits; it may be the most valuable thing in this brief, and it is not a cost saving.**

### 5.2 The prompt-cache prefix discipline is unused (high confidence)

See Fact A / Fact B. The `<final_task>`-after-body placement, the byte-identical-prefix rule, and the `prompt_serialization` escaping discipline all exist to protect a cache breakpoint that was never added. Either add the breakpoint (hypothesis 1) or correct the documentation to say the discipline is forward-looking. Right now `CLAUDE.md` reads as though caching is active on the message path.

### 5.3 ~34 KB of JavaScript ships with no syntax check (high confidence, moderate severity)

`src/output/html_report_exporter.py` embeds a ~33,700-character JavaScript string (around line 2243) implementing the Ask AI chat: streaming SSE with CRLF-frame buffering, tool-use loops, model selection, client-side report filtering. The 76 tests in `tests/test_html_report_exporter.py` assert on *strings in the output*. Nothing parses or executes that JavaScript. The only Playwright test in the repo (`tests/test_trace_viewer_offline.py`) targets the trace viewer and skips when Playwright is absent — which is the case in CI (`.github/workflows/tests.yml` installs only `requirements-dev.txt`).

A syntax error in that blob ships silently and breaks the chat for every exported report, with a CSP `sha256` computed over the broken bytes so it fails *quietly*. Cheapest fix: a CI step that extracts the script and runs `node --check` (or `esprima`/`acorn`). Better: a small Playwright smoke test that opens a rendered report and asserts the chat initializes. Note the CSP hash is computed over the exact bytes written and `write_html_report` writes binary specifically so newline translation cannot invalidate it — that care deserves a matching syntax gate.

### 5.4 No linting, type checking, or coverage in CI (high confidence)

`.github/workflows/tests.yml` runs one job: `pip check` plus `pytest` on Python 3.11. No `ruff`/`flake8`, no `mypy`, no coverage gate, no eval run. For 60k lines of production Python with pervasive `Optional`, dict-shaped API payloads, and `getattr`-based defensive reads across module boundaries, a type checker would likely find real defects. My suggestion: introduce `ruff` first (fast, low-noise, high-yield), and `mypy` only in non-strict mode scoped to `src/core/` and `src/verification/` where the invariants concentrate. **Do not propose a repo-wide strict-typing migration** — that is a multi-week project with a poor ratio here.

### 5.5 159 `except Exception` handlers, ~48 of which swallow to `pass` (medium confidence on severity)

Many are legitimately defensive: tracing hooks are explicitly designed never to escape into the pipeline, and the after-spend-degrade-never-raise principle is deliberate and correct. But this is a large surface. The question worth auditing is narrow and specific: **which of these can convert a paid failure into an apparently-clean result?** That is the failure mode the entire "Review-stage failure surfacing" feature exists to prevent, so a swallow that re-opens it is a genuine regression vector. I did not audit them individually.

### 5.6 The eval baseline measures plumbing, not quality (high confidence, high importance)

`evals/baseline.json` reports `review_recall: 1.0` over 5 fixtures — but the fixtures replay *scripted* model responses (`evals/fixtures.py`, `evals/calibration/fixtures/`). The harness validates parsing, classification, grounding enforcement, and edit-shape validation. It cannot observe a prompt change or a model change. `evals/live_capture.py` exists to close exactly this gap and is well designed (LLM-as-judge matching, fail-back-to-substring, hermetic by default, refuses the sentinel key) — but there are only 12 captured live fixtures and no checked-in live baseline with real recall numbers.

**This is the load-bearing constraint on every Tier-2 cost hypothesis.** You cannot responsibly recommend "drop review effort to medium" or "route to Sonnet first" without a recall/precision measurement to check it against. If you propose any quality-affecting cost change, the plan must sequence the measurement *first*. Building or extending that measurement may be the highest-value work available, and it is worth saying so plainly if you conclude that.

### 5.7 Smaller observations (low confidence, worth a glance)

- `pipeline._deduplicate_findings` sets the merged finding's `confidence = max(f.confidence for f in group)`. Max is the optimistic choice for a value the report renders as a trust signal. Min or mean would be more honest. It is a judgment call; flag it, do not assume it is a bug.
- The same function mutates the merged `issue` text to append `"(found in N specs: …)"`, which means the merged finding no longer hashes to its own `_dedup_key`. The code handles this correctly today (`merged_id` is computed from the representative before mutation), but the invariant is implicit. A second dedup pass over merged output would misbehave. Worth a comment or an assertion.
- Model ids are resolved from `os.environ` at **import time** (`api_config.py:51–94`). Env changes after import have no effect. Fine for the desktop app; a trap for tests and any future long-lived process.
- `verifier.py` (3,705), `pipeline.py` (3,280), and the two exporters (3,634 / 3,695) are god modules. The two exporters deliberately share pure summarizers to guarantee parity, which is a good design — but "parity by construction" is easier to hold with a shared render-model than with two walks over the same data. Evaluate whether an intermediate report model is worth extracting. **Weigh this against churn risk; I lean toward leaving it alone unless you find real drift.**
- The single-flight verification grouping keys on the *exact* cache key, so within a run it only collapses byte-identical claims that `_deduplicate_findings` already collapsed. Its real value is across concurrent module collectors. Confirm it is earning its considerable complexity.

---

## 6. Exhaustive checklist

Work through this. Skip nothing without recording why. Where a section reaches a conclusion, state the evidence.

### A. Correctness of the trust model
1. Is the grounding invariant enforced on **every** path that can produce a verified verdict — real-time, batch wave, batch retry, batch continuation, escalation, cache load, cache store, render? Try to construct a path that reaches `VERIFIED_SUPPORTED` without an accepted citation.
2. Is the exactly-once terminal-result invariant actually airtight? Specifically: real-time fallback vs. follow-up-wave submission mutual exclusion; the detach-on-final-wave `break`; escalation skipping findings the fallback already stamped.
3. Continuation-cap parity: `pipeline`'s `> cap` vs. the real-time loop's `range(cap + 1)`. CLAUDE.md argues these are equivalent. Verify by enumeration, not by reading the argument.
4. Can a `DISPUTED` verdict without an accepted citation reach the report as `DISPUTED` rather than `INSUFFICIENT_EVIDENCE`? Through the cache? Through a legacy cache row?
5. `verification_cache` schema versioning and per-row re-checks: can a v3 row that violates a v4 invariant load successfully?
6. Does `models_disagreed` fire only when *both* passes grounded? Is `VERIFIED_CONTESTED` reachable in the report and the sidecar?
7. Budget-exhaustion sentinel: correctly detected on both transports, correctly refused by the cache, correctly rendered?

### B. Concurrency
8. `DiagnosticsReport`'s `RLock` — is every mutable-state path actually guarded?
9. Verification single-flight (`pipeline._verify_findings_singleflight`, ~line 2638): leader death, follower wait bound, re-round cap, the cache-fill race the leader closes with `_stamp_grounded_cache_hits`, and the `finally` that releases the whole atomic claim map. Can a finding end with zero results? Two?
10. Extraction cache single-flight: is the deep copy handed to each caller genuinely isolated?
11. The global synchronous-call semaphore: held per call, released across backoff sleeps. Any path that holds it across a whole chunked pass?
12. Trace context propagation across `ThreadPoolExecutor` boundaries — workers explicitly retain their span. Any executor that forgot?
13. `ui_state` writes serialized under one lock — check for read-modify-write races between controllers.

### C. Cost and API usage — the core assignment
14. **Confirm or refute Facts A–E in §3.** These are the foundation of every Tier-1 recommendation.
15. Get a real per-phase cost breakdown. Use `orchestration/diagnostics.py`'s `cost_summary`. If you cannot run live, build a scripted-client replay that exercises the collect DAG with realistic token counts and read the priced output.
16. Verify `core/pricing.py` rates against current published Anthropic pricing. The module says outright that rates drift.
17. Are batch requests actually getting the 50% discount? Is `service_tier: "auto"` doing anything useful given Opus 5 does not support Priority Tier?
18. Are the 1-hour cache TTL writes paying back on every phase that declares them? Compute break-even per phase (see hypothesis 11).
19. Quantify the continuation-resend amplification (Fact C). This is the number I am least sure of and most interested in.
20. Do the research `max_searches` budgets have empirical justification anywhere in the repo, or are they authored intuitions?
21. Extended-output gate (`LARGE_REVIEW_INPUT_THRESHOLD`): how often does a real run cross it, and what does it cost when it does?
22. Any redundant call in the DAG? A spec routed to two modules is reviewed twice by design — is that the right design, or should a shared first-pass extraction of findings feed both module reviewers?

### D. Prompt engineering and model usage
23. Are the per-phase effort levels defensible? Any evidence, or all intuition?
24. Is the few-shot example set (`review_examples`, registry-validated to anchor all four action types and ≥1 CRITICAL) actually well-calibrated, or just structurally complete?
25. Strict tool use is on by default and gated on the capability whitelist. Are the schemas genuinely inside the strict subset?
26. Prompt-injection posture: spec content is wrapped in XML data blocks with escaping. Try to break `prompt_serialization`. A spec is an untrusted document from an outside party.
27. Is the model capability whitelist (`api_config.model_capabilities`) accurate against current Anthropic documentation — particularly `supports_web_fetch` being **off** for Opus 5, and Sonnet 5's 300k batch beta being **on**?

### E. Data integrity
28. Extraction: text boxes, footnotes, endnotes, headers/footers, nested tables, merged cells. The reconstruction invariant (`"\n\n".join(m.text for m in paragraph_map) == content`) raises on mismatch — can you break it? Documented remaining gaps: SmartArt, grouped shapes, text boxes inside headers/footers, tables inside text boxes or notes.
29. `_detect_content_loss_warning` threshold of 0.20 — right number? Right denominator?
30. Sidecar schema v4/v5: is `(finding_id, fileName)` genuinely unique? Does the per-file fan-out lose anything?
31. Batch resume: does a resumed run produce the same result as an uninterrupted one? The repair-batch reattach path is subtle (`pipeline._reattach_saved_repair_batch`) and it guards real money.
32. Custom-id stem collisions on the bare-id recovery path — the index-based disambiguation and its WARNING fallback.

### F. Security
33. HTML report: escaping of every report-derived string; the `\u`-escaping of `&`/`<`/`>` in the embedded JSON; CSP `sha256` over exact bytes; `default-src 'none'`. Can hostile *spec content* reach the browser as executable anything?
34. The exported HTML never contains an API key; the key lives in tab-scoped `sessionStorage`. Verify.
35. Updater: https enforced on both the manifest and the installer URL, including **after redirects**; SHA-256 verified before `os.replace` promotes the `.part` file. This is the highest-stakes security surface in the app — an unsigned installer authenticated only by a hash in a manifest.
36. `redaction.py`: are API keys and bearer tokens actually stripped before traces are serialized?
37. API key storage via `keyring` with a file fallback — what are the fallback file's permissions?

### G. Engineering health
38. Module size and coupling — god modules; is extraction worth the churn?
39. Duplication between `report_exporter.py` and `html_report_exporter.py` beyond the deliberately shared summarizers.
40. Test suite: 3,158 tests in 18 s is excellent. But how many are AST-shape assertions or string-presence checks rather than behavioral tests? What is the ratio, and does it matter?
41. What is genuinely untested? (I believe: the embedded JavaScript, and the GUI event paths not covered by the AST pins.)
42. CI gaps: lint, types, coverage, JS syntax, eval regression.
43. Doc/code drift beyond §5.1. `CLAUDE.md` is 147 KB and extraordinarily detailed — which is precisely why drift in it is dangerous. **Spot-check its load-bearing claims against the source.** I found one clear error by accident, not by looking; assume there are others.
44. Dependency pinning: `anthropic>=1.4,<1.5` is tight. Is the rest of `requirements.txt` coherent?

### H. Product / domain judgment
45. Is the nine-status trust model comprehensible to a working designer, or is it over-engineered for its audience? The author is the primary user — optimize for him.
46. Is the emit-only edit-instruction design right, given no applier exists yet? Is the sidecar schema stable enough to build one against?
47. The documented cross-division coordination gap: chunked cross-check cannot see conflicts spanning two divisions in different chunks. How often does that matter for a real hyperscale package, and is a program-level coordination pass worth its cost?
48. Division 27 and most of Division 28 are explicit coverage gaps. Is the gap surfaced clearly enough that a user cannot mistake "not reviewed" for "reviewed and clean"? This is the same class of honesty problem the failed-review surfacing feature solved for specs — check that the routing gaps get the same treatment.
49. **NFPA edition currency.** `docs/standards_provenance.md` marks several editions `UNVERIFIED` (ASHRAE 62.1/90.1, IAPMO TSC, the UL listings). The NFPA fire-protection editions are verified against the California Fire Code 2025 Chapter 80 adoption table. The user requires that any NFPA 13 reference reflect the current edition. Verify that the pinned-edition rendering surfaces reach every place the model could reason about an edition, and that `UNVERIFIED` provenance is visible to the operator rather than only to the maintainer.

---

## 7. Where to be skeptical of me

- I did not run the application against the live API. Every cost figure in §3 is derived from reading code, not from measurement. **If your measured numbers contradict my model, your numbers win.**
- I did not read the GUI controllers, the tracing subsystem, the Windows packaging/updater, the report exporters' rendering detail, or the eval harness internals with any depth. There may be significant issues there that I simply did not look at.
- I may have missed a message-level cache mechanism that does not use a literal `cache_control` string. Confirm Fact A independently.
- My ranking in §4 encodes an assumption that token/search spend dominates the author's cost. If his actual runs are small (a handful of CA K-12 specs, no research fan-out, no drawings), then Tier 1 items 3 and 4 are near-worthless and the ranking is wrong. **Check the actual usage shape before optimizing for the shape I assumed.**
- I have a bias toward finding cost savings because that is what the brief asked for. Correct for it. If the honest answer is "this is already well-optimized and the remaining levers trade quality for money," say that.
- **Empirically, my arithmetic and my enumeration both needed correcting.** An automated review of this brief caught three errors before it reached you: I had the prompt-cache break-even wrong (claimed savings from the 2nd request and ~6–7×; the correct figures are the 3rd request and ≈4.2×), I hand-counted the research fan-out as 16 dimensions / 512 searches by extrapolating from one module when the registry says 18 / 628, and I asserted that crossing the extended-output threshold costs money when it only raises a ceiling. All three are fixed above. Draw the obvious inference: **enumerate the registry and redo the arithmetic yourself rather than quoting mine.**

---

## 8. Constraints on any change you propose

1. **The CA K-12 path must stay byte-identical.** The golden tests enforce it. If a change alters CA output bytes, it is a different change and needs its own justification.
2. **Never weaken the grounding invariant, the exactly-once invariant, or the registry-validation gate.** Those may only get stricter.
3. **Never make the report less honest.** Every existing surface that says "this failed / this was replayed / this was not verified / this was not reviewed" exists because its absence was once a real defect. A cost optimization that hides work not done is a product regression, whatever it saves.
4. **The test suite must stay hermetic and fast.** No network, no API key, no display. `tests/test_gui_import_hermeticity.py` enforces the import discipline.
5. **The five version literals must stay synchronized** (`pyproject.toml`, `src/__init__.py`, `README.md`, and two in `CLAUDE.md`) — `packaging/windows/check_release_version.py` and `tests/test_release_metadata.py` enforce it.
6. **Windows is the deployment target.** Path handling, long paths, the frozen-app tiktoken cache, and the OS trust store all have deliberate accommodations. Do not regress them.
7. **Documentation is part of the deliverable.** The author's standing preference: when the implementation changes, update `README.md`; when dependencies change, update `requirements.txt`; when the engineering contract changes, update `CLAUDE.md`. Do not shorten the README — length is fine, additions must be necessary.
8. **Scope discipline.** A 60k-line codebase with 3,158 passing tests and this density of documented invariants is not a candidate for a sweeping rewrite. Prefer surgical, individually-justified, independently-revertible changes.

---

## 9. Your deliverable

### Part 1 — Review report

- **Executive summary.** Your honest overall assessment of the codebase's health, in a few paragraphs. Do not soften it and do not inflate it.
- **What is genuinely good.** Name it specifically. This code has real strengths and the author should know which ones to protect.
- **Findings**, each with: severity (Critical / High / Medium / Low / Informational), category (correctness / cost / security / maintainability / product), file and line, the concrete failure scenario, your confidence, and how you verified it.
- **Explicit disagreements with this brief.** Where I was wrong, say so and show why. This section is not optional.
- **The cost analysis.** Your measured or modeled per-phase breakdown, your validation or refutation of Facts A–E, and your own ranking of levers with expected saving and quality risk for each.
- **Anything you found that this brief does not mention.** This is the section I expect to be most valuable, because it is the one I could not write.

### Part 2 — Implementation plan (only if warranted)

If you conclude that some changes are worth making:

- **Sequenced phases**, ordered by dependency and by risk-adjusted value.
- **Per item:** what changes, which files, why now, blast radius, what could break, how to verify (which existing tests cover it, which new tests are needed), how to revert.
- **An explicit "skip this if…" condition per item**, so the author can drop any single item without unravelling the rest.
- **A "do not do this" list** — changes that look attractive but that you judged not worth it, with reasons. This is as valuable as the recommendations.
- **The measurement prerequisite, stated plainly.** If a proposed change is quality-affecting and cannot be validated against the current eval harness, say so and sequence the measurement work first. Do not recommend a quality-for-money trade the author has no way to check.

If you conclude that **no** changes are warranted, say that clearly and defend it. That is a legitimate and useful answer, and it will be believed.

---

## 10. Fast orientation for your first hour

```bash
# Environment (the container's Debian-managed `packaging` blocks a clean install)
pip install -q --ignore-installed packaging -r requirements.txt -r requirements-dev.txt
python -m pytest                              # expect 3158 passed, 24 skipped, ~18s
python -m evals.runner                        # hermetic golden-set harness
python -m evals.calibration.runner            # hermetic verdict-calibration harness
```

Read in this order:

1. `CLAUDE.md` — the engineering reference. Dense, excellent, and not fully in sync with the code.
2. `src/orchestration/pipeline.py`, `run_batch_collection_headless` (~line 3180) — the whole collect DAG in one function.
3. `src/core/api_config.py` — every cost, capability, and policy decision in one file.
4. `src/verification/verifier.py` + `verification_routing.py` — the trust model.
5. `src/modules/base.py` + `src/modules/california_k12_mep.py` — the engine/domain split.
6. `TRUST_AUDIT.md` — a prior audit; its P0/P1 items are cited throughout the code and tell you what was already found and fixed.
7. `docs/hyperscale_datacenter_module_plan.md` (89 KB) — the design record for the newest and least-settled subsystem.

Good hunting. Be rigorous, be specific, and disagree with me where I have it wrong.
