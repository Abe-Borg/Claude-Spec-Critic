"""Hyperscale data-center electrical specification module (US / Canada).

The module uses the 2024 I-code family and its referenced US electrical
standards as a model-code fallback.  It does not claim that fallback governs a
particular project: the shared Project Requirements Profile researches the
actual state, provincial/territorial, local, utility, and client requirements
for every run.

NFPA 70 / NEC and CSA C22.1 / CEC are deliberately absent from the deterministic
cycle detector.  That detector compares every recognized code year with one
primary I-code year, so mixing the valid 2023 NEC or 2024 CEC into its 2024
I-code vocabulary would create false stale/invalid alerts.  Edition alignment
for those electrical codes is handled by location research, semantic review,
and verification instead.
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


_IBC_CH35 = (
    "https://codes.iccsafe.org/content/IBC2024V2.0/"
    "chapter-35-referenced-standards"
)
_IFC_CH80 = (
    "https://codes.iccsafe.org/content/IFC2024P1/"
    "chapter-80-referenced-standards"
)
_IECC_CH6 = (
    "https://codes.iccsafe.org/content/IECC2024P1/"
    "chapter-6-ce-referenced-standards"
)


DATACENTER_ELECTRICAL_IBC_2024 = CodeCycle(
    label="dc-electrical-ibc-2024",
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
        StandardEdition(
            "NFPA 70 (NEC)",
            "2023",
            source=(
                "2024 IBC/IFC referenced edition, verified 2026-07-20: "
                f"{_IBC_CH35} ; {_IFC_CH80}"
            ),
        ),
        StandardEdition(
            "NFPA 110",
            "2022",
            note="where an EPSS or owner criterion invokes it",
            source=(
                "2024 IBC Section 2702.1.3 / Ch. 35, verified 2026-07-20: "
                "https://codes.iccsafe.org/s/IBC2024V2.0/chapter-27-electrical/"
                f"IBC2024V2.0-Ch27-Sec2702.1.3 ; {_IBC_CH35}"
            ),
        ),
        StandardEdition(
            "NFPA 111",
            "2022",
            note="where a stored-energy emergency/standby system invokes it",
            source=(
                "2024 IBC Section 2702.1.3 / Ch. 35, verified 2026-07-20: "
                "https://codes.iccsafe.org/s/IBC2024V2.0/chapter-27-electrical/"
                f"IBC2024V2.0-Ch27-Sec2702.1.3 ; {_IBC_CH35}"
            ),
        ),
        StandardEdition(
            "ASHRAE 90.1",
            "2022",
            source=f"2024 IECC Ch. 6, verified 2026-07-20: {_IECC_CH6}",
        ),
        StandardEdition(
            "ASHRAE 90.4",
            "2022",
            note="with published errata through Dec. 11, 2023",
            source=(
                "2024 IECC data-center reference and ASHRAE errata, verified "
                "2026-07-20: "
                f"{_IECC_CH6} ; https://www.ashrae.org/file%20library/technical%20"
                "resources/standards%20and%20guidelines/standards%20errata/"
                "standards/90.4-2022errata-12-11-2023-.pdf"
            ),
        ),
        StandardEdition(
            "IEEE 1584",
            "2018",
            note="with Aug. 30, 2019 errata; applicable study scope only",
            source=(
                "IEEE active standard and official errata, verified 2026-07-20: "
                "https://standards.ieee.org/ieee/1584/5802/ ; "
                "https://standards.ieee.org/wp-content/uploads/import/documents/"
                "erratas/1584-2018_errata.pdf"
            ),
        ),
        StandardEdition(
            "UL 2200",
            "2020",
            note="stationary generator-set listing where applicable",
            source=(
                "2024 IBC Ch. 35 and UL Edition 3 publication, verified 2026-07-20: "
                f"{_IBC_CH35} ; https://www.shopulstandards.com/"
                "ProductDetail.aspx?productId=UL2200"
            ),
        ),
    ),
)


_REVIEW_CATEGORIES = """\
1. Internal contradictions within the specification, including incompatible ratings, voltages, topology, performance, testing, warranty, or execution requirements.
2. Project-specific code and edition alignment: the Project Requirements Profile's adopted electrical, building, fire, and energy codes and amendments govern. The US model-code fallback is IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}, and ASCE {asce7} with Supplement 1. Fallback reference editions: {pinned_standards}. Distinguish legally adopted, current, and owner-invoked editions; do not call a newer owner edition wrong merely because it differs from the fallback.
3. Utility service and interconnection: service voltage/configuration, points of ownership, transformer and metering scope, available fault current, service entrance, utility protection, easements, inspections, energization, and conflicts with the serving utility's published requirements.
4. Reliability topology: N, N+1, 2N, A/B-path independence, concurrent maintainability, fault-domain boundaries, spare capacity, phased growth, and single points of failure. Do not infer a Uptime Tier or owner topology that the supplied documents do not establish.
5. Medium-voltage systems: switchgear, transformers, cables, terminations, grounding method, relaying, interlocks, arc-resistant construction, controls, testing, and utility interfaces.
6. Low-voltage distribution: switchgear, switchboards, panelboards, busway, remote power panels, power distribution units, rack-adjacent distribution, interrupting/SCCR ratings, maintainability, and consistent equipment schedules.
7. Generator and paralleling systems: quantity/redundancy, ratings and derating, starting, load steps, synchronization, load shedding, controls, neutral/grounding, fuel/mechanical/emissions interfaces, load-bank provisions, and acceptance criteria.
8. Transfer systems: ATS/STS selection, bypass-isolation, open/closed transition, neutral switching, source sensing, interlocks, maintenance modes, transfer/retransfer sequences, and coordination with generators, UPS, fire alarm, and controls.
9. UPS, batteries, and energy storage: topology, runtime, bypass, battery chemistry, BMS, ventilation/thermal management, listings, disconnects, maintenance, replacement, spill/thermal-runaway controls, and coordination with adopted fire/building-code ESS requirements. Verify project applicability and thresholds rather than assuming every UPS is governed identically.
10. Emergency, legally required standby, optional standby, and critical-operations classifications: source, transfer time, separation, wiring independence, load priority, selective coordination, duration, and conflicts between code-required and owner-reliability systems.
11. Load calculations and capacity: demand/diversity, continuous loads, future growth, inrush, harmonics, ambient/altitude derating, transformer/UPS/generator utilization, spare provisions, and consistency with schedules and one-lines.
12. Power-system studies and protection: short circuit, load flow, protective-device coordination, selective coordination, settings, equipment duty, arc-flash inputs/labels, and study update/turnover requirements. Distinguish NFPA 70 installation rules, IEEE 1584 calculation scope, and workplace-safety criteria such as NFPA 70E or CSA Z462.
13. Grounding and bonding: grounding-electrode systems, separately derived systems, high-resistance grounding, neutral bonding, equipment and signal-reference bonding, lightning bonds, raised-floor/rack bonds, telecom interfaces, and inconsistent conductor/electrode requirements.
14. Power quality: harmonics, neutral sizing, voltage/frequency limits, flicker, ride-through, power-factor correction, resonance, sensitive loads, monitoring, and mitigation responsibility.
15. Surge and lightning protection: risk/applicability basis, service and downstream SPD coordination, ratings, lead length, monitoring, grounding/bonding, external lightning systems, and cross-discipline interfaces.
16. Conductors and pathways: conductor material/insulation, ampacity, voltage drop, fill, bend radius, raceway/cable-tray selection, separation, firestopping, hazardous/wet/corrosive environments, identification, pulling/testing, and support.
17. Electrical rooms and equipment access: working/dedicated space, egress, door hardware, clearances, cooling, flood/water exposure, seismic restraint, equipment removal paths, fire ratings, housekeeping pads, security, and maintainability.
18. Normal and emergency lighting and controls: illuminance, egress lighting, controls, energy-code interfaces, exterior/site lighting, generator/UPS source assignments, testing, and schedule/spec conflicts.
19. EPMS, SCADA, and controls: metering architecture, points lists, protocols, time synchronization, alarm priorities, historian/data ownership, cybersecurity boundaries, sequences, network/power interfaces, and commissioning.
20. Energy performance and sustainability: ASHRAE 90.1/90.4 or adopted energy-code path, UPS efficiency, metering, controls, client carbon/renewable targets, reporting, and conflicts between code minimums and owner goals.
21. Testing, commissioning, and turnover: factory/field testing, NETA criteria where invoked, functional and integrated systems testing, load-bank tests, protection/controls validation, phased energization, witness/acceptance responsibility, O&M data, training, and baseline study files.
22. Product listing and certification: correct standard family/category, NRTL/listing for the US, Canadian certification by an SCC-accredited body, field evaluation, voltage/application limits, and installation instructions. A bare US UL mark is not automatically sufficient in Canada.
23. Multidisciplinary interfaces: mechanical loads/controls, fire alarm and suppression, architecture/rated construction/egress, structural anchorage, civil/utility routing, fuel systems, technology/security, sleeves/openings, and responsibility boundaries.
24. Procurement and specification quality: submittals, approved-equal paths, delegated design, warranties, spares, long-lead equipment, substitutions, commissioning responsibilities, and requirements assigned to the wrong party.
25. Master-specification and document integrity: wrong project, country, authority, listing regime, voltage, client, discipline, or obsolete product; unresolved options/placeholders; duplicate/out-of-sequence articles; broken related-section references; and products named without corresponding execution requirements (or vice versa)."""


_REVIEW_EXAMPLES = """\
Example 1 — valid EDIT (project-adoption conflict):
{
  "severity": "HIGH",
  "fileName": "26 05 00 Common Work Results for Electrical.docx",
  "section": "1.03",
  "issue": "The section hard-codes an NEC edition that conflicts with the adopted edition identified in the Project Requirements Profile.",
  "actionType": "EDIT",
  "existingText": "Comply with NFPA 70, 2017 edition.",
  "replacementText": "Comply with the edition of NFPA 70 adopted for the Project location, including applicable state and local amendments.",
  "codeReference": "Project Requirements Profile — adopted electrical code",
  "confidence": 0.94
}

Example 2 — valid ADD (missing acceptance criterion with a verbatim anchor):
{
  "severity": "HIGH",
  "fileName": "26 32 13 Engine Generators.docx",
  "section": "3.08",
  "issue": "The field-test article requires a load-bank test but provides no acceptance criterion or required record of voltage and frequency recovery.",
  "actionType": "ADD",
  "existingText": null,
  "replacementText": "Record load steps, voltage and frequency dip and recovery, alarms, protective-device operation, and final stable operation; acceptance limits shall be those stated in the approved project performance criteria and applicable adopted standard.",
  "anchorText": "3.08 FIELD QUALITY CONTROL",
  "insertPosition": "after",
  "codeReference": null,
  "confidence": 0.86
}

Example 3 — REPORT_ONLY (multi-section reliability conflict):
{
  "severity": "CRITICAL",
  "fileName": "26 36 23 Automatic Transfer Switches.docx",
  "section": "2.04",
  "issue": "The transfer-switch section permits both A and B distribution paths to transfer through one common bypass source, while the UPS sequence describes independent fault domains. Resolve the topology across the one-line, ATS, UPS, generator, and controls documents before editing any single section.",
  "actionType": "REPORT_ONLY",
  "existingText": null,
  "replacementText": null,
  "anchorText": null,
  "insertPosition": null,
  "codeReference": null,
  "confidence": 0.93
}

Example 4 — DO NOT REPORT:
Do not report generic coordination boilerplate, a newer standards edition merely
because it exists, an Uptime Tier or redundancy requirement that the supplied
documents do not establish, or the presumed contents of a confidential owner
standard that was cited but not provided. Emit a finding only for a concrete,
quoted defect or a grounded missing requirement.
"""


_REVIEW_SEVERITY_DEFINITIONS = """\
CRITICAL — a life-safety/permit failure, equipment duty below available fault current, crossed or common-mode A/B power path, unsafe emergency-power defect, or direct controlling requirement conflict likely to cause outage, injury, or approval failure.
HIGH — a material reliability, utility, protection, constructability, commissioning, listing, or client-requirement defect that must be corrected before issue.
MEDIUM — a meaningful but bounded technical, edition, performance, or coordination defect with a clear correction path.
GRIPES — editorial and specification-quality defects that do not materially change electrical performance but should be corrected."""


_CROSS_CHECK_SEVERITY_DEFINITIONS = """\
CRITICAL — a package-level contradiction likely to defeat required emergency power, cross independent fault domains, exceed equipment duty, create an unsafe sequence, or block utility/AHJ approval.
HIGH — a major conflict in topology, ratings, protection, controls, testing, or responsibility that must be resolved before issue.
MEDIUM — a meaningful inconsistency in equipment data, settings, products, schedules, terminology, or related-section references.
GRIPES — minor package coordination, naming, or formatting inconsistencies."""


_VERIFIER_SOURCE_PRIORITIES = """\
1. Project-location authorities and utilities:
   official adopting statutes/regulations; state/local electrical boards,
   electrical inspectors, building/fire authorities, and serving-utility
   service/interconnection manuals; for Canada, NRC and provincial/territorial
   legislation, safety authorities, inspection bodies, and utility standards.

2. Code and standards publishers:
   nfpa.org, codes.iccsafe.org, csagroup.org, scc-ccn.ca, standards.ieee.org,
   netaworld.org, nema.org, ashrae.org, and ansi.org.

3. Listing, certification, and field-evaluation authorities:
   ul.com / Product iQ, csagroup.org certification directories, Intertek/ETL,
   and official directories of accredited Canadian certification or field-
   evaluation bodies.

4. Workplace-safety authorities when the claim is operational rather than design:
   osha.gov, ccohs.ca, and the applicable provincial/territorial OHS regulator.

5. Manufacturer technical literature for product-specific claims only:
   Eaton, Schneider Electric/Square D, Siemens, ABB, GE Vernova, Vertiv,
   ASCO, Russelectric, Cummins, Caterpillar, Kohler, SEL, Legrand/Starline,
   and PDI official sites. Never use reseller or SEO pages as compliance proof.

6. Owner/industry criteria and archives:
   Uptime Institute, BICSI, insurer, and owner criteria only when the project
   invokes them; archive.org only to establish historical requirements."""


_DETECTOR_VOCABULARY = DetectorVocabulary(
    code_abbreviations=("IBC", "IFC", "IECC", "IEBC"),
    plausible_cycle_years=("2009", "2012", "2015", "2018", "2021", "2024"),
    valid_cycle_years=("2009", "2012", "2015", "2018", "2021", "2024", "2027"),
    asce7_plausible_editions=("88", "93", "95", "98", "02", "05", "10", "16", "22"),
    stale_cycle_extra_patterns=(
        r"\b(20\d{2})\s+International\s+(?:Building|Fire|Energy Conservation|Existing Building)\s+Code\b",
    ),
    flag_leed_references=False,
    jurisdiction_label="",
)


_PROFILE_KEYWORDS = ProfileKeywords(
    jurisdictional=(
        "electrical inspector",
        "inspection authority",
        "authority having jurisdiction",
        "ahj",
        "serving utility",
        "utility service",
        "interconnection",
        "state electrical code",
        "provincial electrical code",
        "local amendment",
        "permit",
        "plan review",
        "field evaluation",
        "fire marshal",
        "building official",
    ),
    manufacturer=(
        "eaton",
        "square d",
        "schneider",
        "siemens",
        "abb",
        "ge vernova",
        "vertiv",
        "asco",
        "russelectric",
        "cummins",
        "caterpillar",
        "kohler",
        "schweitzer",
        "sel-",
        "starline",
        "pdi",
        "model number",
        "model no",
        "datasheet",
        "submittal",
        "listed product",
        "basis of design",
        "or approved equal",
    ),
    code_standard=(
        "nfpa 70",
        "national electrical code",
        "csa c22.1",
        "canadian electrical code",
        "nfpa 70b",
        "nfpa 70e",
        "nfpa 110",
        "nfpa 111",
        "ibc",
        "ifc",
        "iecc",
        "ieee",
        "neta",
        "ul ",
        "ul-",
        "ulc",
        "csa",
        "nema",
        "ansi",
        "ashrae 90.1",
        "ashrae 90.4",
        "electrical code",
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
    ChunkGroup("procurement_general", "Procurement / General Requirements", ("00", "01")),
    ChunkGroup(
        "building_equipment_interfaces",
        "Structure / Enclosure / Equipment Interfaces",
        ("03", "05", "07", "08", "11", "13", "14"),
    ),
    ChunkGroup(
        "mechanical_fire_interfaces",
        "Fire / Plumbing / Mechanical Interfaces",
        ("21", "22", "23", "25"),
    ),
    ChunkGroup(
        "electrical_technology",
        "Electrical / Technology / Electronic Safety",
        ("26", "27", "28"),
    ),
    ChunkGroup(
        "site_utility_generation",
        "Site / Utility / Power Generation",
        ("31", "32", "33", "34", "48"),
    ),
)


_RESEARCH_PERSONA = (
    "You are an electrical code, utility, and owner-requirements researcher for "
    "hyperscale data-center projects in the United States and Canada. Report only "
    "facts supported by sources you actually retrieved. Distinguish adopted law, "
    "utility rules, AHJ requirements, owner criteria, consensus standards, and "
    "common practice; never convert a benchmark into a controlling requirement."
)


_RESEARCH_DIMENSIONS = (
    ResearchDimension(
        dimension_id="governing_codes_certification",
        title="Governing electrical codes, amendments, and certification",
        max_searches=24,
        max_fetches=8,
        prompt_template=(
            "Determine the electrical code basis currently in force for a new "
            "hyperscale data-center project in {city}, {state_or_province}, "
            "{country}. For a US project, retrieve the adopted NFPA 70/NEC edition, "
            "state and local amendments, related IBC/IFC/IECC provisions, effective "
            "dates, electrical licensing/inspection rules, and the standards editions "
            "actually incorporated by those instruments. For a Canadian project, "
            "retrieve the province/territory's adopted CSA C22.1/Canadian Electrical "
            "Code edition and amendments, applicable NBC/NFC/NECB or provincial code "
            "lineage, effective dates, inspection/licensing regime, and recognized "
            "certification and field-evaluation paths. Distinguish adopted editions "
            "from publisher-current and owner-invoked editions; do not infer one from "
            "a model-code year. The US fallback is IBC {ibc}, IFC {ifc}, IECC {iecc}, "
            "IEBC {iebc}, and ASCE {asce7} with Supplement 1, but project-location "
            "adoptions govern. Prefer official adopting instruments and authority "
            "publications."
        ),
    ),
    ResearchDimension(
        dimension_id="utility_service_interconnection",
        title="Serving utility and interconnection requirements",
        max_searches=20,
        max_fetches=7,
        prompt_template=(
            "Identify the serving electric utility and its published requirements for "
            "a hyperscale data center in {city}, {state_or_province}, {country}: "
            "available service voltages/configurations; service and transformer "
            "ownership; metering; available-fault-current data and study process; "
            "customer switchgear, relaying, grounding, protection, and control; "
            "redundant services; generator or DER interconnection; power-quality "
            "limits; duct-bank/easement/inspection standards; and energization "
            "prerequisites. Separate design requirements from project-specific facts "
            "that remain unknown and from process advisories such as queue timing, "
            "fees, study duration, and application milestones. Cite official utility "
            "manuals, tariffs, interconnection rules, and regulator decisions."
        ),
    ),
    ResearchDimension(
        dimension_id="ahj_permitting_emergency_power",
        title="Electrical AHJs, emergency power, and ESS permitting",
        max_searches=20,
        max_fetches=6,
        prompt_template=(
            "Identify every authority and published requirement affecting electrical "
            "work for a data-center project in {city}, {state_or_province}, {country}: "
            "electrical/building/fire plan review, sealed documents, permits and "
            "inspections, emergency and standby power classification, generators and "
            "fuel/emissions/noise interfaces, UPS and energy-storage thresholds, "
            "battery rooms, witnessed tests, labeling, field evaluation, utility "
            "release, and energization/occupancy prerequisites. Identify the exact "
            "standard editions the adopted instruments invoke. Treat fees, notice "
            "periods, and scheduling facts as process advisories rather than text the "
            "construction specification must necessarily contain."
        ),
    ),
    ResearchDimension(
        dimension_id="client_reliability_commissioning",
        title="Client electrical reliability and commissioning criteria",
        max_searches=14,
        max_fetches=5,
        prompt_template=(
            "Research retrievable electrical design and construction requirements of "
            "{client_name} for hyperscale data centers: topology and A/B fault domains, "
            "redundancy, maintainability, capacity/growth, approved equipment, utility "
            "strategy, power quality, UPS/battery/generator runtime and controls, EPMS, "
            "metering, testing, commissioning and integrated systems testing, spares, "
            "energy/carbon targets, and insurer/risk criteria. State any Uptime Tier "
            "only when an authoritative source assigns it. Use owner sources and "
            "public project filings first; if the actual owner standard is confidential "
            "or merely cited by the specs, say so and do not invent its contents."
        ),
    ),
    ResearchDimension(
        dimension_id="site_environment_electrical_design",
        title="Site hazards and electrical design environment",
        max_searches=12,
        max_fetches=5,
        prompt_template=(
            "Identify official site and environmental inputs for electrical "
            "specifications in {city}, {state_or_province}, {country}: seismic, wind, "
            "flood, wildfire and lightning context; ambient temperature, altitude, "
            "humidity, corrosion and equipment derating; snow/ice and generator/"
            "switchgear enclosures; hazardous or classified conditions where actually "
            "applicable; frost/underground utility constraints; and resilience criteria "
            "for electrical rooms and outdoor equipment. US projects use the "
            "jurisdiction's adopted ASCE 7 edition and incorporated supplements; "
            "Canadian projects use the governing NBC/provincial framework. Cite "
            "official hazard, climate, code, utility, and authority sources and "
            "separate design requirements from investigations the project team must "
            "still perform."
        ),
    ),
)


_COMPLIANCE_PERSONA = (
    "You are an electrical code-compliance reviewer for hyperscale data-center "
    "specification packages. Evaluate whether the package correctly represents "
    "the project's adopted electrical/building/fire/energy codes, utility and AHJ "
    "requirements, product-certification regime, site hazards, and controlling "
    "client reliability criteria."
)


_COMPLIANCE_SEVERITY_DEFINITIONS = """\
CRITICAL — the package omits or contradicts a controlling life-safety, emergency-power, equipment-duty, utility, or electrical-code requirement in a way likely to block approval, create an unsafe condition, or defeat required power continuity.
HIGH — a material adopted-code, AHJ, utility, listing, site-hazard, or client requirement is missing or misrepresented and must be corrected before issue.
MEDIUM — a controlling requirement is represented incompletely or imprecisely, but the deficiency is bounded and has a clear correction path.
GRIPES — editorial or traceability gaps in how electrical project requirements are cited or coordinated."""


_POLITY_SUSPECT_TOKENS = (
    PolityTokenRule(
        country="CA",
        pattern=r"\bNFPA\s*70\b|\bNEC\b|(?i:\bNational Electrical Code\b)",
        note=(
            "NFPA 70 / NEC is the US model electrical code. Confirm the Canadian "
            "project's adopted CSA C22.1 / Canadian Electrical Code and amendments; "
            "retain NEC only if the client separately invokes it as a benchmark."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bOSHA\b",
        note=(
            "OSHA is a US federal authority. Canadian workplace requirements arise "
            "under the applicable federal or provincial/territorial regime."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bU\.?L\.?[- ]+(?i:Listed|Listing)\b",
        note=(
            "A bare US UL listing may not establish Canadian approval. Verify a "
            "certification mark/category accepted by the provincial authority and an "
            "SCC-accredited body, or the applicable field-evaluation path."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bIBC\b|\bIFC\b|\bIECC\b|\bIEBC\b",
        note=(
            "Bare I-code citations are likely US master-spec remnants unless the "
            "Canadian Project Requirements Profile confirms a benchmark or adoption "
            "relationship."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"(?i:\bBuy American\b|\bMade in (?:the )?USA\b|\bdomestically made\b)",
        note=(
            "US domestic-sourcing language is suspicious on a Canadian project; "
            "confirm the owner's actual procurement requirement before retaining it."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bCSA\s*C22\.1\b|(?i:\bCanadian Electrical Code\b|\bCEC Part I\b)",
        note=(
            "CSA C22.1 / Canadian Electrical Code is a Canadian model code. Verify "
            "the US jurisdiction's adopted NFPA 70/NEC edition instead."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bCSA\s*Z462\b",
        note=(
            "CSA Z462 is a Canadian workplace electrical-safety standard; verify the "
            "US project's applicable workplace/owner safety criteria."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=(
            r"\bNBC\b|\bNECB\b|"
            r"\bNFC\b(?=\s*(?:\(?20\d{2}\)?|of Canada\b))|"
            r"(?i:\bNational (?:Building|Fire) Code of Canada\b)"
        ),
        note=(
            "This is Canadian code vocabulary and is likely a wrong-polity remnant "
            "unless the US project deliberately invokes it as a benchmark."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bULC\b|\bCSA\s*SPE-?1000\b",
        note=(
            "This Canadian certification/field-evaluation reference may not satisfy "
            "the US project's NRTL/listing requirements; verify the intended product "
            "approval path."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"(?i:\bOntario Electrical Safety Code\b|\bTechnical Safety BC\b)|O\.?\s*Reg\.?\s*\d+[/\-]\d+",
        note=(
            "This names a Canadian provincial authority or regulation and is likely "
            "a master-spec remnant on a US project."
        ),
    ),
)


_CORPUS_SIGNAL_PATTERNS = (
    r"\bbasis of design\b",
    r"\bBoD\b",
    r"\bowner'?s? project requirements\b",
    r"\bOPR\b",
    r"\belectrical design (?:criteria|guide(?:lines)?|standards?)\b",
    r"\bowner master specifications?\b",
    r"\bsingle[- ]line diagram\b",
    r"\bone[- ]line diagram\b",
    r"\bpower[- ]system stud(?:y|ies)\b",
    r"\bshort[- ]circuit stud(?:y|ies)\b",
    r"\bcoordination stud(?:y|ies)\b",
    r"\barc[- ]flash stud(?:y|ies)\b",
    r"\bprotection and control philosophy\b",
    r"\breliability (?:topology|criteria)\b",
    r"\bintegrated systems test(?:ing)?\b",
    r"\bsequence of operations\b",
    r"\bEPMS point(?:s)? list\b",
    r"\butility service requirements?\b",
)


DATACENTER_ELECTRICAL = ReviewModule(
    module_id="datacenter_electrical",
    display_name="Hyperscale Data Center — Electrical (US/Canada)",
    description=(
        "Electrical specifications for hyperscale data-center projects in the "
        "United States and Canada, with project-location, utility, AHJ, client, "
        "reliability, and certification research."
    ),
    cycle=DATACENTER_ELECTRICAL_IBC_2024,
    reviewer_persona=(
        "You are an electrical specification reviewer specializing in hyperscale "
        "data-center facilities in the United States and Canada. Review utility "
        "service through rack-adjacent distribution, MV/LV systems, generators, "
        "transfer systems, UPS/energy storage, protection, grounding, power quality, "
        "lighting, EPMS, testing/commissioning, and multidisciplinary interfaces "
        "against the Project Requirements Profile and submitted project documents."
    ),
    review_user_intro=(
        "Review the following electrical specification for a hyperscale data-center "
        "project. When Project Context contains a Project Requirements Profile, its "
        "adopted codes/amendments, utility and AHJ rules, certification regime, site "
        "criteria, and controlling client standards take precedence over the module's "
        "US model-code fallback. Treat cited but unprovided owner/design documents "
        "conditionally and do not invent their content."
    ),
    review_severity_definitions=_REVIEW_SEVERITY_DEFINITIONS,
    review_confidence_high_example=(
        'an explicit "2017 NEC" governing-code statement where the supplied '
        "Project Requirements Profile identifies a different adopted edition"
    ),
    review_categories_template=_REVIEW_CATEGORIES,
    review_examples=_REVIEW_EXAMPLES,
    cross_check_persona=(
        "You are a cross-specification coordination reviewer for electrical "
        "packages on hyperscale data-center projects."
    ),
    cross_check_severity_definitions=_CROSS_CHECK_SEVERITY_DEFINITIONS,
    verifier_persona=(
        "You are a construction-specification verification assistant for "
        "electrical systems on hyperscale data-center projects in the United "
        "States and Canada."
    ),
    verifier_source_priorities=_VERIFIER_SOURCE_PRIORITIES,
    review_user_code_basis_line=(
        "US model-code fallback: IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}, "
        "ASCE {asce7} with Supplement 1. Fallback electrical references: "
        "{pinned_standards}. Project-profile adoptions govern; Canadian projects "
        "use the researched provincial/territorial code basis."
    ),
    cross_check_code_basis_line=(
        "US model-code fallback: IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}, "
        "ASCE {asce7} with Supplement 1. Fallback electrical references: "
        "{pinned_standards}. Project-profile adoptions govern."
    ),
    verifier_system_code_basis_lines=(
        "US model-code fallback: IBC {ibc}, IFC {ifc}, IECC {iecc}, IEBC {iebc}, "
        "ASCE {asce7} with Supplement 1.\nUse the Project Requirements Profile's "
        "adopted electrical code, amendments, utility/AHJ rules, and Canadian "
        "provincial/territorial basis when present."
    ),
    verifier_user_code_basis_lines=(
        "US fallback references: {pinned_standards}.\nProject-profile adoptions and "
        "applicability govern; distinguish installation code, energy code, workplace "
        "safety, maintenance, and owner criteria."
    ),
    detector_vocabulary=_DETECTOR_VOCABULARY,
    profile_keywords=_PROFILE_KEYWORDS,
    cross_check_chunk_groups=_CROSS_CHECK_CHUNK_GROUPS,
    report_context_phrase="hyperscale data-center electrical projects",
    report_title="Spec Critic — Electrical Specification Review Report",
    project_profile_enabled=True,
    research_persona=_RESEARCH_PERSONA,
    research_dimensions=_RESEARCH_DIMENSIONS,
    corpus_signal_patterns=_CORPUS_SIGNAL_PATTERNS,
    compliance_persona=_COMPLIANCE_PERSONA,
    compliance_severity_definitions=_COMPLIANCE_SEVERITY_DEFINITIONS,
    polity_suspect_tokens=_POLITY_SUSPECT_TOKENS,
)
