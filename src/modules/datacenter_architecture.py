"""Hyperscale data-center architectural specification module (US / Canada).

This module reviews architectural specifications for hyperscale data-center
projects.  It deliberately follows the same jurisdiction posture as the
existing fire-suppression module: the frozen module pins current model codes as
its fallback basis, while the per-run :class:`~src.core.project_profile.ProjectProfile`
drives research of the code editions, amendments, authorities, and client
standards that actually govern the project location.

The file is self-contained and registry-ready, but is not registered here.
Registration is the composition root's responsibility; keeping it out of this
module also lets its contract tests exercise the definition without changing
the application's selectable modules.

Canadian projects are handled through the researched Project Requirements
Profile.  The deterministic cycle detector remains I-code-only because its
current contract has one shared year family and one primary target year; adding
NBC/NFC/NECB abbreviations to that set would create false stale/invalid alerts.
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


# A registry-unique label is required because cycle labels namespace the
# verification cache and back the module_for_cycle reverse lookup.  The model
# codes below are a fallback, not a claim about the codes adopted at any
# particular project location.
DATACENTER_ARCHITECTURE_IBC_2024 = CodeCycle(
    label="dc-architecture-ibc-2024",
    base_codes=(
        BaseCode(
            "ibc",
            "IBC",
            "2024",
            source="ICC 2024 IBC v2.0: https://codes.iccsafe.org/content/IBC2024V2.0",
        ),
        BaseCode(
            "ifc",
            "IFC",
            "2024",
            source="ICC 2024 IFC v1.0: https://codes.iccsafe.org/content/IFC2024V1.0",
        ),
        BaseCode(
            "iecc",
            "IECC",
            "2024",
            source="ICC 2024 IECC: https://codes.iccsafe.org/content/IECC2024P1",
        ),
        BaseCode(
            "iebc",
            "IEBC",
            "2024",
            source="ICC 2024 IEBC v1.0: https://codes.iccsafe.org/content/IEBC2024V1.0",
        ),
    ),
    asce7="7-22",
    asce7_previous="7-16",
    standards=(
        # These are verified model-basis / commonly invoked architectural
        # editions, not a substitute for location-specific adoption research.
        # ASCE 7 stays in its detector-specific field above; the 2024 IBC
        # reference includes ASCE 7-22 Supplement 1, which every
        # architecture prompt states explicitly below.
        StandardEdition(
            "ICC A117.1",
            "2017 with Supplement 1",
            source=(
                "ICC 2024 IBC accessibility provisions, verified 2026-07-20: "
                "https://codes.iccsafe.org/content/IBCACCPB2024P1/"
                "icc-a117-1-2017-with-supplement-1-american-national-standard-"
                "standard-for-accessible-and-usable-buildings-and-facilities"
            ),
        ),
        StandardEdition(
            "ASHRAE 90.1",
            "2022",
            source=(
                "ICC 2024 IECC/ASHRAE 90.1-2022 publication, verified 2026-07-20: "
                "https://codes.iccsafe.org/content/IECCASHRAE2024P1"
            ),
        ),
        StandardEdition(
            "NFPA 80",
            "2022",
            source=(
                "2024 IBC Ch. 35 and NFPA 80-2022, verified 2026-07-20: "
                "https://codes.iccsafe.org/content/IBC2024V2.0/chapter-35-"
                "referenced-standards"
            ),
        ),
        StandardEdition(
            "NFPA 101",
            "2024",
            note=(
                "limited IBC/IFC Section 1030.6.2 reference; otherwise owner/AHJ "
                "invoked where applicable"
            ),
            source=(
                "2024 IFC Ch. 80 and NFPA 101-2024, verified 2026-07-20; "
                "the I-code reference is limited to Section 1030.6.2: "
                "https://codes.iccsafe.org/content/IFC2024P1/chapter-80-"
                "referenced-standards ; "
                "https://link.nfpa.org/all-publications/101/2024"
            ),
        ),
        StandardEdition(
            "NFPA 285",
            "2023",
            source=(
                "2024 IBC Ch. 35 and NFPA 285-2023, verified 2026-07-20: "
                "https://codes.iccsafe.org/content/IBC2024V2.0/chapter-35-"
                "referenced-standards ; "
                "https://link.nfpa.org/all-publications/285/2023"
            ),
        ),
        StandardEdition(
            "ASTM E119",
            "20",
            source=(
                "2024 IBC referenced ASTM E119-20, verified 2026-07-20: "
                "https://codes.iccsafe.org/content/ASTM-E119-20"
            ),
        ),
        StandardEdition(
            "ASTM E84",
            "21a",
            source=(
                "2024 IBC Ch. 35 / ASTM E84-21a, verified 2026-07-20: "
                "https://codes.iccsafe.org/content/IBC2024V2.0/chapter-35-"
                "referenced-standards ; "
                "https://store.astm.org/e0084-21a.html"
            ),
        ),
    ),
)


_REVIEW_CATEGORIES = """\
1. Internal contradictions within the specification, including conflicting performance, material, warranty, or execution requirements.
2. Code-basis alignment: use the Project Requirements Profile's adopted codes and amendments when present; otherwise use IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}, and ASCE {asce7} with Supplement 1 as model-code fallbacks. Check cited standards against the project-specific adoption before calling an edition stale. Fallback pinned editions: {pinned_standards}.
3. Occupancy, construction type, allowable area/height, mixed-use separation, incidental-use, and high-hazard assumptions that are internally inconsistent or conflict with the supplied project requirements.
4. Means of egress and life safety: occupant-load assumptions, exit count/capacity, travel distance, common path, dead ends, exit access, horizontal exits, stairs, guards, handrails, and door egress hardware.
5. Accessibility and inclusive design: accessible routes, entrances, clearances, maneuvering space, toilet/support rooms, signage, operable parts, level changes, and conflicts between federal, state/provincial, and local requirements.
6. Fire-resistance and compartmentation: rated assemblies, shaft and room enclosures, continuity, joints, penetrations, opening protectives, fire/smoke doors, dampers, and inconsistent tested-design references.
7. Data-hall and mission-critical separations: white space, gray space, battery/UPS rooms, generator support, fuel-related rooms, loading/service zones, offices, and security boundaries.
8. Exterior enclosure continuity: water, air, vapor, and thermal control layers; transitions at roofs, walls, foundations, louvers, doors, penetrations, and expansion joints.
9. Hygrothermal and climate suitability: condensation risk, vapor-retarder location, thermal bridging, freeze exposure, wind-driven rain, snow/ice, extreme heat, and material compatibility with the researched site conditions.
10. Roofing and waterproofing: drainage/slope, overflow, wind uplift, edge securement, penetrations, equipment curbs, vegetated/reflective systems, below-grade waterproofing, and warranty conflicts.
11. Doors, frames, glazing, and hardware: fire/smoke ratings, security and access-control interfaces, free egress, electrified hardware, forced-entry criteria, glazing safety, and inconsistent schedules or sets.
12. Interior materials and environmental criteria: flame/smoke performance, durability, static-control needs, moisture resistance, cleanability, VOC/content requirements, and room-finish schedule conflicts.
13. Structural and seismic interfaces: design responsibility, delegated design, movement joints, drift compatibility, anchorage/support of architectural components, and ASCE {asce7} with Supplement 1 coordination.
14. MEP/technology interfaces that affect architecture: equipment access/removal paths, housekeeping pads and curbs, louvers, intake/exhaust separation, sleeves/openings, ceilings, raised floors, containment, cable pathways, and maintainable clearances.
15. Security and resilience interfaces: site perimeter, vestibules/mantraps, loading and receiving, ballistic/forced-entry criteria where specified, access-control zoning, emergency egress, and fail-safe/fail-secure conflicts.
16. Site and civil coordination: finished floors, grading and drainage interfaces, accessible site routes, retaining/guard conditions, paving and curbs, utility entries, flood protection, and landscape/setback constraints.
17. Sustainability and energy requirements: envelope performance, air leakage, commissioning/testing, material disclosure, embodied-carbon or certification targets, and conflicts between code minimums and client goals. LEED references are legitimate scope when the project pursues certification.
18. Client prototype/design standards, insurer criteria, and authority requirements: identify conflicts, omissions, or unsupported claims relative to the Project Requirements Profile; distinguish controlling requirements from benchmarks and preferences.
19. Specification quality and procurement: incomplete substitutions, proprietary requirements without an approved-equal path where required, conflicting submittals/mockups/testing, warranty gaps, and responsibility assigned to the wrong party.
20. Master-specification remnants: another project, jurisdiction, building type, discipline, client, or obsolete product left in the section; unresolved options/placeholders; wrong-polity agencies, listing marks, procurement clauses, or code families.
21. Document integrity: duplicate/out-of-sequence articles, empty paragraphs, dangling or mismatched related-section references, schedule/spec conflicts, doubled words, and products named in PART 2 without corresponding execution requirements (or vice versa)."""


_REVIEW_EXAMPLES = """\
Example 1 — valid EDIT (wrong project code assumption):
{
  "severity": "HIGH",
  "fileName": "08 71 00 Door Hardware.docx",
  "section": "1.04",
  "issue": "The section hard-codes an obsolete model-code edition instead of the edition adopted for the project location.",
  "actionType": "EDIT",
  "existingText": "Comply with the 2015 International Building Code for means-of-egress hardware.",
  "replacementText": "Comply with the building code and accessibility requirements adopted for the Project location, including local amendments, for means-of-egress hardware.",
  "codeReference": "Project Requirements Profile — governing building code",
  "confidence": 0.9
}

Example 2 — valid ADD (missing continuity requirement with a verbatim anchor):
{
  "severity": "HIGH",
  "fileName": "07 27 26 Fluid-Applied Membrane Air Barriers.docx",
  "section": "3.05",
  "issue": "The installation article omits continuity requirements at transitions to adjacent roof and foundation control layers.",
  "actionType": "ADD",
  "existingText": null,
  "replacementText": "Maintain continuous air-barrier transitions at roofs, foundations, openings, penetrations, and changes in substrate; coordinate transition materials with adjacent Sections.",
  "anchorText": "3.05 FIELD QUALITY CONTROL",
  "insertPosition": "before",
  "codeReference": null,
  "confidence": 0.82
}

Example 3 — REPORT_ONLY (multi-section coordination without one safe edit):
{
  "severity": "CRITICAL",
  "fileName": "08 71 00 Door Hardware.docx",
  "section": "3.06",
  "issue": "The electrified-hardware sequence calls for fail-secure operation at an exit door while the access-control narrative requires free egress on alarm. Resolve the life-safety and security sequence across the door, hardware, fire-alarm, and access-control sections before editing any one section.",
  "actionType": "REPORT_ONLY",
  "existingText": null,
  "replacementText": null,
  "anchorText": null,
  "insertPosition": null,
  "codeReference": null,
  "confidence": 0.92
}

Example 4 — DO NOT REPORT:
Do not report generic coordination clauses, ordinary product options that the
project has actually resolved, or LEED/sustainability references merely because
they appear in a data-center specification. Emit a finding only for a concrete,
quoted defect in the submitted document.\
"""


_REVIEW_SEVERITY_DEFINITIONS = """\
CRITICAL — a life-safety, occupancy, accessibility, enclosure, security/egress, or permit defect likely to block approval, create an unsafe condition, or expose mission-critical space to major water/fire loss.
HIGH — a material architectural defect requiring correction before issue, such as a broken control-layer transition, noncompliant egress arrangement, incompatible rated-assembly requirement, or conflict with a controlling client/AHJ criterion.
MEDIUM — a meaningful but bounded defect, such as an imprecise adopted-edition citation, incomplete performance requirement, or coordination gap with a clear resolution path.
GRIPES — editorial and specification-quality defects that do not materially change performance but should be corrected."""


_CROSS_CHECK_SEVERITY_DEFINITIONS = """\
CRITICAL — a package-level contradiction likely to cause a life-safety failure, permit rejection, loss of required separation, or major water/security exposure.
HIGH — a major cross-section conflict requiring coordinated correction before issue, such as incompatible door/security sequences or discontinuous roof/wall/foundation control layers.
MEDIUM — a meaningful inconsistency in products, ratings, dimensions, responsibilities, schedules, or related-section references.
GRIPES — minor package coordination or nomenclature inconsistencies."""


_VERIFIER_SOURCE_PRIORITIES = """\
1. Project-location authorities and official adopting instruments:
   US state and municipal building, planning, fire, energy, and accessibility
   authority sites (.gov); for Canada, nrc.canada.ca, canada.ca, official
   provincial/territorial legislation and code-authority sites, and municipal
   bylaw/permit portals.

2. Code publishers and accessibility authorities:
   codes.iccsafe.org, iccsafe.org, access-board.gov, ada.gov, and official
   Canadian model-code / provincial accessibility publications.

3. Standards, testing, and certification organizations:
   ashrae.org, nfpa.org, astm.org, ansi.org, ul.com, csagroup.org,
   scc-ccn.ca, and official evaluation/listing directories.

4. Building-enclosure and architectural technical authorities:
   iibec.org, airbarrier.org, nrca.net, spri.org, fgialonline.org, and
   peer-reviewed building-science publications.

5. Manufacturer technical literature and tested-assembly directories:
   use current manufacturer data only after code, authority, listing, and
   standards sources; do not treat marketing pages as proof of compliance.

6. Archived or historical material:
   archive.org, only to establish what an obsolete citation previously said."""


_DETECTOR_VOCABULARY = DetectorVocabulary(
    code_abbreviations=("IBC", "IFC", "IECC", "IEBC"),
    plausible_cycle_years=("2009", "2012", "2015", "2018", "2021", "2024"),
    valid_cycle_years=("2009", "2012", "2015", "2018", "2021", "2024", "2027"),
    asce7_plausible_editions=("88", "93", "95", "98", "02", "05", "10", "16", "22"),
    stale_cycle_extra_patterns=(
        r"\b(20\d{2})\s+International\s+(?:Building|Fire|Energy Conservation|Existing Building)\s+Code\b",
    ),
    # Sustainability certification is legitimate data-center scope.
    flag_leed_references=False,
    # Multi-jurisdictional module: report/detector wording remains generic.
    jurisdiction_label="",
)


_PROFILE_KEYWORDS = ProfileKeywords(
    jurisdictional=(
        "building official",
        "building department",
        "planning department",
        "zoning",
        "plan review",
        "fire marshal",
        "authority having jurisdiction",
        "ahj",
        "local amendment",
        "state building code",
        "provincial building code",
        "accessibility authority",
        "permit",
    ),
    manufacturer=(
        "product data",
        "manufacturer",
        "model number",
        "model no",
        "tested assembly",
        "evaluation report",
        "icc-es",
        "ul design",
        "listed assembly",
        "basis of design product",
        "or approved equal",
        "substitution request",
    ),
    code_standard=(
        "ibc",
        "ifc",
        "iecc",
        "iebc",
        "national building code of canada",
        "national energy code of canada",
        "icc a117.1",
        "ada standards",
        "ashrae",
        "nfpa",
        "astm",
        "ansi",
        "asce",
        "csa",
        "ul ",
        "ul-",
        "building code",
        "energy code",
        "code section",
        "standard",
    ),
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
        "self-referen",
        "inconsistent within",
    ),
)


_CROSS_CHECK_CHUNK_GROUPS = (
    ChunkGroup(
        "procurement_general",
        "Procurement / General Requirements",
        ("00", "01"),
    ),
    ChunkGroup(
        "structure_enclosure",
        "Existing Conditions / Structure / Enclosure",
        ("02", "03", "04", "05", "06", "07"),
    ),
    ChunkGroup(
        "openings_interiors",
        "Openings / Finishes / Specialties / Equipment",
        ("08", "09", "10", "11", "12", "13", "14"),
    ),
    ChunkGroup(
        "building_system_interfaces",
        "Building-System Interfaces",
        ("21", "22", "23", "25", "26", "27", "28"),
    ),
    ChunkGroup(
        "sitework",
        "Earthwork / Exterior Improvements / Utilities",
        ("31", "32", "33"),
    ),
)


_RESEARCH_PERSONA = (
    "You are an architectural code and requirements researcher for hyperscale "
    "data-center projects in the United States and Canada. You research the "
    "location's adopted building, fire, energy, existing-building, accessibility, "
    "planning, and enclosure requirements together with client and insurer "
    "criteria. Report only facts supported by sources you actually retrieved, "
    "and distinguish controlling requirements from guidance or common practice."
)


_RESEARCH_DIMENSIONS = (
    ResearchDimension(
        dimension_id="governing_codes_accessibility",
        title="Governing architectural codes and accessibility",
        max_searches=24,
        max_fetches=8,
        prompt_template=(
            "Determine the architectural code basis currently in force for a new "
            "hyperscale data-center project in {city}, {state_or_province}, "
            "{country}. For a US project, identify the adopted building, fire, "
            "energy, and existing-building code editions and the jurisdiction's "
            "accessibility stack (federal ADA requirements plus adopted code/ICC "
            "A117.1 and state/local provisions). For a Canadian project, identify "
            "the applicable National/provincial building and fire code lineage, "
            "energy code or NECB path, and provincial/municipal barrier-free and "
            "accessibility law. In either country: retrieve the actual adopting "
            "instruments and effective dates; identify local amendments affecting "
            "occupancy, construction type, area/height, egress, fire-resistance, "
            "exterior walls/roofs, energy/envelope performance, and accessibility; "
            "identify the referenced editions of ICC A117.1, ASHRAE 90.1, NFPA 80, "
            "and the principal ASTM/NFPA tests used by the architectural sections; "
            "and distinguish adopted editions from current editions and voluntary "
            "owner enhancements. The model-code fallback is IBC {ibc}, IFC {ifc}, "
            "IECC {iecc}, IEBC {iebc}, and ASCE {asce7} with Supplement 1, but "
            "never substitute that "
            "fallback for the project location's researched adoption."
        ),
    ),
    ResearchDimension(
        dimension_id="ahj_planning_permitting",
        title="Architectural AHJ, planning, zoning, and permitting requirements",
        max_searches=20,
        max_fetches=6,
        prompt_template=(
            "Identify the authorities and published architectural requirements for "
            "a hyperscale data-center project in {city}, {state_or_province}, "
            "{country}: building and fire plan review; planning, zoning, site-plan, "
            "and design-review approvals; accessibility review/enforcement; energy-"
            "code documentation; land-use conditions that affect setbacks, height, "
            "screening, facade, lighting, noise barriers, loading/service areas, or "
            "equipment yards; floodplain, stormwater, wildfire, heritage, airport, "
            "or environmental overlays when applicable; professional seal and "
            "delegated-design requirements; required special inspections, envelope "
            "testing/commissioning, mockups, or third-party reports; and certificate-"
            "of-occupancy prerequisites. Separate specification requirements from "
            "process advisories such as fees, hearings, submission windows, and review "
            "durations. Prefer official bylaws, ordinances, checklists, bulletins, "
            "permit manuals, and approval conditions over consultant summaries."
        ),
    ),
    ResearchDimension(
        dimension_id="client_architectural_standards",
        title="Client, insurer, and prototype architectural standards",
        max_searches=14,
        max_fetches=5,
        prompt_template=(
            "Research retrievable architectural design and construction requirements "
            "of {client_name} for hyperscale data centers, including public prototype "
            "or design-guideline material, planning/permit filings for comparable "
            "campuses, sustainability and carbon commitments, certification targets, "
            "envelope/roof resilience and water-intrusion controls, material or "
            "chemical restrictions, security zoning and forced-entry criteria, "
            "accessibility/inclusive-design commitments, white-space and support-space "
            "separations, maintainability/equipment-removal expectations, standard "
            "room data or finish criteria, and insurer/risk-consultant requirements. "
            "Determine whether FM Global or another risk authority is controlling or "
            "benchmark-only. Use public owner sources and project filings first; if "
            "the actual owner standard is confidential or merely cited by the specs, "
            "state that limitation and do not invent its contents."
        ),
    ),
    ResearchDimension(
        dimension_id="site_climate_enclosure",
        title="Site climate, hazards, and enclosure design inputs",
        max_searches=12,
        max_fetches=5,
        prompt_template=(
            "Identify official site and climate requirements for architectural "
            "specifications in {city}, {state_or_province}, {country}: energy/climate "
            "zone; winter and summer design conditions; rain, humidity, freeze-thaw, "
            "frost depth, wind, snow, ice, hail, wildfire, flood, and other hazards "
            "material to enclosure and site design; governing structural/environmental "
            "criteria for nonstructural components and cladding (US projects use the "
            "jurisdiction's adopted ASCE 7 edition and incorporated supplements; "
            "Canadian projects use the governing NBC/"
            "provincial framework); required roof wind/fire classifications, air-"
            "leakage or whole-building testing, envelope commissioning, radon or soil-"
            "gas provisions where applicable, flood-protection elevations, and local "
            "durability/material restrictions. Cite official climate tables, hazard "
            "tools, adopted maps, utility/authority criteria, or other primary sources. "
            "Classify design facts the specification must reflect as requirements and "
            "project-team investigations as process advisories."
        ),
    ),
)


_COMPLIANCE_PERSONA = (
    "You are an architectural code-compliance reviewer for hyperscale data-center "
    "specification packages. Evaluate whether the package correctly represents "
    "the project's adopted codes, accessibility obligations, local amendments, "
    "planning/AHJ requirements, site hazards, and controlling client standards."
)


_COMPLIANCE_SEVERITY_DEFINITIONS = """\
CRITICAL — the package omits or contradicts a controlling life-safety, accessibility, occupancy, fire-resistance, or enclosure requirement in a way likely to block approval or create a major operational/loss exposure.
HIGH — a material adopted-code, AHJ, planning, site-hazard, or client requirement is missing or misrepresented and must be corrected before issue.
MEDIUM — a controlling requirement is represented incompletely or imprecisely, but the deficiency is bounded and has a clear correction path.
GRIPES — editorial or traceability gaps in how project requirements are cited or coordinated."""


_POLITY_SUSPECT_TOKENS = (
    # Canadian projects: flag unqualified US-only legal/code vocabulary.
    PolityTokenRule(
        country="CA",
        pattern=r"\bADA\b|(?i:\bAmericans with Disabilities Act\b|\bADA Standards for Accessible Design\b)",
        note=(
            "ADA is United States federal accessibility law. A Canadian project "
            "uses the applicable provincial/territorial accessibility and barrier-"
            "free regime unless the client separately adopts ADA as an enhancement."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bIBC\b|\bIFC\b|\bIECC\b|\bIEBC\b",
        note=(
            "Bare I-code citations are likely US master-spec remnants on a Canadian "
            "project unless the Project Requirements Profile confirms a specific "
            "benchmark or local adoption relationship."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bICC\s*A117\.1\b",
        note=(
            "ICC A117.1 is a US accessibility standard and does not automatically "
            "satisfy the Canadian project's governing accessibility legislation."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bU\.?L\.?[- ](?i:listed)\b",
        note=(
            "A bare US UL listing may not establish Canadian certification; verify "
            "the required cUL/ULC/CSA mark and product category for the location."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bOSHA\b",
        note=(
            "OSHA is a US federal agency; Canadian occupational requirements are "
            "established under the applicable federal or provincial regime."
        ),
    ),
    # US projects: flag unqualified Canadian code/legal vocabulary.
    PolityTokenRule(
        country="US",
        pattern=r"\bNBC\b|(?i:\bNational Building Code of Canada\b)",
        note=(
            "NBC / National Building Code of Canada is a Canadian model code; a US "
            "project normally uses the state/local I-code adoption."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bNECB\b|(?i:\bNational Energy Code of Canada for Buildings\b)",
        note=(
            "NECB is a Canadian energy code; verify the US jurisdiction's adopted "
            "IECC/ASHRAE compliance path instead."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bCSA\s*B651\b",
        note=(
            "CSA B651 is a Canadian accessibility standard; it is not a substitute "
            "for the US project's ADA and adopted building-code accessibility stack."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bULC\b",
        note=(
            "ULC is a Canadian certification/standards mark; verify the listing or "
            "certification recognized by the US project jurisdiction."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"O\.?\s*Reg\.?\s*\d+[/\-]\d+",
        note=(
            "'O. Reg.' identifies an Ontario regulation and is likely a Canadian "
            "master-spec remnant on a US project."
        ),
    ),
)


_CORPUS_SIGNAL_PATTERNS = (
    r"\bbasis of design\b",
    r"\bBoD\b",
    r"\bowner'?s? project requirements\b",
    r"\bOPR\b",
    r"\barchitectural design (?:criteria|guide(?:lines)?|standards?)\b",
    r"\bprototype (?:design|specification|standard)s?\b",
    r"\broom data sheets?\b",
    r"\bbuilding enclosure commissioning\b",
    r"\bBECx\b",
    r"\bsecurity design criteria\b",
    r"\bdesign and construction standards?\b",
)


DATACENTER_ARCHITECTURE = ReviewModule(
    module_id="datacenter_architecture",
    display_name="Hyperscale Data Center — Architecture (US/Canada)",
    description=(
        "Architectural specifications for hyperscale data-center projects in the "
        "United States and Canada, with location/client research and compliance "
        "against the project's adopted requirements."
    ),
    cycle=DATACENTER_ARCHITECTURE_IBC_2024,
    reviewer_persona=(
        "You are an architectural specification reviewer specializing in "
        "hyperscale data-center facilities in the United States and Canada. "
        "Review life safety, accessibility, enclosure, interiors, openings, "
        "security, site, and multidisciplinary interfaces against the Project "
        "Requirements Profile and the submitted project documents."
    ),
    review_user_intro=(
        "Review the following architectural specification for a hyperscale data-"
        "center project. When Project Context contains a Project Requirements "
        "Profile, its adopted codes, accessibility obligations, local amendments, "
        "AHJ/planning requirements, site criteria, and controlling client standards "
        "take precedence over the module's model-code fallback. Treat cited but "
        "unprovided owner/design documents conditionally; do not invent their content."
    ),
    review_severity_definitions=_REVIEW_SEVERITY_DEFINITIONS,
    review_confidence_high_example=(
        'an explicit "2015 IBC" governing-code statement where the supplied '
        "Project Requirements Profile identifies a different adopted edition"
    ),
    review_categories_template=_REVIEW_CATEGORIES,
    review_examples=_REVIEW_EXAMPLES,
    cross_check_persona=(
        "You are a cross-specification coordination reviewer for architectural "
        "packages on hyperscale data-center projects."
    ),
    cross_check_severity_definitions=_CROSS_CHECK_SEVERITY_DEFINITIONS,
    verifier_persona=(
        "You are a construction-specification verification assistant for "
        "architectural requirements on hyperscale data-center projects in the "
        "United States and Canada."
    ),
    verifier_source_priorities=_VERIFIER_SOURCE_PRIORITIES,
    review_user_code_basis_line=(
        "Model-code fallback: IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}, "
        "ASCE {asce7} with Supplement 1; project-profile adoptions "
        "govern when supplied."
    ),
    cross_check_code_basis_line=(
        "Model-code fallback: IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}, "
        "ASCE {asce7} with Supplement 1; project-profile adoptions "
        "govern when supplied."
    ),
    verifier_system_code_basis_lines=(
        "Model-code fallback: IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}, "
        "ASCE {asce7} with Supplement 1.\nUse the Project Requirements "
        "Profile's adopted code and "
        "location requirements when present."
    ),
    verifier_user_code_basis_lines=(
        "Model-code fallback: IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}\n"
        "Fallback seismic standard: ASCE {asce7} with Supplement 1; "
        "project-profile adoptions govern."
    ),
    detector_vocabulary=_DETECTOR_VOCABULARY,
    profile_keywords=_PROFILE_KEYWORDS,
    cross_check_chunk_groups=_CROSS_CHECK_CHUNK_GROUPS,
    report_context_phrase="hyperscale data-center architectural projects",
    report_title="Spec Critic — Architectural Specification Review Report",
    project_profile_enabled=True,
    research_persona=_RESEARCH_PERSONA,
    research_dimensions=_RESEARCH_DIMENSIONS,
    corpus_signal_patterns=_CORPUS_SIGNAL_PATTERNS,
    compliance_persona=_COMPLIANCE_PERSONA,
    compliance_severity_definitions=_COMPLIANCE_SEVERITY_DEFINITIONS,
    polity_suspect_tokens=_POLITY_SUSPECT_TOKENS,
)


__all__ = [
    "DATACENTER_ARCHITECTURE",
    "DATACENTER_ARCHITECTURE_IBC_2024",
]
