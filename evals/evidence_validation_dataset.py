"""EX-04 evidence set: verdicts, their quoted evidence, and whether it supports them.

The decision record is ``plans/experiments/EX-04-evidence-validation-source-reuse.md``;
the runner and scorer are in :mod:`evals.evidence_validation`.

Each :class:`EvidenceCase` is one verified finding as the verifier would leave
it — the finding's claim, the verdict, the verifier's ``source_quote``, the
accepted sources, and the API's native citations — with two labels:

* ``support`` — the adjudicated answer to **"does the recorded evidence
  support the verdict as written?"** ``supports``, ``does_not_support``, or
  ``cannot_tell`` (the evidence is too thin to say either way);
  ``not_applicable`` for a result with no conclusive verdict. For a DISPUTED
  verdict, "supports" means the evidence shows the claim wrong.
* ``expected_checks`` — for the checks a case was written to exercise, the
  status each should report (:mod:`src.verification.evidence_validation`).
  Only the listed checks are scored; the others are free.

**The passages are constructed.** Every quote and cited passage here was
written for this set in the register of a code or standard. None is a
quotation of NFPA 13, NFPA 72, the IBC, or any other document, and the URLs
use a ``/constructed/`` path so no one mistakes a case for a citation. The
set judges how a validator reads the relationship between a claim and a
passage; it does not state what any standard requires. (``evidence_basis``
is ``constructed`` on every case, and validation enforces it.)

**What this set can and cannot show.** The validator and this set were
written by the same author in the same session. The ``tuning`` split was used
while writing the rules; the ``held_out`` split was written after the rules
were frozen and scored once, and its result is recorded in the decision
record without any rule changed afterwards. Even so, both splits are
constructed cases scored against rules written with such cases in mind: a
pass rate here says the rules do what they were written to do, not that they
agree with a person on real verifier output. That comparison is the live
observation protocol in :mod:`evals.evidence_validation`.

Nothing here calls a model.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

DATASET_VERSION = 1

SPLIT_TUNING = "tuning"
SPLIT_HELD_OUT = "held_out"
SPLITS = (SPLIT_TUNING, SPLIT_HELD_OUT)

SUPPORT_SUPPORTS = "supports"
SUPPORT_DOES_NOT = "does_not_support"
SUPPORT_CANNOT_TELL = "cannot_tell"
SUPPORT_NOT_APPLICABLE = "not_applicable"
SUPPORT_LABELS = (SUPPORT_SUPPORTS, SUPPORT_DOES_NOT, SUPPORT_CANNOT_TELL, SUPPORT_NOT_APPLICABLE)

#: The kinds of case the plan names (EX-04 A), plus the controls.
CATEGORIES = (
    "correct_support",
    "paraphrase",
    "unit_equivalent",
    "omitted_exception",
    "exception_accounted",
    "reversed_negation",
    "comparative_bound",
    "changed_number",
    "changed_unit",
    "wrong_edition",
    "edition_consistent",
    "wrong_authority",
    "misattributed_quote",
    "attributed_quote",
    "missing_quote",
    "vague_quote",
    "disputed",
    "corrected",
    "not_applicable",
)

EVIDENCE_CONSTRUCTED = "constructed"

_CHECK_NAMES = (
    "source_identity",
    "edition",
    "authority",
    "quoted_support",
    "numbers_units",
    "negation",
    "exception",
)
_CHECK_STATUSES = ("consistent", "concern", "unknown", "not_applicable")


@dataclass(frozen=True)
class EvidenceCase:
    case_id: str
    split: str
    category: str
    description: str
    finding: dict
    verification: dict
    support: str
    rationale: str
    expected_checks: dict = field(default_factory=dict)
    evidence_basis: str = EVIDENCE_CONSTRUCTED


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

_NFPA = "https://www.nfpa.org/constructed/"
_ICC = "https://codes.iccsafe.org/constructed/"
_GOV = "https://www.dgs.ca.gov/constructed/"
_FORUM = "https://www.reddit.com/r/constructed/"
_WIKI = "https://en.wikipedia.org/wiki/Constructed_"
_VENDOR = "https://www.example-sprinkler-vendor.com/constructed/"


def _finding(issue: str, *, code_reference: str = "", replacement: str = "", action: str = "EDIT") -> dict:
    return {
        "finding_id": "",
        "fileName": "21 13 13 - Wet-Pipe Sprinkler Systems.docx",
        "issue": issue,
        "codeReference": code_reference,
        "replacementText": replacement,
        "actionType": action,
    }


def _cite(url: str, text: str, *, title: str = "", tool: str = "web_search") -> dict:
    return {
        "type": "web_search_result_location" if tool == "web_search" else "char_location",
        "recognized": True,
        "tool": tool,
        "url": url,
        "title": title,
        "cited_text": text,
        "resolution": "direct" if tool == "web_search" else "document_text",
        "retrieved": True,
        "verdict_cites_source": False,
    }


def _verification(
    verdict: str,
    quote: str = "",
    *,
    sources: tuple[str, ...] = (),
    citations: list[dict] | None = None,
    correction: str | None = None,
    failed: bool = False,
    mode: str = "standard_reasoning",
) -> dict:
    return {
        "verdict": verdict,
        "source_quote": quote,
        "accepted_sources": list(sources),
        "sources": list(sources),
        "native_citations": citations,
        "correction": correction,
        "verification_failed": failed,
        "verification_mode": mode,
        "cache_status": "miss",
    }


def _case(case_id: str, split: str, category: str, description: str, **kwargs: Any) -> EvidenceCase:
    return EvidenceCase(case_id=case_id, split=split, category=category, description=description, **kwargs)


T, H = SPLIT_TUNING, SPLIT_HELD_OUT

# ---------------------------------------------------------------------------
# Tuning split — used while the rules were written
# ---------------------------------------------------------------------------

_TUNING: tuple[EvidenceCase, ...] = (
    _case(
        "ev-t01", T, "correct_support",
        "The quote states the claimed clearance, from the cited edition, on a standards publisher's page.",
        finding=_finding(
            "Section 3.02 allows 12 in. below deflectors; NFPA 13-2022 requires 18 in. clearance to storage.",
            code_reference="NFPA 13-2022",
            replacement="Maintain a clearance of not less than 18 in. between sprinkler deflectors and the top of storage.",
        ),
        verification=_verification(
            "CONFIRMED",
            "NFPA 13 (2022): the clearance between the deflector and the top of storage shall be 18 in. or greater.",
            sources=(_NFPA + "t01",),
            citations=[_cite(_NFPA + "t01", "the clearance between the deflector and the top of storage shall be 18 in. or greater")],
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent", "edition": "consistent", "source_identity": "consistent", "authority": "consistent"},
        rationale="Same quantity, same edition, the quoted passage is the API's cited text from the accepted source.",
    ),
    _case(
        "ev-t02", T, "paraphrase",
        "The source says the same limit in different words.",
        finding=_finding(
            "Hangers for 1 in. steel branch lines must be no more than 12 ft apart per NFPA 13.",
            code_reference="NFPA 13",
            replacement="Space hangers on 1 in. steel branch lines at 12 ft maximum.",
        ),
        verification=_verification(
            "CONFIRMED",
            "The distance between hangers on steel pipe of this size shall not exceed 12 ft.",
            sources=(_NFPA + "t02",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent", "negation": "consistent"},
        rationale="'Shall not exceed 12 ft' is a maximum of 12 ft, which is the claim; a comparative bound is not a negation. Low word overlap must not count against it.",
    ),
    _case(
        "ev-t03", T, "unit_equivalent",
        "The claim says 1 ft; the source says 12 in.",
        finding=_finding(
            "The spec allows 6 in. of lateral offset; the standard requires 1 ft minimum from the obstruction.",
            code_reference="NFPA 13",
        ),
        verification=_verification(
            "CONFIRMED",
            "Sprinklers shall be located at least 12 in. from the obstruction.",
            sources=(_NFPA + "t03",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent"},
        rationale="12 in. is 1 ft; a unit change is not a changed number.",
    ),
    _case(
        "ev-t04", T, "unit_equivalent",
        "The claim is metric; the source is in inches.",
        finding=_finding(
            "Drain valves shall be at least 25 mm; the spec shows 15 mm.",
            code_reference="NFPA 13",
            replacement="Provide auxiliary drains of not less than 25 mm.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Auxiliary drains shall be not less than 1 in. in size.",
            sources=(_NFPA + "t04",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent"},
        rationale="25 mm and 1 in. differ by conversion rounding only.",
    ),
    _case(
        "ev-t05", T, "omitted_exception",
        "The source qualifies the requirement with an exception the finding leaves out.",
        finding=_finding(
            "Sprinklers are omitted from the electrical room; NFPA 13 requires sprinklers in every electrical room.",
            code_reference="NFPA 13",
            replacement="Provide sprinkler protection in all electrical rooms.",
            action="ADD",
        ),
        verification=_verification(
            "CONFIRMED",
            "Sprinklers shall be provided throughout the building, except that sprinklers shall not be required in electrical rooms where the room is dedicated to dry-type equipment and meets the listed conditions.",
            sources=(_NFPA + "t05",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"exception": "concern"},
        rationale="The passage establishes an exception that may cover this room; it does not support 'every electrical room'.",
    ),
    _case(
        "ev-t06", T, "exception_accounted",
        "The finding already states the exception the source gives.",
        finding=_finding(
            "Sprinklers are required in the pump room; the omission exception for dry-type electrical rooms does not apply to it.",
            code_reference="NFPA 13",
            replacement="Provide sprinklers in the pump room.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Sprinklers shall be installed throughout, except that sprinklers shall not be required in electrical rooms dedicated to dry-type equipment.",
            sources=(_NFPA + "t06",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"exception": "consistent"},
        rationale="The claim names the exception and why it does not apply.",
    ),
    _case(
        "ev-t07", T, "reversed_negation",
        "The source forbids what the finding says it requires.",
        finding=_finding(
            "Cast iron fittings are required on the standpipe risers.",
            code_reference="NFPA 14",
            replacement="Cast iron fittings shall be used on standpipe risers.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Cast iron fittings shall not be used on standpipe risers where the pressure exceeds the listed rating.",
            sources=(_NFPA + "t07",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"negation": "concern"},
        rationale="'Shall not be used' is the opposite of 'shall be used'.",
    ),
    _case(
        "ev-t08", T, "comparative_bound",
        "'Shall not be less than' is a minimum, not a prohibition.",
        finding=_finding(
            "Fire pump rated pressure must be at least 175 psi; the spec lists 150 psi.",
            code_reference="NFPA 20",
            replacement="The fire pump rated pressure shall be 175 psi minimum.",
        ),
        verification=_verification(
            "CONFIRMED",
            "The rated pressure shall not be less than 175 psi for this arrangement.",
            sources=(_NFPA + "t08",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"negation": "consistent", "numbers_units": "consistent"},
        rationale="A comparative bound reads as a requirement with the same polarity as the claim.",
    ),
    _case(
        "ev-t09", T, "changed_number",
        "The source states a different clearance.",
        finding=_finding(
            "The spec shows 12 in.; NFPA 13 requires 18 in. below the deflector.",
            code_reference="NFPA 13",
            replacement="Maintain 18 in. below sprinkler deflectors.",
        ),
        verification=_verification(
            "CONFIRMED",
            "A clearance of not less than 36 in. shall be maintained below the deflector.",
            sources=(_NFPA + "t09",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"numbers_units": "concern"},
        rationale="Neither 12 in. nor 18 in. is the 36 in. the passage states.",
    ),
    _case(
        "ev-t10", T, "changed_unit",
        "The same digit in a different unit.",
        finding=_finding(
            "The hose connection must be within 4 in. of the floor landing edge per NFPA 14.",
            code_reference="NFPA 14",
            replacement="Locate hose connections within 4 in. of the landing edge.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Hose connections shall be located within 4 ft of the landing edge.",
            sources=(_NFPA + "t10",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"numbers_units": "concern"},
        rationale="4 in. is not 4 ft.",
    ),
    _case(
        "ev-t11", T, "wrong_edition",
        "The source is an older edition than the one the finding cites.",
        finding=_finding(
            "NFPA 72-2022 requires listed notification appliances in the data hall.",
            code_reference="NFPA 72-2022",
            replacement="Notification appliances shall be listed.",
        ),
        verification=_verification(
            "CONFIRMED",
            "NFPA 72 (2016 edition): notification appliances shall be listed for the purpose.",
            sources=(_NFPA + "t11",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"edition": "concern"},
        rationale="A 2016 passage does not establish what the 2022 edition requires.",
    ),
    _case(
        "ev-t12", T, "edition_consistent",
        "A stale-edition finding: the source names the current edition the finding names.",
        finding=_finding(
            "The spec cites NFPA 72-2019; the current edition is 2022.",
            code_reference="NFPA 72",
            replacement="Comply with NFPA 72, 2022 edition.",
        ),
        verification=_verification(
            "CONFIRMED",
            "NFPA 72, 2022 Edition, supersedes the 2019 edition.",
            sources=(_NFPA + "t12",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"edition": "consistent"},
        rationale="The claim names both editions; the passage names them in the same relation.",
    ),
    _case(
        "ev-t13", T, "wrong_authority",
        "Every accepted source is a forum or a wiki.",
        finding=_finding(
            "IBC 2021 requires a 2-hour rating for the fire pump room enclosure.",
            code_reference="IBC 2021",
            replacement="Enclose the fire pump room with 2-hour construction.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Most jurisdictions require a 2-hour enclosure for fire pump rooms in high-rise buildings.",
            sources=(_FORUM + "t13", _WIKI + "t13"),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"authority": "concern"},
        rationale="A forum post and a wiki article cannot establish what the IBC requires.",
    ),
    _case(
        "ev-t14", T, "misattributed_quote",
        "The API attributes the quoted passage to a page the verdict does not cite.",
        finding=_finding(
            "NFPA 13 requires 18 in. below deflectors.",
            code_reference="NFPA 13",
        ),
        verification=_verification(
            "CONFIRMED",
            "A clearance of 18 in. shall be maintained below the deflector.",
            sources=(_NFPA + "t14",),
            citations=[_cite(_VENDOR + "t14", "A clearance of 18 in. shall be maintained below the deflector.")],
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"source_identity": "concern"},
        rationale="The words came from a vendor page; the verdict presents a standards page as their source.",
    ),
    _case(
        "ev-t15", T, "attributed_quote",
        "The quote is the cited passage of the accepted source.",
        finding=_finding(
            "NFPA 25 requires quarterly inspection of waterflow alarm devices.",
            code_reference="NFPA 25",
            replacement="Inspect waterflow alarm devices quarterly.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Waterflow alarm devices shall be inspected quarterly.",
            sources=(_NFPA + "t15",),
            citations=[_cite(_NFPA + "t15", "Waterflow alarm devices shall be inspected quarterly.")],
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"source_identity": "consistent", "negation": "consistent"},
        rationale="Attributed, same requirement, same polarity.",
    ),
    _case(
        "ev-t16", T, "missing_quote",
        "A CONFIRMED with no quote (a result cached before quotes were required).",
        finding=_finding("NFPA 13 requires listed hangers.", code_reference="NFPA 13"),
        verification=_verification("CONFIRMED", "", sources=(_NFPA + "t16",)),
        support=SUPPORT_DOES_NOT,
        expected_checks={"source_identity": "concern"},
        rationale="No recorded passage supports the verdict.",
    ),
    _case(
        "ev-t17", T, "vague_quote",
        "A general passage from the right publisher that says nothing about the claim's number.",
        finding=_finding(
            "NFPA 13 requires 0.20 gpm/ft2 over 2,500 sq ft for this hazard.",
            code_reference="NFPA 13",
        ),
        verification=_verification(
            "CONFIRMED",
            "Sprinkler systems shall be designed in accordance with the requirements of this standard.",
            sources=(_NFPA + "t17",),
        ),
        support=SUPPORT_CANNOT_TELL,
        expected_checks={"authority": "consistent", "numbers_units": "unknown"},
        rationale="The passage neither states nor contradicts the density; where it came from is not what it says.",
    ),
    _case(
        "ev-t18", T, "disputed",
        "DISPUTED: the source states a different quantity than the claim.",
        finding=_finding(
            "NFPA 13 requires 4 ft of clearance below deflectors.",
            code_reference="NFPA 13",
        ),
        verification=_verification(
            "DISPUTED",
            "The clearance below the deflector shall be not less than 18 in.",
            sources=(_NFPA + "t18",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent"},
        rationale="18 in. is not 4 ft, which is what the dispute says.",
    ),
    _case(
        "ev-t19", T, "disputed",
        "DISPUTED: the source states exactly what the claim states.",
        finding=_finding(
            "NFPA 13 requires 18 in. of clearance below deflectors.",
            code_reference="NFPA 13",
        ),
        verification=_verification(
            "DISPUTED",
            "The clearance below the deflector shall be not less than 18 in.",
            sources=(_NFPA + "t19",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"numbers_units": "unknown"},
        rationale="The passage agrees with the claim it is said to contradict. The validator is not expected to flag this: a dispute can rest on applicability or authority, which a text comparison cannot see.",
    ),
    _case(
        "ev-t20", T, "disputed",
        "DISPUTED: the source negates the claimed requirement.",
        finding=_finding(
            "NFPA 13 requires sprinklers in the elevator machine room.",
            code_reference="NFPA 13",
            replacement="Sprinklers shall be installed in the elevator machine room.",
        ),
        verification=_verification(
            "DISPUTED",
            "Sprinklers shall not be required in the elevator machine room where the listed conditions are met.",
            sources=(_NFPA + "t20",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"negation": "consistent", "exception": "consistent"},
        rationale="The passage contradicts the claim, which is what the dispute says.",
    ),
    _case(
        "ev-t21", T, "corrected",
        "CORRECTED: the source states the corrected quantity.",
        finding=_finding(
            "NFPA 13 requires 24 in. of clearance to storage.",
            code_reference="NFPA 13",
        ),
        verification=_verification(
            "CORRECTED",
            "The clearance to the top of storage shall be 36 in. or greater for this commodity.",
            sources=(_NFPA + "t21",),
            correction="The required clearance is 36 in., not 24 in.",
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent"},
        rationale="The correction's 36 in. is the passage's 36 in.",
    ),
    _case(
        "ev-t22", T, "corrected",
        "CORRECTED: the correction states a quantity the source does not.",
        finding=_finding(
            "NFPA 13 requires 24 in. of clearance to storage.",
            code_reference="NFPA 13",
        ),
        verification=_verification(
            "CORRECTED",
            "The clearance to the top of storage shall be 36 in. or greater for this commodity.",
            sources=(_NFPA + "t22",),
            correction="The required clearance is 30 in.",
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"numbers_units": "concern"},
        rationale="The passage says 36 in.; the correction says 30 in.",
    ),
    _case(
        "ev-t23", T, "not_applicable",
        "UNVERIFIED: nothing to validate.",
        finding=_finding("NFPA 13 requires listed hangers.", code_reference="NFPA 13"),
        verification=_verification("UNVERIFIED", "", sources=()),
        support=SUPPORT_NOT_APPLICABLE,
        rationale="No conclusive verdict.",
    ),
    _case(
        "ev-t24", T, "not_applicable",
        "An operational failure: nothing was checked.",
        finding=_finding("NFPA 13 requires listed hangers.", code_reference="NFPA 13"),
        verification=_verification("UNVERIFIED", "", sources=(), failed=True),
        support=SUPPORT_NOT_APPLICABLE,
        rationale="No verdict.",
    ),
    _case(
        "ev-t25", T, "paraphrase",
        "Both sides prohibit the same thing in different words.",
        finding=_finding(
            "Sprinkler piping shall not be routed through the electrical room per NFPA 13.",
            code_reference="NFPA 13",
            replacement="Sprinkler piping shall not be routed through electrical rooms.",
            action="EDIT",
        ),
        verification=_verification(
            "CONFIRMED",
            "Routing sprinkler piping through electrical rooms is prohibited.",
            sources=(_GOV + "t25",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"negation": "consistent", "authority": "consistent"},
        rationale="'Shall not be routed' and 'is prohibited' are the same prohibition on the same subject.",
    ),
)

# ---------------------------------------------------------------------------
# Held-out split — written after the rules were frozen; scored once
# ---------------------------------------------------------------------------

def _unresolved_cite(text: str) -> dict:
    """A document citation whose source could not be established."""
    return {
        "type": "char_location",
        "recognized": True,
        "tool": "",
        "url": "",
        "title": "",
        "cited_text": text,
        "resolution": "unresolved",
        "retrieved": False,
        "verdict_cites_source": False,
    }


_HELD_OUT: tuple[EvidenceCase, ...] = (
    _case(
        "ev-h01", H, "unit_equivalent",
        "Pressure in kPa against psi.",
        finding=_finding(
            "Standpipe outlets need 100 psi residual; the spec shows 65 psi.",
            code_reference="NFPA 14",
            replacement="Provide 100 psi residual pressure at the outlet.",
        ),
        verification=_verification(
            "CONFIRMED",
            "The residual pressure at the outlet shall be not less than 690 kPa.",
            sources=(_NFPA + "h01",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent"},
        rationale="690 kPa is 100 psi.",
    ),
    _case(
        "ev-h02", H, "paraphrase",
        "Passive restatement of the same requirement.",
        finding=_finding(
            "Each riser must have its own control valve per NFPA 13.",
            code_reference="NFPA 13",
            replacement="Provide a control valve on each riser.",
        ),
        verification=_verification(
            "CONFIRMED",
            "A control valve is required on every system riser.",
            sources=(_NFPA + "h02",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"negation": "consistent"},
        rationale="Same requirement, same polarity, different words.",
    ),
    _case(
        "ev-h03", H, "omitted_exception",
        "An exception phrased 'other than'.",
        finding=_finding(
            "Every concealed space must be sprinklered per NFPA 13.",
            code_reference="NFPA 13",
            replacement="Sprinkler all concealed spaces.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Concealed spaces other than those of noncombustible construction with limited access shall be sprinklered.",
            sources=(_NFPA + "h03",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"exception": "concern"},
        rationale="The passage excludes a class of concealed spaces the finding includes.",
    ),
    _case(
        "ev-h04", H, "reversed_negation",
        "The claim prohibits what the source permits.",
        finding=_finding(
            "Plastic pipe is prohibited in the return air plenum.",
            code_reference="IMC",
            replacement="Plastic pipe shall not be installed in return air plenums.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Listed plastic pipe shall be permitted in return air plenums where it meets the flame spread limits.",
            sources=(_ICC + "h04",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"negation": "concern"},
        rationale="'Shall be permitted' contradicts 'shall not be installed'.",
    ),
    _case(
        "ev-h05", H, "changed_number",
        "A fractional size against a whole size.",
        finding=_finding(
            "The fire department connection needs a 1-1/2 in. drain.",
            code_reference="NFPA 13",
            replacement="Provide a 1-1/2 in. drain at the fire department connection.",
        ),
        verification=_verification(
            "CONFIRMED",
            "The drain at the fire department connection shall be 2 in. in size.",
            sources=(_NFPA + "h05",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"numbers_units": "concern"},
        rationale="1-1/2 in. is not 2 in.",
    ),
    _case(
        "ev-h06", H, "unit_equivalent",
        "A temperature in Celsius against Fahrenheit.",
        finding=_finding(
            "Wet-pipe systems require the space be kept at or above 40 °F.",
            code_reference="NFPA 13",
            replacement="Maintain the space at 40 °F minimum.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Areas containing wet-pipe systems shall be maintained at or above 4.4 °C.",
            sources=(_NFPA + "h06",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent"},
        rationale="4.4 °C is 40 °F.",
    ),
    _case(
        "ev-h07", H, "wrong_edition",
        "ASCE 7 two-digit editions.",
        finding=_finding(
            "Seismic bracing must follow ASCE 7-22 for this building.",
            code_reference="ASCE 7-22",
            replacement="Design seismic bracing per ASCE 7-22.",
        ),
        verification=_verification(
            "CONFIRMED",
            "ASCE 7-16 requires nonstructural components to be braced for the design seismic force.",
            sources=(_ICC + "h07",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"edition": "concern"},
        rationale="An ASCE 7-16 passage does not establish an ASCE 7-22 requirement.",
    ),
    _case(
        "ev-h08", H, "wrong_edition",
        "The edition appears only in the cited source's title.",
        finding=_finding(
            "NFPA 20-2022 requires the pump room to be heated to 40 °F.",
            code_reference="NFPA 20-2022",
        ),
        verification=_verification(
            "CONFIRMED",
            "The pump room shall be heated to a temperature of not less than 40 °F.",
            sources=(_NFPA + "h08",),
            citations=[
                _cite(
                    _NFPA + "h08",
                    "The pump room shall be heated to a temperature of not less than 40 °F.",
                    title="NFPA 20: Standard for the Installation of Stationary Pumps, 2016 Edition",
                )
            ],
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"edition": "concern"},
        rationale="The cited page is the 2016 edition; the claim is about 2022.",
    ),
    _case(
        "ev-h09", H, "correct_support",
        "A manufacturer claim supported by the manufacturer's page.",
        finding=_finding(
            "The specified sprinkler has a K-factor of 5.6 and a 175 psi rating; the spec lists 250 psi.",
            code_reference="",
            replacement="Sprinklers rated 175 psi.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Rated working pressure: 175 psi.",
            sources=(_VENDOR + "h09",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent", "authority": "unknown"},
        rationale="The rating is the manufacturer's to state, and the page states the claimed value.",
    ),
    _case(
        "ev-h10", H, "misattributed_quote",
        "The quote matches a document citation whose source could not be resolved.",
        finding=_finding(
            "NFPA 72 requires smoke detectors within 12 in. of the ceiling on sloped ceilings.",
            code_reference="NFPA 72",
        ),
        verification=_verification(
            "CONFIRMED",
            "Detectors shall be located within 12 in. of the ceiling measured horizontally from the peak.",
            sources=(_NFPA + "h10",),
            citations=[_unresolved_cite("Detectors shall be located within 12 in. of the ceiling measured horizontally from the peak.")],
        ),
        support=SUPPORT_CANNOT_TELL,
        expected_checks={"source_identity": "unknown"},
        rationale="The API cited a document the app could not identify; the passage may well be from the accepted page. Unresolved is not the same as another source.",
    ),
    _case(
        "ev-h11", H, "disputed",
        "DISPUTED: the source is a different edition than the claim names.",
        finding=_finding(
            "NFPA 25-2023 requires annual flow testing of the fire pump.",
            code_reference="NFPA 25-2023",
        ),
        verification=_verification(
            "DISPUTED",
            "NFPA 25 (2020 edition) requires weekly or monthly churn testing and annual flow testing of fire pumps.",
            sources=(_NFPA + "h11",),
        ),
        support=SUPPORT_CANNOT_TELL,
        expected_checks={"edition": "consistent"},
        rationale="The passage is from another edition and agrees with the claim on its face; it neither shows the claim wrong nor right for 2023.",
    ),
    _case(
        "ev-h12", H, "corrected",
        "CORRECTED to the current edition, and the source names it.",
        finding=_finding(
            "The spec should cite NFPA 13-2019.",
            code_reference="NFPA 13-2019",
        ),
        verification=_verification(
            "CORRECTED",
            "NFPA 13, 2022 Edition, is the current edition of the standard.",
            sources=(_NFPA + "h12",),
            correction="The current edition is NFPA 13-2022.",
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"edition": "consistent"},
        rationale="The correction's edition is the passage's edition.",
    ),
    _case(
        "ev-h13", H, "vague_quote",
        "A general statement for a specific prohibition.",
        finding=_finding(
            "NFPA 13 prohibits sprinklers directly over electrical switchgear.",
            code_reference="NFPA 13",
            replacement="Do not locate sprinklers directly over switchgear.",
        ),
        verification=_verification(
            "CONFIRMED",
            "Sprinklers shall be positioned to provide protection of the area consistent with the overall design.",
            sources=(_NFPA + "h13",),
        ),
        support=SUPPORT_CANNOT_TELL,
        expected_checks={"numbers_units": "not_applicable"},
        rationale="The passage says nothing about switchgear.",
    ),
    _case(
        "ev-h14", H, "omitted_exception",
        "An exception phrased 'shall not apply'.",
        finding=_finding(
            "IBC 2021 requires a fire barrier around the data hall.",
            code_reference="IBC 2021",
            replacement="Enclose the data hall with a fire barrier.",
        ),
        verification=_verification(
            "CONFIRMED",
            "The fire barrier requirement shall not apply where the building is protected throughout by an automatic sprinkler system.",
            sources=(_ICC + "h14",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"exception": "concern"},
        rationale="The passage waives the requirement under a condition the finding does not address.",
    ),
    _case(
        "ev-h15", H, "changed_number",
        "A percentage that differs.",
        finding=_finding(
            "The fire pump must deliver 150% of rated flow at 65% of rated pressure.",
            code_reference="NFPA 20",
        ),
        verification=_verification(
            "CONFIRMED",
            "The pump shall supply not less than 140% of rated capacity at not less than 65% of rated pressure.",
            sources=(_NFPA + "h15",),
        ),
        support=SUPPORT_DOES_NOT,
        expected_checks={"numbers_units": "concern"},
        rationale="65% matches, but 150% is not 140%: the passage contradicts half of the claim.",
    ),
    _case(
        "ev-h16", H, "correct_support",
        "A flow requirement stated exactly.",
        finding=_finding(
            "The standpipe demand is 500 gpm for the first standpipe per NFPA 14.",
            code_reference="NFPA 14",
        ),
        verification=_verification(
            "CONFIRMED",
            "The minimum flow rate for the hydraulically most remote standpipe shall be 500 gpm.",
            sources=(_NFPA + "h16",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent"},
        rationale="Same flow.",
    ),
    _case(
        "ev-h17", H, "not_applicable",
        "A local classification.",
        finding=_finding("Placeholder [SELECT] remains in 2.01.", code_reference=""),
        verification=_verification("UNVERIFIED", "", sources=(), mode="local_skip"),
        support=SUPPORT_NOT_APPLICABLE,
        rationale="No web verification ran.",
    ),
    _case(
        "ev-h18", H, "unit_equivalent",
        "'At least 3 ft' against 'minimum 36 in.'",
        finding=_finding(
            "Provide at least 3 ft of working clearance at the riser per NFPA 13.",
            code_reference="NFPA 13",
            replacement="Maintain 3 ft minimum clearance at the riser.",
        ),
        verification=_verification(
            "CONFIRMED",
            "A clear space of not less than 36 in. shall be maintained in front of the riser.",
            sources=(_NFPA + "h18",),
        ),
        support=SUPPORT_SUPPORTS,
        expected_checks={"numbers_units": "consistent"},
        rationale="36 in. is 3 ft.",
    ),
)

CASES: tuple[EvidenceCase, ...] = _TUNING + _HELD_OUT


# ---------------------------------------------------------------------------
# Validation and digests
# ---------------------------------------------------------------------------


def validate_cases(cases: tuple[EvidenceCase, ...] = CASES) -> list[str]:
    """Structural problems in the set ([] when it is sound)."""
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        where = case.case_id
        if case.case_id in seen:
            problems.append(f"{where}: duplicate case id")
        seen.add(case.case_id)
        if case.split not in SPLITS:
            problems.append(f"{where}: unknown split {case.split!r}")
        if case.category not in CATEGORIES:
            problems.append(f"{where}: unknown category {case.category!r}")
        if case.support not in SUPPORT_LABELS:
            problems.append(f"{where}: unknown support label {case.support!r}")
        if case.evidence_basis != EVIDENCE_CONSTRUCTED:
            problems.append(f"{where}: every passage in this set is constructed")
        for name, status in case.expected_checks.items():
            if name not in _CHECK_NAMES:
                problems.append(f"{where}: unknown check {name!r}")
            if status not in _CHECK_STATUSES:
                problems.append(f"{where}: unknown status {status!r} for {name}")
        verdict = str(case.verification.get("verdict", "")).upper()
        conclusive = verdict in ("CONFIRMED", "CORRECTED", "DISPUTED") and not case.verification.get(
            "verification_failed"
        )
        if conclusive == (case.support == SUPPORT_NOT_APPLICABLE):
            problems.append(f"{where}: 'not_applicable' is for results without a conclusive verdict")
        urls = list(case.verification.get("accepted_sources") or []) + [
            c.get("url", "") for c in case.verification.get("native_citations") or []
        ]
        for url in urls:
            # An unresolved citation has no URL by definition.
            if url and "/constructed" not in url and "Constructed_" not in url:
                problems.append(f"{where}: {url} is not marked as a constructed source")
        if not case.rationale.strip():
            problems.append(f"{where}: no rationale")
    return problems


def case_digest(case: EvidenceCase) -> str:
    """SHA-256 of one case's canonical JSON."""
    return hashlib.sha256(
        json.dumps(asdict(case), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def dataset_digest(cases: tuple[EvidenceCase, ...] = CASES) -> str:
    """SHA-256 over the case digests in id order: one change anywhere moves it."""
    joined = "\n".join(f"{c.case_id}:{case_digest(c)}" for c in sorted(cases, key=lambda c: c.case_id))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def split_cases(split: str, cases: tuple[EvidenceCase, ...] = CASES) -> tuple[EvidenceCase, ...]:
    return tuple(case for case in cases if case.split == split)
