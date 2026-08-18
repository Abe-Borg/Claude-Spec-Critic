# Drawings: Vision at Attach Time & Impact Synthesis

A construction specification is half a document. The other half is the drawing
set, and a reviewer who has read only the specification is working with one eye
closed. The spec says the sprinkler system shall be hydraulically calculated; the
drawings say which areas are Extra Hazard Group 2. The spec references "the
mechanical schedules"; the schedules are on sheet M-601. The spec is silent about
a mezzanine that the plans clearly show.

This chapter is about the two passes that bring drawings into a review — and it
is the one chapter where Spec Critic sends something other than text to the API.

It is also a chapter about a constraint that shaped both passes: **everything
downstream of the attach step stays plain text.** The vision pass happens once,
at attach time, and everything after it — prompts, prompt-cache breakpoints,
pending-batch persistence, resume — is byte-untouched. That constraint is why
drawings could be added without disturbing any of the machinery Chapters 5
through 11 describe.

## 1. Why a digest, and why at attach time

The obvious design is to send the drawing PDFs along with every review call. It
is also wrong, for three compounding reasons.

**Cost.** A drawing set is hundreds of pages of dense vector graphics. Sending it
with every per-spec review multiplies that cost by the number of specs.

**Cache.** The prompt-cache discipline of [**Ch 5 — The Review Engine**](05_review_engine.md)
depends on a byte-stable instruction prefix. Threading document content blocks
through the per-spec request would relocate cache breakpoints.

**Resume.** [**Ch 7 — Orchestration & State**](07_orchestration.md)'s pending-batch
state deliberately never serializes spec bodies; they are re-extracted
deterministically. Binary PDFs have no such cheap reconstruction, and a resumed
run would either re-pay the vision cost or persist megabytes of base64.

So the design inverts it: **one synchronous vision pass at attach time** turns
the PDFs into a plain-text digest, the operator merges that digest into Project
Context, and every downstream phase sees ordinary text. The vision cost is paid
exactly once. A resumed run pays nothing, because the digest text already rides
in the persisted `project_context`.

There is a fourth benefit that is really a trust benefit: **the operator can read
and edit the digest before any review spend.** A vision model's reading of a
drawing set is exactly the kind of output that should not be trusted silently. It
lands in an editable textbox first.

## 2. The request shape

The PDFs go up as native base64 `document` content blocks. Two details are
load-bearing.

**No beta header.** Native PDF input is generally available. The retired
`web-fetch-2026-02-09` header is the cautionary precedent here — see [**Ch 17 —
Evolution & Lessons**](17_evolution_and_lessons.md) — because an *unrecognized*
`anthropic-beta` value is rejected with a 400, not ignored. The digest request
sends no beta header.

**No tools.** The task is transcription of provided documents, so the request
carries no tools at all. That has a pleasant consequence: with no server tools
there is no `pause_turn` loop to manage. An unexpected pause is treated as a
failure of that chunk rather than something to resume.

The model returns a fixed-section digest — PROJECT IDENTITY & OVERVIEW, SHEET
INDEX, GENERAL NOTES, SCHEDULES, COORDINATION OBSERVATIONS — with `[<file> p.N]`
page references and `[ILLEGIBLE]` honesty markers. The page references are what
make §6's impact synthesis able to cite a sheet; the illegibility markers are
what keep a low-resolution scan from being silently invented into confident prose.

`PHASE_DRAWING_DIGEST` is registered in `api_config` with a 24k output cap,
`medium` effort, and a system-only cache policy. Registration is not optional
housekeeping: an unregistered phase silently caps at 16k. The default model is
Sonnet 5, overridable via `SPEC_CRITIC_DRAWING_DIGEST_MODEL`.

## 3. Chunking: three caps, and which one actually binds

`build_digest_chunks` packs whole files greedily in order, splitting an oversized
file by page ranges with `pypdf`. Three caps apply:

| Cap | Value | Source |
|---|---|---|
| Pages per request | 600 | Hard API ceiling, total across all document blocks |
| Raw bytes per request | 20 MiB | Base64 head-room under the 32 MB API cap |
| **Context-window pages** | **~320 pages** | Derived: `effective_page_cap` |

The third is the one that actually binds, and the arithmetic is worth showing
because it is counterintuitive. At the conservative `DIGEST_TOKENS_PER_PAGE_ESTIMATE`
of 3,000 tokens per page, the API's own 600-page ceiling would be 1.8M tokens —
comfortably over even a 1M-token context window. So `effective_page_cap` derives
a page cap from the model's window rather than trusting the documented maximum.
**The window, not the 600-page ceiling, is the real limit.**

Two structural guarantees hold regardless of how the packing falls out: a PDF
whose page count cannot be determined is unsplittable and isolates into its own
chunk (you cannot safely range-split what you cannot count), and the **union of
chunk parts always equals the input**. No page is silently dropped.

## 4. Knowing the cost before paying it

`preflight_digest_cost` uses the exact `count_tokens` endpoint, which accepts
document blocks and is free. When that is unavailable it falls back to a local
`pages × 3k` estimate and flags the result `exact=False` — so the GUI can say
"estimated" rather than implying a precision it does not have.

The result feeds a confirmation dialog (`format_digest_confirm_message`) and
pre-flags any chunk that cannot fit the window. The operator sees the cost and
approves it before the vision call runs. For the app's single most expensive
per-unit call, an explicit confirm is proportionate.

## 5. Failure policy: visible, not silent

The digest mirrors the research fan-out of [**Ch 19 — Location-Aware
Review**](19_location_aware_review.md): per-chunk retries via
`DEFAULT_REALTIME_RETRY_POLICY`, and partial failure is tolerated but **inlined
visibly** in the digest text:

```
[Chunk 3 of 7 (plans.pdf pages 301-600) FAILED: ...]
```

The failure is also surfaced in the GUI. A `max_tokens` truncation keeps the
text that was paid for, under a visible warning line. Only an all-chunks-failed
outcome raises `DrawingDigestError`.

The reason the failure marker is inlined into the digest body rather than logged
separately is the same reason [**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md)
cares about surfacing failed reviews: a digest that silently omits sheets 301–600
looks exactly like a digest of a smaller drawing set. The gap has to be visible
in the artifact the reviewer actually reads.

## 6. Drawing-impact synthesis: making the value visible

The digest puts drawings *into* the review — as plain-text context on every call
— but attributes **nothing** back to them. So the report could not answer the
question an operator who just paid for a vision pass will immediately ask: *did
uploading the drawings actually help?*

`drawing_impact/impact_synthesizer.py` closes that gap with one grounded
post-review synthesis call.

### Placement

`run_drawing_impact_for_batch` runs **last** in the collect sequence — after
cross-check, after compliance, and after their round-2 verification. The ordering
is a dependency, not a preference: the pass links its narrative to specific
findings, so every finding it can reference must already carry a stable id and,
where applicable, a verdict.

It is modeled on cross-check: one synchronous structured-tool call
(`submit_drawing_impact` → `DRAWING_IMPACT_SCHEMA`), retries via
`DEFAULT_REALTIME_RETRY_POLICY`, a `<drawing_impact_json>` text fallback, and no
web or search tools — it is synthesis over text the run already produced, so
again there is no `pause_turn` loop.

### Output

A `DrawingImpactResult` carries an `impact_level` (`substantial` / `moderate` /
`minimal` / `none`), a plain-text `narrative`, and per-finding `finding_links`,
each classifying the relationship as `corroborated`, `contradicted`, or
`contextualized`, with the digest's own `[<file> p.N]` sheet references.

### The gate is digest-presence, not the module flag

The pass runs if and only if `extract_drawing_digest(project_context)` finds a
`Construction Drawing Digest` attachment block — matched on the exact
`wrap_attachment` BEGIN/END marker lines. A context file merely *named* something
similar does not match, because its label carries a file extension.

Note carefully that this gate is **not** `project_profile_enabled`. Drawings can
be attached under any module, the California default included. Tying drawing
impact to the location-aware flag would have been an easy mistake and would have
denied the feature to the program that has the most users.

A run without drawings leaves `state.drawing_impact_result` at `None`, renders no
report section and no banner row, and is byte-identical to before — the same
presence-switch discipline as Ch 19.

It runs in both drivers (the GUI collect sequence and
`run_batch_collection_headless`), self-gates identically in each, and reads the
digest from the already-persisted `project_context`. That last detail means
**resume needs no pending-batch schema bump** and the vision cost is never
re-paid.

### Guardrails against a flattering lie

This pass has an obvious incentive problem: it is a feature whose job is to
report on the value of a feature. A model asked "how did the drawings help?" will
find a way to say they helped.

Three mechanisms push against that:

1. The prompt **forbids inventing** a page reference or a finding connection, and
   explicitly instructs an honest `none` or `minimal` when the drawings added
   little.
2. **Every `finding_link` whose id is not one of the real findings passed in is
   dropped at parse time.** `_parse_impact_payload` validates against the set of
   valid ids, so a hallucinated link cannot reach the report — not "is unlikely
   to," cannot.
3. Duplicate ids collapse first-wins, so one finding cannot be counted twice to
   inflate an apparent impact.

`_coerce_level` and `_coerce_relationship` also clamp out-of-vocabulary values to
`minimal` and `contextualized` respectively — the conservative ends of both
scales, rather than the flattering ends.

`PHASE_DRAWING_IMPACT` is registered with a 16k output cap, `high` effort, and a
system-plus-tools cache policy; the default model is Sonnet 5 via
`SPEC_CRITIC_DRAWING_IMPACT_MODEL`.

## 7. How it renders, and the amber note

The report renders **"How the Drawings Informed This Review"** — impact badge,
narrative, per-finding links — immediately after the methodology note and *above*
the findings it references, plus a conditional "Drawing analysis impact"
Run-Diagnostics row.

When the pass fails, the report renders an **honest amber note**, not a red
error. The wording matters and is worth preserving in any future edit: the
drawings *did* inform the review — they were in Project Context on every call —
only the summary of how is missing. A red "the drawings were ignored" would be
false, and would tell an operator to distrust findings that were in fact
drawing-informed.

`DrawingImpactResult` rides `CollectedBatchState` and `PipelineResult` as an
additive field with a `None` default, read downstream via defensive `getattr`,
so legacy callers and test doubles are unaffected.

## 8. The GUI flow

`context_controller.attach_drawing_files` sequences: guards → file picker →
synchronous local validate/pack → background preflight thread → cost-confirm
dialog → background digest thread. Every tkinter mutation is marshaled back
through `app.after(0, ...)`, per the threading discipline of [**Ch 13 — The
Desktop GUI**](13_gui.md), and a running-flag plus button disable prevents
concurrent digests.

An over-cap merge is **refused with an actionable message, never truncated** —
the same rule the Project Context attachment helpers of [**Ch 4 —
Input**](04_input.md) follow. Silently dropping half a drawing digest to fit a
cap would reintroduce exactly the invisible-gap problem §5 works to avoid.

Headless callers use `digest_drawing_files(paths)` and `wrapped_digest_block(result)`,
then pass the merged text to `start_batch_review(project_context=...)`. Zero
pipeline changes — which is the whole architectural point of this chapter.

## 9. Pins

`tests/test_drawing_digest.py` covers packing, the three caps, the split
round-trip, the document-block request shape with its no-beta and no-tools
assertions, index-ordered merge, partial-failure honesty, retry, truncation,
preflight cost math, and phase registration.

`tests/test_drawing_impact.py` covers the extraction gate, the strict-subset
schema, the prompt shape, parse/coercion/id-drop/dedup, a scripted-client
end-to-end, pipeline gating and finalize carry-through, and report rendering in
both the present and absent states.
