# Implementation Plan: `datacenter_fire` Review Module

**Status: NOT IMPLEMENTED. This is a work order for a future coding agent.**

This document specifies how to add the second review module — fire-suppression
specifications for hyperscale data-center projects — to Spec Critic's module
registry. The engine work is already done (Phases 0–5 of the module-extraction
refactor, PRs #290–#294): the review pipeline, prompts, deterministic
detectors, verification routing, cross-check chunking, GUI, and report are all
driven by validated module data. **Adding this module requires no engine
changes.** If you find yourself editing anything outside the files listed in
§2, stop and re-read §8.

---

## 1. Read these first (in order)

1. `CLAUDE.md` — the "Module registry (module-extraction refactor)" section
   and the "Verification Routing" tables. These describe the contracts you
   must not break.
2. `src/modules/base.py` — `ReviewModule` and its companion dataclasses
   (`DetectorVocabulary`, `ProfileKeywords`, `ChunkGroup`) plus
   `validate_module_registry`. **Every validation rule in that file runs at
   import; your module must pass all of them or the app will not start.**
3. `src/modules/california_k12_mep.py` — the reference implementation. Your
   file should have the same shape and comment discipline.
4. `src/core/code_cycles.py` — `CodeCycle` / `BaseCode` / `StandardEdition`,
   including the provenance rules in the `StandardEdition` docstring.
5. `docs/standards_provenance.md` — the provenance ledger format your
   research must extend.
6. `tests/test_module_registry.py` and `tests/test_golden_domain_surfaces.py`
   — how modules are tested and how golden pins work.

## 2. Files you will create or touch

| File | Change |
|---|---|
| `src/modules/datacenter_fire.py` | **New.** The module definition (the bulk of the work). |
| `src/modules/registry.py` | Add the module to `_ALL_MODULES`. Nothing else — the GUI selector, the `module_for_cycle` bridge, resume persistence, and report surfaces pick it up automatically. |
| `docs/standards_provenance.md` | New per-module section recording every pinned edition's source, date checked, and confidence. |
| `tests/test_module_registry.py` | Update the two registry-shape pins (`AVAILABLE_MODULES` equality; module count) and add datacenter-specific behavior tests (§6). |
| `tests/test_golden_datacenter_surfaces.py` + `tests/goldens/dc_*` | **New.** Golden byte-pins for the new module's assembled prompts (§6). |
| `evals/calibration/fixtures/` | New fixtures exercising the module's verification paths (§7). |

## 3. The code basis (`cycle=`) — research-heavy, do this first

### 3.1 Jurisdiction decision (make it explicitly, document it in the module docstring)

Hyperscale data centers are built across many states (Virginia, Oregon, Texas,
Georgia, Arizona, …), each adopting I-codes on its own schedule with its own
amendments. California had one statewide basis; this module does not. The
**v1 recommendation** is:

- Pin the **model codes** — IBC and IFC, current editions — as the base codes,
  not any single state's amended versions.
- State/local amendments and the specific AHJ are per-project facts: instruct
  users (in the module `description` and the review categories) to put the
  governing state code and amendments into **Project Context**, which ships on
  every review, cross-check, and verification call.
- If the sponsoring user later wants a state-pinned variant (e.g. Virginia
  USBC), that is a *separate module* with its own cycle label — do not try to
  make one module multi-jurisdictional.

### 3.2 `CodeCycle` shape

```python
CodeCycle(
    label="dc-ibc-2024",          # MUST be registry-unique (validated). The label
                                   # namespaces the verification cache — never reuse
                                   # "2025" or any other module's label.
    base_codes=(
        # PRIMARY FIRST: primary_code_year is the stale-detector target.
        BaseCode("ibc", "IBC", "2024", source="..."),
        BaseCode("ifc", "IFC", "2024", source="..."),
    ),
    asce7="7-22",                 # verify which edition IBC 2024 Ch. 35 references
    asce7_previous="7-16",
    standards=(...),              # §3.3
)
```

`BaseCode.key` values become the template placeholders for this module's
prompt line slots (`{ibc}`, `{ifc}`, plus the engine-provided `{asce7}` /
`{asce7_prev}` / `{pinned_standards}`). Registration format-checks every
template slot against the module's own cycle, so a placeholder the cycle
doesn't provide fails at startup.

### 3.3 Standards to research and pin (as `StandardEdition` entries)

Research the editions **referenced by the pinned IBC/IFC edition** (IBC
Chapter 35 / IFC Chapter 80 referenced-standards tables) — not simply the
newest NFPA publication. Likely set:

- NFPA 13 (sprinklers), NFPA 14 (standpipe), NFPA 20 (fire pumps),
  NFPA 22 (water tanks), NFPA 24 (private service mains), NFPA 25 (ITM),
  NFPA 72 (alarm/detection — VESDA/aspirating detection lives here)
- NFPA 75 (IT equipment protection) and NFPA 76 (telecom) — often invoked by
  owner standards even where not code-mandated; pin the current editions and
  say so in `note`/`source`
- NFPA 2001 (clean agent) and NFPA 855 (energy storage / BESS — battery rooms
  are a live issue in data-center fire protection)
- UL listings only if the review categories you write actually reference them

**FM Global data sheets** (e.g. loss-prevention data sheets frequently invoked
by FM-insured hyperscalers) are *guidance documents with revision dates, not
adopted-code editions*. Do **not** force them into `StandardEdition` entries;
represent FM as (a) a jurisdictional keyword, (b) a top source tier in
`verifier_source_priorities`, and (c) a review category ("requirements
attributed to FM Global data sheets that conflict with NFPA minimums or lack a
data-sheet citation").

### 3.4 Provenance discipline (non-negotiable)

Follow `StandardEdition.source` rules exactly as the California module does:

- Every entry's `source` names where the edition was confirmed and when.
- Anything you could only corroborate via web snippets gets the
  `UNVERIFIED (web-researched YYYY-MM): …` prefix **and** a row in
  `docs/standards_provenance.md`.
- ICC's referenced-standards tables are frequently paywalled; if you cannot
  read the primary table, say so in the source string (the CA module's UL
  entries show the expected wording).
- Enumerate all `UNVERIFIED` entries in your PR body. Do not silently ship
  unverified editions as verified.

## 4. The module definition, slot by slot

Work through `ReviewModule` in the same order as `california_k12_mep.py`.
Registration validates: non-empty slots, template formatting, detector
vocabulary consistency (including `plausible_cycle_years ⊆ valid_cycle_years`
and pattern compilability), profile keywords, chunk-group uniqueness, and the
few-shot examples against the real parser contract.

- **`module_id`**: `"datacenter_fire"`. Persisted into resume state and trace
  metadata — treat as permanent once shipped.
- **`display_name` / `description` / `report_context_phrase` /
  `report_title`**: e.g. "Hyperscale Data Center — Fire Suppression";
  description should tell the user to put the governing state code + AHJ
  into Project Context; report phrase e.g.
  `"hyperscale data-center fire protection projects"`; report title e.g.
  `"Spec Critic — Fire Protection Specification Review Report"` (the
  exported report's Heading-0 title — the CA module's says "M&P").
- **`reviewer_persona`**: fire-protection reviewer; project context =
  hyperscale data-center new-build / fit-out under IBC/IFC with FM-insured
  owners; AHJ = local fire marshal.
- **`review_severity_definitions`** (protocol severity NAMES are fixed —
  CRITICAL/HIGH/MEDIUM/GRIPES; you write the anchors): CRITICAL = life-safety
  or permit-blocking (fire marshal / plan-review rejection), FM non-approval,
  or protection gaps in occupied white space; HIGH = must fix before issue
  (e.g. pre-action sequence contradicts detection zoning); MEDIUM =
  meaningful with moderate impact (superseded edition citation); GRIPES =
  editorial.
- **`review_categories_template`** (~12–17 numbered items). Draft list —
  refine against real specs, keep the code-edition item using the
  placeholders:
  1. Internal contradictions within the spec.
  2. Code edition misalignment against IBC {ibc} / IFC {ifc} / ASCE {asce7} and the pinned standards ({pinned_standards}).
  3. References to withdrawn/nonexistent standards or test methods.
  4. Pre-action system logic: double-interlock vs. detection zoning vs. releasing-panel sequence consistency.
  5. Detection coordination: aspirating (VESDA-type) vs. spot detection vs. NFPA 72 zoning and the releasing sequence.
  6. Water supply and fire pump arrangements: capacity, redundancy (N+1), tank sizing, churn/test provisions.
  7. Hydraulic design criteria: occupancy/commodity classification, density, remote area, hose allowance — internally consistent and consistent with schedules.
  8. Clean agent / alternative suppression (NFPA 2001) vs. sprinkler scope boundaries.
  9. Battery / BESS rooms: NFPA 855 alignment, ventilation/detection interfaces.
  10. FM Global data-sheet requirements: cited without data-sheet numbers, or conflicting with NFPA minimums.
  11. Corrosion / nitrogen inerting provisions vs. pipe material and ITM requirements.
  12. Seismic bracing responsibility and criteria (ASCE {asce7}).
  13. Ceiling/obstruction coordination: cable tray, busway, containment vs. sprinkler clearances.
  14. Commissioning / ITM handoff: NFPA 25 responsibilities, phased fit-out boundaries.
  15. Cross-references to Division 28 (detection/alarm) and Division 26 that the author should verify.
  16. Warranty / submittal / O&M conflicts.
- **`review_confidence_high_example`**: e.g.
  `an explicit stale "2015 IBC" citation` (rubric bands are engine protocol;
  you supply only this example).
- **`review_examples`**: four examples mirroring the CA shapes — valid EDIT
  (stale code-cycle citation), valid ADD (verbatim anchor + insertPosition),
  REPORT_ONLY (coordination, e.g. detection zoning vs. releasing sequence),
  and a DO-NOT-REPORT negative (generic Division 21 boilerplate). Rules
  enforced at registration: every JSON example must survive
  `reviewer.validate_edit_shape` (no demotable EDIT/ADD, no no-op EDIT),
  severities/actions in the closed sets, confidence in [0,1], and **no**
  `evidenceElementId` or `<para`/`<row`/`<heading` mentions (cached-prefix
  rule).
- **`cross_check_persona` / `cross_check_severity_definitions`**: coordination
  reviewer for data-center fire-protection packages; CRITICAL anchor =
  cross-spec contradiction causing construction conflict or fire-marshal
  rejection (e.g. two sections assigning releasing-panel programming to
  different parties).
- **`verifier_persona`**: verification assistant for data-center fire
  protection under IBC/IFC.
- **`verifier_source_priorities`** (tiers are module data; the surrounding
  framing text is engine): suggested tiers — 1. nfpa.org;
  2. codes.iccsafe.org / up.codes / iccsafe.org; 3. fmglobal.com (data
  sheets); 4. ul.com / fmapprovals.com; 5. state fire marshal / AHJ sites;
  6. manufacturer technical data (vikinggroupinc.com, tyco-fire.com /
  johnsoncontrols.com, reliablesprinkler.com, victaulic.com, pottersignal.com,
  xtralis.com, ansul.com, kiddefiresystems.com); 7. industry associations
  (sfpe.org, afsa.org, nfsa.org); 8. archive.org.
- **Code-basis line slots** (`review_user_code_basis_line`,
  `cross_check_code_basis_line`, `verifier_system_code_basis_lines`,
  `verifier_user_code_basis_lines`): e.g.
  `"Current code basis: IBC {ibc}, IFC {ifc}, ASCE {asce7}."` — you own the
  display labels and which codes each surface names; multi-line slots use
  `\n` (the verifier system slot is spliced line-by-line).
- **`detector_vocabulary`**:
  - `code_abbreviations=("IBC", "IFC", "IEBC", "IFGC")` — year-adjacent
    citation forms ("2018 IBC" / "IBC 2018").
  - `plausible_cycle_years=("2009","2012","2015","2018","2021","2024")` —
    real I-code editions (stale candidates); `valid_cycle_years` = the same
    plus `"2027"`. Subset rule is validated.
  - `asce7_plausible_editions` — same whitelist as CA unless research says
    otherwise.
  - `stale_cycle_extra_patterns` — long-form citations; must capture the year
    as group 1, e.g. `r"\b(20\d{2})\s+International\s+(?:Building|Fire)\s+Code\b"`.
  - `flag_leed_references=False` — **LEED is genuine scope for data centers**,
    not a copy/paste error; the LEED detector must not fire for this module.
  - `jurisdiction_label=""` — renders the generic
    "Invalid code cycle year (…)" alert wording (there is no single
    jurisdiction to name). Known residual: the verification prescreen's
    local-skip keyword list is still engine-global and contains "leed" /
    "invalid california code cycle"; harmless here (GRIPES-only, telemetry
    flag), noted for a future cleanup.
- **`profile_keywords`**:
  - `jurisdictional`: "fire marshal", "authority having jurisdiction", "ahj",
    "fm global", "factory mutual", "fm approved", "insurer", "state fire
    code", "local amendment", "plan review". (CRITICAL findings matching
    these route straight to Opus deep reasoning — pick terms accordingly.)
  - `manufacturer`: brand + product-data terms (viking, tyco, reliable,
    victaulic, potter, xtralis, vesda, ansul, kidde, fike, notifier,
    "model number", "datasheet", "data sheet", "submittal", "listed
    product", "or approved equal", …).
  - `code_standard`: "ibc", "ifc", "nfpa", "ul ", "ul-", "astm", "asme",
    "ansi", "asce", "fire code", "building code", "code section",
    "standard", …
  - `internal_coordination`: reuse the CA generic set (placeholder / tbd /
    typo / duplicate / internal contradiction / formatting / self-referen…)
    **minus `"leed"`** — LEED findings here are substantive, not internal
    noise.
- **`cross_check_chunk_groups`**: suggested —
  `ChunkGroup("div_21", "Division 21 — Fire Suppression", ("21",))`,
  `ChunkGroup("div_28", "Division 28 — Fire Detection & Alarm", ("28",))`,
  `ChunkGroup("div_22", "Division 22 — Plumbing / Water Supply", ("22",))`.
  Unmatched prefixes pool into the engine's reserved `general` chunk;
  remember chunked runs are within-chunk-only coordination (documented
  engine limitation).

## 5. Registration

Add the import + entry in `src/modules/registry.py`:

```python
_ALL_MODULES: tuple[ReviewModule, ...] = (
    CALIFORNIA_K12_MEP,
    DATACENTER_FIRE,
)
```

That is the entire wiring. Verify with `python -c "import src.modules"` —
any contract violation raises `ValueError` at import with a pointed message.
The GUI selector, `module_for_cycle` bridge, `pending_batch.json` resume,
trace metadata, and the report surfaces — title (`report_title`), the
"Code Cycle:" metadata line, the methodology note (`report_context_phrase`
+ the jurisdiction-worded cycle sentence), the pinned-editions paragraph
(rendered from the module's own cycle), and the stale/invalid-cycle alert
headings (worded from `jurisdiction_label` + `plausible_cycle_years`) —
all follow automatically.

## 6. Tests (all hermetic; no API key)

1. **Do not touch** `tests/goldens/*` (the CA pins) or weaken any validation
   to make your content fit. The full suite must stay green — the CA goldens
   byte-identical — with your module registered.
2. Update the two registry-shape pins in `tests/test_module_registry.py`
   (`AVAILABLE_MODULES` equality and the default-module assertions gain the
   new entry; `DEFAULT_MODULE` stays `california_k12_mep`).
3. **New golden file**: `tests/test_golden_datacenter_surfaces.py` mirroring
   `test_golden_domain_surfaces.py` — byte-pin the DC reviewer system prompt,
   both user-message shapes, cross-check system prompt, verifier system
   prompts (±verdict tool), and a preprocessor-alert JSON for a DC fixture
   spec (exercise: stale "2018 IBC", invalid "2019 IBC", long-form
   "2015 International Building Code", ASCE 7-10, **no LEED alert** despite a
   LEED mention, generic invalid-year wording). Use the same
   `SPEC_CRITIC_UPDATE_GOLDENS=1` regeneration mechanism.
4. **Routing behavior**: CRITICAL finding mentioning "fire marshal" or
   "FM Global" → `JURISDICTIONAL` profile + `DEEP_REASONING` via
   `select_routing(cycle=DATACENTER_FIRE.cycle)`; the same finding under the
   CA cycle classifies differently. Chunk assignment: "28 31 00 …" →
   `div_28`; "23 …" → `general`.
5. **Report surface**: `_write_methodology_note(doc, module=DATACENTER_FIRE)`
   renders the DC phrase, a jurisdiction-free cycle sentence ("This review
   used {label} code cycle references."), and the DC cycle's own pinned
   editions (never California's); `_write_title_block` renders
   `report_title` and a bare "Code Cycle: {label}" line;
   `_write_alerts` renders "Stale Code Cycle References" /
   "Invalid Code Cycle Years" with the DC year list.

## 7. Calibration fixtures

Add ≥6 fixtures under `evals/calibration/fixtures/` following the README
there (category = verification profile, e.g. `jurisdictional` /
`code_standard`), mirroring the CA patterns: a true-positive stale-IBC
CORRECTED, a jurisdictional CRITICAL that should ride the deep-reasoning
path, a budget-exhausted UNVERIFIED, and negatives. Run the calibration
scorer and include the summary in the PR.

## 8. Hard constraints (violating any of these means the change is wrong)

- **No engine edits.** Prompts' protocol text, parsers, `validate_edit_shape`,
  grounding invariant, routing precedence, chunking invariants, cache keys,
  and schemas are all off-limits. The module is data.
- **CA goldens byte-identical; CA routing pins unchanged.** Registering a new
  module must not perturb the existing module in any way.
- **Cycle label uniqueness** — pick a fresh label; it namespaces the
  verification cache and backs the `module_for_cycle` bridge.
- **Provenance or `UNVERIFIED`** on every pinned edition; enumerate
  `UNVERIFIED` entries in the PR body.
- **Do not relax `validate_module_registry`** to accommodate content; fix the
  content.
- Full suite green; GUI stub-import check if tkinter is unavailable in the
  environment (see the Phase 1/5 PR descriptions for the technique).

## 9. Suggested commit/PR structure

1. Commit 1 — code basis + provenance doc (research artifacts reviewable on
   their own).
2. Commit 2 — the module definition + registration + tests + goldens.
3. Commit 3 — calibration fixtures + scorer results.
One PR against `master`; PR body lists every `UNVERIFIED` edition and the
jurisdiction decision from §3.1.
