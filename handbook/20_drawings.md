# Drawings: Analyzer Output at Attach Time & Impact Synthesis

A construction specification is half a document. The other half is the drawing
set, and a reviewer who has read only the specification is working with one eye
closed. The spec says the sprinkler system shall be hydraulically calculated; the
drawings say which areas are Extra Hazard Group 2. The spec references "the
mechanical schedules"; the schedules are on sheet M-601. The spec is silent about
a mezzanine that the plans clearly show.

This chapter is about how drawings get into a review and how the report accounts
for what they were worth. It starts from a design decision that is easy to miss
because of what it removes: **Spec Critic does not read drawings.** The operator
runs a separate drawing-analyzer program over the drawing set and attaches that
program's text output. From that point on, everything is plain text — prompts,
prompt-cache breakpoints, pending-batch persistence, resume — and nothing in
Chapters 5 through 11 had to change to accommodate drawings at all.

## 1. Why the app does not read drawings

Through v3.11 the app had a vision pass of its own: drawing PDFs went up to the
API as native `document` blocks, a Sonnet call transcribed each chunk into a
digest, and the digest was merged into Project Context. It was the one non-text
API call in the program, and it dragged a lot of machinery behind it — page and
byte chunking, a `count_tokens` preflight that uploaded an anchor PDF *before*
the cost-confirmation dialog, a cost dialog, a progress bar, a running flag the
close and update guards had to respect, and a trust-dossier card explaining all
of it.

That pass is gone. The operator's own analyzer reads the drawings — it is a
tool built for that job, and it can be run, tuned, and re-run outside this app
at no cost to the review. What the app needs is the *result*, as text. Taking
the result instead of the PDFs:

- removes the only non-text request, the only upload that preceded a cost
  confirmation, and a per-run spend the operator had to approve;
- keeps the digest **editable before any review spend** — it lands in the
  Project Context textbox first, where the operator can read it, trim it, or
  delete it;
- leaves resume untouched: the text rides in the persisted `project_context`,
  so a resumed batch needs nothing extra and nothing is ever re-paid.

The cost of the decision is also plain: the app cannot check the analysis
against the sheets, and the report says so. What the analyzer got wrong, the
review inherits.

## 2. The attachment shape

`input/drawing_analysis.py` owns the format. The analyzer's output file
(`.txt`, `.md`, or `.json`) is read verbatim — UTF-8, undecodable bytes
replaced rather than refused — stripped of surrounding whitespace, and counted
with the local tokenizer. It becomes one attachment block:

```
--- BEGIN ATTACHMENT: Construction Drawing Digest ---
Drawing analysis file: plans_analysis.txt
<the file's text, verbatim>
--- END ATTACHMENT: Construction Drawing Digest ---
```

Two details are load-bearing.

**The label did not change.** `DIGEST_ATTACHMENT_LABEL` is still
`Construction Drawing Digest`, byte for byte. It is a schema string: the
drawing-impact pass of §5 gates on the exact BEGIN/END marker lines, and a
pending-batch record saved by a build that still digested PDFs carries that
label in its persisted context. Keeping it means such a run, resumed on this
build, still gets its impact pass. A context *file* the operator happened to
name "Construction Drawing Digest.docx" does not match, because the Attach
Files… label carries the extension.

**The first line names the source file.** `Drawing analysis file: <name>` is
what lets the FILES-panel readout of §3 keep one row per attached file. It is
parsed back out by `drawing_analysis_blocks`; a block without it (hand-pasted,
or from an earlier build) is still a valid digest and reads out under a
placeholder name. The line rides into the impact pass as provenance — useful
context for the model, never a citation.

## 3. The token counter

Attaching costs nothing, but every later review, cross-check, and compliance
call carries the text. So the attach step is where the operator learns what
they just added, in three places:

- the **activity log** — `Drawing analysis attached: plans_analysis.txt —
  12,345 tokens (local estimate).`, followed by the new Project Context total
  against the cap;
- the **FILES panel** — a `DRAWING ANALYSIS (in Project Context)` section with
  one row per attached file and its count, and the number of analyses plus
  their combined tokens in the panel header;
- the **Project Context label**, which already shows the running total.

The count is the local tokenizer's, the same one the Project Context label
uses: an estimate, not the provider's billed figure, and the log says so.

The readout is not a record of what was attached. It is a **pure function of
the textbox contents** (`context_attachment.drawing_analysis_readout`),
re-derived after every settled edit through the same debounced handler that
recounts the context. Delete a block by hand and its row disappears; trim one
and its count shrinks. Nothing else remembers the attachment, so the readout
can never disagree with what will actually be sent. Unchanged blocks are not
re-counted — a per-app memo keyed by block text — and the panel is only
re-rendered when the rows change, so typing elsewhere in the textbox costs
nothing.

## 4. Failure policy: refused, named, never truncated

The same rule the Project Context attachment helpers of [**Ch 4 —
Input**](04_input.md) follow. A merge that would exceed the 100k-token cap is
refused with the counts (`plans_analysis.txt is 120,000 tokens. Attaching would
push Project Context to 125,000 tokens, over the 100,000-token limit.`), never
cut to fit. An empty file, a non-text extension, a missing file, and a file
over `MAX_DRAWING_ANALYSIS_BYTES` (8 MiB — far above any plausible analysis;
the guard is for a log or an export picked by mistake, and it refuses before
tokenizing) are each named in one warning, and the usable files in the same
pick still attach.

Silently dropping half an analysis to fit a cap would reintroduce exactly the
invisible-gap problem the old digest's inlined failure markers worked to avoid:
a digest missing sheets looks exactly like a digest of a smaller drawing set.

## 5. Drawing-impact synthesis: making the value visible

The attached analysis puts drawings *into* the review — as plain-text context
on every call — but attributes **nothing** back to them. So the report could
not answer the question an operator will immediately ask: *did attaching the
drawing analysis actually help?*

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
web or search tools — it is synthesis over text the run already holds, so there
is no `pause_turn` loop.

### Output

A `DrawingImpactResult` carries an `impact_level` (`substantial` / `moderate` /
`minimal` / `none`), a plain-text `narrative`, and per-finding `finding_links`,
each classifying the relationship as `corroborated`, `contradicted`, or
`contextualized`, with the digest's own sheet or page references.

### The citation form belongs to the analyzer

The old vision digest minted every citation as `[<file> p.N]`, and the prompt
could demand that form back. The attached analysis is whatever the operator's
program wrote — sheet numbers, page references, section labels — so the prompt
now tells the model to **copy the digest's own references verbatim** and never
to invent one, and to say in the explanation when the digest gives no reference
for a fact. The few-shot examples still use one consistent placeholder form and
say it is only one possible form; mixing a bare sheet number in one example with
a page reference in another would teach the model to emit forms the digest never
used.

### The gate is block presence, not the module flag

The pass runs if and only if `extract_drawing_digest(project_context)` finds a
`Construction Drawing Digest` attachment block — matched on the exact
`wrap_attachment` BEGIN/END marker lines.

Note carefully that this gate is **not** `project_profile_enabled`. An analysis
can be attached under any module, the California default included. Tying drawing
impact to the location-aware flag would have been an easy mistake and would have
denied the feature to the program that has the most users.

A run without an analysis leaves `state.drawing_impact_result` at `None`,
renders no report section and no banner row, and is byte-identical to before —
the same presence-switch discipline as Ch 19.

It runs in both drivers (the GUI collect sequence and
`run_batch_collection_headless`), self-gates identically in each, and reads the
digest from the already-persisted `project_context`. That last detail means
**resume needs no pending-batch schema bump**.

### Guardrails against a flattering lie

This pass has an obvious incentive problem: it is a feature whose job is to
report on the value of a feature. A model asked "how did the drawings help?" will
find a way to say they helped.

Three mechanisms push against that:

1. The prompt **forbids inventing** a reference or a finding connection, and
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

`PHASE_DRAWING_IMPACT` is registered with a 32k output cap, `high` effort, and a
system-plus-tools cache policy; the default model is Sonnet 5.5 via
`SPEC_CRITIC_DRAWING_IMPACT_MODEL`. Registration is not optional housekeeping:
an unregistered phase silently caps at 16k.

## 6. How it renders, and the amber note

The report renders **"How the Drawings Informed This Review"** — impact badge,
narrative, per-finding links — immediately after the methodology note and *above*
the findings it references, plus a conditional "Drawing analysis impact"
Run-Diagnostics row.

When the pass fails, the report renders an **honest amber note**, not a red
error. The wording matters and is worth preserving in any future edit: the
drawings *did* inform the review — the analysis was in Project Context on every
call — only the summary of how is missing. A red "the drawings were ignored"
would be false, and would tell an operator to distrust findings that were in
fact drawing-informed.

`DrawingImpactResult` rides `CollectedBatchState` and `PipelineResult` as an
additive field with a `None` default, read downstream via defensive `getattr`,
so legacy callers and test doubles are unaffected.

## 7. The GUI flow

`context_controller.attach_drawing_analysis` sequences: running-flag guard →
file picker → read + count on a worker thread → merge, refusal, log, and readout
on the Tk thread. Every tkinter mutation is marshaled back through
`app.after(0, ...)`, per the threading discipline of [**Ch 13 — The Desktop
GUI**](13_gui.md). The flow takes no credential and makes no request, so there
is nothing for the close guard or the updater's busy check to wait on; it shares
the attach-files running flag because both flows write the same textbox. The
Expand editor has the same button and attaches into its own textbox; the readout
follows on Save & Close.

Headless callers use `read_drawing_analysis(path)` and
`wrapped_drawing_analysis_block(analysis)`, then pass the merged text to
`start_batch_review(project_context=...)`. Zero pipeline changes — which is the
whole architectural point of this chapter.

## 8. Pins

`tests/test_drawing_analysis.py` covers reading and counting (every supported
extension, the named refusals, undecodable bytes), the block format and the
unchanged label, the impact-pass gate finding the block, the lookalike-file
exclusion, block parsing with and without a source line, and the readout as a
function of the textbox (rows, live counts, hand deletion, the memo).

`tests/test_context_controller_background.py` covers the GUI flow: read and
count off the Tk thread, merge and readout on it, the absence of any key or
cost dialog, the owned refusal with the counts, the shared running flag, the
modal target, and the readout following `do_context_change`.

`tests/test_drawing_impact.py` covers the extraction gate, the strict-subset
schema, the prompt's verbatim-reference instruction and placeholder examples,
parse/coercion/id-drop/dedup, a scripted-client end-to-end, pipeline gating and
finalize carry-through, and report rendering in both the present and absent
states.
