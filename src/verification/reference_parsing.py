"""Code and standard references read out of finding and source text (plan EX-04).

Two experiment modules read references the same way, so they share this one
reader: the evidence validator (:mod:`evidence_validation`) compares the
editions a claim names with the editions its evidence names, and the source
reuse prototype (:mod:`source_reuse`) keys retrieved passages by the reference
a claim cites. Neither is on by default, and nothing else in the app reads
this module.

What it recognizes is deliberately narrow — the designators the app's modules
pin or commonly cite:

* standards with a number: ``NFPA 13``, ``ASCE 7`` (also ``ASCE/SEI 7``),
  ``ASHRAE 62.1``, ``UL 300``, ``ULC S524``, ``CAN/ULC-S524``, ``ASTM E119``,
  ``ASME A17.1``, ``ICC A117.1``, ``IEEE 1584``, ``FM Global Data Sheet 2-0``;
* model and state codes by abbreviation: ``IBC`` / ``IFC`` / ``IMC`` /
  ``IPC`` / ``IECC`` / ``IEBC`` / ``IFGC`` / ``CBC`` / ``CFC`` / ``CMC`` /
  ``CPC`` / ``NEC`` / ``NBC`` / ``NFC``, and a few spelled out
  ("International Building Code", "California Fire Code").

``NEC`` is recorded as ``NFPA 70`` (it is that standard), so a claim citing one
and a source citing the other compare as the same reference. Anything else is
not read at all: an unrecognized reference yields nothing, never a guess.

An **edition** is a year attached to a designator in one of the forms codes and
standards are written in: ``NFPA 13-2022``, ``NFPA 13 (2022)``, ``NFPA 13,
2022 Edition``, ``2022 edition of NFPA 13``, ``2022 NFPA 13``, ``2021 IBC``,
``IBC 2021``, ``ASCE 7-16`` (two digits widened to ``2016``). A year standing
anywhere else in the text is not an edition of anything.

Stdlib only.
"""
from __future__ import annotations

import re

__all__ = [
    "canonical_designator",
    "designators",
    "editions",
    "section_numbers",
    "normalize_reference",
]

# Standards written as ORGANIZATION + number. The number may carry a letter
# prefix (ASTM E119, ASME A17.1, ICC A117.1) and dotted parts (62.1, 90.4). A
# dash never continues the number: in "NFPA 13-2022" and "ASCE 7-16" what
# follows the dash is the edition.
_NUMBERED = re.compile(
    r"\b(?P<org>NFPA|ASCE(?:\s*/\s*SEI)?|SEI\s*/\s*ASCE|ANSI\s*/\s*ASHRAE|ASHRAE|"
    r"CAN\s*/\s*ULC|ULC|UL|ASTM|ASME|ICC|IEEE)"
    r"[\s\-]*(?P<num>[A-Z]?\d+(?:\.\d+)*[A-Z]?)\b",
    re.IGNORECASE,
)
# FM Global data sheets are the one numbered family whose number has a dash
# ("Data Sheet 2-0", "DS 8-9").
_FM_DATA_SHEET = re.compile(
    r"\bFM(?:\s+Global)?\s+(?:Data\s+Sheet|DS)\s*(?P<num>\d+-\d+)\b",
    re.IGNORECASE,
)

# Codes written by abbreviation.
_CODE_ABBREVIATIONS = (
    "IBC", "IFC", "IMC", "IPC", "IECC", "IEBC", "IFGC", "IRC",
    "CBC", "CFC", "CMC", "CPC", "NEC", "NBC", "NFC", "NPC",
)
_ABBREVIATED = re.compile(r"\b(" + "|".join(_CODE_ABBREVIATIONS) + r")\b")

# A few codes spelled out, mapped to their abbreviation.
_SPELLED_OUT = {
    "international building code": "IBC",
    "international fire code": "IFC",
    "international mechanical code": "IMC",
    "international plumbing code": "IPC",
    "international energy conservation code": "IECC",
    "international existing building code": "IEBC",
    "international fuel gas code": "IFGC",
    "california building code": "CBC",
    "california fire code": "CFC",
    "california mechanical code": "CMC",
    "california plumbing code": "CPC",
    "national electrical code": "NFPA 70",
    "national building code of canada": "NBC",
    "national fire code of canada": "NFC",
}
_SPELLED = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in _SPELLED_OUT) + r")\b",
    re.IGNORECASE,
)

_YEAR = r"(?P<year>(?:19|20)\d{2})"
_EDITION_AFTER = re.compile(
    # "-2022", " (2022)", ", 2022 Edition", " 2022 edition", ": 2022", " 2022".
    r"^\s*(?:[-–—:,]\s*|\(\s*|\s)(?:\(\s*)?" + _YEAR + r"(?!\d)",
)
_TWO_DIGIT_AFTER = re.compile(r"^\s*[-–—]\s*(?P<yy>\d{2})(?![\d.])")
_EDITION_BEFORE = re.compile(
    # "2022 edition of ", "2022 ", "the 2022 ".
    _YEAR + r"\s+(?:edition\s+(?:of\s+)?(?:the\s+)?)?$",
    re.IGNORECASE,
)

# Any four-digit year that is not part of a longer or dotted number.
_LOOSE_YEAR = re.compile(r"(?<![\d.§])(?:19|20)\d{2}(?![\d.]\d)")

# A section number: two or more dotted numeric parts ("8.15.1", "903.2"),
# optionally after "Section" / "§". A bare integer is never read as one.
# A number that starts with 0, or that is followed by a unit, is a quantity
# ("0.10 gpm/ft2", "2.5 in."), not a section.
_SECTION = re.compile(
    r"(?:§\s*|\bsection\s+)?(?<![\d.])([1-9]\d*(?:\.\d+)+)\b"
    r"(?!\s*(?:%|\"|'|in\b|in\.|inch|ft\b|ft\.|feet|foot|psi|gpm|cfm|mm\b|cm\b|m\b|kpa|bar\b|"
    r"°|deg|hr\b|hour|min\b|minute|sq|lb|kg|kw|v\b|volt|amp|hz\b))",
    re.IGNORECASE,
)


def _widen(yy: str) -> int:
    value = int(yy)
    return 2000 + value if value <= 40 else 1900 + value


def canonical_designator(org: str, number: str = "") -> str:
    """The canonical spelling of one designator (``"NFPA 13"``, ``"ASCE 7"``)."""
    org_key = re.sub(r"\s+", " ", org.strip().upper())
    org_key = re.sub(r"\s*/\s*", "/", org_key)
    if org_key in ("ASCE/SEI", "SEI/ASCE"):
        org_key = "ASCE"
    elif org_key == "ANSI/ASHRAE":
        org_key = "ASHRAE"
    elif org_key == "CAN/ULC":
        org_key = "ULC"
    number = number.strip().upper()
    if org_key == "NEC" and not number:
        return "NFPA 70"
    return f"{org_key} {number}".strip()


def _matches(text: str) -> list[tuple[int, int, str]]:
    """Every designator in ``text`` as ``(start, end, canonical)``, in order."""
    found: list[tuple[int, int, str]] = []
    if not text:
        return found
    for m in _FM_DATA_SHEET.finditer(text):
        found.append((m.start(), m.end(), f"FM DS {m.group('num')}"))
    for m in _NUMBERED.finditer(text):
        if any(s <= m.start() < e for s, e, _ in found):
            continue
        found.append((m.start(), m.end(), canonical_designator(m.group("org"), m.group("num"))))
    for m in _ABBREVIATED.finditer(text):
        if any(s <= m.start() < e for s, e, _ in found):
            continue
        found.append((m.start(), m.end(), canonical_designator(m.group(1))))
    for m in _SPELLED.finditer(text):
        if any(s <= m.start() < e for s, e, _ in found):
            continue
        found.append((m.start(), m.end(), _SPELLED_OUT[m.group(1).lower()]))
    found.sort()
    return found


def designators(text: str | None) -> list[str]:
    """The canonical designators ``text`` names, first appearance order, unique."""
    seen: list[str] = []
    for _, _, name in _matches(text or ""):
        if name not in seen:
            seen.append(name)
    return seen


def editions(text: str | None, *, loose: bool = False) -> dict[str, set[int]]:
    """``{designator: {years}}`` for every edition attached to a designator.

    A designator named without an edition is absent from the result.

    **Strict** (the default) reads only a year written as part of the
    reference (``NFPA 13-2022``, ``2022 edition of NFPA 13``). The validator
    reads evidence this way: a source's edition is what its text calls it.

    **Loose** also attaches every other year after the designator, up to the
    next designator (and, for the first one, any year before it), as in "NFPA
    72-2019 is stale; the current edition is 2022". The validator reads the
    *claim* this way and compares by overlap, so a year read too generously on
    the claim side can only remove a concern, never raise one.
    """
    out: dict[str, set[int]] = {}
    text = text or ""
    matches = _matches(text)
    for index, (start, end, name) in enumerate(matches):
        years: set[int] = set()
        after = text[end:end + 24]
        m = _EDITION_AFTER.match(after)
        if m:
            years.add(int(m.group("year")))
        else:
            m2 = _TWO_DIGIT_AFTER.match(after)
            if m2 and (name.startswith("ASCE") or name.startswith("ASTM")):
                years.add(_widen(m2.group("yy")))
        before = text[max(0, start - 32):start]
        mb = _EDITION_BEFORE.search(before)
        if mb:
            years.add(int(mb.group("year")))
        if loose:
            stop = matches[index + 1][0] if index + 1 < len(matches) else len(text)
            window = text[end:stop]
            if index == 0:
                window = text[:start] + " " + window
            years.update(int(y.group(0)) for y in _LOOSE_YEAR.finditer(window))
        if years:
            out.setdefault(name, set()).update(years)
    return out


def section_numbers(text: str | None) -> list[str]:
    """Dotted section numbers in ``text`` (``"8.15.1"``), in order, unique.

    A dotted number that is part of a designator (``ASHRAE 62.1``,
    ``ASME A17.1``) is not a section number.
    """
    text = text or ""
    spans = [(s, e) for s, e, _ in _matches(text)]
    out: list[str] = []
    for m in _SECTION.finditer(text):
        if any(s <= m.start(1) < e for s, e in spans):
            continue
        value = m.group(1)
        if value not in out:
            out.append(value)
    return out


def normalize_reference(text: str | None) -> tuple[str, ...]:
    """A code reference reduced to its designators and section numbers.

    Editions are dropped (a reuse key carries the edition separately);
    order and duplicates do not matter. ``()`` when nothing is recognized.
    """
    names = designators(text)
    if not names:
        return ()
    sections = section_numbers(text)
    return tuple(sorted(set(names))) + tuple(sorted(set(f"§{s}" for s in sections)))
