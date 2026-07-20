"""Deterministic architecture/fire routing for the hyperscale program.

The classifier is intentionally conservative.  CSI metadata and a strongly
matching title can produce an executable route.  Content-only hints usually
produce an ambiguous candidate unless several independent, discipline-specific
signals agree.  This prevents a cross-reference to another discipline from
silently sending an otherwise unrelated specification to that module.

No electrical module or electrical fallback exists here.  Unmatched Division
26 and non-fire Division 28 specifications remain unsupported.
"""
from __future__ import annotations

from dataclasses import replace
import re
from typing import Iterable

from .catalog import (
    DATACENTER_ARCHITECTURE_MODULE_ID,
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
# alarm, so only its fire detection/alarm families route directly to the fire
# module; access control, CCTV, and other electronic-safety sections do not.
_FIRE_DIVISION = "21"
_FIRE_DIVISION_28_FAMILIES = frozenset({"31", "46"})

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
    ("fire alarm", re.compile(r"\bfire alarm\b", re.I)),
    ("fire detection", re.compile(r"\bfire detection\b", re.I)),
    ("clean agent", re.compile(r"\bclean[- ]agent\b", re.I)),
    ("preaction", re.compile(r"\bpre[- ]?action\b", re.I)),
    ("aspirating smoke detection", re.compile(r"\baspirating smoke\b", re.I)),
    ("water mist", re.compile(r"\bwater mist\b", re.I)),
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
    ("NFPA 72", re.compile(r"\bNFPA\s*72\b", re.I)),
    ("fire alarm", re.compile(r"\bfire alarm\b", re.I)),
    ("clean agent", re.compile(r"\bclean[- ]agent\b", re.I)),
    ("NFPA 2001", re.compile(r"\bNFPA\s*2001\b", re.I)),
    ("preaction", re.compile(r"\bpre[- ]?action\b", re.I)),
    ("releasing panel", re.compile(r"\breleasing panel\b", re.I)),
    ("VESDA", re.compile(r"\bVESDA\b", re.I)),
    ("aspirating smoke detection", re.compile(r"\baspirating smoke\b", re.I)),
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
        and section[1] in _FIRE_DIVISION_28_FAMILIES
    ):
        return (DATACENTER_FIRE_MODULE_ID,)
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
    """Return a deterministic architecture/fire assessment for one spec.

    Ambiguous decisions expose candidates through ``candidate_module_ids``
    but have no executable ``module_ids`` until a user applies an override.
    """

    evidence: list[RoutingEvidence] = []
    scores = {
        module_id: 0.0
        for module_id in (
            DATACENTER_FIRE_MODULE_ID,
            DATACENTER_ARCHITECTURE_MODULE_ID,
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
                    detail="CSI section is outside the implemented architecture/fire map",
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
