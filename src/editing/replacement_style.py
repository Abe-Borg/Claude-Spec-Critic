"""Document-style profiling and replacement-text normalization.

Phase 1 / Step 1.1 of the auto-apply quality plan.

The auto-apply pipeline used to land the model's replacement text in
the source document verbatim. Claude routinely emits curly quotes
(``"…"``), em-dashes (``—``), and Unicode apostrophes (``'``);
most CSI spec templates use straight quotes, hyphens, and ASCII
apostrophes consistently. Verbatim insertion produced sentences that
looked visibly different from their neighbors.

This module fixes that with two pure functions:

* ``profile_document_style(texts)`` runs a majority vote across a
  sample of the source document's text to decide which typographic
  conventions the document uses (quotes, dashes, apostrophes, NBSP
  in measurements). Empty samples default to ASCII/straight — the
  most common CSI template convention — so the legacy no-op path is
  preserved for documents the profiler cannot classify.

* ``normalize_replacement_text(text, profile)`` rewrites a single
  replacement string to match the profile. The rewrite is
  conservative: only characters with unambiguous mappings are
  touched (curly ↔ straight quotes, em-dash → hyphen, NBSP in
  well-known unit phrases). The rewrite is idempotent, and a
  ``None`` profile or empty text is a no-op so callers that have not
  computed a profile yet get the legacy passthrough behavior.

The env-var kill switch ``SPEC_CRITIC_NORMALIZE_REPLACEMENT_STYLE``
lets operators revert to the legacy verbatim behavior without a
redeploy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import os
import re


# ---------------------------------------------------------------------------
# Env-var kill switch
# ---------------------------------------------------------------------------


_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def normalize_replacement_style_enabled() -> bool:
    """Whether replacement-text style normalization runs.

    Default enabled. Set ``SPEC_CRITIC_NORMALIZE_REPLACEMENT_STYLE=0``
    (or false/no/off, case-insensitive) to keep the model's original
    replacement text verbatim.
    """
    raw = os.environ.get("SPEC_CRITIC_NORMALIZE_REPLACEMENT_STYLE")
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLE_TOKENS


def restore_known_formatting_enabled() -> bool:
    """Whether known-pattern bold restoration runs after a partial EDIT.

    Phase 3 / Step 3.2. Default **off** so the feature ships dormant —
    a wrong match could bold something that shouldn't be bold (e.g.,
    a token that happens to look like a standards reference but
    appears inside an ordinary sentence), and the cost of that is
    visibly off output. Operators flip
    ``SPEC_CRITIC_RESTORE_KNOWN_FORMATTING=1`` once they've validated
    the pattern registry against their workflow.
    """
    raw = os.environ.get("SPEC_CRITIC_RESTORE_KNOWN_FORMATTING")
    if raw is None:
        return False
    return raw.strip().lower() not in _DISABLE_TOKENS


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentStyleProfile:
    """Per-document typographic conventions.

    Each flag is a majority vote across the source document's text.
    Empty documents default to ASCII/straight per the most common CSI
    template convention; this keeps Step 1.1 behavior identical to the
    legacy (no-normalization) path for documents the profiler cannot
    classify.
    """

    prefers_straight_quotes: bool = True
    prefers_hyphen_dash: bool = True
    prefers_ascii_apostrophe: bool = True
    uses_nbsp_in_measurements: bool = False


_CURLY_DOUBLE = ("“", "”")  # LEFT/RIGHT DOUBLE QUOTATION MARK
_CURLY_SINGLE = ("‘", "’")  # LEFT/RIGHT SINGLE QUOTATION MARK

# Hyphen between two word characters: counts in-word hyphenated tokens
# ("R-454B", "fire-rated", "60-80"). Same for em/en dash. Standalone
# spaced hyphens are intentionally not counted since "Item A - Item B"
# can render either way without changing meaning.
_HYPHEN_BETWEEN_WORDS = re.compile(r"(?<=\w)-(?=\w)")
_DASH_BETWEEN_WORDS = re.compile(r"(?<=\w)[–—](?=\w)")

# Unit tokens we recognize as measurements. Kept conservative — adding
# every unit ever used in a spec is a footgun (random words like "m"
# would over-trigger NBSP insertion). The list covers the common
# mechanical / plumbing units.
_UNIT_TOKENS = r"in|ft|mm|cm|m|psi|gpm|cfm|hp|kW|°F|°C|°"
_MEASUREMENT_RE = re.compile(
    rf"\d[ \s]*(?:{_UNIT_TOKENS})\b", flags=re.IGNORECASE
)


def profile_document_style(texts: Iterable[str]) -> DocumentStyleProfile:
    """Build a :class:`DocumentStyleProfile` from a sample of document text.

    ``texts`` is any iterable of strings; the typical caller passes the
    paragraph_map's ``.text`` fields. Counts are accumulated across all
    strings, so table cells, headers, and footers contribute on equal
    footing to body paragraphs.

    Ties go to ASCII/straight to preserve the legacy passthrough
    behavior for ambiguous documents.
    """
    n_straight_double = 0
    n_curly_double = 0
    n_straight_single = 0
    n_curly_single = 0
    n_hyphen_word = 0
    n_dash_word = 0
    n_measurements = 0
    n_measurements_nbsp = 0

    for text in texts:
        if not text:
            continue
        n_straight_double += text.count('"')
        for ch in _CURLY_DOUBLE:
            n_curly_double += text.count(ch)
        n_straight_single += text.count("'")
        for ch in _CURLY_SINGLE:
            n_curly_single += text.count(ch)
        n_hyphen_word += len(_HYPHEN_BETWEEN_WORDS.findall(text))
        n_dash_word += len(_DASH_BETWEEN_WORDS.findall(text))
        for match in _MEASUREMENT_RE.finditer(text):
            n_measurements += 1
            if " " in match.group(0):
                n_measurements_nbsp += 1

    return DocumentStyleProfile(
        prefers_straight_quotes=(n_straight_double >= n_curly_double),
        prefers_ascii_apostrophe=(n_straight_single >= n_curly_single),
        prefers_hyphen_dash=(n_hyphen_word >= n_dash_word),
        # Require both a non-zero measurement count AND a majority of
        # those measurements to actually use NBSP — a single stray NBSP
        # in a long doc otherwise flips the preference.
        uses_nbsp_in_measurements=(
            n_measurements > 0
            and n_measurements_nbsp * 2 >= n_measurements
        ),
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _to_straight_quotes(text: str) -> str:
    """Replace curly double quotes with ASCII straight."""
    return text.replace("“", '"').replace("”", '"')


def _to_ascii_apostrophe(text: str) -> str:
    """Replace curly single quotes / apostrophes with ASCII straight."""
    return text.replace("‘", "'").replace("’", "'")


def _to_hyphen(text: str) -> str:
    """Replace ' — '/' – ' (em/en dash with surrounding spaces) with ' - '.

    Bare em/en dashes between word characters (page ranges like
    ``12—15``) are left alone — those are deliberate typographic
    choices that do not have a clean ASCII equivalent.
    """
    return text.replace(" — ", " - ").replace(" – ", " - ")


def _straight_to_curly_double(text: str) -> str:
    """Convert ASCII ``"`` to curly opening/closing based on context.

    Opening (“) follows whitespace, start-of-text, or an opening
    bracket. Closing (”) follows anything else (typically an
    alphanumeric or punctuation).
    """
    out: list[str] = []
    for idx, ch in enumerate(text):
        if ch == '"':
            prev = text[idx - 1] if idx > 0 else ""
            if not prev or prev.isspace() or prev in "([{<":
                out.append("“")
            else:
                out.append("”")
        else:
            out.append(ch)
    return "".join(out)


def _straight_to_curly_single(text: str) -> str:
    """Convert ASCII ``'`` to curly opening / closing / apostrophe.

    Internal apostrophes (``don't``, ``engineer's``) take ’ — the
    RIGHT SINGLE QUOTATION MARK doubles as the typographic apostrophe.
    Otherwise the same position rule as double quotes applies.
    """
    out: list[str] = []
    for idx, ch in enumerate(text):
        if ch == "'":
            prev = text[idx - 1] if idx > 0 else ""
            nxt = text[idx + 1] if idx + 1 < len(text) else ""
            if prev.isalpha() and nxt.isalpha():
                out.append("’")
            elif not prev or prev.isspace() or prev in "([{<":
                out.append("‘")
            else:
                out.append("’")
        else:
            out.append(ch)
    return "".join(out)


def _insert_nbsp_in_measurements(text: str) -> str:
    """Replace the space inside ``<digit> <unit>`` phrases with NBSP."""
    def _swap(match: re.Match) -> str:
        return match.group(0).replace(" ", " ")

    return _MEASUREMENT_RE.sub(_swap, text)


def normalize_replacement_text(
    text: str, profile: DocumentStyleProfile | None
) -> tuple[str, bool]:
    """Normalize ``text`` to match the document's typographic conventions.

    Returns ``(normalized_text, changed)``. ``changed`` is True iff the
    output differs from the input — callers bump the "replacement
    normalized" diagnostics counter when this flips.

    A ``None`` profile is a no-op (returns input unchanged) so callers
    that don't have a profile yet — or operators who disable
    normalization via env var — get the legacy passthrough behavior.
    """
    if not text or profile is None:
        return text, False

    out = text
    if profile.prefers_straight_quotes:
        out = _to_straight_quotes(out)
    else:
        out = _straight_to_curly_double(out)

    if profile.prefers_ascii_apostrophe:
        out = _to_ascii_apostrophe(out)
    else:
        out = _straight_to_curly_single(out)

    if profile.prefers_hyphen_dash:
        out = _to_hyphen(out)

    if profile.uses_nbsp_in_measurements:
        out = _insert_nbsp_in_measurements(out)

    return out, out != text


# ---------------------------------------------------------------------------
# Known-pattern formatting restoration (Phase 3 / Step 3.2)
# ---------------------------------------------------------------------------
#
# When a partial-replacement EDIT crosses runs with distinct formatting,
# ``spec_editor._replace_in_paragraph`` collapses the affected runs into
# the first run's formatting. Bold/italic markup on tokens inside the
# replacement span is silently lost. The classic shape is a standards
# reference rendered as bold ``NFPA 13`` inside otherwise-normal text —
# after a sentence rewrite the bold token reads as plain prose.
#
# The restoration pass scans the post-mutation replacement span for
# tokens matching a small registry of recognized references and re-
# applies bold formatting to each match. The registry intentionally
# stays conservative: every pattern requires a literal organization /
# code identifier plus a number, so an arbitrary "Section 5" in prose
# does not over-trigger. Add new entries here when a real workflow
# proves the new pattern is unambiguous in spec documents.

# Patterns are compiled with ``re.IGNORECASE`` so ``CBC 2025`` /
# ``cbc 2025`` / ``Cbc 2025`` all match. Word boundaries (``\b``) keep
# substrings inside larger tokens from triggering.
KNOWN_BOLD_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Standards organizations followed by a numeric code and optional
    # suffix (``NFPA 13``, ``ASCE 7-22``, ``ASTM A53-22``,
    # ``IAPMO PS 117``, ``UL 1479``). The trailing ``(?:-[\w\d]+)?``
    # captures the year/revision suffix common in spec references.
    re.compile(
        r"\b(?:NFPA|ASCE|ASHRAE|IAPMO|ASTM|ANSI|UL|API|AWWA|AISC|ICC)"
        r"\s+(?:[A-Z]\s+)?\d+(?:[-\.]\w+)*\b",
        flags=re.IGNORECASE,
    ),
    # California codes plus year (``CBC 2025``, ``CMC 2025``,
    # ``CalGreen 2025``) or section reference (``CBC § 5.7.2``,
    # ``CMC 1003.2``). Section pattern requires either ``§`` or a
    # dotted decimal so bare ``CBC code`` does not match.
    re.compile(
        r"\b(?:CBC|CMC|CPC|CEC|CFC|CALGREEN)\s+"
        r"(?:\d{4}|§\s*[\d\.]+|\d+(?:\.\d+)+)\b",
        flags=re.IGNORECASE,
    ),
    # CSI section number (``Section 23 21 13`` — three two-digit
    # groups). Always six digits, always grouped in pairs.
    re.compile(r"\bSection\s+\d{2}\s+\d{2}\s+\d{2}\b", flags=re.IGNORECASE),
)


def known_pattern_spans(text: str) -> list[tuple[int, int]]:
    """Return non-overlapping ``(start, end)`` ranges of recognized references.

    Walks every compiled pattern in :data:`KNOWN_BOLD_PATTERNS`,
    collects ``(start, end)`` for each match, then sorts and merges
    overlapping / adjacent ranges so the caller never has to handle
    two patterns reporting the same token. Returns ``[]`` for empty
    input.
    """
    if not text:
        return []
    raw: list[tuple[int, int]] = []
    for pattern in KNOWN_BOLD_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if span[1] > span[0]:
                raw.append(span)
    if not raw:
        return []
    raw.sort()
    merged: list[tuple[int, int]] = []
    for start, end in raw:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
