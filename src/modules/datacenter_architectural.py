"""The hyperscale data-center architectural module (US / Canada).

The third reviewable domain: architectural specifications (Divisions 03–14 —
shell, envelope, openings, interiors, specialties) for hyperscale data-center
projects, reviewed against the International Building Code and International
Energy Conservation Code as base model codes.

Jurisdiction decision (same as ``datacenter_fire`` — see
``docs/datacenter_fire_module_plan.md`` §3.1): hyperscale data centers are
built across many states and provinces, each adopting the I-codes on its own
schedule with its own amendments. Rather than pin one jurisdiction, this
module pins the **model codes** (IBC / IECC, current editions) as the code
basis; state / provincial / local / AHJ facts are per-project data resolved
by the requirements-research fan-out (``project_profile_enabled=True``) and
Project Context. A state-pinned variant would be a *separate* module with its
own registry-unique cycle label, never a multi-jurisdictional cycle.

**Code-basis flexibility decision — this module pins ZERO standards
editions** (``standards=()``), the first module to do so. The fire module's
experience showed pinned referenced-standard editions for a multi-state
domain end up 100% ``UNVERIFIED`` (the primary ICC tables are paywalled)
while the research phase retrieves the *actual* adopting instrument's
referenced-standards table per project — which the review intro already
treats as controlling (three-way precedence). So adopted editions here come
exclusively from the per-run Project Requirements Profile; the engine
degrades every pinned-editions surface gracefully (reviewer falls back to
"current editions" phrasing, the verifier omits its pinned-editions block,
the report omits the pinned-editions paragraph, and the verification cache
key uses the ``_no_std`` sentinel under this module's unique label). The
pinned base-code years (IBC/IECC 2024 — a matter of public record) remain
only as the deterministic stale-detector target and the fallback floor when
research partially fails.

Jurisdiction-generic content (I-code year vocabulary, wrong-polity token
rules, owner-document corpus patterns) is shared with the other data-center
modules via ``_datacenter_shared``; this file adds the architectural
discipline content on top.

The goldens in ``tests/test_golden_datacenter_arch_surfaces.py`` pin the
assembled prompts byte-exactly, mirroring the fire-module goldens; the
California and fire-module goldens stay byte-identical (this module touches
no engine file).
"""
from __future__ import annotations

from ..core.code_cycles import BaseCode, CodeCycle
from ._datacenter_shared import (
    DC_ASCE7_PLAUSIBLE_EDITIONS,
    DC_ICODE_PLAUSIBLE_YEARS,
    DC_ICODE_VALID_YEARS,
    DC_STALE_LONGFORM_BUILDING_FIRE,
    POLITY_CA_115V,
    POLITY_CA_IBC_IFC,
    POLITY_CA_LIFE_SAFETY_CODE,
    POLITY_CA_MADE_IN_USA,
    POLITY_CA_NEC,
    POLITY_CA_OSHA,
    POLITY_CA_SEISMIC_NOTATION,
    POLITY_CA_UL_LISTED,
    POLITY_US_CSA_C221,
    POLITY_US_NBC,
    POLITY_US_OREG,
    POLITY_US_ULC,
    SHARED_DC_CORPUS_SIGNAL_PATTERNS,
)
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
# amended version), pinned as the deterministic stale-detector target and the
# research-failure fallback floor — NOT as edition truth for the project.
# ``label`` is registry-unique (validated); it namespaces the verification
# cache and backs the ``module_for_cycle`` bridge, so it must never collide
# with California's ``"2025"`` or fire's ``"dc-ibc-2024"``.
#
# ``standards=()`` is deliberate (see the module docstring): adopted standard
# editions are per-project facts the research phase grounds against the
# jurisdiction's own referenced-standards table. Nothing here to go stale,
# nothing to ship UNVERIFIED.
# ---------------------------------------------------------------------------

DATACENTER_ARCH_IBC_2024 = CodeCycle(
    label="dc-arch-ibc-2024",
    base_codes=(
        # Primary code first — the stale-cycle detector compares found years
        # against this entry's year. IBC/IECC 2024 are the current published
        # I-code editions (a matter of public record — ICC published them).
        BaseCode("ibc", "IBC", "2024", source="ICC 2024 International Building Code (current published edition)"),
        BaseCode("iecc", "IECC", "2024", source="ICC 2024 International Energy Conservation Code (current published edition)"),
    ),
    # The 2024 IBC references ASCE 7-22 (same public-record basis as the fire
    # module; the detector does edition arithmetic on these two fields — for
    # architectural scope ASCE 7 governs components & cladding wind and
    # seismic restraint of non-structural components).
    asce7="7-22",
    asce7_previous="7-16",
    standards=(),
)


# The review-scope category list. References the placeholders documented by
# :func:`src.modules.base.code_basis_format_kwargs` ({ibc}/{iecc}/{asce7}/
# {asce7_prev}); formatted against the arch cycle at prompt-build time.
# Deliberately does NOT use {pinned_standards}: this module pins no editions,
# and edition checks are anchored to the Project Requirements Profile instead
# (category 2) — the profile is the controlling source, the model codes only
# the fallback.
_REVIEW_CATEGORIES = """\
1. Internal contradictions within the spec (e.g., conflicting requirements in different articles).
2. Code edition misalignment: the base model codes are IBC {ibc}, IECC {iecc}, ASCE {asce7}. Where the project context includes a Project Requirements Profile, its governing-code and adopted-standard-edition entries control edition checks — the model codes above are only the fallback when the profile is silent. Flag references to superseded editions (e.g., ASCE {asce7_prev} instead of {asce7}); where the project context names the governing state/provincial adoption, defer to it for edition checks.
3. References to withdrawn, superseded, or nonexistent standards, sections, or test methods.
4. Fire-resistance-rated assemblies: tested-design listings (UL / ULC design numbers) consistent with the assembly construction described; rating continuity at heads, joints, and intersections; ratings consistent with the wall and partition types the section names.
5. Firestopping and joint protection: tested-system requirements versus the penetrating services and joint types described; engineering-judgment provisions for field conditions; compatibility with the barrier ratings.
6. Opening protectives: door, frame, and hardware fire ratings consistent with the walls they serve; NFPA 80 installation and maintenance references; egress hardware and electrified-hardware coordination.
7. Glazing: safety glazing at hazardous locations; fire-rated glazing markings and size limits; fenestration performance class and grade (NAFS) consistent with the design pressures the section states.
8. Building envelope continuity: air-, water-, and vapor-control layers defined and continuous across transitions and openings; exterior-wall fire-propagation requirements (NFPA 285) where combustible components are specified; envelope testing and mock-up requirements internally consistent.
9. Roofing: wind-uplift design basis and tested-assembly requirements consistent (code basis vs. insurer/FM ratings cited without conflict); hail or impact classification; edge-metal and attachment requirements consistent with the uplift basis.
10. Accessibility: requirements consistent with the accessibility standard the project context identifies for this jurisdiction; flag hardcoded accessibility citations that conflict with it (do not assume a US standard on a Canadian project, or the reverse).
11. Interior finishes: flame-spread and smoke-developed classifications consistent with the locations and assemblies described.
12. Acoustic requirements: STC/NRC criteria consistent between sections and with the assemblies described, where specified.
13. Physical-security coordination: door hardware, access control, and detection interfaces referencing Division 28 that the author should verify; secure-area construction requirements consistent across sections.
14. Coordination with structure and MEP: embeds, penetrations, shaft walls, and ceiling systems versus the services above; support and backing for wall-mounted items.
15. Building-envelope commissioning and testing handoff: who tests, who witnesses, acceptance criteria, and phased fit-out boundaries.
16. Warranty, submittal, and O&M conflicts (what is required, when, in what form).
17. Location- and client-specific requirements: where the project context includes a Project Requirements Profile, verify the specification aligns with the governing codes, local amendments, AHJ requirements, and client standards it lists; flag conflicts with, and omissions of, profile requirements.
18. Master-specification remnants: content from other disciplines or other jurisdictions left in this section — mechanical/electrical/fire-suppression language in an architectural section; another polity's codes, agencies, listing marks, or procurement clauses; another project's identifiers or placeholder tokens (TBD, XXXX); flag for deletion or adaptation.
19. Document integrity: duplicated or out-of-sequence article numbering, empty lettered paragraphs, doubled words, garbled or dangling cross-references, related-section numbers that do not match their titles, and products/execution mismatches within the section."""


# Stable, cacheable few-shot examples. Like the other modules', these must
# not vary with per-spec content (they are part of the cached system-prompt
# prefix keyed by cycle) and must NOT mention ``evidenceElementId`` or
# ``<para id="…">`` — those are per-request concepts enforced at registration.
# Every JSON example is validated against the parser's edit-shape contract at
# registration. The location-aware phrasing ("the current IBC edition adopted
# for this project location") teaches the model the posture: the research
# profile and Project Context supply the governing adoption.
_REVIEW_EXAMPLES = """\
Example 1 — valid EDIT (stale code-cycle reference):
{
  "severity": "MEDIUM",
  "fileName": "08 11 13 Hollow Metal Doors and Frames.docx",
  "section": "1.03",
  "issue": "Spec cites a stale IBC edition rather than the current adopted edition for the project location.",
  "actionType": "EDIT",
  "existingText": "Comply with 2015 IBC Chapter 7.",
  "replacementText": "Comply with the current IBC edition adopted for this project location.",
  "codeReference": "IBC (current adopted edition)",
  "confidence": 0.9
}

Example 2 — valid ADD (insert missing requirement using a verbatim anchor):
{
  "severity": "HIGH",
  "fileName": "07 84 13 Penetration Firestopping.docx",
  "section": "1.01",
  "issue": "PART 1 omits a general statement requiring tested and listed firestop systems appropriate to each penetration condition.",
  "actionType": "ADD",
  "existingText": null,
  "replacementText": "A. Provide penetration firestopping using systems tested and listed for each penetrating item and barrier rating, per the building code adopted for this project location, including all local amendments.",
  "anchorText": "PART 1 - GENERAL",
  "insertPosition": "after",
  "codeReference": null,
  "confidence": 0.8
}

Example 3 — REPORT_ONLY (cross-section coordination, no clean text edit):
{
  "severity": "HIGH",
  "fileName": "08 11 13 Hollow Metal Doors and Frames.docx",
  "section": "2.02",
  "issue": "The door schedule this section references assigns 90-minute opening protectives at openings the wall-types section describes as 1-hour partitions. Resolve the wall-rating and opening-protective pairing in an architectural coordination review and update both sections together.",
  "actionType": "REPORT_ONLY",
  "existingText": null,
  "replacementText": null,
  "anchorText": null,
  "insertPosition": null,
  "codeReference": null,
  "confidence": 0.75
}

Example 4 — DO NOT REPORT (boilerplate and in-scope LEED are not findings):
Generic Division 01/08 coordination boilerplate such as "Coordinate with related
work specified in other Sections" is not a contradiction, not a code-edition
issue, and not an invalid reference — do not emit a finding for it absent
concrete evidence of a real conflict. Likewise, do NOT flag LEED references as
inappropriate: LEED is genuine scope for data-center projects, not a
copy/paste error.\
"""


_REVIEW_SEVERITY_DEFINITIONS = """\
CRITICAL — life-safety or permit-blocking: an egress or fire-resistance-rated-assembly gap, a building-official or plan-review rejection trigger, a withdrawn or nonexistent standard controlling a rated assembly or means-of-egress component, a direct conflict with the governing code / a local amendment / an insurer requirement that would halt approval, or a commercial/procurement conflict that would materially disrupt tender (e.g., an origin- or tariff-exposed sourcing clause).
HIGH — major technical issues requiring correction before the spec can be issued (e.g., opening-protective ratings that contradict the wall types they serve, or an air-barrier scope that cannot achieve continuity as described).
MEDIUM — meaningful issues with moderate impact (e.g., a superseded standard-edition citation that should be updated to the project's adopted edition).
GRIPES — quality/editorial issues that should still be fixed (e.g., inconsistent capitalization of a defined term)."""


_CROSS_CHECK_SEVERITY_DEFINITIONS = """\
CRITICAL — showstoppers: direct contradictions between specs that would cause construction conflicts or plan-review rejection (e.g., two sections assigning conflicting fire-resistance ratings to the same partition type).
HIGH — major coordination gaps requiring correction before issuing (e.g., a door schedule whose opening-protective ratings do not match the wall-types section, or firestopping scoped to systems the penetrating sections do not use).
MEDIUM — meaningful cross-reference or consistency issues with moderate impact (e.g., the same product given different model numbers in two sections).
GRIPES — minor coordination polish items (e.g., inconsistent cross-reference formatting)."""


# Authoritative-source tiers for the verifier prompt. The surrounding guidance
# (the "Prefer authoritative sources" header and the fallback rules) is engine
# protocol; the tiers below are this module's source-quality policy. Canadian
# authorities are included from day one — the module reviews US and Canadian
# data-center projects.
_VERIFIER_SOURCE_PRIORITIES = """\
1. Code publishers and standards organizations:
   codes.iccsafe.org, up.codes, iccsafe.org, nfpa.org

2. Listing, certification, and insurance authorities:
   ul.com, fmglobal.com, fmapprovals.com (RoofNav wind/hail assembly ratings)

3. Government code authorities:
   state building-code agency and building-department sites (.gov), municipal
   code portals; for Canadian sites nrc.canada.ca, provincial statute /
   e-Laws portals, scc-ccn.ca, csagroup.org

4. Major manufacturer technical data:
   assaabloy.com, allegion.com, kawneer.com, obe.com, vitroglazings.com,
   carlislesyntec.com, gaf.com, sika.com, tremcosealants.com, hilti.com,
   stifirestop.com

5. Industry associations:
   dhi.org, glass.org, nrca.net, airbarrier.org, fcia.org

6. Archived or historical standards:
   archive.org"""


# The deterministic preprocessor's I-code vocabulary. The detector logic (regex
# assembly, span dedup, negation suppression) is engine-owned in
# ``input/preprocessor.py``; the I-code year families, ASCE 7 whitelist, and
# Building/Fire long-form pattern are shared across the data-center modules
# via ``_datacenter_shared`` (including the documented D-10 limitation that
# NBC/NFC years stay out of the shared set — Canadian deterministic coverage
# arrives via the wrong-polity token rules). This module adds the
# architectural-relevant long-form citations (Energy Conservation / Existing
# Building) and its own abbreviation set.
_DETECTOR_VOCABULARY = DetectorVocabulary(
    # Abbreviations recognized next to a year ("2018 IBC" / "IECC 2018").
    code_abbreviations=("IBC", "IEBC", "IECC", "IFC"),
    plausible_cycle_years=DC_ICODE_PLAUSIBLE_YEARS,
    valid_cycle_years=DC_ICODE_VALID_YEARS,
    asce7_plausible_editions=DC_ASCE7_PLAUSIBLE_EDITIONS,
    stale_cycle_extra_patterns=(
        DC_STALE_LONGFORM_BUILDING_FIRE,
        # Long-form citations of the architectural-scope I-codes; the year
        # must be capture group 1 (engine contract), so the code-name
        # alternation is non-capturing.
        r"\b(20\d{2})\s+International\s+(?:Energy\s+Conservation|Existing\s+Building)\s+Code\b",
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
# ``verification_profiles.py``; these are the data-center architectural terms.
# Keywords match as case-folded substrings, so short tokens are padded or
# avoided: no bare "ada" (it is inside "Canada") and no bare "cec" (California
# Energy Code vs. the Canadian Electrical Code — a documented collision).
_PROFILE_KEYWORDS = ProfileKeywords(
    jurisdictional=(
        "building official",
        "authority having jurisdiction",
        "ahj",
        "plan review",
        "building permit",
        "special inspection",
        "fire marshal",
        "accessibility",
        "americans with disabilities",
        "adaag",
        "barrier-free",
        "barrier free",
        "fm global",
        "factory mutual",
        "fm approved",
        "insurer",
        "local amendment",
        "provincial",
    ),
    manufacturer=(
        "assa abloy",
        "allegion",
        "dormakaba",
        "kawneer",
        "oldcastle",
        "vitro",
        "carlisle",
        "gaf",
        "sika",
        "tremco",
        "hilti",
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
        "iecc",
        "iebc",
        "astm",
        "ansi",
        "nfpa",
        "ul ",
        "ul-",
        "ulc",
        "csa",
        "aama",
        "nfrc",
        "bhma",
        "sdi ",
        "tms",
        "building code",
        "energy code",
        "code section",
        "standard",
    ),
    # The generic internal-coordination set MINUS "leed" — LEED references
    # are substantive scope here, not internal noise (fire-module parity).
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


# CSI MasterFormat division families for chunked cross-check. Architectural
# data-center packages span Divisions 03–14; the groups below pair the
# divisions that coordinate most (each CSI prefix lands in exactly one group —
# engine invariant), because chunked runs are within-chunk-only coordination
# (documented engine limitation). **Division 08 is deliberately co-chunked
# with Division 09**: the module's highest-priority coordination scenario —
# opening-protective ratings vs. the wall/partition types they serve — spans
# 08 (door/hardware schedules) and 09 (gypsum assemblies / wall types), and
# splitting them would make that check invisible exactly when the package is
# large enough to chunk. Unmatched prefixes (e.g. Division 01/21/23/26) pool
# into the engine's reserved ``general`` chunk.
_CROSS_CHECK_CHUNK_GROUPS = (
    ChunkGroup("shell_structure", "Divisions 03–06 — Shell & Structure", ("03", "04", "05", "06")),
    ChunkGroup("envelope", "Division 07 — Thermal & Moisture Protection", ("07",)),
    ChunkGroup("openings_interiors", "Divisions 08–09 — Openings & Interiors", ("08", "09")),
    ChunkGroup("specialties", "Divisions 10–12 — Specialties & Furnishings", ("10", "11", "12")),
    ChunkGroup("special_conveying", "Divisions 13–14 — Special Construction & Conveying", ("13", "14")),
)


# ===========================================================================
# Location-aware content (research fan-out + compliance pass + wrong-polity
# detection). These slots are validated non-empty by
# ``modules.base._validate_research_slots`` because ``project_profile_enabled``
# is True below; a module that left the flag off must leave them all empty.
# ===========================================================================

# First line of the research system prompt (who the researcher is). The
# engine wraps it with the byte-stable research protocol block.
_RESEARCH_PERSONA = (
    "You are an architectural code-research assistant for hyperscale "
    "data-center projects. You research jurisdiction-specific building, "
    "energy, and accessibility code adoptions, local amendments, "
    "authority-having-jurisdiction requirements, and owner/client design "
    "standards. You report only requirements you can support with sources "
    "you actually retrieved, and you clearly separate verified facts from "
    "industry practice."
)


# The four research dimensions. Each ``prompt_template`` formats against the
# profile placeholders ({city}/{state_or_province}/{country}/{client_name})
# plus the module's own code-basis placeholders ({asce7} in site_environment);
# registration format-checks them with dummy profile values. Per-dimension
# search/fetch budgets are module data (fire-module parity: the governing-
# codes and AHJ dimensions need 3–5× the engine default). Because this module
# pins zero standards editions, the governing_codes dimension's referenced-
# standards-table retrieval (item (e)) is the run's ONLY source of adopted
# edition facts — it is the load-bearing dimension.
_RESEARCH_DIMENSIONS = (
    ResearchDimension(
        dimension_id="governing_codes",
        title="Governing building, energy, and accessibility codes",
        max_searches=24,
        max_fetches=8,
        prompt_template=(
            "Determine the governing building, energy, and accessibility "
            "codes for a new hyperscale data-center project in {city}, "
            "{state_or_province}, {country}. Identify: (a) the state or "
            "provincial building code edition currently in force and its "
            "model-code basis (IBC year, or NBC year for Canadian sites) "
            "with effective dates; (b) the energy code applicable to the "
            "building envelope (IECC year or adopted ASHRAE 90.1 path for US "
            "sites; NECB or provincial energy code for Canadian sites) and "
            "its compliance-path options; (c) the accessibility regime the "
            "specifications must reflect — for US sites the adopted ICC "
            "A117.1 edition and the federal ADA Standards; for Canadian "
            "sites the provincial accessibility legislation and the building "
            "code's barrier-free requirements, including CSA B651 where "
            "invoked; (d) any municipal or county amendments adopted by "
            "{city} affecting architectural scope: fire-resistance-rated "
            "construction, exterior walls, roofing and wind uplift, glazing, "
            "or accessibility; (e) the editions of the architectural "
            "standards referenced by that adoption — NFPA 80, NFPA 105, "
            "NFPA 252, NFPA 257, NFPA 285, UL 10C, UL 263, UL 1479, ASTM "
            "E84, ASTM E119, ASTM E814, ASTM E1966, ICC A117.1, and the NAFS "
            "fenestration standard (AAMA/WDMA/CSA 101/I.S.2/A440) — retrieve "
            "the adopting instrument's referenced-standards table itself (or "
            "its official summary) and report the edition year for each "
            "standard the specifications cite; do not infer editions from "
            "the model-code year, and do not skip a standard because you "
            "believe you know its edition; (f) the current published edition "
            "of each of those standards, so the review can distinguish the "
            "legal minimum from current-edition enhancements; (g) the "
            "product certification/listing regime — which certification "
            "marks are legally recognized for fire door assemblies, "
            "fire-rated glazing, and firestop systems in this jurisdiction "
            "(e.g., ULC/cULus vs US-only UL in Canada) and any "
            "field-evaluation path for unlisted assemblies; (h) "
            "special-inspection or field-review requirements applicable to "
            "architectural systems — structural and envelope special "
            "inspections under the building code's own framework for US "
            "sites, or provincial professional field-review regimes for "
            "Canadian sites; (i) any licensing or professional-seal "
            "requirements for envelope, roofing, or firestop design and "
            "shop drawings that the specifications must reflect. Prefer "
            "official adoption sources and retrieve and cite the adopting "
            "instrument itself: the state building-code agency or building "
            "official, the provincial regulator or National Research Council "
            "of Canada, and the municipal code of {city}."
        ),
    ),
    ResearchDimension(
        dimension_id="ahj_requirements",
        title="Authority-having-jurisdiction requirements",
        max_searches=20,
        max_fetches=6,
        prompt_template=(
            "Identify every authority having jurisdiction over architectural "
            "scope for a data-center project in {city}, {state_or_province}, "
            "{country} — assume multiplicity (building department, fire "
            "marshal or fire department for rated construction and opening "
            "protectives, and any accessibility or planning review bodies) — "
            "and any published requirements construction specifications "
            "should reflect: plan submittal and deferred-submittal "
            "requirements for exterior envelope, roofing, fire-resistance-"
            "rated assemblies, and door hardware; required third-party "
            "testing and inspections (air-barrier or whole-building "
            "airtightness testing, roofing wind-uplift or fastening "
            "verification, firestop inspection where the adopted code "
            "requires it, glazing or curtain-wall field testing); "
            "accessibility plan-review or sign-off procedures; roofing "
            "permits and any wind- or hail-zone documentation the AHJ "
            "requires; required witnessed tests or mock-up reviews; and the "
            "closeout documentation the AHJ requires (rated-assembly "
            "documentation, door and hardware certification, envelope test "
            "reports). Mark process/schedule facts (fees, windows, notice "
            "periods) as process advisories rather than spec requirements."
        ),
    ),
    ResearchDimension(
        dimension_id="client_standards",
        title="Owner / client and insurer standards",
        max_searches=12,
        max_fetches=4,
        prompt_template=(
            "First determine who reviews risk for {client_name} projects — "
            "FM Global, a named risk consultancy, or self-insurance — since "
            "this decides whether FM construction and roofing data sheets "
            "and RoofNav-rated assemblies are mandatory or benchmark-only. "
            "Then identify published design and construction standards of "
            "{client_name} that apply to data-center architectural scope: "
            "the client's public compliance, trust-center, or "
            "service-assurance documentation describing building "
            "construction and physical security; public planning/permit "
            "filings for {client_name} data-center campuses (including in "
            "{city} itself) with architectural specifics — envelope, "
            "roofing, screening, secure-area construction; which FM data "
            "sheets are commonly invoked for roofing and exterior walls "
            "when FM applies; known {client_name} requirements or "
            "preferences for roofing systems, exterior wall assemblies, "
            "physical-security door hardware, and interior finishes; "
            "sustainability programs (e.g., LEED) {client_name} pursues "
            "that affect architectural specifications; and a brief "
            "benchmark of peer hyperscaler practice for calibration. Report "
            "only what you can ground in retrievable sources; where owner "
            "standards are confidential and not retrievable, say so "
            "explicitly rather than guessing."
        ),
    ),
    ResearchDimension(
        dimension_id="site_environment",
        title="Site and environmental factors",
        max_searches=8,
        max_fetches=4,
        prompt_template=(
            "Identify site and environmental factors for {city}, "
            "{state_or_province}, {country} that architectural "
            "specifications must account for: the wind design context in "
            "the governing code's own framework — for US sites the ASCE "
            "{asce7} basic wind speed, exposure, and any special wind "
            "region; for Canadian sites the NBC climatic-data wind "
            "pressures — including the official lookup source; the hail "
            "exposure and, where the insurer applies, the FM hail zone "
            "affecting roofing and rooftop equipment protection; ground "
            "snow load and rain intensity from the code's climatic data as "
            "they affect roofing and drainage specifications; the seismic "
            "design context for architectural components and cladding "
            "(ASCE {asce7} seismic design category for US sites; NBC "
            "seismic-hazard values and Seismic Category for Canadian "
            "sites), noting whether non-structural component restraint is "
            "triggered or exempt; freeze-thaw exposure and January design "
            "temperatures affecting masonry, sealants, and roofing "
            "application windows; flood or wildfire exposure zones "
            "affecting envelope requirements; and any corrosive or coastal "
            "exposure affecting metal cladding, doors, and hardware "
            "finishes."
        ),
    ),
)


# Compliance-pass persona + severity anchors. The engine supplies the
# <task>/<severity_definitions>/<output> protocol wrapper around these.
_COMPLIANCE_PERSONA = (
    "You are a code-compliance reviewer for hyperscale data-center "
    "architectural specifications. You evaluate whether a specification "
    "package correctly represents the project's governing codes, local "
    "amendments, AHJ requirements, and client standards."
)


_COMPLIANCE_SEVERITY_DEFINITIONS = """\
CRITICAL — the package omits or contradicts a governing-code or AHJ requirement in a way that would block permit issuance or leave a life-safety gap in rated construction or means of egress.
HIGH — a location- or client-specific requirement is materially misrepresented and must be corrected before issue (e.g., the wrong adopted standard edition; a required AHJ inspection or envelope test missing).
MEDIUM — a requirement is present but incomplete or imprecise (e.g., the correct code cited without a required local amendment).
GRIPES — editorial gaps in how requirements are referenced."""


# Wrong-polity token rules: the jurisdiction-generic set is shared via
# ``_datacenter_shared`` (per-rule pattern rationale lives there); the rules
# below are the architectural-discipline additions — accessibility, fire-test,
# and energy-code regimes that differ between the US and Canada. The engine
# compiles polity patterns with NO flags, so acronym rules stay
# case-sensitive (``\bADA\b`` cannot match inside "Canada") and phrase
# families use scoped ``(?i:…)``.
_POLITY_CA_ADA = PolityTokenRule(
    country="CA",
    pattern=r"\bADA\b|\bADAAG\b|(?i:\bamericans with disabilities\b)",
    note=(
        "The ADA is US civil-rights law; Canadian accessibility is governed "
        "by provincial legislation and the building code's barrier-free "
        "requirements (CSA B651 where invoked)."
    ),
)

_POLITY_CA_A117 = PolityTokenRule(
    country="CA",
    pattern=r"(?i:\b(?:ICC|ANSI)[/ ]?A117(?:\.1)?\b)",
    note=(
        "ICC/ANSI A117.1 is the US accessibility standard; Canadian "
        "barrier-free design follows the NBC / provincial building code and "
        "CSA B651 where invoked."
    ),
)

_POLITY_CA_IECC = PolityTokenRule(
    country="CA",
    pattern=r"\bIECC\b",
    note=(
        "The IECC is a US model energy code; Canadian envelope energy "
        "performance is governed by the NECB or the provincial energy code."
    ),
)

_POLITY_CA_ASTM_E119 = PolityTokenRule(
    country="CA",
    pattern=r"(?i:\bASTM\s*E\s*119\b)",
    note=(
        "Canadian fire-resistance ratings are established per CAN/ULC-S101; "
        "an ASTM E119 rating basis on an NBC-governed project should be "
        "confirmed against the governing code."
    ),
)

_POLITY_CA_UL_10C = PolityTokenRule(
    country="CA",
    pattern=r"(?i:\bUL\s*10C\b)",
    note=(
        "The Canadian fire-door test basis is CAN/ULC-S104; a bare UL 10C "
        "citation on a Canadian project should be confirmed against the "
        "governing code and listing regime."
    ),
)

_POLITY_US_CAN_ULC = PolityTokenRule(
    country="US",
    pattern=r"(?i:\bCAN/ULC[- ]?S\d+\b)",
    note=(
        "CAN/ULC S-series standards are Canadian; a US project's rated "
        "assemblies and fire tests are typically established per the "
        "UL/ASTM equivalents."
    ),
)

_POLITY_US_NECB = PolityTokenRule(
    country="US",
    pattern=r"\bNECB\b",
    note=(
        "The NECB (National Energy Code of Canada for Buildings) is "
        "Canadian; a US project's energy code is the IECC or an adopted "
        "ASHRAE 90.1 path."
    ),
)

# Deliberately the spelled-out form only: bare "OBC" is ambiguous on US runs
# (it is also the common abbreviation for the OHIO Building Code, a legitimate
# governing-code citation there). A bare-OBC Ontario remnant still gets caught
# by the shared "O. Reg." rule and the other Canadian-vocabulary tokens.
_POLITY_US_ONTARIO_BUILDING_CODE = PolityTokenRule(
    country="US",
    pattern=r"(?i:\bOntario Building Code\b)",
    note=(
        "The Ontario Building Code is Canadian provincial law; a US project "
        "is not governed by it."
    ),
)

_POLITY_SUSPECT_TOKENS = (
    # --- country=CA: flag US-only vocabulary on a Canadian project ----------
    POLITY_CA_NEC,
    POLITY_CA_OSHA,
    POLITY_CA_LIFE_SAFETY_CODE,
    POLITY_CA_UL_LISTED,
    POLITY_CA_MADE_IN_USA,
    POLITY_CA_SEISMIC_NOTATION,
    POLITY_CA_IBC_IFC,
    POLITY_CA_115V,
    _POLITY_CA_ADA,
    _POLITY_CA_A117,
    _POLITY_CA_IECC,
    _POLITY_CA_ASTM_E119,
    _POLITY_CA_UL_10C,
    # --- country=US: flag Canada-only vocabulary on a US project ------------
    POLITY_US_NBC,
    POLITY_US_ULC,
    POLITY_US_OREG,
    POLITY_US_CSA_C221,
    _POLITY_US_CAN_ULC,
    _POLITY_US_NECB,
    _POLITY_US_ONTARIO_BUILDING_CODE,
)


# Module-owned corpus-signal patterns: the generic owner-document vocabulary
# is shared via ``_datacenter_shared``; the architectural document names are
# appended module-locally. Compiled case-insensitive at scrape time.
_CORPUS_SIGNAL_PATTERNS = SHARED_DC_CORPUS_SIGNAL_PATTERNS + (
    r"\b(?:architectural|envelope|building enclosure) design (?:guide|standard|criteria)\b",
)


DATACENTER_ARCHITECTURAL = ReviewModule(
    module_id="datacenter_architectural",
    display_name="Hyperscale Data Center — Architectural (US/Canada)",
    description=(
        "Architectural specs (Divisions 03–14) for hyperscale data-center "
        "projects in the US and Canada, reviewed against the International "
        "Building Code and International Energy Conservation Code as base "
        "model codes. The project city, state or province, country, and "
        "client are required inputs — the app researches the governing "
        "codes, adopted standard editions, and client standards for that "
        "location before review. Put any additional known project facts "
        "(AHJ correspondence, owner basis-of-design) into Project Context."
    ),
    cycle=DATACENTER_ARCH_IBC_2024,
    reviewer_persona=(
        "You are an architectural specification reviewer specializing in "
        "building envelope, fire-resistance-rated construction, openings, "
        "and interior architecture. The project context is hyperscale "
        "data-center facilities in the United States and Canada, designed "
        "under the International Building Code and International Energy "
        "Conservation Code as base model codes, with the project's governing "
        "state/provincial adoptions, local amendments, "
        "authority-having-jurisdiction requirements, and owner standards "
        "supplied in the project context."
    ),
    review_user_intro=(
        "Review the following architectural specification for a hyperscale "
        "data-center project. Where the project context includes a Project "
        "Requirements Profile, treat its governing-code, local-amendment, "
        "AHJ, and client-standard entries as the project's controlling "
        "requirements — they take precedence over the model-code defaults "
        "for edition and requirement checks. Where the specification "
        "declares its own edition-governance rule, check that rule for "
        "consistency with the profile's adopted editions. Where the "
        "specification cites its own basis-of-design or owner documents "
        "that are not provided for review, phrase findings about them "
        "conditionally ('per the BoD section the spec cites — confirm "
        "against that document') rather than asserting their content."
    ),
    review_severity_definitions=_REVIEW_SEVERITY_DEFINITIONS,
    review_confidence_high_example=(
        'an explicit stale "2015 IBC" citation in a door or envelope section'
    ),
    review_categories_template=_REVIEW_CATEGORIES,
    review_examples=_REVIEW_EXAMPLES,
    cross_check_persona=(
        "You are a cross-spec coordination reviewer for hyperscale "
        "data-center architectural packages."
    ),
    cross_check_severity_definitions=_CROSS_CHECK_SEVERITY_DEFINITIONS,
    verifier_persona=(
        "You are a construction specification verification assistant for "
        "architectural systems in hyperscale data-center projects under the "
        "IBC/IECC family of model codes."
    ),
    verifier_source_priorities=_VERIFIER_SOURCE_PRIORITIES,
    review_user_code_basis_line=(
        "Current code basis: IBC {ibc}, IECC {iecc}, ASCE {asce7}."
    ),
    cross_check_code_basis_line=(
        "Current code basis: IBC {ibc}, IECC {iecc}, ASCE {asce7}."
    ),
    verifier_system_code_basis_lines=(
        "Current code basis: IBC {ibc}, IECC {iecc}, ASCE {asce7}."
    ),
    verifier_user_code_basis_lines=(
        "Current code basis: IBC {ibc}, IECC {iecc}\n"
        "Current seismic standard: ASCE {asce7}"
    ),
    detector_vocabulary=_DETECTOR_VOCABULARY,
    profile_keywords=_PROFILE_KEYWORDS,
    cross_check_chunk_groups=_CROSS_CHECK_CHUNK_GROUPS,
    report_context_phrase="hyperscale data-center architectural projects",
    report_title="Spec Critic — Architectural Specification Review Report",
    # --- Location-aware capability ON (fire-module parity) ---------------
    project_profile_enabled=True,
    research_persona=_RESEARCH_PERSONA,
    research_dimensions=_RESEARCH_DIMENSIONS,
    corpus_signal_patterns=_CORPUS_SIGNAL_PATTERNS,
    compliance_persona=_COMPLIANCE_PERSONA,
    compliance_severity_definitions=_COMPLIANCE_SEVERITY_DEFINITIONS,
    polity_suspect_tokens=_POLITY_SUSPECT_TOKENS,
)
