# Spec Critic — Edit Applier

A separate program that takes the `<report>.edits.json` sidecar a Spec Critic
run produces and applies those edit instructions to the specifications they
came from — **as Word tracked changes, in a copy, never in the original.**

```bash
python -m applier path/to/report.edits.json --specs path/to/specs
```

Installed by `pip install .` along with the app, which also registers a
`spec-critic-apply` console script. It is **not** in the frozen Windows build:
`packaging/windows/spec-critic.spec` collects `src` only, so a copy installed
from `SpecCriticSetup.exe` has the GUI and no applier.

---

## Why this is a separate program

Spec Critic used to apply edits. Version 3.0.0 deleted that capability on the
argument that silently rewriting a legal document is the loudest way to hide
uncertainty, and the report has said "emits edit instructions but does not
apply them" ever since.

This program does not reverse that decision. It answers it:

| The v3.0.0 objection | How the applier answers it |
|---|---|
| A silent rewrite hides what changed | Every edit is a **Word tracked change**, attributed, next to the text it replaced. Accept/Reject stays the human gate. |
| The original is gone | The source file is **never** written to. Edits land in `<name>.applied.docx`, and a copy that would overwrite *any* supplied specification is refused before anything is written. |
| A confident tool applies a wrong edit | It **refuses** on anything ambiguous or drifted, and reports it. No guessing. |
| Uncertain findings get applied anyway | Findings a verifier **disputed** or that two verifiers **disagreed** on are withheld from every policy. |

Nothing under `src/` imports this package, and a test fails the build if that
ever changes — Spec Critic still does not apply edits.

---

## What a run looks like

```
$ python -m applier report.edits.json --specs ./specs

Spec Critic — Edit Applier
  sidecar        report.edits.json (schema v4)
  mode           tracked
  policy         conservative
  assist         off

  3 of 6 instructions applied.
  2 unlocated
  1 held by policy

  215000.docx
    -> 215000.applied.docx
    Not applied:
    [HIGH] rf-bbb EDIT
        2 elements contain the target text and the finding's section does not single one out
    [HIGH] rf-eee EDIT
        verification disputed this finding — applying it would write in a change
        the evidence argues against; pass --force-status DISPUTED to override
    [HIGH] rf-fff DELETE
        the target text does not appear in this document; it may have been edited
        since the review that produced this instruction
    Applied:
    [CRITICAL] rf-aaa EDIT @p2
        tracked replacement of 'NFPA 13, 2019 edition' with 'NFPA 13, 2025 edition
        as amended by California'
    ...
```

The unapplied half is printed first, because it is the half that still needs a
person. A JSON receipt lands beside the sidecar as `<stem>.applied.json` for a
CI job or a downstream tool.

---

## Which file an instruction goes to

A sidecar names each specification by file name. Before any document is
opened, the applier binds every name to exactly one of the files you supplied
and decides where each edited copy will go. Anything unsafe is held, never
guessed:

- **Two different files with the same name** (say `projA/spec.docx` and
  `projB/spec.docx`) are `FILE_AMBIGUOUS`, whichever order you pass them in.
  Supply only the one the sidecar was written for. The same file passed twice
  — or reached by two spellings of one path — is not an ambiguity.
- **Names match case-insensitively**, so a sidecar that names both
  `Spec.docx` and `spec.docx` is ambiguous too.
- **A path in the sidecar is not a file name.** A `fileName` with a `/` or `\`
  in it never selects a file (`FILE_MISSING`).
- **An edited copy may not overwrite a supplied file** — its own source,
  another supplied specification, or another document's edited copy — under
  any name: an existing copy that is a hard link to a specification is that
  specification. That is `DESTINATION_CONFLICT`. The common case is a re-run over a folder that still
  holds the last run's `*.applied.docx`, which may have your own review work in
  it by now: move or rename it, or pick another `--output-dir` or
  `--output-suffix`.

A held document holds all of its instructions, each with the reason, and every
other document is still processed. The run then exits `3`, with or without
`--strict`. `--assist` never chooses between files, and `--dry-run` makes the
same decisions as a real run.

---

## How an edit is located

Spec Critic stamps a stable `element_id` on everything it shows the review
model (`p7` for a body paragraph, `t0r2` for a table row, `s1h0` for a header),
and the sidecar records the one each proposal targeted. Locating an edit is
therefore an index lookup, not a search — which is why the deterministic tier
handles nearly everything **at zero cost and with no model call**:

1. **By element id** — the sidecar names one and the element's text still
   confirms the target.
2. **By unique text** — no usable id, but the text occurs exactly once.
3. **By section** — several matches, but only one sits under the finding's own
   section heading.
4. **Refuse** — `AMBIGUOUS` (several indistinguishable matches), `DRIFTED` (the
   id resolves but its text changed), `NOT_FOUND`, or `UNSUPPORTED_ELEMENT`.

The writer refuses one level further down, for the same reason: an element id
names a paragraph or a table row, never *which occurrence inside it*. If the
target text appears more than once across the resolved elements, the edit is
refused rather than applied to the first — which would silently change the
wrong clause, irreversibly under `--mode direct`.

Text is matched whitespace-tolerantly (Word splits a sentence across runs for
reasons that have nothing to do with meaning) but never case- or
wording-tolerantly. "shall" and "should" differ by one letter and by
everything that matters.

---

## Trust-model gating

Every sidecar entry carries the `report_status` Spec Critic assigned it. The
policy decides which of those may be written:

| Policy | Admits |
|---|---|
| `strict` | `VERIFIED_SUPPORTED` — a verifier grounded the claim **as the review model stated it**, so the proposal is the one the verdict is about |
| `conservative` *(default)* | the above, plus `LOCALLY_CLASSIFIED` — deterministic detector hits like a `[SELECT]` placeholder, where a web search adds no signal |
| `all` | the above, plus findings no verdict was reached on (`INSUFFICIENT_EVIDENCE`, `NOT_CHECKED`, `VERIFICATION_FAILED`, `MANUAL_REVIEW_REQUIRED`) |

**Three statuses are excluded from every policy, including `all`.**

`DISPUTED` tells a reviewer to *discard* the finding, and `VERIFIED_CONTESTED`
means the initial and escalated verifiers reached different grounded
conclusions. Writing either in — even as a tracked change — inverts the signal
the trust model exists to send.

`VERIFIED_CONTRADICTED` is excluded for a subtler reason. It is the
`CORRECTED` verdict: the verifier grounded a **correction** to the finding's
claim. `VerificationResult.correction` carries that correction and both report
exporters render it — but **the sidecar does not serialize it**, and nothing
regenerates the proposal after verification. So the `edit_proposal` the
applier receives is still the review model's original, *pre-correction*
wording, and applying it can write in the very text the verifier refuted.

The calibration fixture `tp_dc_corrected_misattributed_amendment` is the
worked example:

> **Proposal:** `"Test fire pumps annually per NFPA 25."` → `"…at the interval required by the provincial fire-code amendment."`
> **Verifier correction:** *"the base standard interval governs — no provincial amendment shortens it"*

The correct action is to leave the clause alone. The sidecar cannot tell the
applier that, because it carries the verdict and not the correction — so until
a proposal is regenerated from the correction, `VERIFIED_CONTRADICTED` is a
*read the report* signal, not an executable instruction.

`--force-status` exists for a reviewer who has read the evidence panel and the
correction line and decided anyway. Nothing else opens that door.

---

## The assist tier (optional, costs money)

`--assist` handles the one case the deterministic tier cannot: several
identical clauses in different articles, where choosing between them is a
judgement about document structure. A bounded tool loop (search, read, choose
or decline; 6 rounds) picks one.

Three constraints keep it inside the trust model:

- **It chooses a location, never content.** There is no tool through which a
  word the model wrote can reach the document — the replacement text always
  comes from the sidecar. The worst a bad answer can do is put a *correct* edit
  in the wrong place, which the tracked-changes default then shows the reviewer
  in Word.
- **A chosen element id is validated** against the elements it was actually
  shown, and the target text must still be present in it. A hallucinated or
  stale id is dropped and the original refusal stands.
- **It never rescues drift.** When the target text is gone, assist may
  *suggest* where the clause seems to have moved — the receipt prints it — but
  the edit stays unapplied. Applying there would mean rewriting an instruction
  to fit text it was not written against.

Off by default. Without it the applier makes no API calls at all.

---

## Options

| Flag | Default | Effect |
|---|---|---|
| `--specs PATH` | the sidecar's directory | A `.docx` or a directory of them. Repeatable. Skips `~$` lock files. |
| `--mode tracked\|direct` | `tracked` | `direct` writes edits in with no revision marks. |
| `--policy strict\|conservative\|all` | `conservative` | See the table above. |
| `--force-status STATUS` | — | Admit a status regardless of policy, including the two excluded ones. Repeatable. |
| `--min-edit-confidence N` | `0.0` (off) | Withhold edits below this rating. Note it is the *review* model's confidence in the edit, recorded before verification ran. |
| `--only FINDING_ID` | — | Apply only these findings. Repeatable. |
| `--dry-run` | off | Report what would be applied; write nothing. Runs the full pipeline including the writer, skipping only the save, so its report matches what a real run does. |
| `--allow-tracked-source` | off | Proceed on a spec that already has pending revisions. Edits whose own target sits inside an undecided revision are still refused. |
| `--assist` | off | Enable the assist tier. Costs money. |
| `--assist-model` | Sonnet 5 | Model for `--assist`. |
| `--output-dir PATH` | beside each source | Where edited copies go. A copy that would overwrite a supplied file is refused. |
| `--output-suffix S` | `.applied` | Suffix for edited copies. |
| `--receipt PATH` | `<sidecar-stem>.applied.json` | Where the JSON receipt goes. |
| `--strict` | off | Exit `2` when anything could not be applied (for CI). Policy holds do not count. |

Exit codes: `0` ran, `1` fatal error, `2` `--strict` with unapplied instructions,
`3` a document was held because its name matched several supplied files or its
copy would overwrite a supplied file (with or without `--strict`).

---

## What it does not do

- **Text boxes, footnotes, endnotes.** Spec Critic *extracts* their text so a
  requirement authored there is still reviewed, but python-docx does not model
  them as editable containers. Those edits are reported for hand application.
- **A run that must be split at a tab or a line break.** Refused rather than
  rebuilt — a silently mangled tab stop in a spec table is noticed three
  revisions later.
- **Text inside someone else's pending revision.** Accept or reject theirs
  first.
- **A target that appears more than once inside its own element.** The
  sidecar does not record which occurrence was meant.
- **Anything to the source file.** Ever.

---

## Accounting

The receipt accounts for **every** entry the sidecar listed — applied, held,
unlocated, malformed, in a file that was not supplied, or in a file held as
`FILE_AMBIGUOUS` (its entry lists the `candidate_paths`) or
`DESTINATION_CONFLICT` — and reports `balanced: true` when the count out equals
the count in. An applier that
quietly processes 19 of 23 instructions is worse than one that fails, because
the missing four look like clean specifications.
