# The Trust Model & Report Output: Status Labels, the Word Report & the Edit Sidecar

Every subsystem in the chapters before this one exists to learn something about a
finding. Extraction learns what the spec actually says. The deterministic
pre-screen learns that a `TODO:` was left in section 2.3. The review pass learns
that a fire-pump reference cites the wrong NFPA edition. Verification learns
whether a real, retrievable source backs that claim — and whether two different
models reading the same sources agree. All of that learning is worthless if it
can't be handed to a human in a form they can *trust at a glance*.

This chapter is where the learning becomes an artifact. It owns the two closed
vocabularies the program uses to declare how much it believes each finding — the
**nine `ReportStatus` labels** and the **two `EditActionLabel` values** — and the
two files those vocabularies are rendered into: the human-readable **Word
report** (`report_exporter.py`) and the machine-readable **edit sidecar**
(`edit_sidecar.py`). The status logic itself lives in a small, dependency-free
module, `report_status.py`, that nothing in the pipeline can avoid.

The throughline of the whole handbook lands hardest here. Spec Critic is a
compliance tool, and a compliance tool that is *confidently wrong* about a
building code is worse than no tool at all. The report is the surface where that
conviction either holds or fails. So the design rule for this layer is blunt:
**make uncertainty visible rather than hidden.** A finding the verifier confirmed
with a grounded citation must look different — different glyph, different color,
different words — from one it couldn't ground, from one where two models
disagreed, and from one where the verifier simply crashed. The reader should
never have to *guess* how much to believe a row.

## The trust model: nine ways to believe a finding

`report_status.py` defines two `str`-backed enums and the pure functions that map
a `Finding` onto exactly one value of each. The crucial architectural decision is
stated in the module's own docstring: both labels are **derived, not stored**.
Nothing is written onto the `Finding` to mark it "verified" or "edit-eligible."
The labels are computed at render time from fields that already exist — the
finding's `verification` result and its `edit_proposal`. There is no new
persistence column, no migration, no opportunity for a stored label to drift out
of sync with the evidence it was supposed to summarize. Ask the question fresh,
every time, from the underlying facts.

### The nine statuses

`classify_status(finding)` assigns one of nine `ReportStatus` values. Each carries
a glyph (for inline display), a color (which doubles as the summary-table cell
shading), and a human-readable label. The full closed set:

| `ReportStatus` | Glyph | Color | When it fires |
|---|:---:|---|---|
| `VERIFIED_SUPPORTED` | ✓ | Green `008000` | Verdict `CONFIRMED`, grounded, with at least one accepted citation |
| `VERIFIED_CONTRADICTED` | ✎ | Amber `CC8400` | Verdict `CORRECTED`, grounded, with at least one accepted citation |
| `VERIFIED_CONTESTED` | ⚡ | Purple `800080` | `models_disagreed` — initial and escalated verifiers *both* grounded a verdict and *disagreed* |
| `DISPUTED` | ✗ | Red `C00000` | Verdict explicitly `DISPUTED`, or a grounding downgrade |
| `INSUFFICIENT_EVIDENCE` | ? | Gray `808080` | `UNVERIFIED` with no contradictory citation; the verifier ran cleanly but couldn't ground a claim |
| `LOCALLY_CLASSIFIED` | ◆ | Blue `3B82F6` | `cache_status == "local_skip"` — resolved by a deterministic detector, keyword classifier, or Haiku triage |
| `VERIFICATION_FAILED` | ⚠ | Firebrick `B22222` | `verification_failed` sentinel — the verifier hit a transient operational error (rate limit, server error, network, parse error, batch cancellation) |
| `NOT_CHECKED` | — | Dark gray `646464` | No `verification` on the finding at all |
| `MANUAL_REVIEW_REQUIRED` | ! | Orange `FF6600` | Reserved for precondition / parser failures; **no current producer** in `classify_status` |

The reader should internalize the *spirit* of these nine before the mechanics.
They span a deliberate spectrum of trust:

```
   believe it ───────────────────────────────────────────► look yourself
   │                       │                    │                       │
   ✓ VERIFIED_SUPPORTED     ⚡ VERIFIED_CONTESTED  — NOT_CHECKED
   ✎ VERIFIED_CONTRADICTED  ✗ DISPUTED            ⚠ VERIFICATION_FAILED
   ◆ LOCALLY_CLASSIFIED     ? INSUFFICIENT_EVID.   ! MANUAL_REVIEW_REQUIRED
   ────────────────────     ──────────────────    ─────────────────────
   grounded / deterministic the system is         no verdict to trust —
   — act with confidence    flagging doubt        the *run*, not the claim
```

The four leftmost statuses are the ones a reviewer can act on directly:
`VERIFIED_SUPPORTED` and `VERIFIED_CONTRADICTED` carry a real, retrieved source
behind them; `LOCALLY_CLASSIFIED` is a deterministic fact ("there is literally a
`TODO:` in the text") that needed no web search to confirm. The middle band is
where the program is *explicitly telling the reviewer to be careful*:
`VERIFIED_CONTESTED`, `DISPUTED`, and `INSUFFICIENT_EVIDENCE` each mean "we did
the work and the answer is doubt." The rightmost band is not about the claim at
all — it is about the run: `NOT_CHECKED` never reached the verifier,
`VERIFICATION_FAILED` broke on the way, and `MANUAL_REVIEW_REQUIRED` is a reserved
slot for parser/precondition failures that no path currently produces (an honest
gap, noted below).

That a tool would invent *three separate* ways to say "I'm not sure" —
contested, insufficient, failed — is the whole design philosophy in miniature. A
naive tool collapses all doubt into one bucket and renders it the same as
silence. Spec Critic insists the reviewer can tell *which kind* of doubt they're
looking at, because the remedy differs: a contested finding needs a human
adjudicator, an insufficient one needs more search budget or more evidence, and a
failed one just needs a re-run.

### The branch order is load-bearing

`classify_status` is a priority cascade — first match wins — and the *order* of
the branches encodes trust policy that would be wrong in any other arrangement:

```
1. no verification?            → NOT_CHECKED
2. verification_failed sentinel? → VERIFICATION_FAILED
3. models_disagreed sentinel?  → VERIFIED_CONTESTED      ← before the verdict branches
4. cache_status == local_skip? → LOCALLY_CLASSIFIED
5. CONFIRMED + grounded + cited? → VERIFIED_SUPPORTED
6. CORRECTED + grounded + cited? → VERIFIED_CONTRADICTED
7. verdict == DISPUTED?        → DISPUTED
8. everything else             → INSUFFICIENT_EVIDENCE
```

The two sentinel checks (failure, then disagreement) sit *above* the verdict
branches on purpose. The disagreement check is the subtle one. When verification
escalates a finding from Sonnet to Opus and the stronger model produces a grounded
`CONFIRMED`, the finding's `verdict` field now *looks* like a clean supported
result — and rules 5–6 would happily label it `VERIFIED_SUPPORTED`, hiding the
fact that the first model had grounded a *different* verdict on the same sources.
Placing the `models_disagreed` short-circuit at step 3 means a swapped-in,
grounded, CONFIRMED final verdict still renders as `VERIFIED_CONTESTED`. **The
disagreement itself is the quality signal**, more important than the headline
verdict, so it wins. (The production verifier only sets `models_disagreed` when
both passes grounded *and* differed — see [**Ch 10 — Verification II: How We Check
& Judge**](10_verification_grounding.md) for how that flag is computed; this layer only reads and honors it.)

Rules 5 and 6 carry one more piece of belt-and-suspenders: the explicit
`has_accepted` check. The grounding invariant (Ch 10) already downgrades a
source-less CONFIRMED to UNVERIFIED inside the verifier, so in production a
verdict reaching rule 5 *should* always have an accepted citation. The duplicate
check here exists for the call site that bypasses the verifier wrapper — a future
caller, or a unit test that constructs a `VerificationResult` by hand. The report
must never be the place where "Verified — supported" appears next to a finding
with no real source behind it, so the classifier refuses to emit that label
without a citation, independent of what the verifier promised.

### Budget exhausted is a sub-label, not a status

There is a tempting tenth status that the code deliberately does *not* create.
When a verifier spends its entire severity-scaled search budget — eight searches
for a CRITICAL finding, three for a GRIPE — and still can't ground a verdict, that
is a meaningfully different situation from "the verifier gave up after two
searches." The first is actionable: an operator could re-run the finding at a
higher severity to buy more search headroom. `report_status.is_budget_exhausted()`
surfaces exactly that, reading the `budget_exhausted` flag the verifier stamps.

But it stays a *sub-label*. `classify_status` still returns
`INSUFFICIENT_EVIDENCE` for an exhausted finding, because **the trust level is
identical** — an ungrounded UNVERIFIED is an ungrounded UNVERIFIED whether the
budget ran out at search two or search eight. Inventing a top-level status would
have implied a different degree of belief, which would be a lie. So the report
renders the status as `INSUFFICIENT_EVIDENCE` and appends an italic
" (search budget exhausted)" to the status line, colored to match so it reads as
part of the badge rather than a competing field. The distinction is preserved as
an *enrichment* (and as a Run Diagnostics banner row), never as a change in trust.

### The two edit-action labels

The second vocabulary answers a different question — not "how much do I believe
this?" but "is there a concrete edit attached?" `EditActionLabel` has two values:

| `EditActionLabel` | Label | When |
|---|---|---|
| `EDIT_SUGGESTED` | "Edit suggested" | The finding carries a structured edit proposal |
| `REPORT_ONLY` | "Report only" | No proposal — a coordination claim, an interpretation, a multi-paragraph rewrite |

`classify_edit_action` is now almost trivial: ask the finding for its edit
proposal via `as_edit_proposal()`; `None` means `REPORT_ONLY`, anything else means
`EDIT_SUGGESTED`. That simplicity is itself a design statement, and one worth
dwelling on because earlier versions of this code were far more elaborate. There
used to be a confidence gate, a supportive-status filter, a numeric/standards
demotion — machinery whose only job was to decide whether an edit was safe to
*auto-apply*. The v3.0.0 pivot removed the entire surgical write-back stack (Ch 17
tells that story). Spec Critic now **emits edit instructions and never applies
them.** Once nothing is auto-applied, the question "is this edit safe enough to
apply?" stops being this layer's problem. The only question left is "is there an
edit to hand downstream?" — and that's a one-line check.

The trust information didn't disappear; it *rides along*. A finding's
verification status and its `edit_confidence` are both carried into the report and
the sidecar so that a downstream applier — a future, separate program — can do its
own gating with full information. This layer's job is to emit honestly and label
clearly, not to decide on the applier's behalf.

## Anatomy of the Word report

`export_report(pipeline_result, output_path)` assembles the `.docx` top to bottom.
Reading the function is the fastest way to understand the document, because the
report *is* the call order:

```
┌─────────────────────────────────────────────────────────┐
│ TITLE BLOCK         Spec Critic — M&P Review Report       │  _write_title_block
│                     Generated · Model · Files · Cycle     │
├─────────────────────────────────────────────────────────┤
│ RUN DIAGNOSTICS     "did anything operationally bad       │  _write_run_diagnostics_banner
│   (banner)           happen on this run?"                  │
├─────────────────────────────────────────────────────────┤
│ FILES REVIEWED      bullet list of submitted specs        │  _write_files_reviewed
├─────────────────────────────────────────────────────────┤
│ ABOUT THIS REVIEW   methodology + pinned-editions note    │  _write_methodology_note
├─────────────────────────────────────────────────────────┤
│ SUMMARY             severity grid · tokens · time ·       │  _write_summary_table
│                     verdict breakdown                      │
├─────────────────────────────────────────────────────────┤
│ TRUST MODEL SUMMARY status histogram · edit eligibility   │  _write_trust_model_summary
├─────────────────────────────────────────────────────────┤
│ ALERTS              deterministic-check sections          │  _write_alerts
├─────────────────────────────────────────────────────────┤
│ FINDINGS            per-severity, collapsible entries     │  _write_findings_section
│                     └─ each: status line · issue ·         │   └ _write_finding_entry
│                        proposed edit · Sources panel       │      └ _write_evidence_panel
├─────────────────────────────────────────────────────────┤
│ CROSS-SPEC COORD.   coordination findings + narrative     │  _write_cross_check_section
└─────────────────────────────────────────────────────────┘
```

The ordering is itself a trust argument. The two operational/aggregate sections —
the **Run Diagnostics banner** and the **Trust Model Summary** — are deliberately
hoisted near the top, before the reader reaches a single finding. The reader
learns *whether the run was healthy* and *how much of it is actually trustworthy*
before they start reading claims they might otherwise take at face value.

A few sections deserve more than their place in the diagram.

**Title block & methodology note.** The title block (`_write_title_block`) prints
four metadata lines: generation timestamp, the review model, "Files Reviewed: N,"
and the code cycle. The "About This Review" note (`_write_methodology_note`)
explains in prose how the review was produced, adapts its second paragraph to
whether verification ran fully, partially, or not at all, and — importantly —
ends every report with the line that this is *advisory* and findings "should be
reviewed by the engineer of record before acting on them." A compliance tool says
this out loud. The note also embeds the **pinned-editions enumeration**
(`_render_pinned_editions_note`): a one-paragraph list of exactly which
NFPA/ASHRAE/IAPMO/UL editions the verifier treated as authoritative for the cycle
(for `CALIFORNIA_2025`: NFPA 13 "2025, as amended by California," NFPA 25 "2013
California Edition," ASHRAE 90.1 "2019," and so on — see [**Ch 12 — Configuration, Models & Token Economics**](12_configuration_and_models.md) for
where those strings live). Empty edition fields are silently dropped, so a future cycle that hasn't
populated them degrades to a shorter note rather than printing blanks.

**Summary table.** `_write_summary_table` renders the familiar severity grid
(CRITICAL/HIGH/MEDIUM/GRIPES/TOTAL, plus a cross-check column when present),
review-stage token usage, processing time, and a verdict breakdown. One detail
captures the trust ethos: when the raw `UNVERIFIED` *verdict* count is non-zero,
the table adds an italic note breaking that bucket down by trust *status* — using
the same labels as the Trust Model Summary — because the raw UNVERIFIED count
lumps locally-classified findings (placeholders, stale cycles) together with
genuinely unverifiable ones and would overstate "unverified." The report refuses
to let two different numbers ("N unverified" here, "M insufficient evidence"
there) leave the reader wondering which is real.

**Trust Model Summary.** `_write_trust_model_summary` renders the
status histogram as a colored table (one column per status that actually
occurred, in `STATUS_DISPLAY_ORDER` — supportive buckets first, the operational
tail last) and the edit-action histogram as a compact inline "Edit eligibility"
line. The severity table answers *how many issues are critical?*; this one answers
*how many of them are actually trustworthy?*

**Alerts.** `_write_alerts` renders the deterministic pre-screen output, every
sub-section explicitly suffixed "(deterministic check)" so the reader can tell at
a glance which items came from local rules versus the model. These are the
`leed_reference` / `placeholder` / `template_marker` / stale-cycle / structural /
duplicate / naming alerts owned by [**Ch 4 — Input: Extraction, Element IDs & the
Deterministic Pre-Screen**](04_input.md); this layer only lays them out.

## The Run Diagnostics banner

The banner (`_summarize_run_diagnostics` computes it, `_write_run_diagnostics_banner`
renders it) is the report's operational vital-signs panel. It answers one
question for a reviewer who has not been watching the run: *did anything
operationally bad happen?* It is a two-column `label | value` table, rendered
right after the title so it can't be missed, with rows:

| Row | Source | Red when |
|---|---|---|
| Edit suggested | `EDIT_SUGGESTED` count | — |
| Report-only | `REPORT_ONLY` count | — |
| Cache replays | findings with `cache_status == "hit"` (+ oldest age in days) | — |
| Verification failures (operational) | `VERIFICATION_FAILED` count | **> 0** |
| REPORT_ONLY demotions at parse time | findings with a `demotion_reason` | — |
| Spec content extraction warnings | specs with non-empty `extraction_warnings` | **> 0** |
| Budget-exhausted findings | `summarize_budget_exhausted` | **> 0** |
| Cross-spec coordination | cross-check status / finding count | skipped/failed |

Every value derives from data already present on the findings, the status
histogram, or the pipeline result — **no new persistence.** The cache-replay row,
for instance, walks the findings, counts cache hits, and tracks the oldest
entry's age so the reviewer sees the staleness picture without expanding a single
finding. The extraction-warning row counts *affected specs, not warnings* — a
single spec with three drawing-heavy sections counts once, because the
"verify visually" prompt is one-per-document anyway (the underlying content-loss
check is owned by Ch 4). The demotion row surfaces findings where the model
claimed an EDIT/ADD/DELETE but omitted a required field and the parser demoted it
to REPORT_ONLY — a model-output-shape signal distinct from a deliberate
report-only finding.

Two of the rows earn a **recovery-hint paragraph** below the table, and the fact
that there are *two distinct hints* is itself a teaching moment about honest
uncertainty:

- The **failure hint** (firebrick, when `VERIFICATION_FAILED > 0`) explains that
  these findings broke on transient errors, are marked with the ⚠ glyph below, and
  that *re-running will re-attempt them* — because the cache deliberately refuses
  to persist operational failures, so a re-run sees them fresh.
- The **budget-exhaustion hint** (a calmer amber, when budget-exhausted `> 0`)
  explains the opposite: this is *not* transient. Re-running at the same severity
  will exhaust the same budget. The actionable remedy is to raise the finding's
  severity, and the hint names the per-severity budgets — CRITICAL 8, HIGH 7,
  MEDIUM 5, GRIPES 3 — by reading them live from `api_config` via
  `web_search_max_uses_for_severity`, so the hint can never drift from the policy
  it describes.

Same red-ish family, two different colors, two different remedies, because the
*cause* differs. The banner refuses to lump "the verifier crashed" together with
"the verifier worked but ran out of rope."

A clarification the chapter prompt insists on: this Run Diagnostics *banner* — a
section of the Word report — is **not** the same thing as the in-memory
`DiagnosticsReport` (`orchestration/diagnostics.py`), which is the operational ops
report owned by [**Ch 14 — Observability: Tracing & Diagnostics**](14_observability.md). They share a
spirit ("surface operational health") but live in different worlds: one is a few
rows of a printed artifact derived from finding fields; the other is a structured
in-memory object threaded through the run. Don't confuse them.

## The per-finding evidence panel

Each finding is a collapsible block (`_write_finding_entry`), built on Word's
native heading-collapse so a reviewer can fold away a single finding or an entire
severity group with no macros. Within each severity group, findings are ordered
so one spec file's issues stay contiguous (sorted lexically by filename, which is
CSI-section order) and then by descending confidence — a change from the prior
confidence-only sort that scattered a single spec's findings across the whole
group. The header line carries the index, severity badge,
confidence percentage, filename, and section. Immediately beneath it — *before*
the issue text — comes the **status line**, because the trust-model verdict should
be the first thing the eye lands on:

```
Status: ✓ Verified — supported  •  Edit: Edit suggested  •  Cache replay — 12d old
```

That single line packs the status glyph and label (colored by status), the
budget-exhausted sub-label when it applies, the edit-action label, and — when the
verdict came from a cache hit — an inline **cache-replay age badge** colored by
tier: amber under 30 days, orange 30–90, red over 90. The badge lets a reviewer
spot a stale verdict without expanding anything; its age is read from
`cache_entry_created_ts`, and it is suppressed for legacy payloads with no recorded
timestamp and for clock-skew cases where the timestamp is in the future.

Below the status line: the issue, then the **edit block**. A REPORT_ONLY finding
renders an explicit "Action: REPORT_ONLY" plus an italic note — and if it was
demoted at parse time, the note names the *specific* missing field ("the model
claimed EDIT but no existingText was provided") rather than the generic
coordination explanation. A finding with a proposal renders "Action:
\<type\>", then the **inline proposed edit**: "Spec evidence:" (the existing text,
in red) and "Proposed replacement:" (the new text, in green). This is the
human-readable face of the edit; its machine-readable twin goes to the sidecar.
Then the code reference (blue) and the verification verdict line.

Finally, collapsed by default under a "Sources" Heading 4, comes the **evidence
panel** (`_write_evidence_panel`) — the audit trail that answers "*why* did the
verifier reach this verdict?" without the reviewer leaving the report. Its
contents render in a fixed order:

1. **Verifier model** (e.g. Sonnet 5 / Opus 4.8 / local).
2. **Verification mode** in human-readable form (Local skip / Strict structured /
   Standard reasoning / Deep reasoning — the routing dimension owned by [**Ch 9 —
   Verification I: How We Decide to Check**](09_verification_routing.md)).
3. **Search budget used** — "N of M searches used," or, when the verifier pulled
   full pages, "Searches: N of M, Full-page fetches: K." Suppressed for
   `local_skip`, where "0 of N searches" would be misleading by design.
4. **Source quote** — the verbatim snippet the verifier relied on, as an indented
   italic blockquote so it reads as source content, not commentary.
5. **Verifier rationale** — the model's explanation, placed next to the quote it
   leans on.
6. **Escalation history** — when escalation was attempted, an
   "Initial verdict … → Final verdict …" sentence. Its color *is* the signal:
   purple (matching `VERIFIED_CONTESTED`) when the models genuinely disagreed,
   firebrick when escalation merely changed the verdict, gray when neither. In the
   contested case it appends an explicit "the two models disagreed … manual review
   recommended" sentence (so the panel stays self-explanatory even read in
   isolation) and adds an **"Initial verifier sources"** sub-section listing the
   first model's citations, so a reviewer can compare Sonnet's sources here against
   Opus's sources below, side by side.
7. **Accepted source URLs** ("Web/code evidence (cited and found in search
   results)") — green-labeled, blue links.
8. **Rejected source URLs** — cited by the model but *not* present in the search
   results, with the rejection reason. Surfacing rejected sources is a quiet act
   of integrity: it shows the reviewer where the model reached for a citation the
   grounding gate refused to accept.
9. **Full-text sources consulted** — URLs pulled in full via `web_fetch`, in their
   own sub-section so skimmed snippets and deep reads stay visually distinct.
10. **Force-refresh hint** — for cache-hit results only, the exact on-disk cache
    path to delete if the reviewer wants fresh verification.

The order is not decorative. Model → mode → budget establishes *how the verdict
was reached*; quote → rationale gives *the reasoning and its support*; escalation
→ accepted → rejected → fetched gives *the full source picture, including what was
thrown out.* A reviewer who reads top-to-bottom reconstructs the verifier's whole
decision.

## The edit sidecar: emit, don't apply

The sidecar is the concrete realization of the emit-but-don't-apply contract.
After the `.docx` is written, `write_edit_instructions_sidecar` drops a companion
JSON file beside it at `<report-stem>.edits.json`. Where the Word report renders
edits for a *human*, the sidecar serializes them for a *machine* — a future,
separate applier program that will read this file and do the actual document
mutation Spec Critic refuses to do itself.

The structure is deliberately flat and boring, which is what a machine contract
should be. The top level carries a `schema_version` (currently 2), a
`generated_at` timestamp, the report filename, the cycle label, an `edit_count`,
and the `edits` array. Each entry is one finding that **carries an edit proposal**
— REPORT_ONLY findings produce no entry at all, because there is nothing to apply.
A trimmed entry:

```json
{
  "finding_id": "rf-3f9a2b7c4d1e",
  "fileName": "230500_Common_Work_Results_HVAC.docx",
  "section": "2.3 PIPE MATERIALS",
  "severity": "HIGH",
  "issue": "References NFPA 13 (2019) but the 2025 cycle adopts the 2022 edition.",
  "codeReference": "NFPA 13",
  "evidenceElementId": "p47",
  "verification_verdict": "CORRECTED",
  "report_status": "VERIFIED_CONTRADICTED",
  "edit_proposal": {
    "action_type": "EDIT",
    "existing_text": "NFPA 13 (2019)",
    "replacement_text": "NFPA 13 (2022)",
    "anchor_text": null,
    "insert_position": null,
    "target_element_id": "p47",
    "edit_confidence": 0.9
  }
}
```

`_serialize_edit_proposal` is the flattener that produces the `edit_proposal`
object, mirroring the `EditProposal` dataclass field-for-field
(`action_type` / `existing_text` / `replacement_text` / `anchor_text` /
`insert_position` / `target_element_id` / `edit_confidence`; the dataclass itself
is owned by [**Ch 5 — The Review Engine**](05_review_engine.md)). The two trust fields carried alongside
— `verification_verdict` and `report_status` — are the "ride-along" data the
applier gates on: it can decide, for example, to skip any entry whose
`report_status` is `VERIFIED_CONTESTED` and require a human, while applying
`VERIFIED_SUPPORTED` corrections automatically. Spec Critic emits the information
and the recommendation; it does not make that call.

This is the cleanest expression of the v3.0.0 stance. The program's caution is
encoded not in *withholding* edits but in *labeling* them honestly and handing
both the edit and its trust context to whoever applies it.

## The honest edges

The chapter template asks every author to be candid about what's still being
perfected, and this layer has two findings the audits flag as the report's most
important unfinished work. Both are *surfacing* gaps — the spine already has the
data; the report layer just doesn't render it yet — which is exactly the kind of
quiet truthfulness failure a trust-first tool should hate most.

**STRUCTURAL_AUDIT P0-1 — a partially-failed run can look clean.** When a spec
fails review (it truncated, parse-errored, or returned nothing), the data layer is
honest about it: the failure lands in `errors` and `truncated_specs`, and
`combined.error` reads "N spec(s) had errors." But the report never surfaces it.
The title block prints "Files Reviewed: 5" from `len(files_reviewed)`, which
counts *submitted* specs — including the two that produced nothing. The Run
Diagnostics banner has a `verification_failed` row but **no "specs that failed
review" row**, and `report_exporter.py` has zero references to `truncated_specs`.
The consequence is precisely the failure mode this handbook exists to prevent: a
spec that *failed* review (0 findings because it never ran) is indistinguishable
from a clean spec (0 findings) in the exported `.docx`. "We reviewed all 5 and
they're clean" and "2 of 5 silently failed" render identically. The fix is purely
additive — a banner row fed by `truncated_specs`, highlighted red when > 0, and a
"reviewed/submitted" count — and it belongs here in `_summarize_run_diagnostics`,
but the data it needs is produced upstream in the spine ([**Ch 7 — Orchestration &
State: The Pipeline Spine**](07_orchestration.md)). It is the headline finding of the structural audit
([**Ch 16 — Trust Under the Microscope: The Audits**](16_trust_under_the_microscope.md)).

**TRUST_AUDIT P0-1 — the sidecar under-emits for multi-file defects.** When dedup
collapses the same defect across N specs — common for templated DSA master
specs — the merged `Finding` carries `affected_files=[a, b, c]` and per-file
`occurrence_originals`. But the sidecar emits **one entry**, with `fileName` set to
the representative file only, and *does not include `affected_files` at all*. A
downstream applier reading the sidecar fixes file `a` and never learns the
identical defect exists in `b` and `c` — only the human-readable `issue` string
("found in 3 specs") records it. For a tool whose entire premise is feeding an
automated applier, that is silent under-application. The machinery to fan this out
correctly — `group_findings()` / `FindingOccurrence.executable_finding()` —
already *exists*, but is called only from tests, never wired into the sidecar.
CLAUDE.md even claims the per-file originals survive the merge "for the report and
the edit-instruction sidecar," and that intent is simply not yet realized in code.
The fix (emit one entry per affected file, or at minimum include `affected_files`)
straddles this chapter and the dedup spine in [**Ch 7**](07_orchestration.md).

Two smaller edges round out the honesty ledger. The `MANUAL_REVIEW_REQUIRED`
status is a real enum value with a glyph and a color, but `classify_status` has
**no branch that produces it** — it's a reserved slot for precondition/parser
failures with no current producer, which a reader scanning the nine-status table
should know. And cross-check (coordination) findings flow into the sidecar with an
empty `finding_id`, because id-stamping happens only inside dedup and coordination
findings are appended afterward — a traceability gap for any applier that keys
edits by id (a P1 finding; see Ch 7 and Ch 16).

None of these are render *crashes* — the report and sidecar are produced
correctly and completely for what they choose to show. They are gaps in *what gets
shown*, which for a trust tool is the more insidious kind: a missing red row is
quieter, and therefore more dangerous, than a stack trace.

## How it connects

This chapter is the downstream terminus of almost everything. It **renders** the
`VerificationResult` whose verdict, grounding, and telemetry are *produced* in
[**Ch 10 — Verification II**](10_verification_grounding.md) — classification and display only; none of the
judgment happens here. It shows the routing **mode** chosen in [**Ch 9 —
Verification I**](09_verification_routing.md) in each evidence panel. It lays out the deterministic **alerts**
and consumes the content-loss **extraction warnings** from [**Ch 4 — Input**](04_input.md). It
renders the `Finding` / `EditProposal` data model defined in [**Ch 5 — The Review
Engine**](05_review_engine.md), and depends on the dedup / multi-file grouping in [**Ch 7 —
Orchestration & State**](07_orchestration.md), where the spine-side fix for the sidecar's multi-file
fan-out (and the failed-spec data) lives. The pinned-editions note pulls cycle
data owned by [**Ch 12 — Configuration, Models & Token Economics**](12_configuration_and_models.md). The Run
Diagnostics *banner* described here should not be confused with the in-memory
`DiagnosticsReport` of [**Ch 14 — Observability: Tracing & Diagnostics**](14_observability.md). And the
two P0-1 audit findings are examined in full in [**Ch 16 — Trust Under the
Microscope: The Audits**](16_trust_under_the_microscope.md).

## Key takeaways

- **The artifact is the product.** Everything upstream exists to produce a report
  and sidecar a human can trust at a glance; this layer is where the program's
  caution becomes visible.
- **Two closed vocabularies, both derived not stored.** Nine `ReportStatus` labels
  (how much to believe a finding) and two `EditActionLabel` values (is there an
  edit attached), computed fresh from `verification` and `edit_proposal` so a label
  can never drift from its evidence.
- **Branch order is policy.** `classify_status` checks the failure and
  disagreement sentinels before the verdict branches, so a swapped-in grounded
  CONFIRMED still reads as `VERIFIED_CONTESTED` — the disagreement outranks the
  headline verdict.
- **Three distinct flavors of doubt.** `VERIFIED_CONTESTED`,
  `INSUFFICIENT_EVIDENCE`, and `VERIFICATION_FAILED` are kept separate because
  their remedies differ; budget exhaustion stays a sub-label, not a tenth status,
  because the trust level is unchanged.
- **`classify_edit_action` is trivial by design.** Once the program emits but
  never applies edits, "is it safe to apply?" stops being this layer's question;
  verification status and `edit_confidence` ride along for a downstream applier.
- **The Word report front-loads trust.** Run Diagnostics banner and Trust Model
  Summary appear before any finding; each finding leads with its status line and
  carries a collapsed evidence panel reconstructing the verifier's reasoning.
- **The sidecar is emit-but-don't-apply made concrete** — one flattened entry per
  edit proposal, with trust context attached, for a future applier.
- **The honest edges are surfacing gaps.** A partially-failed run can read as clean
  (STRUCTURAL P0-1) and the sidecar under-emits for multi-file defects (TRUST
  P0-1); both have the data upstream and await a rendering fix — the report layer's
  most important unfinished work.
