"""Hyperscale data-center electronic safety and security module (US / Canada).

Phase 1 is deliberately limited to fire detection and alarm.  The stable
module identity can later grow to other Division 28 systems, but this version
does not claim competence for access control, video surveillance, intrusion
detection, detention security, or general communications.

The 2024 I-code family and its referenced NFPA editions are a US model-code
fallback.  Every run researches the actual state, provincial/territorial,
local, AHJ, electrical, monitoring, certification, and client requirements.
Canadian CAN/ULC editions are intentionally project data because national
model codes have force only through provincial or territorial adoption.

NFPA 72, NFPA 70/NEC, and Canadian code abbreviations are absent from the
single-year deterministic cycle detector.  Their valid edition years do not
necessarily match the primary I-code year; location research and semantic
review handle edition alignment without false stale-cycle alerts.
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
_IFC_CH9 = (
    "https://codes.iccsafe.org/content/IFC2024V2.0/"
    "chapter-9-fire-protection-and-life-safety-systems"
)
_IFC_CH80 = (
    "https://codes.iccsafe.org/content/IFC2024P1/"
    "chapter-80-referenced-standards"
)


DATACENTER_ELECTRONIC_SAFETY_SECURITY_IBC_2024 = CodeCycle(
    label="dc-electronic-safety-fire-alarm-ibc-2024",
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
            source="ICC 2024 IFC v2.0: https://codes.iccsafe.org/content/IFC2024V2.0",
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
            "NFPA 72",
            "2022",
            source=(
                "2024 IBC Ch. 35 / IFC Chs. 9 and 80, verified 2026-07-20: "
                f"{_IBC_CH35} ; {_IFC_CH9} ; {_IFC_CH80} ; "
                "https://link.nfpa.org/all-publications/72/2022"
            ),
        ),
        StandardEdition(
            "NFPA 70 (NEC)",
            "2023",
            note="US fire-alarm wiring fallback; actual adoption governs",
            source=(
                "2024 IFC Section 907.6.1 / Ch. 80, verified 2026-07-20: "
                f"{_IFC_CH9} ; {_IFC_CH80}"
            ),
        ),
    ),
)


_REVIEW_CATEGORIES = """\
1. Internal contradictions, including inconsistent system type, device quantities, pathway class, survivability, voltage, battery duration, sequence, testing, warranty, or responsibility requirements.
2. Project-specific code and edition alignment: the Project Requirements Profile's adopted building, fire, electrical, accessibility, and existing-building codes and amendments govern. The US fallback is IBC {ibc}, IFC {ifc}, IEBC {iebc}, ASCE {asce7}, and these referenced editions: {pinned_standards}. Distinguish adopted, publisher-current, and owner-invoked editions. Canadian projects use the researched provincial/territorial basis and applicable CAN/ULC editions.
3. Scope and system classification: protected-premises, supervising-station, emergency communications, releasing, dedicated-function, and combination-system boundaries; owner criteria versus code minimums; and clear responsibility for design, programming, monitoring, and certification.
4. System architecture and reliability: fire-alarm control units, network nodes, annunciators, gateways, fault domains, network topology, spare capacity, degraded modes, survivability, and single points of failure appropriate to the established project criteria.
5. Initiating devices: smoke, heat, beam, flame, gas, duct, multi-criteria, manual, waterflow, supervisory, and special-purpose detection; spacing/listing/environment, alarm verification, nuisance-alarm controls, and consistent schedules.
6. Aspirating and air-sampling smoke detection: sampling-pipe design responsibility, transport time, sensitivity/thresholds, environmental compensation, filtration, alarm levels, coverage, testing, and interfaces with suppression and HVAC controls.
7. Notification and emergency communications: audible/visible/tactile appliances, voice evacuation or emergency communications, intelligibility versus audibility, synchronization, zoning, accessibility, ambient noise, message priority, and survivability.
8. Annunciation and operator interfaces: fire-command-center and remote annunciation, graphic displays, zone maps, event text, priorities, controls, printer/logging requirements, and consistent device/point naming.
9. Cause-and-effect and sequence documentation: every alarm, supervisory, trouble, pre-alarm, disablement, and emergency-control output must have an unambiguous initiating condition, delay, latch/reset behavior, priority, responsible system, and acceptance test.
10. Suppression and releasing interfaces: sprinkler waterflow/supervisory signals, pre-action and clean-agent releasing, abort/manual release, cross-zoning, detection voting, discharge notification, shutdowns, lockouts, and conflicts with Division 21 sequences.
11. HVAC, smoke-control, damper, and fan interfaces: control ownership, permissives, proof/status, time delays, firefighters' controls, smoke-control panel coordination, and fail-safe behavior.
12. Elevator, door, access-control, and egress interfaces: recall, shunt trip, hoistway/machine-room detection, door release/unlocking, delayed/controlled egress, stair doors, security override, and division-of-responsibility boundaries.
13. Primary and secondary power: dedicated branch circuits, disconnect identification/locking, surge protection, grounding, charger/power-supply loading, battery calculations, standby/alarm duration, voltage drop, generator transfer, and consistency with Division 26.
14. Signaling-line, notification, data, and control pathways: class/style where required by the adopted standard, routing, separation, fault isolation, short-circuit protection, conductor/raceway selection, pathway survivability, firestopping, and cable-support requirements.
15. Supervising-station and external communications: service type, transmission paths, communicator compatibility, network/cellular/radio dependencies, signal categories, account/site data, cybersecurity boundaries, ownership, testing, and AHJ acceptance.
16. Product listing, compatibility, and certification: control-unit/device/power-supply compatibility, US NRTL listing or Canadian SCC-recognized certification/field evaluation, correct product category, environmental rating, software/firmware compatibility, and installation instructions. Do not assume a bare US UL mark is sufficient in Canada.
17. Programming, software, data, and cybersecurity: programming ownership, cause-and-effect source of truth, passwords/roles, remote access, backups, version control, database handover, change authorization, time synchronization, and isolation from non-life-safety networks.
18. Testing, commissioning, and integrated systems testing: factory/field tests, device-by-device acceptance, audibility/intelligibility, battery and voltage-drop tests, pathway fault tests, supervising-station receipt, interface demonstrations, integrated testing, witnesses, deficiencies, retesting, and records.
19. Phasing, impairment, and occupied-operation requirements: temporary detection, cutovers, migration, existing-system compatibility, impairment permits, fire watch, notifications, rollback, record updates, and protection during phased data-hall turnover.
20. Closeout and lifecycle information: record drawings, device addresses, point lists, sequence/cause-effect matrix, calculations, test reports, certificates, programming/database files, licenses, training, spare parts, maintenance responsibilities, and recurring-test baseline data.
21. Data-center conditions: high-airflow and high-ceiling spaces, containment, underfloor/overhead voids, cable density, commissioning smoke sources, staged fit-out, white-space operations, battery/UPS rooms, generator support spaces, and client reliability constraints supported by project documents.
22. Multidisciplinary coordination: architecture, fire suppression, electrical, mechanical, controls, elevators, security, telecom, and civil/emergency-responder interfaces; identify conflicting scopes and requirements assigned to the wrong party.
23. Procurement and specification quality: delegated design, submittals, approved equals, long-lead products, proprietary compatibility, licensing, warranties, service agreements, monitoring contracts, substitutions, and acceptance responsibility.
24. Master-specification and document integrity: wrong project, country, authority, listing regime, client, system, device family, or obsolete product; unresolved options/placeholders; duplicated or broken articles; mismatched section numbers/titles; and non-fire-alarm Division 28 content that belongs to a later module phase."""


_REVIEW_EXAMPLES = """\
Example 1 — valid EDIT (adopted-edition conflict):
{
  "severity": "HIGH",
  "fileName": "28 46 00 Fire Detection and Alarm.docx",
  "section": "1.03",
  "issue": "The section hard-codes an NFPA 72 edition that conflicts with the adopted edition identified in the Project Requirements Profile.",
  "actionType": "EDIT",
  "existingText": "Comply with NFPA 72, 2016 edition.",
  "replacementText": "Comply with the edition of NFPA 72 adopted for the Project location, including applicable amendments.",
  "codeReference": "Project Requirements Profile — adopted fire-alarm standard",
  "confidence": 0.94
}

Example 2 — valid ADD (missing calculation with a verbatim anchor):
{
  "severity": "HIGH",
  "fileName": "28 46 00 Fire Detection and Alarm.docx",
  "section": "1.05",
  "issue": "The design-submittal article requires shop drawings but omits secondary-power and notification-appliance-circuit voltage-drop calculations needed to validate the proposed system.",
  "actionType": "ADD",
  "existingText": null,
  "replacementText": "Submit primary- and secondary-power calculations and notification/control-circuit voltage-drop calculations for the complete connected system, including spare capacity and the adopted standby/alarm duration criteria.",
  "anchorText": "1.05 ACTION SUBMITTALS",
  "insertPosition": "after",
  "codeReference": null,
  "confidence": 0.88
}

Example 3 — REPORT_ONLY (multidisciplinary sequence conflict):
{
  "severity": "CRITICAL",
  "fileName": "28 46 00 Fire Detection and Alarm.docx",
  "section": "3.08",
  "issue": "The fire-alarm sequence calls for immediate release of the pre-action valve on one detector, while the same section's releasing-system criteria require cross-zoned detection. Reconcile the fire-alarm cause-and-effect matrix, then coordinate the resolved sequence with Division 21 before editing an interface requirement in isolation.",
  "actionType": "REPORT_ONLY",
  "existingText": null,
  "replacementText": null,
  "anchorText": null,
  "insertPosition": null,
  "codeReference": null,
  "confidence": 0.95
}

Example 4 — DO NOT REPORT:
Do not report ordinary coordination boilerplate, a newer standard merely because
it exists, access-control/video/intrusion requirements as though this phase
reviewed those systems, or the presumed contents of confidential owner criteria
that were cited but not provided. Emit only concrete, quoted, grounded defects.
"""


_REVIEW_SEVERITY_DEFINITIONS = """\
CRITICAL — a life-safety, emergency-control, releasing, pathway-survivability, power, notification, or approval defect likely to prevent required alarm operation, cause unintended discharge/shutdown, or block occupancy.
HIGH — a material detection, sequence, interface, listing, monitoring, testing, reliability, or controlling-requirement defect that must be corrected before issue.
MEDIUM — a meaningful but bounded technical, edition, documentation, performance, or coordination defect with a clear correction path.
GRIPES — editorial and specification-quality defects that do not materially change fire-alarm performance but should be corrected."""


_CROSS_CHECK_SEVERITY_DEFINITIONS = """\
CRITICAL — a package-level contradiction likely to defeat required detection/notification, cause an unsafe release or emergency-control action, compromise survivability/power, or block AHJ approval.
HIGH — a major conflict in architecture, zoning, sequence, interfaces, products, monitoring, testing, or responsibility that must be resolved before issue.
MEDIUM — a meaningful inconsistency in device data, calculations, messages, points, terminology, schedules, or related-section references.
GRIPES — minor package coordination, naming, or formatting inconsistencies."""


_VERIFIER_SOURCE_PRIORITIES = """\
1. Project-location authorities and adopted instruments:
   official state/provincial/territorial statutes and regulations; municipal
   building/fire authorities, fire-prevention bureaus, electrical inspectors,
   fire marshals, and published permit/monitoring/acceptance requirements.

2. Code and standards publishers:
   nfpa.org, codes.iccsafe.org, nrc.canada.ca, ulc.ca, csagroup.org,
   scc-ccn.ca, and official provincial or territorial code publishers.

3. Listing, certification, and approval directories:
   UL Product iQ, FM Approvals, CSA certification directories, Intertek/ETL,
   and official Canadian accredited certification/field-evaluation directories.

4. Supervising-station and emergency-service authorities:
   official municipal communications requirements, recognized listing
   directories, and the project's contracted service documentation when supplied.

5. Manufacturer technical literature for product-specific claims only:
   Notifier/Honeywell, Siemens, Edwards/EST, Simplex/Johnson Controls, Mircom,
   Potter, Bosch, Kidde, Fike, Xtralis/VESDA, and other official manufacturer
   sites. Never use reseller pages as code, compatibility, or listing proof.

6. Owner/insurer/industry criteria:
   owner standards, FM Global data sheets, and insurer criteria only when the
   project invokes them; archive.org only for historical requirements."""


_DETECTOR_VOCABULARY = DetectorVocabulary(
    code_abbreviations=("IBC", "IFC", "IEBC"),
    plausible_cycle_years=("2009", "2012", "2015", "2018", "2021", "2024"),
    valid_cycle_years=("2009", "2012", "2015", "2018", "2021", "2024", "2027"),
    asce7_plausible_editions=("88", "93", "95", "98", "02", "05", "10", "16", "22"),
    stale_cycle_extra_patterns=(
        r"\b(20\d{2})\s+International\s+(?:Building|Fire|Existing Building)\s+Code\b",
    ),
    flag_leed_references=False,
    jurisdiction_label="",
)


_PROFILE_KEYWORDS = ProfileKeywords(
    jurisdictional=(
        "fire marshal",
        "fire prevention bureau",
        "fire department",
        "building official",
        "electrical inspector",
        "authority having jurisdiction",
        "ahj",
        "local amendment",
        "permit",
        "plan review",
        "occupancy approval",
        "supervising station",
        "monitoring approval",
        "provincial fire code",
        "territorial fire code",
    ),
    manufacturer=(
        "notifier",
        "honeywell",
        "siemens",
        "edwards",
        "est4",
        "simplex",
        "johnson controls",
        "mircom",
        "potter",
        "bosch",
        "kidde",
        "fike",
        "xtralis",
        "vesda",
        "model number",
        "model no",
        "datasheet",
        "submittal",
        "listed product",
        "compatibility list",
        "basis of design",
        "or approved equal",
    ),
    code_standard=(
        "nfpa 72",
        "national fire alarm and signaling code",
        "nfpa 70",
        "national electrical code",
        "ibc",
        "ifc",
        "iebc",
        "can/ulc-s524",
        "can/ulc-s536",
        "can/ulc-s537",
        "can/ulc-s561",
        "can/ulc-s1001",
        "ul 864",
        "ul 268",
        "ulc listed",
        "fm approved",
        "accessibility code",
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
        "building_vertical_interfaces",
        "Architecture / Openings / Conveying Interfaces",
        ("07", "08", "10", "14"),
    ),
    ChunkGroup(
        "suppression_mechanical_interfaces",
        "Fire Suppression / Mechanical Interfaces",
        ("21", "23", "25"),
    ),
    ChunkGroup("electrical_power", "Electrical Power Interfaces", ("26",)),
    ChunkGroup(
        "electronic_safety",
        "Communications / Electronic Safety and Security",
        ("27", "28"),
    ),
)


_RESEARCH_PERSONA = (
    "You are a fire-alarm code, AHJ, certification, and owner-requirements "
    "researcher for hyperscale data-center projects in the United States and "
    "Canada. Report only facts supported by retrieved sources. Distinguish "
    "adopted law, AHJ rules, electrical requirements, listing/certification, "
    "monitoring-service rules, owner criteria, and common practice."
)


_RESEARCH_DIMENSIONS = (
    ResearchDimension(
        dimension_id="governing_codes_certification",
        title="Governing fire-alarm codes, editions, and certification",
        max_searches=24,
        max_fetches=8,
        prompt_template=(
            "Determine the fire detection/alarm code basis currently in force for "
            "a hyperscale data-center project in {city}, {state_or_province}, "
            "{country}. For a US project, retrieve the adopted building, fire, "
            "electrical, accessibility, and existing-building codes; NFPA 72 and "
            "NFPA 70 editions; amendments; effective dates; and required listing or "
            "field-evaluation paths. For Canada, retrieve the province/territory's "
            "adopted building, fire, and electrical codes and exact applicable "
            "CAN/ULC-S524, S536, S537, S561, S1001, and related editions. National "
            "model publications do not establish local force by themselves. The US "
            "fallback is IBC {ibc}, IFC {ifc}, IEBC {iebc}, ASCE {asce7}, and "
            "{pinned_standards}, but project-location adoption governs. Prefer "
            "official adopting instruments, authorities, and standards publishers."
        ),
    ),
    ResearchDimension(
        dimension_id="ahj_permitting_monitoring",
        title="AHJ permitting, monitoring, and acceptance requirements",
        max_searches=22,
        max_fetches=7,
        prompt_template=(
            "Identify every authority and published fire-alarm requirement for "
            "{city}, {state_or_province}, {country}: design professional or "
            "contractor licensing, delegated-design rules, permit/submittal forms, "
            "sequence and battery calculations, device/zoning drawings, monitoring "
            "connection and signal-transmission rules, fire-department key/annunciator "
            "requirements, emergency responder interfaces, inspections, witnessed "
            "acceptance and integrated tests, completion/verification certificates, "
            "impairment procedures, and occupancy prerequisites. Separate controlling "
            "design requirements from fees, scheduling, and process advisories."
        ),
    ),
    ResearchDimension(
        dimension_id="detection_notification_special_hazards",
        title="Detection, notification, and special-hazard criteria",
        max_searches=18,
        max_fetches=6,
        prompt_template=(
            "Research official or controlling requirements applicable in {city}, "
            "{state_or_province}, {country} for high-airflow data halls, aspirating "
            "detection, high ceilings, underfloor/overhead voids, battery and UPS "
            "rooms, generator/support spaces, pre-action and clean-agent releasing, "
            "audible/visible notification, voice or emergency communications, "
            "intelligibility, pathway survivability, smoke control, elevator recall, "
            "door release, and mass-notification boundaries. State thresholds and "
            "applicability; do not turn optional owner practices into code mandates."
        ),
    ),
    ResearchDimension(
        dimension_id="client_sequences_reliability_commissioning",
        title="Client sequences, reliability, and commissioning criteria",
        max_searches=14,
        max_fetches=5,
        prompt_template=(
            "Research retrievable fire-alarm requirements of {client_name} for "
            "hyperscale data centers: approved platforms, network/fault-domain "
            "architecture, detection strategy, alarm thresholds, point naming, "
            "cause-and-effect matrices, suppression/HVAC/elevator/security interfaces, "
            "monitoring, cybersecurity, phasing, testing, integrated systems testing, "
            "turnover data, service agreements, and spares. Use owner sources and "
            "public filings first. If owner criteria are confidential or merely cited "
            "by the specs, say so and do not invent their contents."
        ),
    ),
    ResearchDimension(
        dimension_id="site_campus_emergency_interfaces",
        title="Site, campus, and emergency-response interfaces",
        max_searches=12,
        max_fetches=5,
        prompt_template=(
            "Identify site/campus facts and official requirements in {city}, "
            "{state_or_province}, {country} affecting fire-alarm specifications: "
            "campus network/monitoring topology, emergency dispatch or municipal "
            "connection, responder radio and command-center interfaces, climate and "
            "environmental ratings, lightning/surge exposure, seismic restraint, "
            "construction phasing, occupied operations, impairment/fire-watch rules, "
            "and coordination with utility or emergency-power outages. Separate known "
            "requirements from project investigations still needed."
        ),
    ),
)


_COMPLIANCE_PERSONA = (
    "You are a fire detection and alarm compliance reviewer for hyperscale "
    "data-center specification packages. Evaluate whether the package correctly "
    "represents the adopted fire/building/electrical code basis, AHJ and monitoring "
    "requirements, certification regime, controlling client sequences, and site "
    "interfaces in the Project Requirements Profile."
)


_COMPLIANCE_SEVERITY_DEFINITIONS = """\
CRITICAL — the package omits or contradicts a controlling detection, notification, releasing, emergency-control, power, survivability, or acceptance requirement in a way likely to defeat life safety or block occupancy.
HIGH — a material adopted-code, AHJ, monitoring, certification, interface, or client requirement is missing or misrepresented and must be corrected before issue.
MEDIUM — a controlling requirement is represented incompletely or imprecisely, but the deficiency is bounded and has a clear correction path.
GRIPES — editorial or traceability gaps in how fire-alarm project requirements are cited or coordinated."""


_POLITY_SUSPECT_TOKENS = (
    PolityTokenRule(
        country="CA",
        pattern=r"\bNFPA\s*70\b|\bNEC\b|(?i:\bNational Electrical Code\b)",
        note=(
            "NFPA 70 / NEC is the US model electrical code. Confirm the Canadian "
            "project's adopted provincial/territorial electrical code instead."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bIBC\b|\bIFC\b|\bIEBC\b|\bADA\b|\bOSHA\b",
        note=(
            "This is US code/authority vocabulary and may be a master-spec remnant "
            "unless the Canadian project deliberately invokes it as a benchmark."
        ),
    ),
    PolityTokenRule(
        country="CA",
        pattern=r"\bU\.?L\.?[- ]+(?i:Listed|Listing)\b",
        note=(
            "A bare US UL listing may not establish Canadian approval. Verify the "
            "accepted Canadian certification mark/category or field-evaluation path."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bCAN/ULC[- ]?S(?:524|536|537|561|1001)\b",
        note=(
            "This is a Canadian fire-alarm standard family. Verify the US project's "
            "adopted NFPA 72/IBC/IFC basis unless the owner invokes it separately."
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
            "This is Canadian model-code vocabulary and is likely a wrong-polity "
            "remnant unless the US project deliberately invokes it as a benchmark."
        ),
    ),
    PolityTokenRule(
        country="US",
        pattern=r"\bCSA\s*C22\.1\b|(?i:\bCanadian Electrical Code\b)",
        note=(
            "CSA C22.1 / Canadian Electrical Code is a Canadian model code. Verify "
            "the US jurisdiction's adopted NFPA 70/NEC edition instead."
        ),
    ),
)


_CORPUS_SIGNAL_PATTERNS = (
    r"\bbasis of design\b",
    r"\bBoD\b",
    r"\bowner'?s? project requirements\b",
    r"\bOPR\b",
    r"\bfire alarm (?:design|system) (?:criteria|guide|standard)s?\b",
    r"\bfire protection basis of design\b",
    r"\bcause[- ]and[- ]effect matrix\b",
    r"\bsequence of operations\b",
    r"\binput/output matrix\b",
    r"\bpoint(?:s)? list\b",
    r"\bbattery calculation\b",
    r"\bvoltage[- ]drop calculation\b",
    r"\bnotification appliance circuit\b",
    r"\bemergency control function interface\b",
    r"\bintegrated systems test(?:ing)?\b",
    r"\bmonitoring agreement\b",
)


DATACENTER_ELECTRONIC_SAFETY_SECURITY = ReviewModule(
    module_id="datacenter_electronic_safety_security",
    display_name=(
        "Hyperscale Data Center — Electronic Safety & Security: "
        "Fire Detection & Alarm (US/Canada)"
    ),
    description=(
        "Division 28 electronic safety and security, phase 1: fire detection and "
        "alarm only for hyperscale data-center projects in the United States and "
        "Canada. Access control, video surveillance, intrusion detection, and "
        "general communications remain outside this version's scope."
    ),
    cycle=DATACENTER_ELECTRONIC_SAFETY_SECURITY_IBC_2024,
    reviewer_persona=(
        "You are a fire detection and alarm specification reviewer specializing "
        "in hyperscale data-center facilities in the United States and Canada. "
        "Review protected-premises and supervising-station systems, initiating "
        "devices, aspirating detection, notification/emergency communications, "
        "pathways, power, programming, monitoring, testing, and multidisciplinary "
        "emergency-control and releasing interfaces. This phase does not review "
        "access control, video surveillance, intrusion detection, or general "
        "communications except where they interface with fire alarm."
    ),
    review_user_intro=(
        "Review the following fire detection and alarm specification for a "
        "hyperscale data-center project. The Project Requirements Profile's adopted "
        "codes/amendments, AHJ and monitoring rules, certification regime, and "
        "controlling client criteria take precedence over the US model-code fallback. "
        "Treat cited but unprovided owner/design documents conditionally and do not "
        "invent their contents. Do not expand this phase into a substantive review "
        "of access control, CCTV/video, intrusion detection, or communications."
    ),
    review_severity_definitions=_REVIEW_SEVERITY_DEFINITIONS,
    review_confidence_high_example=(
        "an explicit cause-and-effect conflict that would trigger an unintended "
        "suppression release or omit a required emergency-control function"
    ),
    review_categories_template=_REVIEW_CATEGORIES,
    review_examples=_REVIEW_EXAMPLES,
    cross_check_persona=(
        "You are a cross-specification coordination reviewer for fire detection "
        "and alarm packages on hyperscale data-center projects."
    ),
    cross_check_severity_definitions=_CROSS_CHECK_SEVERITY_DEFINITIONS,
    verifier_persona=(
        "You are a construction-specification verification assistant for fire "
        "detection and alarm systems on hyperscale data-center projects in the "
        "United States and Canada."
    ),
    verifier_source_priorities=_VERIFIER_SOURCE_PRIORITIES,
    review_user_code_basis_line=(
        "US model-code fallback: IBC {ibc}, IFC {ifc}, IEBC {iebc}, ASCE {asce7}; "
        "fire-alarm references: {pinned_standards}. Project-profile adoptions govern."
    ),
    cross_check_code_basis_line=(
        "US model-code fallback: IBC {ibc}, IFC {ifc}, IEBC {iebc}, ASCE {asce7}; "
        "fire-alarm references: {pinned_standards}. Project-profile adoptions govern."
    ),
    verifier_system_code_basis_lines=(
        "US model-code fallback: IBC {ibc}, IFC {ifc}, IEBC {iebc}, ASCE {asce7}.\n"
        "Use the Project Requirements Profile's adopted fire, building, electrical, "
        "and Canadian provincial/territorial basis when present."
    ),
    verifier_user_code_basis_lines=(
        "US fallback references: {pinned_standards}.\nProject-profile adoption, "
        "amendments, AHJ rules, and applicability govern."
    ),
    detector_vocabulary=_DETECTOR_VOCABULARY,
    profile_keywords=_PROFILE_KEYWORDS,
    cross_check_chunk_groups=_CROSS_CHECK_CHUNK_GROUPS,
    report_context_phrase="hyperscale data-center fire detection and alarm projects",
    report_title="Spec Critic — Fire Detection & Alarm Specification Review Report",
    project_profile_enabled=True,
    research_persona=_RESEARCH_PERSONA,
    research_dimensions=_RESEARCH_DIMENSIONS,
    corpus_signal_patterns=_CORPUS_SIGNAL_PATTERNS,
    compliance_persona=_COMPLIANCE_PERSONA,
    compliance_severity_definitions=_COMPLIANCE_SEVERITY_DEFINITIONS,
    polity_suspect_tokens=_POLITY_SUSPECT_TOKENS,
)
