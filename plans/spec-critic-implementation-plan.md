# Spec Critic — Implementation Plan

**Revised:** September 23, 2026 (first written September 22, 2026)  
**Repository:** Abe-Borg/Claude-Spec-Critic. All paths are relative to the repository root.  
**Progress tracker:** [`plans/PROGRESS.md`](PROGRESS.md), which says what's done, what's next, and gives the prompt for the next session.  
**Code checked against:** master `f9da027` (Spec Critic 3.9.0, Anthropic SDK 1.7.0). The original plan reviewed commit `01781ed`; between the two, `src/` changed by one docstring line.

## Read this first

**What this is.** This is the to-do list of fixes for Spec Critic that came out of the independent review in September 2026. Part 4 is the full technical spec, written for the coding agents. You don't need to read it.

**Where we are.** Nothing in this plan has been done yet. On September 23 every problem the review describes was re-checked against the current code, and every one is still there (Part 3 has the evidence). Nothing is broken or half-done: the code is exactly v3.9.0.

**How the work gets done.** In about 25 coding sessions, one after another. Each session does one chunk of work (Part 2), opens one pull request, and updates `plans/PROGRESS.md`.

| Sessions | What happens | Your part |
|---|---|---|
| S01–S18 | The fixes. All of them are required. | Merge each pull request |
| S19 | The release | A short Windows smoke test, then merge and tag |
| S20–S25 | Optional experiments that need real API spending | Say yes or no to a spending limit, or skip them all |

**What you do each time.**

1. Start a new Claude Code session on this repository and paste the prompt. The first one is below. After that, each session gives you the next one, and the current one is always at the top of `plans/PROGRESS.md`.
2. When the session says its pull request is ready, merge it.
3. Tell the session it's merged. It replies with the prompt for the next session.
4. Start the next session. Never run two sessions at once.

**How you'll know it's finished.** When the last chunk is merged, the session prints a huge **ALL DONE** banner, and `plans/PROGRESS.md` says ALL DONE at the top.

**Prompt for the first session:**

```text
Continue the Spec Critic implementation plan.

Next chunk: S01 — Starting point and shared test fixtures (WP-01).

Start from the latest master. Read CLAUDE.md, then plans/PROGRESS.md, then Part 1
and chunk S01 in plans/spec-critic-implementation-plan.md, and its packages in Part 4.
Do only this chunk. If PROGRESS.md names a different next chunk, follow PROGRESS.md.
Open one PR. When I tell you it's merged, give me the prompt for the next session.
```

---

## Part 1 — Rules for every coding session

These rules add to `CLAUDE.md` and to the engineering invariants in Part 4 (§2). When this plan and the code disagree, the code and its tests are the evidence; see rule 7.

### 1. One chunk, one pull request, one session at a time

- Do exactly one chunk per session: the first chunk in `plans/PROGRESS.md` that isn't DONE. A chunk marked PARTLY DONE comes first.
- Open exactly one pull request per session, against `master`.
- Don't start the next chunk in the same session, even with time left.
- If the prompt names a different chunk than `PROGRESS.md`, follow `PROGRESS.md` and say so.

### 2. Start of session

1. Base your branch on the latest `origin/master`, using the branch name your session gives you.
2. Read `CLAUDE.md`, `plans/PROGRESS.md`, this Part 1, your chunk in Part 2, and the full sections in Part 4 for your chunk's packages.
3. Compare each package's "Current state" note with the code. If the code has changed since September 23, adapt. If the defect is already gone, prove it with a test and record the closure (rule 7).
4. Run the offline suite (`python -m pytest -m "not network"`) and note the result. Record any failure that already exists on master in `PROGRESS.md`; don't silently fix unrelated failures.
5. Mark the chunk IN PROGRESS in `PROGRESS.md`. This edit goes in your pull request.

### 3. During the work

- Follow the required behavior and acceptance criteria in Part 4, and the invariants in Part 4 §2.
- Write a regression test for every defect you fix. Tests stay offline: no network, no real API key, stubbed token counts, injected clocks, sleeps, and random sources, and temporary directories for every file.
- From S01 on, each known defect has a strict-xfail test. When your fix makes one pass, remove its xfail marker in the same pull request.
- Update whatever your change makes stale: `CLAUDE.md` (invariants, environment variables), `README.md` for anything a user would notice, `requirements.txt` for dependency changes, and any row of "Known-wrong statements" in `PROGRESS.md` that your chunk fixes.
- Review every changed golden file individually and explain each intentional change in the pull request. Bulk regeneration is not proof.
- Add a plain line under "Release-note lines" in `PROGRESS.md` for anything a user would notice.
- Container limits: the cloud container has no tkinter, so GUI test modules skip, and it can't download tiktoken's `cl100k_base` file, so tests must stub token counts. If your chunk touches GUI code, try `apt-get install -y python3-tk`. If that fails, rely on the fake-app controller tests and say so in the pull request.

### 4. If the chunk is too big for one session

- Stop at a point where master stays green and safe. Never merge half of a contract migration, such as a sidecar writer without its reader.
- Mark the chunk PARTLY DONE and write what remains as unticked checkboxes under the chunk in `PROGRESS.md`. The next session continues the same chunk.
- Part 2 marks the chunks most likely to need this as "may split".

### 5. End of session

1. Run your focused tests, the full offline suite, and `python -m pip check`. If you touched the HTML report, also run `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1 python -m pytest tests/test_html_report_javascript.py tests/test_html_report_exporter.py`. Node 22 is installed in the container.
2. Update `PROGRESS.md`:
   - Tick the chunk's boxes and set its status to DONE or PARTLY DONE.
   - Add a session-log entry: date, what changed, the test result, and what's left.
   - Point "Right now" at the next chunk, and write the next-session prompt there.
3. Open one pull request titled `S0N: <chunk title> (WP-xx)`. The body covers what changed and why, the tests run and their results, any skips, and follow-ups. Then add the pull request number to `PROGRESS.md` in a follow-up commit on the same branch.
4. Watch the pull request. Fix CI failures and review comments on the same branch until it's green and mergeable.

### 6. After the owner merges the pull request: the next-session prompt

- Wait until the pull request is merged. The owner will tell you, or you can check with the GitHub tools.
- Then reply with the next-session prompt in a code block, ready to paste, plus one plain sentence on what the next chunk does. Use this template:

```text
Continue the Spec Critic implementation plan.

Next chunk: <ID> — <title> (<packages>).

Start from the latest master. Read CLAUDE.md, then plans/PROGRESS.md, then Part 1
and chunk <ID> in plans/spec-critic-implementation-plan.md, and its packages in Part 4.
Do only this chunk. If PROGRESS.md names a different next chunk, follow PROGRESS.md.
Open one PR. When I tell you it's merged, give me the prompt for the next session.
```

- If the merged chunk was PARTLY DONE, the next prompt names the same chunk.
- After S19 merges, print the milestone notice below, then the prompt.
- After the last chunk merges, print the final banner below instead of a prompt. The last chunk's pull request also sets "Right now" in `PROGRESS.md` to ALL DONE.

### 7. When the plan is wrong or already done

- If the code already does what a package asks, prove it with a test, record it under "Decisions and deviations" in `PROGRESS.md`, and tick the box.
- If a requirement is wrong for the current code, don't follow it blindly. Do the right thing, record what you changed and why under "Decisions and deviations", and say so in the pull request.

### Milestone notice (after S19 merges)

Print this heading exactly:

```text
## ✅ REQUIRED WORK DONE — only the optional experiments are left
```

Then explain in two or three plain sentences: the correctness release is merged; S20–S25 measure possible improvements with real API calls, so each of those sessions asks for a spending limit first. Tell the owner that to skip all six they can start the next session with *"Skip the Spec Critic experiments: mark S20–S25 as not evaluated and finish the plan."* That session updates `PROGRESS.md`, opens one pull request, and after it merges prints the final banner.

### Final banner (after the last chunk merges)

Print this heading:

```text
# ✅ THE SPEC CRITIC IMPLEMENTATION PLAN IS COMPLETE ✅
```

Then this banner, in a code block:

```text
 █████╗ ██╗     ██╗        ██████╗  ██████╗ ███╗   ██╗███████╗
██╔══██╗██║     ██║        ██╔══██╗██╔═══██╗████╗  ██║██╔════╝
███████║██║     ██║        ██║  ██║██║   ██║██╔██╗ ██║█████╗
██╔══██║██║     ██║        ██║  ██║██║   ██║██║╚██╗██║██╔══╝
██║  ██║███████╗███████╗   ██████╔╝╚██████╔╝██║ ╚████║███████╗
╚═╝  ╚═╝╚══════╝╚══════╝   ╚═════╝  ╚═════╝ ╚═╝  ╚═══╝╚══════╝
```

Then one line: **Every chunk in `plans/PROGRESS.md` is done. There is no next session.**

---

## Part 2 — The chunks, in order

Each chunk is one session and one pull request. Its checklist, the definition of done, is in `plans/PROGRESS.md`. The order puts the most serious (P1) fixes first wherever dependencies allow. "Depends on" means those chunks must be merged first.

| Chunk | What it does, in plain words | Packages | Depends on |
|---|---|---|---|
| S01 | Record the starting point; build the fake Word documents and tests later chunks use | WP-01 | — |
| S02 | Stop merging findings that are actually different; make the applier refuse two input files with the same name | WP-06A, WP-07 | S01 |
| S03 | Stop the false "empty section" and "duplicate heading" alerts; fix the placeholder, stale-code-year, and file-name checks | WP-04 | S01 |
| S04 | Make the report's Ask AI chat recover from errors; keep the API key out of browser storage | WP-12 | S01 |
| S05 | Stop saving "couldn't verify" as if it were an answer; treat garbled verifier replies as failures | WP-10 | S01 |
| S06 | Measure big requests with the right ruler for the model, and split them when they're too big | WP-08 | S01 |
| S07 | Make the report say when compliance coverage is incomplete | WP-09 | S06 |
| S08 | Keep the saved record of a paid repair batch until it's actually collected | WP-14 | S01 |
| S09 | Count the cost of every paid attempt exactly once, including failed ones | WP-15 | S08 |
| S10 | Read text inside Word content controls, fields, and smart tags | WP-02 | S01 |
| S11 | Keep every edit location, part 1: data model and applier reader | WP-06B | S02, S10 |
| S12 | Keep every edit location, part 2: sidecar writer and reports | WP-06B | S11 |
| S13 | Route specs using the SECTION heading inside the document | WP-05 | S10 |
| S14 | Read Word's automatic numbering (1.01, A., …) | WP-03 | S03, S10 |
| S15 | Wait as long as the API asks before retrying | WP-11 | S05 |
| S16 | Don't let a tracing failure freeze the app; keep typed keys out of the environment | WP-13 | S01 |
| S17 | Keep the verifier's citations; fix the fetch instructions | WP-16 | S05 |
| S18 | Make prompts, report wording, and docs match the code | WP-17 | S01–S17 |
| S19 | Correctness release | release | S18 |
| S20 | Experiment: cache the shared Project Context | EX-01 | S06, S09 |
| S21 | Experiment: strict answer formats | EX-02 | S05 |
| S22 | Experiment: model, effort, and confidence | EX-03 | S01, S05, S09, S17 |
| S23 | Experiment: check that sources really support claims; reuse sources | EX-04 | S05, S17 |
| S24 | Experiment: reuse research across runs | EX-05 | S09 |
| S25 | Experiment: find conflicts across chunks and disciplines | EX-06 | S06–S14 |

### Chunk notes

**S01 — Starting point and shared test fixtures (WP-01).** Main files: `tests/fixtures/` (new DOCX builder module), a new reproduction test module, and `plans/check_plan_status.py`, which is converted into tests and then deleted. This chunk adds fixtures and tests only; it changes no behavior. The reproduction tests are strict xfails: when a later chunk fixes a defect, its test starts passing, strict mode turns that into a failure, and the fixing session removes the marker. That makes the test suite a second, CI-enforced progress tracker.

**S02 — Different findings and ambiguous files (WP-06A, WP-07).** These are two small P1 fixes the original plan chose to release early. Main files:

- `src/orchestration/pipeline.py`: `_normalize_issue_text`, `_dedup_key`, `compute_finding_id`, and `_deduplicate_findings`.
- `applier/run.py`, `applier/cli.py`, `applier/models.py`, and `applier/receipt.py`.

Some finding IDs change, so add a release-note line.

**S03 — Detectors (WP-04).** Main files: `src/input/preprocessor.py`, the detector vocabularies in `src/modules/`, and the golden files. May split: headings and placeholders (A + D) first, then suppression, citation syntax, and file names (B + C + E).

**S04 — Chat (WP-12).** Main files: `src/output/html_report_exporter.py` (its embedded JavaScript), `tests/test_html_report_javascript.py`, and a new Node behavioral harness. May split: the harness plus stream and transaction handling first, then citations and key storage.

**S05 — Verification failures and cache (WP-10).** Main files:

- `src/verification/verifier.py`, `verification_cache.py`, and `source_grounding.py`
- `src/output/report_status.py`
- the verification sharing code in `src/orchestration/pipeline.py`
- `src/orchestration/diagnostics.py`

Parsing, caching, and same-run sharing change together.

**S06 — Request budgets (WP-08).** Main files: `src/core/tokenizer.py`, `src/core/api_config.py`, `src/cross_check/cross_checker.py`, `src/compliance/compliance_checker.py`, `src/core/chunked_pass.py`, and `src/review/review_request_builder.py`. May split: the budget contract and gates first, then subdivision and the extended-output threshold.

**S07 — Compliance completeness (WP-09).** Main files: `src/compliance/compliance_checker.py`, `src/core/chunked_pass.py`, result models, both exporters, diagnostics, and program aggregation.

**S08 — Repair recovery (WP-14).** Main files: `src/orchestration/pipeline.py`, `program_pipeline.py`, and `batch_resume.py`; `src/gui/batch_controller.py` and `review_run_controller.py`; and `scripts/recover_batch.py`. May split: the outcome contract and one shared cleanup decision first, then provisional reports and deferred downstream stages. It touches GUI code, so see Part 1 rule 3 about tkinter.

**S09 — Attempt accounting (WP-15).** Main files: review and repair result aggregation in `src/orchestration/pipeline.py`, `src/orchestration/diagnostics.py`, `src/review/realtime_review.py`, and the cost displays.

**S10 — Word content controls, fields, smart tags (WP-02).** Main files: `src/input/extractor.py`, `src/review/prompt_serialization.py`, `applier/locator.py`, and `applier/docx_edit.py`. May split: extraction plus applier refusal first, then nested depths and identical-text safety.

**S11 — Edit locations, part 1 (WP-06B).** Main files: groups and occurrences in `src/orchestration/pipeline.py`; `applier/sidecar.py`, `models.py`, `run.py`, `receipt.py`, and `policy.py`. The reader must land before the writer (S12). In this chunk the sidecar writer still emits schemas 4 and 5.

**S12 — Edit locations, part 2 (WP-06B).** Main files: `src/output/edit_sidecar.py`, both exporters, the applier end-to-end tests, and the docs. The writer moves to schemas 6 and 7.

**S13 — Routing (WP-05).** Main files: `src/programs/assignments.py`, `src/programs/routing.py`, the routing models, extractor metadata, and headless preparation. It comes after S10 because a SECTION heading can sit inside a content control.

**S14 — Automatic numbering (WP-03).** Main files: `src/input/extractor.py` (or a new numbering helper), `ParagraphMapping`, prompt serialization, section attribution, the heading candidates from S03, and the applier's locator and editor. May split: the resolver and display labels first, then the consumers and the applier boundary.

**S15 — Retries (WP-11).** Main files: `src/verification/retry_policy.py` and every loop the app retries itself: research, verification, batch retrieval, and token counts.

**S16 — Tracing and keys (WP-13).** Main files: `src/gui/review_run_controller.py`, `batch_controller.py`, and `context_controller.py`; `src/tracing/session.py`; `src/core/api_config.py`; and the client factory in `src/review/reviewer.py`. May split: tracing plus deep-trace thinking first, then credentials. It touches GUI code, so see Part 1 rule 3 about tkinter.

**S17 — Evidence and fetch (WP-16).** Main files: tool-result parsing and the prompt in `src/verification/verifier.py`, the evidence and cache models, trace and report display, and the verifier golden files.

**S18 — Docs and prompts match the code (WP-17).** Main files: `src/core/api_config.py`, the prompts, banners and status summaries, `CLAUDE.md`, `README.md`, and `handbook/`. It also fixes every row still open in "Known-wrong statements".

**S19 — Correctness release.**

- **Bump the version** in every literal the release check reads (CLAUDE.md, "Windows desktop build + self-update").
- **Release notes:** move the collected lines from `PROGRESS.md` into README "Changelog (recent)".
- **Smoke test:** put a short Windows smoke-test checklist in the pull request for the owner to run before merging.
- **Tag:** the agent never pushes a tag. It tells the owner the exact tag command to run after merging.

After S19 merges, the agent prints the milestone notice.

**S20–S25 — Experiments (EX-01 … EX-06).** At the start of each one, ask the owner (with the question tool) whether a live evaluation is authorized. That needs a spending cap and an API key in the session's environment. Without both, do only the offline part and record "not evaluated". That is a valid, complete result for the chunk. Each chunk writes a decision record at `plans/experiments/EX-0N-<name>.md` and changes no default unless the promotion criteria in Part 4 pass. S25 may split.

---

## Part 3 — What was checked on September 23, 2026

### Summary

- **Nothing in this plan had been implemented.** Since the original plan's baseline (`01781ed`), master had received only docs and test-cleanup pull requests (#371–#373) and the plan file itself. The only change under `src/` was one docstring line.
- **Every package's defect is still present** in master `f9da027`. `plans/check_plan_status.py` reproduces 25 of them, all OPEN; the rest were confirmed by reading the code. Each package in Part 4 now opens with a "Current state" note giving the evidence.
- **Starting test result:** 3,979 passed, 14 skipped, and 10 network tests deselected, in about 30 seconds. All 14 skips are gaps in the cloud container, not failures: 11 need tkinter (all of `tests/test_program_pipeline.py` is one of them), 1 needs tiktoken's rank file offline, 1 needs PyInstaller, and 1 needs Playwright.

### Corrections made to the original plan

1. **WP-17 item 8 named the wrong models.** It said "Sonnet 4.6 is 40% of Opus 4.6 token pricing, not one fifth." The review statement it corrects (API-3) was about Sonnet 5 vs Opus 5. At current list prices Sonnet 5 ($2/$10 per million tokens) is 40% of Opus 5 ($5/$25), and Sonnet 4.6 ($3/$15) is 60% of Opus 4.6 ($5/$25). Corrected in Part 4.
2. **Scheduling.** The original assumed parallel agents plus an integrator. Work now runs as one session at a time in Part 2's order, and the dependency notes in Part 4 §4 explain that order.
3. **Paths.** The original said paths were relative to `C:/Github-Repos/Claude-Spec-Critic`. They are relative to the repository root, wherever it is checked out.

### Documentation that is wrong today

Five statements in the docs and code comments describe behavior the code doesn't have: one about the verification cache, one about token counts, two in the handbook about extraction and the sidecar, and one about Haiku's cache minimum. The table in `plans/PROGRESS.md` ("Known-wrong statements") lists each one with where it is, what's true, and the chunk that fixes it. Until then, don't rely on them.

### Claims in the plan that were checked and are right

- Haiku 4.5's minimum cacheable prompt is 4,096 tokens (provider documentation).
- List prices per million tokens: Opus 5 $5/$25, Sonnet 5 $2/$10, Opus 4.6 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5. They match `src/core/pricing.py`.
- Web fetch can use any URL already present in the conversation, not only URLs from a prior search.
- Thinking display defaults to "omitted" on Opus 5 and Sonnet 5, and "summarized" returns a readable summary. Display changes what's visible, not what's billed.
- The GUI copies the API key into the process environment in exactly three places.
- Sidecar schema numbers 6 and 7 are unused, and the applier reads only 4 and 5.
- Every test suite the plan names exists. The three new ones it suggests don't exist yet.
- CI uses Python 3.11 and Node 22, and runs `python -m pip check` and `python -m pytest` with `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`.

Prices and model capabilities change, so recheck them before any live evaluation.

---

## Part 4 — Detailed specifications (the original plan, corrected)

The rest of this document is the original September 22 plan, changed in three ways:

- Each package opens with its session, a plain-words summary, and its current state.
- The corrections in Part 3 are applied.
- Revision notes mark where the session process replaces the original's parallel-agent guidance.

Section numbers are the original's. Line numbers in the "Current state" notes are approximate; search for the named function if they've drifted.

### 1. Outcomes and scope

Implement the confirmed correctness and reliability fixes before making model, prompt, or architecture changes whose benefits require measurement. The intended outcomes are:

1. Supported Word content reaches review completely, in order, with trustworthy document locations.
2. Deterministic checks recognize ordinary specification structure without generating misleading alerts.
3. Distinct findings and distinct edit locations survive review, grouping, export, and application.
4. Every outbound package request has a model-aware input budget.
5. Reports distinguish completed analysis, incomplete coverage, operational failure, and legitimate uncertainty.
6. Outstanding paid work retains a usable recovery record, and every known paid attempt is accounted for.
7. The embedded chat recovers from stream/tool failures without corrupting subsequent conversations.
8. Verification evidence is more inspectable without pretending that retrieval or text overlap proves a claim.
9. Performance changes are promoted only after reproducible quality and cost comparisons.

#### Required work versus gated work

- **WP-01 through WP-17 are required implementation packages.** Their acceptance criteria define the correctness release. WP-03 includes bounded support for common Word automatic numbering; unsupported formats must remain visible as limitations.
- **EX-01 through EX-06 are evaluation or staged capability packages.** Complete their investigation and record an evidence-backed decision. A default change is not required when the experiment fails, evidence is insufficient, or an authorized evaluation budget is unavailable.
- Do not label a gated experiment implemented merely because a prompt or flag exists. Separate implementation, offline validation, live evaluation, and default enablement.
- Do not reopen already-correct behavior just to match an obsolete report statement. In particular, preserve the coverage-first confidence rubric, the standard-edition/adoption qualifications, forced default Haiku triage, existing continuation caching, and real-time per-attempt telemetry.

The implementation agents should inspect the then-current repository before editing. This plan identifies behavior and contracts, not immutable line numbers. If a defect has been fixed since the baseline, demonstrate that with the acceptance tests and close the corresponding item without rewriting it.

### 2. Execution rules and engineering invariants

Read the repository's current guidance, including CLAUDE.md, applicable AGENTS.md files, and CI configuration. Preserve unrelated user changes. Use isolated branches/worktrees when multiple agents would otherwise edit the same files; follow the repository's branch and PR conventions.

#### Invariants that must survive every package

- The main application emits edit instructions. The separate applier remains separate; nothing under src/ imports applier/.
- The input specification is never overwritten by the applier.
- Failed verification, inconclusive verification, local classification, and grounded conclusive verification remain distinct.
- A valid zero-findings review is different from a failed or unparseable review.
- Partial failures never erase completed findings.
- Every executable instruction retains the actual source file, target location, and original locator text.
- Report grouping must not destroy executable occurrences.
- Source retrieval, native attribution, and semantic support are separate concepts.
- Governing editions derive from applicable adoption/project authority, not merely the newest publication or a module default.
- No duplicate retry ownership: app-owned loops disable SDK retries; single-shot SDK-owned paths retain them.
- No duplicate billing from cached/shared results or aggregate-plus-per-attempt telemetry.
- Persisted state never contains API keys or full specification bodies. Do not casually expand persisted content as part of recovery work.
- GUI widget changes remain marshaled through the existing dispatch mechanism.
- The final HTML stays self-contained, escapes untrusted content, and has a CSP hash matching its exact executable script.
- Golden changes must be reviewed for meaning. Bulk regeneration is not proof of correctness.

#### Validation policy

Write regression tests for confirmed bugs and changed contracts, not tests that merely mirror an implementation. Prefer complete, adversarial examples and boundary transitions.

Tests should use the existing fake Anthropic fixtures, injected clocks/random sources, and temporary fixture outputs. No normal test may call a paid API. Explicitly select non-network tests even if the developer's environment contains a real key.

Live comparisons require a bounded evaluation run with a dataset, maximum spend, stopping rule, and results artifact. CLAUDE.md already treats applicability evaluation and cache adoption as measurement-gated. This plan does not invent an unlimited API-spend authorization.

### 3. Traceability from the review

| Review issue or adjustment | Owning package | Session |
|---|---|---|
| P1-1: false empty headings; related duplicate-heading false positives | WP-04, WP-01 | S03, S01 |
| P1-2: issue normalization merges different findings | WP-06A | S02 |
| P1-3: content controls, simple fields, smart tags | WP-02 | S10 |
| Automatic-numbering information loss noted under P1-1 | WP-03 | S14 |
| P1-4 / API-4: unsafe package token gates | WP-08 | S06 |
| P1-5: swallowed stream errors and unmatched tool calls | WP-12 | S04 |
| P2-1: persistent UNVERIFIED cache entries | WP-10 | S05 |
| P2-2 / API-8: Retry-After and synchronized retries | WP-11 | S15 |
| P2-3: SECTION-heading and compact-name routing | WP-05 | S13 |
| P2-4/5/6/7: suppression, ASCE, TBD, naming checks | WP-04 | S03 |
| P2-8: trace startup strands the GUI | WP-13 | S16 |
| P3: Haiku minimum, request-count terminology, standards wording | WP-17 | S18 |
| P3: extended-output threshold | WP-08 | S06 |
| P3: search-first fetch restriction | WP-16, WP-17 | S17, S18 |
| P3: summarized thinking for deep traces | WP-13 | S16 |
| P3: placeholder prefixes and long-form editions | WP-04 | S03 |
| P3: blank sources and missing no-op demotion reason | WP-10, WP-17 | S05, S18 |
| P3: chat citation replay and session key persistence | WP-12 | S04 |
| P3: process environment API-key exposure | WP-13 | S16 |
| API-1: shared project-context caching | EX-01 | S20 |
| API-2: native citation capture and calibrated validation | WP-16, EX-04 | S17, S23 |
| API-3/7: escalation model and review effort | EX-03 | S22 |
| API-5: schema-constrained final output | EX-02 | S21 |
| API-6: coverage-first prompt already substantially addressed | WP-17, EX-03 | S18, S22 |
| ARCH-1: cross-chunk and cross-module coordination | WP-17, EX-06 | S18, S25 |
| ARCH-2: collector divergence | WP-14 | S08 |
| ARCH-3: shared citation resolution | EX-04 | S23 |
| ARCH-4: research reuse | EX-05 | S24 |
| ARCH-5: realistic clean and mutated fixtures | WP-01 | S01 |
| New: multiple locations in one file collapse | WP-06B | S11–S12 |
| New: applier chooses the first same-named input | WP-07 | S02 |
| New: absent compliance coverage can look complete | WP-09 | S07 |
| New: malformed real-time verdict is treated as cacheable uncertainty | WP-10 | S05 |
| New: pending repair loses its automatic recovery record | WP-14 | S08 |
| New: repair replaces the original attempt's cost | WP-15 | S09 |

### 4. Dependencies and parallel work

> **Revision note (2026-09-23):** Sessions now run one at a time in the order of Part 2, so there is no concurrent file ownership to manage and no separate integrator. The dependency notes below are the reason Part 2 is ordered the way it is. Read "agent" as "session" throughout.

Start with a short contract review, not a broad framework rewrite. Agree the document-location contract, occurrence/sidecar contract, coverage-completeness metadata, collection outcome contract, and attempt-usage contract before agents edit shared models.

| Track | Work | Dependencies and integration boundaries |
|---|---|---|
| Input | WP-02, WP-03, WP-04, WP-05 | WP-01 starts fixtures. Numbering follows traversal/source-mapping decisions. Coordinate extraction IDs with applier and occurrence work. |
| Finding and edit identity | WP-06, WP-07 | WP-07 can land first. New sidecar reader must land before or with the new writer. |
| Request sizing and coverage | WP-08, WP-09 | Share result metadata with recovery/report owners. Coverage must preserve partial chunk findings. |
| Verification | WP-10, WP-11, WP-16 | Parse/cache/sharing changes land together. Citation enforcement remains separate from capture. |
| UI and chat | WP-12, WP-13 | HTML exporter overlaps citation/report work; assign one integrator or serialize edits. |
| Recovery and accounting | WP-14, WP-15 | Agree repair lifecycle and attempt identities first. Both affect pipeline.py and diagnostics. |
| Integration | WP-17 | Follow all required packages; review changed goldens and documentation together. |
| Experiments | EX-01 through EX-06 | Start measurements after WP-15 makes repair costs trustworthy and relevant correctness packages pass. |

The largest merge-conflict surfaces are pipeline.py, verifier.py, reviewer.py, api_config.py, both exporters, and CLAUDE.md. Do not give independent agents unrestricted concurrent ownership of these files. Each agent should return its contract changes, tests, compatibility consequences, and unresolved assumptions to the integrator.

Small fixes need not wait for the largest migration. Good early releases include the filename-normalization defect, applier filename rejection, heading correction, chat error propagation, trace failure handling, and malformed-verdict/cache policy. Keep each change reviewable; do not hide schema or recovery redesign inside a regex-fix PR.

### 5. WP-01 — Baseline, fixtures, and contract tests

> **Session:** S01.
>
> **In plain words:** Build the small fake Word documents and tests that later sessions need, and record what the test suite looks like before anything changes.
>
> **Current state (2026-09-23):** Not started. The three suggested suites (`test_extraction_content_controls.py`, `test_extraction_numbering.py`, `test_heading_structure.py`) don't exist. The starting test result is in Part 3. The review's reproductions are in `plans/check_plan_status.py`. S01 turns them into strict-xfail tests, so a fix makes its test pass and forces the fixing session to remove the marker, then deletes the script.

**Purpose:** Make the expected behavior independent of model responses and prevent newly supported input structures from breaking edit application.

**Primary targets:** tests/fixtures/, tests/test_deterministic_checks.py, tests/test_preprocessor_policy.py, extraction tests, routing tests, tests/test_edit_sidecar.py, applier tests, and tests/fixtures/fake_anthropic.py.

#### Work

1. Record the actual starting commit and existing non-network test result. Identify any pre-existing failures or skips without attributing them to new work.
2. Create a small, anonymized specification fixture family, using deterministic DOCX builders where clearer than committed binaries.
3. Include manual CSI numbering, automatic numbering, rich/dropdown controls, simple fields, hyperlinks, tracked insertions/deletions, table-only article bodies, merged/nested tables, and representative compact filenames.
4. Keep clean fixtures and single-defect mutations separate. A clean zero-alert document cannot prove that TBD or an alternate stale citation is detected.
5. Pin public contracts: extraction reconstruction, unique IDs, old-ID meaning, safe unsupported-element handling, group versus occurrence identity, coverage completeness, and collection cleanup eligibility.
6. Turn the offline reproductions from the review into maintained behavioral tests. Do not depend on the original review session's scratch files.
7. Ensure generated input/output fixtures live in temporary test directories; never alter real project documents.

#### Acceptance

- Clean documents produce zero alerts for the categories they are intended to exercise.
- Each mutated document produces its expected alert/warning/failure and does not produce unrelated alerts.
- Every new XML container is tested through extraction and through the locator/editor boundary.
- The same extracted input reaches ordinary and repair request construction consistently.
- No network connection or production credential is needed.

**Suggested new focused suites:** test_extraction_content_controls.py, test_extraction_numbering.py, test_heading_structure.py, and a compact input-to-sidecar integration suite. Use existing suites when they already express the behavior cleanly.

### 6. WP-02 — Restore supported Word content without corrupting locations

> **Session:** S10 (may split).
>
> **In plain words:** Some text in Word files never reaches the review: text inside content controls (the fill-in boxes and dropdowns in templates), the stored results of fields, and smart tags. Read that text in order, and make sure the applier can't edit through those wrappers by mistake.
>
> **Current state (2026-09-23):** Open. A paragraph inside a block content control and a REF field result ("23 05 00") are both missing from extraction (reproduced). `_collect_accept_all_text` in `src/input/extractor.py` (~line 400) deliberately doesn't descend into `w:sdt` or `w:smartTag`, and `w:fldSimple` isn't handled at all.
>
> **Doc fix in the same session:** `handbook/04_input.md` (~line 20) lists what is "still not extracted" and leaves these out. Correct it.

**Priority:** P1.  
**Targets:** src/input/extractor.py; src/review/prompt_serialization.py; applier/locator.py; applier/docx_edit.py; extraction, prompt-serialization, and applier tests.

#### Required behavior

Support block and inline content controls, smart-tag containers, and stored visible results of simple fields. Traverse body paragraphs, cells, nested tables, and currently supported supplemental surfaces without losing or duplicating text.

#### Implementation requirements

1. Add explicit traversal for w:sdt/w:sdtContent, w:smartTag, and w:fldSimple where appropriate. Use structured traversal, not a catch-all descendant-text search.
2. Apply Accept-All revision semantics at every supported depth: include insertions and move-to content; exclude deletions and move-from content. Audit hyperlinks containing revisions too.
3. Read stored field results only. Never execute fields, fetch external field content, or treat field instructions as visible prose.
4. Preserve source order and emit each visible fragment once. Nested tables and independently collected text boxes must not be double-counted.
5. Warn on unsupported text-bearing structures that remain omitted. Distinguish known unsupported content from a proven count of lost characters; do not fabricate completeness metrics.
6. Preserve legacy element-ID meaning. Existing pN IDs address physical body children; ordinary tN IDs correspond to the existing table indexing semantics. Wrapped tables must not silently renumber ordinary tables.
7. Assign newly supported wrapped blocks a distinct stable structural path namespace. Either implement matching deterministic resolution in the same release or explicitly classify the new surface as unsupported for automatic application.
8. Treat reviewability and editability separately. The writer currently handles direct runs. An inline field/control becoming readable does not authorize editing through it. Refuse an edit intersecting an unsupported wrapper, even if similar text elsewhere would match.
9. Keep ExtractedSpec content and paragraph-map reconstruction consistent, preserving source_path and unique element identity.

#### Acceptance cases

- A block control containing a paragraph and table yields both in the correct order.
- Inline dropdown text survives between surrounding runs; an unresolved stored dropdown placeholder reaches preprocessing.
- The stored REF result “23 05 00” and smart-tag text survive.
- Nested controls and controls in table cells honor accepted/deleted revisions.
- Adding a wrapped table before an ordinary table does not make the ordinary table's existing address point elsewhere.
- Each new ID resolves to the intended physical container or produces an explicit unsupported result.
- Identical text inside and outside a control never causes an edit to target the wrong occurrence.
- Existing supported documents retain their established text and legacy locations.

**Release boundary:** Do not claim universal Word support. Document exactly which wrappers and surfaces are handled and which remain readable-only or unsupported.

### 7. WP-03 — Preserve automatic numbering as display metadata with source mapping

> **Session:** S14 (may split).
>
> **In plain words:** Word's automatic numbering ("1.01", "A.") isn't in the text the review sees, so articles lose their numbers. Show the numbers to the review and the detectors, but never let the applier treat a generated number as editable text.
>
> **Current state (2026-09-23):** Open. `src/input/extractor.py` has no numbering support: no `numPr`, `numId`, or `abstractNum` handling.
>
> **Doc fix in the same session:** add automatic numbering to the handbook's list of what is and isn't extracted (`handbook/04_input.md`).

**Priority:** P2; necessary to restore article identifiers and reliable structural review on common Word templates.  
**Targets:** src/input/extractor.py and an optional focused numbering helper; ParagraphMapping; prompt serialization; section attribution; applier locator/editor boundaries.

#### Design decision

Represent the displayed number separately from literal editable run text. A synthesized “1.01” is not a substring in w:t and must never be treated as an ordinary replaceable span.

#### Work

1. Resolve common Word numbering via numPr, numId, abstractNum, level text, starts, overrides, restarts, and style-inherited numbering.
2. Keep numbering state document-local and scoped to the correct list instance. Do not share counters across files or extraction workers.
3. Supply displayed labels to review, section attribution, and structural detection. Also define how plain context-DOCX extraction exposes labels.
4. Preserve both literal source text and any synthetic display spans. If content becomes the displayed view, update reconstruction deliberately and retain a separate source-text contract for editing.
5. Keep the actual XML structural locator independent of the display label.
6. Do not duplicate manually typed numbering.
7. Preserve body text and issue a clear warning for unsupported numbering formats or ambiguous resolution. Do not guess counters.
8. Refuse automatic edits to synthetic labels. Translate an edit involving only actual body text only when the source mapping proves the offsets; otherwise report it for manual action.

#### Acceptance

- An automatically numbered article is shown as “1.01 SUMMARY” and attributed to section 1.01.
- Multilevel lists, overrides, restarts, and independent list instances remain distinct.
- Typed labels are not repeated.
- Revision handling never resurrects deleted body content.
- An edit containing a synthetic prefix cannot delete unrelated source characters.
- An edit to genuine body text following the label still locates correctly.
- Unsupported numbering produces an honest warning, not silent loss.

**Tests:** Add numbering-specific fixtures and an extraction → prompt → finding → applier boundary test. Do not extend the XML writer to edit numbering definitions as part of this package.

### 8. WP-04 — Correct deterministic structure and text detectors

> **Session:** S03 (may split).
>
> **In plain words:** The local checks that run before any AI call raise false alarms and miss real problems. Every PART heading is flagged "empty", and ordinary lines like "2 coats of primer" are treated as headings. Bare TBD is missed, and so are stale code years sitting next to words like "prior" or "historical".
>
> **Current state (2026-09-23):** Open. Reproduced with the plan's own examples:
>
> - The clean 3-PART spec in Appendix A gets 3 false "Empty section" alerts (PART 1, 2, 3). "2 coats of primer…" is read as a heading, and a repeated quantity line becomes a "duplicate heading". The cause is `_HEADING_LINE_RE` / `detect_empty_sections` in `src/input/preprocessor.py`.
> - All three example sentences in B below produce 0 alerts, because `_STALE_CYCLE_SUPPRESS_PATTERNS` includes `prior`, `historical`, and `may not`.
> - `ASCE/SEI 7-16`, `ASCE 7–16` (en dash), and `ASCE 7-2016` are not recognized.
> - Bare `TBD` isn't detected, and `[EDITION …]` and `[SELECTED …]` are flagged as EDIT and SELECT placeholders. `PLACEHOLDER_PATTERNS` has no word boundaries.
> - `21 05 00.docx`, `211313.docx`, and `SECTION 21 13 16.DOCX` together produce no naming notice. `detect_inconsistent_file_naming` stays silent when unrecognized names are the largest group.

**Priority:** P1 for heading false positives; P2/P3 for the narrower checks.  
**Targets:** src/input/preprocessor.py, module-owned detector vocabulary, and relevant deterministic/golden tests.

#### A. Heading hierarchy

- Replace the flat heading interpretation with qualified candidates carrying normalized number, title, level, source position, and available style/numbering provenance.
- Exclude ordinary integer-led prose, including “2 coats of primer,” “12 inches minimum,” and “1 year from Substantial Completion.”
- Do not merely accept every dotted number: “1.5 inches minimum” is also prose. Use structural shape and metadata conservatively.
- A heading's content extends through its subtree to the next sibling/ancestor or EOF. A PART with populated articles is not empty.
- Report a truly empty leaf article. Report an empty PART when it has no substantive descendant content. Define a nonredundant ancestor/leaf alert policy.
- Reuse qualified candidates for duplicate-heading checks; fixing empty detection alone must not leave quantity paragraphs classified as duplicate headings.
- Preserve rule IDs, original text, positions, deterministic ordering, and alert limits.

#### B. Stale-citation suppression

Replace unrelated nearby keywords with citation-related historical/rejection phrases. Preserve genuine “previous edition” and “superseded citation” contexts while flagging active requirements in:

- “Submit shop drawings prior to fabrication in accordance with 2022 CBC Section 1704.”
- “Coordinate with the historical society and comply with 2022 CBC.”
- “Contractor may not deviate from 2022 CBC Chapter 17.”
- Equivalent “shall not deviate” and “cannot depart” formulations.

Retain clause boundaries and test multiple citations in one sentence. Preserve modules that intentionally suppress stale-cycle checks; do not turn a syntax improvement into a new governing-edition policy.

#### C. Citation syntax

Recognize ASCE/SEI, the optional word Standard, ordinary Unicode dash variants, and two-/four-digit edition years. Normalize editions before comparison, preserving plausibility checks and century handling.

Add the demonstrated long-form California references through the relevant module vocabulary: “2019 California Building Standards Code,” “2022 Edition of the CBC,” “CBC (2022 edition),” and jurisdiction-qualified “Title 24, 2022.” Do not equate generic Title 24 with CBC or activate California assumptions in other modules.

#### D. Placeholders

- Detect standalone TBD and deduplicate overlap with bracketed TBD.
- Require keyword boundaries so EDITION does not become EDIT and SELECTED does not become SELECT.
- Keep product identifiers such as TBDF-200 clean. Define the policy for TBD-200 explicitly.
- Decide whether a complete bracketed OPTIONAL marker is an editorial choice in the supported templates; do not automatically classify every such marker as a false positive.
- Preserve existing legitimate marker detection and avoid treating new text extraction as proof that every bracketed phrase is defective.

#### E. Filename consistency

Recognize separated and compact six-digit names, optional SECTION prefixes, and extension case variants. Unknown names must not suppress an observed mixture among recognized styles.

When there is no dominant style, report a neutral mixture rather than inventing a winning convention. Keep this informational naming notice separate from coverage/routing defects.

#### Acceptance

The clean three-PART fixture in Appendix A produces no empty alerts. True empty/duplicate articles still alert. Every syntax expansion has positive and negative tests. The original suppression examples flag, genuine historical references remain suppressed, and location-aware module policies remain unchanged.

**Tests:** test_deterministic_checks.py, test_preprocessor_policy.py, test_asce7_stale_editions.py, test_keyword_word_boundaries.py, test_preprocessor_synthetic_paragraphs.py, and the affected golden-domain suites.

### 9. WP-05 — Route from credible document metadata and preserve source identity

> **Session:** S13.
>
> **In plain words:** In the data-center program, each spec is sent to a discipline module mostly based on its filename. A compact name like `210500.docx` is rejected even when the document itself says SECTION 21 05 00. Read the document's own SECTION heading.
>
> **Current state (2026-09-23):** Open, reproduced. `210500.docx` whose body starts "SECTION 21 05 00" routes as unsupported. `211313.docx` with "SECTION 21 13 13" routes as ambiguous. `ExtractedSpec` has no section number or title fields, and `src/programs/assignments.py` passes `section_title or filename`, so the body heading never reaches `route_spec`.

**Priority:** P2.  
**Targets:** src/programs/assignments.py, src/programs/routing.py, routing evidence models, extractor metadata, and headless preparation entry points.

#### Work

1. Extract an actual SECTION heading from a bounded opening body region. Require heading-shaped text; “See Section 21 13 13” in a related-sections paragraph is not the document's identity.
2. Carry section number and section title separately, with provenance. Avoid extracting identity repeatedly with different rules in assignment and routing code.
3. Support a compact leading filename number when corroborated by the body heading. Preserve guards against dates, project identifiers, NFPA references, and arbitrary embedded numbers.
4. Surface contradictory strong filename/body evidence as ambiguity. Do not silently prefer whichever regex runs first.
5. Preserve intended unsupported-division behavior and legacy fire-alarm routing corroboration.
6. Use the ExtractedSpec's trustworthy source path or an unambiguous input mapping. Reject distinct inputs with colliding basenames at non-GUI boundaries before submission; the GUI-only guard is not a universal invariant.
7. Retain assignment provenance and behavior across saved-state serialization and relevant resume paths.

#### Acceptance

- 210500.docx + SECTION 21 05 00 routes to fire suppression.
- 211313.docx + the corresponding wet-pipe heading is supported.
- A compact filename without credible corroboration remains conservative.
- Related-section references do not override the real heading.
- Contradictory headings/names produce an explicit ambiguous result.
- Unsupported Division 27/28 scopes remain unsupported as intended.
- Reversed input order cannot silently select a different same-named source.

**Tests:** test_program_routing.py, test_datacenter_routing.py, test_domain_routing_pins.py, test_program_pipeline.py, and test_file_name_collision_guard.py. Cover the headless boundary, not just the GUI selector.

### 10. WP-06 — Preserve semantic findings and executable occurrences

> **Sessions:** WP-06A in S02 (with WP-07); WP-06B in S11 and S12.

#### WP-06A: Remove unsafe filename normalization

> **In plain words:** When two findings differ only in the words between a section number and a filename, dedup deletes those words and merges the findings, so the second one disappears from the report.
>
> **Current state (2026-09-23):** Open. `_normalize_issue_text` (`src/orchestration/pipeline.py`, ~line 440) still strips `\d{2}\s?\d{2}\s?\d{2}[^.]*\.docx`. Reproduced: the copper and PVC findings in Appendix A both normalize to "section ." and `_deduplicate_findings` keeps only one.

**Priority:** P1.  
**Targets:** pipeline.py functions _normalize_issue_text, _dedup_key, compute_finding_id, and _deduplicate_findings.

1. Delete the generic pattern that strips a CSI number through the next .docx.
2. Normalize only exact known corpus filenames with literal escaping and clear boundaries. Prefer retaining an unknown filename to deleting potentially meaningful prose.
3. Apply one normalization context consistently across review, cross-check, compliance, and stable finding-ID generation. Avoid global mutable corpus state.
4. When no corpus context is available, preserve the text rather than guessing a filename.
5. Preserve case/whitespace normalization that does not change meaning. Keep rf-/cf-/lc- namespaces.
6. Document that newly generated IDs may change when the old key was wrong; do not rewrite existing exported reports.

**Acceptance:** The copper/PVC examples in Appendix A remain distinct. Otherwise identical issues mentioning different known source filenames can still group. Overlapping filenames, spaces, punctuation, uppercase extensions, unknown filenames, and reordered input do not corrupt identity.

#### WP-06B: Separate display groups from executable locations

> **In plain words:** If the same fix is needed in two places in one file, only the first place makes it into the edit instructions. The second is silently dropped.
>
> **Current state (2026-09-23):** Open. Reproduced: the same EDIT at p4 and p8 of one file becomes one group and one sidecar entry targeting p4. The applier reads only schemas 4 and 5 (`SUPPORTED_SCHEMA_VERSIONS` in `applier/sidecar.py`), and `is_program` compares against 5 only. Schema numbers 6 and 7 are unused.
>
> **Split across two sessions.**
> - **S11** adds the occurrence model, stable occurrence IDs, the applier reader for schemas 4, 5, 6, and 7, and conflict detection. The writer still emits 4 and 5.
> - **S12** moves the writer to 6 and 7 and updates the exporters, receipts, docs, and end-to-end tests.
>
> **Doc fix in S12:** `handbook/11_trust_model_and_output.md` (~line 28) says "The sidecar no longer under-emits". That's true across files but not within one file. Correct it.

**Priority:** P1.  
**Targets:** Finding, FindingOccurrence, group_findings, per-original lookup, edit_sidecar.py, both exporters, and applier models/reader/receipt/policy.

1. A display group may have several locations in one file and several files. Replace “filename → first original” with location-preserving occurrences.
2. Prefer validated element identity; combine it with instruction identity where different actions or insertion sides target one anchor.
3. Collapse genuine duplicate emissions at the same target. Preserve distinct validated targets.
4. Without a resolvable element ID, preserve uncertainty. Identical prose does not prove identical location. Never manufacture two actionable locations from indistinguishable anchors.
5. Create stable occurrence IDs independent of input order and presentation counters such as grp-0000. Include module identity for program entries.
6. Keep representative verification/display fields separate from each occurrence's original executable fields.
7. Preserve explicit missing-original status; do not silently borrow another location's anchor.
8. Audit all consumers of finding_id and EditEntry.key, including receipts, filters, chat links, and conflict detection.
9. Detect incompatible instructions targeting the same region. Hold/report the conflict; do not let application order choose a winner.
10. Keep resolve-before-mutate behavior so earlier insertions cannot shift later targets.

#### Sidecar migration

The current single-module schema is 4 and the program schema is 5. A naive increment would reuse an existing meaning.

Reserve **6 for single-module occurrence-aware output and 7 for program occurrence-aware output**, after verifying these are still unused. New entries must carry occurrence_id and a documented unique-entry contract; program identity must include module provenance.

Upgrade the applier to read 4/5/6/7 before or with the writer. Preserve legacy 4/5 interpretation without pretending missing location information was recovered. LoadedSidecar.is_program must handle both program versions. Old readers should refuse new schemas cleanly.

Update sidecar documentation, receipts, compatibility tests, and release notes together. This package is an actual contract migration.

#### Acceptance

- Same issue at p4 and p8 produces two sidecar entries and two correct tracked changes.
- Duplicate emission for p4 produces one instruction.
- Two files with two locations each produce four instructions with their own anchors.
- Cross-module entries do not collide.
- Conflicting edits are visible and withheld appropriately.
- Legacy sidecars still behave identically.
- New report → sidecar → applier → receipt preserves every executable occurrence.

**Tests:** test_dedup_edit_identity.py, test_cross_check_finding_ids.py, test_edit_sidecar.py, test_applier_sidecar.py, test_applier_run.py, test_applier_docx_edit.py, and test_applier_isolation.py.

### 11. WP-07 — Refuse ambiguous applier file bindings and destinations

> **Session:** S02 (with WP-06A).
>
> **In plain words:** If the applier is given two different files that are both named `spec.docx`, it silently uses whichever came first and may edit the wrong one.
>
> **Current state (2026-09-23):** Open. `_index_specs` (`applier/run.py`, ~line 83) maps each lower-cased filename to the first path it sees ("first occurrence winning"). Reproduced: reversing the input order binds the other file.

**Priority:** P1. This safety fix can land independently of the schema migration.  
**Targets:** applier/run.py, applier/cli.py, applier/models.py, applier/receipt.py.

#### Work

1. Map a normalized basename to all distinct resolved supplied paths, not the first path.
2. Repeating the same actual input is harmless; different same-named inputs are ambiguous.
3. Resolve every file binding and destination before writing. Hold affected instructions with a specific ambiguity reason and account for them in the receipt.
4. Check output collisions, source/destination aliasing, and destinations that would overwrite another supplied source—not just the current source.
5. Do not let model assistance select among ambiguous files.
6. If a later explicit mapping is introduced, restrict it to supplied inputs. A path embedded in a sidecar is not authority to read or overwrite arbitrary files.
7. Keep uniquely bound files actionable when other files are held, with an appropriately non-success exit/report outcome.
8. Ensure dry-run and real-run decision logic match.

#### Acceptance

Two project folders containing spec.docx remain ambiguous in either input order. Repeating one resolved file does not create false ambiguity. Case behavior matches the supported filesystem policy. Colliding destinations are rejected before writes. Every held instruction appears in the receipt. No source file is overwritten.

**Tests:** test_applier_run.py, test_applier_sidecar.py, relevant CLI/receipt tests, and a multi-file output-dir scenario.

### 12. WP-08 — Use canonical, model-aware request budgets

> **Session:** S06 (may split).
>
> **In plain words:** Before sending the big cross-check and compliance requests, the app measures their size with the wrong ruler: a local count that runs low for Sonnet 5. So a request can be too big for the model. Measure with the right ruler, split the work when needed, and never silently cut anything.
>
> **Current state (2026-09-23):** Open.
>
> - Cross-check and compliance choose between one call and chunks using raw local counts: `src/cross_check/cross_checker.py` (~lines 336 and 655) and `src/compliance/compliance_checker.py` (~lines 630 and 815).
> - The batch extended-output threshold also uses a raw local count (`src/review/review_request_builder.py`, ~line 171).
> - Count-API results are called "exact" in `src/core/tokenizer.py` (~lines 105, 169–170, and 433) and `src/orchestration/pipeline.py` (~lines 671 and 686). Change that wording in this session.

**Priority:** P1.  
**Targets:** core/tokenizer.py, core/api_config.py, cross_check/cross_checker.py, compliance/compliance_checker.py, core/chunked_pass.py, review/review_request_builder.py, and review preparation/preflight paths.

#### Budget contract

Build each actual request once and derive its counting form from the same inputs. Include system, user content, tools, project context, prior findings, chunk notes, and supported request features that affect input counting.

The API count is a model-aware estimate, not a mathematical guarantee. Keep a documented reserve. The effective input ceiling must not exceed the selected model's context window minus its actual requested output cap and the safety reserve. Preserve existing practical phase limits where more conservative.

Never treat a raw cl100k count or a safety multiplier as measured Anthropic usage.

#### Work

1. Introduce a small common request-budget result: count, count source, selected model, input ceiling, requested output reserve, fit decision, and unavailability reason when applicable.
2. Reuse the existing preflight toggle and API-count helper, correcting misleading “exact” terminology. Validate malformed/absent count responses; they must not become a trustworthy zero.
3. When preflight is unavailable or explicitly disabled, use model-specific conservative padding on all locally counted request components, including tool overhead.
4. Decide single-call versus chunked execution using that result. Apply the same policy to every final chunk.
5. Avoid duplicate preflights for identical shapes within a run. Any count cache must include the model and every shape-changing input; never reuse a count across model overrides.
6. Extend grouping with bounded token-aware subdivision when one CSI bucket is still too large. Preserve order, source ownership, and stable chunk identities. A large single bucket must not be assumed safe merely because grouping occurred.
7. If a single document plus required context cannot fit, report an explicit unanalyzed portion or skipped request. Do not silently truncate document text, requirements, or prior-finding context.
8. Preserve completed chunks if another cannot run. Surface reduced cross-chunk coordination and exact failed/skipped coverage.
9. Honor per-network-call concurrency gates during preflight where needed; avoid holding a permit across an entire multi-chunk operation.
10. Use the same count source for the batch extended-output threshold. Resolve input shape/count before output-cap selection to avoid a circular builder. Recheck the final context fit after choosing the cap. Real-time review must remain on its supported non-extended path.

#### Acceptance

Use stubbed counts rather than giant synthetic requests:

- Local count under 822k, API count over the safe ceiling → chunking, no oversized message submission.
- Preflight unavailable → padded estimate blocks the same unsafe case.
- Fitting API count → one request with the counted shape.
- Model override to a smaller window → smaller safe ceiling.
- Tool/context overhead changes → a different decision when appropriate.
- One oversized CSI group → deterministic subdivision; an unsplittable item is explicitly surfaced.
- Mixed successful/failed chunks preserve all completed findings.
- Batch extended-output threshold uses the selected count basis; streaming never receives the batch-only beta.
- Invalid count responses never authorize an oversized call.

**Tests:** test_token_budgets.py, test_cross_check_chunking.py, test_compliance_pass.py, test_chunked_pass_engine.py, test_token_analysis_gate_and_threading.py, test_prompt_serialization.py, test_realtime_review.py, and client-factory/retry ownership tests.

**Numerical note:** Under the existing 822,000 input budget and 1.45 fallback multiplier, the corresponding local count is about 566,897, not 690,000. Do not hard-code that conversion as a universal limit; derive it from the model/request policy.

### 13. WP-09 — Make compliance completeness explicit

> **Session:** S07.
>
> **In plain words:** If the compliance model skips some requirements, the report still looks complete. Track which requirements were actually assessed, and say clearly when some weren't.
>
> **Current state (2026-09-23):** Open. `src/compliance/compliance_checker.py` doesn't compute an expected coverage set or the omitted IDs, and nothing marks coverage as incomplete.

**Priority:** P1 because an incomplete analysis can currently appear successful.  
**Targets:** src/compliance/compliance_checker.py; src/core/chunked_pass.py; review result models; both exporters; diagnostics; program aggregation.

#### Required contract

A successful API response is not evidence that every controlling requirement was assessed. Track execution status and coverage completeness separately.

1. Define the expected coverage set from grounded, controlling, non-process requirements. Unverified research items remain advisory and must not silently become mandatory coverage rows.
2. Reconcile prompt instructions and examples with that set. Remove contradictory instructions about covering every profile item versus only controlling items.
3. Normalize returned coverage against known IDs, then compute omitted expected IDs. Distinguish an explicitly returned “unclear” assessment from a row the model never returned.
4. Represent an omitted row as synthetic “unclear/not assessed,” with a reason and an origin marker. Never infer “represented,” “missing from the specification,” or a successfully completed assessment from omission.
5. Add metadata for expected count, returned expected count, omitted IDs/count, and completeness. Document zero-expected-item behavior.
6. Preserve usable findings and returned rows. Do not discard an otherwise useful response merely because some rows are absent.
7. Propagate completeness through chunk synthesis, program aggregation, JSON/profile output where applicable, DOCX, HTML, and diagnostics. A prominent partial-analysis notice must survive every output path.

#### Chunk semantics

Inspect every consumer before introducing new status values. At the baseline, chunk synthesis keeps findings only for results whose status is “completed.” Adding a “partial” status in one producer could erase valid findings downstream.

Prefer additive completeness metadata while retaining existing execution status semantics, unless the entire status contract is migrated together. A completed request with omitted coverage can have incomplete assessment metadata; an operationally failed request stays failed.

A conclusion that a requirement is absent across a scope requires every relevant chunk to have been assessed sufficiently to support that conclusion. A failed, skipped, or omitted chunk cannot establish global absence. Preserve useful supported findings, but suppress executable additions that depend on unassessed absence; retain them visibly as conditional/report-only findings with the reason.

Review the existing coverage merge precedence and ADD filtering together. Do not let synthetic rows turn unknown coverage into a proved deficiency. Define how contradictory and represented evidence in different chunks is summarized without discarding their locations.

#### Acceptance

- Empty coverage with a nonempty controlling set produces an incomplete report.
- One omitted ID remains distinguishable from one explicitly returned unclear row.
- Unknown IDs, duplicate rows, malformed rows, and reordered rows are handled deterministically.
- A zero-controlling-item profile produces a valid no-applicable-items result.
- Failed/skipped chunks preserve completed findings and prevent unsupported global-absence edits.
- Single-file, package, program, GUI, CLI, and recovered reports display the same completeness semantics.

**Tests:** test_compliance_pass.py, test_chunked_pass_engine.py, test_report_status.py, test_program_pipeline.py, and exporter tests.

### 14. WP-10 — Separate failed verification, uncertainty, and reusable verdicts

> **Session:** S05.
>
> **In plain words:** When the verifier can't decide ("UNVERIFIED"), the app saves that non-answer for 60 days and replays it, so later runs never retry. A garbled verifier reply can also pass as an ordinary "couldn't decide", and an empty source string can count as a real citation.
>
> **Current state (2026-09-23):** Open. Both of these are reproduced:
>
> - A grounded UNVERIFIED result is written to the cache and returned as a hit. `VerificationCache.put` (`src/verification/verification_cache.py`, ~line 634) checks `grounded` and the failure and budget flags, but not the verdict.
> - A DISPUTED verdict whose only source is `""` still classifies as DISPUTED.
>
> **Doc fix in the same session:** CLAUDE.md's "Budget-exhaustion sentinel" section (~line 441) says "The `grounded` guard already drops every UNVERIFIED". That's false. Correct it, and any related cache text.

**Priority:** P1.  
**Targets:** src/verification/verifier.py; src/verification/verification_cache.py; pipeline verification sharing and diagnostics.

#### Parsing and failure classification

1. Audit real-time and batch verdict construction against one classification contract.
2. An ordinary end-of-turn response with malformed or missing verdict content is an operational/parsing failure, not a grounded UNVERIFIED result.
3. Preserve attempt usage, error details, and any safely captured evidence on failures. Failure must not become zero-cost merely because no verdict parsed.
4. A legitimate, well-formed UNVERIFIED verdict remains genuine uncertainty. Do not force it to be “ungrounded” just to bypass the cache.
5. Handle refusal, output exhaustion, malformed tool input, missing expected tool output, and unexpected stop reasons explicitly.
6. Do not add an unbounded repair/escalation loop. Use the existing bounded policy, with a clear terminal result.

#### Persistent cache eligibility

Use one predicate consistently at write, read, and disk-load boundaries.

- Reuse only eligible grounded conclusive verdicts, such as properly evidenced CONFIRMED or DISPUTED results that satisfy existing source/quote requirements.
- Do not persist or reuse UNVERIFIED, malformed, failed, budget-exhausted, or local-only results as conclusive evidence.
- Ignore ineligible old entries individually. Do not flush unrelated valid cache entries.
- Preserve schema compatibility when a policy-only change suffices. If the serialized evidence contract changes, document migration and versioning.
- Continue validating expiry and standards fingerprints. Treat invalid timestamps or nonfinite numeric data as invalid records, not cache hits.
- A later independent run must be able to retry an earlier inconclusive finding under the normal escalation policy.

#### Same-run sharing must remain functional

The current sharing helper assumes grounded results are handled by the persistent cache. Removing UNVERIFIED from that cache requires a coordinated single-flight change.

Allow an eligible, well-formed UNVERIFIED result to be shared among equivalent in-flight findings within the same run, without making it a persistent success. Explicitly exclude parsing failures, local classifications, and budget failures from this uncertainty-sharing rule. Ensure cancellation or leader failure releases waiting followers.

Followers must not duplicate billed usage. Preserve evidence and outcome, but clear or otherwise exclude attempt usage and chargeable counters through the existing shared-result accounting contract.

#### Nonblank source validation

Reject citation arrays whose entries contain only whitespace or empty fields. Apply the same minimum substantive-source rule across batch and real-time paths. Preserve existing authority and quote-validation rules; source presence alone never proves support.

#### Acceptance

- The same malformed response fails in batch and real-time modes.
- Failed parses retain known token usage and never become durable cache hits.
- A valid UNVERIFIED result is shared once within a run but retried in a later run.
- Existing valid conclusive cache entries still load; legacy UNVERIFIED entries are ignored.
- Whitespace-only citations fail evidence validation.
- Concurrent followers settle correctly after success, uncertainty, failure, and cancellation.
- Cost diagnostics bill only the actual leader attempts.

**Tests:** verifier parsing suites; verification cache serialization/source-quote/LRU tests; pipeline sharing and concurrency tests; diagnostic cost tests.

### 15. WP-11 — Respect rate-limit timing and release concurrency during backoff

> **Session:** S15.
>
> **In plain words:** When the API says "slow down, retry in N seconds", the app ignores N and retries on its own schedule. Every worker retries at the same moment, and each keeps holding its concurrency slot while it waits.
>
> **Current state (2026-09-23):** Open. There is no Retry-After handling and no jitter anywhere in `src/`.

**Priority:** P2.  
**Targets:** src/verification/retry_policy.py; application-owned API loops; research, verification, batch retrieval, and token-count call gates.

#### Retry contract

1. Normalize applicable server delay headers, including numeric Retry-After seconds, HTTP-date Retry-After, and supported millisecond delay headers.
2. Validate values. Missing, malformed, negative, nonfinite, or expired values fall back to the local retry policy.
3. Use bounded exponential backoff with injected randomness when no valid server floor exists. Add jitter without retrying before a valid server-requested delay.
4. Bound both attempt count and elapsed retry budget. If a server delay exceeds the remaining budget, stop or defer with a clear outcome; do not shorten the delay and immediately retry.
5. Preserve documented meaning of existing retry-count settings, including zero. Do not inadvertently change “retries” into “total attempts.”
6. Keep retryable classifications explicit. Authentication and invalid-request errors should not be repeatedly retried as transient failures.
7. Keep batch per-item error waves distinct from transport requests with HTTP headers. Apply the appropriate policy to each layer without inventing missing headers.

#### Concurrency and ownership

Acquire request concurrency at the actual outbound call boundary, including continuations and escalation calls. Release it before sleeping. Do not wrap an entire research dimension or finding-verification lifecycle in a network semaphore and then sleep while holding a slot.

Avoid nested acquisition of the same gate. Keep any deliberate server-limited pool semantics separate from local network concurrency.

For each outbound path, document its retry owner. App-owned retry loops must use a client with SDK retries disabled. Paths intentionally relying on SDK retries should not acquire a second app retry loop. Include batch-result retrieval and token-count calls in this audit.

#### Acceptance

Use injected clocks/sleep/random functions; tests must not wait in real time.

- Numeric, HTTP-date, millisecond, malformed, and absent headers behave as specified.
- Concurrent calls receive different local backoff delays.
- No retry precedes a valid server floor.
- Another request can use a slot while the first call backs off.
- Cancellation interrupts retry waiting promptly.
- Attempt and elapsed limits both terminate correctly.
- One simulated transient failure produces the expected number of actual API calls, without hidden double retries.

**Tests:** test_retry_policy.py, test_client_retry_policy.py, test_batch_results_retry.py, test_research_concurrency.py, test_collection_call_gate.py, and token-analysis gate tests.

### 16. WP-12 — Make embedded chat streaming transactional and recoverable

> **Session:** S04 (may split).
>
> **In plain words:** In the HTML report's Ask AI chat, an error in the middle of an answer is swallowed. The chat keeps a half-finished tool call in its history, so every later message fails. The API key is also kept in browser storage.
>
> **Current state (2026-09-23):** Open.
>
> - `src/output/html_report_exporter.py` (~line 2693) wraps the event handler itself in `try { onEvent(JSON.parse(raw)); } catch (err) { /* ignore malformed frame */ }`, so errors thrown while handling an event are swallowed.
> - Invalid tool-argument JSON becomes `{}` (~lines 2817–2818).
> - The key is stored with `sessionStorage.setItem("sc_api_key", …)` (~line 2948).
> - The only JavaScript test is a syntax check (`node --check`). There is no behavioral harness yet.

**Priority:** P1.  
**Targets:** src/output/html_report_exporter.py and the exact embedded JavaScript it emits; HTML JavaScript/exporter tests.

#### Stream parser and error boundaries

1. Narrow catch blocks to the operation they can recover from. A JSON decoding guard must not swallow an API error thrown by event handling.
2. Surface API error events, transport failures, reader failures, malformed required data, and premature EOF to the chat state machine.
3. Process split UTF-8 characters, arbitrary byte boundaries, LF/CRLF event separators, multiple data lines where supported, and the decoder's final buffered data.
4. Require a valid terminal stream state before treating a response as complete. EOF without the expected terminal event is not success.
5. Parse tool arguments explicitly. Invalid/incomplete arguments must not silently become an empty object that triggers a tool with unintended defaults.
6. Handle each relevant stop reason, continuation limit, and tool-call limit visibly and deterministically.

#### Conversation transaction

Maintain a working turn and a committed, replayable conversation.

- Never leave an assistant tool-use block in committed history without the corresponding tool result required for the next request.
- On failure, either roll back the uncommitted portion or append a valid error tool result according to a documented policy. Choose one consistent transaction model and test it.
- Commit complete assistant/tool exchanges at explicit boundaries. Retain earlier committed turns.
- Show partial text as interrupted when useful, but do not silently replay it as a complete assistant answer.
- Preserve supported server-tool blocks and thinking/signature fields needed for valid continuation rather than flattening everything to text.
- Stop, New Chat, and model changes must invalidate outstanding callbacks using a turn/session identity. An old promise must not append text or tool results into a newer conversation.
- Restore controls in a finally path regardless of the failure origin.

#### Citation replay

Accumulate streamed citation deltas into the correct text block. Preserve their association when rendering, serializing history, and replaying subsequent messages. Handle multiple text blocks and unknown optional citation shapes without fabricating attribution.

#### API key lifetime

Keep the key in page memory for the current session. Stop storing it in sessionStorage; remove the legacy stored-key entry without automatically reimporting it. Clear in-memory references when the user chooses Forget Key and on page teardown where practical.

Wrap optional nonsecret preference storage so unavailable or restricted storage does not break chat. Preserve report context and other intended functionality.

#### Acceptance

Run behavior tests against the exact shipped script, not a hand-copied simplified parser.

- An error event following tool_use cannot corrupt the next turn.
- Premature EOF, invalid tool JSON, request rejection, and reader failure all restore the UI and leave valid history.
- Split frames and multibyte text reconstruct correctly.
- Citation deltas survive the next request.
- Stop/New Chat/model-change races cannot mutate the wrong session.
- No API key is written to web storage or interpolated into the exported report.
- Existing HTML escaping and CSP checks still pass.

Use the existing Node-based test harness for deterministic stream tests. Add a browser smoke test only if it exercises behavior the harness cannot cover; keep any browser interception tooling a test dependency.

**Tests:** test_html_report_javascript.py and test_html_report_exporter.py; new focused behavioral cases in the existing harness.

### 17. WP-13 — Make tracing optional, honor deep-trace settings, and limit credential lifetime

> **Session:** S16 (may split).
>
> **In plain words:** If the trace folder can't be created, the app gets stuck in "processing". A key typed into the app is copied into the process environment, where child processes can see it. And deep traces record empty "thinking" because the app never asks for the readable summary.
>
> **Current state (2026-09-23):** Open.
>
> - The trace recorder starts before the worker's `try` in both the submit worker and the resume worker (`src/gui/batch_controller.py`, ~lines 165 and 1285). `start_run_recorder` (`src/tracing/session.py`) doesn't catch its own failures.
> - The GUI key is written to `os.environ["ANTHROPIC_API_KEY"]` in three places: `src/gui/context_controller.py` (~line 332), `src/gui/review_run_controller.py` (~line 368), and `src/gui/batch_controller.py` (~line 1189).
> - No core request sets `thinking.display`. On Opus 5 and Sonnet 5 the default is "omitted", which gives empty thinking text.

**Priority:** P2.  
**Targets:** src/gui/review_run_controller.py, src/gui/batch_controller.py, and src/gui/context_controller.py; tracing startup/teardown; src/core/api_config.py and client construction; token-analysis and other consumers of runtime client configuration.

#### Failure-safe run startup

Move trace startup, session reattachment, and other fallible preparation inside the worker's lifecycle protection. A trace-directory or recorder failure must not strand a busy GUI or suppress the primary review error.

- Continue the review without tracing when only optional tracing fails, with one concise warning.
- Dispose of a partially initialized recorder and its worker resources.
- Clear the global recorder only if it belongs to the failed/finished run; an old callback must not clear a newer run's recorder.
- Ensure widget restoration and completion notification run on every exit.
- Teardown failures must not replace the meaningful review exception.
- Cover both fresh-run and resumed/reattached-run entry points.

#### Thinking display in deep traces

When a supported deep-trace setting is enabled, request the documented summarized thinking display on the relevant core API paths. Keep ordinary requests unchanged. Apply model/feature capability checks and preserve existing chat behavior, which already requests summarized thinking.

The setting changes diagnostic visibility; it is not a promised cost reduction. Do not log absent/omitted thinking as if it had been returned.

#### GUI credential ownership

Stop copying a GUI-entered key into process-global environment variables in the three affected controller flows.

Pass a nonserialized runtime credential/client provider through the existing orchestration boundaries. Capture the selected credential/client for a run so changing the UI field mid-run does not unexpectedly mix accounts or clients.

Preserve command-line environment input as a supported input path. Do not temporarily set and restore a global environment variable around a call; that remains unsafe with concurrent workers.

Review subprocess launches the application controls and sanitize inherited sensitive variables where feasible without breaking platform behavior. Do not claim that os.startfile supports an explicit environment. The primary fix is eliminating GUI injection into the global environment.

#### Acceptance

- Trace initialization and reattachment failures leave a usable GUI and an otherwise functioning review.
- Teardown after a partial startup leaves no live recorder thread or stale active recorder.
- A failed old run cannot clear a new run's recorder.
- Deep trace changes only the intended supported request field; normal-mode request snapshots remain stable.
- Fake GUI keys do not appear in the process environment, saved state, report payloads, or tracing metadata.
- CLI environment credentials and concurrent runs still behave correctly.

**Tests:** trace recorder teardown/retention tests; GUI controller tests; prompt/request serialization tests; client configuration tests. Use fake keys exclusively.

### 18. WP-14 — Preserve pending repairs and unify collection completion decisions

> **Session:** S08 (may split).
>
> **In plain words:** A repair batch is a paid re-run of the specs whose review failed. While one is still running, the app can throw away the saved record it needs to collect it, and resuming can pay for downstream work again. Make the keep-or-clear decision in one place, and keep the record until the repair is actually finished.
>
> **Current state (2026-09-23):** Open.
>
> - The GUI clears saved state after any successful collection (`src/gui/batch_controller.py`, ~line 901). It does so even when `_reattach_saved_repair_batch` found the repair still pending and collection went ahead with the primary results only.
> - `scripts/recover_batch.py` (~lines 599–606) uses a different rule.
> - There is no structured collection outcome.

**Priority:** P1.  
**Targets:** src/orchestration/pipeline.py; src/orchestration/program_pipeline.py; GUI batch/review controllers; CLI collection; saved batch state; repair collection tests.

#### Collection outcome contract

Separate “there are reportable primary findings” from “the remote job and its repairs are finished.”

Represent at least these distinctions explicitly: no repair needed, repair submitted/pending, repair temporarily unreachable, repair consumed, and repair conclusively terminal/unusable. Unknown transport state is not terminal completion.

Persist the submitted repair batch ID and the item/request mapping required to retrieve it. Preserve compatibility with existing recovery manifests. Infer legacy state conservatively; missing newer metadata must not authorize destructive cleanup.

Return a structured collection outcome that carries completion and cleanup eligibility alongside usable results. Do not hide pending repair inside an ordinary success return.

#### Cleanup policy

1. Centralize the decision to clear saved work and use it in GUI, CLI, single-module, and program flows.
2. Keep state while a repair is pending, temporarily unreachable, or otherwise retrievable but unconsumed.
3. Keep an all-failed collection recoverable by default, matching the safer existing CLI behavior. Explicit discard is a separate user action.
4. A valid zero-findings completed review may clear state when no outstanding work remains.
5. Partial results may be displayed without clearing unresolved state.
6. Validate the run/batch identity before clearing. A stale callback cannot delete a newer run's recovery record.
7. Avoid blind resubmission. Resume the existing repair batch unless it is conclusively unusable and the retry policy explicitly permits replacement.

#### Prevent duplicate downstream work on resume

A reportable primary result with a pending repair must not trigger a fresh cycle of paid downstream analysis every time collection is retried.

Prefer the simplest reliable policy: expose primary results as provisional and defer dependent verification, cross-check, compliance, and drawing coordination until the required repair inputs settle. Indicate which stages are awaiting repair.

For program runs, resolve collection readiness before launching dependent paid stages, or maintain explicit completed-stage checkpoints with input fingerprints. Do not introduce checkpoints casually: a changed repair result can invalidate downstream inputs and legitimately require new work, which must be visible and accounted for.

Re-reading remote completed results is different from starting new paid analysis. Tests should count actual paid submissions, not prohibit harmless collection polling.

#### Collector consolidation

Share the pure outcome/cleanup and stage-readiness decisions first. Consolidate more of the four collection paths only if that reduces verified drift without a risky wholesale rewrite. Preserve progress callbacks and GUI dispatch.

#### Acceptance

- Primary output requiring repair remains automatically recoverable while repair is pending.
- Restart/resume collects the same repair ID and does not submit a duplicate.
- A temporary retrieval failure preserves state.
- All-failed, valid-empty, partial, successful, and explicitly discarded outcomes follow the same rules across entry points.
- Program children retain unresolved state independently and do not repeat completed paid work.
- A provisional report identifies pending stages.
- A stale completion cannot clear a newer manifest.

**Tests:** existing batch/repair collection and program-pipeline tests; saved-state roundtrip tests; GUI/CLI parity cases; paid-call-count assertions.

### 19. WP-15 — Account for every actual review and repair attempt

> **Session:** S09.
>
> **In plain words:** When a repair re-run replaces a failed review, the cost of the failed original disappears from the cost report, so the total is too low.
>
> **Current state (2026-09-23):** Open. `_merge_repair_results` replaces the primary result (`src/orchestration/pipeline.py`, ~line 1786). `collect_review_batch_results` adds up usage only after that merge, so the original attempt's paid usage is dropped.

**Priority:** P1 for trustworthy cost reporting.  
**Targets:** reviewer/repair result aggregation; src/orchestration/diagnostics.py; batch and real-time collectors; cost/report displays.

#### Accounting contract

Keep finding selection separate from attempt accounting. Replacing unusable primary findings with repaired findings must not replace the original request's known billable usage.

1. Create or consistently use a per-attempt usage representation carrying operation, model, transport, token/cache categories, known search usage, and a stable attempt identity.
2. For remote batches, batch ID plus item/custom ID and attempt role should identify an attempt without conflating the original review and its repair.
3. Count known usage from failed, truncated, or malformed attempts. Unknown usage stays unknown, with a diagnostic qualifier; do not invent zero or estimate it as an exact invoice.
4. Preserve primary and repair attempts even when only repaired findings become the final result.
5. Choose one billing input per operation: attempt records or an already aggregated total. Do not charge both.
6. Preserve existing real-time attempt telemetry, which already includes primary/repair calls. Refactor consumers without double-counting it.
7. Apply the relevant pricing categories: ordinary versus batch token prices, input/output, cache writes by TTL, reads, and separately priced search usage.
8. Cached/shared followers must not duplicate the leader's charge.

#### Recovery scope and presentation

When recovering an older batch, distinguish known historical batch spend from new spending caused by recovery. Do not imply that a recovery report reconstructs an account invoice or earlier research costs that were never saved.

Expose useful subtotals for original review, repair, verification/escalation, and other passes where records exist. Keep estimates labeled as estimates.

#### Acceptance

- Primary plus repair cost equals the sum of both attempts even when the primary result is replaced.
- Failed parses with usage remain billable in diagnostics.
- Batch discount, cache TTL categories, reads, and search fees are each applied once.
- Repeated collection of the same attempt does not duplicate it.
- Existing real-time totals remain unchanged for an equivalent scenario.
- Shared followers cost zero additional API spend.
- Legacy results without attempt metadata remain readable and visibly limited.

**Tests:** test_diagnostics_cost_pricing.py, test_cache_write_accounting.py, test_diagnostics_budget_telemetry.py, test_realtime_review.py, and batch-repair collection tests.

### 20. WP-16 — Capture native evidence and correct fetch instructions

> **Session:** S17.
>
> **In plain words:** The API's own citations for the verifier's sources are thrown away, and the prompt wrongly tells the model it may only fetch URLs that came from a search.
>
> **Current state (2026-09-23):** Open.
>
> - Nothing in `src/verification/` captures `cited_text` or other native citation data.
> - The fetch instructions say fetch is for "a URL that previously appeared in a web_search result" (`src/verification/verifier.py`, ~line 1087).
> - The provider documentation (checked 2026-09-23) allows fetching any URL already present in the conversation.

**Priority:** P2.  
**Targets:** verifier tool/result parsing; evidence/cache models; trace and report presentation; verifier prompt goldens.

#### Evidence capture now; stronger enforcement later

Capture the native citation and retrieval information already returned by supported responses. Preserve, where available:

- Source URL/title and retrieval/tool identity.
- Quoted cited text and returned locator information.
- Association between a citation, its source document/result, and the claim/verdict.
- Which attempt/model produced it and whether it was fresh, cached, or shared.

Some citation shapes use document indexes and locations rather than a direct URL. Resolve through the corresponding response document metadata. Never attach an arbitrary nearby URL when the mapping is unknown.

Preserve compatibility with legacy cache entries lacking these optional fields. Bound stored/displayed evidence to what supports the verdict; do not start persisting whole fetched documents. Unknown optional citation shapes should be observable without discarding an otherwise valid result.

Maintain distinctions between model-written source text, native attribution, retrieved evidence, and validated semantic support. A native citation is evidence of attribution, not proof that the requirement or proposed edit is correct.

#### Fetch prompt correction

Update instructions that categorically require search before every fetch. Document the provider-supported path for fetching a URL explicitly supplied by the user or already present in allowed conversation context, subject to the tool's actual constraints.

This does not mean a user-supplied URL is already verified. The verifier must still retrieve relevant content and assess support, edition, authority, and applicability.

Preserve existing tool capability gates. Do not enable fetch for a response-format/model combination that the client currently cannot support merely because the prompt mentions it.

#### Acceptance

- Native citation metadata survives parsing, evidence display, and optional cache roundtrip.
- Missing native metadata leaves legacy behavior usable and honestly labeled.
- Document-index citations resolve only to the correct source.
- Blank sources remain invalid under WP-10.
- Search-derived and user-supplied permitted URL paths have consistent instructions.
- No lexical-overlap threshold changes acceptance in this package.

**Tests:** verifier evidence/source tests; cache serialization; prompt goldens; trace/HTML display tests.

### 21. WP-17 — Reconcile prompts, diagnostics, and documentation

> **Session:** S18.
>
> **In plain words:** A clean-up pass: make the prompts, report wording, and docs say exactly what the code does.
>
> **Current state (2026-09-23):** Open.
>
> - The Haiku cache minimum is still given as 2048 (`src/core/api_config.py`, ~lines 961 and 999; CLAUDE.md, ~line 633). The provider's figure for Haiku 4.5 is 4,096.
> - A no-op EDIT built outside the parser shows as report-only, but its `demotion_reason` stays empty, so the banner counts 0 demotions instead of 1 (reproduced).
> - Item 8 below has been corrected.

**Priority:** P2/P3; complete before the correctness release.

1. Correct the Haiku prompt-cache minimum comment/documentation to the provider's current supported threshold; the reviewed value is 4,096 tokens. Verify capability tables against the selected model before implementation.
2. Describe token-count API results as estimates, not an exact guarantee. Distinguish provider counts, local estimates, fallback padding, context capacity, and configured output capacity.
3. Preserve and document existing cache continuation behavior instead of presenting it as missing.
4. Preserve the implemented coverage-first confidence rubric and governing-edition/adoption qualifications. Resolve remaining contradictory examples without weakening evidence requirements.
5. Make no-op demotion explainable. Derive or record a clear demotion reason at a stable normalization boundary; do not mutate findings inside a read-only report helper. Keep report severity counts and sidecar exclusion consistent.
6. Document the current cross-check scope before EX-06: analysis is within its actual chunks/modules, and a small program is not automatically checked across disciplines merely because it fits in one context window.
7. Distinguish “analysis incomplete,” “verification inconclusive,” “operational failure,” and “no issue found” in banners and status summaries.
8. Keep model/price commentary accurate. **Corrected 2026-09-23:** at current list prices, Sonnet 5 ($2/$10 per million tokens) is 40% of Opus 5 ($5/$25), not the "one fifth" the review said. Sonnet 4.6 ($3/$15) is 60% of Opus 4.6 ($5/$25). The original text of this item named Sonnet 4.6 and Opus 4.6. Recheck current prices before a live comparison.
9. Describe thinking display as visibility control, not a reduction in billed reasoning.
10. Update architecture notes, troubleshooting, schema/recovery documentation, and trust-audit checklists only after their corresponding behavior is validated.
11. Review affected prompt/report goldens individually. Explain intentional changes and retain assertions for evidence, authority, escaping, and confidence behavior.
12. Keep release notes specific: fixed omissions, safer edits, visible incomplete coverage, retained repairs, and corrected accounting. Do not announce unmeasured quality or cost gains.
13. Fix every row still open in the "Known-wrong statements" table in `plans/PROGRESS.md`.

**Acceptance:** Documentation matches actual defaults and supported paths. Every previously identified misleading statement is corrected or explicitly marked as an unresolved measured proposal. No experiment is described as enabled before its promotion criteria pass.

### 22. EX-01 — Measure shared project-context prompt caching

> **Session:** S20. **In plain words:** Measure whether caching the shared Project Context text would actually save money. Turn it on only if the numbers say so.
>
> **Current state:** Not started. It needs S06 and S09 first.

**Classification:** Measured optimization; do not promise savings before observing cache reads.  
**Prerequisites:** WP-08 and WP-15; stable request serialization.

#### Investigation and implementation

1. Capture the exact current request layout: tools, system blocks, project context, per-file content, continuation history, cache breakpoints, TTLs, and model.
2. Identify a genuinely identical project prefix across eligible requests. Module instructions, changing project facts, or different tools/models can prevent reuse.
3. Test whether moving stable project context ahead of variable file content improves reuse without altering prompt meaning or introducing a new trust boundary.
4. Respect the provider's current breakpoint limits and mixed-TTL ordering rules. Count breakpoints already used by continuation/resume logic; do not add one in isolation.
5. Keep variable filename/content, repair-specific material, and changing metadata outside the intended stable prefix where feasible.
6. Make cache write/read usage visible per attempt using WP-15. Include write premiums, expiry, batch behavior, and real-time request timing in the comparison.

#### Evaluation and promotion

Compare baseline and candidate on the same corpus, model, prompts, and settings. Measure cold writes and warm repeated requests separately. Include a changed-context control that must invalidate the intended prefix.

Report cacheable-prefix size, actual read/write tokens, net cost, latency, and any output-quality change. Do not infer cache reuse solely from a configured cache_control field or a request occurring “within five minutes.”

Promote only if measured net savings or latency gains are worthwhile and the request semantics remain sound. Otherwise retain the baseline and document the result. Do not force a caching redesign merely to complete this item.

### 23. EX-02 — Evaluate schema-constrained final outputs

> **Session:** S21. **In plain words:** Test whether making the model answer in a strict format reduces unreadable replies without hurting quality.
>
> **Current state:** Not started. Default Haiku triage already forces its tool; keep that.

**Classification:** Reliability improvement requiring capability and stop-reason validation.  
**Prerequisites:** WP-10; current client/model capability inventory.

#### Scope

Evaluate provider-supported schema-constrained final output for review, compliance, research, or verification where it reduces real parse failures. Default Haiku triage already forces its output tool; preserve that behavior rather than treating it as missing.

#### Requirements

1. Select a bounded first consumer with a measurable parse-failure problem.
2. Distinguish strict tool argument validation, forced tool invocation, and a constrained final response. One does not automatically imply the others.
3. Verify supported combinations of model, thinking, tools, citations, and output format against current documentation and the pinned SDK.
4. Preserve current capability guards, including incompatible fetch/format paths. Do not assume native citations and every constrained-output mode compose.
5. Keep semantic validation after schema validation: valid JSON can still have unknown IDs, missing coverage, blank evidence, unsafe edits, or contradictory claims.
6. Keep explicit refusal and output-limit handling. Schema guarantees do not eliminate all terminal failure modes.
7. Read older saved batch responses using the compatible legacy parser. Do not make a pending pre-upgrade batch uncollectible because new submissions use a new format.
8. Implement behind a narrow configuration switch until validated.

#### Promotion criteria

Demonstrate fewer unparseable outputs without increased omission, unsupported findings, or loss of useful evidence. Compare retry/repair rate, total attempts, latency, and cost.

Offline fake responses must cover valid constrained output, legacy output, refusal, truncation, missing content, and unsupported capability selection. An SDK upgrade, if required, belongs in a separate reviewed change with its own compatibility checks.

### 24. EX-03 — Evaluate model selection, effort, and confidence behavior

> **Session:** S22. **In plain words:** Test whether a different escalation model or effort level would be better or cheaper. The current defaults stay unless the measurements say otherwise.
>
> **Current state:** Not started. Current defaults: Opus 5 for review and escalation, Sonnet 5 for verification, and an effort ceiling of `high`.

**Classification:** Quality/cost decision; preserve current defaults until measured.  
**Prerequisites:** WP-01, WP-10, WP-15, and WP-16 where evidence is scored.

#### Dataset and controls

Build an adjudicated set of representative findings with source evidence and expected outcomes. Include:

- Correct citations and supported deficiencies.
- Wrong editions and adoption-date ambiguity.
- Numeric/units differences, exceptions, and negation.
- Ambiguous or inaccessible evidence that should remain UNVERIFIED.
- Plausible but false model claims.
- Low-severity factual issues and severe omissions.
- Project-specific overrides and mixed module authority.

Record corpus/prompt/schema versions and dataset hashes. Separate training/tuning examples from held-out evaluation cases.

Isolate both in-memory and disk caches by experimental arm, or use fresh isolated instances. The existing cache is intentionally not simply keyed by model; disabling disk persistence alone does not prevent in-memory cross-arm contamination.

#### Experiments

Evaluate one change at a time:

1. Current escalation path versus a compatible alternative model.
2. Current review effort versus one justified lower or higher setting.
3. Specific remaining prompt contradictions or confidence-calibration changes.

Keep governing-basis fingerprint behavior and existing feature defaults unchanged unless that is itself the isolated experiment. Do not silently enable an unrelated currently disabled feature.

#### Metrics and decision

Measure severe-defect recall, unsupported-finding rate, false CONFIRMED and false DISPUTED rates separately, legitimate uncertainty, evidence quality, repair/failure rate, cost, and latency distribution. Report sample sizes and uncertainty; a few examples cannot establish broad quality equivalence.

Set acceptable regressions before running the evaluation. A cheaper model is not a success if it misses consequential defects. Publish the observed tradeoff and retain the current default when the result is inconclusive.

Recheck model capability and prices at evaluation time. Avoid assumptions that one model is a fixed fraction of another's cost or that an effort setting imposes a hard reasoning-token cap.

### 25. EX-04 — Calibrate evidence validation and reuse resolved sources

> **Session:** S23. **In plain words:** Build a check that a cited source really supports the claim, starting in watch-only mode, and try reusing sources across findings about the same material.
>
> **Current state:** Not started.

**Classification:** Two related but independently gated changes.  
**Prerequisites:** WP-10 and WP-16.

#### A. Evidence validation in observation mode

Develop validation against an adjudicated evidence set before allowing it to change verdicts.

- Check source identity, edition, authority, quoted support, and claim meaning.
- Include paraphrases, omitted exceptions, reversed negation, and changed numbers/units.
- Treat lexical similarity as a diagnostic feature, never an arbitrary universal acceptance threshold.
- Keep retrieval success, native citation attribution, and semantic entailment separate.
- Report disagreement with current verdicts for human assessment before enforcing a new rule.

If a stronger acceptance policy is promoted, version its semantics and prevent old cached verdicts from silently bypassing it. Target affected entries/policies rather than wiping unrelated caches.

#### B. Shared source resolution

Prototype reuse of retrieved/resolved evidence across findings about the same authoritative material.

Keys must reflect the actual claim context: normalized reference, edition, jurisdiction/adoption basis, relevant authority or client standard, source snapshot/freshness, applicable profile/module, and resolver-policy version. A bare standard name and calendar month are insufficient.

Reuse source retrieval, not a verdict stripped of its claim context. Every finding still needs its own support assessment. When the required source is absent, stale, incompatible, or insufficient, fall back to fresh resolution.

Preserve provenance and distinguish shared-source retrieval from same-finding verdict reuse. Do not pretend the current call freshly fetched content that came from a prior cache.

#### Acceptance and promotion

Offline tests prove key separation, invalidation, fallback, provenance, and no double billing. A bounded live comparison must show saved retrieval work without degraded support judgments. Promote validation and source reuse separately if only one has adequate evidence.

### 26. EX-05 — Evaluate requirements-research reuse

> **Session:** S24. **In plain words:** Test whether research results can be reused across runs for the same place and client without going stale or being applied where they don't fit.
>
> **Current state:** Not started. CLAUDE.md §10 lists it as blocked on measured repetition.

**Classification:** Measured optimization with freshness and applicability risks.  
**Prerequisites:** Correct research accounting and stable requirements-profile serialization.

#### Cache contract

Design the key from canonical, materially relevant inputs, including:

- Location/jurisdiction and the effective date basis.
- Client/project requirements and applicable module(s).
- Relevant corpus signals and explicit edition constraints.
- Research prompts, schema, tools, source policy, and resolver version.
- Settings that change authority or applicability decisions.

Do not normalize unknown values into assumed equivalents. Define how dates are resolved; “same month” is not a sufficient applicability guarantee.

Store a bounded completed research profile with its provenance and freshness metadata, not full input specifications. Incomplete/failed research is not a normal reusable success. Define partial-profile behavior explicitly if later supported.

Offer a deliberate refresh path. Show the reused profile's age and governing basis. A cache hit must not silently override newly supplied project constraints.

#### Acceptance and promotion

Test same-input reuse, every materially relevant changed-input miss, stale-entry handling, corrupted-entry rejection, and explicit refresh. Measure hit rate, saved calls, and inappropriate reuse on representative repeated projects.

Do not enable by default until the key and freshness policy have demonstrated safe applicability. Keep current governing-basis feature defaults unchanged unless separately evaluated.

### 27. EX-06 — Add bounded cross-chunk and cross-module coordination

> **Session:** S25 (may split). **In plain words:** Add a limited check for conflicts between specs in different chunks or disciplines, for example a fire-alarm spec against a sprinkler spec. It starts in observation mode.
>
> **Current state:** Not started. Today cross-check stays within one chunk and one module (CLAUDE.md, "Cross-check chunking").

**Classification:** Staged capability development, not a guaranteed small-cost patch.  
**Prerequisites:** WP-02 through WP-09, WP-15, and stable source identity.

#### Problem and initial scope

Chunked analysis can miss relationships across chunk boundaries. Existing module-specific checks also do not establish program-wide coordination, even for a small project.

Start with a narrow set of coordination facts likely to support useful, verifiable checks: equipment/system identity, capacity/rating, material, supply characteristics, location, interface requirements, and responsibility assignments. Choose the initial categories from corpus evidence rather than building a universal fact schema.

#### Architecture constraints

1. Extract source-anchored facts with original text, file/element locations, normalized values, units, scope, and uncertainty.
2. Retain raw values alongside normalized values. Do not equate similar-looking systems or units without a justified mapping.
3. Use facts to select candidate relationships and retrieve relevant original passages for final reasoning.
4. Every reported conflict must cite both sides and explain why they refer to the same relevant scope.
5. Carry module and governing-basis provenance into mixed-discipline verification. Do not verify every cross-module claim using an arbitrary default module.
6. Keep discovery bounded by explicit budgets and candidate limits. Surface unassessed areas rather than claiming complete coordination.
7. Preserve finding-group and occurrence identities through the new pass.
8. Keep the current detailed per-file analysis. A lossy digest must not silently replace the source review.
9. Apply the same token, failure, completeness, and cost contracts used by existing passes.

#### Rollout

Begin in observation mode behind a capability switch. First prove cross-chunk detection within one module, then cross-module checks for a small program.

Include fixtures where each document is individually plausible but two documents conflict, and controls where similar values belong to different equipment or phases. Measure false joins and missed conflicts, not only the number of findings generated.

Promote by category after adjudication. Report actual incremental cost and latency; do not promise “a few percent” overhead without measurement.

### 28. Validation matrix and execution order

#### Focused validation by contract

| Area | Minimum evidence before integration |
|---|---|
| Extraction and numbering | Supported visible text recovered once; revision semantics preserved; old IDs retain meaning; synthetic numbering cannot corrupt edits. |
| Deterministic checks | Clean fixtures stay clean; each single-defect mutation triggers its intended result; suppression is context-sensitive. |
| Routing | Explicit, compact, conflicting, unknown, and ambiguous inputs resolve predictably; related-section references cannot override the document's own identity. |
| Finding identity | Distinct issues survive normalization; repeated locations survive grouping/export/application; stable IDs survive ordering changes. |
| Applier | Ambiguous basename fails before mutation; legacy and new schema behavior is explicit; source files are not overwritten. |
| Token budgets | Final request shape drives the decision; fallback padding is complete; oversized single groups are handled; model overrides change ceilings. |
| Compliance | Missing rows and failed chunks remain visibly incomplete; no unsupported global-absence edit is emitted. |
| Verification/cache | Malformed output fails; UNVERIFIED is not durable; same-run sharing works; valid old entries survive. |
| Retry/concurrency | Server floors respected; jitter deterministic under tests; no slot held during backoff; one retry owner. |
| Chat | Exact exported script survives split streams, API errors, orphan-tool scenarios, cancellation races, and citation replay. |
| Tracing/credentials | Startup/teardown failure restores UI; no stale recorder survives; GUI keys remain out of process-global environment and persistence. |
| Recovery | Pending repairs survive restart; no duplicate paid submission; cleanup policy agrees across entry points. |
| Cost | Primary and repair attempts both counted once; failed-output usage retained; cached/shared results do not add charges. |
| Evidence | Native attribution roundtrips honestly; legacy data remains readable; stronger enforcement remains gated. |
| Experiments | Isolated baseline/candidate measurements; reproducible datasets; explicit enable/defer/reject decision. |

#### Test execution

> **Revision note (2026-09-23):** In the cloud container at `f9da027`, `python -m pytest -m "not network"` took about 30 seconds: 3,979 passed, 14 skipped, and 10 network tests deselected. The skips are tkinter ×11, the tiktoken rank file ×1, PyInstaller ×1, and Playwright ×1. Node 22 is installed, so the JavaScript check runs with `SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1`. The owner runs the Windows smoke test in step 6 during S19.

1. Run focused tests while developing each package.
2. Run integration tests at contract boundaries before merging consumers.
3. Run the repository's full non-network suite once the integrated correctness candidate is stable.
4. Run dependency consistency checks and the JavaScript-required CI configuration.
5. Re-run relevant checks after further changes or failures; do not repeatedly run the whole suite without a reason.
6. Perform a Windows application smoke test for review startup, recoverable batch collection, HTML chat, and safe sidecar application using controlled fixtures.
7. Validate changed report output for readable partial-status notices, evidence labels, and occurrence locations.

Use the current repository-prescribed commands and environment. At the reviewed baseline, the relevant checks include:

~~~text
python -m pip check
python -m pytest -m "not network"
~~~

Set SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1 for the required JavaScript coverage, as CI does. Ensure Node is available rather than accepting an unnoticed skip. The reviewed CI uses Python 3.11 and Node 22; follow the actual CI at implementation time.

Do not run release/version checks that assume a version change unless the release process calls for one. Do not run live API tests as part of the default suite.

#### Representative integration scenarios

Keep these as a few comprehensible end-to-end cases plus focused failure injections, rather than one enormous brittle test.

**A. Input to safe edit:** A document contains a content-control paragraph, an automatically numbered article, a table-only article body, and the same correctable issue in two locations. Extraction preserves all supported content. Grouping retains both occurrences. The sidecar preserves literal locators. Application edits only supported targets and refuses unsupported wrapper intersections.

**B. Incomplete analysis:** Mocked request counts force chunking. One chunk fails, another omits a controlling coverage row, and another returns useful findings. The final report retains useful findings, displays incomplete coverage, and emits no unsupported global-absence addition.

**C. Paid repair and resume:** A primary review is truncated with known usage. A repair batch remains pending. A provisional report is available, saved state survives restart, and downstream paid work is deferred. Later collection consumes the existing repair, accounts for both attempts, completes downstream work once, and only then clears eligible state.

**D. Verification and user recovery:** Repeated equivalent findings share one legitimate UNVERIFIED result within the run. A malformed result is a failure rather than a cache hit. A later independent run retries the uncertainty. Chat encounters an API error after a tool request, restores controls, and sends a valid next turn.

### 29. Compatibility, rollout, and rollback

#### Sidecar and applier migration

Land a reader capable of the legacy and new sidecar contracts before or with the new writer. Document exact supported versions and fail clearly on unsupported versions.

Do not downgrade a new multiple-occurrence sidecar into a legacy format by dropping locations. A rollback to an older application build may require retaining the newer applier reader for already-generated sidecars. Original sidecar files and source documents remain unchanged.

#### Saved state and result models

Prefer additive optional fields with explicit legacy defaults. Missing completeness, attempt-usage, or repair-state metadata must not be interpreted as newly proven success.

Include compatibility tests using baseline serialized examples. Resuming an old batch must still use the parser and context necessary for that batch.

#### Verification caches

Invalidate entries by eligibility, evidence-policy version, standards fingerprint, or actual schema incompatibility. Avoid destructive blanket invalidation when safe entries can be retained.

#### Request and experiment switches

Keep experimental prompt/model/caching/coordination behavior independently switchable. Disabling an experiment must restore the validated request behavior without reverting unrelated correctness fixes.

Do not expose confusing implementation toggles in ordinary product flows. Keep developer/evaluation controls where the repository already supports such controls.

#### Release order

1. Land baseline regressions and low-risk independent fixes.
2. Land shared result contracts and compatible readers.
3. Integrate producers and consumers of extraction, occurrences, coverage, recovery, and attempt usage.
4. Complete required cross-entry-point tests, reports, and documentation.
5. Release the correctness work.
6. Run and assess gated experiments independently; release only promoted changes.

A measured optimization should not delay urgent correctness fixes. Conversely, a passing unit test is not sufficient grounds to enable an unmeasured model or architecture change.

### 30. Agent handoff requirements and completion criteria

> **Revision note (2026-09-23):** Part 1 now defines the handoff. The pull request description carries items 1–5 below, and `plans/PROGRESS.md` records status and deviations. Each session ends, once its pull request is merged, by giving the owner the next-session prompt, or the final banner after the last chunk.

#### Deliverable from each implementation package

Each agent should provide:

1. The behavior changed and the defect it prevents.
2. Files/contracts changed, including consumer migrations.
3. Focused test results and meaningful edge cases covered.
4. Compatibility, rollback, and pending-state consequences.
5. Any remaining unsupported case or revised assumption.
6. For experiments: dataset/configuration hashes, isolated arm settings, measurements, spend, and an enable/defer/reject recommendation.

If current code differs from this baseline, explain the difference and adapt the implementation. Do not mechanically apply stale function names or force a redesign when a smaller verified fix satisfies the contract.

#### Correctness release is complete when

- Every WP-01 through WP-17 acceptance criterion has a linked implementation/test or a documented evidence-backed closure because the behavior was already fixed.
- All supported text survives extraction with usable provenance; unsupported editing is refused safely.
- Different issues and different occurrences remain distinct through application.
- Request budgeting uses the assembled selected-model request and a conservative failure path.
- Missing coverage and operational failure cannot appear as complete clean analysis.
- Inconclusive verdicts do not become long-lived cache successes.
- Retry waits respect server timing and do not consume request slots.
- Chat failures leave replayable history and usable controls.
- Trace failures cannot strand a run; GUI credentials do not enter global environment state.
- Pending paid repairs remain recoverable, with no blind duplicate submissions or repeated downstream spending.
- Attempt totals include both original and repair requests exactly once.
- Legacy sidecars, caches, and recovery records follow their documented compatibility rules.
- Required offline/JavaScript checks pass, and material skips or limitations are explicit.
- Documentation and report wording match shipped behavior.

#### Experimental work is complete when

Each EX item has a reproducible result and a recorded enable/defer/reject decision. If a live comparison has not run, say “not evaluated”; do not substitute a speculative savings estimate or mark the capability production-ready.

Default enablement requires its own stated quality, reliability, and cost criteria to pass. An unfavorable result is a valid outcome of an experiment.

### Appendix A. Compact regression examples

These examples express intended behavior; use the repository's actual input and result models.

| Case | Expected result |
|---|---|
| Article heading followed by valid nested subparagraphs | Not an empty article. |
| Article body exists only in its table | Not an empty article. |
| Quantity such as 1.5 or an ordinary numbered list item | Not automatically a CSI article heading. |
| “Prior to fabrication, submit shop drawings” | Does not suppress an unrelated stale edition. |
| “Historical Society” as an organization name | Does not make surrounding requirements historical. |
| “May not deviate” / “shall not deviate” | Does not suppress the mandatory requirement. |
| Bare “TBD” in active specification content | Detected once, subject to explicit placeholder policy. |
| A SECTION heading appears only under Related Sections | Cannot override the document's actual module identity. |
| Copper and PVC requirements in one known file | Remain distinct findings after filename normalization. |
| Same valid issue at p4 and p8 in one file | One presentation group may contain two executable occurrences. |
| Two selected files share a basename in different directories | Applier refuses an ambiguous basename binding before any write. |
| Nonempty controlling set and zero coverage rows | Incomplete assessment, not a clean completed compliance result. |
| Well-formed UNVERIFIED verdict | May share within the current run; is not a durable conclusive cache hit. |
| Malformed verdict after an ordinary end turn | Operational failure with retained known usage. |
| API error following assistant tool_use | Valid recovery/rollback; no unmatched tool call in the next request. |
| Trace initialization fails | Review lifecycle and GUI recover; optional tracing is disabled visibly. |
| Primary review needs repair and repair is still pending | Recovery manifest survives; dependent paid stages do not repeat. |
| Primary costs X and repair costs Y | Known review total includes X + Y exactly once. |

#### Clean three-PART structure

Build a DOCX with the following manual heading structure and ordinary body paragraphs. It should produce no empty-heading or duplicate-heading alerts.

~~~text
PART 1 GENERAL
1.01 SUMMARY
A. Provide the specified piping system.
1.02 SUBMITTALS
A. Submit product data before fabrication.
PART 2 PRODUCTS
2.01 MATERIALS
A. Provide materials meeting the scheduled requirements.
PART 3 EXECUTION
3.01 INSTALLATION
A. Install in accordance with the approved product instructions.
~~~

Create separate variants with a table as the only article body, automatic numbering, an actually empty article, and an actually duplicated article heading. Keep the clean control separate from each defect mutation.

#### Filename-normalization collision

With 210500.docx in the known corpus, use two findings whose other deduplication fields are deliberately identical:

- “Section 21 05 00 requires copper pipe in 210500.docx.”
- “Section 21 05 00 requires PVC pipe in 210500.docx.”

The old broad pattern can remove the meaningful material distinction between the section number and the filename. The corrected normalization may remove the exact known filename, but must preserve copper versus PVC and produce distinct issue identities.


### Appendix B. Provider documentation to recheck during implementation

These references supported the September 22, 2026 review. Provider behavior, limits, pricing, and SDK support can change; use the current documentation for the exact selected model and pinned SDK.

- [Token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting): request counts are estimates; validate the assembled request and fallback behavior.
- [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching): model minimums, cache prefixes, breakpoint limits, TTL rules, and observed read/write accounting.
- [Web fetch tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool): eligible URLs, tool behavior, and citation/result shapes.
- [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs): capability constraints, refusal/truncation exceptions, and format/tool distinctions.
- [Model pricing](https://platform.claude.com/docs/en/about-claude/pricing): model, batch, caching, and search price categories.
- [Thinking, steering, and cost](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost): effort and thinking-display semantics.

Repository guidance, current source, executable regression tests, and measured results remain the implementation authority. External documentation must not be used to justify removing a local compatibility guard without testing the actual request path.
