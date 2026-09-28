"""What a PART or article heading line looks like — one rule (plan WP-04A, WP-03).

A paragraph is a heading only when its number AND its title are
heading-shaped:

* The number is ``PART n`` (level 0) or a dotted CSI article number:
  ``1.01`` / ``1.1`` (level 1) or ``1.01.1`` (level 2). A bare integer is
  never a heading number. "2 coats of primer", "12 inches minimum" and
  "1 year from Substantial Completion" are quantities, and a SectionFormat
  PART heading always carries the word PART.
* The title reads as a title, not as the rest of a sentence: its first
  letter is a capital ("1.5 inches minimum cover" continues a sentence), it
  has no ``shall`` / ``must`` (a requirement is prose even in capitals), it
  does not end a mixed-case sentence with a period, it is not a table row
  (the extractor joins cells with " | "), and it is at most 120 characters.

The structure checks (``preprocessor.heading_candidates``) read every heading
of a document with :data:`HEADING_LINE_RE`; the extractor's section
attribution reads one paragraph at a time with :func:`is_heading_line`. Both
see a paragraph as Word displays it, automatic number included (plan WP-03),
so a heading Word numbers is a heading to both. The module is pure and
stdlib-only so the extractor can use it without importing the detectors.
"""
from __future__ import annotations

import re

# A numbered line at the start of a paragraph (preceded by the paragraph
# delimiter "\n\n" or the start of the text). The number and title must be in
# the same paragraph (a line break between them is allowed, a paragraph break
# is not), and the title ends at the end of its line; ``heading_title_shaped``
# then decides whether the line is a heading.
HEADING_LINE_RE = re.compile(
    r"(?:^|\n\n)\s*"
    r"(?P<num>PART[^\S\n]+\d+|\d+(?:\.\d+){1,2})"
    r"(?:[^\S\n]|\n(?!\n))+"
    r"(?P<title>[^\n]+)",
    flags=re.IGNORECASE,
)

HEADING_TITLE_MAX_CHARS: int = 120

# A requirement verb makes a line a sentence, whatever its capitalization.
_REQUIREMENT_IN_TITLE_RE = re.compile(r"\b(?:shall|must)\b", flags=re.IGNORECASE)


def heading_title_shaped(title: str) -> bool:
    """True when ``title`` reads as a heading title rather than prose."""
    if not title or len(title) > HEADING_TITLE_MAX_CHARS or "|" in title:
        return False
    letters = [ch for ch in title if ch.isalpha()]
    if not letters or letters[0].islower():
        return False
    if _REQUIREMENT_IN_TITLE_RE.search(title):
        return False
    if title.endswith((".", "!", "?")) and any(ch.islower() for ch in letters):
        return False
    return True


def is_heading_line(text: str) -> bool:
    """Whether one paragraph's text is a PART or article heading."""
    match = HEADING_LINE_RE.match(text or "")
    if match is None:
        return False
    title = match.group("title").strip()
    return heading_title_shaped(title) and bool(title.rstrip(":").strip())


__all__ = [
    "HEADING_LINE_RE",
    "HEADING_TITLE_MAX_CHARS",
    "heading_title_shaped",
    "is_heading_line",
]
