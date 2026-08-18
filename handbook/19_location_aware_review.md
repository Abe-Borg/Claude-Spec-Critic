# Location-Aware Review: Profile, Research & Compliance

The California module can pin its code basis because California has one. Title
24 is Title 24; the 2025 cycle adopts the standards editions listed in
`CALIFORNIA_2025`; the AHJ is DSA or HCAI. A module author can write those facts
down once and they stay true until the next triennial cycle.

Now try to write a module for **hyperscale data centers in the USA and Canada**.
Which building code applies? It depends on the state or province. Which edition
of NFPA 72? It depends on local adoption, which lags the national standard by a
variable number of years and is amended locally. Who is the AHJ? A city fire
marshal whose interpretations are not in any standard. What does the client
require *beyond* code? That is in a design guideline the model has never seen.

There is no set of strings a module author can pin that answers those questions,
because the answers are properties of a **project**, not of a domain. This
chapter is about the pipeline that closes that gap: the operator supplies a
location, the program researches what that location requires, and the review is
checked against what it found.

The whole thing hangs off one boolean.

## 1. One flag, one discipline

`ReviewModule.project_profile_enabled` (default `False`) gates the entire
pipeline described here. `california_k12_mep` leaves it off. The four data-center
modules turn it on.

The discipline that makes this safe to have in the codebase at all is stated in
[**Ch 18 — Modules & Programs**](18_modules_and_programs.md) and is worth
repeating because this chapter is where it gets stressed: **every surface a
profile-less run touches must stay byte-identical.** The California goldens, the
routing pins, the verification cache keys, the report bytes, the tool
dictionaries. The flag — or, downstream of it, the mere *presence* of a profile
object — is the switch. Not a code path that "usually" produces the same output.

You will see that switch implemented four different ways in this chapter,
because it has to hold at four different layers: a `None` default on a dataclass
field, an optional trailing cache-key segment, a conditional report section, and
a tool-dict key that is only ever added.

## 2. `ProjectProfile`: four fields that steer everything

`src/core/project_profile.py` is dependency-free and frozen, and carries exactly
four per-run facts: `city`, `state_or_province`, `country` (`"US"` or `"CA"`),
and `client_name`.

That is a very small object to hang a pipeline on, and its normalization is
disproportionately load-bearing. Two derived values do the real work:

- **`jurisdiction_fingerprint()`** keys the verification cache's optional sixth
  segment (§6).
- **`web_search_user_location()`** steers every web search the run makes.

So a typo in the city name does not produce a visible error — it silently
misroutes both the cache and the searches, and the run confidently researches
the wrong place. The mitigations are deliberately boring: `state_or_province` is
a dropdown of canonical codes rather than free text, city is trimmed and
casefolded, and the run **echoes the parsed location back to the operator before
any review spend**. The last one is the real defense. A fingerprint is not
human-readable; a rendered "Toronto, Ontario, Canada" line is.

The profile persists additively on `BatchSubmission`, `PendingBatch`, and
`PipelineResult` — no schema bump, so older resume states still load.

## 3. Requirements research: a fan-out before the first review

Research runs **inside `start_batch_review`**, before review submission. That
placement is not incidental. `start_batch_review` is the single submission entry
point, so putting research there puts the GUI and headless callers in lockstep,
and — critically — it means the rendered research block is already inside
`project_context` when the token preflight counts and the batch submits.

### The free part first

`research/corpus_signals.py` runs a deterministic, **no-API** scrape over the
extracted specs, using the module's `corpus_signal_patterns`. It collects
`document_names`, `consultant_insurer_mentions`, `edition_governance_sentences`,
and `standards_with_editions`.

This is the cheapest possible signal and it runs first for a reason: the specs
themselves often name the governing guideline, the insurer (FM Global's
requirements are frequently the controlling authority on a data-center fire
system), or an explicit edition-governance sentence. Scraping that costs nothing
and makes every subsequent search better-targeted.

### The paid part

Each module declares `research_dimensions` — a tuple of `ResearchDimension`,
each with a `dimension_id`, a `title`, a `prompt_template`, and per-dimension
`max_searches` / `max_fetches` budgets. One streaming call per dimension runs in
parallel through a `ThreadPoolExecutor` (default 4 workers,
`SPEC_CRITIC_RESEARCH_WORKERS`), each with `web_search` and `web_fetch`, and each
grounded through the same `validate_cited_sources` used by verification — see
[**Ch 10 — Verification II**](10_verification_grounding.md). Research citations
are held to the same standard as verification citations: a URL the model asserts
but never retrieved does not count.

Output is a typed `RequirementsProfile`:

| Field | Meaning |
|---|---|
| `items` | The `ResearchItem`s found across all dimensions |
| `dimension_statuses` | Per-dimension `completed` / `failed`, with item counts and search/fetch usage |
| `research_date` | ISO date — edition and process facts are *time-stamped* claims |
| `project` | The serialized `ProjectProfile` the research ran for |

And each `ResearchItem` carries `topic`, `category`, `requirement`, `authority`,
`code_reference`, `source_urls`, `accepted_sources`, `grounded`, `confidence`,
`notes`, and — the field §5 turns on — **`actionability`**, either
`spec_requirement` or a process advisory.

### Failure policy

- **At least one dimension succeeds** → continue with a partial profile, and the
  run ends in an amber terminal state rather than a clean green.
- **Every dimension fails** → `ResearchFanoutError` aborts the run **before
  anything is billed for review.**

The asymmetry is the point. A partial profile still improves the review; a
profile that is entirely absent means the location-aware modules would review
against nothing, and paying for a full review batch to discover that is the
expensive way to learn it.

### How the research reaches the model

`RequirementsProfile.render_text()` produces a deterministic block that is
spliced into `project_context` through the ordinary `wrap_attachment` helper of
[**Ch 4 — Input**](04_input.md). This is the same channel a human-typed project
note uses, which means review, cross-check, and verification all see the research
at plain-text cost with no new plumbing.

If the merged context would exceed the 100k `PROJECT_CONTEXT_MAX_TOKENS` cap, the
block is trimmed **lowest-confidence-first** — and trimmed **from the rendered
block only, never from the structured items**. That distinction matters
downstream: the compliance pass (§5) reads the structured items, so a run whose
rendered block was trimmed for context budget still checks the full requirement
set.

Research is **never re-run on resume.** The rendered text is already inside the
persisted `project_context`, and the structured items persist additively. A
resumed run does not re-pay for research, and — more importantly — cannot
silently research a *different* answer than the one the review was submitted
against.

## 4. The compliance pass

Cross-check asks "do these specs contradict each other?" Compliance asks a
different question: **"does this package address what this jurisdiction and this
client actually require?"** The first is a question about internal consistency;
the second needs an external requirement list, which is exactly what §3 just
produced.

`src/compliance/compliance_checker.py` is modeled byte-for-byte on the
cross-check pass of [**Ch 8 — Cross-Spec Coordination**](08_cross_spec_coordination.md)
— a synchronous structured-tool call, the same retry policy, the same tagged-JSON
fallback — and is inserted **after cross-check and before verification round 2**.

**The gate is two-part:** the flag is on *and* a structured `requirements_profile`
is present. Flag off ⇒ `state.compliance_result` stays `None` and no report
section renders. Flag on but no profile ⇒ an explicit `skipped` status, which is
a different and more honest thing than `None`: the operator asked for a
compliance pass and did not get one.

### Only grounded spec requirements are controlling

This is invariant 4 of the location-aware work order and the most important rule
in the pass. A requirement is **controlling** — eligible to produce a `missing`
row — only if it is both `grounded` and carries `actionability ==
"spec_requirement"`.

Process advisories are excluded *entirely*. An item saying "submit the fire
protection narrative to the AHJ before permit" is a real, useful, correctly
researched fact about the project — and it is a statement about the project team's
schedule, not about the specification's content. Letting it generate a `missing`
finding would tell the specifier to write a schedule commitment into a
specification section, which is a defect the tool would have invented.

### Output shape

The pass emits an ordinary `ReviewResult`, reusing `cross_check_status` as its
own pass status, so every downstream consumer (report, sidecar, diagnostics)
works unchanged. Its findings get `[Compliance]` section labels and — the
collision firewall — **`lc-` ids** via `assign_compliance_finding_ids`, alongside
review's `rf-` and coordination's `cf-`. Since ids are content-derived, the
prefix is what guarantees a compliance finding and a review finding with
identical content never collapse into one sidecar entry.

It also produces a **coverage matrix** on `ReviewResult.coverage` — one entry per
controlling requirement, classified `represented` / `missing` / `contradicted` /
`unclear`.

### Chunking, and the absence rule

When the package exceeds `COMPLIANCE_RECOMMENDED_MAX` (which reuses
`CROSS_CHECK_RECOMMENDED_MAX` — both are package-level passes), the pass chunks
over the module's CSI chunk groups. Merging chunk results uses the precedence:

```
contradicted  >  represented  >  unclear  >  missing
```

with `missing` surviving **only when every chunk that classified the requirement
said missing**.

That asymmetry encodes a real epistemic point. A chunk that did not see the
requirement addressed has learned that it is *absent from that chunk*, which is
not evidence it is absent from the package — the sprinkler requirement may well
be addressed in a division this chunk never read. A chunk-local absence is not a
package miss. Only unanimity across every chunk that looked justifies the claim.
ADD/missing findings survive only when the merged status for their referenced
requirement is genuinely `missing`.

### Compliance findings always ride verification

Round-2 verification is not optional for compliance findings. The reason is that
their characteristic failure mode is not the one URL-grounding catches.

A compliance finding says, in effect, "the Ontario Building Code requires X and
your package does not address it." The dangerous version of that claim is not a
bad citation — it is a **fabricated designation** or a **jurisdiction
misattribution**: a requirement that exists in one province asserted for another,
or a standard designation that does not exist at all. Refutation is where
verification earns its cost, and that is a round-2 job.

## 5. Location-aware verification

An optional `user_location` dict threads to the **`web_search` tool builder
only**. Two details are contractual:

- It travels *alongside* the routing decision, not inside it. The routing
  decision is a pure policy object that round-trips through `to_dict`/`from_dict`
  for batch telemetry; location is request-shaping data.
- **`web_fetch` has no location parameter and must never carry the key.** A tool
  dict with an unrecognized key is a 400 at submit, and this codebase has already
  paid for that lesson once (the retired web-fetch beta header — see [**Ch 17 —
  Evolution & Lessons**](17_evolution_and_lessons.md)).

`None` everywhere ⇒ today's bytes.

## 6. The cache key's sixth segment

The verification cache key of [**Ch 10 — Verification II**](10_verification_grounding.md) is:

```
cycle_label | standards_fingerprint | actionType | codeReference | sha256(claim_summary)
```

plus a trailing `| jurisdiction_fingerprint` segment **only when the run carries
a `ProjectProfile`.**

The design choice worth noting is what was *not* done: there is no `_no_loc`
sentinel segment for profile-less runs. A California run produces a key
byte-identical to the five-segment shape it always produced, so **every existing
California cache entry stays warm** across this entire feature.

And a profile-present run gets exactly the isolation it needs: a verdict grounded
against Toronto's adopted codes can never replay for a query about Phoenix.
Without the segment it would, because the claim text and code reference could be
identical while the correct answer differs.

## 7. Two deterministic guards

The location-aware modules added two checks that run without a model call, and
both fit the deterministic-pre-screen philosophy of [**Ch 4 — Input**](04_input.md).

### Wrong-polity tokens

The module slot `polity_suspect_tokens` is a tuple of `PolityTokenRule`
(`country` / `pattern` / `note`). When — and only when — a profile is present,
the pre-screen applies the rules for the profile's country, emitting alerts with
`deterministic_rule="wrong_polity_token"`.

The motivating defect is a template artifact: a Canadian project's specification
that says equipment shall be "UL listed," when the Canadian requirement is a
`cULus` or `ULC` listing. It is a small phrase with real consequence, and it is
exactly the kind of thing a US-authored template carries across the border
unnoticed.

The engine applies each pattern **flat**, with no proximity suppression. That
places the burden on the seed rules to be precise, and the data-center seed set
relies on word boundaries: a bare `UL listed` matches, while `cULus` and `ULC`
do not.

### Deterministic anchor validation

`validate_finding_anchors` demotes any ADD/EDIT/DELETE finding whose `anchorText`
or `existingText` is not a verbatim (or whitespace-collapsed) substring of the
spec it names — to REPORT_ONLY, with a `demotion_reason` stamped.

It applies to review, cross-check, **and** compliance findings, and it **never
drops** a finding (invariant 8). That is the correct severity: a finding whose
quoted anchor does not appear in the document has failed to prove it can locate
its own target, so it must not ship an edit instruction — but the underlying
observation may still be true and a human should see it.

## 8. What the operator ends up reading

The report gains a **"Jurisdiction & Client Requirements"** section: per-category
items, an adopted-vs-current edition delta table, the coverage matrix, and a
separate "Process & Schedule Advisories" area for the non-controlling items from
§4. Two conditional Run-Diagnostics rows appear ("Location/client research",
"Local-code compliance"), and the title block gains centered `Project:` and
`Client:` lines.

All of it is gated on profile-presence, so a profile-less report is byte-identical
to the one [**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md)
describes.

The edit sidecar moves to **schema v4** — compliance findings join the sweep, and
the top level gains `project` and `requirements_coverage`.

There is also a standalone **`<report-stem>.profile.json`**, and it is arguably
the most valuable artifact the run produces. The report is about one review of one
package at one moment. The researched requirements profile — what this
jurisdiction adopts, which editions, which authority, what the client's guideline
demands — has the **longest half-life** of anything in the run. It stays true
after the findings are fixed, and it is worth keeping.

## 9. Where the invariants live

The location-aware work order enumerates its own invariants, and two are worth
carrying into any future edit of this subsystem:

- **Invariant 9 — register new phases.** `PHASE_RESEARCH` and `PHASE_COMPLIANCE`
  must be registered in `api_config` with an output budget, a cache policy, and
  an effort level. An *unregistered* phase does not fail loudly; it silently caps
  at 16k and drops its effort setting. See [**Ch 12 — Configuration, Models &
  Token Economics**](12_configuration_and_models.md).
- **Invariant 10 — never relax the conditional slot rule.** The D-2 validation in
  `_validate_research_slots` (enabled ⇒ non-empty, disabled ⇒ required-empty) may
  be *extended* to new slots, never loosened.

The pins are `tests/test_project_profile*.py`,
`tests/test_requirements_research.py`, `tests/test_compliance_pass.py`,
`tests/test_anchor_and_polity.py`, `tests/test_location_aware_verification.py`,
the data-center goldens in `tests/test_golden_datacenter_surfaces.py`, and the
end-to-end `tests/test_datacenter_e2e.py` — which also pins **California
neutrality through the identical driver**, so the byte-identity claim this
chapter opened with is tested rather than asserted.
