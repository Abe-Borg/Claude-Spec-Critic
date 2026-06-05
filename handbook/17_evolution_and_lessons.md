# Evolution & Lessons: The v3.0.0 Pivot and the Road Ahead

Every chapter so far has answered *what* and *how*: what Spec Critic is, how each
subsystem works, how a `.docx` becomes a grounded, trust-labelled report. This
final chapter answers the question those leave open — ***why did it become this?***
Software is sedimentary. The shape you've been reading about is the residue of
decisions made under pressure, of features that earned their keep and features
that didn't, and of at least one outage that taught a lesson the hard way. To
inherit this codebase well, you need its memory, not just its anatomy.

The memory has a surprising moral. The single most consequential piece of work in
the project's recent history was not something that was *added*. It was something
that was *removed*. In version 3.0.0, Spec Critic deleted its ability to apply
edits — the surgical write-back stack, the apply dialogs, the elaborate
confidence gating that decided whether an edit was safe to make automatically —
and replaced all of it with a quieter promise: *I will tell you what to change,
precisely and in a machine-readable form, but I will not change it myself.* That
subtraction made the program smaller, simpler, and — this is the argument of the
chapter — **more trustworthy**. It is a satisfying counter-narrative to the
instinct that more features mean a better tool, and it is the natural endpoint of
the throughline this book has followed since [**Ch 1 — The Problem Domain**](01_problem_domain.md): a
compliance tool earns its keep by making uncertainty *visible*, and there is no
louder way to hide uncertainty than to silently rewrite a legal document.

This chapter draws the arc that led there, tells the war story that bit the team
along the way, distills the design creed the whole codebase quietly obeys, and
lays out an honest road ahead — grounded in the two audits that [**Ch 16 — Trust
Under the Microscope**](16_trust_under_the_microscope.md) examines in full.

## 1. The arc: from construction to hardening to subtraction

Spec Critic's history falls into three movements. You don't need an exhaustive
changelog — the git history and the `README.md` carry that — but you do need the
*shape*, because the shape is the argument.

```
  CONSTRUCTION              TRUST HARDENING               SUBTRACTION
  (chunks A–P,              (v2.11.0)                     (v3.0.0 + M-series)
   v2.8.x → v2.10.0)
  ──────────────────►       ──────────────────►          ──────────────────►
  • non-GUI refactor        • Opus 4.7 review/x-check     • remove write-back stack
    into clean packages      • persistent claim cache       (src/editing/*)
  • core pipeline:          • Haiku triage (opt-in)       • remove apply dialogs
    extract → review        • severity-tiered budgets     • remove auto-edit gating
    → verify → report       • VERIFIED_CONTESTED          • EditActionLabel → 2 values
  • deterministic            • budget-exhausted sentinel   • delete edit-apply env vars
    pre-screen              ───────────────────           • M1: resume subsystem gone
                            the additive peak              • M2b: dep-suppression gone
                                                           • M3: locator fossils purged
                                                           • M7/M9: unify + clean
                                                           • tests 601 → 448
```

**Movement one — construction.** The earliest visible history is a long,
disciplined refactor (the internal "chunks A–P") that pulled the program out of a
monolith and into the eight-package `src/` layout you navigate today: `core`,
`input`, `review`, `batch`, `orchestration`, `cross_check`, `verification`,
`output`, plus `gui` and `tracing`. This is unglamorous work — moving code, not
changing behavior — but it is what made everything later *possible*. You cannot
reason about a pipeline you cannot see the seams of.

**Movement two — trust hardening (v2.11.0).** This is the additive peak, and it
is where the project's character was set. The default review and cross-check
models were upgraded to **Claude Opus 4.7**; a **persistent, claim-keyed
verification cache** landed on disk (60-day default TTL, atomic writes); optional
**Haiku 4.5 triage** arrived to cheaply pre-classify findings; **severity-tiered
search budgets** (CRITICAL=8, HIGH=7, MEDIUM=5, GRIPES=3) gave the verifier a
proportionate allowance. And — most tellingly — two "trust upgrade" features
appeared whose entire purpose was to *surface doubt more precisely*:
`VERIFIED_CONTESTED`, for when the initial and escalated verifiers both grounded
their verdicts yet disagreed; and the `budget_exhausted` sentinel, for when a
verifier spent its full allowance without grounding anything. (Their original
internal "chunk" labels were later stripped from the code in the M9 cleanup; the
`README.md` changelog still names them historically.) Read [**Ch 10 — Verification
II**](10_verification_grounding.md) and [**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md) for the mechanics. What
matters *here* is the direction of travel: every one of these investments made
the program better at saying *"I am not sure."*

**Movement three — subtraction (v3.0.0).** And that direction of travel is
exactly what made auto-application indefensible. The more the team taught the tool
to expose uncertainty — contested verdicts, exhausted budgets, the grounding
gate, the nine-label trust model — the more glaring it became that *applying an
edit automatically does the precise opposite*: it takes a finding the program has
worked so hard to annotate with doubt and silently bakes it into a stamped legal
document. The trust philosophy of v2.11.0 and the auto-apply feature were on a
collision course. v3.0.0 resolved it in favor of trust.

## 2. The v3.0.0 pivot: emit, don't apply

For most of its life, Spec Critic *changed documents*. A finding didn't just
describe a problem; it could carry a correction that the program would locate in
the `.docx` and rewrite in place. Supporting that was an entire subsystem —
`src/editing/`, with a **locator** that found the target text, a **spec_editor**
and **apply_edits** layer that performed the mutation, a **replacement_style**
module that tried to preserve the surrounding formatting, and an
**edit_candidates** layer that proposed what to change. The GUI had **apply
dialogs** for a human to approve edits. And gating all of it was an elaborate
**auto-edit confidence** apparatus: a composite confidence score, numeric- and
standards-based *demotions* that distrusted edits touching dimensions or code
citations, and an *auto-edit floor* below which an edit would not be applied
without review.

v3.0.0 deleted the whole apparatus. In its place: the program emits a structured
**edit proposal** per finding (action / existing text → replacement text /
anchor / target element id / confidence), renders it inline in the Word report as
a "Proposed replacement," and writes it to a machine-readable
`<report-stem>.edits.json` **sidecar** for a *separate, future applier* to ingest.
Nothing in the codebase now locates or mutates a spec. (See [**Ch 11**](11_trust_model_and_output.md) for how the
proposal and sidecar are rendered today.)

### Why give up a working feature

Because applying edits to a compliance document is a fundamentally different — and
far higher-stakes — act than *finding problems* in one, and the two had been
welded together.

- **The asymmetry of a wrong application.** A wrong *finding* costs a reviewer a
  few seconds of dismissal. A wrong *applied edit* silently mutates a stamped
  legal instrument and may sail through to a school site, exactly the "confident
  error is a hazard" failure mode from [**Ch 1**](01_problem_domain.md). The downside is not symmetric, so
  the bar for acting should not be the same as the bar for suggesting.
- **Locating is fragile.** The locator had to find the *right* run of text inside
  a messy `.docx` — across tables, list numbering, split runs, near-duplicate
  paragraphs — and edit it without corrupting formatting. That is a hard,
  brittle problem with its own long tail of failure modes, and getting it wrong
  reintroduced the very risk the rest of the pipeline works to eliminate.
- **Accountability belongs to a human or a dedicated, auditable tool.** A
  professional stamps these documents. The defensible factoring is: Spec Critic,
  the *reviewer*, produces precise, evidence-carrying instructions; a separate
  *applier* (and a human in the loop) owns the act of changing the document, with
  its own gating, its own audit trail, and its own tests. Emitting a clean,
  typed hand-off is a feature; owning the mutation was a liability.

### The cascade: how much complexity existed *only* to apply

The pivot's quiet lesson is how much of the codebase turned out to be scaffolding
for auto-apply — load-bearing for *nothing else*. Once application left, it could
all go, and the program got materially simpler for it.

| Removed / collapsed in v3.0.0 | Why it existed | What its removal simplified |
|---|---|---|
| `src/editing/` package (locator, spec_editor, apply_edits, replacement_style, edit_candidates) | To find and mutate edit targets in the `.docx` | Whole brittle locator/mutation surface gone; no formatting-preservation logic to maintain |
| GUI apply dialogs | To let a human approve in-app mutations | A simpler GUI whose terminal act is "export report + sidecar," not "change files" |
| Auto-edit confidence gating (composite confidence, numeric/standards demotion, auto-edit floor) | To decide whether an edit was *safe to apply automatically* | `classify_edit_action` collapsed to a one-line question (below) |
| `EditActionLabel` value set | Multiple labels to express apply-readiness; a `SUPPRESSED` label rode with cross-check dependency-suppression | Two values: `EDIT_SUGGESTED` / `REPORT_ONLY` |
| A raft of edit-application env vars (`SPEC_CRITIC_TABLE_CELL_AUTO_EDIT`, `_EDIT_TRANSACTIONAL`, `_NORMALIZE_REPLACEMENT_STYLE`, `_AUTO_EDIT_CONFIDENCE_FLOOR`, …) | To tune mutation behavior | A smaller, comprehensible configuration surface ([**Ch 12 — Configuration, Models & Token Economics**](12_configuration_and_models.md)) |

The headline simplification is `classify_edit_action`. It used to be the
gatekeeper that weighed confidence, verification status, and numeric/standards
risk to decide whether an edit could be auto-applied. Today, verified in
`src/output/report_status.py`, it is two lines of policy: *no edit proposal →
`REPORT_ONLY`; otherwise → `EDIT_SUGGESTED`.* The verification status
(`VERIFIED_SUPPORTED`, `VERIFIED_CONTESTED`, `VERIFICATION_FAILED`, …) and the
`edit_confidence` still travel alongside in the report and the sidecar — but as
*information for a downstream applier to gate on*, not as a decision this program
makes. The question "is it safe to apply this?" didn't get answered better; it
got *handed to the layer that should own it.* That is the whole pivot in
miniature.

## 3. Finishing the demolition: the M-series

A pivot this large leaves rubble — features that only made sense in the old world,
now orphaned. The **M-series** refactors (visible in the git history as commits
labelled M1, M2b, M3, M5, M7, M9) were the disciplined cleanup that followed,
each removing a piece of newly-dead weight:

- **M1 — the resume / durable-state subsystem.** A mechanism to persist and resume
  partial runs. With the pipeline simplified and the long write-back phase gone,
  the durable-state machinery was more complexity than it earned, and it was
  removed.
- **M2b — cross-check dependency-suppression.** An orphaned feature in the
  coordination pass (and the source of that retired `SUPPRESSED` edit label),
  deleted outright once nothing consumed it.
- **M3 — auto-apply / locator fossils.** The last fragments of the old editing
  world — stray references and helpers the main v3.0.0 deletion had left behind —
  purged so no caller could accidentally reach for a capability that no longer
  existed.
- **M7 / M9 — unification and cosmetic cleanup.** `VerificationResult` cache
  serialization was unified behind a single field policy (M7); chunk labels were
  stripped, test files and a router module renamed, and a dead `docx_fixtures`
  module with zero callers deleted (M9). Even the development-time nomenclature
  ("Trust Upgrade Chunk 12") was scrubbed — subtraction reaching all the way down
  to names.

The cumulative effect is captured in one number the project is rightly proud of:
the v3.0.0 trim took the test suite from **601 to 448 tests** (shedding ~2.2k
lines), not because coverage got worse but because there was *less surface to
cover*. A smaller, honest codebase needs fewer tests to pin it down. (The suite
kept shrinking through the trim's tail — to ~396 around the time this handbook was
assembled — and has since grown back to ~645 as new features and hardening brought
their own regression tests; [**Ch 15 — Quality Engineering**](15_quality_engineering.md) counts the current `def
test_` functions and explains what the survivors guarantee.) The M-series is the
quiet, professional half of a pivot: not just deciding to remove a feature, but
following the removal all the way to the corners.

## 4. The lesson paid for in an incident: the beta-header crash

Not every lesson came from a deliberate decision. The most memorable one arrived
as an outage, and it is worth telling in full because it teaches a general
principle about programming against an external contract.

Verification's richer modes (`standard_reasoning` and `deep_reasoning`) use a
second server tool alongside web search: **web_fetch**, which retrieves the full
text of a page rather than a search snippet (see [**Ch 10**](10_verification_grounding.md)). When web_fetch was
wired in, the code attached an `anthropic-beta: web-fetch-2026-02-09` header to
every request that carried the tool. The reasoning seemed airtight at the time,
and it is the kind of reasoning a careful engineer makes every day:

> *A beta header is harmless when the API treats the feature as generally
> available, and required when the feature is still gated. So attaching it is the
> safe choice either way.*

**Both halves of that sentence were wrong**, and the error was a hard one.

1. web_fetch is **generally available**. The tool dict (`web_fetch_20260209`)
   *alone* enables it; there is no gate, so the header buys nothing.
2. An *unrecognized* `anthropic-beta` value is not silently ignored — the API
   **rejects** it with `HTTP 400 invalid_request_error: Unexpected value(s) … for
   the anthropic-beta header`. A retired beta value is a hard error, not a no-op.

The combination was the worst case. Every verification routed to
`standard_reasoning` or `deep_reasoning` — **the common path** — carried the
retired header and **crashed at submit**, on both the real-time and batch routes.
The pipeline didn't degrade or skip verification; it failed loudly the moment it
tried to verify almost anything.

The fix was a deletion (fittingly): attach **no** beta header for web_fetch. The
`web_fetch_20260209` tool dict is current and valid, so it is included
unconditionally for the two fetch-eligible modes; the `extra_headers` seam stays
empty. You can read the fix's own epitaph in the source — `api_config.py` now
carries a comment block that states flatly that web_fetch "takes NO
`anthropic-beta` header" and explains *why*, so the next engineer cannot make the
same inference. That comment is the lesson, fossilized on purpose.

> **The lesson:** *A beta header is a hard contract with the API, not a hint.* An
> `anthropic-beta` value is matched exactly; an unknown one is a 400, not a shrug.
> Never attach a speculative header on the theory that it is "harmless if
> unneeded." If a feature is GA, the header is at best noise and at worst a
> landmine; if it is gated, you attach the *exact, current* value and nothing
> else.

### The same risk class, still live

The beta-header story is not closed, because the codebase still contains **one
more hardcoded beta header of the same risk class** — and the team knows it. The
300k extended-output path (batch-only, for inputs ≥200k tokens) attaches
`BATCH_OUTPUT_BETA = "output-300k-2026-03-24"`, verified in `api_config.py`. Its
guard, `assert_extended_output_allowed`, checks only that the header is
**present**, never that the API still **accepts** it. If that beta value is ever
retired or renamed, every large-input batch review will crash at submit — the
*exact* failure mode web_fetch already taught, just on a less-frequent path. The
Trust Audit flags this as **P0-4** precisely because the codebase has *already
been bitten once* by a hardcoded beta value, and the second one is structurally
identical. The honest disposition: this is a known, accepted, still-open risk,
documented rather than buried. It lives on the road ahead (§6), and [**Ch 6 —
Batch Processing**](06_batch_processing.md), [**Ch 12**](12_configuration_and_models.md), and [**Ch 16**](16_trust_under_the_microscope.md) all return to it.

## 5. The design philosophy, distilled

Stand back from the individual decisions and a coherent creed emerges — a set of
principles that no single chapter declares but that every chapter obeys. If you
internalize one thing from this book before you change a line of code, make it
this table. Each principle is a constraint on *how* you are allowed to make the
program better.

| Principle | What it means in practice | Where it shows up |
|---|---|---|
| **Determinism before the model** | Run cheap, certain, no-API checks first; never ask a model what a regex can prove | Deterministic pre-screen ([**Ch 4 — Input**](04_input.md)) |
| **Evidence-grounded verdicts** | A positive verdict requires a cited URL the search tool *actually retrieved*; the model's word alone is never enough | The grounding gate ([**Ch 10**](10_verification_grounding.md)) |
| **Emit, don't apply** | Propose precise edits; never mutate the document — applying is a separate, accountable act | The v3.0.0 pivot; the edit sidecar ([**Ch 11**](11_trust_model_and_output.md)) |
| **Degrade to safe defaults** | A misconfiguration produces a *smaller, safe* request, never an API rejection or a silent corruption | Unknown model ids → capability defaults ([**Ch 12**](12_configuration_and_models.md)) |
| **Make uncertainty visible** | When the program isn't sure, it must *say so* in the artifact — contested, insufficient, failed, exhausted | The nine-label trust model ([**Ch 11**](11_trust_model_and_output.md)) |
| **Observe without mutating** | Instrumentation reads existing state; it never alters a `Finding` or changes a run's outcome | The forensic trace silo ([**Ch 14 — Observability**](14_observability.md)) |
| **Pin invariants with tests** | The contracts that matter (grounding, dedup identity, status order) are nailed down by a hermetic suite | The test harness ([**Ch 15**](15_quality_engineering.md)) |
| **Keep the docs honest about the edges** | Known gaps and caveats are written down and *kept in the repo*, not quietly omitted | The audits ([**Ch 16**](16_trust_under_the_microscope.md)); this chapter |

Two of these deserve a closing emphasis because they are the most distinctive.
*Make uncertainty visible* is the throughline of the whole book — it is why the
trust model has nine labels and not three, why a contested finding gets its own
purple status, why budget exhaustion earns a sub-label. And *keep the docs honest
about the edges* is the principle this very chapter, and [**Ch 16**](16_trust_under_the_microscope.md), embody: a
project that writes down "here is where we might be wrong" and commits it
alongside the code is making a trust claim no marketing copy can.

## 6. The road ahead

A handbook that ended on "and it's all finished" would violate the last principle
in that table. Spec Critic's core — extraction, deterministic screening, the
grounded verification engine — is genuinely well-built, and the audits confirm it
(see the "verified-clean" findings in [**Ch 16**](16_trust_under_the_microscope.md)). The surprising inversion the
audits surface is that **the risk does not live where people expect it.** The LLM
is not the weak link; the grounding gate holds. The higher-risk surface is the
*edges*: the honesty of the final artifact and the completeness of what reaches
the sidecar — the places where a perfectly correct internal verdict can still
become an incomplete or misrepresented external instruction.

The road ahead is therefore mostly about **closing honesty and completeness gaps
at the edges**, plus the one large constructive project the whole emit-only stance
is built *for*: a downstream applier. The agenda below is prioritized roughly as
the audits sequence it — surfacing partial failure first.

| Item | Motivation | Source / cross-ref |
|---|---|---|
| **Surface partial-failure in the artifact** *(headline)* | A run where some specs *failed* review reads as a clean run: "Files Reviewed: 5" even when 2 silently failed; the data (`truncated_specs`) exists but isn't shown. For a compliance tool this is the most dangerous gap. | Structural P0-1 → [**Ch 11**](11_trust_model_and_output.md) (report), [**Ch 7 — Orchestration & State**](07_orchestration.md) |
| **Per-file sidecar fan-out** | A defect found across N specs emits *one* edit instruction; a downstream applier fixes one file and never learns of the others. The fan-out helper (`group_findings()`) exists but is wired only into tests. | Trust P0-1/P0-2 → [**Ch 7**](07_orchestration.md), [**Ch 11**](11_trust_model_and_output.md) |
| **Build the downstream applier** | The sidecar is a hand-off with no recipient yet. The whole emit-only bet pays off only when a dedicated, auditable applier (plus a human) consumes it. | The v3.0.0 stance → [**Ch 11**](11_trust_model_and_output.md) |
| **Model-whitelist maintenance / loud-warn on unknown ids** | An operator setting a *newer, better* model (e.g. `claude-opus-4-8`) silently degrades to capped output and no extended thinking. Safe-default protects against rejection but trades it for quiet quality loss. | Trust P0-3 → [**Ch 12**](12_configuration_and_models.md) |
| **Beta-header acceptance, not just presence** | The hardcoded 300k header is checked for presence, not acceptance — the same risk class as the web_fetch crash. Confirm validity; add graceful fallback to 128k. | Trust P0-4 → [**Ch 6**](06_batch_processing.md), [**Ch 12**](12_configuration_and_models.md) |
| **Extraction completeness** | Text in headers/footers, text boxes, or footnotes may not be extracted, so a real defect there is never reviewed — a "miss a real problem" gap. | Trust P0-6 → [**Ch 4**](04_input.md) |
| **Cross-check finding ids + dedup** | Coordination findings reach the sidecar with an empty `finding_id` and aren't deduped across CSI-division chunks. | Structural P1-1 → [**Ch 7**](07_orchestration.md), [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md) |
| **Prove batch grounding parity & fallback handoff** | The batch (default) verification path is believed to mirror the real-time grounding gate, and the batch→real-time tail is believed to write exactly one result — both *must be proven*, not assumed, at the trust bar. | Trust P0-5, Structural P1-2 → [**Ch 10**](10_verification_grounding.md) |
| **Keep the pinned-edition matrix current** | The adopted NFPA/ASHRAE/IAPMO/UL editions are hand-maintained against the California adoption matrix; they must be re-confirmed as cycles advance. | Trust P1-4 → [**Ch 12**](12_configuration_and_models.md) |

And one item that is **not** a bug but is the most important caveat in the entire
book, repeated here because the road ahead cannot improve it away with code: a
`VERIFIED_SUPPORTED` / `CONFIRMED` verdict guarantees only that the cited URL was
*actually retrieved by the search tool* — **not** that the page's content
demonstrably supports the specific code claim. **Grounding proves the source is
real, not that the source proves the claim.** Human spot-checking of `VERIFIED_*`
findings remains warranted, and always will. [**Ch 16**](16_trust_under_the_microscope.md) states this caveat at
length; the road ahead's job is to keep it honest, not to pretend it away.

## 7. An honest ending

The book opened, in [**Ch 1**](01_problem_domain.md), with a stale code reference hiding in a
forty-page plumbing spec, one approval stamp away from a school site — and with
the distinction that organizes everything: *a missed defect is a cost; a confident
error is a hazard.* Seventeen chapters later, that sentence is still the key to
the whole design. Every subsystem you've read about is a different answer to the
same question — *how do we catch the defect without ever manufacturing false
confidence?* — and the answers compound: check the certain things locally, ground
the uncertain ones in real sources, label honestly what you cannot resolve,
record everything so a human can reconstruct it, and — the v3.0.0 verdict —
**propose the fix but let an accountable party apply it.**

The arc of this chapter is that the program got more trustworthy by getting
*smaller*. It shed the feature that most directly contradicted its own philosophy,
and the cascade of simplifications that followed — fewer labels, fewer env vars,
fewer subsystems, fewer tests for less surface — left a codebase that is easier to
understand, harder to misuse, and more candid about what it does and does not
know. The remaining work is the work of a mature project: not chasing features,
but closing the last honesty gaps at the edges and building the dedicated applier
the emit-only stance was designed to feed.

That is a good place for a compliance tool to be. Trustworthy in its core, candid
about its edges, and disciplined about the difference. The program does not
pretend to be finished, and it does not pretend to be certain — and in this
domain, refusing to pretend *is* the feature. The handbook ends where it began: on
trust, earned the only way it can be — by making uncertainty visible, and by
knowing exactly what you do not yet know.

## How it connects

This chapter reflects on the whole system rather than owning a subsystem. It
closes the loop on the problem framing of [**Ch 1 — The Problem Domain**](01_problem_domain.md); it tells
the *why* behind the emit-only artifact realized in [**Ch 11 — The Trust Model &
Report Output**](11_trust_model_and_output.md); it traces the beta-header lesson whose mechanics live in [**Ch 10
— Verification II**](10_verification_grounding.md) and whose still-live cousin (the 300k header) belongs to
[**Ch 6 — Batch Processing**](06_batch_processing.md) and [**Ch 12 — Configuration, Models & Token
Economics**](12_configuration_and_models.md); and it draws its road ahead directly from the audits anatomized in
[**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md), deferring every open-item detail there.

## Key takeaways

- **Subtraction was the breakthrough.** v3.0.0's removal of the auto-apply /
  write-back stack made the program simpler *and* more trustworthy — a deliberate
  counter to "more features = better."
- **A lot of complexity existed only to apply.** Once application left, the
  `src/editing/` package, the apply dialogs, the confidence gating, a raft of env
  vars, and whole orphaned subsystems (resume, dependency-suppression) could all
  go; the test suite fell 601 → 448. `classify_edit_action` became a one-line
  question.
- **Emit, don't apply, is the defining stance.** The tool proposes precise,
  evidence-carrying edit instructions (report + `edits.json` sidecar) and leaves
  the accountable act of changing a stamped document to a human or a dedicated,
  future applier.
- **The beta-header crash is the marquee lesson.** A speculative
  `anthropic-beta: web-fetch-2026-02-09` header crashed the common verification
  path with HTTP 400; web_fetch is GA and takes *no* beta header. *A beta value is
  a hard contract, not a hint.* The hardcoded 300k header is the same risk class
  and still open (presence-checked, not acceptance-checked).
- **The risk lives at the edges, not in the model.** The grounding core is sound;
  the road ahead is about surfacing partial failure, fanning out multi-file edits,
  building the applier, and keeping the model whitelist and pinned editions
  current.
- **The deepest caveat is permanent.** Grounding proves a source is *real*, not
  that it *proves the claim* — human spot-checking of `VERIFIED_*` findings is
  always warranted.
- **California 2025 is the only cycle.** The 2022 cycle was removed and is not to
  be reintroduced; the cycle label is part of the cache key, so a cycle bump
  invalidates stale verdicts by design.
