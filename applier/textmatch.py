"""Whitespace-tolerant text matching with raw-offset recovery.

The text Spec Critic reviewed is not byte-identical to the text stored in the
document's runs. The extractor strips each paragraph, joins table-row cells
with ``" | "``, and reads the *accept-all-changes* view of any tracked
revision. Word itself splits a sentence across runs at every formatting
boundary and at spell-check boundaries that have nothing to do with meaning.

So an exact ``in`` test is too strict to find real targets, and a fuzzy match
is too loose to be trusted with a legal document. The middle is this:
normalize runs of whitespace to a single space for *comparison*, but keep an
index back to the raw offsets so the writer edits exactly the characters the
match covered. Nothing else is normalized — case, punctuation and wording are
compared literally, because "shall" and "should" differ by one letter and by
everything that matters.
"""
from __future__ import annotations


def normalize(text: str) -> str:
    """Collapse whitespace runs to single spaces and strip the ends."""
    return " ".join(text.split())


def _normalized_with_offsets(text: str) -> tuple[str, list[int]]:
    """Return the normalized text and, per normalized char, its raw index.

    The offset list has one entry per character of the returned string, so a
    normalized span ``[i, j)`` maps to raw ``[offsets[i], offsets[j - 1] + 1)``.
    """
    out: list[str] = []
    offsets: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        if char.isspace():
            in_space = True
            continue
        if in_space and out:
            out.append(" ")
            # The synthesized space stands for the whitespace run that ended
            # here; anchor it at this character so a span starting on it
            # never reaches backwards past the run.
            offsets.append(index)
        in_space = False
        out.append(char)
        offsets.append(index)
    return "".join(out), offsets


def contains(haystack: str, needle: str) -> bool:
    """Whether ``needle`` appears in ``haystack`` ignoring whitespace shape."""
    if not needle:
        return False
    if needle in haystack:
        return True
    return normalize(needle) in normalize(haystack)


def count_occurrences(haystack: str, needle: str) -> int:
    """How many times ``needle`` appears in ``haystack``, whitespace-tolerantly.

    Counted in the normalized space, which is the conservative choice: an
    exact match is always also a normalized match, so normalized counting can
    only ever find *more* candidate targets, and finding more is what makes
    the caller refuse. Used to detect an ambiguity an element id cannot
    resolve — an id names a paragraph, not which occurrence inside it.
    """
    if not needle:
        return 0
    normalized_needle = normalize(needle)
    if not normalized_needle:
        return 0
    return normalize(haystack).count(normalized_needle)


def find_span(haystack: str, needle: str) -> tuple[int, int] | None:
    """Raw ``[start, end)`` offsets of ``needle`` in ``haystack``, or ``None``.

    Tries an exact match first so the common case costs one ``str.find`` and
    returns offsets that are exact by construction; falls back to the
    normalized search with offset recovery.
    """
    if not needle:
        return None
    exact = haystack.find(needle)
    if exact != -1:
        return (exact, exact + len(needle))

    normalized_needle = normalize(needle)
    if not normalized_needle:
        return None
    normalized_haystack, offsets = _normalized_with_offsets(haystack)
    position = normalized_haystack.find(normalized_needle)
    if position == -1:
        return None
    start = offsets[position]
    end = offsets[position + len(normalized_needle) - 1] + 1
    return (start, end)
