# Agent Prompt — Chapter 8: Cross-Spec Coordination

**Full title:** *Cross-Spec Coordination*

## Your mission
Explain the pass that catches defects **no single-spec review can see**: conflicts
*between* specifications — a value defined one way in the HVAC section and another
in the controls section, a responsibility that falls in the gap between two
divisions, a reference in spec A to a section that spec B renamed. This is the
"coordination" problem in construction documents, and the chapter explains how
the program chunks a large project by CSI division to make it tractable.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (facts: CSI chunking Div 21/22/23/Controls/25+01),
   §7 (glossary: cross-check / coordination).
2. `CLAUDE.md` — the cross-check entries in the source layout and high-level flow;
   any cross-check chunking notes.
3. Source you own — `src/cross_check/cross_checker.py`:
   `run_cross_check`, `run_chunked_cross_check`, `_build_cross_check_input`,
   `_cross_system_prompt`, `_csi_prefix`, `_assign_chunk`, `_chunk_label`,
   `_group_specs_by_chunk`, `_filter_findings_for_chunk`,
   `_label_finding_with_chunk`, `_synthesize_chunk_findings`,
   `_sanitize_narrative`.
4. `STRUCTURAL_AUDIT.md` P1-1 (cross-check findings have no `finding_id`, never
   deduped) and P2-3 (the "parallel vs sequential" doc-drift). `TRUST_AUDIT.md`
   P1-3 (chunking can't drop/mis-attribute coordination findings; cross-division
   issues split across chunks).

## In scope (what you own)
- **The coordination problem.** Why cross-spec conflicts are invisible to a
  per-spec reviewer and why they're high-value on K-12 projects (RFIs, change
  orders, field conflicts). Concrete examples.
- **Chunking by CSI division.** Why a whole project won't fit (or shouldn't go)
  in one context window; how specs are assigned to chunks
  (`_csi_prefix`/`_assign_chunk`) — Div 21 (fire), 22 (plumbing), 23 (HVAC),
  Controls, 25 + 01 — and how each chunk is reviewed with its own specs plus the
  existing per-spec findings as context.
- **The cross-check call & synthesis.** The cross-check system prompt's framing;
  how findings from chunks are labeled (`_label_finding_with_chunk`) and
  synthesized (`_synthesize_chunk_findings`); narrative sanitization.
- **When it runs / when it's skipped.** It's optional; large projects chunk; the
  status (skipped/failed) is surfaced later (defer the banner to Ch 11).

## Explicitly OUT of scope (owned elsewhere)
- Dedup and finding-id assignment for cross-check findings → **Ch 7** (you should
  *state the consequence* — cross-check findings currently carry `finding_id=""`
  and aren't deduped — but the fix belongs to the spine chapter; cross-reference).
- Verification of cross-check findings → **Ch 9/10**.
- The batch wrapper → **Ch 6** (note cross-check uses a synchronous call, not the
  batch path, and why).
- Report rendering of the coordination section / status → **Ch 11**.

## Narrative beats to hit
- *Why chunking is a compromise.* A single division-boundary conflict that spans
  two chunks (e.g., a 22↔23 plumbing/HVAC handoff) may be split across chunk
  boundaries and missed — name this as a known limitation (Audit TRUST P1-3) and
  explain the trade (context-window tractability vs. completeness). Be honest
  that the chunking is a heuristic.
- *The doc-drift story (Audit P2-3).* `CLAUDE.md`'s high-level flow once said
  cross-check runs "in parallel with verification," but the batch flow actually
  runs it **sequentially** (review → verify → cross-check → verify-cross-check).
  Sequential is *safer* (no shared-`Finding` race). Tell this as a small but
  instructive example of documentation drifting from code — and resolve it: write
  what the code does, and note the discrepancy (per `HANDBOOK_PLAN.md` §5
  conflict rule).
- *Traceability gap.* Coordination edits reaching the sidecar with empty
  `finding_id` (Audit P1-1) — a real downstream-applier problem; cross-reference
  the spine chapter for the fix.

## Invariants & facts you MUST get right
- Chunking groups: Div 21 / 22 / 23 / Controls / 25 + 01.
- Cross-check findings currently get `finding_id=""` and are not deduped (P1-1).
- The batch flow runs cross-check **sequentially**, not in parallel with
  verification (P2-3) — write the code's behavior.

## Diagrams & tables
- A diagram: all specs → group by CSI division into chunks → per-chunk cross-check
  (with per-spec findings as context) → labeled + synthesized coordination
  findings.
- A table mapping CSI division → chunk → example coordination defects.

## Cross-references to make
- To **Ch 7** (dedup/id gap, where it would be fixed), **Ch 9/10** (verification),
  **Ch 11** (coordination section + status banner), **Ch 16** (the chunking/doc-
  drift audit items).

## Deliverable
- Write to **`handbook/08_cross_spec_coordination.md`**. H1 = the full title.
  Target **3,000–4,500 words**.

## Quality bar
- A reader understands the coordination problem, why chunking is necessary and
  imperfect, and the known traceability/completeness edges. The parallel-vs-
  sequential question is resolved in favor of the code.
