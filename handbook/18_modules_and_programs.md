# Modules & Programs: How the Domain Left the Engine

Every chapter before this one describes a program that reviews **California K-12
DSA mechanical and plumbing specifications**. That is not a configuration of
Spec Critic; in the v3.0.0 codebase those chapters describe, it *was* Spec
Critic. The reviewer persona named California. The prompt's severity
definitions cited DSA. The deterministic detectors knew that `CBC` was a code
abbreviation and that `2019` was a stale cycle. The verification profile that
recognized a jurisdictional claim was literally called `CALIFORNIA_AHJ`. The
domain was not *in* the engine — the domain *was* the engine, spread across
prompt builders, detectors, routers, and report headings as hardcoded strings.

This chapter is about undoing that, and about what the undoing cost. Between
v3.0.0 and v3.4.0 the domain content was extracted into **modules** — one frozen
data object per reviewable domain — and grouped for the operator into
**programs**. The engine kept the protocol; the module supplies the content.
The result is that Spec Critic can now review hyperscale data-center fire
suppression in Ontario with the review and coordination prompts carrying no
California content at all — and, the harder half, *without changing a byte* of
what the California run produces. One string did not make it out, and §2.1
documents it rather than rounding the claim up.

That last constraint is the whole story. A refactor that generalizes a system
by rewriting its only working configuration is not a generalization; it is a
rewrite with extra steps. So the extraction was done under a rule that shows up
throughout this chapter: **every surface a California run touches must stay
byte-identical.** The goldens in `tests/test_golden_domain_surfaces.py` are the
enforcement.

## 1. The two layers, and why there are two

It would have been simpler to have one concept. The codebase has two, because
they answer different questions.

A **`ReviewModule`** (`src/modules/base.py`) is one *reviewable domain*: a code
basis, a reviewer persona, a detector vocabulary, a set of routing keywords.
`california_k12_mep` is a module. `datacenter_fire` is a module. A module is an
engineering object — it is what the prompt builders, the detectors, and the
verification router consult.

A **`ProgramDefinition`** (`src/programs/models.py`) is one *operator choice*:
the thing that appears in the GUI's program selector. It groups one or more
modules behind a single name. `california_k12` is a program containing exactly
one module; `hyperscale_datacenter` is a program containing four.

The split exists because the unit a reviewer picks and the unit the engine
reasons with are not the same unit. Nobody selecting "Hyperscale Data Centers —
USA and Canada" wants to first decide whether their Division 21 sprinkler spec
is a fire-suppression document or an electrical one. That is the program
layer's job: the operator picks a program, and **routing** (§4) assigns each
individual spec to the module or modules that should review it.

| | `ReviewModule` | `ProgramDefinition` |
|---|---|---|
| Answers | "What domain knowledge reviews this spec?" | "What is the operator working on?" |
| Granularity | One code basis, one discipline | One GUI choice, one or more modules |
| Consulted by | Prompt builders, detectors, verification router, report exporter | The GUI selector, the routing pass, the composite pipeline |
| Registry | `AVAILABLE_MODULES` (`modules/registry.py`) | `AVAILABLE_PROGRAMS` (`programs/catalog.py`) |

Membership is deliberately independent: the program catalog does not derive from
the module registry. A module can exist without being offered in any program
(useful while a new domain is being built), and a program names its modules by
id.

## 2. What a module actually carries

`ReviewModule` is a frozen dataclass, and its field list is the most honest
inventory of "what was California-specific" that exists anywhere in the project.
Each field is a slot the engine used to hardcode.

**Identity and code basis.** `module_id`, `display_name`, `description`, and a
single `cycle` (a `CodeCycle` — see [**Ch 12 — Configuration, Models & Token
Economics**](12_configuration_and_models.md)). One module pins exactly one code
basis. This is a hard rule, not a convenience: the cycle label is part of the
verification cache key (§6), so a module cannot straddle two code bases without
making cached verdicts ambiguous.

**Prompt content slots.** `reviewer_persona`, `review_user_intro`,
`review_severity_definitions`, `review_confidence_high_example`,
`review_categories_template`, `review_examples`, `cross_check_persona`,
`cross_check_severity_definitions`, `verifier_persona`,
`verifier_source_priorities`. These are the strings that used to live inside
`prompts.py` and `verifier.py`.

**Code-basis line templates.** `review_user_code_basis_line`,
`cross_check_code_basis_line`, `verifier_system_code_basis_lines`,
`verifier_user_code_basis_lines`. Each is a format template rendered against
`code_basis_format_kwargs(cycle)` — one placeholder per `BaseCode.key`, plus
`asce7`, `asce7_prev`, and `pinned_standards`. Separate templates per surface
because the surfaces legitimately differ: the verifier prompt says "CEC" where
the reviewer prompt says "Energy Code."

**Deterministic-detector vocabulary.** A `DetectorVocabulary` carrying
`code_abbreviations`, `plausible_cycle_years`, `valid_cycle_years`,
`asce7_plausible_editions`, `stale_cycle_extra_patterns`,
`flag_leed_references`, and `jurisdiction_label`. Note the invariant baked into
the type: `plausible_cycle_years` must be a subset of `valid_cycle_years`, which
is what keeps the *stale-cycle* and *invalid-cycle* detectors of [**Ch 4 —
Input**](04_input.md) disjoint by construction rather than by careful authoring.

**Routing and chunking data.** `profile_keywords` (a `ProfileKeywords` with
`jurisdictional` / `manufacturer` / `code_standard` / `internal_coordination`
term tuples) and `cross_check_chunk_groups` (a tuple of `ChunkGroup`, each with
a `chunk_id`, a `label`, and CSI prefixes).

**Report display slots.** `report_context_phrase` and `report_title`.

**The capability flag and what it gates.** `project_profile_enabled` (default
`False`) plus the slots it turns on: `research_persona`, `research_dimensions`,
`corpus_signal_patterns`, `compliance_persona`,
`compliance_severity_definitions`, and `polity_suspect_tokens`. This is the
subject of [**Ch 19 — Location-Aware Review**](19_location_aware_review.md).

### The protocol/domain line

The critical design decision is *what did not move*. The prompt builders still
own the **protocol**: the task framing, the output and tool contracts, the
confidence-rubric bands, the review procedure, the verifier's source-tier
framing rules. Those are byte-identical across every module. Only the domain
content renders from module slots.

The reason is a trust argument, not an aesthetic one. The parsers in [**Ch 5 —
The Review Engine**](05_review_engine.md) — the tool schema, the tagged-JSON
salvage path, the confidence clamp — are the machinery that turns an unreliable
narrator into reliable structure. If a module author could restate the output
contract, a module author could break every downstream consumer while appearing
to have only written some domain prose. Keeping the protocol engine-owned means
**a module author cannot break the parser.** They can make the review *worse* —
a bad persona produces bad findings — but they cannot make it *unparseable*.

### 2.1 One string that did not make it out

The extraction is not complete, and the handbook's own convention is to flag
drift rather than assert a clean result. Here is the drift.

`verifier._get_verification_system_prompt` builds its `web_fetch` usage block
unconditionally, and that block hardcodes a source-priority ordering:

```
- Fetch the most authoritative-looking source first (California
  regulatory pages > code-publisher full text > standards bodies >
  manufacturer datasheets). ...
```

So an Ontario data-center verification prompt *does* contain the word
California, and does tell the model to prefer California regulatory pages when
choosing what to read in full. Confirmed by building the prompt for
`datacenter_fire`: the review and cross-check system prompts come back clean;
the verifier system prompt does not.

Two things keep this from being worse than it is. The guidance only orders
*which already-surfaced URL to fetch first* — it does not steer `web_search`,
which is the primary grounding mechanism, and it cannot manufacture a California
source for an Ontario query that never returned one. And on the current defaults
the deepest verification tier routes to Opus 5, which does not support web fetch
at all (see [**Ch 12 — Configuration, Models & Token
Economics**](12_configuration_and_models.md)), so the block is frequently inert.

It is still a domain string in a protocol builder, which is exactly what §2 says
should not happen. The reason it is unconditional is explained in a comment at
the site: the prompt is cached and shared across modes for a cycle, so the
builder cannot know whether *this* call will have `web_fetch` attached, and it
leans on the tool list to gate availability.

The natural fix is that the ordering should render from the module's existing
`verifier_source_priorities` slot rather than being hardcoded — the slot already
exists and already carries exactly this kind of content. That change touches the
highest-stakes prompt in the program and would move the data-center verifier
golden, so it is called out here as known work rather than done quietly.

## 3. Registration is a validation gate

`validate_module_registry` runs at import and refuses to start on a malformed
module. This is deliberate: a domain module is data, data gets authored by
humans, and the failure mode of bad domain data is a silently degraded review
rather than a crash. So the registry front-loads the crash.

It rejects duplicate module ids; **duplicate cycle labels** (labels are
registry-unique because they namespace the verification cache *and* back the
`module_for_cycle` bridge of §6); empty prompt slots; any template slot that
does not format against the module's own cycle; empty or duplicate-keyed
`base_codes`; an inconsistent detector vocabulary (the subset invariant above);
chunk groups with duplicate ids, the reserved `general` id, or one CSI prefix
claimed by two groups; and — the sharpest check — **few-shot examples that
violate the real parse contract**.

That last one is worth dwelling on. `_validate_review_examples` extracts every
JSON object from a module's `review_examples` string and runs it through
`reviewer.validate_edit_shape` — the *actual* production validator from [**Ch 11
— The Trust Model & Report Output**](11_trust_model_and_output.md). A module
that ships an example the parser would demote does not load. Examples must also
not mention `evidenceElementId` or element-id tags, because the example block
sits inside the cached system-prompt prefix and would teach the model to emit a
field the prefix has not yet introduced.

The conditional rule for the location-aware slots (`_validate_research_slots`)
is two-directional: flag enabled ⇒ the slots must be non-empty; flag disabled ⇒
they must be *empty*. The second half is what prevents dead content — a module
carrying research dimensions it will never run is a lie about its own
capabilities.

Unknown ids degrade rather than raise: `get_module(None)` and
`get_module("typo")` both return `DEFAULT_MODULE` (`california_k12_mep`). That
asymmetry is intentional. A malformed *registry* is an authoring bug and should
stop the program; an unknown *id* is usually a stale persisted selection and
should quietly fall back.

## 4. Routing: assigning specs to modules

A single-module program needs no routing — every spec goes to the one module.
The interesting case is `hyperscale_datacenter`, whose four modules
(`datacenter_fire`, `datacenter_architecture`, `datacenter_electrical`,
`datacenter_electronic_safety_security`) each own a slice of the discipline
space.

`programs/routing.py` is a **deterministic** classifier — no model call. It
reads three document surfaces, in descending order of trust:

| `RoutingEvidenceSource` | Signal |
|---|---|
| `CSI_SECTION` | The parsed CSI section number — Division 21 → fire suppression, Division 26 → electrical, and so on |
| `SECTION_TITLE` | Title-term patterns (e.g. sprinkler, enclosure, switchgear, fire alarm) |
| `CONTENT` | Body-term patterns, weighted by match count |

Each signal becomes a `RoutingEvidence` record with a weight, and the accumulated
evidence produces a `SpecRoutingDecision` in one of three `RoutingState`s:

- **`SUPPORTED`** — the evidence names one or more modules the program contains.
- **`AMBIGUOUS`** — the evidence is contradictory or too weak to commit.
- **`UNSUPPORTED`** — the spec belongs to a discipline the program has no module
  for. Division 27, and Division 28 scopes other than fire detection and alarm
  (access control, video surveillance, intrusion detection), route here
  deliberately.

`UNSUPPORTED` is a feature. The alternative — quietly assigning an access-control
spec to the nearest module and letting it be reviewed by a reviewer that does not
know the domain — is exactly the *confidently wrong* failure mode [**Ch 1 — The
Problem Domain**](01_problem_domain.md) argues is worse than no tool at all. An
explicit coverage gap is an honest answer. The decision is also overridable
(`apply_user_override` / `remove_user_override`), so an operator who knows better
can force an assignment, and the override is recorded alongside the automatic
decision rather than replacing it.

The fire-alarm case shows the routing granularity. Fire alarm specifications
appear under legacy Division 28 31 or current Division 28 46; both route to the
electronic-safety-and-security module. Division 21 stays with fire suppression
and Division 26 stays with electrical. So a project's fire *suppression* and
fire *alarm* specs are reviewed by different modules with different code bases —
which raises the obvious question §5 answers.

## 5. What routing does not buy you

**Cross-spec coordination is still child-module scoped.** The coordination pass
of [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md) runs
*within* a module's set of specs, not across the program. A Division 21
suppression spec is therefore **not** directly compared against a Division 28
alarm spec, even though both are in the same program and the same building.

This is a real blind spot and the handbook should not soften it. Suppression and
detection are exactly the pair a coordination pass would most want to read
together — the alarm system's initiating devices and the sprinkler system's flow
switches are one interface. Catching a mismatch there requires a future
program-level coordination pass, which does not exist today.

There is a tempting non-fix: dual-route every alarm spec into the fire-suppression
module as well, so that one module sees both. Do not. It duplicates the review
cost of every alarm spec and produces duplicate findings that then have to be
deduplicated across modules with different code bases — paying twice for a worse
answer than the real fix.

This is the same category of honest compromise as the chunking limitation in
Ch 8: the program does the tractable thing and says out loud what the tractable
thing cannot see.

## 6. The `module_for_cycle` bridge

The extraction happened in phases, and the phases had to interleave with a
running program. The bridge is the seam that made that possible.

Prompt builders and the verification router still take `cycle=` in their
signatures, not `module=`. They resolve the owning module via
`registry.module_for_cycle(cycle)` — a reverse lookup keyed on the cycle's
label, which the registry guarantees is unique. This is why Phase 2 could move
every prompt string into module slots while changing **zero public signatures**.

Orchestration is further along: `start_batch_review`,
`reconstruct_batch_submission`, `thin_submission_from_batch_results`,
`start_batch_verification`, and `collect_batch_verification_results` all take
`module=` directly. Stage functions that hold a submission derive the module from
`submission.module_id` rather than accepting one, so a single state object cannot
pair one module's identity with another module's cycle.

`module_id` persists on `BatchSubmission`, `PipelineResult`, and `PendingBatch`
as an additive field with a default — **no pending-batch schema bump** — so a
resume state written by an older build still loads, resolving to the default
module. It also lands in the trace's `run.json`, which means [**Ch 14 —
Observability**](14_observability.md)'s replay can tell you which domain
reviewed a spec.

The bridge is scaffolding, and it is meant to come down: it retires when the
remaining content layers thread `module=` explicitly.

## 7. Where the cache key sits in all this

The verification cache key (see [**Ch 10 — Verification II**](10_verification_grounding.md))
leads with `cycle_label`. Because labels are registry-unique per module, this
single fact carries a lot of weight:

- A verdict grounded under one module's code basis **can never** replay for
  another module's. Two modules cannot collide in the cache even if their claim
  text is identical.
- Changing a module's code basis naturally invalidates its prior entries, because
  the label changes.
- The California module's legacy 2022-cycle mapping was removed and **must not
  be reintroduced**. A different code basis is a different module with its own
  registry-unique label — not a second cycle inside one module.

## 8. Phase 5: making the report tell the truth

The last extraction phase was the report, and it is the phase where a subtle
class of bug lived.

Every domain-worded report surface now renders from the **assigned run module**,
resolved once from `PipelineResult.module_id` in `export_report`: the Heading-0
title (`report_title`), the "Code Cycle:" metadata line, the methodology cycle
sentence (whose jurisdiction wording comes from
`detector_vocabulary.jurisdiction_label`, rendering a generic form when empty),
the methodology `report_context_phrase`, the pinned-editions paragraph, and the
stale/invalid-cycle alert headings.

The pinned-editions paragraph is the instructive one. `_render_pinned_editions_note`
previously took a cycle *label* and looked it up in `AVAILABLE_CYCLES` — and that
lookup **silently fell back to the California default for unknown labels**. On a
single-domain program that is invisible. On a data-center report it would have
rendered California's NFPA and ASHRAE editions into a document reviewing an
Ontario building. The fix was to pass the module's own `cycle` object rather than
a label to be re-resolved.

That bug is a good closing argument for this chapter's premise. Extracting the
domain was not merely tidy-up; the hardcoded default was a *correctness* hazard
that only became visible once a second domain existed to be wrong about.

A smaller instance of the same care: the invalid-year parenthetical renders
`plausible_cycle_years` — the *published* list — rather than `valid_cycle_years`,
which also admits an anticipated-but-unpublished future cycle. And the "every N
years" cadence phrase is computed only when the year gaps are actually uniform,
rather than asserted.

## 9. Authoring a new module

The work is module data plus registry, program, and routing wiring. In practice:

1. Define the `CodeCycle` (base codes, pinned standards editions, a
   registry-unique label).
2. Fill every required prompt, code-basis-line, detector-vocabulary,
   profile-keyword, chunk-group, and report slot.
3. Decide on `project_profile_enabled`, and fill *or deliberately leave empty*
   the location-aware slots accordingly — the validator enforces both directions.
4. Register in `AVAILABLE_MODULES`; add to a program in `AVAILABLE_PROGRAMS`.
5. For a multi-module program, add routing signals so specs actually reach it.
6. Write goldens. The existing pins — `tests/test_golden_domain_surfaces.py` and
   `tests/test_domain_routing_pins.py` for California,
   `tests/test_golden_datacenter_surfaces.py` for the data-center modules — are
   the pattern, and the California ones must stay byte-green.

`docs/datacenter_fire_module_plan.md` is the full work order — contract, research
protocol, tests, hard constraints — and doubles as the general authoring guide.

## 10. What this chapter changes about the rest of the handbook

Chapters 1 through 17 describe the engine truthfully but describe the *domain*
as though it were still welded in. Read them with one substitution: where a
chapter says the prompt names California, read "the prompt renders the assigned
module's persona, which for the default module names California." The mechanism
each chapter describes is unchanged; its content source moved.

The three chapters that follow take up what the module system made possible:
[**Ch 19 — Location-Aware Review**](19_location_aware_review.md) for the
capability flag and the pipeline it gates, [**Ch 20 — Drawings**](20_drawings.md)
for construction-drawing input, and [**Ch 21 — The Real-Time Review
Transport**](21_realtime_transport.md) for the alternative to the batch backbone.
