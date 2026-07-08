"""The California K-12 DSA mechanical/plumbing module.

The original — and for now only — domain configuration: mechanical and
plumbing specs for California K-12 education facilities under DSA
jurisdiction, reviewed against the California 2025 code cycle
(:data:`src.core.code_cycles.CALIFORNIA_2025`).

Phase 2 moved the prompt *content* here: the personas, severity anchors,
review categories, few-shot examples, and the verifier's source tiers below
are the exact strings that used to live hardcoded in ``review/prompts.py``,
``cross_check/cross_checker.py``, and ``verification/verifier.py``. The
goldens in ``tests/test_golden_domain_surfaces.py`` pin the assembled
prompts byte-exactly, so any edit to these strings is a deliberate,
review-visible change to what the model is sent. Prompt *protocol* (tool
contracts, rubric bands, grounding rules) stays in the engine builders.

Remaining hardcoded domain content (detector vocabulary, verification
profile keywords, cross-check chunk map) moves in later phases.
"""
from __future__ import annotations

from ..core.code_cycles import CALIFORNIA_2025
from .base import ReviewModule

# The review-scope category list. May reference the placeholders documented
# by :func:`src.modules.base.category_format_kwargs`; formatted against the
# module's cycle at prompt-build time.
_REVIEW_CATEGORIES = """\
1. Internal contradictions within the spec (e.g., conflicting requirements in different articles).
2. Code edition misalignment: the current cycle is CBC {cbc}, CMC {cmc}, CPC {cpc}, Energy {energy}, CALGreen {calgreen}, ASCE {asce7}. Pinned standard editions for this cycle: {pinned_standards}. Flag references to superseded editions (e.g., ASCE {asce7_prev} instead of {asce7}).
3. References to sections, standards, or test methods that do not exist or have been withdrawn.
4. Explicit cross-references to other CSI sections, equipment tags, or coordination dependencies that the spec author should verify.
5. Constructability and coordination conflicts (e.g., requirements that contradict typical means and methods, or that depend on equipment/access not provided by another section).
6. Test, adjust, and balance (TAB) and commissioning conflicts (e.g., commissioning sequences that disagree with controls or HVAC narratives).
7. Equipment schedule / spec contradictions when schedules are referenced or supplied (capacity, voltage, accessory, or basis-of-design mismatches).
8. Division 01 coordination conflicts (general requirements that the technical section duplicates or contradicts).
9. Warranty conflicts within and across sections (duration, coverage, start date).
10. Product basis-of-design / approved-equal language conflicts.
11. Controls sequence / spec conflicts (sequence of operations vs. devices and points listed).
12. DSA / HCAI / Title 24 closeout and testing requirements that are missing or under-specified.
13. Fire and smoke damper access coordination (access doors, ceiling access, labeling).
14. Seismic restraint references (OSHPD/OPM/OPA preapprovals, anchor design responsibility).
15. Sprinkler / hydraulic calculation language conflicts (occupancy hazard, density, demand area, listed components).
16. Pipe / duct material conflicts across related sections.
17. Submittal and O&M conflicts (what is required, when, in what form)."""


# Stable, cacheable few-shot examples. The examples must not vary with
# per-spec content — they are part of the cached system-prompt prefix (keyed
# by cycle). They must NOT mention ``evidenceElementId`` or ``<para id="…">``
# — those are per-request concepts (enforced at registration); the cached
# system prefix is pinned by
# ``test_system_prompt_constant_and_does_not_embed_specs``. Every JSON
# example below is validated against the parser's edit-shape contract at
# registration.
_REVIEW_EXAMPLES = """\
Example 1 — valid EDIT (stale code-cycle reference):
{
  "severity": "MEDIUM",
  "fileName": "23 05 00 Common HVAC.docx",
  "section": "1.03",
  "issue": "Spec cites a superseded California Building Code edition for the current project cycle.",
  "actionType": "EDIT",
  "existingText": "Comply with 2019 CBC Chapter 6.",
  "replacementText": "Comply with the current CBC edition for this project cycle.",
  "codeReference": "CBC (current cycle)",
  "confidence": 0.9
}

Example 2 — valid ADD (insert missing requirement using a verbatim anchor):
{
  "severity": "HIGH",
  "fileName": "23 09 23 Controls.docx",
  "section": "1.01",
  "issue": "PART 1 omits the general code-compliance statement expected by DSA review.",
  "actionType": "ADD",
  "existingText": null,
  "replacementText": "A. All work shall comply with the current California Building, Mechanical, Plumbing, Energy, and CALGreen Codes for this project cycle.",
  "anchorText": "PART 1 - GENERAL",
  "insertPosition": "after",
  "codeReference": null,
  "confidence": 0.8
}

Example 3 — REPORT_ONLY (cross-section coordination, no clean text edit):
{
  "severity": "HIGH",
  "fileName": "23 09 23 Controls.docx",
  "section": "3.04",
  "issue": "Sequence of operations references damper types not listed in the 23 33 00 damper schedule. Resolve in a controls / HVAC coordination meeting and update the affected sections together.",
  "actionType": "REPORT_ONLY",
  "existingText": null,
  "replacementText": null,
  "anchorText": null,
  "insertPosition": null,
  "codeReference": null,
  "confidence": 0.7
}

Example 4 — DO NOT REPORT (generic boilerplate is not a finding):
The phrase "Coordinate with related work specified in other Sections" is
standard Division 23 boilerplate. It is not a contradiction, not a code-cycle
issue, and not an invalid reference. Do not emit a finding for boilerplate
coordination language unless there is concrete evidence that the
coordination requirement actually conflicts with another section.\
"""


_REVIEW_SEVERITY_DEFINITIONS = """\
CRITICAL — showstoppers for DSA approval, safety, or code compliance (e.g., a referenced fire-sprinkler standard that has been withdrawn, or a missing fire/smoke damper rating a plan-checker will reject).
HIGH — major technical issues requiring correction before the spec can be issued (e.g., a controls sequence that references a damper type absent from the equipment schedule).
MEDIUM — meaningful issues with moderate impact (e.g., a superseded code-edition citation that should be updated to the current cycle).
GRIPES — quality/editorial issues that should still be fixed (e.g., inconsistent capitalization of a defined term)."""


_CROSS_CHECK_SEVERITY_DEFINITIONS = """\
CRITICAL — showstoppers: direct contradictions between specs that would cause construction conflicts or DSA rejection (e.g., two sections assigning the same seismic anchorage to different responsible parties).
HIGH — major coordination gaps requiring correction before issuing (e.g., a controls point referenced in one spec that the controls section never lists).
MEDIUM — meaningful cross-reference or consistency issues with moderate impact (e.g., the same equipment given different model numbers in two sections).
GRIPES — minor coordination polish items (e.g., inconsistent section-number formatting in cross-references)."""


# Authoritative-source tiers for the verifier prompt. The surrounding
# guidance ("Prefer authoritative sources in this priority order:", the
# tier-1-3 fallback rule, regulatory-beats-manufacturer) is engine protocol;
# the tiers and domains below are this module's source-quality policy.
_VERIFIER_SOURCE_PRIORITIES = """\
1. California regulatory authorities:
   dgs.ca.gov, dsa.ca.gov, hcai.ca.gov, bsc.ca.gov, energy.ca.gov,
   osfm.fire.ca.gov, calbo.org

2. Code publishers with full text:
   up.codes, codes.iccsafe.org, iccsafe.org

3. Standards organizations:
   nfpa.org, ashrae.org, iapmo.org, smacna.org, aspe.org, astm.org, asce.org

4. Testing and listing agencies:
   ul.com, fmglobal.com

5. Major manufacturer technical data:
   greenheck.com, trane.com, carrier.com, watts.com, zurn.com, victaulic.com

6. Industry associations:
   phccweb.org, mcaa.org, csinet.org, seaoc.org

7. Healthcare-specific (for HCAI projects):
   fgiguidelines.org, jointcommission.org

8. Archived or historical standards:
   archive.org"""


CALIFORNIA_K12_MEP = ReviewModule(
    module_id="california_k12_mep",
    display_name="California K-12 (DSA) — Mechanical & Plumbing",
    description=(
        "Mechanical and plumbing specs for California K-12 education "
        "facilities under DSA jurisdiction (California 2025 code cycle)."
    ),
    cycle=CALIFORNIA_2025,
    reviewer_persona=(
        "You are a specification reviewer for mechanical and plumbing "
        "disciplines. The project context is California K-12 education "
        "facilities under DSA jurisdiction."
    ),
    review_user_intro=(
        "Review the following specification document for a California K-12 "
        "project under DSA jurisdiction."
    ),
    review_severity_definitions=_REVIEW_SEVERITY_DEFINITIONS,
    review_confidence_high_example='an explicit stale "2019 CBC" citation',
    review_categories_template=_REVIEW_CATEGORIES,
    review_examples=_REVIEW_EXAMPLES,
    cross_check_persona=(
        "You are a cross-spec coordination reviewer for California K-12 DSA "
        "mechanical/plumbing specs."
    ),
    cross_check_severity_definitions=_CROSS_CHECK_SEVERITY_DEFINITIONS,
    verifier_persona=(
        "You are a construction specification verification assistant for "
        "California K-12 DSA projects."
    ),
    verifier_source_priorities=_VERIFIER_SOURCE_PRIORITIES,
)
