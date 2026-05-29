# Preface & How to Read This Handbook

Somewhere in a forty-page plumbing specification, one paragraph still reads
*"Comply with the 2019 California Plumbing Code."* The project is bidding under
the 2025 cycle. Nobody put that line there on purpose — it rode in from a
template, survived three rounds of editing, and is now one approval stamp away
from a K-12 construction site. A reviewer skimming at 11 p.m. reads straight past
it, because it *looks* like every other compliance sentence in the document.

That paragraph is why this book exists. Spec Critic catches exactly that kind of
defect — the stale reference, the placeholder never filled in, the value that
contradicts the value three specs over, the code citation that sounds
authoritative and is quietly wrong. It reviews California K-12 **DSA mechanical
and plumbing** specifications and surfaces the problems a tired human eye slides
past.

But a compliance tool carries a second, sharper risk — the one this whole
codebase is organized around. **A tool that is confidently wrong about a building
code is worse than no tool at all.** If Spec Critic waves through a non-compliant
detail — or, worse, *asserts* that a wrong section number is correct and a
reviewer believes it — it has not saved work; it has manufactured false
confidence and shipped it to a school. A missed defect is a cost. A confident
error is a hazard.

## The throughline: trust

Almost every design decision in this codebase exists to manage that second risk.
The book returns to this thread so often that it is worth stating up front as the
single idea to read everything else against:

> **Spec Critic is built to make its own uncertainty *visible* rather than
> hidden.**

You will see this principle take many concrete forms, and each later chapter owns
one of them:

- **It checks the cheap, certain things locally first** — deterministic detectors
  that need no model and never guess (see [**Ch 4 — Input**](04_input.md)).
- **It refuses to call a claim "confirmed" on the model's word alone.** A positive
  verdict must be *grounded* in a source the search tool actually retrieved
  ([**Ch 10 — Verification II**](10_verification_grounding.md)).
- **It emits edit instructions but never applies them.** The tool proposes; a
  human or a separate, future program disposes ([**Ch 11 — The Trust Model &
  Report Output**](11_trust_model_and_output.md)).
- **It labels every finding with one of nine trust statuses** — including honest
  labels for *"the verifier ran but couldn't ground this,"* *"two models
  disagreed,"* and *"verification failed operationally"* ([**Ch 11**](11_trust_model_and_output.md)).
- **It records a forensic trace of every run**, so that when a verdict looks
  wrong you can reconstruct exactly what the model saw and how the pipeline
  interpreted it ([**Ch 14 — Observability**](14_observability.md)).

None of these is a feature bolted on for polish; they are the load-bearing
structure. Read the handbook with the question *"how does this part make
uncertainty visible?"* in mind and the architecture will make sense.

## Why this book, and why now

This handbook is the **"story so far"** of the Spec Critic codebase, captured at
**version 3.0.0**. It is deliberately a *blended engineering handbook and
narrative*: it teaches the system precisely enough that you can navigate, modify,
and trust the code, and it tells the story of *why* the code is shaped the way it
is — what was genuinely hard, what bit the team, and where the program is still
being perfected.

v3.0.0 is a natural moment to write it down because the project just made its
sharpest turn: it removed the entire surgical "write-back" stack — the machinery
that located edit targets in a `.docx` and mutated them in place — and replaced
it with an *emit-only* stance. Spec Critic now produces structured edit
*instructions* and hands them off, but never touches the source document. That
pivot rippled through the trust model, the report, and the configuration surface,
and much of this book explains the system as it stands after the decision
settled. [**Ch 17 — Evolution & Lessons**](17_evolution_and_lessons.md) tells that story directly. This is also,
plainly, a *living* system with documented edges — known gaps, honest caveats,
and audit findings the team chose to surface rather than bury, which [**Ch 16 —
Trust Under the Microscope**](16_trust_under_the_microscope.md) is built entirely from.

## Who this is for

The primary reader is a **competent software engineer who is new to this
codebase** *and* new to the **California DSA mechanical/plumbing spec-review
domain**. You bring general engineering skill; the book supplies the rest. No
background in building codes, the Construction Specifications Institute's
numbering scheme, or the alphabet soup of California authorities is assumed — the
next chapter and the glossary below bring you up to speed.

The promise is specific: by the end you should be able to *reason about* the
system and its trade-offs — to predict how it behaves, to know where to look when
it surprises you, and to change it without breaking the trust guarantees — not
merely recite its parts.

## How the book is organized

The handbook is **front matter plus seventeen chapters, grouped into six Parts**.
Chapter numbers and titles below are canonical; cross-references throughout the
book use these exact titles.

| Part | Ch | Title |
|---|---|---|
| *Front Matter* | 0 | Preface & How to Read This Handbook *(you are here)* |
| **I — The Problem & The Shape** | 1 | The Problem Domain: California DSA Mechanical & Plumbing Spec Review |
| | 2 | Architecture at a Glance: Subsystems, Dependencies & the Core Data Model |
| | 3 | A Run, End to End: Following the Data from `.docx` to Report |
| **II — Ingestion & Review** | 4 | Input: Extraction, Element IDs & the Deterministic Pre-Screen |
| | 5 | The Review Engine: Prompts, Schemas & the Anthropic Client |
| | 6 | Batch Processing: The Message Batches Backbone |
| **III — Coordination & Verification** | 7 | Orchestration & State: The Pipeline Spine |
| | 8 | Cross-Spec Coordination |
| | 9 | Verification I: How We Decide to Check (Routing, Modes, Profiles, Triage) |
| | 10 | Verification II: How We Check & Judge (Grounding, Verdicts, Escalation, Cache) |
| **IV — Output & Trust** | 11 | The Trust Model & Report Output: Status Labels, the Word Report & the Edit Sidecar |
| **V — Cross-Cutting Systems** | 12 | Configuration, Models & Token Economics |
| | 13 | The Desktop GUI & Its Controller Architecture |
| | 14 | Observability: Tracing & Diagnostics |
| | 15 | Quality Engineering: Testing & Calibration |
| **VI — The Meta-Story** | 16 | Trust Under the Microscope: The Audits |
| | 17 | Evolution & Lessons: The v3.0.0 Pivot and the Road Ahead |

Parts I–IV follow the data — problem and shape, ingestion and review,
coordination and verification, output. Parts V and VI step back to the
cross-cutting systems that touch every stage, then the meta-story of how the team
interrogates and evolves the tool.

## Suggested reading paths

You do not have to read straight through. Three paths cover the common reasons
you might be here:

| If you are… | Read in this order |
|---|---|
| **A new engineer onboarding** | **Ch 1 → Ch 2 → Ch 3**, then dive into whichever subsystem you'll touch (Parts II–V). Ch 3 gives you the whole run; the subsystem chapters go deep. |
| **A domain reviewer or non-coder** | **Ch 1 → Ch 11 → Ch 16.** What the tool reviews, how to read its report and trust labels, and where its honest limits are — without the implementation. |
| **Debugging a strange verdict** | **Ch 3 → Ch 9 → Ch 10 → Ch 14.** The flow, then how a finding was routed for checking, then how it was judged and grounded, then how to replay the run from its trace. |

## Conventions used throughout

A few conventions hold across every chapter; knowing them now will save friction.

- **Why over how.** Chapters explain *mechanisms and rationale*, not line-by-line
  code. You should grasp a subsystem without the source open, and know where to
  look when you open it. Code appears only in short, illustrative fragments — a
  signature, a data shape, a representative schema snippet — never a verbatim file
  dump.
- **The source code is the final authority.** Where this handbook, the project's
  `CLAUDE.md`, and a chapter disagree, **the code wins.** Model ids, version
  strings, and token caps drift, so a chapter citing a value names the file (and
  often the symbol) for you to confirm — and is expected to flag any contradiction
  it finds. That drift is what the audits in [**Ch 16**](16_trust_under_the_microscope.md) care about.
- **Cross-references** read as *"see [**Ch 10 — Verification II**](10_verification_grounding.md),"* using the
  canonical title so they stay unambiguous out of context.
- **Diagrams** favor plain-text legibility — simple ASCII boxes and arrows, or
  Mermaid where it helps.
- **"Emit, don't apply" is everywhere.** Since v3.0.0 removed write-back, you'll
  repeatedly meet the stance that Spec Critic *proposes* an edit and stops. An
  "edit proposal," an "edit sidecar," or an `EDIT_SUGGESTED` label is a suggestion
  for a human or a downstream tool — never a change the program made. The tool
  never mutates a spec.

With that framing set, [**Ch 1 — The Problem Domain**](01_problem_domain.md) begins with the world of
California DSA spec review and why reviewing these documents by hand is so
unforgiving. The glossary below is here to flip back to whenever a term goes by
faster than you'd like.

---

## Glossary

Terms a reader new to the domain *and* the codebase will meet repeatedly,
alphabetized for flip-back reference. Domain vocabulary, the core data objects,
and the process terms are interleaved. Each data object is *introduced* in [**Ch 2
— Architecture at a Glance**](02_architecture.md) and detailed in the chapter that owns its code.

- **AHJ — Authority Having Jurisdiction.** The agency empowered to review,
  approve, and enforce code compliance for a project; for California K-12 work
  that agency is the DSA, for healthcare it is HCAI.

- **Batch / wave / `custom_id`.** Concepts from Anthropic's Message Batches API,
  through which Spec Critic submits *every* model pass (≈50% cheaper than
  synchronous calls). A *batch* is a queued set of requests; a *wave* is one
  submit→poll→collect cycle within verification; a `custom_id` matches a returned
  response back to the finding that produced it.

- **Budget exhausted.** A sentinel raised when the verifier spent its *entire*
  web-search budget without grounding a verdict. It is runtime telemetry, not a
  separate trust status — such a finding is still reported as insufficient
  evidence.

- **Calibration eval.** The fixture-driven scoring harness in `evals/calibration/`;
  it replays recorded model responses against expected outcomes so routing,
  grounding, and status changes can be regression-tested without spending API
  calls.

- **Code cycle.** The dated set of adopted California codes — here, **California
  2025** — plus the pinned editions of every referenced standard. The cycle label
  is part of the verification cache key, so moving cycles invalidates stale
  verdicts. (The previous 2022 cycle was removed and is not to be reintroduced.)

- **Contested.** The state in which the initial (Sonnet) and escalated (Opus)
  verifiers *both* grounded their verdicts in real sources yet reached *different*
  conclusions. The disagreement itself is the signal: the finding is labelled
  `VERIFIED_CONTESTED` and steered toward human review.

- **Cross-check / coordination.** The cross-spec pass that hunts for defects
  spanning more than one document — a value set in one spec and contradicted in
  another. Distinct from the per-spec *review*.

- **CSI / CSI division.** The Construction Specifications Institute's MasterFormat
  numbering scheme. Specs are organized by division: **21** fire suppression,
  **22** plumbing, **23** HVAC, **25** integrated automation, **01** general
  requirements.

- **Diagnostics report.** The in-memory operational-health summary
  (`DiagnosticsReport`) produced for each run — failures, cache replays,
  demotions, extraction warnings — which feeds the report's Run Diagnostics
  banner.

- **DSA — Division of the State Architect.** The California authority that reviews
  and approves construction documents for K-12 and community-college projects.
  DSA approval is the gate Spec Critic's users are trying to clear.

- **EditProposal.** A structured edit attached to a finding: an action
  (edit / delete / add / report-only), the existing text, the replacement text, an
  anchor, a target element id, and a confidence. Spec Critic *emits* these; it
  never applies them.

- **Edit sidecar.** The `<report-stem>.edits.json` file written next to the Word
  report — the machine-readable feed of edit proposals for a separate, downstream
  program to apply.

- **Element id.** A stable handle for one extracted element — `p7` (paragraph 7),
  `t0r2` (table 0, row 2), `s1h0` (section header) — so an edit proposal can name
  its target precisely.

- **Escalation.** Re-running an uncertain finding on a stronger model (Sonnet →
  Opus) to see whether more capability resolves it. (When it instead yields a
  grounded *disagreement*, the result is *contested*.)

- **ExtractedSpec.** The product of reading one `.docx`: its extracted text, the
  *element-id* map that lets later stages point at exact paragraphs and table
  cells, and any extraction warnings.

- **Finding.** One issue raised by the reviewer or a deterministic detector. It
  carries a severity, the relevant text, an optional `EditProposal`, and — after
  verification — a `VerificationResult`. Findings are the spine of everything the
  tool produces.

- **FindingGroup / FindingOccurrence.** The structures that group the *same*
  defect appearing across multiple specs while preserving each file's individual
  occurrence, so per-file existing/replacement text survives the merge.

- **Grounding.** Proving that a verdict's cited URL was *actually retrieved* by a
  search tool, not invented by the model. The caveat lives in this word:
  **grounding proves the source is real, not that the source proves the claim.**

- **HCAI — Department of Health Care Access and Information** (formerly OSHPD).
  The AHJ for California healthcare facilities — the healthcare analogue to the
  DSA, which surfaces because its code references overlap with K-12 work.

- **M&P — Mechanical & Plumbing.** The two specification disciplines this tool
  reviews; HVAC, fire suppression, and integrated automation all fall under this
  umbrella.

- **Mode / profile.** The two routing dimensions of verification. The *mode*
  (`local_skip`, `strict_structured`, `standard_reasoning`, `deep_reasoning`)
  decides *how hard* to check; the *profile* (`california_ahj`, `code_standard`,
  `manufacturer`, `constructability`, `internal_coordination`) decides *which
  sources* to prefer.

- **Pinned editions.** The specific NFPA, ASHRAE, IAPMO, and UL editions
  California adopted for the cycle; the reviewer and verifier are told to flag any
  drift away from them.

- **PipelineResult / CollectedBatchState.** The aggregate run-state objects that
  carry findings, extracted specs, and diagnostics from one stage to the next, and
  ultimately into the report.

- **Pre-screen / deterministic detector.** The local, no-API checks that run
  *before* any model call — placeholders, stale code cycles, duplicate paragraphs,
  and the like. Each carries a stable `deterministic_rule` id so its findings are
  traceable and testable.

- **Prompt cache / cache breakpoint.** Anthropic's prompt-caching mechanism, which
  Spec Critic leans on for cost. *Breakpoints* mark where the cacheable prefix
  ends and must land in **byte-stable** positions across calls, which constrains
  how prompts are assembled.

- **Review.** The per-spec Claude pass that reads one specification and produces
  findings — the first model call in the pipeline and the source of most findings.

- **ReviewResult.** The output of a single review (or cross-check) call: the
  findings it produced, plus metadata and any errors.

- **Spec.** A single `.docx` specification *section* in CSI format — the unit of
  input. A project is a set of specs.

- **Trace / span / event.** The forensic, default-on observability layer. A
  *trace* is one run's directory of JSONL files; a *span* is a timed, nestable
  unit of work (`pipeline` → `review` → `api_call`); an *event* is a
  point-in-time record within a span (a search query, a grounding outcome, an
  escalation decision).

- **Triage.** An optional Haiku pre-classification step that decides whether a
  finding needs web search at all, cheaply filtering out findings a deterministic
  rule already explains.

- **Verdict.** The verifier's raw judgement on a claim: `CONFIRMED`, `CORRECTED`,
  `DISPUTED`, or `UNVERIFIED`. The reader-facing *trust status* is derived from
  the verdict together with its grounding.

- **Verification.** The web-search-backed pass that adjudicates a finding into a
  *grounded* verdict — where most of the trust machinery lives.

- **VerificationResult.** The verdict, the grounding evidence (the *accepted*
  source URLs, never the model's invented ones), and the run telemetry for one
  finding.

- **VerificationRoutingDecision.** The policy bundle — mode, model, search budget,
  and tools — computed for verifying a single finding before the call is made.
