# Input: Extraction, Element IDs & the Deterministic Pre-Screen

Every finding the system will ever produce begins as a paragraph in a Word file
that someone, somewhere, edited under deadline. Before Claude reads a single
token, that file has to be turned into text the model can reason about — and a
surprising amount of what a reviewer needs to catch can be caught *right here*,
on the local machine, for free, with perfect reliability. This chapter is about
the front door of the pipeline: how `.docx` files become structured, reviewable
text with stable addresses, and how a layer of **deterministic, no-API
detectors** sweeps up an entire class of defects before any money or latency is
spent on the model.

The chapter is also the first concrete expression of a philosophy that runs
through the whole codebase: **do the cheap, certain checks locally; save the
model for the judgment calls.** You should never pay a language model to find a
literal `TODO:`. A regex finds it instantly, for nothing, and is never wrong
about it. The deterministic pre-screen exists precisely so the expensive,
probabilistic part of the system — the review, the verification, the grounding —
is reserved for the questions that genuinely require judgment: *is this code
citation actually wrong? does this requirement contradict another spec?* Those
are model questions. "Is there an unresolved `[SELECT]` placeholder in section
2.01" is not.

Three small files own this front door. `input/extractor.py` turns bytes into an
`ExtractedSpec`. `input/extraction_cache.py` makes sure a file is never parsed
twice and never served stale. `input/preprocessor.py` runs the deterministic
detectors. Between them they define what the model *sees* — and, just as
importantly, what it does **not** see, which is where the honest edges of this
chapter live.

```
   one .docx                                       one project (N specs)
       │                                                    │
       ▼                                                    ▼
  ┌───────────────────────┐                      ┌─────────────────────────┐
  │ extractor.py           │   cached by         │ preprocessor.py          │
  │ extract_text_from_docx │◄── extraction_cache │ preprocess_spec          │
  └───────────┬───────────┘   (mtime+fingerprint)└────────────┬────────────┘
              │                                                │
              ▼                                                ▼
      ExtractedSpec                                    PreprocessResult
      ├─ content (flattened text)                      ├─ leed_alerts
      ├─ paragraph_map  [ParagraphMapping]             ├─ placeholder_alerts
      │   └─ element_id: p7 / t0r2 / s1h0              ├─ code_cycle_alerts
      ├─ document_id                                   ├─ structural_alerts
      ├─ word_count                                    ├─ template_marker_alerts
      └─ extraction_warnings ──► Run Diagnostics       ├─ invalid_code_cycle_alerts
                                  banner (Ch 11)        └─ duplicate_paragraph_alerts
```

`ExtractedSpec` and `PreprocessResult` were both introduced as part of the data
model in [**Ch 2 — Architecture at a Glance**](02_architecture.md); this chapter owns their full
detail. Everything downstream — the review prompt ([**Ch 5 — The Review
Engine**](05_review_engine.md)), the routing decisions ([**Ch 9 — Verification I**](09_verification_routing.md)), the report
([**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md)) — consumes what these three files
produce.

## From `.docx` to `ExtractedSpec`

A `.docx` is a zip archive of XML parts. python-docx parses it into a document
object, and `extract_text_from_docx` walks the parts that carry reviewable text.
The walk is deliberately narrow and ordered. It iterates the direct children of
the document body in document order, and for each child it asks only two
questions: *is this a paragraph (`}p`)?* or *is this a table (`}tbl`)?* A
paragraph's stripped text, if non-empty, becomes one entry. A table is flattened
row by row, with each row's non-empty cells joined by ` | ` into a single line.
Empty paragraphs and empty cells are dropped — they carry no requirement and
would only dilute the token budget.

After the body, a **second pass** walks `doc.sections` and pulls text from each
section's header and footer paragraphs, prefixing each with `[Header]` or
`[Footer]` so the model can tell chrome from body. This pass matters more than it
looks: DSA specs sometimes park a project name, a revision note, or even a stray
requirement in a running header. (The pass is also where one of this chapter's
honest edges hides — it captures header/footer *paragraphs* but not tables or
text boxes inside them; more on that below.) When any header/footer text exists,
the extractor splices in a synthetic delimiter line — `===== HEADER/FOOTER
CONTENT =====` — so the boundary between body and chrome is visible in the
flattened text.

The output is an `ExtractedSpec`: a dataclass carrying the flattened `content`
string, a `word_count`, the `source_path`, a `document_id` (the filename stem,
via `_derive_document_id`), an `extraction_warnings` list (usually empty), and a
`paragraph_map` — a list of `ParagraphMapping` records, one per extracted line,
each with a stable `element_id`. The flattened `content` is what the reviewer
reads; the `paragraph_map` is the addressing layer that lets a later stage point
*back* at exactly which element a finding came from.

### The reconstruction invariant

The two views — the flattened `content` and the `paragraph_map` — must agree.
The extractor enforces this with a hard check before it returns:

```python
content       = "\n\n".join(paragraphs)
reconstructed = "\n\n".join(m.text for m in paragraph_map)
if reconstructed != content:
    raise ValueError(f"Paragraph map for '{filepath.name}' does not "
                     f"reconstruct extracted content ...")
```

If the map and the text ever drift apart, extraction *raises* rather than
returning a quietly inconsistent object. This is a deliberate "make failures
loud" choice: a downstream consumer (an evidence panel, a future edit applier)
can rely on the guarantee that the text the model reviewed and the element-id map
are in byte-exact correspondence. The check was once a bare `assert`, which the
audit noted gets stripped under `python -O` and degrades to an opaque
`AssertionError`; it is now an explicit `ValueError` that preserves the file name
and the character counts (audit Issue 10). The same instinct — surface the
problem, don't swallow it — recurs everywhere in this codebase.

### Parallelism, order, and the context path

A real project is a folder of specs, not one file. `extract_multiple_specs` runs
the per-file extraction across a bounded thread pool (`min(8, len(paths))`
workers; a single file or `max_workers=1` runs sequentially). DOCX parsing is
I/O-bound, so threads help; but the function is careful to **preserve result
order** to match the input `filepaths`. That ordering is load-bearing:
downstream deduplication keys, request maps, and `custom_id` assignment all
assume a deterministic spec order, so a parallel speed-up must never reshuffle
the list.

Two narrower entry points round out the module. `extract_text` is the
single-file dispatcher; it accepts only `.docx` (the `SUPPORTED_EXTENSIONS` set).
`extract_context_text` handles **project-context attachments** — background
reference material a reviewer can attach to inform the review — and accepts both
`.docx` and `.pdf` (`CONTEXT_ATTACHMENT_EXTENSIONS`). Context attachments are
*reference*, not editable specs, so they get no paragraph map: the PDF path
(`_extract_pdf_text`, via `pypdf`) returns plain text, rejects encrypted files,
and tolerates a page that fails to extract by skipping it. The asymmetry is
intentional — you can address an edit into a spec, but you never edit the
background material, so it needs no element ids.

## The element-id scheme

Stable element ids are the addressing system of the whole pipeline. A finding
that says "this clause cites a superseded edition" is far more useful if it can
also say *which clause* — and ids are how. Each `ParagraphMapping` carries an
`element_id` whose format is intentionally human-readable so a finding that cites
it can be debugged at a glance:

| Element | id format | Example | Meaning |
|---|---|---|---|
| Body paragraph | `p<body_index>` | `p7` | the paragraph at body-child index 7 |
| Table cell-row | `t<table>r<row>` | `t0r2` | first table, row 2 |
| Header paragraph | `s<n>h<i>` | `s1h0` | section 1, header, paragraph 0 |
| Footer paragraph | `s<n>f<i>` | `s1f0` | section 1, footer, paragraph 0 |
| HF delimiter | `meta:hf` | `meta:hf` | the synthetic header/footer separator |

Two subtleties are worth internalizing. First, **paragraph ids are not
consecutive.** `body_index` is the enumerate position over *all* body children —
paragraphs, tables, and the section-properties element alike — so when a table or
other non-paragraph child sits between two paragraphs, the id sequence skips. The
paragraph after the first table is not `p3`; it is whatever its body position
happens to be. Second, **tables use a separate 0-based counter** (`table_counter`)
rather than the body index, so the first table is always `t0` regardless of where
it sits in the document. A tiny document makes both behaviors concrete:

```
SECTION 23 21 13 - HYDRONIC PIPING     body child 0 → p0   (heading)
PART 1 - GENERAL                       body child 1 → p1   (heading)
1.01  This Section includes ...        body child 2 → p2
┌─ submittals table ─┐                 body child 3 → t0r0, t0r1, ...
1.02  Comply with 2019 CBC.            body child 4 → p4   (note: not p3)
[Header] District Standard Spec        section 0 hdr → s0h0
```

The id `p3` simply never appears — body child 3 was the table, addressed as
`t0r*`. This is fine: ids only have to be *stable and unique within one
document*, not dense. They are stable across re-extractions of the same bytes
(the cache guarantees the same input yields the same map), and unique within the
document, which is all a consumer needs. Because ids are only document-scoped, a
downstream applier pairs `(document_id, element_id)` to disambiguate the same
text appearing in two different specs.

Alongside the id, each mapping carries a best-effort `section_id`: the extractor
tracks the most recent heading paragraph (via `_is_heading_paragraph`, a cheap
heuristic matching `PART …`, `SECTION …`, and numbered CSI subheadings like
`1.01`) and stamps it onto every element beneath it. A false positive merely
shifts a section boundary by one paragraph — harmless — so the heuristic can stay
cheap and deterministic rather than perfect.

Who consumes ids? The report's per-finding evidence rendering and any future edit
applier. Crucially, **nothing in this codebase applies edits** — that is the
emit-but-don't-apply stance the book keeps returning to (the surgical write-back
stack was removed in v3.0.0). Ids exist so an edit *instruction* can name its
target precisely; locating and applying it is a separate, future program's job.
The full story of how ids ride into the report and the JSON sidecar belongs to
[**Ch 11 — The Trust Model & Report Output**](11_trust_model_and_output.md).

## The content-loss warning: knowing what you can't see

Text extraction has a blind spot, and the system is honest about it. A spec that
is mostly *drawings* — embedded figures, equipment schedules rendered as images,
OLE objects — may carry requirements the text walk simply cannot reach. If the
model reviews only the extractable text, it reviews an incomplete document and
can miss a real defect, silently. That is a trust failure in the
"don't-miss-real-problems" direction, so the extractor raises a flag.

`_detect_content_loss_warning` counts the direct children of `<w:body>` — skipping
the `<w:sectPr>` section-properties child, which is metadata — and, for each,
checks whether it contains at least one descendant `<w:drawing>`, `<w:pict>`, or
`<w:object>`. The proportion of body elements that are "non-text" in this sense
becomes a ratio. When that ratio **exceeds 0.20** the extractor appends a warning
to `extraction_warnings`:

> *"Spec contains {N}% non-text elements ({drawings} drawings, {pictures}
> pictures, {objects} OLE objects). Some content may not have been extracted for
> review. Verify visually."*

The threshold is a **strict `>`**, implemented as `if proportion <= threshold:
return None`. This polarity matters enough that the structural audit calls it out
as *verified-clean*: a sub-agent once flagged the line as a CRITICAL inverted-bug,
reading it as the opposite of its intent, and was wrong — it is correct as
designed. Warn when more than a fifth of the body is non-text; stay silent at
exactly a fifth or below. A typical drawing-supplemented spec carries inline
figures at roughly 10% of body elements, so 20% is a conservative line chosen so
the warning means something when it fires rather than crying wolf on every
ordinary document. (The same restraint shows up in the suppression window
below — the recurring tension of this chapter is *completeness versus noise*.)

The warning is a **per-spec** signal, not a per-drawing count: one spec with three
embedded objects is one affected file. It rides on `ExtractedSpec.extraction_warnings`
all the way to the report, where the Run Diagnostics banner counts the number of
affected specs and shades the row red when any are present. That rendering is
[**Ch 11**](11_trust_model_and_output.md)'s territory; what matters here is the principle — the system tells the
reviewer *what it could not see* rather than pretending it saw everything.

## The extraction cache: never re-parse, never serve stale

Re-running a review after toggling a UI option, or re-opening the same project,
should not re-parse unchanged files. `extraction_cache.py` provides a small,
in-process **LRU cache** of `ExtractedSpec` objects. DOCX parsing already
finishes in milliseconds, so the cache is not a heroic optimization — but across
a 200-file project resubmitted after a parameter tweak, the savings add up, and
the cache is engineered so that it is *never* a correctness hazard.

The whole game is the cache key. A naïve key of `(path, size, mtime)` can lie in
at least three realistic ways:

- a `touch -d` that preserves the modification time across a content edit;
- an in-place edit that preserves file size (a cosmetic whitespace swap);
- an atomic rename-over with a copy that preserves both size and `mtime_ns`.

Each of those would return a **stale extraction** for a file that actually
changed. So the key is four-part:

```
(resolved_path, st_size, st_mtime_ns, content_fingerprint)
```

where `content_fingerprint` is a SHA-256 over the size plus the first and last
64 KiB of the file (`_FINGERPRINT_SAMPLE_BYTES`). Hashing head **and** tail is
not arbitrary: a DOCX's central directory and its opening XML parts both land
near the two ends of the archive, so ~128 KiB of sampling catches any practical
bit-level change without paying for a full-file SHA on every lookup. If the
fingerprint read fails (a transient I/O error, the file vanished between `stat`
and `open`), it returns an empty string and the cache simply falls back to the
stat-only key — degrading safely rather than crashing.

Three more properties make the cache safe to share across the run:

- **Thread-safe.** All reads and writes hold a `threading.Lock`, so the parallel
  extractor can populate it from worker threads without a race.
- **LRU-bounded.** An `OrderedDict` capped at 64 entries; the least-recently-used
  entry is evicted on overflow. The cache cannot grow without bound across a long
  session.
- **Deep-copy on the boundary.** `ExtractedSpec` is *mutable* — a caller might set
  `paragraph_map = None` to build a derived view. So the cache stores a deep copy
  on `put` and returns a deep copy on `get`. A caller mutating what it received
  can never corrupt the cached state or leak into the next consumer.

The structural audit lists this key as *verified-clean*: "a changed file is not
served a stale extraction in any realistic case." The cache is also intentionally
**process-local and not persisted** — crash recovery is the resume-state
subsystem's job, and persisting extracted text would force a sensitive-data
retention decision the cache deliberately avoids. (The same module also holds a
token-count cache used by the preflight; its semantics belong to [**Ch 12 —
Configuration, Models & Token Economics**](12_configuration_and_models.md).)

## The deterministic pre-screen

With clean text in hand, `preprocess_spec` runs the local detectors. Every alert
is a uniform dict — `filename`, `type` (a human-readable description), `match`
(the literal text matched), `context` (a ~120-character window for human review),
`position` (the character offset), and a stable **`deterministic_rule`** id — and
the alerts are grouped on a `PreprocessResult` by category. The uniform shape is
what lets the report render them, and the routing layer branch on them, without
re-parsing prose.

There are **nine detectors**. Eight are per-document and run inside
`preprocess_spec`; the ninth, `inconsistent_filename`, is the lone *cross-file*
check — it compares filenames across the whole project and so is invoked
separately, with the list of names, before submission. (The stale-cycle detector
emits two distinct `deterministic_rule` ids — `stale_code_cycle` and
`stale_asce7` — so the table below lists ten id rows for nine detectors.)

| `deterministic_rule` | What it catches | Example |
|---|---|---|
| `leed_reference` | LEED / USGBC mentions inappropriate for a K-12 DSA project | `LEED-NC Credit 4.1` |
| `placeholder` | unresolved editorial markers | `[SELECT manufacturer]`, `[TBD]`, `___` |
| `template_marker` | template/authoring debris the placeholder regexes miss | `TODO:`, `FIXME`, `???`, `Lorem ipsum` |
| `stale_code_cycle` | a *real* California cycle year that isn't the current one | `2019 CBC` (cycle is 2025) |
| `stale_asce7` | an ASCE 7 edition older than the cycle's | `ASCE 7-10` (cycle is 7-22) |
| `invalid_code_cycle` | a year/code combination that is not a real cycle | `2018 CBC` |
| `empty_section` | a numbered heading with no body before the next heading | `2.01 SUBMITTALS` (then nothing) |
| `duplicate_heading` | the same section number appearing twice | a second `2.01` |
| `duplicate_paragraph` | a substantial paragraph (≥80 chars) repeated verbatim | a copy-pasted QA clause |
| `inconsistent_filename` | mixed CSI filename styles across the project | `23 21 13 …` vs `23-21-13 …` |

A shared helper, `_find_matches`, drives the regex-based detectors. It does
**span-based deduplication**: if a match's character range is fully contained in
an already-recorded span, it is skipped. That is why the LEED patterns are
ordered specific-before-generic — `LEED-NC` claims its span first, so the generic
`\bLEED\b` does not then fire a redundant second alert on the `LEED` substring.
Each detector also caps its output (`max_matches`) so a pathological file cannot
flood the report with thousands of identical alerts.

Two pieces of this layer are subtle enough to deserve their own treatment: the
distinction between *stale* and *invalid* code cycles, and the suppression window
that keeps the stale-cycle detector from flagging descriptive prose.

### Stale vs. invalid: disjoint by construction

Both detectors look at the same `"<year> <code>"` shape — `2019 CBC`, `CBC 2019`,
`2019 California Building Code` — but they apply *different admissibility tests*,
and those tests are designed to never overlap:

- **Stale** (`detect_stale_code_cycle_references`): the year is a real, published
  California cycle (`_PLAUSIBLE_CODE_YEARS = {2010, 2013, 2016, 2019, 2022,
  2025}`) but is **not** the currently selected cycle's year. `2019 CBC` against
  the 2025 cycle is stale — it was once correct and now is not.
- **Invalid** (`detect_invalid_code_cycle_strings`): the year looks like a year
  (`20\d{2}`) but is **not** in the set of real California cycles
  (`_VALID_CALIFORNIA_CODE_YEARS`, which extends the plausible set with the
  anticipated `2028`). `2018 CBC` is invalid — California never published a 2018
  cycle, so it is a typo or a fabrication, not a stale-but-real reference.

The two admissibility sets are **disjoint by construction**: a year is either a
real published cycle (stale's domain) or it is not (invalid's domain), never both.
So the two detectors can scan the same text with the same patterns and never
double-flag the same citation. The distinction is not academic — it tells the
reviewer a different story. A stale reference means "update this to the current
cycle"; an invalid one means "this number is wrong, full stop." The ASCE 7 cousin
(`stale_asce7`) works the same way against `_ASCE7_PLAUSIBLE_EDITIONS = {"05",
"10", "16", "22"}`, flagging only editions *older* than the cycle's.

### The stale-cycle suppression window

A blunt "flag every non-current year next to a code" detector would be
intolerable, because specs legitimately *mention* old cycles in descriptive prose:
"this section was *previously* governed by the 2019 CBC," or "the 2019 CBC is *no
longer* applicable." Those are not defects; they are an author correctly
narrating history. `_should_suppress_stale_cycle` is the guard that keeps the
detector quiet on them.

When a stale-cycle match is found, the function scans up to **80 characters on
each side** (`_STALE_CYCLE_SUPPRESS_WINDOW`) for a whole-word negation or
historical keyword: `previously`, `formerly`, `superseded`, `withdrawn`,
`obsolete`, `no longer` (matched only as a two-word phrase), `prior`,
`historical`, plus auxiliary-verb negations like `shall not` / `will not` /
`is not` and a set of contractions (`isn't`, `won't`, `doesn't`, `cannot`, …). To
keep a negation in a *neighboring* sentence from bleeding across and silencing a
genuine requirement, the window is **narrowed at the nearest sentence terminator**
(`.`, `;`, or a `\n\n` paragraph break) — the preceding window is trimmed to the
text after the last terminator, the trailing window to the text before the next
one. If both trimmed windows are empty, there is nothing to suppress and the alert
stands.

The most instructive design decision here is what is *deliberately excluded*:
**bare `not` is not a suppressor.** Only `not` bound to an auxiliary verb (a real
verb-phrase negation) counts. The reason is a specific false-suppression the
authors anticipated: a sentence like "Section X is also referenced in 2019 CBC
and *not* 2022 CBC" contains a bare `not`, but it negates the *wrong* year — and
treating that `not` as a suppressor would silence the active 2019 reference that
should have been flagged. So the matcher demands a verb-phrase negation and lets
bare `not` through. An active requirement — "Comply with 2019 CBC" — has no
suppressor anywhere near it and flags exactly as it should. (The audit notes the
pre-window's apparently "missing `break`" is in fact correct: the loop reassigns
the window per terminator, which is equivalent to trimming to the rightmost
terminator. Not a bug.)

### The rule ids are public

One last point ties this section forward. Every alert's `deterministic_rule` id
is a stable, exported constant (`DETERMINISTIC_RULE_LEED`,
`DETERMINISTIC_RULE_PLACEHOLDER`, and so on), and those ids are **public on
purpose**. The verification router reads them: a finding whose lineage traces to a
deterministic detector — a `placeholder`, a `template_marker`, a
`duplicate_paragraph` — can be *locally skipped* during verification, because
there is nothing on the web to check about a `[TBD]` placeholder. Branching on a
known id is robust in a way that keyword-sniffing the human-readable `type` string
is not. How those ids actually steer the local-skip routing is [**Ch 9 —
Verification I**](09_verification_routing.md); what this chapter guarantees is that the id exists and means
exactly one thing.

It is also worth stating plainly: this module **detects, it never modifies.** The
preprocessor reports a `[SELECT]` placeholder; it does not delete it. Document
cleanup is an explicitly separate concern (and a separate tool). That separation
is the same emit-but-don't-apply discipline that governs edits — the input layer
observes the document and reports on it, and changing the document is never its
job.

## Design tensions & the honest edges

The recurring tension across all three files is **completeness versus noise**. A
pre-screen that fires too eagerly trains reviewers to ignore it, which is worse
than not having it. So the load-bearing constants are all tuned conservatively:
the content-loss threshold is a strict `> 20%`; the duplicate-paragraph detector
ignores anything under 80 characters (so `PART 1 - GENERAL` repeating across
sections is not "duplication"); the suppression window is a tight ±80 characters
clipped at sentence boundaries; and bare `not` is excluded from suppression. Each
of those choices trades a sliver of completeness for a meaningful reduction in
false alarms, on the theory that a quiet, trustworthy pre-screen earns the
reviewer's attention when it does speak.

But there are real, bounded gaps the reader should know about — and the book's
trust thesis demands they be named rather than hidden.

**Extraction completeness (Audit P0-6).** The body walk sees paragraphs and
tables; the second pass adds header and footer *paragraphs*. What still falls
outside what python-docx surfaces through these iterations: **text boxes**
(`w:txbxContent`, which live inside drawing XML), **footnotes and endnotes**
(stored in separate document parts), **tables nested inside headers or footers**
(the header/footer pass walks `container.paragraphs`, not its tables), and
**SmartArt / grouped-shape text**. DSA specs do occasionally tuck a requirement
or a revision note into exactly these places, and when they do, the model reviews
an incomplete document.[^p06] The mitigations are partial and worth being precise
about: a *text-box-heavy* spec will usually trip the content-loss warning, because
text boxes live inside `<w:drawing>` elements that the warning counts — so that
particular gap tends to announce itself. Footnotes and header/footer tables,
however, would *not* raise the warning. The ultimate backstop is the LLM review
itself plus the per-spec "verify visually" prompt; but a reader should understand
this as a known, bounded limitation, not a solved problem.

**ASCE 7 pre-2005 editions (Audit P2-1).** The stale-ASCE-7 detector only
recognizes editions in `_ASCE7_PLAUSIBLE_EDITIONS = {"05", "10", "16", "22"}`. A
genuinely ancient citation — `ASCE 7-98`, `7-02` — is `not in` that set, so it is
skipped rather than flagged. The plausible-set gate exists to avoid flagging
garbage two-digit captures, but it has the side effect of missing truly old
editions. This is a *deterministic-layer completeness gap only* — it produces no
*wrong* findings, and the LLM review very likely still catches such an obviously
outdated reference — which is why the audit rates it low. It is the same shape of
trade-off as everywhere else in this layer: a conservative recognizer that errs
toward silence, with the model as the backstop.

Both of these are tracked in [**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md), where the
extraction-completeness item and the audit's verification-first approach to it are
discussed in full. The pattern to take away is the one that defines the whole
input layer: *be certain about what you can see, and honest about what you can't.*

## How it connects

- **Upstream:** nothing — this *is* the front door. The orchestrator
  (`extract_multiple_specs_cached` → `preprocess_spec`) is sequenced in [**Ch 7 —
  Orchestration & State**](07_orchestration.md) and shown in motion in [**Ch 3 — A Run, End to End**](03_end_to_end_flow.md).
- **The data shapes** (`ExtractedSpec`, `PreprocessResult`) were mapped in [**Ch 2
  — Architecture at a Glance**](02_architecture.md); this chapter owns their detail.
- **The review** consumes the flattened `content` and the element-id map to build
  its prompt → [**Ch 5 — The Review Engine**](05_review_engine.md).
- **Verification routing** reads the public `deterministic_rule` ids to decide
  local-skips → [**Ch 9 — Verification I**](09_verification_routing.md).
- **The report** renders detector alerts under a "(deterministic check)" heading
  and surfaces the content-loss count in the Run Diagnostics banner → [**Ch 11 —
  The Trust Model & Report Output**](11_trust_model_and_output.md).
- **The active code cycle** that the stale/invalid detectors compare against
  (`CALIFORNIA_2025`, with its CBC year and ASCE 7 edition) is defined in [**Ch 12
  — Configuration, Models & Token Economics**](12_configuration_and_models.md).
- **The known gaps** (extraction completeness, ASCE 7 pre-2005) are audited in
  [**Ch 16 — Trust Under the Microscope**](16_trust_under_the_microscope.md).

## Key takeaways

- The input layer turns a `.docx` into an **`ExtractedSpec`** — a flattened
  `content` string plus a `paragraph_map` of stably-addressed elements — and a
  **reconstruction invariant** guarantees the two views stay byte-exact or
  extraction raises.
- **Element ids** (`p7`, `t0r2`, `s1h0`, `s1f0`, `meta:hf`) are document-scoped,
  stable, and human-readable. Paragraph ids skip where tables sit (they use the
  body index); tables use a separate counter. Ids exist so an edit *instruction*
  can name its target — nothing here applies edits.
- The **content-loss warning** fires when more than **20% (strict `>`)** of body
  elements are drawings/pictures/OLE objects, telling the reviewer what text
  extraction may have missed. The polarity is audit-confirmed correct, not an
  off-by-one.
- The **extraction cache** keys on `(resolved_path, size, mtime_ns, head+tail
  SHA-256)`, is thread-safe and LRU-bounded, and returns **deep copies** — so a
  changed file is never served stale and a caller can never corrupt cached state.
- The **deterministic pre-screen** runs nine detectors locally, for free, with
  perfect reliability on what they can see. **Stale and invalid** code cycles are
  disjoint by construction; the **suppression window** keeps the stale detector
  quiet on descriptive prose, and **bare `not` is intentionally not a suppressor**.
- Every alert carries a **public `deterministic_rule` id** that downstream routing
  branches on. The layer *detects but never modifies* — the same
  emit-but-don't-apply discipline that defines the product.
- The honest edges — **text boxes, footnotes, header/footer tables, and
  grouped-shape text** (P0-6), and **pre-2005 ASCE 7 editions** (P2-1) — are known,
  bounded gaps with the LLM review as the backstop. The input layer's creed:
  *certain about what it sees, honest about what it can't.*

[^p06]: The audit (P0-6) phrases this as python-docx "body iteration typically
misses headers/footers." That is true of body iteration *alone*, but the current
extractor adds a dedicated `doc.sections` pass that does capture header and footer
*paragraphs* (giving them `s<n>h<i>` / `s<n>f<i>` ids) — so the paragraph case the
audit raised is at least partly addressed in source. The genuinely uncaptured
parts are the narrower set named above: text boxes, footnotes/endnotes,
header/footer *tables*, and grouped-shape text. Source wins over the audit's
summary; the gap is real but narrower than the one-line phrasing suggests. A
second, smaller drift: the `ParagraphMapping` docstring describes the
header/footer delimiter id as `meta<n>`, but the code emits the literal
`meta:hf` — the value above is what the code actually produces.
