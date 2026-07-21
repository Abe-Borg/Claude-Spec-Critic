"""Deterministic per-discipline routing for the hyperscale program.

The classifier is intentionally conservative.  CSI metadata and a strongly
matching title can produce an executable route.  Content-only hints usually
produce an ambiguous candidate unless several independent, discipline-specific
signals agree.  This prevents a cross-reference to another discipline from
silently sending an otherwise unrelated specification to that module.

Division 21 routes to fire suppression, Division 26 and explicit electrical
utility/generation sections route to electrical, and Division 28 fire-alarm
families route to the fire detection/alarm phase of electronic safety and
security.  Division 27 and other Division 28 systems remain outside the
implemented scope; even a suggestive title cannot silently route them.
"""
from __future__ import annotations

from dataclasses import replace
import re
from typing import Iterable

from .catalog import (
    DATACENTER_ARCHITECTURE_MODULE_ID,
    DATACENTER_ELECTRICAL_MODULE_ID,
    DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
    DATACENTER_FIRE_MODULE_ID,
    HYPERSCALE_DATACENTER_PROGRAM,
)
from .models import (
    ProgramDefinition,
    RoutingEvidence,
    RoutingEvidenceSource,
    RoutingState,
    SpecRoutingDecision,
    SpecRoutingInput,
    UserRoutingOverride,
)


# High-signal architectural divisions.  Structural/civil-heavy Divisions 02,
# 03, and 05 are intentionally omitted until those discipline packs exist.
_ARCHITECTURE_DIVISIONS = frozenset(
    {"04", "06", "07", "08", "09", "10", "12", "13", "14"}
)

# Division 21 is fire suppression.  Division 28 is much broader than fire
# alarm, so only its current and legacy fire detection/alarm families route to
# the electronic-safety module.  Access control, video, intrusion detection,
# and other electronic-safety sections do not.
_FIRE_DIVISION = "21"
_FIRE_ALARM_DIVISION_28_FAMILIES = frozenset({"31", "46"})

# Division 26 is electrical work.  Division 33 families 71-73 cover utility
# electrical transmission/distribution, substations, and utility transformers;
# Division 48 covers electrical power generation.  Division 27 communications
# and non-fire Division 28 electronic safety/security stay unsupported until
# their own discipline modules exist.
_ELECTRICAL_DIVISION = "26"
_ELECTRICAL_DIVISION_33_FAMILIES = frozenset({"71", "72", "73"})
_ELECTRICAL_DIVISION_48 = "48"
_RESTRICTED_UNIMPLEMENTED_DIVISIONS = frozenset({"27", "28"})

# CSI metadata must be recognizably metadata, not merely the first 2–6 digits
# found anywhere in a filename.  The old unanchored optional-separator regex
# interpreted strings such as ``NFPA 13`` as Division 13 and project/date
# numbers as section numbers.  Keep three intentionally narrow forms:
#
# * ``SECTION 07 27 26`` anywhere in a metadata field (explicit label);
# * ``07 27 26`` / ``07-27-26`` at the beginning of a title or filename;
# * compact ``072726`` only when supplied in the dedicated section-number
#   field, where the caller has already identified the value as metadata.
_CSI_SEPARATOR = r"(?:\s*[-._]\s*|\s+)"
_EXPLICIT_CSI_RE = re.compile(
    rf"\bSECTION\s+(\d{{2}}){_CSI_SEPARATOR}(\d{{2}})"
    rf"{_CSI_SEPARATOR}(\d{{2}})(?!\d)",
    re.I,
)
_LEADING_SEPARATED_CSI_RE = re.compile(
    rf"^\s*(\d{{2}}){_CSI_SEPARATOR}(\d{{2}})"
    rf"{_CSI_SEPARATOR}(\d{{2}})(?!\d)"
)
# Labeled compact form: ``SECTION 072726``. The explicit SECTION label makes
# the six digits credible CSI metadata even without separators (observed in
# real spec titles); bare compact numerics in titles/filenames stay rejected.
_LABELED_COMPACT_CSI_RE = re.compile(
    r"\bSECTION\s+(\d{2})(\d{2})(\d{2})(?!\d)", re.I
)
_DEDICATED_COMPACT_CSI_RE = re.compile(
    r"^\s*(?:(\d{2})|(\d{2})(\d{2})|(\d{2})(\d{2})(\d{2}))\s*$"
)

_ARCHITECTURE_TITLE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("architectural", re.compile(r"\barchitectur(?:e|al)\b", re.I)),
    ("building envelope", re.compile(r"\bbuilding envelope\b", re.I)),
    ("roofing", re.compile(r"\broof(?:ing| membrane| system)?s?\b", re.I)),
    ("waterproofing", re.compile(r"\bwaterproof(?:ing)?\b", re.I)),
    ("air barrier", re.compile(r"\bair barriers?\b", re.I)),
    ("thermal insulation", re.compile(r"\bthermal insulation\b", re.I)),
    ("curtain wall", re.compile(r"\bcurtain walls?\b", re.I)),
    ("storefront", re.compile(r"\bstorefronts?\b", re.I)),
    ("glazing", re.compile(r"\bglaz(?:ing|ed)\b", re.I)),
    ("doors", re.compile(r"\bdoors?(?: and frames)?\b", re.I)),
    ("door hardware", re.compile(r"\bdoor hardware\b", re.I)),
    ("louvers", re.compile(r"\blouvers?\b", re.I)),
    ("gypsum board", re.compile(r"\bgypsum board\b", re.I)),
    ("acoustical ceiling", re.compile(r"\bacoustical ceilings?\b", re.I)),
    ("flooring", re.compile(r"\bflooring\b", re.I)),
    ("painting", re.compile(r"\bpainting\b", re.I)),
    ("wall panels", re.compile(r"\bwall panels?\b", re.I)),
    ("elevator", re.compile(r"\belevators?\b", re.I)),
)

_FIRE_TITLE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fire suppression", re.compile(r"\bfire suppression\b", re.I)),
    ("sprinkler", re.compile(r"\bsprinklers?\b", re.I)),
    ("standpipe", re.compile(r"\bstandpipes?\b", re.I)),
    ("fire pump", re.compile(r"\bfire pumps?\b", re.I)),
    ("clean agent", re.compile(r"\bclean[- ]agent\b", re.I)),
    ("preaction", re.compile(r"\bpre[- ]?action\b", re.I)),
    ("water mist", re.compile(r"\bwater mist\b", re.I)),
)

_FIRE_ALARM_TITLE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "fire detection and alarm",
        re.compile(r"\bfire detection (?:and|&) alarm\b", re.I),
    ),
    (
        "fire alarm",
        re.compile(
            r"\bfire alarm\b"
            r"(?!(?:\s+systems?)?\s+(?:interfaces?|coordination)\b)",
            re.I,
        ),
    ),
    (
        "fire alarm system",
        re.compile(r"\bfire alarm systems?\b(?!\s+interfaces?\b)", re.I),
    ),
    ("fire detection", re.compile(r"\bfire detection\b", re.I)),
    (
        "fire alarm control unit",
        re.compile(
            r"\bfire alarm control (?:unit|panel)s?\b|\bFAC[PU]s?\b",
            re.I,
        ),
    ),
    ("initiating devices", re.compile(r"\binitiating devices?\b", re.I)),
    (
        "notification appliances",
        re.compile(r"\bnotification appliances?\b", re.I),
    ),
    (
        "emergency voice/alarm communications",
        re.compile(
            r"\bemergency voice(?:/alarm)? communication systems?\b|\bEVACS\b",
            re.I,
        ),
    ),
    (
        "aspirating smoke detection",
        re.compile(r"\baspirating smoke detection\b|\bVESDA\b", re.I),
    ),
    (
        "supervising station fire alarm",
        re.compile(r"\bsupervising[- ]station fire alarm\b", re.I),
    ),
    (
        "fire-alarm releasing controls",
        re.compile(r"\bfire[- ]alarm releasing control(?: unit| panel)?s?\b", re.I),
    ),
)

_NON_ALARM_SECURITY_TITLE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("access control", re.compile(r"\baccess control\b", re.I)),
    (
        "credentials/card readers",
        re.compile(r"\bcredentials?\b|\bcard readers?\b", re.I),
    ),
    (
        "video surveillance/CCTV",
        re.compile(r"\bvideo surveillance\b|\bCCTV\b", re.I),
    ),
    ("intrusion detection", re.compile(r"\bintrusion detection\b", re.I)),
    ("duress", re.compile(r"\bduress\b", re.I)),
    ("intercom", re.compile(r"\bintercom\b", re.I)),
    (
        "security management",
        re.compile(r"\bsecurity management systems?\b", re.I),
    ),
    ("electronic security", re.compile(r"\belectronic security\b", re.I)),
)

_ELECTRICAL_TITLE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("electrical", re.compile(r"\belectrical\b", re.I)),
    ("power distribution", re.compile(r"\bpower distribution\b", re.I)),
    (
        "medium voltage",
        re.compile(r"\bmedium[- ]voltage\b|\bMV distribution\b", re.I),
    ),
    ("switchgear", re.compile(r"\bswitchgear\b", re.I)),
    ("switchboard", re.compile(r"\bswitchboards?\b", re.I)),
    ("panelboard", re.compile(r"\bpanelboards?\b", re.I)),
    ("transformer", re.compile(r"\btransformers?\b", re.I)),
    ("busway", re.compile(r"\bbusways?\b", re.I)),
    (
        "generator",
        re.compile(r"\bgenerators?\b|\bengine[- ]generators?\b", re.I),
    ),
    (
        "paralleling",
        re.compile(r"\bparalleling (?:gear|switchgear|system)\b", re.I),
    ),
    (
        "UPS",
        re.compile(r"\buninterruptible power(?: supply| system)?\b|\bUPS\b", re.I),
    ),
    (
        "battery energy storage",
        re.compile(r"\bbattery energy storage\b|\bBESS\b", re.I),
    ),
    (
        "transfer switch",
        re.compile(r"\b(?:automatic|static) transfer switches?\b", re.I),
    ),
    (
        "grounding and bonding",
        re.compile(r"\bgrounding(?: and| &) bonding\b", re.I),
    ),
    (
        "power monitoring",
        re.compile(r"\b(?:electrical )?power monitoring\b|\bEPMS\b", re.I),
    ),
    ("protective relaying", re.compile(r"\bprotective relay(?:ing|s)?\b", re.I)),
    ("metering", re.compile(r"\bmetering\b", re.I)),
    ("lighting controls", re.compile(r"\blighting controls?\b", re.I)),
    ("branch circuits", re.compile(r"\bbranch circuits?\b", re.I)),
    ("raceways", re.compile(r"\braceways?\b|\bconduits?\b|\bcable trays?\b", re.I)),
    ("surge protection", re.compile(r"\bsurge protect(?:ion|ive device)s?\b", re.I)),
    ("lightning protection", re.compile(r"\blightning protection\b", re.I)),
    ("power-system studies", re.compile(r"\bpower[- ]system studies\b", re.I)),
    (
        "short-circuit study",
        re.compile(r"\bshort[- ]circuit stud(?:y|ies)\b", re.I),
    ),
    ("selective coordination", re.compile(r"\bselective coordination\b", re.I)),
    ("arc-flash study", re.compile(r"\barc[- ]flash stud(?:y|ies)\b", re.I)),
)

_ARCHITECTURE_CONTENT_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("air barrier", re.compile(r"\bair barriers?\b", re.I)),
    ("curtain wall", re.compile(r"\bcurtain walls?\b", re.I)),
    ("roof membrane", re.compile(r"\broof membranes?\b", re.I)),
    ("door hardware", re.compile(r"\bdoor hardware\b", re.I)),
    ("finish schedule", re.compile(r"\bfinish schedules?\b", re.I)),
    ("thermal insulation", re.compile(r"\bthermal insulation\b", re.I)),
    ("wall assembly", re.compile(r"\bwall assemblies?\b", re.I)),
    ("glazing", re.compile(r"\bglaz(?:ing|ed)\b", re.I)),
    ("gypsum board", re.compile(r"\bgypsum board\b", re.I)),
    ("acoustical ceiling", re.compile(r"\bacoustical ceilings?\b", re.I)),
)

_FIRE_CONTENT_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("NFPA 13", re.compile(r"\bNFPA\s*13\b", re.I)),
    ("sprinkler", re.compile(r"\bsprinklers?\b", re.I)),
    ("standpipe", re.compile(r"\bstandpipes?\b", re.I)),
    ("fire pump", re.compile(r"\bfire pumps?\b", re.I)),
    ("clean agent", re.compile(r"\bclean[- ]agent\b", re.I)),
    ("NFPA 2001", re.compile(r"\bNFPA\s*2001\b", re.I)),
    ("preaction", re.compile(r"\bpre[- ]?action\b", re.I)),
)

_FIRE_ALARM_CONTENT_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("NFPA 72", re.compile(r"\bNFPA\s*72\b", re.I)),
    ("fire alarm", re.compile(r"\bfire alarm\b", re.I)),
    ("fire detection", re.compile(r"\bfire detection\b", re.I)),
    (
        "fire alarm control unit",
        re.compile(
            r"\bfire alarm control (?:unit|panel)s?\b|\bFAC[PU]s?\b",
            re.I,
        ),
    ),
    (
        "signaling line circuit",
        re.compile(r"\bsignaling[- ]line circuits?\b|\bSLCs?\b", re.I),
    ),
    (
        "notification appliance circuit",
        re.compile(r"\bnotification appliance circuits?\b|\bNACs?\b", re.I),
    ),
    (
        "notification appliance",
        re.compile(r"\bnotification appliances?\b(?!\s+circuits?\b)", re.I),
    ),
    ("initiating device", re.compile(r"\binitiating devices?\b", re.I)),
    (
        "aspirating smoke detection",
        re.compile(
            r"\baspirating smoke detection\b|\bair[- ]sampling smoke detection\b|"
            r"\bVESDA\b|\bASD system\b",
            re.I,
        ),
    ),
    (
        "emergency voice/alarm communications",
        re.compile(
            r"\bemergency voice(?:/alarm)? communication systems?\b|"
            r"\bvoice evacuation\b|\bEVACS\b",
            re.I,
        ),
    ),
    ("supervising station", re.compile(r"\bsupervising station\b", re.I)),
    (
        "cause-and-effect",
        re.compile(r"\bcause[- ]and[- ]effect\b|\binput/output matrix\b", re.I),
    ),
    ("releasing panel", re.compile(r"\breleasing (?:control )?panel\b", re.I)),
)

_ELECTRICAL_CONTENT_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("NFPA 70", re.compile(r"\bNFPA\s*70\b", re.I)),
    ("NEC", re.compile(r"\bNEC\b|\bNational Electrical Code\b", re.I)),
    ("CSA C22.1", re.compile(r"\bCSA\s*C22\.1\b", re.I)),
    (
        "Canadian Electrical Code",
        re.compile(r"\bCanadian Electrical Code\b", re.I),
    ),
    ("NFPA 110", re.compile(r"\bNFPA\s*110\b", re.I)),
    ("IEEE 1584", re.compile(r"\bIEEE\s*1584\b", re.I)),
    ("NETA ATS", re.compile(r"\bNETA\s+ATS\b", re.I)),
    ("fault current", re.compile(r"\bfault current\b", re.I)),
    ("selective coordination", re.compile(r"\bselective coordination\b", re.I)),
    ("SCCR", re.compile(r"\bSCCR\b|\bshort[- ]circuit current rating\b", re.I)),
    ("arc flash", re.compile(r"\barc[- ]flash\b", re.I)),
    ("grounding electrode", re.compile(r"\bgrounding electrode\b", re.I)),
    ("A/B power path", re.compile(r"\bA\s*[/&-]\s*B power paths?\b", re.I)),
    ("EPMS", re.compile(r"\bEPMS\b|\belectrical power monitoring system\b", re.I)),
    ("switchgear", re.compile(r"\bswitchgear\b", re.I)),
    ("busway", re.compile(r"\bbusways?\b", re.I)),
    (
        "UPS",
        re.compile(r"\buninterruptible power(?: supply| system)?\b|\bUPS\b", re.I),
    ),
    ("generator paralleling", re.compile(r"\bgenerator paralleling\b", re.I)),
)


def _extract_csi_section(
    value: str,
    *,
    dedicated_section_number: bool = False,
) -> tuple[str, ...]:
    """Extract credible CSI metadata from one routing field.

    Arbitrary numeric substrings are deliberately ignored.  Compact values
    are accepted only from ``SpecRoutingInput.section_number``; titles and
    filenames need either an explicit ``SECTION`` label or a leading,
    separated six-digit CSI form.
    """

    text = value or ""
    match = _EXPLICIT_CSI_RE.search(text)
    if not match:
        match = _LABELED_COMPACT_CSI_RE.search(text)
    if not match:
        match = _LEADING_SEPARATED_CSI_RE.match(text)
    if not match and dedicated_section_number:
        compact = _DEDICATED_COMPACT_CSI_RE.fullmatch(text)
        if compact:
            return tuple(part for part in compact.groups() if part is not None)
    if not match:
        return ()
    return tuple(part for part in match.groups() if part is not None)


def _section_module_ids(section: tuple[str, ...]) -> tuple[str, ...]:
    if not section:
        return ()
    division = section[0]
    if division in _ARCHITECTURE_DIVISIONS:
        return (DATACENTER_ARCHITECTURE_MODULE_ID,)
    if division == _FIRE_DIVISION:
        return (DATACENTER_FIRE_MODULE_ID,)
    if (
        division == "28"
        and len(section) >= 2
        and section[1] in _FIRE_ALARM_DIVISION_28_FAMILIES
    ):
        return (DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,)
    if division == _ELECTRICAL_DIVISION:
        return (DATACENTER_ELECTRICAL_MODULE_ID,)
    if (
        division == "33"
        and len(section) >= 2
        and section[1] in _ELECTRICAL_DIVISION_33_FAMILIES
    ):
        return (DATACENTER_ELECTRICAL_MODULE_ID,)
    if division == _ELECTRICAL_DIVISION_48:
        return (DATACENTER_ELECTRICAL_MODULE_ID,)
    return ()


def _matched_terms(
    value: str, rules: tuple[tuple[str, re.Pattern[str]], ...]
) -> tuple[str, ...]:
    return tuple(label for label, pattern in rules if pattern.search(value or ""))


def _ordered_module_ids(
    module_ids: Iterable[str], program: ProgramDefinition
) -> tuple[str, ...]:
    requested = set(module_ids)
    return tuple(module_id for module_id in program.module_ids if module_id in requested)


def _title_weight(match_count: int) -> float:
    if match_count <= 0:
        return 0.0
    return min(0.95, 0.85 + (match_count - 1) * 0.05)


def _content_weight(match_count: int) -> float:
    if match_count <= 0:
        return 0.0
    if match_count == 1:
        return 0.20
    if match_count == 2:
        return 0.45
    if match_count == 3:
        return 0.65
    return min(0.95, 0.85 + (match_count - 4) * 0.05)


def route_spec(
    spec: SpecRoutingInput,
    *,
    program: ProgramDefinition = HYPERSCALE_DATACENTER_PROGRAM,
) -> SpecRoutingDecision:
    """Return a deterministic discipline assessment for one spec.

    Ambiguous decisions expose candidates through ``candidate_module_ids``
    but have no executable ``module_ids`` until a user applies an override.
    """

    evidence: list[RoutingEvidence] = []
    scores = {
        module_id: 0.0
        for module_id in (
            DATACENTER_FIRE_MODULE_ID,
            DATACENTER_ARCHITECTURE_MODULE_ID,
            DATACENTER_ELECTRICAL_MODULE_ID,
            DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID,
        )
        if module_id in program.module_ids
    }

    section_source = RoutingEvidenceSource.CSI_SECTION
    section = _extract_csi_section(
        spec.section_number,
        dedicated_section_number=True,
    )
    if not section:
        section = _extract_csi_section(spec.section_title)
        section_source = RoutingEvidenceSource.SECTION_TITLE
    section_ids = tuple(
        module_id
        for module_id in _section_module_ids(section)
        if module_id in scores
    )
    canonical_section = " ".join(section)
    if section:
        if section_ids:
            for module_id in section_ids:
                scores[module_id] += 0.95
                evidence.append(
                    RoutingEvidence(
                        source=section_source,
                        signal=canonical_section,
                        detail=f"CSI {canonical_section} maps to {module_id}",
                        module_id=module_id,
                        weight=0.95,
                    )
                )
        else:
            evidence.append(
                RoutingEvidence(
                    source=section_source,
                    signal=canonical_section,
                    detail=(
                        "CSI section is outside the implemented "
                        "hyperscale discipline map"
                    ),
                    module_id=None,
                    weight=0.0,
                )
            )

    title_matches = {
        DATACENTER_ARCHITECTURE_MODULE_ID: _matched_terms(
            spec.section_title, _ARCHITECTURE_TITLE_TERMS
        ),
        DATACENTER_FIRE_MODULE_ID: _matched_terms(
            spec.section_title, _FIRE_TITLE_TERMS
        ),
        DATACENTER_ELECTRICAL_MODULE_ID: _matched_terms(
            spec.section_title, _ELECTRICAL_TITLE_TERMS
        ),
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID: _matched_terms(
            spec.section_title, _FIRE_ALARM_TITLE_TERMS
        ),
    }
    title_ids: set[str] = set()
    for module_id in program.module_ids:
        matches = title_matches.get(module_id, ())
        if not matches or module_id not in scores:
            continue
        title_ids.add(module_id)
        weight = _title_weight(len(matches))
        scores[module_id] += weight
        evidence.append(
            RoutingEvidence(
                source=RoutingEvidenceSource.SECTION_TITLE,
                signal=", ".join(matches),
                detail=f"Section title contains {module_id} signal(s)",
                module_id=module_id,
                weight=weight,
            )
        )

    content_matches = {
        DATACENTER_ARCHITECTURE_MODULE_ID: _matched_terms(
            spec.content, _ARCHITECTURE_CONTENT_TERMS
        ),
        DATACENTER_FIRE_MODULE_ID: _matched_terms(spec.content, _FIRE_CONTENT_TERMS),
        DATACENTER_ELECTRICAL_MODULE_ID: _matched_terms(
            spec.content, _ELECTRICAL_CONTENT_TERMS
        ),
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID: _matched_terms(
            spec.content, _FIRE_ALARM_CONTENT_TERMS
        ),
    }
    for module_id in program.module_ids:
        matches = content_matches.get(module_id, ())
        if not matches or module_id not in scores:
            continue
        weight = _content_weight(len(matches))
        scores[module_id] += weight
        evidence.append(
            RoutingEvidence(
                source=RoutingEvidenceSource.CONTENT,
                signal=", ".join(matches),
                detail=f"Specification content contains {module_id} signal(s)",
                module_id=module_id,
                weight=weight,
            )
        )

    section_id_set = set(section_ids)
    non_alarm_security_title_matches = _matched_terms(
        spec.section_title,
        _NON_ALARM_SECURITY_TITLE_TERMS,
    )
    is_mapped_division_28_alarm_family = bool(
        section
        and section[0] == "28"
        and len(section) >= 2
        and section[1] in _FIRE_ALARM_DIVISION_28_FAMILIES
    )
    is_legacy_division_28_31 = bool(
        is_mapped_division_28_alarm_family and section[1] == "31"
    )
    alarm_title_matches = title_matches[
        DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID
    ]
    if (
        non_alarm_security_title_matches
        and alarm_title_matches
        and DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID in scores
    ):
        # A title that substantively names both fire alarm and an unsupported
        # security system may be a legitimate interface specification, but
        # phase 1 cannot silently claim the whole document.  Apply this guard
        # even when section metadata is absent.
        evidence.append(
            RoutingEvidence(
                source=RoutingEvidenceSource.SECTION_TITLE,
                signal=", ".join(non_alarm_security_title_matches),
                detail=(
                    "Section title combines fire alarm with Division 28 work "
                    "outside the implemented fire detection/alarm phase"
                ),
                module_id=None,
                weight=0.0,
            )
        )
        candidates = _ordered_module_ids(section_id_set | title_ids, program)
        return SpecRoutingDecision(
            spec_id=spec.spec_id,
            program_id=program.program_id,
            automatic_state=RoutingState.AMBIGUOUS,
            automatic_module_ids=candidates,
            confidence=0.50,
            evidence=tuple(evidence),
        )

    if (
        is_mapped_division_28_alarm_family
        and section_id_set
        and non_alarm_security_title_matches
    ):
        # 28 31 is a legacy fire-alarm family but is also used for intrusion
        # detection in newer MasterFormat editions.  Likewise, mislabeled
        # 28 46 files occur.  Explicit non-alarm security titles therefore
        # remain an explicit coverage gap instead of silently entering phase 1.
        evidence.append(
            RoutingEvidence(
                source=RoutingEvidenceSource.SECTION_TITLE,
                signal=", ".join(non_alarm_security_title_matches),
                detail=(
                    "Section title identifies Division 28 work outside the "
                    "implemented fire detection/alarm phase"
                ),
                module_id=None,
                weight=0.0,
            )
        )
        return SpecRoutingDecision(
            spec_id=spec.spec_id,
            program_id=program.program_id,
            automatic_state=RoutingState.UNSUPPORTED,
            automatic_module_ids=(),
            confidence=0.95,
            evidence=tuple(evidence),
        )

    if (
        is_legacy_division_28_31
        and section_id_set
        and not title_matches[DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID]
        and len(
            content_matches[DATACENTER_ELECTRONIC_SAFETY_SECURITY_MODULE_ID]
        )
        < 4
    ):
        # Older project manuals used 28 31 for fire detection/alarm, while
        # current MasterFormat places intrusion work in that family.  Require
        # corroborating fire-alarm metadata or content before auto-routing.
        evidence.append(
            RoutingEvidence(
                source=section_source,
                signal=canonical_section,
                detail=(
                    "Legacy CSI 28 31 requires a corroborating fire-alarm "
                    "title or strong fire-alarm content"
                ),
                module_id=None,
                weight=0.0,
            )
        )
        candidates = _ordered_module_ids(section_id_set, program)
        return SpecRoutingDecision(
            spec_id=spec.spec_id,
            program_id=program.program_id,
            automatic_state=RoutingState.AMBIGUOUS,
            automatic_module_ids=candidates,
            confidence=0.50,
            evidence=tuple(evidence),
        )

    if section_id_set and title_ids and section_id_set.isdisjoint(title_ids):
        # A strong section/title disagreement is likely a mislabeled file or a
        # multi-discipline document.  Do not execute either route silently.
        candidates = _ordered_module_ids(section_id_set | title_ids, program)
        return SpecRoutingDecision(
            spec_id=spec.spec_id,
            program_id=program.program_id,
            automatic_state=RoutingState.AMBIGUOUS,
            automatic_module_ids=candidates,
            confidence=0.50,
            evidence=tuple(evidence),
        )

    restricted_unimplemented_section = bool(
        section
        and not section_ids
        and section[0] in _RESTRICTED_UNIMPLEMENTED_DIVISIONS
    )
    if restricted_unimplemented_section:
        # Communications and non-alarm electronic safety/security do not yet
        # have reviewers.  A discipline-flavored title or body may be useful
        # as a user-confirmed candidate, but never overrides the explicit CSI
        # coverage gap automatically.
        candidates = _ordered_module_ids(
            (module_id for module_id, score in scores.items() if score >= 0.20),
            program,
        )
        if candidates:
            confidence = min(
                0.69,
                max(0.35, max(scores[mid] for mid in candidates)),
            )
            return SpecRoutingDecision(
                spec_id=spec.spec_id,
                program_id=program.program_id,
                automatic_state=RoutingState.AMBIGUOUS,
                automatic_module_ids=candidates,
                confidence=confidence,
                evidence=tuple(evidence),
            )

    supported: list[str] = []
    for module_id in program.module_ids:
        if module_id not in scores:
            continue
        has_metadata_signal = module_id in section_id_set or module_id in title_ids
        content_only_is_strong = not section and len(content_matches[module_id]) >= 4
        if scores[module_id] >= 0.80 and (
            has_metadata_signal or content_only_is_strong
        ):
            supported.append(module_id)

    if supported:
        selected = tuple(supported)
        confidence = min(0.99, min(scores[module_id] for module_id in selected))
        return SpecRoutingDecision(
            spec_id=spec.spec_id,
            program_id=program.program_id,
            automatic_state=RoutingState.SUPPORTED,
            automatic_module_ids=selected,
            confidence=confidence,
            evidence=tuple(evidence),
        )

    candidates = _ordered_module_ids(
        (module_id for module_id, score in scores.items() if score >= 0.20),
        program,
    )
    if candidates:
        confidence = min(0.69, max(0.35, max(scores[mid] for mid in candidates)))
        return SpecRoutingDecision(
            spec_id=spec.spec_id,
            program_id=program.program_id,
            automatic_state=RoutingState.AMBIGUOUS,
            automatic_module_ids=candidates,
            confidence=confidence,
            evidence=tuple(evidence),
        )

    return SpecRoutingDecision(
        spec_id=spec.spec_id,
        program_id=program.program_id,
        automatic_state=RoutingState.UNSUPPORTED,
        automatic_module_ids=(),
        confidence=0.98 if section else 0.80,
        evidence=tuple(evidence),
    )


def route_specs(
    specs: Iterable[SpecRoutingInput],
    *,
    program: ProgramDefinition = HYPERSCALE_DATACENTER_PROGRAM,
) -> tuple[SpecRoutingDecision, ...]:
    """Route specs independently while preserving their input order."""

    return tuple(route_spec(spec, program=program) for spec in specs)


def apply_user_override(
    decision: SpecRoutingDecision,
    module_ids: Iterable[str],
    *,
    reason: str,
    program: ProgramDefinition = HYPERSCALE_DATACENTER_PROGRAM,
) -> SpecRoutingDecision:
    """Return ``decision`` with an audited, program-valid user selection."""

    if decision.program_id != program.program_id:
        raise ValueError(
            f"decision belongs to program {decision.program_id!r}, not "
            f"{program.program_id!r}"
        )
    requested = tuple(module_ids)
    if len(set(requested)) != len(requested):
        raise ValueError("override module_ids must not contain duplicates")
    unknown = set(requested) - set(program.module_ids)
    if unknown:
        unknown_display = ", ".join(sorted(unknown))
        raise ValueError(f"override contains module ids outside the program: {unknown_display}")
    ordered = _ordered_module_ids(requested, program)
    return replace(
        decision,
        user_override=UserRoutingOverride(module_ids=ordered, reason=reason),
    )


def remove_user_override(decision: SpecRoutingDecision) -> SpecRoutingDecision:
    """Restore the retained automatic assessment as the effective outcome."""

    return replace(decision, user_override=None)
