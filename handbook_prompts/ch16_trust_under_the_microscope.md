# Agent Prompt — Chapter 16: Trust Under the Microscope — The Audits

**Full title:** *Trust Under the Microscope: The Audits*

## Your mission
Tell the story of how the team **stress-tested its own trustworthiness**. Two
formal audits exist in the repo — one asking "are the trust-critical *leaf*
functions correct?" and one asking "is the *spine* sound enough that correct leaf
output can't be silently dropped or misrepresented?" This chapter is the
consolidated narrative of **inherent challenges and what's still being
perfected**: the audit *method*, the headline findings, the defense-in-depth the
audits confirmed, and the honest list of open issues. It is the book's clearest
answer to "can I trust this program, start to finish?"

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §1 (the trust throughline), §6 (facts).
2. **`TRUST_AUDIT.md`** (in full) — the leaf-correctness audit.
3. **`STRUCTURAL_AUDIT.md`** (in full) — the spine audit.
4. Skim the chapters that own each cited subsystem so your cross-references are
   accurate (Ch 4, 5, 7, 8, 10, 11, 12).

## In scope (what you own)
- **Why audit at all.** Frame it from the trust mandate: a compliance tool earns
  trust only if someone has tried hard to break that trust. Introduce the two
  audits as complementary lenses — *leaf correctness* (do grounding, edit
  proposals, detectors, status classification do the right thing in isolation?)
  vs. *spine correctness* (orchestration, joins, state, error handling, and the
  honesty of the final artifact).
- **The method.** Both audits used the same approach: **three parallel code
  sweeps, then a careful personal re-read** of the trust-critical paths to
  separate real issues from noise. Crucially, **several sub-agent "CRITICAL"
  claims were false alarms** in both audits — tell this honestly; it's a lesson in
  not trusting an automated sweep without verification (a nice mirror of the
  product's own grounding philosophy).
- **The defense-in-depth that held (the "verified-clean" story).** Synthesize the
  reassuring findings: the grounding gate / URL matching is sound (fabricated URLs
  can't match; independent re-check in `classify_status`); dedup won't falsely
  merge distinct edits (full-text SHA-256 digests in the key); the verdict-to-
  finding join can't bind to the wrong finding (dedup before verify, stable
  index); batch results are reconciled against the submitted set; the extraction
  cache key is robust; the on-disk caches are written atomically; GUI threading is
  sound; truncated review JSON is salvaged. This is the chapter's good news —
  present it as evidence the core is genuinely well-built.
- **The headline gap.** Structural P0-1: a *partially failed* run is not made
  obviously distinguishable from a *fully clean* one in the final deliverable —
  "Files Reviewed: 5" even when 2 silently failed; the UI shows green/"success";
  the data exists (`truncated_specs`) but isn't surfaced. For a compliance tool,
  this is the single most important thing still being perfected. Explain it as a
  *surfacing* gap, not a data-loss bug.
- **The other open items**, grouped and prioritized (with cross-references to the
  owning chapter where the fix lives):
  - Sidecar under-emission for multi-file defects (Trust P0-1/P0-2 → Ch 7/11).
  - Cross-check findings lack `finding_id` and aren't deduped (Structural P1-1 →
    Ch 7/8).
  - Batch→real-time fallback handoff needs an end-to-end proof of "exactly one
    terminal result" (Structural P1-2 → Ch 10).
  - Model-capability whitelist staleness degrades a newer model; the 300k beta
    header is checked for presence not acceptance (Trust P0-3/P0-4 → Ch 12).
  - Extraction completeness: headers/footers/text-boxes/footnotes may be
    unextracted (Trust P0-6 → Ch 4).
  - Batch grounding parity with real-time — likely fine, "must be proven" given
    the trust bar (Trust P0-5 → Ch 10).
  - Minor/hardening items (continuation off-by-one, doc-drift, recorder reset).
- **The trust-model caveat to the user (not a bug).** State it plainly: a
  `VERIFIED_SUPPORTED`/`CONFIRMED` verdict only guarantees the cited URL was
  *actually retrieved by the search tool*, **not** that the page demonstrably
  supports the specific code claim. Automated grounding proves the source is
  *real*, not that it *proves the claim*. Human spot-checking of VERIFIED_*
  findings remains warranted. This is the most important caveat in the whole book.

## Explicitly OUT of scope (owned elsewhere)
- Re-explaining each subsystem's mechanics → its owning chapter (you reference
  them; this chapter is about the *audit lens* and the consolidated picture).
- The *fixes* themselves — these are open items; describe them, don't pretend
  they're done. (If an agent notices a fix has since landed in the code, write
  what the code says and note it per `HANDBOOK_PLAN.md` §5.)

## Narrative beats to hit
- *Honesty as a feature.* The very existence of these audits — and the fact that
  they're kept in the repo, including the "we were wrong about X" entries — is
  part of the program's trust story.
- *Where the risk really lives.* The core verification/grounding engine is the
  strong part; the higher-risk surface is the **edges**: the honesty of the
  artifact and the completeness of emitted edits. Make this inversion explicit —
  it surprises people who assume the LLM is the weak link.
- *A prioritized agenda, not a panic.* Frame the open items as a credible backlog
  with clear payoff ordering (surfacing partial failure first), echoing the
  audits' own "suggested sequencing."

## Invariants & facts you MUST get right
- Both audits: 3 parallel sweeps + personal re-read; false alarms were filtered.
- The data plane is honest (joins sound, nothing silently dropped); the gap is
  *surfacing* partial failure, not losing data.
- The grounding caveat (source real ≠ claim proven).
- Attribute each open item to the correct audit and priority (P0/P1/P2) and the
  owning chapter.

## Diagrams & tables
- A **two-audit comparison** table (lens | question | headline finding).
- A prioritized **open-items table** (item | audit/priority | owning chapter |
  one-line status).
- A **"verified-clean" table** (the defenses that held).

## Cross-references to make
- To every chapter that owns an open item (4, 7, 8, 10, 11, 12), and to **Ch 17**
  (how the program got here and where it's going).

## Deliverable
- Write to **`handbook/16_trust_under_the_microscope.md`**. H1 = the full title.
  Target **3,500–5,000 words**.

## Quality bar
- A reader finishes knowing exactly how much to trust the program and where its
  honest edges are. Every audit finding is represented accurately and attributed.
  The grounding caveat is unmistakable. Reassurance and candor are balanced.
