"""A specification's CSI section identity, read by one rule (plan WP-05).

A routed program sends each specification to the review module(s) for its
discipline, and the strongest signal it has is the CSI section number:
Division 21 is fire suppression, Division 26 electrical, and so on. A number
can be read from three places, and they do not deserve the same trust:

* **The document's own SECTION heading** — ``SECTION 21 05 00`` above a title
  line, or MasterSpec's one-line ``SECTION 211313 - WET-PIPE SPRINKLER
  SYSTEMS``. It is what the author says the document is. The extractor reads
  it once (:func:`read_section_heading`) and carries it on
  ``ExtractedSpec.section_heading`` with the element ids it came from.
* **A caller's metadata or a title** — a labeled number anywhere
  (``... SECTION 21 13 13 ...``) or a separated number leading the text
  (``21 05 00 - Common Work Results ...``).
* **The file name** — the same two forms, and the compact leading form
  ``210500.docx``, which is credible only when the document's heading
  confirms it: six leading digits are as often a date (``240105``) or a
  project number as a section.

The heading is read only from the opening of the body and only from
heading-shaped text. ``See Section 21 13 13`` in a Related Sections paragraph
names *another* document; so does ``Section 21 13 13 applies to ...``, which
is a sentence. See :func:`read_section_heading` for the exact rules.

This module is pure and stdlib-only. The extractor (which reads the heading)
and the router (which reads file names, titles, and metadata) both use it, so
there is one rule for what a section number is, not two that can drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# How a number was written. The form decides how much a number is trusted.
#: ``SECTION 21 05 00`` / ``SECTION 210500`` — the word SECTION says it is one.
FORM_LABELED = "labeled"
#: ``21 05 00`` / ``21-05-00`` / ``21.05.00`` leading a title or file name.
FORM_SEPARATED = "separated"
#: ``210500`` leading a file name. Needs the document's heading to confirm it.
FORM_COMPACT = "compact"
#: A caller's dedicated section-number field: ``"21"``, ``"2113"``, or
#: ``"211313"`` — the caller has already said the value is a section number.
FORM_DEDICATED = "dedicated"

#: How many non-empty entries at the start of the body are searched for the
#: SECTION heading. A title block (project name, number, issue date) sits
#: above the heading in many templates; a heading further down than this is
#: not the document's identity line.
OPENING_REGION_ENTRIES: int = 12

# One separator between the two-digit groups of a separated number.
_SEP = r"(?:\s*[-._]\s*|\s+)"
_SEPARATED_GROUPS = rf"(\d{{2}}){_SEP}(\d{{2}}){_SEP}(\d{{2}})"
_COMPACT_GROUPS = r"(\d{2})(\d{2})(\d{2})"

# The word SECTION, then a separated or compact six-digit number. Searched
# anywhere in a title or file name: the label is what makes it metadata.
_LABELED_RE = re.compile(
    rf"\bSECTION[\s_-]+(?:{_SEPARATED_GROUPS}|{_COMPACT_GROUPS})(?!\d)",
    re.IGNORECASE,
)
# A separated number at the very start of a title or file name. Unanchored,
# the old router read ``NFPA 13`` as Division 13 and dates as sections.
_LEADING_SEPARATED_RE = re.compile(rf"^\s*{_SEPARATED_GROUPS}(?!\d)")
# Six digits at the very start of a file name, not glued to more letters or
# digits (``210500.docx``, ``210500 - Title.docx``; not ``210500a`` or a
# seven-digit project number).
_LEADING_COMPACT_RE = re.compile(rf"^\s*{_COMPACT_GROUPS}(?![0-9A-Za-z])")
# A dedicated field may hold a division, a division and family, or all six.
_DEDICATED_RE = re.compile(
    r"^\s*(?:(\d{2})|(\d{2})(\d{2})|(\d{2})(\d{2})(\d{2}))\s*$"
)


@dataclass(frozen=True)
class CsiNumber:
    """A CSI section number and the form it was written in.

    ``parts`` holds one to three two-digit groups (a dedicated field may give
    only the division); ``canonical`` joins them with spaces (``"21 05 00"``).
    """

    parts: tuple[str, ...]
    form: str

    @property
    def canonical(self) -> str:
        return " ".join(self.parts)

    @property
    def division(self) -> str:
        return self.parts[0]

    def agrees_with(self, other: "CsiNumber") -> bool:
        """True when the two name the same section: equal, or one gives only
        the leading groups of the other (a dedicated ``"21"`` and a heading's
        ``21 13 13`` do not disagree)."""
        shorter, longer = sorted((self.parts, other.parts), key=len)
        return longer[: len(shorter)] == shorter


def _groups(match: re.Match[str] | None) -> tuple[str, ...]:
    if match is None:
        return ()
    return tuple(group for group in match.groups() if group is not None)


def labeled_number(text: str) -> CsiNumber | None:
    """``SECTION 21 05 00`` or ``SECTION 210500`` anywhere in ``text``."""
    parts = _groups(_LABELED_RE.search(text or ""))
    return CsiNumber(parts, FORM_LABELED) if parts else None


def title_number(text: str) -> CsiNumber | None:
    """The section number a title or metadata string credibly carries.

    A labeled number anywhere, else a separated number at the start. A
    compact number in a title is not read: without a label it is as likely
    to be a date or a project number.
    """
    labeled = labeled_number(text)
    if labeled is not None:
        return labeled
    parts = _groups(_LEADING_SEPARATED_RE.match(text or ""))
    return CsiNumber(parts, FORM_SEPARATED) if parts else None


def filename_number(filename: str) -> CsiNumber | None:
    """The section number a file name carries, in the form it was written.

    As :func:`title_number`, plus a compact leading number
    (:data:`FORM_COMPACT`), which the caller must treat as a claim to be
    confirmed by the document's own heading, never as identity on its own.
    """
    number = title_number(filename)
    if number is not None:
        return number
    parts = _groups(_LEADING_COMPACT_RE.match(filename or ""))
    return CsiNumber(parts, FORM_COMPACT) if parts else None


def dedicated_number(text: str) -> CsiNumber | None:
    """The number in a caller's dedicated section-number field.

    Everything :func:`title_number` accepts, plus a bare compact value of
    two, four, or six digits: the caller has already identified the field
    as a section number, which is exactly what a compact file name lacks.
    """
    number = title_number(text)
    if number is not None:
        return number
    parts = _groups(_DEDICATED_RE.fullmatch(text or ""))
    return CsiNumber(parts, FORM_DEDICATED) if parts else None


# ---------------------------------------------------------------------------
# The document's own SECTION heading
# ---------------------------------------------------------------------------

#: Where the heading reads from: the document's own opening.
SOURCE_SECTION_HEADING = "section_heading"


@dataclass(frozen=True)
class SectionHeading:
    """The document's own SECTION heading, and where it was read.

    Attributes:
        number: The canonical section number, ``"21 05 00"``.
        title: The section title (``"COMMON WORK RESULTS FOR FIRE
            SUPPRESSION"``), from the heading line or the paragraph right
            after it; ``""`` when the heading has none.
        number_text: The heading paragraph as written
            (``"SECTION 211313 - WET-PIPE SPRINKLER SYSTEMS"``).
        number_element_id: The extractor's element id of that paragraph
            (``"p0"``, or ``"cc0p0"`` inside a content control).
        title_element_id: The element id the title came from: the same
            paragraph for a one-line heading, the next one otherwise, ``""``
            without a title.
    """

    number: str
    title: str
    number_text: str
    number_element_id: str
    title_element_id: str = ""

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.number.split())

    @property
    def csi_number(self) -> CsiNumber:
        return CsiNumber(self.parts, FORM_LABELED)

    @property
    def location(self) -> str:
        """The element id(s) the heading was read from: ``"p0"`` or
        ``"p0-p1"`` when the title is the next paragraph."""
        if self.title_element_id and self.title_element_id != self.number_element_id:
            return f"{self.number_element_id}-{self.title_element_id}"
        return self.number_element_id

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "number_text": self.number_text,
            "number_element_id": self.number_element_id,
            "title_element_id": self.title_element_id,
        }


# A heading paragraph: the word SECTION first, then the number (optionally a
# ``.NN`` level-four suffix), then nothing or the title. Nothing may precede
# SECTION — "See Section 21 13 13" is a reference, not a heading.
_HEADING_LINE_RE = re.compile(
    rf"^\s*SECTION[\s_-]+(?:{_SEPARATED_GROUPS}|{_COMPACT_GROUPS})"
    r"(?:\.\d{2})?(?![0-9A-Za-z])(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
# What may stand between the number and a title on the same line.
_TITLE_LEAD_RE = re.compile(r"^[\s\-\u2013\u2014:.]+")
_TITLE_MAX_CHARS = 120
# A requirement verb makes a line a sentence, whatever its capitalization.
_REQUIREMENT_RE = re.compile(r"\b(?:shall|must)\b", re.IGNORECASE)

# Lines that mean the opening is over: the specification body has begun, or
# a list of *other* sections is starting. A heading below one of these is
# not the document's identity line. An article number must be followed by a
# word ("1.01 SUMMARY"), so a date line such as "07.20.2026" in a title block
# does not end the opening early.
_OPENING_ENDS_RE = re.compile(
    r"^\s*(?:"
    r"PART\s+(?:\d+|ONE|TWO|THREE)\b"  # PART 1 - GENERAL
    r"|\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\s+[A-Za-z]"  # 1.01 SUMMARY
    r"|END\s+OF\s+SECTION\b"
    r"|RELATED\s+(?:SECTIONS|REQUIREMENTS|DOCUMENTS|WORK)\b"
    r")",
    re.IGNORECASE,
)


def _title_shaped(text: str) -> bool:
    """True when ``text`` reads as a section title rather than prose.

    The same principles as the structural checks' heading titles
    (``preprocessor._heading_title_shaped``): a capital first letter, no
    ``shall`` / ``must``, not a mixed-case sentence ending in a period, not a
    table row, at most 120 characters. It must also not be the start of
    something else (a PART or article line, another SECTION line).
    """
    if not text or len(text) > _TITLE_MAX_CHARS or "|" in text:
        return False
    letters = [ch for ch in text if ch.isalpha()]
    if not letters or letters[0].islower():
        return False
    if _REQUIREMENT_RE.search(text):
        return False
    if text.endswith((".", "!", "?")) and any(ch.islower() for ch in letters):
        return False
    if _OPENING_ENDS_RE.match(text) or _HEADING_LINE_RE.match(text):
        return False
    return True


def _heading_line(text: str) -> tuple[tuple[str, ...], str] | None:
    """``(parts, same-line title)`` when ``text`` is a SECTION heading line.

    ``None`` for anything else, including a line that starts with a SECTION
    number and continues as prose (``Section 21 13 13 applies to ...``).
    """
    match = _HEADING_LINE_RE.match(text)
    if match is None:
        return None
    parts = tuple(
        group
        for group in match.groups()[:6]
        if group is not None
    )
    rest = _TITLE_LEAD_RE.sub("", match.group("rest") or "").strip()
    if rest and not _title_shaped(rest):
        return None
    return parts, rest


def is_section_heading_line(text: str) -> bool:
    """Whether one paragraph is a heading-shaped SECTION line (the rules of
    :func:`read_section_heading`, without its opening-region bound). The
    extractor's section attribution starts a section at one."""
    return _heading_line(text or "") is not None


def read_section_heading(entries: Iterable[object]) -> SectionHeading | None:
    """The document's own SECTION heading, or ``None`` when it has none.

    ``entries`` is the extractor's paragraph map in document order (any
    objects with ``element_id``, ``element_type``, and ``text``). The rules:

    * **Opening region.** Only the start of the body is read: at most
      :data:`OPENING_REGION_ENTRIES` non-empty entries, ending early at the
      first PART heading, article heading (``1.01 SUMMARY``),
      ``END OF SECTION``, or ``RELATED SECTIONS`` line, and at the
      supplemental blocks the extractor appends after the body (text boxes,
      notes, headers and footers — a page header is not the document's
      identity line). Table rows count toward the bound but are never read
      as the heading.
    * **Heading-shaped.** A paragraph that *starts* with ``SECTION`` and a
      six-digit number (separated or compact, optionally ``.NN``), followed
      by nothing or by a title-shaped remainder. ``See Section 21 13 13`` and
      ``Section 21 13 13 applies to ...`` are not headings.
    * **Title.** From the same line, else the immediately following
      paragraph when it is title-shaped.
    * **One identity.** When the opening holds SECTION headings with
      different numbers, the document does not say what it is, and ``None``
      is returned rather than a guess.
    """
    region: list[tuple[str, str, str]] = []
    for entry in entries:
        element_type = str(getattr(entry, "element_type", "") or "")
        if element_type == "meta":
            break  # the supplemental blocks begin; the body is over
        text = str(getattr(entry, "text", "") or "").strip()
        if not text:
            continue
        if _OPENING_ENDS_RE.match(text):
            break
        region.append((str(getattr(entry, "element_id", "") or ""), element_type, text))
        if len(region) >= OPENING_REGION_ENTRIES:
            break

    found: SectionHeading | None = None
    for index, (element_id, element_type, text) in enumerate(region):
        if element_type != "paragraph":
            continue
        parsed = _heading_line(text)
        if parsed is None:
            continue
        parts, title = parsed
        number = " ".join(parts)
        if found is not None:
            if number != found.number:
                return None  # two identities in one opening
            continue
        title_element_id = element_id if title else ""
        if not title and index + 1 < len(region):
            next_id, next_type, next_text = region[index + 1]
            if next_type == "paragraph" and _title_shaped(next_text):
                title, title_element_id = next_text, next_id
        found = SectionHeading(
            number=number,
            title=title,
            number_text=text,
            number_element_id=element_id,
            title_element_id=title_element_id,
        )
    return found


__all__ = [
    "FORM_COMPACT",
    "FORM_DEDICATED",
    "FORM_LABELED",
    "FORM_SEPARATED",
    "OPENING_REGION_ENTRIES",
    "SOURCE_SECTION_HEADING",
    "CsiNumber",
    "SectionHeading",
    "dedicated_number",
    "filename_number",
    "is_section_heading_line",
    "labeled_number",
    "read_section_heading",
    "title_number",
]
