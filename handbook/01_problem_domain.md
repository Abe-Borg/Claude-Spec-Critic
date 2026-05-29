# The Problem Domain: California DSA Mechanical & Plumbing Spec Review

A new public school in California does not begin as a building. It begins as
paper — a thick stack of it. Long before the first trench is cut, the project
exists as a set of drawings and an even larger set of *specifications*: written,
legally binding instructions that tell the contractor exactly what to install,
to which standard, tested how, warranted for how long. The drawings show *where*
the air handler goes; the specification says *which* air handler, that it must
comply with the current energy code, that its refrigerant circuit follows a
named safety standard, that it is seismically restrained to a pre-approved
detail, and that it is commissioned before the building is occupied. For the
mechanical and plumbing scope of a single K-12 school, that written half of the
contract runs to dozens of documents and hundreds of pages.

And every page of it has to clear one gate before anyone breaks ground: the
**Division of the State Architect**. In California, the DSA reviews and approves
the construction documents for K-12 and community-college projects, and its
approval is not a formality. A specification that cites a superseded code
edition, contradicts itself between two articles, or leaves a template
placeholder unresolved can be kicked back — costing weeks on a schedule that is
usually counted in school years. Worse, a defect that *isn't* caught does not
disappear; it gets built. The wrong sprinkler density, the missing damper
access door, the seismic restraint specified to a withdrawn pre-approval — these
become physical conditions in a building that will hold a thousand children for
the next fifty years.

That is the world Spec Critic lives in, and it sets up the single idea the rest
of this book is organized around. Reviewing these documents is high-volume,
high-stakes, and unforgiving of inattention — exactly the kind of work you'd
want to hand to a machine. But it is also a domain where being *confidently
wrong* is a distinct and worse failure than missing something. A specification
is a stamped, accountable, legal instrument. A tool that asserts a wrong section
number is correct — and is believed — has not saved a reviewer's time; it has
laundered a guess into a document headed for a school site. **A missed defect is
a cost. A confident error is a hazard.** Hold that distinction; it is why the
tool is shaped the way it is, and it returns in every later chapter.

## 1. The document: what a "spec" actually is

The unit of input is a **spec** — a single `.docx` specification *section*, written
in the format defined by the Construction Specifications Institute's
**MasterFormat**. Specs are not free-form prose. They are numbered, sectioned,
and ruthlessly conventional, because a contractor, an estimator, an inspector,
and a reviewer all have to find the same clause fast. A section like *"23 05 00 —
Common Work Results for HVAC"* opens with a six-digit CSI number, then divides
into three predictable PARTs — **PART 1 General**, **PART 2 Products**, **PART 3
Execution** — each subdivided into numbered Articles (`1.01`, `1.02`, `3.04`) and
lettered paragraphs. A reviewer's mental model is built on that grid: code
compliance and submittals live in PART 1, the actual equipment in PART 2, and
how it's installed and tested in PART 3.

The first two digits of the section number place it in a **CSI division**, and
the divisions are how the trades are carved up. Spec Critic's lane is the
**mechanical and plumbing (M&P)** disciplines, which span:

- **Division 22 — Plumbing** (fixtures, piping, water heaters, gas)
- **Division 23 — HVAC** (air handlers, ductwork, hydronic piping, controls)
- **Division 21 — Fire Suppression** (sprinklers, standpipes, hydraulic
  calculations)
- **Division 25 — Integrated Automation** (building controls that tie the above
  together)
- **Division 01 — General Requirements**, which sits above all of them and
  states project-wide rules the technical sections must not contradict

A real project is not one of these documents but a *set* of them — a project
manual where Division 22 and Division 23 and Division 21 sit side by side, each
authored or edited by different hands, often at different times. That
multiplicity matters enormously, and it is the seed of one of the hardest defect
classes we'll meet: a requirement can be perfectly self-consistent inside one
file and flatly contradict a sibling file no single reader ever opens beside it.

There is one more structural fact that explains almost everything about why these
documents go wrong: **specs are templated.** Few are written from scratch. They
descend from office master guide specifications or commercial template libraries,
edited project-to-project by deleting what doesn't apply and filling in what
does. That inheritance is a gift — it encodes hard-won institutional knowledge —
and a curse. A template carries forward not just its good language but its stale
references, its bracketed *"[SELECT ONE]"* placeholders, and the editorial debris
of every project that touched it before. The defects propagate by copy-paste.
When you understand that specs are *inherited*, the rest of this chapter stops
being a list of unrelated mistakes and becomes a single recognizable pattern.

## 2. The gate: DSA, HCAI, and the AHJ

A recurring term in this domain is the **Authority Having Jurisdiction (AHJ)** —
the agency empowered to review, approve, and enforce code compliance for a given
project. "Who is the AHJ?" is the question that decides which rules bind and who
says *yes*.

For the projects Spec Critic targets, the AHJ is the **DSA — the Division of the
State Architect** — the California authority over K-12 and community-college
construction. DSA review is the hard gate already described: documents go in,
and either approval or a correction list comes back. The economics are brutal in
one direction. Catching a defect *before* submission costs an editor five
minutes; catching it *after* DSA flags it costs a resubmission cycle; not
catching it at all can cost a change order in the field or a life-safety problem
in an occupied school.

A second authority surfaces often enough to be worth naming: **HCAI — the
Department of Health Care Access and Information** (formerly **OSHPD**, the Office
of Statewide Health Planning and Development). HCAI is the AHJ for California
*healthcare* facilities — the healthcare analogue to the DSA. It appears in K-12
mechanical specs not because schools are hospitals, but because the two
jurisdictions *share infrastructure*. The most common bleed-through is seismic
restraint: California's healthcare seismic pre-approval catalogs (the OSHPD/HCAI
"OPM/OPA" pre-approval listings for equipment anchorage and bracing) are
referenced across disciplines, and a mechanical spec that calls for a seismic
restraint detail will often cite them. A spec author who copies that language
without checking whether the pre-approval is current, or correctly attributed,
plants exactly the kind of subtle, authoritative-sounding defect that is easy to
miss and expensive to be wrong about.

The takeaway for the rest of the book: the tool isn't checking against one
monolithic rulebook. It is checking a document against *a specific authority's
specific expectations at a specific point in time* — and "a specific point in
time" turns out to be the crux of the whole problem.

## 3. "Correct" is a moving target: the code cycle

Here is the fact that makes this domain genuinely hard, and not merely tedious:
**in spec review, "correct" is not absolute. It is defined relative to the
adopted code cycle.** The same sentence — *"Comply with ASCE 7-16 for seismic
design"* — is correct in one cycle and a defect in the next. There is no
context-free way to judge it. You have to know which cycle the project is bidding
under, and then you have to know, in detail, which editions of which standards
that cycle actually adopted.

A **code cycle** is the dated set of California codes in force, together with the
pinned editions of every standard those codes reference. Spec Critic is
configured, by default, for the **California 2025 cycle** — `CALIFORNIA_2025`,
the single `DEFAULT_CYCLE` defined in `src/core/code_cycles.py`. (An earlier 2022
cycle existed and was deliberately removed; California 2025 is the only cycle the
v3.0.0 tool knows, and it is not to be reintroduced. The cycle is wired into the
verification machinery deeply enough — it is even part of the evidence-cache
key — that switching cycles correctly invalidates stale conclusions rather than
silently trusting them.)

California's codes are not separate books. They are *parts* of one umbrella, the
California Building Standards Code (Title 24 of the California Code of
Regulations). The pieces an M&P reviewer cares about are:

| Code | Title 24 part | What it governs |
|---|---|---|
| **CBC** — California Building Code | Part 2 | The base building, structural and fire-rated assemblies |
| **CMC** — California Mechanical Code | Part 4 | HVAC, ductwork, ventilation, refrigeration |
| **CPC** — California Plumbing Code | Part 5 | Plumbing fixtures, piping, gas, drainage |
| **Energy Code** (California Energy Code) | Part 6 | Equipment efficiency, ventilation rates, controls |
| **CALGreen** — Green Building Standards Code | Part 11 | Water/energy conservation, indoor air quality |

On top of these, the cycle adds **ASCE 7** for structural/seismic loading
(California 2025 pins **ASCE 7-22**, with **7-16** as its immediate
predecessor — the canonical "stale edition" trap), and it *pins specific editions*
of a long list of referenced industry standards.

This pinning is where the inherent difficulty becomes concrete, and it is worth
dwelling on because it motivates a surprising amount of the architecture. The
intuitive assumption — *"current means the newest edition"* — is wrong, and
dangerously so. California adopts particular editions, frequently **not** the
latest published one, and frequently **with California Amendments** that change
the model standard's text. The 2025 cycle, as captured in `code_cycles.py`,
pins editions like these:

| Standard | What it covers | Edition pinned for California 2025 |
|---|---|---|
| **NFPA 13** | Sprinkler system design | **2022, with California Amendments** |
| **NFPA 14** | Standpipe systems | **2019** |
| **NFPA 20** | Fire pumps | **2022, with California Amendments** |
| **NFPA 25** | Inspection/testing of water-based systems | **2020, with California Amendments** |
| **NFPA 72** | Fire alarm & signaling | **2022, with California Amendments** |
| **ASHRAE 62.1 / 90.1 / 15** | Ventilation / energy / refrigeration safety | **2022** |
| **IAPMO** Uniform Plumbing trade standards | Plumbing trade companion to the CPC | **2024** |
| **UL 300 / 555 / 555S / 268 / 1479** | Kitchen fire suppression, dampers, smoke detectors, firestop | various *(e.g. UL 555 — 2006 revised)* |

Stare at that NFPA column for a moment. Three different "current" NFPA editions
(2022, 2019, 2020) coexist in the *same* cycle, several of them amended by the
state. A reviewer cannot reason from a single rule like "use the 2022 editions."
They have to carry a lookup table in their head — and so must any tool that
hopes to replace them. A spec that cites *"NFPA 13, 2025 edition"* is wrong even
though 2025 is *newer* than the adopted 2022; a spec that cites the unamended
national NFPA 20 is wrong even though the edition year matches, because
California amended it. "Newer" is not "correct," and "right year" is not
"complete." This is the difficulty in one sentence: **building codes are
versioned, cross-referenced, jurisdiction-amended, and the correct answer is a
moving target that depends on a cycle the document itself rarely states.**

There is one more honest wrinkle, and it sets the tone for the whole book. The
pinned editions are how the tool defines ground truth — the reviewer and
verifier are told to check claims against *these* editions and to flag drift away
from them (see **Ch 12 — Configuration, Models & Token Economics** for how the
cycle is modeled, and **Ch 5 — The Review Engine** for how it reaches the prompt).
But the source file that holds those editions does not pretend they are
infallible. Its own comments call them "a best-effort snapshot at the time the
cycle was integrated" and instruct an engineer to **verify each string against
the California Building Standards Commission's published adoption matrix before
relying on it.** The tool's own ground truth ships with a *verify-me* caveat.
That candor is not a weakness in the documentation; it is the trust posture of
the entire system, stated in miniature. We'll see it again, formalized, in
**Ch 16 — Trust Under the Microscope**.

## 4. Why human review is unforgiving: the pain

If the rules are this intricate, why is human review error-prone? Not because
reviewers are careless — most are expert and conscientious — but because the work
is structured to defeat attention.

First, **volume and tedium.** A reviewer faces hundreds of pages of dense,
deliberately uniform prose, much of it identical boilerplate from project to
project. Vigilance is a finite resource, and a defect on page 180 gets the
dregs of it.

Second — and this is the cruel part — **the defects look exactly like the
correct text around them.** A stale code reference is a grammatically perfect,
authoritative-sounding compliance sentence. It does not announce itself with a
typo. *"Comply with the 2019 California Mechanical Code"* reads precisely like
*"Comply with the 2025 California Mechanical Code"*; only a reviewer actively
holding the current cycle in mind catches the difference, and only if they happen
to be reading that line with full attention. The signal and the noise are written
in the same hand.

Third, **the worst defects span files.** A self-consistent Division 23 controls
section can reference a damper type that the Division 23 damper schedule never
lists; a warranty duration in one section can contradict the duration in another;
a value set in plumbing can be silently undone in mechanical. No single document
is wrong on its own. The conflict only exists in the *relationship between*
documents — and human reviewers, reading one section at a time, are structurally
poorly positioned to see it.

The concrete defect classes Spec Critic targets are not invented; they are mined
directly from what actually goes wrong in these documents, and they map to the
tool's local detectors and to the seventeen review categories its reviewer prompt
enumerates (`src/review/prompts.py`). Named as *real-world defects* — not as
code — they look like this:

| Defect class | What it looks like in a spec | Why it matters on a K-12 project |
|---|---|---|
| **Stale code-cycle reference** | "Comply with the 2019 CBC" on a 2025 project | Cites a real but superseded edition; an authoritative-sounding sentence that is simply out of date — a classic DSA correction item |
| **Invalid / fabricated code cycle** | "2018 CBC" — a year/code combination that never existed | A reference that *sounds* like a citation but points at nothing real |
| **Code-edition drift on standards** | "NFPA 13, 2025 edition" or an unamended national standard | "Newer" or "right year" isn't "adopted"; the spec must track California's *pinned, sometimes-amended* editions |
| **Unfilled placeholder** | `[SELECT ONE]`, `[VERIFY]`, `TBD` left in the text | A template prompt the author never resolved — an explicit "decision not made yet" shipped as if final |
| **Template / authoring marker** | `TODO:`, `FIXME`, `???`, stray lorem ipsum | Editorial debris that should never reach a stamped submittal |
| **Leftover LEED language** | LEED credit requirements on a non-LEED project | Inherited from a template; imposes obligations the project never agreed to and the AHJ doesn't expect |
| **Internal contradiction** | Two articles in one section state conflicting requirements | The contractor cannot satisfy both; whichever they pick may be wrong |
| **Cross-spec coordination conflict** | Controls sequence references a damper the schedule omits; warranty durations disagree across sections | The defect lives *between* documents; no single-file read catches it, and it surfaces as a field change order |
| **Duplicate or empty section** | A heading repeated, or a section header with no body beneath it | Signals copy-paste damage or an unfinished edit; ambiguous requirements invite RFIs |
| **Inconsistent file naming** | A filename's CSI number disagrees with the section number inside | A filing/organization defect that confuses everyone downstream, including the AHJ |

A useful way to read that table — and a thread **Ch 4 — Input** picks up in
detail — is that the classes split by *how certain* the judgment is. Some are
mechanical and local: a `TODO:`, an empty heading, a duplicated paragraph, a
filename mismatch can be found by a deterministic check with no model and no
guessing, because the rule for "is this a placeholder?" is exact. Others —
"does this controls sequence actually contradict the damper schedule?", "is this
cited section withdrawn?" — require reading comprehension, domain knowledge, and
sometimes a trip to the live code text. That split between *cheap certainty* and
*expensive judgment* is not an implementation detail; it is the organizing
principle of the entire pipeline, and recognizing it here is the point of this
chapter.

## 5. Why an AI reviewer — and the sharper risk it carries

Laid out that way, the case for an AI reviewer is strong. A language model does
not get tired on page 180. It can read *every* article in *every* section, hold
the entire pinned-editions table in working memory, and — crucially — cross-
reference claims *across* files in a way a serial human reader cannot. It knows
the shape of the code corpus. It can recognize that a sentence is a compliance
citation and ask whether the cited edition matches the cycle. For the high-
volume, pattern-matching, cross-referencing core of spec review, this is a
genuinely good fit.

But the same capability that makes a model useful here makes it dangerous, and
the danger is specific to *this* domain. A model's failure mode is not silence;
it is **fluent, confident fabrication.** Asked about a code section, it will
happily produce a section number, an edition year, and a paragraph of plausible
justification — whether or not any of it is real. In most applications a
confidently wrong answer is an annoyance. In compliance review it is the *worst*
outcome on the board, because the entire value of the tool rests on a reviewer
being able to *believe* it. A spec reviewer who is told "this NFPA citation is
correct" by a tool they trust, and who therefore does not double-check, is in a
worse position than one who had no tool at all and checked it themselves — the
tool has manufactured false confidence and attached it to a document bound for a
school.

This is why Spec Critic cannot be "an LLM that reviews specs" and stop there.
The model's reach is the asset; the model's confidence is the liability; and the
architecture exists to capture the first while refusing to trust the second. Two
commitments fall directly out of that, and they define the shape of everything
downstream:

1. **Check the cheap, certain things without a model at all.** The mechanical
   defect classes — placeholders, template markers, empty sections, duplicates,
   filename mismatches — are caught by deterministic, no-API detectors that run
   *before* any model is consulted. They never guess, because their rules are
   exact, and they keep the expensive probabilistic machinery from being asked
   questions it doesn't need to answer. (**Ch 4 — Input** owns these.)

2. **Never accept a positive verdict on the model's word alone.** When the tool
   does claim a finding is confirmed or corrected, that claim must be *grounded*
   in a real source — a citation the system can show was actually retrieved, not
   one the model produced from memory. (**Ch 10 — Verification II** is built
   entirely around this; the important caveat, foreshadowed here, is that
   grounding proves the *source is real*, not that the source proves the claim.)

## 6. The chosen shape: emit, don't apply

There is a third commitment, and it is the one that most defines the *product* —
the design stance you'll meet under a dozen different names throughout the book.
**Spec Critic emits structured edit instructions. It never applies them.** The
tool proposes; a human, or a separate downstream program, disposes.

Concretely: when the reviewer identifies a fixable defect, the finding may carry
a structured **edit proposal** — an action (edit / delete / add, or simply
*report only*), the existing text, a proposed replacement, a target element, and a
confidence. That proposal is *rendered* two ways. It appears inline in the
human-readable **Word report** as a "Proposed replacement," and it is written, in
machine-readable form, to a JSON **edit sidecar** named `<report-stem>.edits.json`
placed next to the report. The sidecar is a clean hand-off contract: a separate,
future *applier* program can ingest it and make the edits, with each finding's
verification status and confidence riding along for that program to gate on. Spec
Critic itself never opens the `.docx` to change it. Every finding is labeled
either `EDIT_SUGGESTED` (it carries a proposal) or `REPORT_ONLY` (it doesn't,
because some defects — "these two disciplines need to meet and resolve this" —
have no clean one-line fix), and even an `EDIT_SUGGESTED` finding is exactly that:
a suggestion.

This was not always the shape. An earlier version of Spec Critic carried a full
"surgical write-back" stack — machinery that *located* an edit target inside a
`.docx` and *mutated it in place*, gated by an elaborate auto-apply confidence
calculation. **Version 3.0.0 removed all of it** — the locator, the spec editor,
the apply machinery, the GUI "apply" dialogs, and the confidence gating whose only
purpose was to decide whether to auto-edit. The story of *why* that machinery was
torn out, and what the team learned from running it, belongs to **Ch 17 —
Evolution & Lessons.** But the *reason it stays out* is pure domain logic, and it
belongs here:

```
  TRADITIONAL MANUAL REVIEW
    spec.docx ──► reviewer reads ──► reviewer edits in place ──► stamped & submitted
                  (serial, tiring;     (a human is both             (an accountable,
                   quiet defects        finder AND fixer)            licensed professional
                   slip past)                                        owns the result)

  SPEC CRITIC — emit, don't apply
    spec.docx ──► local detectors ──► review + grounded ──► WORD REPORT + edits.json
                  (no model; cheap     verification          │   (findings, trust labels,
                   certainties)        (uncertainty is        │    proposed edits)
                                        labeled, not hidden)   ▼
                                                         human / downstream applier
                                                         decides what to apply
                                                         (the accountable human
                                                          stays in the loop)
```

Why would a compliance tool deliberately stop short of fixing what it finds? Two
reasons, both rooted in the domain. The first is **accountability.** A
specification is stamped by a licensed professional who is legally responsible
for its contents. Auto-applying an AI-generated edit silently inserts an unowned
change into a stamped, legal instrument — it launders a model's guess into the
professional's signature. The emit-only stance keeps the human firmly in the
decision seat: the tool can argue for an edit, with evidence, but a person
accepts it. The second is **the asymmetry of being wrong**, the same one this
chapter opened on. A flagged-but-wrong *suggestion* is cheap — a reviewer reads
it, disagrees, moves on. An *applied*-but-wrong edit is expensive and insidious:
it silently changes a document that already looked finished, and a later reader
has no reason to suspect the sentence they're reading was rewritten by a machine
that misjudged the code. Given a model whose worst failure is confident error,
the safe place to draw the line is exactly where v3.0.0 drew it — at *emit*.

## 7. The trust mandate, in domain terms

Step back and the whole domain resolves into a single mandate. Spec Critic reviews
high-stakes, high-volume compliance documents whose correctness is a
jurisdiction-specific, cycle-dependent, cross-referenced moving target — work that
genuinely benefits from a tireless, code-literate machine reader. But it does that
work in a setting where a confident error is a liability, not a mere miss, because
the output feeds a stamped document headed for a public school under the DSA's
review. **Therefore the tool's first job is not to be right; it is to be honest
about how sure it is.**

Everything the later chapters describe is a mechanism for making that honesty
concrete and visible rather than rhetorical. The deterministic detectors exist so
the certain things are answered with certainty and never muddied by a model
(**Ch 4 — Input**). Grounded verification exists so a positive verdict has to
point at a real, retrieved source (**Ch 10 — Verification II**). The trust model
labels every finding with one of nine honest statuses — including frank labels for
"the verifier ran but couldn't ground this," "two capable models read real
sources and *disagreed*," and "verification failed for an operational reason" —
so the report never flattens uncertainty into a false binary (**Ch 11 — The Trust
Model & Report Output**). The emit-only stance keeps an accountable human in the
loop. And when a verdict still looks wrong, a forensic trace lets you reconstruct
exactly what the model saw — while the project's own audits, the subject of
**Ch 16 — Trust Under the Microscope**, deliberately surface the system's edges
rather than bury them, in the same spirit as that "verify-me" caveat on the pinned
editions.

That is the problem, the stakes, and the posture. **Ch 2 — Architecture at a
Glance** is where the posture becomes structure: it draws the map of subsystems
and the core data objects — chief among them the `Finding`, the unit of currency
that is born in review, gathers a grounded verdict, and arrives at the report
carrying everything a human needs to decide whether to trust it. We have argued
*why* the system must keep its uncertainty visible. The next chapter shows the
shape that argument forced the code into.

## Key takeaways

- **The domain.** Spec Critic reviews California K-12 **mechanical & plumbing**
  CSI-format `.docx` specifications — dozens of numbered sections per project,
  across CSI divisions 21/22/23/25 and the Division 01 umbrella — that must clear
  approval by the **DSA**, the Authority Having Jurisdiction for K-12 work
  (with **HCAI/OSHPD**, the healthcare AHJ, bleeding in through shared seismic
  pre-approvals).
- **"Correct" is cycle-relative.** Compliance is judged against an adopted **code
  cycle** — here **California 2025** (`DEFAULT_CYCLE`), the only cycle the v3.0.0
  tool knows. The cycle pins *specific, often-not-latest, often-California-amended*
  editions (NFPA 13 *2022 w/ Amendments*, NFPA 14 *2019*, ASHRAE 62.1/90.1 *2022*,
  ASCE *7-22* over *7-16*, …), so "newer" is not "correct."
- **The pain is structural.** Review is high-volume and tedious; defects read
  exactly like the correct text around them; and the worst defects span *multiple
  files* no single read catches. Real defect classes — stale/invalid cycles,
  edition drift, placeholders, template markers, leftover LEED, internal
  contradictions, cross-spec conflicts, duplicate/empty sections, filename
  mismatches — split into *cheap certainties* and *expensive judgments*.
- **Why AI, and its sharper risk.** A model's reach (reads everything,
  cross-references, knows the corpus) is the asset; its confident fabrication is
  the liability. **A missed defect is a cost; a confident error is a hazard.**
- **The chosen shape: emit, don't apply.** Spec Critic produces structured edit
  *instructions* — rendered in the Word report and written to a
  `<report-stem>.edits.json` sidecar for a downstream applier — but **never mutates
  a spec.** The surgical write-back stack was removed in **v3.0.0**, for reasons of
  professional accountability and the asymmetry of an applied-but-wrong edit.
- **The throughline.** Because a stamped compliance document is unforgiving of
  false confidence, the architecture is bent toward making uncertainty *visible* —
  via deterministic-first checks, grounded verdicts, a nine-label trust model, and
  a forensic trace. The chapters that follow are that posture, turned into code.
