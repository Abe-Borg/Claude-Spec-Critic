"""The hyperscale data-center fire-suppression module (US / Canada).

Fire-suppression (Division 21) specifications
for hyperscale data-center projects, reviewed against the International Building
Code and International Fire Code as base model codes. Fire-alarm and releasing
knowledge remains here for suppression-interface review and legacy pending-run
compatibility; new Division 28 fire-alarm specifications route to the dedicated
electronic safety and security module.

WS-1 of ``docs/hyperscale_datacenter_module_plan.md`` shipped the *module
data* half; **WS-5 turns the location-aware features on** — this module now
sets ``project_profile_enabled=True`` and carries the research /
compliance / wrong-polity content those phases consume (§§5.9–5.13 of the
plan). With the flag on, selecting this module makes the GUI collect the
project city / state-or-province / country / client, runs the
requirements-research fan-out before review submission, evaluates the
package against the researched requirements in a compliance pass, and lets
verification search the project's own jurisdiction. Operators may still put
additional known facts (AHJ correspondence, owner basis-of-design) into
**Project Context**; the research phase supplements, not replaces, that
input. Every location-aware behavior is engine-owned and gated on the flag —
this file is pure module data (no engine edits), and the California module
stays byte-identical because it leaves the flag off.

Jurisdiction decision (``docs/datacenter_fire_module_plan.md`` §3.1): hyperscale
data centers are built across many states and provinces, each adopting the
I-codes on its own schedule with its own amendments. Rather than pin one
jurisdiction, this module pins the **model codes** (IBC / IFC, current
editions) as the code basis; state / provincial / local / AHJ facts are
per-project data. A state-pinned variant (e.g. a Virginia USBC cycle) would be
a *separate* module with its own registry-unique cycle label, never a
multi-jurisdictional cycle.

The pinned standard editions below are the editions the current-edition I-codes
reference (best-grounded against the published 2024 IBC Chapter 35 / IFC
Chapter 80 referenced-standards tables), plus the current editions of the
data-center-relevant standards owner programs invoke (NFPA 75 / 76). Every
entry is flagged ``UNVERIFIED`` — the primary ICC tables are paywalled
(HTTP 403 to automated fetch, same limitation the California cycle documents)
— with per-entry provenance in ``docs/standards_provenance.md``. See
``StandardEdition.source`` for the machine-readable flag and
``cycle.unverified_standards()`` for the list.

The goldens in ``tests/test_golden_datacenter_surfaces.py`` pin the assembled
DC prompts byte-exactly, mirroring the California goldens; the California
goldens stay byte-identical (this module touches no engine file).
"""
from __future__ import annotations

from ..core.code_cycles import BaseCode, CodeCycle, StandardEdition
from .base import (
    ChunkGroup,
    DetectorVocabulary,
    PolityTokenRule,
    ProfileKeywords,
    ResearchDimension,
    ReviewModule,
)

# ---------------------------------------------------------------------------
# Code basis: current-edition I-codes (model codes, not any single state's
# amended version). Defined here — not in ``core/code_cycles.py`` (which is the
# California cycle table) and NOT registered in ``AVAILABLE_CYCLES`` — because
# the module carries its own cycle directly. ``label`` is registry-unique
# (validated); it namespaces the verification cache and backs the
# ``module_for_cycle`` bridge, so it must never collide with California's
# ``"2025"``.
# ---------------------------------------------------------------------------

# Best-grounded but UNVERIFIED against the primary (paywalled) referenced-
# standards tables — provenance in docs/standards_provenance.md. The
# web-research month is recorded so a maintainer knows how stale the pass is.
_DC_RESEARCH = "web-researched 2026-07"

DATACENTER_IBC_2024 = CodeCycle(
    label="dc-ibc-2024",
    base_codes=(
        # Primary code first — the stale-cycle detector compares found years
        # against this entry's year. IBC/IFC 2024 are the current published
        # I-code editions (a matter of public record — ICC published them).
        BaseCode("ibc", "IBC", "2024", source="ICC 2024 International Building Code (current published edition)"),
        BaseCode("ifc", "IFC", "2024", source="ICC 2024 International Fire Code (current published edition)"),
    ),
    # The 2024 IBC references ASCE 7-22, replacing ASCE 7-16 (well corroborated
    # by ASCE / StructureMag / SEAO; the detector does edition arithmetic on
    # these two fields).
    asce7="7-22",
    asce7_previous="7-16",
    standards=(
        # --- Water-based suppression (2024 IBC Ch. 35 / IFC Ch. 80) --------
        # The 2024 I-codes reference the 2022 NFPA installation family for the
        # sprinkler / pump / mains / alarm standards; 13/20/24/72 are strongly
        # corroborated across secondary sources, 14/22 are less certain (newer
        # editions exist that the code's reference freeze predates).
        StandardEdition(
            "NFPA 13", "2022",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): the 2024 IBC/IFC reference NFPA "
                "13-2022 (well corroborated — the 2024 Life Safety Code and "
                "multiple secondary sources cite NFPA 13-2022). Confirm against "
                "the published 2024 IBC Ch. 35 table. See docs/standards_provenance.md."
            ),
        ),
        StandardEdition(
            "NFPA 14", "2019",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): the 2024 IBC's reference freeze "
                "predates NFPA 14-2024, so it most likely references NFPA "
                "14-2019 — but NFPA 14-2024 exists and this may have moved. "
                "Verify against the published 2024 IBC Ch. 35 table. "
                "See docs/standards_provenance.md."
            ),
        ),
        StandardEdition(
            "NFPA 20", "2022",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): the 2024 IBC/IFC reference NFPA "
                "20-2022 (good secondary corroboration). Confirm against the "
                "published 2024 IBC Ch. 35 table. See docs/standards_provenance.md."
            ),
        ),
        StandardEdition(
            "NFPA 22", "2018",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): the 2024 IBC most likely "
                "references NFPA 22-2018 — NFPA 22-2023 exists and postdates the "
                "code's reference freeze. Verify against the published 2024 IBC "
                "Ch. 35 table. See docs/standards_provenance.md."
            ),
        ),
        StandardEdition(
            "NFPA 24", "2022",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): the 2024 IBC/IFC reference NFPA "
                "24-2022 (good secondary corroboration). Confirm against the "
                "published 2024 IBC Ch. 35 table. See docs/standards_provenance.md."
            ),
        ),
        StandardEdition(
            "NFPA 25", "2020",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): NFPA 25 (ITM) is referenced by the "
                "2024 IFC; the referenced edition is most likely NFPA 25-2020 — "
                "NFPA 25-2023 exists and postdates the reference freeze. The fire "
                "/ operations code's ITM edition often differs from the building "
                "code's install editions. Verify against the published 2024 IFC "
                "referenced-standards table. See docs/standards_provenance.md."
            ),
        ),
        StandardEdition(
            "NFPA 72", "2022",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): the 2024 IBC/IFC reference NFPA "
                "72-2022 (well corroborated — the 2024 Life Safety Code cites "
                "NFPA 72-2022). Confirm against the published 2024 IBC Ch. 35 "
                "table. See docs/standards_provenance.md."
            ),
        ),
        # --- Special-hazard / gaseous suppression --------------------------
        StandardEdition(
            "NFPA 2001", "2022",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): NFPA 2001 (clean agent) — the "
                "2022 edition is the one in the 2024 I-code reference window. "
                "Verify against the published referenced-standards table. "
                "See docs/standards_provenance.md."
            ),
        ),
        # --- Energy storage (BESS) -----------------------------------------
        StandardEdition(
            "NFPA 855", "2023",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): NFPA 855 (stationary energy "
                "storage) is referenced by the 2024 IFC (§1207); the current "
                "edition is 2023 (a 2026 edition is in development). Confirm the "
                "code-referenced edition against the published 2024 IFC table. "
                "See docs/standards_provenance.md."
            ),
        ),
        # --- Data-center / telecom owner-invoked (current editions) --------
        # Frequently invoked by owner standards even where not code-mandated;
        # pinned at their current editions per the module plan.
        StandardEdition(
            "NFPA 75", "2024",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): NFPA 75 (IT-equipment fire "
                "protection) — current edition 2024, often invoked by owner "
                "standards for data centers rather than code-mandated. Pinned at "
                "the current edition. See docs/standards_provenance.md."
            ),
        ),
        StandardEdition(
            "NFPA 76", "2024",
            source=(
                f"UNVERIFIED ({_DC_RESEARCH}): NFPA 76 (telecommunications-"
                "facility fire protection) — current edition 2024 (issued Dec "
                "2023; well corroborated via NFPA/ANSI store listings), owner-"
                "invoked rather than code-mandated. Pinned at the current "
                "edition. See docs/standards_provenance.md."
            ),
        ),
    ),
)


# The review-scope category list. References the placeholders documented by
# :func:`src.modules.base.code_basis_format_kwargs` ({ibc}/{ifc}/{asce7}/
# {pinned_standards}); formatted against the DC cycle at prompt-build time.
_REVIEW_CATEGORIES = """\
1. Internal contradictions within the spec (e.g., conflicting requirements in different articles).
2. Code edition misalignment: the base model codes are IBC {ibc}, IFC {ifc}, ASCE {asce7}. Pinned standard editions for this cycle: {pinned_standards}. Flag references to superseded editions (e.g., ASCE {asce7_prev} instead of {asce7}); where the project context names the governing state/provincial adoption, defer to it for edition checks.
3. References to withdrawn, superseded, or nonexistent standards, sections, or test methods.
4. Pre-action system logic: double-interlock vs. detection zoning vs. releasing-panel sequence consistency.
5. Detection coordination: aspirating (VESDA-type) smoke detection vs. spot detection vs. NFPA 72 zoning and the releasing sequence.
6. Water supply and fire pump arrangements: capacity, redundancy (N+1), tank sizing, churn/test provisions.
7. Hydraulic design criteria: occupancy/commodity classification, density, remote area, and hose allowance — internally consistent and consistent with the schedules.
8. Clean agent / alternative suppression (NFPA 2001) vs. sprinkler scope boundaries.
9. Battery / energy-storage (BESS) rooms: NFPA 855 alignment, ventilation and detection interfaces.
10. FM Global data-sheet requirements: cited without data-sheet numbers, or conflicting with NFPA minimums.
11. Corrosion / nitrogen-inerting provisions vs. pipe material and inspection/testing/maintenance requirements.
12. Seismic bracing responsibility and criteria (ASCE {asce7}); who designs, who approves.
13. Ceiling and obstruction coordination: cable tray, busway, and containment vs. sprinkler clearances and coverage.
14. Commissioning / ITM handoff: NFPA 25 responsibilities and phased fit-out boundaries.
15. Cross-references to Division 28 (fire detection/alarm) and Division 26 (electrical) that the author should verify.
16. Warranty, submittal, and O&M conflicts (what is required, when, in what form).
17. Location- and client-specific requirements: where the project context includes a Project Requirements Profile, verify the specification aligns with the governing codes, local amendments, AHJ requirements, and client standards it lists; flag conflicts with, and omissions of, profile requirements.
18. Master-specification remnants: content from other disciplines or other jurisdictions left in this section — HVAC/plumbing/refrigerant language in a fire-suppression section; another polity's codes, agencies, listing marks, or procurement clauses; another project's identifiers or placeholder tokens (TBD, XXXX); flag for deletion or adaptation.
19. Document integrity: duplicated or out-of-sequence article numbering, empty lettered paragraphs, doubled words, garbled or dangling cross-references, related-section numbers that do not match their titles, and products/execution mismatches within the section."""


# Stable, cacheable few-shot examples. Like the California module's, these must
# not vary with per-spec content (they are part of the cached system-prompt
# prefix keyed by cycle) and must NOT mention ``evidenceElementId`` or
# ``<para id="…">`` — those are per-request concepts enforced at registration.
# Every JSON example is validated against the parser's edit-shape contract at
# registration. The location-aware phrasing ("the current IBC edition adopted
# for this project location") teaches the model the v1 posture: the operator
# supplies the governing adoption via Project Context.
_REVIEW_EXAMPLES = """\
Example 1 — valid EDIT (stale code-cycle reference):
{
  "severity": "MEDIUM",
  "fileName": "21 13 13 Wet-Pipe Sprinkler Systems.docx",
  "section": "1.03",
  "issue": "Spec cites a stale IBC edition rather than the current adopted edition for the project location.",
  "actionType": "EDIT",
  "existingText": "Comply with 2015 IBC Chapter 9.",
  "replacementText": "Comply with the current IBC edition adopted for this project location.",
  "codeReference": "IBC (current adopted edition)",
  "confidence": 0.9
}

Example 2 — valid ADD (insert missing requirement using a verbatim anchor):
{
  "severity": "HIGH",
  "fileName": "21 13 16 Dry-Pipe Sprinkler Systems.docx",
  "section": "1.01",
  "issue": "PART 1 omits a general code-compliance statement naming the governing codes and NFPA 13.",
  "actionType": "ADD",
  "existingText": null,
  "replacementText": "A. All work shall comply with NFPA 13 and the building and fire codes adopted for this project location, including all local amendments.",
  "anchorText": "PART 1 - GENERAL",
  "insertPosition": "after",
  "codeReference": null,
  "confidence": 0.8
}

Example 3 — REPORT_ONLY (cross-discipline coordination, no clean text edit):
{
  "severity": "HIGH",
  "fileName": "21 13 16 Dry-Pipe Sprinkler Systems.docx",
  "section": "3.05",
  "issue": "The pre-action detection zoning in this section conflicts with the releasing-sequence description that references Division 28 fire detection/alarm. Resolve in a fire-protection / fire-alarm coordination meeting and update both sections together.",
  "actionType": "REPORT_ONLY",
  "existingText": null,
  "replacementText": null,
  "anchorText": null,
  "insertPosition": null,
  "codeReference": null,
  "confidence": 0.75
}

Example 4 — DO NOT REPORT (boilerplate and in-scope LEED are not findings):
Generic Division 21 coordination boilerplate such as "Coordinate with related
work specified in other Sections" is not a contradiction, not a code-edition
issue, and not an invalid reference — do not emit a finding for it absent
concrete evidence of a real conflict. Likewise, do NOT flag LEED references as
inappropriate: LEED is genuine scope for data-center projects, not a
copy/paste error.\
"""


_REVIEW_SEVERITY_DEFINITIONS = """\
CRITICAL — life-safety or permit-blocking: protection gaps in occupied or mission-critical white space, fire-marshal or plan-review rejection triggers, a withdrawn or nonexistent standard controlling a life-safety system, a direct conflict with the governing code / a local amendment / an FM Global requirement that would halt approval, or a commercial/procurement conflict that would materially disrupt tender (e.g., an origin- or tariff-exposed sourcing clause).
HIGH — major technical issues requiring correction before the spec can be issued (e.g., a pre-action releasing sequence that contradicts the detection zoning, or fire pump / water supply arrangements that cannot meet the stated demand).
MEDIUM — meaningful issues with moderate impact (e.g., a superseded standard-edition citation that should be updated to the project's adopted edition).
GRIPES — quality/editorial issues that should still be fixed (e.g., inconsistent capitalization of a defined term)."""


_CROSS_CHECK_SEVERITY_DEFINITIONS = """\
CRITICAL — showstoppers: direct contradictions between specs that would cause construction conflicts or fire-marshal rejection (e.g., two sections assigning releasing-panel programming to different responsible parties).
HIGH — major coordination gaps requiring correction before issuing (e.g., Division 28 detection zoning that does not match the Division 21 pre-action zones).
MEDIUM — meaningful cross-reference or consistency issues with moderate impact (e.g., the same equipment given different model numbers in two sections).
GRIPES — minor coordination polish items (e.g., inconsistent cross-reference formatting)."""


# Authoritative-source tiers for the verifier prompt. The surrounding guidance
# (the "Prefer authoritative sources" header and the fallback rules) is engine
# protocol; the tiers below are this module's source-quality policy. Canadian
# authorities are included from day one — the module reviews US and Canadian
# data-center projects.
_VERIFIER_SOURCE_PRIORITIES = """\
1. Standards organizations and code publishers:
   nfpa.org, codes.iccsafe.org, up.codes, iccsafe.org

2. Insurance and listing authorities:
   fmglobal.com, fmapprovals.com, ul.com

3. Government code authorities:
   state fire marshal and building-code agency sites (.gov), municipal code
   portals; for Canadian sites nrc.canada.ca, provincial statute / e-Laws
   portals, provincial fire-marshal communiqués, scc-ccn.ca, csagroup.org

4. Major manufacturer technical data:
   vikinggroupinc.com, tyco-fire.com, johnsoncontrols.com,
   reliablesprinkler.com, victaulic.com, pottersignal.com, xtralis.com,
   ansul.com, kiddefiresystems.com

5. Industry associations:
   sfpe.org, afsa.org, nfsa.org

6. Archived or historical standards:
   archive.org"""


# The deterministic preprocessor's I-code vocabulary. The detector logic (regex
# assembly, span dedup, negation suppression) is engine-owned in
# ``input/preprocessor.py``; these are the domain facts it scans for. NBC/NFC
# (Canadian) years are intentionally NOT added — one shared cycle-year set can't
# hold both I-code and NBC year families without the stale/invalid detectors
# misfiring (documented v1 limitation, ``hyperscale_datacenter_module_plan.md``
# D-10); Canadian deterministic coverage arrives via the profile-gated
# wrong-polity token detector in a later workstream.
_DETECTOR_VOCABULARY = DetectorVocabulary(
    # Abbreviations recognized next to a year ("2018 IBC" / "IBC 2018").
    code_abbreviations=("IBC", "IFC", "IEBC", "IFGC"),
    # Real, published I-code editions in the recent window the stale detector
    # flags (a found year in this set that differs from the primary 2024 alerts).
    plausible_cycle_years=("2009", "2012", "2015", "2018", "2021", "2024"),
    # Every published cycle plus the next anticipated one (2027); a year/code
    # citation outside this set is a typo or fabrication ("2019 IBC").
    valid_cycle_years=("2009", "2012", "2015", "2018", "2021", "2024", "2027"),
    # Real, published ASCE 7 editions (recognition whitelist) — same as the CA
    # module until research says otherwise.
    asce7_plausible_editions=("88", "93", "95", "98", "02", "05", "10", "16", "22"),
    # Long-form citations ("2015 International Building Code"); year is group 1.
    stale_cycle_extra_patterns=(
        r"\b(20\d{2})\s+International\s+(?:Building|Fire)\s+Code\b",
    ),
    # Data-center projects genuinely pursue LEED — references are legitimate
    # scope, not copy/paste errors, so the LEED detector must NOT fire here.
    flag_leed_references=False,
    # No single jurisdiction to name — renders the generic
    # "Invalid code cycle year (…)" alert wording.
    jurisdiction_label="",
)


# Verification-profile classifier vocabulary. Classification precedence
# (internal-coordination first, then jurisdictional, manufacturer,
# code-standard, constructability default) is engine logic in
# ``verification_profiles.py``; these are the data-center fire-protection terms.
_PROFILE_KEYWORDS = ProfileKeywords(
    jurisdictional=(
        "fire marshal",
        "authority having jurisdiction",
        "ahj",
        "fm global",
        "factory mutual",
        "fm approved",
        "insurer",
        "state fire code",
        "local amendment",
        "plan review",
    ),
    manufacturer=(
        "viking",
        "tyco",
        "reliable",
        "victaulic",
        "potter",
        "xtralis",
        "vesda",
        "ansul",
        "kidde",
        "fike",
        "notifier",
        "model number",
        "model no",
        "datasheet",
        "data sheet",
        "submittal",
        "listed product",
        "or approved equal",
    ),
    code_standard=(
        "ibc",
        "ifc",
        "nfpa",
        "ul ",
        "ul-",
        "astm",
        "asme",
        "ansi",
        "asce",
        "fire code",
        "building code",
        "code section",
        "standard",
    ),
    # The California generic internal-coordination set MINUS "leed" — LEED
    # references are substantive scope here, not internal noise.
    internal_coordination=(
        "internal contradiction",
        "internally contradicts",
        "contradiction within",
        "duplicate paragraph",
        "duplicate heading",
        "duplicate section",
        "placeholder",
        "tbd",
        "[select]",
        "[verify]",
        "[insert",
        "formatting",
        "typo",
        "typographical",
        "missing placeholder",
        "self-referen",  # "self-referential", "self-references"
        "inconsistent within",
    ),
)


# CSI MasterFormat division families for chunked cross-check. Division 21 (fire
# suppression) and Division 28 (fire detection/alarm) dominate data-center fire
# packages; Division 22 is included for **fire-water supply** coordination — the
# incoming water service, fire-service mains, backflow prevention, and the
# supply feeding fire pumps and sprinkler risers live in CSI Division 22
# ("Plumbing"), so a fire finding about the water supply lands in the right
# chunk. This module does not review general plumbing scope. Unmatched prefixes
# (e.g. Division 23/26) pool into the engine's reserved ``general`` chunk.
# Chunked runs are within-chunk-only coordination (documented engine limitation).
_CROSS_CHECK_CHUNK_GROUPS = (
    ChunkGroup("div_21", "Division 21 — Fire Suppression", ("21",)),
    ChunkGroup("div_28", "Division 28 — Fire Detection & Alarm", ("28",)),
    ChunkGroup("div_22", "Division 22 — Water Supply (Plumbing)", ("22",)),
)


# ===========================================================================
# WS-5: location-aware content (research fan-out + compliance pass +
# wrong-polity detection). These slots are validated non-empty by
# ``modules.base._validate_research_slots`` because ``project_profile_enabled``
# is True below; a module that left the flag off must leave them all empty.
# ===========================================================================

# §5.9 — first line of the research system prompt (who the researcher is). The
# engine wraps it with the byte-stable research protocol block.
_RESEARCH_PERSONA = (
    "You are a fire-protection code-research assistant for hyperscale "
    "data-center projects. You research jurisdiction-specific code adoptions, "
    "local amendments, authority-having-jurisdiction requirements, and "
    "owner/client design standards. You report only requirements you can "
    "support with sources you actually retrieved, and you clearly separate "
    "verified facts from industry practice."
)


# §5.10 — the four research dimensions. Each ``prompt_template`` formats against
# the profile placeholders ({city}/{state_or_province}/{country}/{client_name})
# plus the module's own code-basis placeholders ({asce7} in site_environment);
# registration format-checks them with dummy profile values. Per-dimension
# search/fetch budgets are module data (D-6/D-11 [FT]): the field session's
# governing-codes and AHJ dimensions each touched dozens of primary
# instruments, so a flat engine default (12/4) is far too small for them.
_RESEARCH_DIMENSIONS = (
    ResearchDimension(
        dimension_id="governing_codes",
        title="Governing building and fire codes",
        max_searches=24,
        max_fetches=8,
        prompt_template=(
            "Determine the governing building and fire codes for a new "
            "hyperscale data-center project in {city}, {state_or_province}, "
            "{country}. Identify: (a) the state or provincial building and fire "
            "code editions currently in force and their model-code basis "
            "(IBC/IFC year, or NBC/NFC year for Canadian sites) with effective "
            "dates; (b) any municipal or county amendments adopted by {city} "
            "affecting fire suppression, fire pumps, water supply, or fire "
            "alarm; (c) the editions of NFPA 13, 14, 20, 22, 24, 25, and 72 "
            "referenced by that adoption, including any state or provincial "
            "amendments to those standards; (d) any licensing requirements for "
            "sprinkler contractors or design professionals that the "
            "specifications must reflect, including compulsory-trade or "
            "contractor-license regimes; (e) the fire code or operations code "
            "applicable to the completed facility and the editions of "
            "inspection/testing/maintenance standards (e.g., NFPA 25) it "
            "references — these frequently differ from the building code's "
            "referenced editions — including in-force dates of recent "
            "amendments; (f) retrieve the adopting instrument's "
            "referenced-standards table itself (or its official summary) and "
            "report the edition year for each standard the specifications cite "
            "— do not infer editions from the model-code year, and do not skip "
            "a standard because you believe you know its edition; (g) the "
            "current published edition of each of those standards, so the "
            "review can distinguish the legal minimum from current-edition "
            "enhancements; (h) the product certification/listing regime — which "
            "certification marks are legally recognized for fire-protection and "
            "electrical components in this jurisdiction (e.g., ULC/cULus vs "
            "US-only UL in Canada) and any field-evaluation path for unlisted "
            "equipment; (i) pressure-vessel design-registration requirements "
            "applicable to dry/pre-action air or nitrogen receivers (e.g., CRN "
            "in Canada); (j) the fuel-storage regime applicable to diesel "
            "fire-pump fuel systems. Prefer official adoption sources and "
            "retrieve and cite the adopting instrument itself: the state fire "
            "marshal or building-code agency, the provincial regulator or "
            "National Research Council of Canada, and the municipal code of "
            "{city}."
        ),
    ),
    ResearchDimension(
        dimension_id="ahj_requirements",
        title="Authority-having-jurisdiction requirements",
        max_searches=20,
        max_fetches=6,
        prompt_template=(
            "Identify every authority having jurisdiction over fire protection "
            "for a data-center project in {city}, {state_or_province}, "
            "{country} — assume multiplicity (fire department or fire marshal, "
            "building department, and in two-tier jurisdictions a regional "
            "water wholesaler distinct from the municipal distributor) — and "
            "any published requirements construction specifications should "
            "reflect: plan submittal and shop-drawing requirements for "
            "sprinkler, fire pump, and standpipe work; hydrant flow test and "
            "water-supply data requirements including permits, fees, notice "
            "periods, and any seasonal testing windows; required witnessed "
            "acceptance tests; fire department connection and access "
            "requirements; local policies or bulletins on pre-action systems, "
            "aspirating smoke detection, or clean-agent systems; and the "
            "inspection, testing, and maintenance documentation the AHJ "
            "requires at closeout. Treat the water purveyor/utility as its own "
            "authority: identify its requirements for fire service connections "
            "— engineering-seal requirements for service drawings, metering "
            "rules for fire lines, backflow-prevention device class and tester "
            "registration, main flushing/disinfection sign-off, and any "
            "water-allocation constraints or pending capacity reviews affecting "
            "data centers. Mark process/schedule facts (fees, windows, notice "
            "periods) as process advisories rather than spec requirements."
        ),
    ),
    ResearchDimension(
        dimension_id="client_standards",
        title="Owner / client and insurer standards",
        max_searches=12,
        max_fetches=4,
        prompt_template=(
            "First determine who reviews risk for {client_name} projects — FM "
            "Global, a named risk consultancy, or self-insurance — since this "
            "decides whether FM data sheets are mandatory or benchmark-only. "
            "Then identify published design and construction standards of "
            "{client_name} that apply to data-center fire protection: the "
            "client's public compliance, trust-center, or service-assurance "
            "documentation describing data-center fire protection; public "
            "planning/permit filings for {client_name} data-center campuses "
            "(including in {city} itself) with fire-protection specifics; which "
            "FM data sheets are commonly invoked for data centers when FM "
            "applies; known {client_name} requirements or preferences for "
            "pre-action versus wet systems, aspirating smoke detection, "
            "clean-agent or water-mist systems, and lithium-ion battery (BESS) "
            "protection; sustainability programs (e.g., LEED) {client_name} "
            "pursues that affect fire-protection specifications; and a brief "
            "benchmark of peer hyperscaler practice for calibration. Report "
            "only what you can ground in retrievable sources; where owner "
            "standards are confidential and not retrievable, say so explicitly "
            "rather than guessing."
        ),
    ),
    ResearchDimension(
        dimension_id="site_environment",
        title="Site and environmental factors",
        max_searches=8,
        max_fetches=4,
        prompt_template=(
            "Identify site and environmental factors for {city}, "
            "{state_or_province}, {country} that fire-suppression "
            "specifications must account for: the seismic design context "
            "expressed in the governing code's own framework — for US sites the "
            "ASCE {asce7} seismic design category; for Canadian sites the NBC "
            "seismic-hazard values and Seismic Category, noting whether "
            "non-structural component restraint is triggered or exempt — "
            "including the official hazard-lookup tool for the location; freeze "
            "exposure that would require dry-pipe, pre-action, or antifreeze "
            "protection in unheated areas, with January design temperatures "
            "from the code's climatic data; the minimum burial/frost-cover "
            "depth for water mains per the local utility or code; municipal "
            "water-supply reliability and published static/residual pressure "
            "ranges, and whether on-site fire-water storage is commonly "
            "required; any water-use or drought regulations affecting "
            "fire-protection water storage and discharge testing; and any "
            "current municipal or regional actions on water allocation for data "
            "centers (moratoria, capacity studies) that affect fire-water "
            "supply or storage decisions."
        ),
    ),
)


# §5.11 — compliance-pass persona + severity anchors. The engine supplies the
# <task>/<severity_definitions>/<output> protocol wrapper around these (§6.5).
_COMPLIANCE_PERSONA = (
    "You are a code-compliance reviewer for hyperscale data-center "
    "fire-protection specifications. You evaluate whether a specification "
    "package correctly represents the project's governing codes, local "
    "amendments, AHJ requirements, and client standards."
)


_COMPLIANCE_SEVERITY_DEFINITIONS = """\
CRITICAL — the package omits or contradicts a governing-code or AHJ requirement in a way that would block permit issuance or leave a life-safety protection gap.
HIGH — a location- or client-specific requirement is materially misrepresented and must be corrected before issue (e.g., the wrong adopted standard edition; a required AHJ acceptance test missing).
MEDIUM — a requirement is present but incomplete or imprecise (e.g., the correct code cited without a required local amendment).
GRIPES — editorial gaps in how requirements are referenced."""


# §5.13 [FT] — wrong-polity token seed sets. Each rule is a pure function of the
# run's project country; the pre-screen applies the profile country's rules
# ONLY when a profile is present, so profile-less runs stay byte-identical. The
# engine applies each pattern flat (no proximity suppression), so the bare
# ``UL listed`` rule relies on a word boundary — ``cULus``/``ULC`` have no
# boundary before the ``U`` and are not matched. Patterns are compile-checked
# at registration; scoped ``(?i:…)`` handles the phrase families case-blindly
# while keeping acronyms case-sensitive. Notes render into the alert so the
# operator sees WHY the token is suspicious.
_POLITY_SUSPECT_TOKENS = (
    # --- country=CA: flag US-only vocabulary on a Canadian project ----------
    PolityTokenRule(
        country="CA",
        pattern=r"\bNFPA\s*70\b|\bNEC\b",
        note=(
            "NFPA 70 / NEC is the US National Electrical Code; Canadian "
            "electrical work is governed by CSA C22.1 (Canadian Electrical "
            "Code)."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bOSHA\b",
        note=(
            "OSHA is a US federal safety agency; Canadian occupational safety "
            "is provincially regulated."
        ),
    ),
    PolityTokenRule(
        country="CA",
        # Allow the hyphenated compound "life-safety code" as well as the
        # spaced form.
        pattern=r"(?i:\blife[- ]safety code\b)",
        note=(
            "The Life Safety Code (NFPA 101) is a US code; Canadian life "
            "safety is governed by the National / provincial Building Code."
        ),
    ),
    PolityTokenRule(
        country="CA",
        # Case-insensitive on "listed" so "UL Listed" / "U.L. Listed" (the
        # common title-case forms) are caught — the engine compiles polity
        # patterns with NO flags, so the scoped ``(?i:…)`` is what makes it
        # case-blind. ``UL`` stays uppercase-required, and the word boundary
        # before ``U`` still excludes ``cULus``/``ULC`` (no boundary there).
        pattern=r"\bU\.?L\.?[- ](?i:listed)\b",
        note=(
            "A bare UL listing may not be recognized in Canada; "
            "fire-protection and electrical components generally require "
            "cULus or ULC listing."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=(
            r"\bDOT\b[^.\n]{0,80}\b(?i:tank|vessel|receiver|cylinder)\b"
            r"|\b(?i:tank|vessel|receiver|cylinder)\b[^.\n]{0,80}\bDOT\b"
        ),
        note=(
            "A DOT cylinder/vessel rating is a US pressure-vessel regime; "
            "Canadian vessels require ASME construction with CRN registration "
            "under CSA B51."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"(?i:\bmade in (?:the )?usa\b|\bdomestically made\b)",
        note=(
            "US-origin / domestic-sourcing language is a US procurement clause; "
            "on a Canadian project it may be non-compliant or tariff-exposed — "
            "revise to a listing/standard-based basis."
        ),
    ),
    PolityTokenRule(
        country="CA",
        # Match the ASCE 7 seismic *notation* (S_DS / S_D1 design spectral
        # accelerations, SDC) and the phrase — NOT bare "SDS", which collides
        # with the ubiquitous "Safety Data Sheets (SDS)" submittal requirement
        # and would fire a spurious seismic alert on every submittal section.
        pattern=r"\bS_DS\b|\bS_D1\b|\bSDC\b|(?i:\bseismic design category\b)",
        note=(
            "S_DS / S_D1 / SDC / Seismic Design Category are the ASCE 7 / IBC "
            "seismic parameters; Canadian projects use the NBC seismic-hazard "
            "framework instead."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bIBC\b|\bIFC\b",
        note=(
            "This project's governing codes are the NBC/NFC family per the "
            "requirements profile; a bare IBC/IFC citation is likely a US "
            "master-spec remnant unless the profile confirms I-code adoption."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\b115[- ]?V(?:AC)?\b",
        note=(
            "115 V is a US nominal-voltage convention; Canadian systems are "
            "specified at 120 / 208 / 347 / 600 V."
        ),
    ),
    # --- country=US: flag Canada-only vocabulary on a US project ------------
    PolityTokenRule(
        country="US",
        pattern=r"\bNBC\b|(?i:\bnational building code of canada\b)",
        note=(
            "The NBC / National Building Code of Canada is a Canadian model "
            "code; a US project is governed by the IBC/IFC family."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bULC\b",
        note=(
            "A ULC (Underwriters Laboratories of Canada) listing is Canadian; "
            "a US project generally requires a UL or cULus listing."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bCRN\b",
        note=(
            "A CRN (Canadian Registration Number) is a Canadian pressure-vessel "
            "registration under CSA B51; US vessels use the ASME / National "
            "Board regime."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"O\. ?Reg\.",
        note=(
            "'O. Reg.' cites an Ontario regulation; a US project is not "
            "governed by Ontario law."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bCSA\s*C22\.1\b",
        note=(
            "CSA C22.1 (Canadian Electrical Code) governs Canadian electrical "
            "work; a US project's electrical code is NFPA 70 (NEC)."
        ),
    ),
)


# Module-owned corpus-signal patterns (family (a), D-3 [FT]): document-name
# vocabulary the deterministic scrape looks for so research searches with the
# project's own terms. Compiled case-insensitive at scrape time; the other
# three signal families (risk consultant/insurer, edition-governance,
# standards-with-editions) are engine-owned.
_CORPUS_SIGNAL_PATTERNS = (
    r"\bbasis of design\b",
    r"\bBoD\b",
    r"\bowner'?s? project requirements\b",
    r"\bOPR\b",
    r"\bdesign (?:basis|criteria|guide(?:lines)?|standard)s?\b",
    r"\b(?:master|guide)[ -]?spec(?:ification)?s?\b",
    r"\bfire protection design (?:guide|standard|criteria)\b",
)


DATACENTER_FIRE = ReviewModule(
    module_id="datacenter_fire",
    display_name="Hyperscale Data Center — Fire Suppression (US/Canada)",
    description=(
        "Fire-suppression (Division 21) specs for hyperscale data-center "
        "projects in the US and Canada, reviewed against the International "
        "Building Code and International Fire Code as base model codes, including "
        "coordination with fire-alarm and releasing interfaces. Put the "
        "governing state or provincial code, local amendments, and "
        "authority-having-jurisdiction requirements for the project location "
        "into Project Context."
    ),
    cycle=DATACENTER_IBC_2024,
    reviewer_persona=(
        "You are a fire-protection specification reviewer specializing in "
        "automatic sprinkler and suppression systems. The project context is "
        "hyperscale data-center facilities in the United States and Canada, "
        "designed under the International Building Code and International Fire "
        "Code as base model codes, with the project's governing "
        "state/provincial adoptions, local amendments, "
        "authority-having-jurisdiction requirements, and owner standards "
        "supplied in the project context."
    ),
    review_user_intro=(
        "Review the following fire-suppression specification for a hyperscale "
        "data-center project. Where the project context includes a Project "
        "Requirements Profile, treat its governing-code, local-amendment, AHJ, "
        "and client-standard entries as the project's controlling "
        "requirements — they take precedence over the model-code defaults for "
        "edition and requirement checks. Where the specification declares its "
        "own edition-governance rule, check that rule for consistency with the "
        "profile's adopted editions. Where the specification cites its own "
        "basis-of-design or owner documents that are not provided for review, "
        "phrase findings about them conditionally ('per the BoD section the "
        "spec cites — confirm against that document') rather than asserting "
        "their content."
    ),
    review_severity_definitions=_REVIEW_SEVERITY_DEFINITIONS,
    review_confidence_high_example='an explicit stale "2015 IBC" citation',
    review_categories_template=_REVIEW_CATEGORIES,
    review_examples=_REVIEW_EXAMPLES,
    cross_check_persona=(
        "You are a cross-spec coordination reviewer for hyperscale "
        "data-center fire-protection packages."
    ),
    cross_check_severity_definitions=_CROSS_CHECK_SEVERITY_DEFINITIONS,
    verifier_persona=(
        "You are a construction specification verification assistant for "
        "fire-protection systems in hyperscale data-center projects under the "
        "IBC/IFC family of model codes."
    ),
    verifier_source_priorities=_VERIFIER_SOURCE_PRIORITIES,
    review_user_code_basis_line=(
        "Current code basis: IBC {ibc}, IFC {ifc}, ASCE {asce7}."
    ),
    cross_check_code_basis_line=(
        "Current code basis: IBC {ibc}, IFC {ifc}, ASCE {asce7}."
    ),
    verifier_system_code_basis_lines=(
        "Current code basis: IBC {ibc}, IFC {ifc}, ASCE {asce7}."
    ),
    verifier_user_code_basis_lines=(
        "Current code basis: IBC {ibc}, IFC {ifc}\n"
        "Current seismic standard: ASCE {asce7}"
    ),
    detector_vocabulary=_DETECTOR_VOCABULARY,
    profile_keywords=_PROFILE_KEYWORDS,
    cross_check_chunk_groups=_CROSS_CHECK_CHUNK_GROUPS,
    report_context_phrase="hyperscale data-center fire protection projects",
    report_title="Spec Critic — Fire Protection Specification Review Report",
    # --- WS-5: location-aware capability turned ON ----------------------
    project_profile_enabled=True,
    research_persona=_RESEARCH_PERSONA,
    research_dimensions=_RESEARCH_DIMENSIONS,
    corpus_signal_patterns=_CORPUS_SIGNAL_PATTERNS,
    compliance_persona=_COMPLIANCE_PERSONA,
    compliance_severity_definitions=_COMPLIANCE_SEVERITY_DEFINITIONS,
    polity_suspect_tokens=_POLITY_SUSPECT_TOKENS,
)
