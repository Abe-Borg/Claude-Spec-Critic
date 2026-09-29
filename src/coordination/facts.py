"""Source-anchored coordination facts (plan EX-06, default off).

The coordination experiment looks for conflicts between specifications that
no cross-check request ever saw together: two specs in different chunks of a
chunked module, or two specs routed to different modules of a program. It
cannot send every such pair to a model, so it first reads each specification
*deterministically* for a narrow set of facts, and only a pair of facts that
look like two statements of one requirement with different values becomes a
candidate for the model (``candidates.py``).

This module is that reader. It is stdlib-only and makes no API call.

**Three categories, chosen from the corpus.** The only corpus of coordination
problems this repository holds is what the five modules' authors wrote down
as coordination anchors (their cross-check severity examples and review
categories). Counted in ``plans/experiments/EX-06-cross-coordination.md``:

- ``responsibility`` — division of work: who furnishes, installs, wires,
  programs, or tests an item. Named by every module (both CRITICAL examples
  that are cross-spec conflicts are responsibility conflicts).
- ``electrical`` — supply characteristics of an item: voltage, phase,
  frequency. The classic interface between the discipline that specifies the
  equipment and the one that feeds it.
- ``rating`` — capacity and rating quantities of an item: flow, pressure,
  power, apparent power, current.

Model numbers, zoning, materials, locations, and interface sequences are the
other categories the plan names; none is read yet (the record says why).

**What a fact is.** One statement, in one clause of one element, of one
attribute of one *subject*: the text as written (``raw_value``), the values
normalized (``values``, with each value's qualifier: exact, minimum, or
maximum), the element it came from, and the *scope* its clause states
(construction phase, building, data hall, and new/existing status). Anything
the reader had to assume is listed in ``uncertainty``; nothing is dropped
silently.

**Subjects are identified conservatively.** Two ways only:

- an equipment tag written with a hyphen (``FP-1``, ``ATS-2A``, ``UPS-A``),
  upper-cased and otherwise as written — ``FP-01`` is not ``FP-1``, since a
  different numbering scheme is not proof of a different item, but neither is
  the resemblance proof of the same one;
- a closed vocabulary of item names (:data:`SUBJECT_TERMS`), each with the
  only synonyms whose equivalence is documented (an abbreviation of the same
  term, a spelling variant, a term a standard renamed). "Fire pump" is never
  "jockey pump" and "emergency generator" is never "standby generator".

A value with no subject in its clause takes the subject of the element's
heading, marked as such. Values that find no subject at all are counted, not
guessed.

**Units are converted only where the conversion is exact** (gpm / L/s /
L/min; psi / kPa / bar; kV / V), and voltages carry one documented mapping:
a motor nameplate (utilization) voltage and the nominal system voltage it is
designed for (NEMA MG 1 / ANSI C84.1: 115↔120, 200↔208, 230↔240, 460↔480,
575↔600) are compatible, not different. Horsepower and kilowatts are kept
apart: a motor's shaft power and an electrical rating are different
attributes. Voltages are compared only within one class (below 50 V, 50 V to
1 kV, above 1 kV): a 24 V control circuit and a 120 V supply are two
attributes of one panel, not two statements of one value.

``POLICY_VERSION`` names these rules. A change to them is a new version, and
its measurement needs cases written after the change (the EX-04 rule).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: The candidate-selection rules' version (this module and ``candidates.py``).
POLICY_VERSION = "cx1"

CATEGORY_RESPONSIBILITY = "responsibility"
CATEGORY_ELECTRICAL = "electrical"
CATEGORY_RATING = "rating"
#: In priority order: the two CRITICAL cross-spec anchors in the corpus are
#: responsibility conflicts.
CATEGORIES: tuple[str, ...] = (
    CATEGORY_RESPONSIBILITY,
    CATEGORY_ELECTRICAL,
    CATEGORY_RATING,
)

QUALIFIER_EXACT = "exact"
QUALIFIER_MIN = "min"
QUALIFIER_MAX = "max"

SUBJECT_SOURCE_TEXT = "text"
SUBJECT_SOURCE_HEADING = "heading"
SUBJECT_SOURCE_PREVIOUS = "previous_clause"

#: The passage text kept on a fact (the element text, windowed around the
#: value when longer). Bounded so a very long paragraph cannot dominate a
#: request, and so a diagnostics record never carries a whole specification.
PASSAGE_MAX_CHARS = 1200
#: At most this many facts are read from one specification; the rest are
#: counted (``FactExtraction.facts_over_limit``), never silently dropped.
MAX_FACTS_PER_SPEC = 400

_DASHES = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "−": "-", " ": " ", " ": " ", " ": " ",
})


def _normalize_text(text: Any) -> str:
    return str(text or "").translate(_DASHES)


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubjectTerm:
    """One item name in the closed vocabulary.

    ``names`` are matched case-insensitively as whole words (a trailing
    plural ``s`` is allowed); ``abbreviations`` are matched case-sensitively,
    so "ups" in prose is never the UPS. ``justification`` says why every name
    in the entry is the same item — an entry with more than one name must
    carry one.
    """

    key: str
    display: str
    names: tuple[str, ...]
    abbreviations: tuple[str, ...] = ()
    justification: str = ""


SUBJECT_TERMS: tuple[SubjectTerm, ...] = (
    SubjectTerm("fire_pump", "fire pump", ("fire pump",)),
    SubjectTerm("fire_pump_controller", "fire pump controller", ("fire pump controller",)),
    SubjectTerm(
        "jockey_pump", "jockey pump",
        ("jockey pump", "pressure maintenance pump"),
        justification="NFPA 20 names it the pressure maintenance (jockey) pump.",
    ),
    SubjectTerm(
        "fire_alarm_control_unit", "fire alarm control unit",
        ("fire alarm control panel", "fire alarm control unit"),
        ("FACP", "FACU"),
        justification=(
            "NFPA 72 renamed the fire alarm control panel the fire alarm "
            "control unit; FACP and FACU abbreviate the two terms."
        ),
    ),
    SubjectTerm(
        "releasing_control_unit", "releasing control unit",
        ("releasing control panel", "releasing control unit", "releasing panel"),
        justification=(
            "Panel and unit are the older and current NFPA 72 words for the "
            "same releasing service control equipment."
        ),
    ),
    SubjectTerm(
        "automatic_transfer_switch", "automatic transfer switch",
        ("automatic transfer switch",), ("ATS",),
        justification="ATS abbreviates the term.",
    ),
    SubjectTerm(
        "ups", "uninterruptible power supply",
        ("uninterruptible power supply",), ("UPS",),
        justification="UPS abbreviates the term.",
    ),
    SubjectTerm("emergency_generator", "emergency generator", ("emergency generator",)),
    SubjectTerm("standby_generator", "standby generator", ("standby generator",)),
    SubjectTerm(
        "duct_smoke_detector", "duct smoke detector",
        ("duct smoke detector", "duct-mounted smoke detector", "duct detector"),
        justification="Spelling variants of the one device type.",
    ),
    SubjectTerm(
        "valve_supervisory_switch", "valve supervisory switch",
        ("valve supervisory switch", "valve tamper switch", "tamper switch"),
        justification=(
            "NFPA 72 names the device a valve supervisory switch; tamper "
            "switch is the trade name for it."
        ),
    ),
    SubjectTerm(
        "waterflow_switch", "waterflow switch",
        ("waterflow switch", "water flow switch", "flow switch"),
        justification="Spelling variants of the one device type.",
    ),
    SubjectTerm("air_compressor", "air compressor", ("air compressor",)),
    SubjectTerm(
        "fire_smoke_damper", "combination fire/smoke damper",
        (
            "combination fire/smoke damper",
            "combination fire and smoke damper",
            "fire/smoke damper",
        ),
        justification="Spelling variants of the combination damper.",
    ),
    SubjectTerm("smoke_damper", "smoke damper", ("smoke damper",)),
    SubjectTerm("fire_damper", "fire damper", ("fire damper",)),
    SubjectTerm(
        "shunt_trip", "shunt trip", ("shunt trip", "shunt-trip"),
        justification="Spelling variants.",
    ),
    SubjectTerm("clean_agent_system", "clean agent system", ("clean agent system",)),
    SubjectTerm(
        "preaction_system", "preaction system",
        ("preaction system", "pre-action system"),
        justification="Spelling variants.",
    ),
    SubjectTerm(
        "heat_tracing", "heat tracing", ("heat tracing", "heat trace"),
        justification="Spelling variants.",
    ),
    SubjectTerm("seismic_bracing", "seismic bracing", ("seismic bracing",)),
    SubjectTerm("seismic_anchorage", "seismic anchorage", ("seismic anchorage",)),
    SubjectTerm("seismic_restraint", "seismic restraint", ("seismic restraint",)),
)


def _term_regex(term: SubjectTerm) -> re.Pattern:
    parts = []
    for name in term.names:
        words = [re.escape(w) for w in name.split(" ")]
        body = r"\s+".join(words)
        parts.append(body + r"(?:e?s)?")
    pattern = r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])"
    return re.compile(pattern, re.IGNORECASE)


def _abbreviation_regex(term: SubjectTerm) -> re.Pattern | None:
    if not term.abbreviations:
        return None
    body = "|".join(re.escape(a) for a in term.abbreviations)
    # Case-sensitive whole word; a hyphenated tag (ATS-1) is read as a tag.
    return re.compile(r"(?<![A-Za-z0-9-])(?:" + body + r")s?(?![A-Za-z0-9-])")


_TERM_PATTERNS: tuple[tuple[SubjectTerm, re.Pattern, re.Pattern | None], ...] = tuple(
    (term, _term_regex(term), _abbreviation_regex(term)) for term in SUBJECT_TERMS
)

# Prefixes that read like tags but are standards, codes, or document parts.
TAG_STOPLIST: frozenset[str] = frozenset({
    "AHRI", "ANSI", "ARI", "ASCE", "ASHRAE", "ASME", "ASTM", "AWG", "AWS",
    "AWWA", "BICSI", "CAN", "CBC", "CEC", "CFC", "CLASS", "CMC", "CPC", "CSA",
    "CSI", "DIV", "DSA", "EIA", "FIG", "FIGURE", "FM", "HCAI", "IAPMO", "IBC",
    "IEBC", "IEC", "IECC", "IEEE", "IFC", "IFGC", "IMC", "IP", "IPC", "ISO",
    "MSS", "NBC", "NEC", "NEMA", "NETA", "NFC", "NFPA", "NPC", "NRTL", "OSHA",
    "OSHPD", "PART", "SCH", "SCHED", "SEC", "SECTION", "SEI", "SMACNA",
    "TABLE", "TIA", "TYPE", "UFC", "UL", "ULC", "ZONE",
    # Hyphenated prefixes in capitalized prose ("NON-UL", "PRE-ACTION" is
    # too long to match, "CO-OP"), never equipment.
    "ANTI", "AUTO", "CO", "HI", "LO", "MID", "MULTI", "NON", "PRE", "RE",
    "SELF", "SEMI", "SUB",
})

_TAG_RE = re.compile(
    r"(?<![A-Za-z0-9/.\-])([A-Z]{2,6})-(\d{1,3}[A-Z]?|[A-Z]{1,2}\d{0,2})(?![A-Za-z0-9\-])"
)


@dataclass(frozen=True)
class SubjectMatch:
    key: str  # "tag:FP-1" or "term:fire_pump"
    text: str
    start: int
    end: int

    @property
    def is_tag(self) -> bool:
        return self.key.startswith("tag:")


def find_subjects(text: str) -> list[SubjectMatch]:
    """Every subject named in ``text``, left to right, longest match first.

    Overlapping matches keep the longest ("fire pump controller" over "fire
    pump"; "combination fire/smoke damper" over "smoke damper"), so an item
    is never also read as a shorter item it contains.
    """
    text = _normalize_text(text)
    found: list[SubjectMatch] = []
    for match in _TAG_RE.finditer(text):
        prefix = match.group(1)
        if prefix in TAG_STOPLIST:
            continue
        tag = f"{prefix}-{match.group(2)}"
        found.append(SubjectMatch(f"tag:{tag}", match.group(0), match.start(), match.end()))
    for term, pattern, abbreviation in _TERM_PATTERNS:
        for match in pattern.finditer(text):
            found.append(SubjectMatch(f"term:{term.key}", match.group(0), match.start(), match.end()))
        if abbreviation is not None:
            for match in abbreviation.finditer(text):
                found.append(
                    SubjectMatch(f"term:{term.key}", match.group(0), match.start(), match.end())
                )
    found.sort(key=lambda m: (m.start, -(m.end - m.start), m.key))
    chosen: list[SubjectMatch] = []
    for match in found:
        if chosen and match.start < chosen[-1].end:
            continue
        chosen.append(match)
    return _merge_named_tags(text, chosen)


# Between an item's name and its tag: punctuation, "no.", "number", "tag".
_NAME_TAG_GAP = re.compile(
    r"^[\s,:(\[\-]*(?:(?:tag|no\.|number)\s*)?[\s,:(\[\-]*$", re.IGNORECASE
)


def _merge_named_tags(text: str, subjects: list[SubjectMatch]) -> list[SubjectMatch]:
    """Read "AC-1 air compressor" and "the fire pump, FP-1," as one subject.

    A name written right beside a tag is the tagged item's name, so the pair
    is one subject under the tag's key. A name standing alone stays a name:
    whether "the fire pump" is FP-1 is not something one clause can settle.
    """
    out: list[SubjectMatch] = []
    i = 0
    while i < len(subjects):
        current = subjects[i]
        if i + 1 < len(subjects):
            following = subjects[i + 1]
            gap = text[current.end:following.start]
            if current.is_tag != following.is_tag and _NAME_TAG_GAP.match(gap):
                tag = current if current.is_tag else following
                out.append(SubjectMatch(
                    tag.key, text[current.start:following.end], current.start, following.end
                ))
                i += 2
                continue
        out.append(current)
        i += 1
    return out


def subject_display(key: str) -> str:
    """A readable name for a subject key."""
    if key.startswith("tag:"):
        return key[4:]
    wanted = key[5:] if key.startswith("term:") else key
    for term in SUBJECT_TERMS:
        if term.key == wanted:
            return term.display
    return wanted


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

SCOPE_PHASE = "phase"
SCOPE_BUILDING = "building"
SCOPE_DATA_HALL = "data_hall"
SCOPE_STATUS = "status"

_ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6"}
# The keyword is case-insensitive; the identifier is not, so "building a
# wall" is no building. A construction phase is "Phase 2" — never the
# supply characteristic "3-phase" or "three phase 60 Hz" (see
# ``_electrical_phase_word``).
_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        SCOPE_PHASE,
        re.compile(
            r"(?<![\w-])(?i:phase)\s+(\d{1,2}|I{1,3}|IV|VI?)(?![\w-])"
            r"(?!\s*-?\s*(?i:hz|cycles?|wires?))"
        ),
    ),
    (SCOPE_BUILDING, re.compile(r"(?<![\w-])(?i:building)\s+([A-Z]\d{0,2}|\d{1,3})(?![\w-])")),
    (SCOPE_DATA_HALL, re.compile(r"(?<![\w-])(?i:data\s+hall)\s+([A-Z]\d{0,2}|\d{1,3})(?![\w-])")),
)
_ELECTRICAL_PHASE_BEFORE = re.compile(r"(?i:single|three|[13])\s*-?\s*$")
_STATUS_WORDS = r"existing|future|temporary|relocated|alternate"
_STATUS_RE = re.compile(r"(?<![\w-])(" + _STATUS_WORDS + r")(?![\w-])", re.IGNORECASE)
# A status word modifies the subject it stands before ("existing fire pump",
# "the future UPS-3 module"), with at most two words between.
_STATUS_BEFORE_SUBJECT = re.compile(
    r"(?<![\w-])(" + _STATUS_WORDS + r")(?:\s+[A-Za-z][\w/-]*){0,2}\s*$", re.IGNORECASE
)


def find_scope(text: str) -> tuple[tuple[str, str], ...]:
    """The location scope a clause states: ``((kind, value), ...)``, sorted.

    Construction phase ("Phase 2" — never "3-phase" or "three phase 60 Hz",
    which are supply characteristics), building, and data hall. Status
    (existing, future, …) is read per subject by :func:`status_scope`, since
    "connect to the existing fire alarm system" says nothing about the new
    panel the same sentence names.
    """
    text = _normalize_text(text)
    out: set[tuple[str, str]] = set()
    for kind, pattern in _SCOPE_PATTERNS:
        for match in pattern.finditer(text):
            if kind == SCOPE_PHASE and _ELECTRICAL_PHASE_BEFORE.search(text[:match.start()]):
                continue
            value = match.group(1).upper()
            if kind == SCOPE_PHASE:
                value = _ROMAN.get(value, value.lstrip("0") or "0")
            out.add((kind, value))
    return tuple(sorted(out))


def status_scope(text: str, subject_start: int | None = None) -> tuple[tuple[str, str], ...]:
    """Status words that modify a subject.

    With ``subject_start`` (a subject named in ``text``), the words standing
    just before the subject; without it (a heading), every status word.
    """
    text = _normalize_text(text)
    if subject_start is None:
        words = {m.group(1).lower() for m in _STATUS_RE.finditer(text)}
    else:
        match = _STATUS_BEFORE_SUBJECT.search(text[max(0, subject_start - 60):subject_start])
        words = {match.group(1).lower()} if match else set()
    return tuple(sorted((SCOPE_STATUS, word) for word in words))


def scope_conflict(a: Sequence[tuple[str, str]], b: Sequence[tuple[str, str]]) -> str:
    """Why two scopes cannot be the same scope, or ``""`` when they can be.

    A kind both sides state with no value in common (Phase 1 / Phase 2,
    Building A / Building B) separates them, and so does any difference in
    status: an existing fire pump and a new one are two pumps.
    """
    kinds: dict[str, tuple[set[str], set[str]]] = {}
    for kind, value in a:
        kinds.setdefault(kind, (set(), set()))[0].add(value)
    for kind, value in b:
        kinds.setdefault(kind, (set(), set()))[1].add(value)
    status_a, status_b = kinds.get(SCOPE_STATUS, (set(), set()))
    if status_a != status_b:
        return (
            "status differs ("
            + (", ".join(sorted(status_a)) or "none stated")
            + " / "
            + (", ".join(sorted(status_b)) or "none stated")
            + ")"
        )
    for kind, (va, vb) in sorted(kinds.items()):
        if kind == SCOPE_STATUS:
            continue
        if va and vb and not (va & vb):
            return f"{kind.replace('_', ' ')} differs ({', '.join(sorted(va))} / {', '.join(sorted(vb))})"
    return ""


def scope_one_sided(a: Sequence[tuple[str, str]], b: Sequence[tuple[str, str]]) -> bool:
    """Whether one side states a phase, building, or hall the other does not."""
    kinds_a = {kind for kind, _ in a if kind != SCOPE_STATUS}
    kinds_b = {kind for kind, _ in b if kind != SCOPE_STATUS}
    return kinds_a != kinds_b


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

ATTR_VOLTAGE = "voltage"
ATTR_PHASE = "phase"
ATTR_FREQUENCY = "frequency"
ATTR_FLOW = "flow"
ATTR_PRESSURE = "pressure"
ATTR_POWER_HP = "power_hp"
ATTR_POWER_KW = "power_kw"
ATTR_APPARENT_POWER = "apparent_power_kva"
ATTR_CURRENT = "current"

#: Actions a responsibility statement assigns. ``provide`` is "furnish and
#: install" (the usual Division 01 definition), so it is both.
ACTION_FURNISH = "furnish"
ACTION_INSTALL = "install"
ACTION_WIRE = "wire"
ACTION_PROGRAM = "program"
ACTION_TEST = "test"
ACTION_DESIGN = "design"

_VERB_ACTIONS: dict[str, frozenset[str]] = {
    "furnish": frozenset({ACTION_FURNISH}),
    "supply": frozenset({ACTION_FURNISH}),
    "provide": frozenset({ACTION_FURNISH, ACTION_INSTALL}),
    "install": frozenset({ACTION_INSTALL}),
    "mount": frozenset({ACTION_INSTALL}),
    "wire": frozenset({ACTION_WIRE}),
    "connect": frozenset({ACTION_WIRE}),
    "program": frozenset({ACTION_PROGRAM}),
    "test": frozenset({ACTION_TEST}),
    "commission": frozenset({ACTION_TEST}),
    "design": frozenset({ACTION_DESIGN}),
}
_VERB_FORMS = (
    r"furnish(?:ed|es)?|suppl(?:y|ied|ies)|provid(?:e|ed|es)|install(?:ed|s)?|"
    r"mount(?:ed|s)?|wir(?:e|ed|es)|connect(?:ed|s)?|program(?:med|s)?|"
    r"test(?:ed|s)?|commission(?:ed|s)?|design(?:ed|s)?"
)


_VERB_PREFIXES: tuple[tuple[str, frozenset[str]], ...] = (
    ("furnish", frozenset({ACTION_FURNISH})),
    ("suppl", frozenset({ACTION_FURNISH})),
    ("provid", frozenset({ACTION_FURNISH, ACTION_INSTALL})),
    ("install", frozenset({ACTION_INSTALL})),
    ("mount", frozenset({ACTION_INSTALL})),
    ("wir", frozenset({ACTION_WIRE})),
    ("connect", frozenset({ACTION_WIRE})),
    ("program", frozenset({ACTION_PROGRAM})),
    ("test", frozenset({ACTION_TEST})),
    ("commission", frozenset({ACTION_TEST})),
    ("design", frozenset({ACTION_DESIGN})),
)
# A work noun before the verb says what is being provided: "power wiring to
# the fire pump controller shall be provided by Division 26" assigns the
# wiring, not the controller.
_WORK_NOUNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?<![\w-])(?:wiring|conduit|raceways?|connections?)(?![\w-])", re.IGNORECASE), ACTION_WIRE),
    (re.compile(r"(?<![\w-])programming(?![\w-])", re.IGNORECASE), ACTION_PROGRAM),
    (re.compile(r"(?<![\w-])(?:testing|commissioning)(?![\w-])", re.IGNORECASE), ACTION_TEST),
    (re.compile(r"(?<![\w-])(?:design|calculations?)(?![\w-])", re.IGNORECASE), ACTION_DESIGN),
    (re.compile(r"(?<![\w-])installation(?![\w-])", re.IGNORECASE), ACTION_INSTALL),
)


def _verb_actions(word: str) -> frozenset[str]:
    word = word.lower()
    for prefix, actions in _VERB_PREFIXES:
        if word.startswith(prefix):
            return actions
    return frozenset()


def _work_noun_actions(text: str) -> set[str]:
    return {action for pattern, action in _WORK_NOUNS if pattern.search(text)}


# A party: a division, a section (its division is the party), this section,
# a named contractor, the owner, or a manufacturer. "Others" is no party.
_PARTY = (
    r"(?P<division>division\s+(?P<div>\d{2}))"
    r"|(?P<section>section\s+(?P<sdiv>\d{2})\s?\d{2}\s?\d{2}(?:\.\d+)?)"
    r"|(?P<this>this\s+section)"
    r"|(?:the\s+)?(?P<contractor>(?:[a-z]+\s+){0,2}?(?:contractor|subcontractor|installer))"
    r"|(?:the\s+)?(?P<owner>owner)"
    r"|(?:the\s+)?(?P<maker>(?:equipment\s+)?(?:manufacturer|supplier|vendor))"
    r"|(?P<others>others)"
)
_PASSIVE_RE = re.compile(
    r"(?<![\w-])(?P<verb1>" + _VERB_FORMS + r")"
    r"(?:\s*(?:,|and|/|&)\s*(?P<verb2>" + _VERB_FORMS + r"))?"
    r"(?:\s*(?:,|and|/|&)\s*(?P<verb3>" + _VERB_FORMS + r"))?"
    r"\s+(?:by|under|in)\s+(?:" + _PARTY + r")(?![\w])",
    re.IGNORECASE,
)
# "Wiring of tamper switches by Division 28": no verb, so only a work noun
# before it says what is assigned (without one, "by Division 28" is not a
# responsibility statement this reader trusts).
_BARE_RE = re.compile(
    r"(?<![\w-])(?:by|under)\s+(?:" + _PARTY + r")(?![\w])",
    re.IGNORECASE,
)
_ACTIVE_RE = re.compile(
    r"(?<![\w-])(?:" + _PARTY + r")\s+(?:shall|will|is\s+to|to)\s+"
    r"(?P<verb1>" + _VERB_FORMS + r")"
    r"(?:\s*(?:,|and|/|&)\s*(?P<verb2>" + _VERB_FORMS + r"))?",
    re.IGNORECASE,
)


def _party_from(match: re.Match, *, own_division: str) -> tuple[str, str]:
    """``(party key, uncertainty)`` for a matched party, ``("", "")`` for none."""
    if match.group("division"):
        return f"division:{match.group('div')}", ""
    if match.group("section"):
        return f"division:{match.group('sdiv')}", ""
    if match.group("this"):
        if own_division:
            return f"division:{own_division}", "party read from 'this Section' and the file's CSI number"
        return "", ""
    if match.group("contractor"):
        words = re.sub(r"\s+", " ", match.group("contractor").lower()).strip()
        return f"contractor:{words}", ""
    if match.group("owner"):
        return "owner", ""
    if match.group("maker"):
        return "manufacturer", ""
    return "", ""


_NUMBER = r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_VOLT_PAIR_RE = re.compile(
    r"(?<![\d.,])(?P<a>\d{2,3})\s*Y?\s*/\s*(?P<b>\d{2,3})\s*-?\s*(?P<unit>VAC|VDC|V|volts?)(?![A-Za-z])",
    re.IGNORECASE,
)
_VOLT_RE = re.compile(
    r"(?<![\d.,/])" + _NUMBER + r"\s*-?\s*(?P<unit>kV|VAC|VDC|V|volts?)(?![A-Za-z])",
    re.IGNORECASE,
)
_PHASE_RE = re.compile(
    r"(?<![\w])(?P<n>single|three|1|3)\s*-?\s*(?:phase|ph\b|Ø)|(?P<m>[13])Ø",
    re.IGNORECASE,
)
_FREQ_RE = re.compile(r"(?<![\d.])(?P<num>50|60)\s*-?\s*Hz(?![A-Za-z])", re.IGNORECASE)

# (attribute, unit pattern, factor to the attribute's base unit, base unit)
_RATING_UNITS: tuple[tuple[str, str, float, str], ...] = (
    (ATTR_FLOW, r"gpm|gal(?:lons)?\s*(?:/|per)\s*min(?:ute)?", 1.0, "gpm"),
    (ATTR_FLOW, r"L\s*/\s*s|l/s", 15.850323, "gpm"),
    (ATTR_FLOW, r"L\s*/\s*min|lpm", 0.264172, "gpm"),
    (ATTR_PRESSURE, r"psig?", 1.0, "psi"),
    (ATTR_PRESSURE, r"kPa", 0.1450377, "psi"),
    (ATTR_PRESSURE, r"bar", 14.503774, "psi"),
    (ATTR_POWER_HP, r"hp|horsepower", 1.0, "hp"),
    (ATTR_POWER_KW, r"kW", 1.0, "kW"),
    (ATTR_POWER_KW, r"MW", 1000.0, "kW"),
    (ATTR_APPARENT_POWER, r"kVA", 1.0, "kVA"),
    (ATTR_APPARENT_POWER, r"MVA", 1000.0, "kVA"),
    (ATTR_CURRENT, r"A|amps?|amperes?", 1.0, "A"),
)
_RATING_RE = re.compile(
    r"(?<![\w.,/])" + _NUMBER + r"\s*-?\s*(?P<unit>"
    + "|".join(f"(?P<r{i}>{pattern})" for i, (_, pattern, _, _) in enumerate(_RATING_UNITS))
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CASE_SENSITIVE_SPELLINGS = frozenset({
    "A", "amp", "amps", "ampere", "amperes", "Amp", "Amps", "Ampere", "Amperes",
    "kW", "KW", "MW", "kVA", "KVA", "MVA",
})

# A bound word anywhere between the previous number and the value ("minimum
# rated capacity: 1,000 gpm"), or just after it ("1,000 gpm minimum").
_MIN_BEFORE = re.compile(
    r"(?<![\w-])(?:minimum|min\.|not\s+less\s+than|at\s+least|no\s+less\s+than)(?![\w-])",
    re.IGNORECASE,
)
_MAX_BEFORE = re.compile(
    r"(?<![\w-])(?:maximum|max\.|not\s+more\s+than|not\s+to\s+exceed|not\s+exceeding|"
    r"no\s+more\s+than|up\s+to)(?![\w-])",
    re.IGNORECASE,
)
_MIN_AFTER = re.compile(r"^\s*(?:,\s*)?(?:minimum|min\.?|or\s+(?:more|greater))(?![\w])", re.IGNORECASE)
_MAX_AFTER = re.compile(r"^\s*(?:,\s*)?(?:maximum|max\.?|or\s+less)(?![\w])", re.IGNORECASE)
_APPROX_BEFORE = re.compile(r"(?<![\w-])(?:approximately|approx\.|about|nominal(?:ly)?)(?![\w-])", re.IGNORECASE)
_LAST_DIGIT = re.compile(r"\d(?!.*\d)", re.DOTALL)

#: Motor nameplate (utilization) voltage -> nominal system voltage it is
#: designed for (NEMA MG 1 / ANSI C84.1). Used only to call two statements
#: compatible, never to call them equal.
UTILIZATION_TO_NOMINAL: dict[int, int] = {115: 120, 200: 208, 230: 240, 460: 480, 575: 600}


def _to_float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _qualifier(text: str, start: int, end: int) -> tuple[str, bool]:
    """``(qualifier, approximate)`` for the value at ``text[start:end]``."""
    before = text[max(0, start - 40):start]
    # Only the words since the previous number qualify this value.
    last_digit = _LAST_DIGIT.search(before)
    if last_digit is not None:
        before = before[last_digit.end():]
    after = text[end:end + 25]
    approximate = bool(_APPROX_BEFORE.search(before))
    if _MIN_BEFORE.search(before) or _MIN_AFTER.search(after):
        return QUALIFIER_MIN, approximate
    if _MAX_BEFORE.search(before) or _MAX_AFTER.search(after):
        return QUALIFIER_MAX, approximate
    return QUALIFIER_EXACT, approximate


def voltage_class(volts: float) -> str:
    """``low`` (below 50 V), ``utilization`` (50 V to 1 kV), or ``medium``."""
    if volts < 50:
        return "low"
    if volts <= 1000:
        return "utilization"
    return "medium"


@dataclass(frozen=True)
class ValueHit:
    """One value found in a clause, before it is given to a subject."""

    category: str
    attribute: str  # "voltage:utilization:ac", "flow", "furnish", ...
    values: tuple[tuple[Any, str], ...]  # ((value, qualifier), ...)
    unit: str
    raw: str
    start: int
    end: int
    notes: tuple[str, ...] = ()
    party: str = ""


def _electrical_hits(text: str) -> list[ValueHit]:
    hits: list[ValueHit] = []
    taken: list[tuple[int, int]] = []
    for match in _VOLT_PAIR_RE.finditer(text):
        a, b = int(match.group("a")), int(match.group("b"))
        unit = match.group("unit").upper()
        current = "dc" if unit == "VDC" else "ac"
        klass = voltage_class(max(a, b))
        hits.append(ValueHit(
            CATEGORY_ELECTRICAL, f"{ATTR_VOLTAGE}:{klass}:{current}",
            ((a, QUALIFIER_EXACT), (b, QUALIFIER_EXACT)), "V",
            match.group(0), match.start(), match.end(),
        ))
        taken.append((match.start(), match.end()))
    for match in _VOLT_RE.finditer(text):
        if any(s <= match.start() < e for s, e in taken):
            continue
        number = _to_float(match.group("num"))
        if number is None:
            continue
        unit = match.group("unit")
        if unit.lower() == "kv":
            number *= 1000.0
        current = "dc" if unit.upper() == "VDC" else "ac"
        qualifier, approximate = _qualifier(text, match.start(), match.end())
        notes = ("the value is stated as approximate",) if approximate else ()
        hits.append(ValueHit(
            CATEGORY_ELECTRICAL, f"{ATTR_VOLTAGE}:{voltage_class(number)}:{current}",
            ((round(number, 3), qualifier),), "V",
            match.group(0), match.start(), match.end(), notes,
        ))
    for match in _PHASE_RE.finditer(text):
        word = (match.group("n") or match.group("m") or "").lower()
        count = 1 if word in ("single", "1") else 3
        hits.append(ValueHit(
            CATEGORY_ELECTRICAL, ATTR_PHASE, ((count, QUALIFIER_EXACT),), "phase",
            match.group(0), match.start(), match.end(),
        ))
    for match in _FREQ_RE.finditer(text):
        hits.append(ValueHit(
            CATEGORY_ELECTRICAL, ATTR_FREQUENCY, ((int(match.group("num")), QUALIFIER_EXACT),),
            "Hz", match.group(0), match.start(), match.end(),
        ))
    return hits


# A rating quantity's role, read from the words just before it. Two values
# with different roles are different attributes ("churn 120 psi" is not the
# rated pressure); a value with no role word is the plain rating.
_ROLE_WORDS: dict[str, tuple[tuple[re.Pattern, str], ...]] = {
    ATTR_PRESSURE: (
        (re.compile(r"churn|shut-?off", re.IGNORECASE), "churn"),
        (re.compile(r"suction", re.IGNORECASE), "suction"),
        (re.compile(r"discharge", re.IGNORECASE), "discharge"),
        (re.compile(r"static", re.IGNORECASE), "static"),
        (re.compile(r"residual", re.IGNORECASE), "residual"),
        (re.compile(r"working", re.IGNORECASE), "working"),
        (re.compile(r"hydrostatic|test", re.IGNORECASE), "test"),
        (re.compile(r"150\s*%|overload", re.IGNORECASE), "overload"),
    ),
    ATTR_FLOW: (
        (re.compile(r"150\s*%|overload", re.IGNORECASE), "overload"),
        (re.compile(r"demand", re.IGNORECASE), "demand"),
    ),
    ATTR_CURRENT: (
        (re.compile(r"frame", re.IGNORECASE), "frame"),
        (re.compile(r"trip", re.IGNORECASE), "trip"),
        (re.compile(r"full[\s-]*load|\bFLA\b", re.IGNORECASE), "full_load"),
        (re.compile(r"locked[\s-]*rotor|\bLRA\b", re.IGNORECASE), "locked_rotor"),
    ),
    ATTR_POWER_KW: (
        (re.compile(r"standby", re.IGNORECASE), "standby"),
        (re.compile(r"prime", re.IGNORECASE), "prime"),
        (re.compile(r"continuous", re.IGNORECASE), "continuous"),
    ),
}
_ROLE_WINDOW = 40


def _role(attribute: str, text: str, start: int, end: int) -> str:
    """The role word nearest before (or just after) a value, or ``""``."""
    rules = _ROLE_WORDS.get(attribute, ())
    before = text[max(0, start - _ROLE_WINDOW):start]
    after = text[end:end + 15]
    best = ("", -1)
    for pattern, role in rules:
        for match in pattern.finditer(before):
            if match.start() > best[1]:
                best = (role, match.start())
    if best[0]:
        return best[0]
    for pattern, role in rules:
        if pattern.match(after.lstrip()):
            return role
    return ""


def _rating_hits(text: str) -> list[ValueHit]:
    hits: list[ValueHit] = []
    for match in _RATING_RE.finditer(text):
        number = _to_float(match.group("num"))
        if number is None:
            continue
        for i, (attribute, _pattern, factor, base) in enumerate(_RATING_UNITS):
            unit_text = match.group(f"r{i}")
            if unit_text is None:
                continue
            if base in {"A", "kW", "kVA"} and unit_text not in _CASE_SENSITIVE_SPELLINGS:
                # Case matters for these units: "a" is an article, and "kw"
                # or "kva" is not how a specification writes a unit.
                break
            qualifier, approximate = _qualifier(text, match.start(), match.end())
            notes = ("the value is stated as approximate",) if approximate else ()
            role = _role(attribute, text, match.start(), match.end())
            hits.append(ValueHit(
                CATEGORY_RATING, f"{attribute}:{role}" if role else attribute,
                ((round(number * factor, 4), qualifier),), base,
                match.group(0), match.start(), match.end(), notes,
            ))
            break
    return hits


def _responsibility_hits(text: str, *, own_division: str) -> list[ValueHit]:
    hits: list[ValueHit] = []
    covered: list[tuple[int, int]] = []
    for pattern in (_PASSIVE_RE, _ACTIVE_RE, _BARE_RE):
        for match in pattern.finditer(text):
            if pattern is _BARE_RE:
                if any(s <= match.start() < e for s, e in covered):
                    continue
                nouns = _work_noun_actions(text[:match.start()])
                if not nouns:
                    continue
            party, note = _party_from(match, own_division=own_division)
            if not party:
                continue
            covered.append((match.start(), match.end()))
            if pattern is _BARE_RE:
                for action in sorted(nouns):
                    hits.append(ValueHit(
                        CATEGORY_RESPONSIBILITY, action, ((party, QUALIFIER_EXACT),), "party",
                        match.group(0), match.start(), match.end(),
                        (note,) if note else (), party=party,
                    ))
                continue
            actions: set[str] = set()
            for name in ("verb1", "verb2", "verb3"):
                verb = match.groupdict().get(name)
                if verb:
                    actions |= _verb_actions(verb)
            if pattern is _PASSIVE_RE:
                nouns = _work_noun_actions(text[:match.start()])
            else:
                nouns = _work_noun_actions(text[match.end():])
            if nouns and actions & {ACTION_FURNISH, ACTION_INSTALL}:
                actions = nouns
            for action in sorted(actions):
                hits.append(ValueHit(
                    CATEGORY_RESPONSIBILITY, action, ((party, QUALIFIER_EXACT),), "party",
                    match.group(0), match.start(), match.end(),
                    (note,) if note else (), party=party,
                ))
    return hits


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinationFact:
    """One statement of one attribute of one subject, anchored to its source."""

    fact_id: str
    file_name: str
    element_id: str
    heading: str
    category: str
    attribute: str
    subject: str
    subject_text: str
    subject_source: str
    raw_value: str
    values: tuple[tuple[Any, str], ...]
    unit: str
    scope: tuple[tuple[str, str], ...]
    passage: str
    span: tuple[int, int]
    uncertainty: tuple[str, ...] = ()
    module_id: str = ""

    @property
    def subject_is_tag(self) -> bool:
        return self.subject.startswith("tag:")

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "file_name": self.file_name,
            "element_id": self.element_id,
            "module_id": self.module_id,
            "heading": self.heading,
            "category": self.category,
            "attribute": self.attribute,
            "subject": self.subject,
            "subject_text": self.subject_text,
            "subject_source": self.subject_source,
            "raw_value": self.raw_value,
            "values": [[value, qualifier] for value, qualifier in self.values],
            "unit": self.unit,
            "scope": [list(item) for item in self.scope],
            "uncertainty": list(self.uncertainty),
        }


@dataclass
class FactExtraction:
    """What the reader found in one specification."""

    file_name: str
    facts: list[CoordinationFact] = field(default_factory=list)
    #: Values found with no subject in their clause or heading. Counted, so
    #: an area the reader could not attribute is visible, not silent.
    unattributed_values: int = 0
    facts_over_limit: int = 0
    elements_read: int = 0


# A clause ends at ";" or a sentence end; a table row is one clause.
_CLAUSE_SPLIT = re.compile(r";|\.(?=\s+[A-Z(])|\n")


def _clauses(text: str, *, is_row: bool) -> list[tuple[int, int]]:
    if is_row:
        return [(0, len(text))]
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _CLAUSE_SPLIT.finditer(text):
        spans.append((cursor, match.start()))
        cursor = match.end()
    spans.append((cursor, len(text)))
    return [(s, e) for s, e in spans if text[s:e].strip()]


def _window(text: str, start: int, end: int, limit: int = PASSAGE_MAX_CHARS) -> tuple[str, int]:
    """``(passage, offset)``: ``text`` itself, or a window of it around a span."""
    if len(text) <= limit:
        return text, 0
    half = max(0, (limit - (end - start)) // 2)
    lo = max(0, start - half)
    hi = min(len(text), lo + limit)
    lo = max(0, hi - limit)
    return text[lo:hi], lo


def own_division(file_name: str) -> str:
    """The two-digit CSI division a file name leads with, or ``""``."""
    match = re.match(r"\s*(?:section\s*)?(\d{2})[\s\-_]?\d{2}", file_name or "", re.IGNORECASE)
    return match.group(1) if match else ""


def _fact_id(*parts: Any) -> str:
    return "fx-" + hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:12]


def _subjects_for(
    hit: ValueHit, subjects: Sequence[SubjectMatch], *, is_row: bool = False
) -> tuple[list[SubjectMatch], tuple[str, ...]]:
    """The subject(s) a value in a clause belongs to, and why that is uncertain.

    A responsibility statement applies to every subject in its clause ("duct
    smoke detectors and fire/smoke dampers shall be installed by Division
    23"). A value belongs to the nearest subject before it, else the first
    after it.
    """
    distinct = {s.key for s in subjects}
    notes: list[str] = []
    if is_row:
        # An equipment-schedule row that names one tag describes that item:
        # every value in the row is its value.
        tags = [s for s in subjects if s.is_tag]
        if len({s.key for s in tags}) == 1:
            return [tags[0]], ()
    if len(distinct) > 1:
        notes.append("several subjects are named in the clause")
    if hit.category == CATEGORY_RESPONSIBILITY:
        seen: dict[str, SubjectMatch] = {}
        for subject in subjects:
            seen.setdefault(subject.key, subject)
        return list(seen.values()), tuple(notes)
    before = [s for s in subjects if s.end <= hit.start]
    if before:
        return [before[-1]], tuple(notes)
    after = [s for s in subjects if s.start >= hit.end]
    if after:
        return [after[0]], tuple(notes)
    return [], ()


@dataclass
class _Pending:
    """The values of one attribute of one subject in one clause, merged."""

    subject: SubjectMatch
    source: str
    hits: list[ValueHit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def extract_facts(spec: Any, *, module_id: str = "") -> FactExtraction:
    """Read one extracted specification for coordination facts.

    ``spec`` is an ``ExtractedSpec`` (or any object with ``filename`` and a
    ``paragraph_map`` of objects with ``element_id``, ``text``,
    ``section_id``, and ``element_type``). A spec without a paragraph map
    yields nothing, and the caller records it as unread. Deterministic: the
    same spec gives the same facts, in element order.

    Every value of one attribute that a clause states for one subject becomes
    one fact ("TR-1: 480 V primary, 208Y/120 V secondary" is one voltage fact
    holding 480, 208, and 120), so a second specification that states any one
    of them is not read as contradicting it.
    """
    file_name = str(getattr(spec, "filename", "") or "")
    extraction = FactExtraction(file_name=file_name)
    division = own_division(file_name)
    for mapping in getattr(spec, "paragraph_map", None) or ():
        raw_text = _normalize_text(getattr(mapping, "text", ""))
        if not raw_text.strip():
            continue
        element_id = str(getattr(mapping, "element_id", "") or "")
        if element_id.startswith("meta:"):
            continue
        extraction.elements_read += 1
        heading = _normalize_text(getattr(mapping, "section_id", "") or "").strip()
        is_row = getattr(mapping, "element_type", "") == "table_cell"
        heading_subjects = find_subjects(heading) if heading else []
        heading_scope = find_scope(heading) if heading else ()
        heading_status = status_scope(heading) if heading else ()
        # The subject a clause ends on carries into the next clause of the
        # same element ("FP-1 motor: 460 V; rated 1,000 gpm"), never further.
        carried: SubjectMatch | None = None
        carried_status: tuple[tuple[str, str], ...] = ()
        for clause_start, clause_end in _clauses(raw_text, is_row=is_row):
            clause = raw_text[clause_start:clause_end]
            subjects = find_subjects(clause)
            previous, previous_status = carried, carried_status
            carried = subjects[-1] if subjects else None
            carried_status = status_scope(clause, carried.start) if carried else ()
            hits = (
                _responsibility_hits(clause, own_division=division)
                + _electrical_hits(clause)
                + _rating_hits(clause)
            )
            if not hits:
                continue
            location = tuple(sorted(set(find_scope(clause)) | set(heading_scope)))
            pending: dict[tuple[str, str], _Pending] = {}
            for hit in hits:
                chosen, notes = _subjects_for(hit, subjects, is_row=is_row)
                source = SUBJECT_SOURCE_TEXT
                if not chosen and previous is not None:
                    chosen = [previous]
                    source = SUBJECT_SOURCE_PREVIOUS
                    notes = ("the subject is carried from the previous clause",)
                if not chosen:
                    if len({s.key for s in heading_subjects}) == 1:
                        chosen = [heading_subjects[0]]
                        source = SUBJECT_SOURCE_HEADING
                        notes = ("the subject is read from the element's heading",)
                    else:
                        extraction.unattributed_values += 1
                        continue
                for subject in chosen:
                    entry = pending.setdefault(
                        (subject.key, hit.attribute), _Pending(subject=subject, source=source)
                    )
                    entry.hits.append(hit)
                    entry.notes.extend(notes)
            for (subject_key, attribute), entry in pending.items():
                if len(extraction.facts) >= MAX_FACTS_PER_SPEC:
                    extraction.facts_over_limit += 1
                    continue
                hits_ = entry.hits
                category = hits_[0].category
                values = tuple(dict.fromkeys(v for hit in hits_ for v in hit.values))
                notes = list(entry.notes)
                for hit in hits_:
                    notes.extend(hit.notes)
                if len({v for v, _q in values}) > 1:
                    notes.append("the clause states several values of this attribute")
                if not entry.subject.is_tag:
                    notes.append("the subject is named, not tagged")
                if entry.source == SUBJECT_SOURCE_PREVIOUS:
                    status = tuple(sorted(set(previous_status) | set(heading_status)))
                elif entry.source == SUBJECT_SOURCE_HEADING:
                    status = heading_status
                else:
                    status = tuple(sorted(
                        set(status_scope(clause, entry.subject.start)) | set(heading_status)
                    ))
                start = clause_start + min(hit.start for hit in hits_)
                end = clause_start + max(hit.end for hit in hits_)
                passage, offset = _window(raw_text, start, end)
                raw_value = "; ".join(dict.fromkeys(hit.raw.strip() for hit in hits_))
                extraction.facts.append(CoordinationFact(
                    fact_id=_fact_id(
                        file_name, element_id, clause_start, category, attribute,
                        subject_key, raw_value,
                    ),
                    file_name=file_name,
                    element_id=element_id,
                    heading=heading[:200],
                    category=category,
                    attribute=attribute,
                    subject=subject_key,
                    subject_text=entry.subject.text,
                    subject_source=entry.source,
                    raw_value=raw_value,
                    values=values,
                    unit=hits_[0].unit,
                    scope=tuple(sorted(set(location) | set(status))),
                    passage=passage,
                    span=(start - offset, end - offset),
                    uncertainty=tuple(dict.fromkeys(notes)),
                    module_id=module_id,
                ))
    return extraction


def extract_all(specs: Iterable[Any], *, module_id: str = "") -> list[FactExtraction]:
    return [extract_facts(spec, module_id=module_id) for spec in specs]


# ---------------------------------------------------------------------------
# Comparing two facts
# ---------------------------------------------------------------------------


def _expanded_voltages(values: Iterable[tuple[Any, str]]) -> set[float]:
    out: set[float] = set()
    for value, _qualifier in values:
        number = float(value)
        out.add(number)
        nominal = UTILIZATION_TO_NOMINAL.get(int(round(number)))
        if nominal is not None and abs(number - round(number)) < 1e-9:
            out.add(float(nominal))
    return out


def _numbers_compatible(a: tuple[float, str], b: tuple[float, str], *, tolerance: float) -> bool:
    (va, qa), (vb, qb) = a, b
    va, vb = float(va), float(vb)
    slack = tolerance * max(abs(va), abs(vb))
    if qa == QUALIFIER_EXACT and qb == QUALIFIER_EXACT:
        return abs(va - vb) <= slack
    if qa == QUALIFIER_EXACT:
        va, qa, vb, qb = vb, qb, va, qa
    # Now ``a`` is a bound.
    if qb == QUALIFIER_EXACT:
        return vb >= va - slack if qa == QUALIFIER_MIN else vb <= va + slack
    if qa == qb:
        return True
    low, high = (va, vb) if qa == QUALIFIER_MIN else (vb, va)
    return low <= high + slack


def values_conflict(a: CoordinationFact, b: CoordinationFact) -> bool:
    """Whether two facts on one subject and attribute cannot both hold.

    Responsibility: the parties differ in kind-compatible terms (two
    divisions, two named contractors); a division and a contractor are not
    comparable without a mapping the documents do not give, so they never
    conflict here. Voltages: no value in common, after the utilization /
    nominal mapping. Everything else: no pair of values compatible, bounds
    included, with a 1% allowance only across a unit conversion.
    """
    if a.category == CATEGORY_RESPONSIBILITY:
        pa = {v for v, _q in a.values}
        pb = {v for v, _q in b.values}
        if pa & pb:
            return False
        kinds_a = {p.split(":", 1)[0] for p in pa}
        kinds_b = {p.split(":", 1)[0] for p in pb}
        return kinds_a == kinds_b and kinds_a <= {"division", "contractor"}
    if a.attribute.startswith(ATTR_VOLTAGE):
        return not (_expanded_voltages(a.values) & _expanded_voltages(b.values))
    tolerance = 0.01 if _raw_unit(a) != _raw_unit(b) else 0.0
    for va in a.values:
        for vb in b.values:
            if _numbers_compatible(va, vb, tolerance=tolerance):
                return False
    return True


def _raw_unit(fact: CoordinationFact) -> str:
    match = re.search(r"[A-Za-z/]+\s*$", fact.raw_value)
    return match.group(0).strip().lower() if match else ""


__all__ = [
    "ACTION_DESIGN",
    "ACTION_FURNISH",
    "ACTION_INSTALL",
    "ACTION_PROGRAM",
    "ACTION_TEST",
    "ACTION_WIRE",
    "CATEGORIES",
    "CATEGORY_ELECTRICAL",
    "CATEGORY_RATING",
    "CATEGORY_RESPONSIBILITY",
    "CoordinationFact",
    "FactExtraction",
    "MAX_FACTS_PER_SPEC",
    "PASSAGE_MAX_CHARS",
    "POLICY_VERSION",
    "SUBJECT_TERMS",
    "SubjectMatch",
    "SubjectTerm",
    "TAG_STOPLIST",
    "UTILIZATION_TO_NOMINAL",
    "extract_all",
    "extract_facts",
    "find_scope",
    "status_scope",
    "find_subjects",
    "own_division",
    "scope_conflict",
    "scope_one_sided",
    "subject_display",
    "values_conflict",
    "voltage_class",
]
