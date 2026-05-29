# Agent Prompt — Chapter 11: The Trust Model & Report Output

**Full title:** *The Trust Model & Report Output: Status Labels, the Word Report & the Edit Sidecar*

## Your mission
Explain how everything the pipeline learned about a finding becomes a **trustworthy
artifact**: the nine-label trust model, the two edit-action labels, the Word
report (its structure, the Run Diagnostics banner, the per-finding evidence
panel, the methodology note), and the machine-readable `edits.json` **sidecar**
that hands structured edit instructions to a future applier. This is where the
program's caution becomes *visible to a human reviewer* — and where the
emit-but-don't-apply contract is fulfilled.

## Read first (in order)
1. `HANDBOOK_PLAN.md` — §6 (the 9 statuses, 2 labels, emit-not-apply), §7.
2. `CLAUDE.md` — **§4 Trust Model / Report Output** (the full status & label
   tables), "Edit-action labels," "Run Diagnostics banner," "Per-finding evidence
   panel," "Pinned standards editions" (the report methodology note), the report
   portions of "Escalation disagreement surfacing" and "Budget-exhaustion
   sentinel," and "Cache-replay visibility" (the age badge).
3. Source you own:
   - `src/output/report_status.py` — `ReportStatus` (9), `EditActionLabel` (2),
     `classify_status` (branch order!), `classify_edit_action`,
     `is_budget_exhausted`, `summarize_*`, the glyph/color/shading maps and
     `STATUS_DISPLAY_ORDER`.
   - `src/output/report_exporter.py` (~2,066 lines) — the report structure:
     `_write_title_block`, `_summarize_run_diagnostics` + `_write_run_diagnostics_banner`,
     `_write_methodology_note` + `_render_pinned_editions_note`, `_write_summary_table`,
     `_write_trust_model_summary`, `_write_alerts`, `_write_finding_entry`,
     `_write_evidence_panel`, `_write_cross_check_section`, `export_report`.
   - `src/output/edit_sidecar.py` — `write_edit_instructions_sidecar`,
     `_serialize_edit_proposal` (the flattener), the entry shape.
4. `STRUCTURAL_AUDIT.md` P0-1 (the banner needs a "specs that failed review" row;
   "Files Reviewed" counts submitted, not reviewed). `TRUST_AUDIT.md` P0-1
   (sidecar emits one entry for a multi-file defect; doesn't include
   `affected_files`).

## In scope (what you own)
- **The trust model.** The **nine `ReportStatus`** values as a closed set
  (reproduce the §4 table: when each fires) and the **two `EditActionLabel`**
  values. The crucial **branch order** in `classify_status`: `models_disagreed →
  VERIFIED_CONTESTED` is checked *before* local-skip and verdict branches; and
  the independent grounding re-check. Why `classify_edit_action` is now simply
  "does this finding carry an edit proposal?" — no confidence gate, because the
  app emits and never applies (verification status + `edit_confidence` ride along
  for a downstream applier to gate on).
- **The Word report's anatomy**, top to bottom: title block → **Run Diagnostics
  banner** → methodology note (with the **pinned-editions** enumeration) →
  summary table → trust-model summary → alerts (including the "(deterministic
  check)" section) → per-finding entries → cross-check section. Explain each at
  the level of *what it tells the reviewer and why*.
- **The Run Diagnostics banner.** The operational-health table: edit-suggested /
  report-only counts, cache replays + oldest entry age, **verification failures**
  (red when > 0), parse-time REPORT_ONLY demotions, **spec content extraction
  warnings**, **budget-exhausted findings** (red when > 0), and cross-spec
  coordination status — plus the recovery-hint paragraphs (the failure hint vs.
  the calmer budget-exhaustion hint naming the severity budget knob). All derived
  from existing `Finding`/`VerificationResult` fields (no new persistence).
- **The per-finding evidence panel.** The collapsed "Sources" section: verifier
  model, mode, search budget (with web_fetch count when present), source quote,
  rationale, **escalation history** (+ the contested "manual review recommended"
  sentence and the initial-verifier-sources sub-section when `models_disagreed`),
  the **cache-replay age badge**, accepted vs. rejected source URLs, the
  full-text-fetched sub-section. The inline "Proposed replacement" (existing →
  replacement) that renders above the panel. The budget-exhausted sub-label on
  the status line.
- **The edit sidecar.** The `<report-stem>.edits.json` contract: one entry per
  finding's edit proposal, the flattened fields a downstream applier consumes
  (action / existing / replacement / anchor / target element id / confidence /
  verification status), and that REPORT_ONLY findings carry no proposal. This is
  the concrete realization of emit-but-don't-apply.

## Explicitly OUT of scope (owned elsewhere)
- How a `VerificationResult` (verdict/grounding/telemetry) is *produced* →
  **Ch 10** (you *classify and render* it).
- Routing/mode decisions → **Ch 9**.
- The `Finding`/`EditProposal` data model definition → **Ch 5** (you render it).
- Dedup, multi-file grouping, and the spine-side fix for sidecar fan-out →
  **Ch 7** (you should *state* the current sidecar behavior and the audit gap, but
  the fix lives in the spine).
- `DiagnosticsReport` (the in-memory ops report) → **Ch 14** (distinct from the
  *report's* Run Diagnostics banner, which you own — clarify the difference).

## Narrative beats to hit
- *The artifact is the product.* Everything upstream exists to produce a report a
  reviewer can trust at a glance: the status glyph/color tells them how much to
  believe each finding; the banner tells them whether the *run itself* was
  healthy.
- *Designing for honest uncertainty.* Walk the statuses as a spectrum of trust —
  from `VERIFIED_SUPPORTED` down through `INSUFFICIENT_EVIDENCE`,
  `VERIFICATION_FAILED`, and `VERIFIED_CONTESTED` — and explain why the program
  would rather show "we couldn't confirm this / our models disagreed" than fake
  confidence.
- *The honest edges (the artifact's truthfulness).* Audit STRUCTURAL P0-1: the
  banner has no "specs that failed review" row and "Files Reviewed" counts
  submitted specs, so a partially-failed run can read as clean — the data exists
  (`truncated_specs`) but isn't surfaced here. Audit TRUST P0-1: the sidecar
  under-emits for multi-file defects. Present both as the report layer's most
  important unfinished work (the fixes are surfacing fixes — the spine already
  has the data; cross-ref Ch 7).

## Invariants & facts you MUST get right
- Nine statuses, two labels (exact names from §6).
- `classify_status` checks `models_disagreed` (→ VERIFIED_CONTESTED) *first*.
- Budget-exhausted is a *sub-label*, status stays `INSUFFICIENT_EVIDENCE`.
- `classify_edit_action`: proposal → `EDIT_SUGGESTED`, else `REPORT_ONLY` (no
  confidence gate).
- Banner values derive from existing fields; affected-spec count (not warning
  count) for extraction warnings.
- The sidecar emits edit instructions; nothing is applied.

## Diagrams & tables
- The **nine-status table** (status | glyph/color | when) and the **two-label
  table** — reproduce from `CLAUDE.md` §4.
- A **report anatomy diagram** (the document top-to-bottom).
- A **sample sidecar entry** (trimmed JSON) showing the flattened proposal shape.
- A "trust spectrum" visual ordering the statuses by how much to believe them.

## Cross-references to make
- To **Ch 10** (where verdicts/grounding come from), **Ch 9** (routing/mode shown
  in the panel), **Ch 4** (deterministic alerts + content-loss banner row),
  **Ch 7** (sidecar fan-out + failed-spec data), **Ch 12** (pinned editions),
  **Ch 14** (the *other* diagnostics — the trace/ops report), **Ch 16** (P0-1s).

## Deliverable
- Write to **`handbook/11_trust_model_and_output.md`**. H1 = the full title.
  Target **4,000–5,000 words** (this chapter carries a lot).

## Quality bar
- A reader can read a Spec Critic report fluently: decode any status, know what
  the banner is warning about, and understand the sidecar contract. Status/label
  tables match `CLAUDE.md` §4 and `report_status.py`. The honesty gaps are stated.
